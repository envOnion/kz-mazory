import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { createHmac, randomUUID } from 'node:crypto'
import type { Candidate, Page } from '../src/types/platform'
import type { Conversation, Artifact } from '../src/types/dialogue'

const enabled = process.env.MAZORY_E2E_MODEL === 'qwen3.8-27b'
test.skip(!enabled, 'Requires the isolated real Qwen provider')
test.setTimeout(1800000)
const dir = process.env.MAZORY_E2E_DIR!
interface Proof {
  messages: { id: number; message_id: string; processed: boolean }[]
  commitments: { id: number; commitment_text: string; status: string; source_message_id: number; deadline_at: string; fulfilled_at: string | null }[]
}
const proof = (): Proof => JSON.parse(readFileSync(join(dir, 'laya-proof.json'), 'utf8'))
function control(extra: Record<string, unknown>) {
  const file = join(dir, 'laya-control.json'), temporary = `${file}.${randomUUID()}`
  writeFileSync(temporary, JSON.stringify({ nonce: randomUUID(), mode: 'laya', live_crm: false, ...extra }))
  renameSync(temporary, file)
}

test('two actions in one original stay distinct, repetition is deduplicated and only the completed action closes', async ({ request, page, context }) => {
  const session: { access: string; refresh: string; cookie_name: string } = JSON.parse(readFileSync(join(dir, 'laya-session.json'), 'utf8'))
  const headers = { Authorization: `Bearer ${session.access}` }
  control({})
  let sequence = 0
  async function send(body: string, timestamp: number) {
    const id = `laya-qwen-promises-${++sequence}`
    const data = JSON.stringify({ event: 'message', session: 'default', payload: {
      id, body, from: 'laya-synthetic-routing@g.us', participant: '979999@lid', notifyName: 'Синтетический менеджер', timestamp } })
    expect((await request.post('/api/whatsapp/webhook/', { data, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex') } })).ok()).toBe(true)
    await expect.poll(() => proof().messages.find(m => m.message_id === id)?.processed, { timeout: 1200000, intervals: [2000, 5000] }).toBe(true)
    return proof().messages.find(m => m.message_id === id)!.id
  }
  const original = 'По объекту Текстильный цех обещаю завтра отправить КП Алексею, а послезавтра передать таблицу Марине. Договор ещё не подписан, оплаты не было.'
  const timestamp = Date.UTC(2026, 7, 19, 6) / 1000
  const originalId = await send(original, timestamp)
  await expect.poll(() => proof().commitments.length, { timeout: 300000 }).toBe(2)
  await send('Повторяю: завтра отправлю КП Алексею, послезавтра передам таблицу Марине по объекту Текстильный цех.', timestamp + 60)
  await expect.poll(() => proof().commitments.length, { timeout: 300000 }).toBe(2)
  expect(proof().commitments.every(c => c.source_message_id === originalId)).toBe(true)
  await send('По объекту Текстильный цех КП Алексею отправил. Таблицу Марине пока не передал. Оплаты не было.', timestamp + 3 * 86400)
  await expect.poll(() => proof().commitments.find(c => /Алексе/i.test(c.commitment_text))?.status, { timeout: 300000 }).toBe('fulfilled')
  const commitments = proof().commitments
  expect(commitments).toHaveLength(2)
  const offer = commitments.find(c => /Алексе/i.test(c.commitment_text))!
  const table = commitments.find(c => /Марин/i.test(c.commitment_text))!
  expect(offer.status).toBe('fulfilled')
  expect(offer.deadline_at.slice(0, 10)).toBe('2026-08-20')
  expect(offer.fulfilled_at?.slice(0, 10)).toBe('2026-08-22')
  expect(table.status).toBe('pending')
  expect(table.deadline_at.slice(0, 10)).toBe('2026-08-21')
  const facts: Page<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
  expect(facts.results.filter(c => c.chat_name === 'LAYA synthetic routing' && c.fact_type === 'payment')).toHaveLength(0)
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  await page.goto('/')
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Обязательства', exact: true }).click()
  await expect(page.getByText(offer.commitment_text, { exact: true })).toBeVisible()
  await expect(page.getByText(table.commitment_text, { exact: true })).toBeVisible()
  await page.screenshot({ path: join(dir, 'laya-results/qwen-distinct-promises.png'), fullPage: true })
  writeFileSync(join(dir, 'laya-results/qwen-distinct-promises.json'), JSON.stringify({ original, originalId, commitments, proof: proof() }, null, 2))
})

test('real reasoning and tool token counts survive provider restart, and the browser receives a grounded CRM chart', async ({ request, page, context }) => {
  let previousTask: string | undefined
  try { previousTask = JSON.parse(readFileSync(join(dir, 'qwen-canary-task.json'), 'utf8')).id } catch { /* No earlier probe. */ }
  control({ verify_qwen: true })
  await expect.poll(() => {
    try {
      const task: { id: string } = JSON.parse(readFileSync(join(dir, 'qwen-canary-task.json'), 'utf8'))
      const result: { task_id: string; success: boolean } = JSON.parse(readFileSync(join(dir, 'qwen-canary-result.json'), 'utf8'))
      return task.id !== previousTask && result.task_id === task.id && result.success
    } catch { return false }
  }, { timeout: 300000 }).toBe(true)
  const canary = JSON.parse(readFileSync(join(dir, 'qwen-canary-result.json'), 'utf8'))
  expect(canary.result).toMatchObject({ model: 'qwen3.8-27b', native_count_matches: true, native_tool_count_matches: true, answer: 391 })
  const session: { access: string; refresh: string; cookie_name: string; user_id: number } = JSON.parse(readFileSync(join(dir, 'analytics-session.json'), 'utf8'))
  const headers = { Authorization: `Bearer ${session.access}` }
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  await page.goto('/')
  await page.getByTestId('nav-kpi-dashboard').waitFor()
  const newDialogue = page.getByRole('button', { name: 'Новый диалог', exact: true })
  if (await newDialogue.count()) await newDialogue.click()
  const analyticalPrompt = page.locator('#analytical-prompt')
  const input = await analyticalPrompt.count() ? analyticalPrompt : page.getByPlaceholder('Спросите Mazory...')
  await input.fill('Покажи график количества сделок CRM по текущим стадиям за всё доступное время.')
  await input.press('Enter')
  await expect.poll(() => page.evaluate((id: number) => typeof JSON.parse(localStorage.getItem(`mazory-conversation-${id}`) || 'null') === 'number', session.user_id)).toBe(true)
  const id = await page.evaluate((uid: number): number => {
    const value: unknown = JSON.parse(localStorage.getItem(`mazory-conversation-${uid}`)!)
    if (typeof value !== 'number') throw new Error('Conversation ID missing')
    return value
  }, session.user_id)
  let conversation: Conversation
  await expect.poll(async () => {
    conversation = await (await request.get(`/api/chat/conversations/${id}/`, { headers })).json()
    return conversation.turns[0]?.state
  }, { timeout: 900000, intervals: [2000, 5000] }).toBe('succeeded')
  conversation = await (await request.get(`/api/chat/conversations/${id}/`, { headers })).json()
  expect(conversation.turns[0]!.artifacts).toHaveLength(1)
  const artifact: Artifact = await (await request.get(`/api/chat/artifacts/${conversation.turns[0]!.artifacts[0]!.id}/`, { headers })).json()
  expect(Object.values(artifact.presentation!.datasets)[0]!.rows.reduce((total, row) => total + (typeof row.project_count === 'number' ? row.project_count : 0), 0)).toBe(3)
  await expect(page.getByTestId('presentation')).toBeVisible()
  await page.screenshot({ path: join(dir, 'laya-results/qwen-analytics.png'), fullPage: true })
  writeFileSync(join(dir, 'laya-results/qwen-analytics.json'), JSON.stringify({ canary, conversation, artifact }, null, 2))
})

test('two explicitly separate receipts with the same company, date and amount stay separate', async ({ request, page, context }) => {
  const session: { access: string; refresh: string; cookie_name: string } = JSON.parse(readFileSync(join(dir, 'laya-session.json'), 'utf8'))
  const headers = { Authorization: `Bearer ${session.access}` }
  control({})
  const timestamp = Date.UTC(2026, 7, 23, 6) / 1000
  const bodies = [
    'По объекту Склад Север от компании Сфера сегодня фактически поступило 500000 тенге на расчётный счёт.',
    'По объекту Склад Север от компании Сфера сегодня фактически поступил ещё один отдельный платёж 500000 тенге на расчётный счёт. Это второе поступление, не повтор первого.'
  ]
  for (const [index, body] of bodies.entries()) {
    const id = `laya-qwen-receipts-${index+1}`
    const data = JSON.stringify({ event: 'message', session: 'default', payload: { id, body, from: 'laya-synthetic-baseline@g.us', participant: '979999@lid', notifyName: 'Синтетический менеджер', timestamp: timestamp+index*60 } })
    expect((await request.post('/api/whatsapp/webhook/', { data, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex') } })).ok()).toBe(true)
    await expect.poll(() => proof().messages.find(m=>m.message_id===id)?.processed, { timeout:1200000, intervals:[2000,5000] }).toBe(true)
  }
  const facts: Candidate[]=[]
  for (const status of ['pending','approved']) {
    let next: string | null = `/api/candidates/?status=${status}`
    while (next) {
      const result: Page<Candidate> = await (await request.get(next,{headers})).json()
      facts.push(...result.results.filter(c=>c.chat_name==='LAYA synthetic baseline' && c.fact_type==='payment'))
      next=result.next
    }
  }
  expect(facts).toHaveLength(2)
  expect(facts.every(c=>c.proposed_changes.amount==='500000.00' && c.proposed_changes.currency==='KZT' && c.proposed_changes.payment_date==='2026-08-23')).toBe(true)
  await context.addCookies([{ name:session.cookie_name,value:session.refresh,url:`${process.env.MAZORY_E2E_URL}/api/auth/`,httpOnly:true,sameSite:'Lax' }])
  await page.goto('/')
  await page.getByRole('button',{name:'Рабочий кабинет',exact:true}).click()
  await page.getByRole('button',{name:'Проверка фактов',exact:true}).click()
  await expect(page.getByText('Проверка фактов',{exact:true}).first()).toBeVisible()
  writeFileSync(join(dir,'laya-results/qwen-distinct-receipts.json'),JSON.stringify({bodies,facts,proof:proof()},null,2))
})
