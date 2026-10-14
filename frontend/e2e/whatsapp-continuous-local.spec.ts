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
  journal_fixture: { success: boolean; result: { deal_id: string; comment_id: string; partial_delivery_id: number } } | null
  journal_rebuild: { success: boolean; result: { confirmed: number; unavailable: number; skipped: number } } | null
  journal: { id: number; object_type: string; external_id: string; method: string; status: string; journal_key: string; delivery_id: number | null; reconstructed: boolean; payload: { fields?: { OPPORTUNITY?: string } }; created_at: string }[]
  topic_labels: { original_topic: string; display_topic: string }[]
  control_nonce: string | null
  messages: { id: number; message_id: string; processed: boolean; processing_state: string }[]
  events: { id: number; event_type: string; state: string; error_code: string; payload: { raw_id?: number; batch_ids?: number[]; thread_id?: number; kind?: string } }[]
  deliveries: { id: number; state: string; patch: Record<string, unknown>; outbox_event_id: number; external_object_link__external_id: string | null }[]
  routing: { raw_message_id: number; outcome: string; selected_thread_id: number | null }[]
  catalog: { generation: number; state: string }[]
  hourly_at: string | null
  periodic: { id: number | null; duplicate_id: number | null } | null
  multi_threads: { id: number; project_id: number | null }[]
  evidence_links: { message_id: string; status: string; error_code: string; full_history_policy: string | null; repairs: string[]; ranges: Record<string, number> }[]
  bounded_traces: { id: number; status: string; error_code: string; full_history_policy: string | null }[]
}
interface Provider { deals?: Record<string, Record<string, unknown>>; writes?: Record<string, unknown>[]; tasks?: Record<string, { id: string; title: string; description: string; deadline: string | null; responsibleId: string; ufCrmTask: string[] }>; comments?: { COMMENT: string }[]; [key: string]: unknown }
function proof(): Proof { return JSON.parse(readFileSync(join(dir, 'continuous-proof.json'), 'utf8')) }
function provider(): Provider { return JSON.parse(readFileSync(join(dir, 'provider-state.json'), 'utf8')) }
function write(path: string, value: Record<string, unknown>) { const temporary = `${path}.${randomUUID()}`; writeFileSync(temporary, JSON.stringify(value)); chmodSync(temporary, 0o666); renameSync(temporary, path) }
async function control(value: Record<string, unknown>) {
  const nonce = randomUUID()
  write(join(dir, 'continuous-control.json'), { nonce, ...value })
  await expect.poll(() => existsSync(join(dir, 'continuous-applied.json')) ? JSON.parse(readFileSync(join(dir, 'continuous-applied.json'), 'utf8')).nonce : null).toBe(nonce)
  await expect.poll(() => proof().control_nonce).toBe(nonce)
}
async function message(request: APIRequestContext, group: string, id: string, body: string, replyToId?: string, timestamp = Math.floor(Date.now()/1000)-10) {
  const data = JSON.stringify({ event: 'message', session: 'default', payload: { id, body, from: group, participant: '79990000002@c.us', notifyName: 'Боб', timestamp, ...(replyToId ? { replyTo: { quotedMessageId: replyToId } } : {}) } })
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
  // Provider acknowledgement survives the business transaction's rollback.
  expect(proof().journal.some(row => row.method === 'crm.deal.update' && row.status === 'accepted' && row.payload.fields?.OPPORTUNITY === '25000000.00')).toBe(true)
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
  await expect.poll(() => Object.values(provider().tasks || {}).some(task => task.responsibleId === '7' && task.title.endsWith('Подготовить смету БЦ Север'))).toBe(true)
  const task = Object.values(provider().tasks || {}).find(row => row.title.endsWith('Подготовить смету БЦ Север'))!
  await expect.poll(() => proof().deliveries.find(row => row.external_object_link__external_id === task.id)?.state).toBe('delivered')
  expect(task.deadline).toBeFalsy()
  expect(task.description).toMatch(/Срок по переписке: \d{4}-\d{2}-\d{2} \(дата, время не указано\)/)
  expect(task.ufCrmTask).toEqual(['D_101'])
  const taskDelivery = proof().deliveries.find(row => row.external_object_link__external_id === task.id)!
  await control({ action: 'retry_unbound_task', delivery_id: taskDelivery.id })
  await expect.poll(() => proof().deliveries.find(row => row.id === taskDelivery.id)?.state).toBe('delivered')
  expect(proof().deliveries.find(row => row.id === taskDelivery.id)?.external_object_link__external_id).toBe(task.id)
  const matchingTasks = Object.values(provider().tasks || {}).filter(row => row.title === task.title)
  expect(matchingTasks).toHaveLength(1)
  expect(matchingTasks[0]!.id).toBe(task.id)
  expect(matchingTasks[0]!.deadline).toBeFalsy()
  expect(matchingTasks[0]!.responsibleId).toBe('7')
  expect(matchingTasks[0]!.ufCrmTask).toEqual(['D_101'])
  expect(proof().journal.some(row => row.method === 'tasks.task.add' && row.external_id === task.id)).toBe(true)
  expect(proof().journal.some(row => row.method === 'tasks.task.update' && row.external_id === task.id)).toBe(true)
  expect(proof().journal.every(row => !row.method.endsWith('.get') && !row.method.endsWith('.list'))).toBe(true)
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
  const agreedRows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
  const agreedProject = agreedRows.results.find(row => row.proposed_changes.current_action === 'КП согласовано')!.project_id!
  await message(request, evidenceGroup, 'continuous-older-status', 'БЦ Восток: историческое КП подготовлено', undefined, Math.floor(Date.now()/1000)-86400)
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
    return rows.results.find(row => row.proposed_changes.current_action === 'КП подготовлено')?.automatic_decision?.reason_code
  }).toBe('historical_observation')
  const actualProjects: ApiPage<{ id: number; current_action: string }> = await (await request.get(`/api/projects/?search=${encodeURIComponent('Восток')}`, { headers })).json()
  expect(actualProjects.results.find(row => row.id === agreedProject)?.current_action).toBe('КП согласовано')
  await expect.poll(() => (provider().comments || []).some(row => row.COMMENT.includes('БЦ Восток: историческое КП подготовлено'))).toBe(true)
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
  await message(request, group, 'continuous-proposal-price', 'КП: БЦ Север, предложение на 29 млн тенге')
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=rejected', { headers })).json()
    return rows.results.find(row => row.proposed_changes.evidence === 'КП: БЦ Север, предложение на 29 млн тенге')?.automatic_decision?.reason_code
  }).toBe('invoice_not_supported')
  expect((provider().comments || []).some(row => row.COMMENT.includes('КП: БЦ Север, предложение на 29 млн тенге'))).toBe(false)
  for (const [id, body] of [
    ['continuous-not-performed', 'БЦ Север: заказчиком не произведена 100% оплата в размере 14 500 000 тенге'],
    ['continuous-future-performed', 'БЦ Север: заказчиком будет произведена 100% оплата в размере 14 500 000 тенге'],
  ]) {
    await message(request, group, id!, body!)
    await expect.poll(async () => {
      const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=rejected', { headers })).json()
      return rows.results.some(row => row.proposed_changes.evidence === body && row.automatic_decision?.reason_code === 'plan_not_actual')
    }).toBe(true)
  }
  const performed = 'БЦ Север: заказчиком произведена 100% оплата в размере 14 500 000 тенге'
  await message(request, group, 'continuous-performed', performed)
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=approved', { headers })).json()
    return rows.results.some(row => row.proposed_changes.evidence === performed && row.automatic_decision?.reason_code === 'source_validated')
  }).toBe(true)
  await expect.poll(() => (provider().comments || []).some(row => row.COMMENT.includes(performed))).toBe(true)
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  await message(request, group, 'continuous-long-label', 'Длинная тема: получен управленческий отчёт')
  await expect.poll(() => proof().messages.find(row => row.message_id === 'continuous-long-label')?.processed).toBe(true)
  await expect.poll(() => proof().topic_labels.length).toBeGreaterThan(0)
  expect(proof().topic_labels[0]!.original_topic.length).toBeGreaterThan(255)
  expect(proof().topic_labels[0]!.display_topic.length).toBeLessThanOrEqual(255)
  const focusedGroup = 'continuous-focused-long@g.us'
  await control({ action: 'routing', group: focusedGroup })
  await message(request, focusedGroup, 'continuous-focused-long-label', 'Длинная тема: КП отдельного объекта подготовлено')
  await expect.poll(() => proof().messages.find(row => row.message_id === 'continuous-focused-long-label')?.processed).toBe(true)
  await expect.poll(() => proof().topic_labels.length).toBeGreaterThan(1)
  expect(proof().topic_labels.every(row => row.display_topic.length <= 255 && row.original_topic.length > 255)).toBe(true)
  const reclassifiedGroup = 'continuous-reclassified@g.us'
  await control({ action: 'start', group: reclassifiedGroup })
  await control({ action: 'review_next', message_id: 'continuous-retained-facts' })
  await message(request, reclassifiedGroup, 'continuous-retained-facts', 'БЦ Север: получено ТЗ. БЦ Запад: новый объект, получено ТЗ.')
  const retainedRaw = () => proof().messages.find(row => row.message_id === 'continuous-retained-facts')?.id
  await expect.poll(async () => {
    const rows: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=pending', { headers })).json()
    return rows.results.filter(row => row.proposed_changes.evidence_message_id === retainedRaw()).length
  }).toBe(2)
  await message(request, reclassifiedGroup, 'continuous-reclassified-context', 'Темы уточнены без новых фактов', 'continuous-retained-facts')
  await expect.poll(() => proof().messages.find(row => row.message_id === 'continuous-reclassified-context')?.processed).toBe(true)
  const retained: ApiPage<Candidate> = await (await request.get('/api/candidates/?status=pending', { headers })).json()
  expect(retained.results.filter(row => row.proposed_changes.evidence_message_id === retainedRaw())).toHaveLength(2)
  await page.goto('/')
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
  await page.getByRole('button', { name: 'Отчеты по WhatsApp', exact: true }).click()
  await expect(page.getByText('Автоматическая обработка WhatsApp включена', { exact: true })).toBeVisible()
  await page.screenshot({ path: join(dir, 'continuous-delivery.png'), fullPage: true })
  await page.goto('/admin/login/?next=/admin/api/bitrixdealchangelog/')
  await page.getByLabel('Имя пользователя').fill('79990000001')
  await page.getByLabel('Пароль').fill('local-e2e-only')
  await page.getByRole('button', {name:/Войти/}).click()
  await expect(page.locator('#result_list')).toContainText('crm.deal.add')
  await expect(page.locator('#result_list')).toContainText('tasks.task.add')
  await control({action:'journal_prepare'})
  await expect.poll(() => proof().journal_fixture?.success).toBe(true)
  const fixture = proof().journal_fixture!.result
  const mutations = () => readFileSync(join(dir,'crm-requests.jsonl'),'utf8').trim().split('\n').map(line => JSON.parse(line) as {method:string}).filter(row => /\.(add|update|delete|complete|defer)\.json$/.test(row.method)).length
  const beforeHistoryMutations = mutations()
  const beforeHistoryWrites = provider().writes?.length
  const beforeHistoryTasks = Object.keys(provider().tasks || {}).length
  const beforeHistoryComments = provider().comments?.length
  await control({action:'journal_history'})
  await expect.poll(() => proof().journal_rebuild?.success).toBe(true)
  // Earlier full-suite cases reset the provider, so their historical objects
  // can be unavailable. Current objects must be positively verified.
  expect(proof().journal.some(row => row.reconstructed && row.object_type === 'task' && row.external_id === task.id && row.status === 'confirmed')).toBe(true)
  expect(proof().journal.some(row => row.reconstructed && row.object_type === 'timeline' && row.status === 'confirmed')).toBe(true)
  expect(proof().journal.some(row => row.reconstructed && row.delivery_id === fixture.partial_delivery_id)).toBe(true)
  expect(proof().journal.some(row => row.reconstructed && row.method === 'crm.deal.delete' && row.external_id === fixture.deal_id && row.status === 'confirmed')).toBe(true)
  expect(proof().journal.some(row => row.reconstructed && row.method === 'crm.timeline.comment.delete' && row.external_id === fixture.comment_id && row.status === 'confirmed')).toBe(true)
  const recovered = proof().journal.length
  await control({action:'journal_retry'})
  await expect.poll(() => proof().journal_rebuild?.result.confirmed).toBe(2)
  expect(proof().journal.length).toBe(recovered)
  await control({action:'journal_history_again'})
  await expect.poll(() => proof().journal_rebuild?.result.confirmed).toBe(0)
  expect(proof().journal.length).toBe(recovered)
  expect(provider().writes?.length).toBe(beforeHistoryWrites)
  expect(Object.keys(provider().tasks || {})).toHaveLength(beforeHistoryTasks)
  expect(provider().comments?.length).toBe(beforeHistoryComments)
  expect(mutations()).toBe(beforeHistoryMutations)
  await page.reload()
  await expect(page.locator('#result_list')).toContainText('Восстановленная доставка')
  const taskLink=page.locator(`#result_list a[href$="/tasks/task/view/${task.id}/"]`).first()
  await expect(taskLink).toBeVisible()
  await expect(taskLink).toHaveAttribute('href',new RegExp(`/company/personal/user/7/tasks/task/view/${task.id}/$`))
  await page.screenshot({path:join(dir,'bitrix-journal-history.png'),fullPage:true})
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
