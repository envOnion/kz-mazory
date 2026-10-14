import { test, expect, type APIRequestContext } from '@playwright/test'
import { readFileSync, writeFileSync, renameSync, existsSync, chmodSync } from 'node:fs'
import { join } from 'node:path'
import { createHmac, randomUUID } from 'node:crypto'
import type { Candidate, Page as ApiPage } from '../src/types/platform'
import type { DialogueThread } from '../src/types/factReview'
import type { AutonomousOverview } from '../src/types/autonomous'

const dir = process.env.MAZORY_E2E_DIR!
test.skip(process.env.MAZORY_E2E_REAL_MODEL === '1', 'Portable isolated external provider profile required')
interface Proof {
  control_nonce: string | null
  messages: { id: number; message_id: string; processed: boolean; processing_state: string }[]
  events: { id: number; event_type: string; state: string; error_code: string; payload: { raw_id?: number; batch_ids?: number[]; thread_id?: number; kind?: string } }[]
  deliveries: { id: number; state: string; patch: Record<string, unknown>; outbox_event_id: number }[]
  routing: { raw_message_id: number; outcome: string; selected_thread_id: number | null }[]
  catalog: { generation: number; state: string }[]
  hourly_at: string | null
  periodic: { id: number | null; duplicate_id: number | null } | null
  multi_threads: { id: number; project_id: number | null }[]
  evidence_links: { message_id: string; status: string; error_code: string; full_history_policy: string | null; repairs: string[]; ranges: Record<string, number> }[]
  bounded_traces: { id: number; status: string; error_code: string; full_history_policy: string | null }[]
}
interface Provider { deals?: Record<string, Record<string, unknown>>; writes?: Record<string, unknown>[]; tasks?: Record<string, { id: string; title: string; responsibleId: string }>; [key: string]: unknown }
function proof(): Proof { return JSON.parse(readFileSync(join(dir, 'continuous-proof.json'), 'utf8')) }
function provider(): Provider { return JSON.parse(readFileSync(join(dir, 'provider-state.json'), 'utf8')) }
function write(path: string, value: Record<string, unknown>) { const temporary = `${path}.${randomUUID()}`; writeFileSync(temporary, JSON.stringify(value)); chmodSync(temporary, 0o666); renameSync(temporary, path) }
async function control(value: Record<string, unknown>) {
  const nonce = randomUUID()
  write(join(dir, 'continuous-control.json'), { nonce, ...value })
  await expect.poll(() => existsSync(join(dir, 'continuous-applied.json')) ? JSON.parse(readFileSync(join(dir, 'continuous-applied.json'), 'utf8')).nonce : null).toBe(nonce)
  await expect.poll(() => proof().control_nonce).toBe(nonce)
}
async function message(request: APIRequestContext, group: string, id: string, body: string, replyToId?: string) {
  const data = JSON.stringify({ event: 'message', session: 'default', payload: { id, body, from: group, participant: '79990000002@c.us', notifyName: 'Боб', timestamp: Math.floor(Date.now()/1000)-10, ...(replyToId ? { replyTo: { quotedMessageId: replyToId } } : {}) } })
  const response = await request.post('/api/whatsapp/webhook/', { data, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex') } })
  expect([200, 202]).toContain(response.status())
}
test.describe.configure({ mode: 'serial' })
test.setTimeout(180000)

test('continuous CRM confirms readback, retries a discarded write, and quarantines percentage and retired-model facts', async ({ request, page, context }) => {
  const group = 'continuous-delivery@g.us'
  await control({ action: 'start', group })
  const session: { access: string; refresh: string; cookie_name: string } = JSON.parse(readFileSync(join(dir, 'continuous-session.json'), 'utf8'))
  const headers = { Authorization: `Bearer ${session.access}` }
  await control({ action: 'coalesce' })
  const periodicId = proof().periodic!.id
  expect(periodicId).not.toBeNull()
  expect(proof().periodic!.duplicate_id).toBeNull()
  expect(proof().events.filter(row => row.event_type === 'autonomous_reconcile' && ['pending', 'enqueued', 'processing'].includes(row.state))).toHaveLength(1)
  await control({ action: 'release_coalesce' })
  await expect.poll(() => proof().events.find(row => row.id === periodicId)?.state).toBe('done')
  const overviewResponse = await request.get('/api/autonomous/overview/', { headers })
  expect(overviewResponse.ok()).toBe(true)
  const overview: AutonomousOverview = await overviewResponse.json()
  expect(overview.coverage.some(scope => scope.counts.no_facts === 735 && scope.gaps.length === 0)).toBe(true)
  expect((await request.post('/api/directory/crm-sync/', { headers, data: { team_id: 1 } })).ok()).toBe(true)
  await expect.poll(() => proof().catalog[0]?.state).toBe('succeeded')
  await control({ action: 'review_next', message_id: 'continuous-multi-binding' })
  await message(request, group, 'continuous-multi-binding', 'БЦ Север: получено ТЗ. БЦ Запад: новый объект, получено ТЗ.')
  const mixedRaw = () => proof().messages.find(row => row.message_id === 'continuous-multi-binding')?.id
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=pending', { headers })).json()
    const mixed = rows.results.filter(row => row.proposed_changes.evidence_message_id === mixedRaw())
    return mixed.length === 2 && mixed.some(row => row.crm_resolution.state === 'matched') && mixed.some(row => row.crm_resolution.state === 'not_found')
  }).toBe(true)
  expect(proof().multi_threads).toHaveLength(1)
  expect(proof().multi_threads[0]!.project_id).toBeNull()
  await control({ action: 'release_review', message_id: 'continuous-multi-binding' })
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
    return rows.results.filter(row => row.proposed_changes.evidence_message_id === mixedRaw()).length
  }).toBe(2)
  await expect.poll(() => Object.values(provider().deals || {}).find(row => row.TITLE === 'БЦ Запад')?.ID).toBeTruthy()
  expect(proof().multi_threads[0]!.project_id).toBeNull()
  await message(request, group, 'continuous-east', 'Новый объект БЦ Восток')
  await expect.poll(() => Object.values(provider().deals || {}).find(row => row.TITLE === 'БЦ Восток')?.ID).toBeTruthy()
  await expect.poll(() => proof().deliveries.filter(row => row.state === 'delivered').length).toBeGreaterThan(0)
  write(join(dir, 'provider-state.json'), { ...provider(), crm_discard_write: true })
  await message(request, group, 'continuous-contract', 'БЦ Восток: договор подписан на 25 млн тенге')
  await expect.poll(() => proof().events.some(row => row.error_code === 'crm_readback_mismatch')).toBe(true)
  expect(proof().deliveries.find(row => row.patch.OPPORTUNITY === '25000000.00')?.state).toBe('pending')
  expect(Object.values(provider().deals || {}).find(row => row.TITLE === 'БЦ Восток')?.OPPORTUNITY).toBeUndefined()
  write(join(dir, 'provider-state.json'), { ...provider(), crm_discard_write: false })
  await control({ action: 'retry' })
  await expect.poll(() => Object.values(provider().deals || {}).find(row => row.TITLE === 'БЦ Восток')?.OPPORTUNITY).toBe('25000000.00')
  await expect.poll(() => proof().deliveries.find(row => row.patch.OPPORTUNITY === '25000000.00')?.state).toBe('delivered')
  const creates = provider().writes?.length
  await message(request, group, 'continuous-contract', 'БЦ Восток: договор подписан на 25 млн тенге')
  expect(provider().writes?.length).toBe(creates)
  await message(request, group, 'continuous-unmapped-stage', 'БЦ Восток: монтаж начался')
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
    return rows.results.some(row => row.proposed_changes.stage === 'in_execution')
  }).toBe(true)
  await expect.poll(() => proof().deliveries.filter(row => row.state === 'delivered').length).toBeGreaterThan(3)
  expect(proof().events.some(row => row.error_code === 'crm_stage_mapping_missing')).toBe(false)
  expect(Object.values(provider().deals || {}).find(row => row.TITLE === 'БЦ Восток')?.STAGE_ID).toBeUndefined()
  await expect.poll(() => JSON.stringify(provider()).includes('Стадия Mazory: В исполнении')).toBe(true)
  await message(request, group, 'continuous-percent', 'БЦ Север: цена предложения на 15% ниже первоначальной стоимости')
  let candidate: Candidate | undefined
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=pending', { headers })).json()
    candidate = rows.results.find(row => row.proposed_changes.amount === '15.00')
    return candidate?.automatic_decision?.reason_code
  }).toBe('unknown_amount')
  await control({ action: 'legacy', candidate_id: candidate!.id })
  await expect.poll(async () => {
    const row: Candidate = await (await request.get(`/api/candidates/${candidate!.id}/`, { headers })).json()
    return row.automatic_decision?.reason_code
  }).toBe('extraction_model_changed')
  await message(request, group, 'continuous-mixed-report', 'БЦ Север: оплата 60 000 000 ₸ поступила сегодня. По БЦ Южный выставлен счет примерно на 30 000 000 ₸, оплату ожидаем завтра.')
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
    const receipt = rows.results.find(row => row.proposed_changes.amount === '60000000.00')
    return receipt?.proposed_changes.amount_precision
  }).toBe('exact')
  const generation = proof().catalog[0]!.generation
  await control({ action: 'hourly' })
  await expect.poll(() => proof().catalog[0]?.state).toBe('succeeded')
  expect(proof().catalog[0]!.generation).toBe(generation + 1)
  expect(proof().hourly_at).toBeTruthy()
  await control({ action: 'assignee' })
  await message(request, group, 'continuous-task-request', 'БЦ Север: подготовь смету')
  await message(request, group, 'continuous-task-promise', 'БЦ Север: да, подготовлю смету завтра')
  await expect.poll(() => Object.values(provider().tasks || {}).some(task => task.responsibleId === '7')).toBe(true)
  await expect.poll(() => proof().deliveries.some(row => row.state === 'delivered' && proof().events.find(event => event.id === row.outbox_event_id)?.payload.kind === 'task')).toBe(true)
  await control({ action: 'bounded' })
  await expect.poll(() => proof().bounded_traces.some(trace => trace.status === 'success')).toBe(true)
  expect(proof().bounded_traces.length).toBeGreaterThan(1)
  expect(proof().bounded_traces.every(trace => trace.full_history_policy === null)).toBe(true)
  await message(request, group, 'continuous-company-only', 'От компании Меридиан поступила оплата 117 млн тенге, объект не указан')
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=pending', { headers })).json()
    return rows.results.find(row => row.proposed_changes.object_name === 'Меридиан')?.automatic_decision?.reason_code
  }).toBe('company_not_object')
  expect(Object.values(provider().deals || {}).some(row => row.TITLE === 'Меридиан')).toBe(false)
  await message(request, group, 'continuous-unknown-party', 'БЦ Север: от компании Меридиан поступила оплата 20 000 000 ₸ сегодня')
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
    return rows.results.some(row => row.fact_type === 'payment' && row.proposed_changes.amount === '20000000.00' && row.proposed_changes.company_name === 'Меридиан')
  }).toBe(true)
  expect(proof().events.some(event => event.event_type === 'autonomous_crm' && event.payload.kind === 'company')).toBe(false)
  await control({ action: 'review_next', message_id: 'continuous-audit-held' })
  await message(request, group, 'continuous-audit-held', 'БЦ Южный: оплата 45 млн тенге поступила сегодня')
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=pending', { headers })).json()
    return rows.results.find(row => row.proposed_changes.amount === '45000000.00')?.automatic_decision?.reason_code
  }).toBe('maintenance_review_required')
  await control({ action: 'release_review', message_id: 'continuous-audit-held' })
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
    return rows.results.some(row => row.proposed_changes.amount === '45000000.00')
  }).toBe(true)
  const evidenceGroup = 'continuous-evidence@g.us'
  await control({ action: 'evidence_source', group: evidenceGroup })
  await message(request, evidenceGroup, 'continuous-evidence-origin', 'Новый объект БЦ Восток')
  await expect.poll(() => proof().messages.find(row => row.message_id === 'continuous-evidence-origin')?.processed).toBe(true)
  await control({ action: 'review_next', message_id: 'continuous-context-evidence' })
  await message(request, evidenceGroup, 'continuous-context-evidence', 'БЦ Восток: КП согласовано', 'continuous-evidence-origin')
  await expect.poll(() => proof().evidence_links.some(trace => trace.status === 'success')).toBe(true)
  await control({ action: 'release_review', message_id: 'continuous-context-evidence' })
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
    return rows.results.some(row => row.proposed_changes.current_action === 'КП согласовано')
  }).toBe(true)
  const contextSource = proof().messages.find(row => row.message_id === 'continuous-evidence-origin')!
  expect(proof().evidence_links.some(trace => trace.status === 'success' && trace.full_history_policy === null && trace.repairs.includes('literal_fact_source_link') && trace.ranges[String(contextSource.id)] === 'Новый объект БЦ Восток'.length)).toBe(true)
  expect(proof().evidence_links.some(trace => trace.error_code)).toBe(false)
  await control({ action: 'review_next', message_id: 'continuous-unavailable-evidence' })
  await message(request, evidenceGroup, 'continuous-unavailable-evidence', 'БЦ Восток: КП согласовано по недоступному сообщению')
  await expect.poll(() => proof().evidence_links.some(trace => trace.message_id === 'continuous-unavailable-evidence' && trace.error_code === 'fact_thread_evidence_missing')).toBe(true)
  const invalidSource = proof().messages.find(row => row.message_id === 'continuous-unavailable-evidence')!
  for (const status of ['pending', 'approved']) {
    const rows: ApiPage<Candidate> = await (await request.get(`/api/candidates/?status=${status}`, { headers })).json()
    expect(rows.results.some(row => row.proposed_changes.evidence_message_id === invalidSource.id)).toBe(false)
  }
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  await page.goto('/')
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Отчеты по WhatsApp', exact: true }).click()
  await expect(page.getByText('Автоматическая обработка WhatsApp включена', { exact: true })).toBeVisible()
  await page.screenshot({ path: join(dir, 'continuous-delivery.png'), fullPage: true })
})

