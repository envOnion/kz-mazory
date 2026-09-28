import { test, expect } from '@playwright/test'
import { createHmac, randomUUID } from 'node:crypto'
import { login } from './auth-helper'
import type { Candidate, Page as ApiPage, Operation, OperationReceipt } from '../src/types/platform'
import type { ChatResponse } from '../src/types/chat'
const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'

test('Real UI: KPI, queued chat, all widget types and error state', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await login(page, '77000000002')
  await page.getByRole('button', { name: 'KPI и чат', exact: true }).click()
  await expect(page.getByText('Полная история', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Менеджер A', exact: true })).toBeVisible()
  await expect(page.getByText('E2E Private B')).toHaveCount(0)
  const input = page.getByPlaceholder('Спросите Mazory...')
  for (const [prompt, heading] of [['Покажи график', 'План и факт поступлений'], ['Покажи обещания', 'Контроль обещаний и дедлайнов (SLA)'], ['Покажи проекты', 'Воронка проектов и контроль экономики сделок']]) {
    await input.fill(prompt!); await input.press('Enter')
    await expect(page.getByRole('status').filter({ hasText: 'Обрабатываю' })).toBeVisible()
    await expect(page.getByRole('heading', { name: heading!, exact: true })).toBeVisible({ timeout: 30000 })
  }
  await input.fill('SIMULATE_AI_FAILURE'); await input.press('Enter')
  await expect(page.getByRole('alert')).toContainText('Не удалось обработать запрос', { timeout: 30000 })
  expect(errors).toEqual([])
  expect(await page.evaluate(() => localStorage.getItem('mazory_access_token'))).toBeNull()
})

test('Inbox → Q2 → AI proposal → human review → one payment and KPI delta', async ({ page, request }) => {
  const session = await login(page)
  const headers = { Authorization: `Bearer ${session.access}` }
  const before = await (await request.get('/api/kpi/summary/', { headers })).json()
  const body = JSON.stringify({ event: 'message', session: 'default', payload: { id: randomUUID(), from: 'e2e@g.us', participant: '77000000002@c.us', notifyName: 'Менеджер A', timestamp: Math.floor(Date.now() / 1000), body: `E2E payment: amount=250000.00 date=${new Date().toISOString().slice(0, 10)}` } })
  const signature = createHmac('sha512', 'isolated-test-webhook').update(body).digest('hex')
  const replies = await Promise.all(Array.from({ length: 10 }, () => request.post('/api/messages/ingest/', { data: body, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': signature } })))
  expect(replies.every(r => [200, 202].includes(r.status()))).toBeTruthy()
  let candidate: Candidate | undefined
  await expect.poll(async () => {
    const data: ApiPage<Candidate> = await (await request.get('/api/candidates/', { headers })).json()
    candidate = data.results.find(c => c.proposed_changes.amount === '250000.00')
    return candidate?.id
  }, { timeout: 30000 }).toBeTruthy()
  const unchanged = await (await request.get('/api/kpi/summary/', { headers })).json()
  expect(unchanged.fact).toBe(before.fact)
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Проверка фактов', exact: true }).click()
  const card = page.getByTestId(`candidate-${candidate!.id}`)
  await card.getByRole('button', { name: /Источник/ }).click()
  await expect(page.locator('dialog')).toContainText('E2E payment:')
  await page.locator('dialog').getByRole('button', { name: 'Закрыть' }).click()
  await card.getByRole('button', { name: 'Подтвердить факт' }).click()
  await expect(card).toHaveCount(0)
  const repeated = await request.post(`/api/candidates/${candidate!.id}/review/`, { headers, data: { action: 'approve', base_version: candidate!.base_version } })
  expect(repeated.status()).toBe(200)
  const after = await (await request.get('/api/kpi/summary/', { headers })).json()
  expect(Number(after.fact) - Number(before.fact)).toBe(250000)
  await page.getByRole('button', { name: 'Финансы', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Реестр платежей' })).toBeVisible()
  await expect(page.getByText('250000.00 KZT', { exact: true })).toBeVisible()
})

test('Profile is read-only for KPI; real session revocation clears the UI', async ({ page, request }) => {
  const session = await login(page, '77000000002')
  expect((await request.put('/api/profile/', { headers: { Authorization: `Bearer ${session.access}` }, data: { current_sales: '999999999' } })).status()).toBe(400)
  await page.getByRole('button', { name: 'Настройки профиля', exact: true }).click()
  await page.getByRole('button', { name: 'Безопасность и сессии', exact: true }).click()
  await expect(page.getByRole('button', { name: `Завершить сессию #${session.session_id}`, exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Завершить все сессии', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  expect((await request.get('/api/projects/', { headers: { Authorization: `Bearer ${session.access}` } })).status()).toBe(401)
  await expect(page.getByText('Менеджер A', { exact: true })).toHaveCount(0)
})

test('Recipient notification is durable, queued through worker and does not claim delivery', async ({ page, request }) => {
  const session = await login(page)
  const title = `Уведомление E2E ${randomUUID().slice(0, 8)}`
  const response = await request.post('/api/notifications/dispatch/', { headers: { Authorization: `Bearer ${session.access}` }, data: { recipient_id: session.user_id, title, message: 'Проверка безопасной доставки', send_whatsapp: true, idempotency_key: randomUUID() } })
  expect(response.status()).toBe(202)
  await expect.poll(async () => {
    const data = await (await request.get('/api/notifications/', { headers: { Authorization: `Bearer ${session.access}` } })).json()
    return data.notifications.find((n: { title: string }) => n.title === title)?.deliveries[0]?.state
  }, { timeout: 30000 }).toBe('sent')
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Уведомления', exact: true }).click()
  const card = page.locator('article').filter({ hasText: title })
  await expect(card).toContainText('WhatsApp: Отправлено')
  await card.getByRole('button', { name: 'Подтвердить получение' }).click()
  await expect(card.getByRole('button', { name: 'Подтвердить получение' })).toHaveCount(0)
  const captured = await (await request.get(`${provider}/test/messages`)).json()
  const providerMessage = captured.messages.find((item: { text: string }) => item.text.startsWith(title))
  const ackBody = JSON.stringify({ event: 'message.ack', session: 'default', payload: { id: providerMessage.id, fromMe: true, participant: null, ack: 2 } })
  const signature = createHmac('sha512', 'isolated-test-webhook').update(ackBody).digest('hex')
  expect((await request.post('/api/whatsapp/webhook/', { data: ackBody, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': signature } })).status()).toBe(202)
  await expect.poll(async () => {
    const data = await (await request.get('/api/notifications/', { headers: { Authorization: `Bearer ${session.access}` } })).json()
    return data.notifications.find((item: { title: string }) => item.title === title).deliveries[0].state
  }, { timeout: 30000 }).toBe('delivered')
  await page.getByRole('button', { name: 'Проекты', exact: true }).click()
  await page.getByRole('button', { name: 'Уведомления', exact: true }).click()
  await expect(card).toContainText('WhatsApp: Доставлено')
})

test('Client cabinet hides financial internals and AI access', async ({ page, request }) => {
  const session = await login(page, '77000000005')
  await expect(page.getByText('КАБИНЕТ КЛИЕНТА', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Финансы', exact: true })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'E2E Alpha' })).toBeVisible()
  expect((await request.get('/api/kpi/summary/', { headers: { Authorization: `Bearer ${session.access}` } })).status()).toBe(403)
  const data = await (await request.get('/api/projects/', { headers: { Authorization: `Bearer ${session.access}` } })).json()
  expect(data.results[0]).not.toHaveProperty('cost_amount')
  expect(data.results[0]).not.toHaveProperty('paid_amount')
})

test('OTP reaches isolated provider and logs in through real browser form', async ({ page, request }) => {
  const previous = await (await request.get(`${provider}/test/messages`)).json()
  const previousIds = new Set(previous.messages.map((item: { id: string }) => item.id))
  await page.goto('/')
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
  await page.locator('input[type=tel]').fill('77000000004')
  await page.getByRole('button', { name: /Получить код/ }).click()
  let code = ''
  await expect.poll(async () => {
    const data = await (await request.get(`${provider}/test/messages`)).json()
    const delivery = data.messages.findLast((m: { id: string; chatId: string; text: string }) => !previousIds.has(m.id) && m.chatId === '77000000004@c.us' && m.text.startsWith('Код входа Mazory'))
    code = delivery?.text.match(/\b\d{4}\b/)?.[0] || ''
    return code
  }, { timeout: 30000 }).toHaveLength(4)
  const inputs = page.locator('input[inputmode=numeric]')
  if (await inputs.count() === 4) {
    for (let i = 0; i < 4; i++) await inputs.nth(i).fill(code[i]!)
  } else { await inputs.fill(code) }
  const submit = page.getByRole('button', { name: /Подтвердить|Войти/ }).last()
  if (await submit.isVisible() && await submit.isEnabled()) await submit.click()
  await expect(page.getByRole('button', { name: 'Рабочий кабинет', exact: true })).toBeVisible()
  expect((await request.post('/api/auth/verify-code/', { data: { phone: '77000000004', code } })).status()).toBe(401)
})

test('Commitment help, postponement and completion persist through real UI', async ({ page, request }) => {
  const session = await login(page)
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Обязательства', exact: true }).click()
  const card = page.locator('article').filter({ hasText: 'E2E подготовить акт' })
  await card.getByPlaceholder('Причина переноса или запрос помощи').fill('Нужна проверка комплекта')
  await card.getByRole('button', { name: 'Нужна помощь' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Сохранено' })).toBeVisible()
  await expect(card.getByRole('button', { name: 'Выполнено' })).toBeVisible()
  const future = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 16)
  await card.getByLabel('Новый срок').fill(future)
  await card.getByPlaceholder('Причина переноса или запрос помощи').fill('Согласован новый срок')
  await card.getByRole('button', { name: 'Перенести', exact: true }).click()
  await expect(card).toContainText('Причина переноса: Согласован новый срок')
  await card.getByRole('button', { name: 'Выполнено' }).click()
  await expect(card.getByRole('button', { name: 'Выполнено' })).toHaveCount(0)
  const tasks = await (await request.get('/api/commitments/', { headers: { Authorization: `Bearer ${session.access}` } })).json()
  expect(tasks.commitments.find((task: { text: string }) => task.text === 'E2E подготовить акт').status_code).toBe('fulfilled')
})

test('Private document → OCR worker → publication → restricted client download', async ({ page, request, browser }) => {
  await login(page)
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Документы', exact: true }).click()
  await page.locator('form').getByRole('combobox').selectOption({ label: 'E2E Alpha' })
  await page.locator('input[type=file]').setInputFiles({ name: 'synthetic.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4\nSynthetic E2E document') })
  await page.getByRole('button', { name: /Загрузить/ }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Файл принят' })).toBeVisible()
  const ctx = await browser.newContext()
  const clientPage = await ctx.newPage()
  const clientSession = await login(clientPage, '77000000005')
  const headers = { Authorization: `Bearer ${clientSession.access}` }
  expect((await (await request.get('/api/attachments/', { headers })).json()).length).toBe(0)
  await page.getByRole('button', { name: 'Опубликовать клиенту' }).first().click()
  await expect(page.getByRole('button', { name: 'Скрыть от клиента' })).toBeVisible()
  let id = 0
  await expect.poll(async () => {
    const files = await (await request.get('/api/attachments/', { headers })).json()
    id = files[0]?.id || 0
    return files[0]?.state
  }, { timeout: 30000 }).toBe('ready')
  const transcript = await (await request.get(`/api/attachments/${id}/`, { headers })).json()
  expect(transcript.transcript).toContain('Изолированный тестовый документ')
  await clientPage.getByRole('button', { name: 'Документы', exact: true }).click()
  const download = clientPage.waitForEvent('download')
  await clientPage.getByRole('button', { name: /Скачать/ }).click()
  expect((await download).suggestedFilename()).toContain('document-')
  expect((await request.get(`/api/attachments/${id}/?download=1`)).status()).toBe(401)
  await ctx.close()
})
