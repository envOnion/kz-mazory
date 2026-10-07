import { test, expect, type APIRequestContext, type BrowserContext } from '@playwright/test'
import { readFileSync, writeFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { createHmac, randomUUID } from 'node:crypto'
import type { Candidate, Directory, Health, Page } from '../src/types/platform'
import type { DialogueThread } from '../src/types/factReview'
import type { McpConnectionResponse } from '../src/types/mcp'

const directory = process.env.MAZORY_E2E_DIR
if (!directory) throw new Error('Use backend/e2e/run.py to start the isolated local stack')
const session: { access: string; refresh: string; cookie_name: string; config_id: number } = JSON.parse(readFileSync(join(directory, 'session.json'), 'utf8'))
let currentRefresh = session.refresh
let currentAccess = session.access
const headers = { Authorization: `Bearer ${session.access}` }
test.describe.configure({ mode: 'serial' })

function writeProviderState(path: string, value: Record<string, unknown>) {
  const temporary = `${path}.${randomUUID()}.tmp`
  writeFileSync(temporary, JSON.stringify(value))
  renameSync(temporary, path)
}

test('cabinet credential survives reload and authorizes only fact-review MCP', async ({ page, context, request }) => {
  await authenticate(context)
  const openSecurity = async () => {
    await page.goto('/')
    await page.getByTestId('nav-profile').click()
    await page.getByRole('button', { name: 'Безопасность и сессии', exact: true }).click()
  }
  await openSecurity()
  await page.getByRole('button', { name: 'Получить токен MCP', exact: true }).click()
  const region = page.getByRole('region', { name: 'MCP проверки фактов' })
  const input = region.getByLabel('Персональный токен')
  await expect(input).toHaveAttribute('type', 'password')
  await expect(input).toHaveValue(/^mcp_/)
  const token = await input.inputValue()
  await openSecurity()
  await expect(input).toHaveValue(token)
  await page.screenshot({ path: join(directory, 'desktop-mcp.png'), fullPage: true })
  const mcpHeaders = { Authorization: `Bearer ${token}`, Accept: 'application/json, text/event-stream' }
  const call = async (method: string, params?: Record<string, unknown>) => request.post('/api/mcp/fact-review/', { headers: mcpHeaders, data: { jsonrpc: '2.0', id: 1, method, ...(params ? { params } : {}) } })
  const initialized = await call('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'local-cabinet-test', version: '1' } })
  expect(initialized.status()).toBe(200)
  const tools = await call('tools/list')
  expect(tools.status()).toBe(200)
  expect((await tools.json()).result.tools.map((tool: { name: string }) => tool.name)).toContain('get_candidate_context')
  expect((await request.get('/api/mcp/connection/', { headers: mcpHeaders })).status()).toBe(401)
  await region.getByRole('button', { name: 'Перевыпустить токен', exact: true }).click()
  await expect(input).not.toHaveValue(token)
  expect((await call('tools/list')).status()).toBe(401)
  const connection: McpConnectionResponse = await (await request.get('/api/mcp/connection/', { headers })).json()
  expect(connection.connection?.token).toBe(await input.inputValue())
  await region.getByRole('button', { name: 'Отозвать токен', exact: true }).click()
  await openSecurity()
  await expect(region.getByRole('button', { name: 'Получить токен MCP', exact: true })).toBeVisible()
  expect((await (await request.get('/api/mcp/connection/', { headers })).json()).connection).toBeNull()
})

async function authenticate(context: BrowserContext) {
  context.on('response', async res => {
    if (res.url().includes('/api/auth/refresh/') && res.request().method() === 'POST' && res.ok()) {
      try {
        const body = await res.json()
        if (body.access) {
          currentAccess = body.access
          headers.Authorization = `Bearer ${currentAccess}`
        }
      } catch {}
      const cookies = await context.cookies()
      const c = cookies.find(x => x.name === session.cookie_name)
      if (c) currentRefresh = c.value
    }
  })
  await context.addCookies([{ name: session.cookie_name, value: currentRefresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
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
async function waitForFactProcessing(request: APIRequestContext) {
  await expect.poll(async () => {
    const response = await request.get('/api/operations-health/', { headers })
    expect(response.ok()).toBe(true)
    const value: Health = await response.json()
    return value.outbox.filter(row =>
      ['thread_backfill', 'extract_message', 'crm_match'].includes(row.event_type)
      && ['pending', 'enqueued', 'processing'].includes(row.state)
    ).reduce((sum, row) => sum + row.count, 0)
  }, { timeout: 45000 }).toBe(0)
}

// One browser session shares the real refresh rotation across serial scenarios.
test('incoming WhatsApp message automatically creates a named silent employee through Q2', async ({ page, context, request }) => {
  await authenticate(context)
  await message(request, 'people-auto-trigger', 'Сделаешь завтра?')
  await expect.poll(async () => {
    const data: Directory = await (await request.get('/api/directory/', { headers })).json()
    return data.participants?.find(person => person.phone === '79990000003')?.user_id
  }, { timeout: 20000 }).toBeTruthy()
  const data: Directory = await (await request.get('/api/directory/', { headers })).json()
  const person = data.participants!.find(row => row.phone === '79990000003')!
  expect(person.access_status).toBe('not_granted')
  await message(request, 'people-auto-trigger', 'Сделаешь завтра?', true)
  await expect.poll(async () => {
    const value: Directory = await (await request.get('/api/directory/', { headers })).json()
    return value.participant_sync?.find(row => row.config_id === session.config_id)?.state
  }, { timeout: 20000 }).toBe('done')
  const repeated: Directory = await (await request.get('/api/directory/', { headers })).json()
  expect(repeated.participants!.map(row => row.id)).toEqual(data.participants!.map(row => row.id))
  await page.goto('/')
  await page.getByTestId('nav-workspace').click()
  await page.getByRole('button', { name: 'Сотрудники', exact: true }).click()
  await expect(page.getByTestId(`participant-${person.id}`)).toContainText('Тихий участник')
  await expect(page.getByTestId(`participant-${person.id}`)).toContainText('+79990000003')
  await expect(page.getByTestId(`participant-${person.id}`)).toContainText('Доступ не предоставлен')
  await page.goto('/admin/login/?next=/admin/auth/user/')
  await page.locator('#id_username').fill('79990000001')
  await page.locator('#id_password').fill('local-e2e-only')
  await page.locator('button[type="submit"], input[type="submit"]').click()
  await page.goto(`/admin/auth/user/${person.user_id}/change/`)
  await expect(page.locator('#id_first_name')).toHaveValue('Тихий участник')
  await expect(page.locator('#id_last_name')).toHaveValue('')
})

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
  await waitForFactProcessing(request)
  expect((await candidates(request)).count).toBe(0)
  expect((await candidates(request, 'approved')).count).toBe(1)
  const endC = (await context.cookies()).find(x => x.name === session.cookie_name); if (endC) currentRefresh = endC.value
  await page.screenshot({ path: join(directory, 'desktop-review.png'), fullPage: true })
})

test('CRM error is shown separately and retry preserves imported identities', async ({ request }) => {
  writeProviderState(join(directory, 'provider-state.json'), { crm_error: true })
  expect((await request.post('/api/directory/crm-sync/', { headers, data: { team_id: 1 } })).status()).toBe(202)
  await expect.poll(async () => { const response = await request.get('/api/directory/', { headers }); const value: Directory = await response.json(); return value.crm_catalog?.[0]?.state }).toBe('error')
  writeProviderState(join(directory, 'provider-state.json'), {})
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

test('full flow from WhatsApp messages to KPI plan/fact, timeline and forecasts', async ({ page, context, request }) => {
  await authenticate(context)
  await page.goto('/')
  await expect(page.getByRole('button', { name: 'KPI и чат', exact: true })).toBeVisible()

  // 1. Ensure CRM catalog is synced (with ТОО «Север Холдинг» / БЦ Север and ТОО «Юг Групп» / БЦ Южный).
  await expect.poll(async () => {
    const response = await request.get('/api/directory/', { headers })
    const value: Directory = await response.json()
    return value.projects_count
  }).toBeGreaterThanOrEqual(2)

  const dir: Directory = await (await request.get('/api/directory/', { headers })).json()
  const northProject = dir.projects.find(p => p.name === 'БЦ Север')
  const southProject = dir.projects.find(p => p.name === 'БЦ Южный')
  expect(northProject).toBeDefined()
  expect(southProject).toBeDefined()

  // 2. WhatsApp Message 1 (Payment):
  //    Send: "БЦ Север: оплата 50 000 000 ₸ поступила сегодня"
  await message(request, 'north-payment', 'БЦ Север: оплата 50 000 000 ₸ поступила сегодня')

  //    Wait for fact candidate with fact_type="payment", amount="50000000.00", project="БЦ Север".
  await expect.poll(async () => {
    const list = await candidates(request)
    return list.results.find(c => c.fact_type === 'payment' && c.project_name === 'БЦ Север' && c.proposed_changes.amount === '50000000.00')
  }, { timeout: 20000 }).toBeTruthy()

  await waitForFactProcessing(request)
  const payFact = (await candidates(request)).results.find(
    c => c.fact_type === 'payment' && c.project_name === 'БЦ Север' && c.proposed_changes.amount === '50000000.00'
  )!
  expect(payFact.fact_type).toBe('payment')
  expect(payFact.project_name).toBe('БЦ Север')
  expect(payFact.proposed_changes.amount).toBe('50000000.00')

  //    Verify notification was generated.
  await expect.poll(async () => {
    const notifs = await (await request.get('/api/notifications/', { headers })).json()
    return notifs.notifications.some((n: { title: string; message: string; project_id: number | null }) =>
      n.title.includes('оплат') || n.message.includes('50 000 000') || n.project_id === payFact.project_id
    )
  }, { timeout: 15000 }).toBe(true)

  //    Go to Workspace Review (or approve via API/UI): POST `/api/candidates/${fact.id}/review/` with action: 'approve'.
  const payReviewResp = await request.post(`/api/candidates/${payFact.id}/review/`, {
    headers,
    data: { action: 'approve', base_version: payFact.base_version, changes: {}, reason: '' }
  })
  expect(payReviewResp.status()).toBe(200)

  //    Verify candidate is approved.
  await expect.poll(async () => {
    const approved = await candidates(request, 'approved')
    return approved.results.some(c => c.id === payFact.id)
  }).toBe(true)

  // 3. WhatsApp Message 2 & 3 (Commitment):
  //    Send: "БЦ Южный: согласуем график платежей"
  //    Send: "БЦ Южный: оплатим 30 000 000 ₸ до 15 октября"
  await message(request, 'south-commit-request', 'БЦ Южный: согласуем график платежей')
  await message(request, 'south-commit-promise', 'БЦ Южный: оплатим 30 000 000 ₸ до 15 октября')

  //    Wait for fact candidate with fact_type="commitment", amount="30000000.00", project="БЦ Южный".
  await expect.poll(async () => {
    const list = await candidates(request)
    return list.results.find(c => c.fact_type === 'commitment' && c.project_name === 'БЦ Южный' && c.proposed_changes.amount === '30000000.00')
  }, { timeout: 20000 }).toBeTruthy()

  // Both messages can produce revisions; review the candidate after Q2 settles.
  await waitForFactProcessing(request)
  const commitFact = (await candidates(request)).results.find(
    c => c.fact_type === 'commitment' && c.project_name === 'БЦ Южный' && c.proposed_changes.amount === '30000000.00'
  )!
  expect(commitFact.fact_type).toBe('commitment')
  expect(commitFact.project_name).toBe('БЦ Южный')
  expect(commitFact.proposed_changes.amount).toBe('30000000.00')

  //    Approve commitment candidate.
  const commitReviewResp = await request.post(`/api/candidates/${commitFact.id}/review/`, {
    headers,
    data: { action: 'approve', base_version: commitFact.base_version, changes: {}, reason: '' }
  })
  expect(commitReviewResp.status()).toBe(200)

  //    Verify candidate is approved.
  await expect.poll(async () => {
    const approved = await candidates(request, 'approved')
    return approved.results.some(c => c.id === commitFact.id)
  }).toBe(true)

  // 4. Verify KPI Dashboard:
  //    Navigate to `/` -> Click 'KPI и чат'.
  await page.goto('/')
  await page.getByRole('button', { name: 'KPI и чат', exact: true }).click()

  //    Fetch or wait for KPI data:
  //    - Expect summary metrics: 50 000 000 ₸ fact, 100 000 000 ₸ target/plan, 50% KPI completion.
  await expect(page.getByTestId('kpi-metric-received')).toContainText('50 000 000')
  await expect(page.getByTestId('kpi-metric-plan')).toContainText('100 000 000')
  await expect(page.getByTestId('manager-card')).toContainText('50%')

  //    - Expect chart canvases to render (plan vs fact by manager, timeline by days).
  await expect(page.locator('canvas').first()).toBeVisible()
  expect(await page.locator('canvas').count()).toBeGreaterThanOrEqual(2)

  //    - Expect 'Прогноз поступлений' to be visible with amount and reasoning.
  await expect(page.getByTestId('kpi-forecast')).toBeVisible()
  await expect(page.getByTestId('kpi-forecast-heading')).toHaveText('Прогноз поступлений')
  await expect(page.getByTestId('kpi-forecast-value')).toBeVisible()
  await expect(page.getByTestId('kpi-forecast-reason')).toBeVisible()

  //    - Click 'Детализация' / metric -> expect payment row (50000000.00 KZT) in the table.
  await page.getByTestId('kpi-metric-received').click()
  await expect(page.getByTestId('kpi-detail-dialog')).toBeVisible()
  await expect(page.getByTestId('operation-row').first()).toContainText('50000000.00 KZT')
  await page.getByTestId('btn-close-detail').click()

  // 5. In AI chat:
  //    - Fill input: 'покажи kpi команды' -> press Enter -> expect KPI widget or chart to be displayed.
  const chatInput = page.getByPlaceholder('Спросите Mazory...')
  await chatInput.fill('покажи kpi команды')
  await chatInput.press('Enter')
  await expect(page.getByTestId('chart-kpi').first()).toBeVisible({ timeout: 20000 })
  await expect(page.locator('canvas').first()).toBeVisible()
})


test('autonomous WhatsApp receipt reaches CRM and reports without a review click', async ({ page, context, request }) => {
  const stateFile = join(directory, 'provider-state.json')
  writeProviderState(stateFile, { ...JSON.parse(readFileSync(stateFile, 'utf8')), autonomous: true })
  await expect.poll(async () => (await (await request.get('/api/directory/', { headers })).json()).autonomous_enabled).toBe(true)
  await message(request, 'south-auto-payment', 'БЦ Южный: оплата 20 000 000 ₸ поступила сегодня')
  await expect.poll(async () => {
    const response = await request.get('/api/autonomous/overview/', { headers })
    const value = await response.json()
    return value.totals.find((row: { currency: string; direction: string; amount_precision: string }) => row.currency === 'KZT' && row.direction === 'income' && row.amount_precision === 'exact')?.amount
  }).toBe('70000000')
  await message(request, 'south-auto-payment', 'БЦ Южный: оплата 20 000 000 ₸ поступила сегодня', true)
  await expect.poll(() => JSON.parse(readFileSync(stateFile, 'utf8')).comments?.some((row: { COMMENT: string }) => row.COMMENT.includes('20 000 000'))).toBe(true)
  await authenticate(context)
  await page.goto('/')
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Отчеты по WhatsApp', exact: true }).click()
  await expect(page.getByText('Автоматическая обработка включена', { exact: false })).toBeVisible()
  await expect(page.getByText('70 000 000', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Подтвердить факт', exact: true })).toHaveCount(0)
})
