import { test, expect, type APIRequestContext, type BrowserContext } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { createHmac } from 'node:crypto'
import type { Candidate, Directory, Page } from '../src/types/platform'
import type { DialogueThread } from '../src/types/factReview'

const directory = process.env.MAZORY_E2E_DIR
if (!directory) throw new Error('Use backend/e2e/run.py to start the isolated local stack')
const session: { access: string; refresh: string; cookie_name: string; config_id: number } = JSON.parse(readFileSync(join(directory, 'session.json'), 'utf8'))
const headers = { Authorization: `Bearer ${session.access}` }
test.describe.configure({ mode: 'serial' })

async function authenticate(context: BrowserContext) {
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
}
async function candidates(request: APIRequestContext, status = 'pending'): Promise<Page<Candidate>> {
  const response = await request.get(`/api/candidates/?status=${status}`, { headers }); expect(response.ok()).toBe(true); return response.json()
}
async function message(request: APIRequestContext, id: string, body: string, duplicate = false) {
  const data = JSON.stringify({ event: 'message', session: 'default', payload: { id, body, from: 'fixture@g.us', participant: '79990000002@c.us', notifyName: 'Боб', timestamp: Math.floor(Date.now() / 1000) - 10 } })
  const response = await request.post('/api/whatsapp/webhook/', { data, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex') } })
  expect(response.status()).toBe(duplicate ? 200 : 202)
}
async function themes(request: APIRequestContext): Promise<Page<DialogueThread>> {
  const response = await request.get('/api/threads/', { headers }); expect(response.ok()).toBe(true); return response.json()
}

// One browser session shares the real refresh rotation across serial scenarios.
test('CRM catalog, incomplete thought, interleaved themes and review through real Q2', async ({ page, context, request }) => {
  await authenticate(context)
  await page.goto('/')
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Проверка фактов', exact: true }).click()
  await expect.poll(async () => {
    const response = await request.get('/api/directory/', { headers }); const value: Directory = await response.json(); return value.projects_count
  }).toBe(2)
  await page.getByRole('button', { name: 'Обновить справочник', exact: true }).click()
  await expect(page.getByText('Справочник CRM загружен', { exact: false })).toBeVisible()
  await message(request, 'north-request', 'БЦ Север: подготовь смету')
  await message(request, 'south-request', 'БЦ Южный: проверь доставку')
  await message(request, 'north-question', 'Сделаешь завтра?')
  await expect.poll(async () => (await themes(request)).count).toBe(2)
  expect((await candidates(request)).count).toBe(0)
  await message(request, 'north-promise', 'БЦ Север: да, подготовлю смету завтра')
  await expect.poll(async () => (await candidates(request)).count).toBe(1)
  const fact = (await candidates(request)).results[0]!
  expect(fact.project_name).toBe('БЦ Север')
  expect(fact.proposed_changes.commitment_text).toBe('Подготовить смету БЦ Север')
  const response = await request.get(`/api/threads/${fact.thread!.id}/`, { headers })
  const thread: DialogueThread = await response.json()
  expect(thread.messages.map(row => row.content)).not.toContain('БЦ Южный: проверь доставку')
  // Duplicate transport delivery cannot create another task or processing lineage.
  await message(request, 'north-promise', 'БЦ Север: да, подготовлю смету завтра', true)
  expect((await candidates(request)).count).toBe(1)
  await page.getByRole('button', { name: 'Темы переписки', exact: true }).click()
  await expect(page.getByText('Доставка БЦ Южный', { exact: false })).toBeVisible()
  await expect(page.getByText('Мысль продолжается', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: 'Проверка фактов', exact: true }).click()
  await expect(page.getByText('Подготовить смету БЦ Север', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Показать переписку и контекст анализа' }).click()
  const thematic = page.getByRole('region', { name: 'Тематический тред' })
  await expect(thematic.getByText('БЦ Север: подготовь смету', { exact: true })).toBeVisible()
  await expect(thematic.getByText('БЦ Южный: проверь доставку', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Подтвердить факт', exact: true }).click()
  await expect(page.getByText('Факт подтверждён', { exact: true })).toBeVisible()
  await expect.poll(async () => (await candidates(request, 'approved')).count).toBe(1)
  await page.getByRole('button', { name: 'Обязательства', exact: true }).click()
  await expect(page.getByText('Подготовить смету БЦ Север', { exact: true })).toBeVisible()
  // Reprocessing history preserves the approved obligation, retaining the open south theme.
  const rebuild = await request.post('/api/threads/backfill/', { headers, data: { config_id: session.config_id, request_key: 'local-rebuild' } })
  expect(rebuild.status()).toBe(202)
  await expect.poll(async () => {
    const health = await request.get('/api/operations-health/', { headers }); const value = await health.json(); return value.outbox.filter((row: { event_type: string; state: string; count: number }) => ['thread_backfill', 'extract_message'].includes(row.event_type) && ['pending', 'enqueued', 'processing'].includes(row.state)).reduce((sum: number, row: { count: number }) => sum + row.count, 0)
  }, { timeout: 45000 }).toBe(0)
  expect((await candidates(request)).count).toBe(0)
  expect((await candidates(request, 'approved')).count).toBe(1)
  await page.screenshot({ path: join(directory, 'desktop-review.png'), fullPage: true })
})

test('CRM error is shown separately and retry preserves imported identities', async ({ request }) => {
  writeFileSync(join(directory, 'provider-state.json'), JSON.stringify({ crm_error: true }))
  expect((await request.post('/api/directory/crm-sync/', { headers, data: { team_id: 1 } })).status()).toBe(202)
  await expect.poll(async () => { const response = await request.get('/api/directory/', { headers }); const value: Directory = await response.json(); return value.crm_catalog?.[0]?.state }).toBe('error')
  writeFileSync(join(directory, 'provider-state.json'), '{}')
  expect((await request.post('/api/directory/crm-sync/', { headers, data: { team_id: 1 } })).status()).toBe(202)
  await expect.poll(async () => { const response = await request.get('/api/directory/', { headers }); const value: Directory = await response.json(); return value.crm_catalog?.[0]?.state }).toBe('succeeded')
  const response = await request.get('/api/directory/', { headers }); const value: Directory = await response.json()
  expect(value.projects_count).toBe(2)
})

test('general obligation needs no project and survives another topic', async ({ request }) => {
  await message(request, 'general-request', 'Подготовь список объектов команды')
  await message(request, 'general-promise', 'Да, подготовлю список объектов завтра')
  await expect.poll(async () => (await candidates(request)).count).toBe(1)
  const item = (await candidates(request)).results[0]!
  expect(item.project_id).toBeNull()
  expect(item.proposed_changes.commitment_text).toBe('Подготовить список объектов команды')
  const response = await request.post(`/api/candidates/${item.id}/review/`, { headers, data: { action: 'approve', base_version: 0, changes: {}, reason: '' } })
  expect(response.status()).toBe(200)
  expect((await candidates(request, 'approved')).count).toBe(2)
})


test('new project requires completed CRM search and sends identity without fabricated finances', async ({ request }) => {
  await message(request, 'east-project', 'Новый объект БЦ Восток')
  await expect.poll(async () => (await candidates(request)).count).toBe(1)
  await expect.poll(async () => (await candidates(request)).results[0]?.crm_resolution.state).toBe('not_found')
  const item = (await candidates(request)).results[0]!
  expect(item.fact_type).toBe('project')
  expect(item.proposed_changes.contract_amount).toBeUndefined()
  const response = await request.post(`/api/candidates/${item.id}/review/`, { headers, data: { action: 'approve', base_version: 0, changes: {}, reason: '' } })
  expect(response.status()).toBe(200)
  await expect.poll(() => JSON.parse(readFileSync(join(directory, 'provider-state.json'), 'utf8')).writes?.length).toBe(1)
  const fields: Record<string, unknown> = JSON.parse(readFileSync(join(directory, 'provider-state.json'), 'utf8')).writes[0]
  expect(fields.TITLE).toBe('БЦ Восток')
  expect(fields.OPPORTUNITY).toBeUndefined()
  const catalog: Directory = await (await request.get('/api/directory/', { headers })).json()
  expect(catalog.projects_count).toBe(3)
})