test('continuous reconciliation restores lost thread extraction without competing full-history jobs', async ({ request }) => {
  const group = 'continuous-routing@g.us'
  await control({ action: 'routing', group })
  const session: { access: string } = JSON.parse(readFileSync(join(dir, 'continuous-session.json'), 'utf8'))
  const headers = { Authorization: `Bearer ${session.access}` }
  await message(request, group, 'continuous-topic', 'БЦ Север: подготовь смету')
  await expect.poll(() => proof().messages.find(row => row.message_id === 'continuous-topic')?.processed).toBe(true)
  await message(request, group, 'continuous-routed', 'БЦ Север: оплата 50 000 000 ₸ поступила сегодня')
  await expect.poll(() => {
    const p = proof(), raw = p.messages.find(row => row.message_id === 'continuous-routed')
    return p.routing.find(row => row.raw_message_id === raw?.id)?.outcome
  }).toBe('laya')
  const raw = proof().messages.find(row => row.message_id === 'continuous-routed')!
  expect(raw.processed).toBe(false)
  await control({ action: 'gap' })
  await expect.poll(() => proof().events.filter(row => row.event_type === 'extract_thread' && row.payload.batch_ids?.includes(raw.id)).length).toBe(1)
  expect(proof().events.filter(row => row.event_type === 'extract_message' && row.payload.raw_id === raw.id)).toHaveLength(0)
  const threadId = proof().routing.find(row => row.raw_message_id === raw.id)!.selected_thread_id!
  const thread: DialogueThread = await (await request.get(`/api/threads/${threadId}/`, { headers })).json()
  expect(thread.messages.some(row => row.id === raw.id)).toBe(true)
  expect((await request.post(`/api/threads/${thread.id}/flush/`, { headers })).ok()).toBe(true)
  await expect.poll(() => proof().messages.find(row => row.id === raw.id)?.processed).toBe(true)
  await message(request, group, 'continuous-routed', 'БЦ Север: оплата 50 000 000 ₸ поступила сегодня')
  expect(proof().messages.filter(row => row.id === raw.id)).toHaveLength(1)
})
