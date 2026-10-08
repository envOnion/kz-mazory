import { test, expect, type BrowserContext, type Page, type APIRequestContext } from '@playwright/test'
import { readFileSync, writeFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import type { Conversation, TurnReceipt, Artifact } from '../src/types/dialogue'
import type { Operation } from '../src/types/platform'
const directory = process.env.MAZORY_E2E_DIR
if (!directory) throw new Error('Run the isolated Docker Compose E2E stack')
const session: { access: string; refresh: string; cookie_name: string; user_id: number } = JSON.parse(readFileSync(join(directory, 'analytics-session.json'), 'utf8'))
let refresh = session.refresh
const headers = { Authorization: `Bearer ${session.access}` }
test.describe.configure({ mode: 'serial' })
test.setTimeout(180000)
async function authenticate(context: BrowserContext) {
  context.on('response', async response => {
    if (response.url().includes('/api/auth/refresh/') && response.request().method() === 'POST' && response.ok()) {
      const cookies = await context.cookies(); const cookie = cookies.find(c => c.name === session.cookie_name)
      if (cookie) refresh = cookie.value
    }
  })
  await context.addCookies([{ name: session.cookie_name, value: refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
}
async function submit(page: Page, prompt: string) {
  const textarea = page.locator('#analytical-prompt')
  if (await textarea.count()) { await textarea.fill(prompt); await textarea.press('Enter') }
  else { const input = page.getByPlaceholder('Спросите Mazory...'); await input.fill(prompt); await input.press('Enter') }
}
async function startDialogue(page: Page) {
  await page.goto('/'); await page.getByTestId('nav-kpi-dashboard').waitFor()
  const button = page.getByRole('button', { name: 'Новый диалог', exact: true })
  if (await button.count()) await button.click()
}
async function conversation(request: APIRequestContext, id: number): Promise<Conversation> {
  const response = await request.get(`/api/chat/conversations/${id}/`, { headers }); expect(response.ok()).toBe(true); return response.json()
}
async function currentConversationId(page: Page): Promise<number> {
  await expect.poll(() => page.evaluate((userId: number) => typeof JSON.parse(localStorage.getItem(`mazory-conversation-${userId}`) || 'null') === 'number', session.user_id)).toBe(true)
  return page.evaluate((userId: number) => { const value: unknown = JSON.parse(localStorage.getItem(`mazory-conversation-${userId}`) || 'null'); if (typeof value !== 'number') throw new Error('Conversation ID missing'); return value }, session.user_id)
}

test('chart descriptor in model prose is corrected through real tools before a workspace result is shown', async ({ page, context, request }) => {
  await authenticate(context); await startDialogue(page)
  await submit(page, 'Придумай сам, чтобы красивый график вывести')
  const id = await currentConversationId(page), turn = await waitTurn(request, id, 1)
  expect(turn.artifacts).toHaveLength(1)
  await expect(page.getByTestId('presentation')).toBeVisible()
  await expect(page.getByTestId('chat-response')).not.toContainText('"blocks"')
  await expect(page.getByTestId('chat-response')).not.toContainText('dataset_id')
  const artifact: Artifact = await (await request.get(`/api/chat/artifacts/${turn.artifacts[0]!.id}/`, { headers })).json()
  expect(Object.values(artifact.presentation!.datasets)[0]!.rows.reduce((total, row) => total + (typeof row.project_count === 'number' ? row.project_count : 0), 0)).toBe(3)
  await page.screenshot({ path: join(directory, 'playwright/chart-prose-corrected.png'), fullPage: true })
})
async function waitTurn(request: APIRequestContext, id: number, sequence: number) {
  await expect.poll(async () => (await conversation(request, id)).turns.find(t => t.sequence === sequence)?.state).toBe('succeeded')
  return (await conversation(request, id)).turns.find(t => t.sequence === sequence)!
}
test('invalid CRM columns and historical period are repaired before a real chart is published', async ({ page, context, request }) => {
  await authenticate(context); await startDialogue(page)
  await submit(page, 'Проверь поля CRM и покажи график сделок по стадиям')
  const id = await currentConversationId(page), turn = await waitTurn(request, id, 1)
  expect(turn.artifacts).toHaveLength(1)
  const artifact: Artifact = await (await request.get(`/api/chat/artifacts/${turn.artifacts[0]!.id}/`, { headers })).json()
  const dataset = Object.values(artifact.presentation!.datasets)[0]!
  expect(dataset.rows.reduce((total, row) => total + (typeof row.project_count === 'number' ? row.project_count : 0), 0)).toBe(3)
  expect(dataset.normalized_query.dimensions).toEqual(['status'])
  await expect(page.getByTestId('presentation')).toBeVisible()
  await expect(page.getByTestId('chat-response')).not.toContainText('unsupported_query')
})
function control(patch: Record<string, unknown>) {
  const path = join(directory, 'provider-state.json'), temporary = `${path}.${randomUUID()}.tmp`
  writeFileSync(temporary, JSON.stringify({ ...JSON.parse(readFileSync(path, 'utf8')), ...patch })); renameSync(temporary, path)
}

test('large overdue records use provider token counts, compact previews and retain the complete chart scope', async ({ page, context, request }) => {
  await authenticate(context); await startDialogue(page)
  await submit(page, 'Какие обещания просрочены? Покажи список без служебного кода.')
  const id = await currentConversationId(page), listed = await waitTurn(request, id, 1)
  const records: Artifact = await (await request.get(`/api/chat/artifacts/${listed.artifacts[0]!.id}/`, { headers })).json()
  expect(Object.values(records.presentation!.datasets)[0]!.rows).toHaveLength(50)
  expect(listed.answer_document!.markdown).toContain('50')
  control({ analytics_force_compaction: true })
  try {
    await submit(page, 'Какие обещания просрочены?')
    const compacted = await waitTurn(request, id, 2)
    const full: Artifact = await (await request.get(`/api/chat/artifacts/${compacted.artifacts[0]!.id}/`, { headers })).json()
    expect(Object.values(full.presentation!.datasets)[0]!.rows).toHaveLength(50)
    expect(compacted.answer_document!.markdown).toContain('50')
  } finally { control({ analytics_force_compaction: false }) }
  await submit(page, 'построй график')
  const chart = await waitTurn(request, id, 3)
  const aggregated: Artifact = await (await request.get(`/api/chat/artifacts/${chart.artifacts[0]!.id}/`, { headers })).json()
  expect(Object.values(aggregated.presentation!.datasets)[0]!.rows[0]!.commitment_count).toBe(50)
  await expect(page.getByTestId('presentation')).toBeVisible()
  await expect(page.getByTestId('chat-turn-3')).toContainText('50')
})

test('analytical dialogue: real facts, controls, plan/fact, history, mobile and selectors', async ({ page, context, request }) => {
  await authenticate(context); await page.goto('/'); await page.getByTestId('nav-kpi-dashboard').waitFor()
  await submit(page, 'Йо')
  await expect(page.getByTestId('chat-response')).toContainText('Привет!')
  await expect(page.getByTestId('chat-response').locator('strong')).toContainText('Помогу с аналитикой.')
  await expect(page.getByTestId('chat-response')).not.toContainText('**')
  const id = await currentConversationId(page)
  await submit(page, 'Покажи сделки CRM по стадиям')
  await waitTurn(request, id, 2)
  await expect(page.getByTestId('presentation')).toBeVisible()
  await expect(page.getByTestId('chat-turn-2')).toContainText('3')
  await expect(page.locator('.chat-shell')).not.toContainText('project_count')
  await page.getByRole('button', { name: 'Таблица', exact: true }).click()
  await expect(page.getByRole('columnheader', { name: 'Количество сделок' })).toBeVisible()
  await page.getByLabel('Группировка', { exact: true }).selectOption('manager')
  await waitTurn(request, id, 3)
  await expect(page.getByTestId('chat-turn-3')).toContainText('Борис')
  await page.getByLabel('Сортировка', { exact: true }).selectOption('value_desc')
  await waitTurn(request, id, 4)
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click()
  await submit(page, 'Покажи поступления с августа по сентябрь 2026 по месяцам')
  const paymentId = await currentConversationId(page)
  const monthly = await waitTurn(request, paymentId, 1)
  await expect(page.getByTestId('chat-turn-1')).toContainText('290,00 KZT')
  await page.getByLabel('Группировка', { exact: true }).selectOption('week')
  const weekly = await waitTurn(request, paymentId, 2)
  const weeklyArtifact = await request.get(`/api/chat/artifacts/${weekly.artifacts[0]!.id}/`, { headers })
  const weeklyData: Artifact = await weeklyArtifact.json()
  expect(Object.values(weeklyData.presentation!.datasets)[0]!.normalized_query.date_range).toEqual({ start: '2026-08-01', end_exclusive: '2026-10-01' })
  await page.getByLabel('Сортировка', { exact: true }).selectOption('date_desc')
  await waitTurn(request, paymentId, 3)
  await submit(page, 'Добавь план')
  await waitTurn(request, paymentId, 4)
  await expect(page.getByTestId('chat-turn-4')).toContainText('Перейти к месяцам')
  await page.getByTestId('chat-turn-1').getByRole('button', { name: 'Поступления', exact: true }).click()
  await submit(page, 'Добавь месячный план к этому графику')
  const combined = await waitTurn(request, paymentId, 5)
  const combinedResponse = await request.get(`/api/chat/artifacts/${combined.artifacts[0]!.id}/`, { headers })
  const combinedArtifact: Artifact = await combinedResponse.json()
  const combinedDs = Object.values(combinedArtifact.presentation!.datasets)[0]!
  expect(combinedDs.rows.filter(r => r.series === 'Поступления').map(r => r.value)).toEqual(['100.00', '190.00'])
  expect(combinedDs.rows.filter(r => r.series === 'План').map(r => r.value)).toEqual(['150.00', '250.00'])
  await page.reload(); await expect(page.getByTestId('chat-turn-5')).toBeVisible()
  const secondTab = await context.newPage(); await secondTab.goto('/'); await expect(secondTab.getByTestId('chat-turn-5')).toBeVisible(); await secondTab.close()
  await page.getByTestId('chat-turn-1').getByRole('button', { name: 'Поступления', exact: true }).click()
  await expect(page.getByTestId('presentation')).toBeVisible()
  for (const width of [320, 390, 736, 1024, 1280]) {
    await page.setViewportSize({ width, height: 960 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const columns = await page.locator('.workspace-layout').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length)
    expect(columns).toBe(width < 1024 ? 1 : 2)
    const inset = await page.getByLabel('Группировка', { exact: true }).evaluate(el => {
      const wrapper = el.parentElement!, arrow = wrapper.querySelector('svg')!
      return wrapper.getBoundingClientRect().right - arrow.getBoundingClientRect().right
    })
    expect(inset).toBe(14)
  }
  await page.screenshot({ path: join(directory, 'playwright/analytics-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 }); await page.screenshot({ path: join(directory, 'playwright/analytics-mobile.png'), fullPage: true })
  expect(monthly.artifacts[0]!.id).not.toBe(combined.artifacts[0]!.id)
})

test('category and period combinations, derived percentages and individually selected charts', async ({ page, context, request }) => {
  await authenticate(context); await startDialogue(page)
  await submit(page, 'Объедини категории CRM в общую группу')
  let id = await currentConversationId(page), turn = await waitTurn(request, id, 1)
  let result: Artifact = await (await request.get(`/api/chat/artifacts/${turn.artifacts[0]!.id}/`, { headers })).json()
  expect(Object.values(result.presentation!.datasets)[0]!.rows).toEqual([{ status: 'Общая группа', project_count: 3 }])
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click()
  await submit(page, 'Объедини август и сентябрь в график поступлений')
  id = await currentConversationId(page); turn = await waitTurn(request, id, 1)
  result = await (await request.get(`/api/chat/artifacts/${turn.artifacts[0]!.id}/`, { headers })).json()
  expect(Object.values(result.presentation!.datasets)[0]!.rows.map(r => r.received_amount)).toEqual(['100.00', '190.00'])
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click()
  await submit(page, 'Покажи план и поступления за август–сентябрь и процент выполнения')
  id = await currentConversationId(page); await waitTurn(request, id, 1)
  await expect(page.getByTestId('chat-response')).toContainText('72,50 %')
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click()
  await submit(page, 'Покажи два графика: поступления и сделки CRM')
  id = await currentConversationId(page); turn = await waitTurn(request, id, 1)
  expect(turn.artifacts).toHaveLength(2)
  const paymentArtifact = turn.artifacts[0]!.id, dealArtifact = turn.artifacts[1]!.id
  await page.getByTestId('chat-turn-1').getByRole('button', { name: 'Сделки CRM', exact: true }).click()
  await page.getByLabel('Группировка', { exact: true }).selectOption('manager')
  const revision = await waitTurn(request, id, 2)
  result = await (await request.get(`/api/chat/artifacts/${revision.artifacts[0]!.id}/`, { headers })).json()
  expect(result.parent_artifact_id).toBe(dealArtifact)
  const original: Artifact = await (await request.get(`/api/chat/artifacts/${paymentArtifact}/`, { headers })).json()
  expect(Object.values(original.presentation!.datasets)[0]!.normalized_query.dimensions).toEqual(['payment_month'])
  control({ analytics_operation_expire: turn.operation_id })
  await expect.poll(async () => (await (await request.get(`/api/operations/${turn.operation_id}/`, { headers })).json()).status).toBe('expired')
  control({ analytics_operation_expire: false })
  await page.getByTestId('chat-turn-1').getByRole('button', { name: 'Сделки CRM', exact: true }).click()
  await page.getByLabel('Группировка', { exact: true }).selectOption('manager')
  await waitTurn(request, id, 3)
})

test('queued refinements, cancellation, join cardinality and grounded fallback use the real worker', async ({ page, context, request }) => {
  await authenticate(context); await startDialogue(page)
  control({ analytics_delay: 5 })
  await submit(page, 'Покажи график поступлений')
  const id = await currentConversationId(page)
  await expect(page.getByTestId('chat-turn-1')).toContainText('Покажи график поступлений')
  await submit(page, 'Уточни график поступлений по неделям')
  await expect(page.getByTestId('chat-turn-2')).toContainText('Жду завершения')
  await page.getByTestId('chat-turn-1').getByRole('button', { name: 'Отменить', exact: true }).click()
  await expect.poll(async () => (await conversation(request, id)).turns.map(t => t.state)).toEqual(['cancelled', 'cancelled'])
  control({ analytics_delay: 0 })
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click()
  await submit(page, 'Объедини договоры и поступления по проектам')
  const joinedId = await currentConversationId(page), joined = await waitTurn(request, joinedId, 1)
  const response = await request.get(`/api/chat/artifacts/${joined.artifacts[0]!.id}/`, { headers }), artifact: Artifact = await response.json()
  const rows = Object.values(artifact.presentation!.datasets)[0]!.rows
  expect(rows.map(r => r.contract_amount)).toEqual(['1000.00', '2000.00', '3000.00'])
  expect(rows.filter(r => r.received_amount !== null).map(r => r.received_amount)).toEqual(['100.00', '190.00'])
  await submit(page, 'Покажи график поступлений fallback')
  await waitTurn(request, joinedId, 2)
  await expect(page.getByTestId('chat-turn-2')).toContainText('290,00 KZT')
  await expect(page.getByTestId('chat-turn-2')).not.toContainText('999999')
  control({ analytics_narrative_error: true })
  await submit(page, 'Покажи график поступлений при сбое пояснения')
  await waitTurn(request, joinedId, 3)
  await expect(page.getByTestId('chat-turn-3')).toContainText('290,00 KZT')
  await expect(page.getByTestId('presentation')).toBeVisible()
  control({ analytics_narrative_error: false })
  const invalid = await request.post(`/api/chat/conversations/${joinedId}/turns/`, { headers, data: { prompt: 'Подмена', idempotency_key: randomUUID(), parent_turn_id: 999999 } })
  expect(invalid.status()).toBe(404)
  const operation: Operation<unknown> = await (await request.get(`/api/operations/${joined.operation_id}/`, { headers })).json()
  expect(operation.status).toBe('succeeded')
})

test('full totals before top-N, safe Markdown and revoked or expired history', async ({ page, context, request }) => {
  await authenticate(context); await startDialogue(page)
  await submit(page, 'Покажи график поступлений, топ 1 месяц')
  const id = await currentConversationId(page), turn = await waitTurn(request, id, 1)
  const response = await request.get(`/api/chat/artifacts/${turn.artifacts[0]!.id}/`, { headers }), artifact: Artifact = await response.json()
  const ds = Object.values(artifact.presentation!.datasets)[0]!
  expect(ds.truncated).toBe(true); expect(ds.rows).toHaveLength(1); expect(ds.total_groups).toBe(2)
  await expect(page.getByTestId('chat-turn-1')).toContainText('290,00 KZT')
  const otherSession: { access: string } = JSON.parse(readFileSync(join(directory, 'session.json'), 'utf8'))
  expect((await request.get(`/api/chat/conversations/${id}/`, { headers: { Authorization: `Bearer ${otherSession.access}` } })).status()).toBe(404)
  await submit(page, 'Йо'); await waitTurn(request, id, 2)
  await expect(page.getByTestId('chat-turn-2')).toContainText('Привет!')
  await expect(page.getByTestId('presentation')).toHaveCount(1)
  control({ analytics_role: 'manager' })
  await expect.poll(async () => (await (await request.get(`/api/chat/artifacts/${artifact.id}/`, { headers })).json()).available).toBe(false)
  await expect(page.getByTestId('chat-turn-1')).not.toContainText('290,00 KZT')
  await expect(page.getByTestId('presentation')).toHaveCount(0)
  control({ analytics_role: 'team_lead' })
  await expect.poll(async () => (await (await request.get(`/api/chat/artifacts/${artifact.id}/`, { headers })).json()).available).toBe(true)
  control({ analytics_revoke: true })
  await expect.poll(async () => (await request.get(`/api/chat/artifacts/${artifact.id}/`, { headers })).status()).toBe(401)
  await expect(page.getByTestId('chat-response')).toHaveCount(0)
  await expect(page.getByTestId('presentation')).toHaveCount(0)
  control({ analytics_revoke: false })
  await expect.poll(async () => (await (await request.get(`/api/chat/artifacts/${artifact.id}/`, { headers })).json()).available).toBe(true)
  await authenticate(context); await page.goto('/')
  await expect(page.getByTestId('presentation')).toHaveCount(1)
  await page.getByTestId('chat-turn-2').getByRole('button', { name: 'Продолжить этот ответ', exact: true }).click()
  control({ analytics_expire: true })
  await expect.poll(async () => (await (await request.get(`/api/chat/artifacts/${artifact.id}/`, { headers })).json()).available).toBe(false)
  await expect(page.getByTestId('presentation')).toHaveCount(0)
  await expect(page.getByTestId('chat-turn-1')).not.toContainText('290,00 KZT')
  await expect(page.getByTestId('chat-turn-2')).toContainText('Привет!')
  control({ analytics_expire: false })
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click()
  await submit(page, 'Проверь форматирование')
  const formattedId = await currentConversationId(page); await waitTurn(request, formattedId, 1)
  await expect(page.getByTestId('chat-response').locator('strong')).toHaveText('Проверено')
  await expect(page.getByTestId('chat-response').locator('img, script, iframe, [onerror], a[href^="javascript:"]')).toHaveCount(0)
  expect(await page.evaluate(() => Object.prototype.hasOwnProperty.call(window, '__xss'))).toBe(false)
})
