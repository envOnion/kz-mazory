import { readFileSync } from './helpers/files'
import { authenticateShell } from './helpers/session'
import { test, expect, type APIRequestContext } from '@playwright/test'
import { createHmac, randomUUID } from 'node:crypto'
import { chmodSync, existsSync, renameSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const dir = process.env.MAZORY_E2E_DIR!
const group = 'sale-clarifications-e2e@g.us'
const lead = '79990000001@c.us'
interface SaleProject {
  id: number; name: string; bitrix_id: string; version: number; sale_amount: string | null
  contract_known: boolean; contract_amount: string; paid_amount: string; cost_amount: string; current_action: string
}
interface Question {
  id: number; project_id: number; kind: string; state: string; notification_id: number | null
  candidate_id: number; price_context: Record<string, unknown>; resolution: Record<string, unknown>
}
interface Proof {
  control: { nonce?: string; task_id?: string }
  task: { success: boolean; result: string } | null
  projects: SaleProject[]; questions: Question[]
  replies: { id: number; question_id: number; provider_message_id: string; state: string; error_code: string; raw_message_id: number | null }[]
  sources: { id: number; message_id: string; content: string; source: string; processing_state: string }[]
  deliveries: { id: number; state: string; error_code: string; target_version: number; patch: { OPPORTUNITY?: string }; fact_event__project_id: number }[]
  assertions: { project_id: number; value: string }[]
  answer_candidates: { id: number; project_id: number; status: string; trace__model_version: string;
    trace__raw_message_id: number; decisions__actor_kind: string; decisions__reason_code: string;
    evidence__raw_message_id: number; evidence__quote: string }[]
  financial: { id: number; project_id: number; amount: string }[]
  reply_raws: { id: number; message_id: string; processing_state: string }[]
  reply_traces: { id: number; raw_message_id: number; model_version: string }[]
  reply_work_items: number[]; reply_thread_links: number[]
  reply_extract_events: { id: number; payload__raw_id: number }[]
  history: { id: number; state: string; scheduled_count: number; existing_count: number; imported_count: number; error_code: string } | null
  history_sources: { id: number; message_id: string; processing_state: string }[]
  notifications: { notification_id: number; state: string; provider_message_id: string }[]
  journal: { id: number; method: string; status: string; external_id: string; delivery_id: number | null; payload: { fields?: { OPPORTUNITY?: string } } }[]
}
interface Provider {
  deals?: Record<string, { ID: string; TITLE: string; OPPORTUNITY: string; CURRENCY_ID: string; ORIGINATOR_ID?: string }>
  waha_messages?: { id: string; text: string; chatId: string }[]
}
const proof = (): Proof => JSON.parse(readFileSync(join(dir, 'sale-clarifications-proof.json'), 'utf8'))
const provider = (): Provider => JSON.parse(readFileSync(join(dir, 'provider-state.json'), 'utf8'))
const project = (name: string) => proof().projects.find(row => row.name === name)
const question = (name: string) => proof().questions.find(row => row.project_id === project(name)?.id)
const deal = (name: string) => provider().deals?.[project(name)?.bitrix_id || '']
const reply = (id: string) => proof().replies.find(row => row.provider_message_id === id)
function write(path: string, value: Record<string, unknown>) {
  const temporary = `${path}.${randomUUID()}`
  writeFileSync(temporary, JSON.stringify(value)); chmodSync(temporary, 0o666); renameSync(temporary, path)
}
async function control(value: Record<string, unknown>) {
  const nonce = randomUUID()
  write(join(dir, 'sale-clarifications-control.json'), { nonce, ...value })
  await expect.poll(() => existsSync(join(dir, 'sale-clarifications-proof.json')) ? proof().control.nonce : null).toBe(nonce)
  if (proof().control.task_id) await expect.poll(() => proof().task?.success).toBe(true)
}
async function webhook(request: APIRequestContext, id: string, body: string, from = lead, extra: Record<string, unknown> = {}) {
  const data = JSON.stringify({ event: 'message.any', session: 'default', payload: {
    id, body, from, fromMe: false, timestamp: Math.floor(Date.now() / 1000), ...extra,
  } })
  return request.post('/api/whatsapp/webhook/', { data, headers: {
    'Content-Type': 'application/json',
    'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex'),
  } })
}
async function awaiting(name: string) {
  await expect.poll(() => question(name)?.state).toBe('awaiting')
  const q = question(name)!
  await expect.poll(() => proof().notifications.find(row => row.notification_id === q.notification_id)?.provider_message_id).toBeTruthy()
  const notification = proof().notifications.find(row => row.notification_id === q.notification_id)!
  await expect.poll(() => provider().waha_messages?.some(row => row.id === notification.provider_message_id)).toBe(true)
  return { q, sent: provider().waha_messages!.find(row => row.id === notification.provider_message_id)! }
}
async function seed(name: string, external: string, offers = false) {
  await control({ action: 'seed', name, external_id: external, offers })
  return awaiting(name)
}
async function answer(request: APIRequestContext, q: Question, suffix: string, body: string) {
  const id = `sale-question-${suffix}`
  expect((await webhook(request, id, `MZ-${q.id} ${body}`)).status()).toBe(202)
  return id
}
function unchangedFinance(name: string) {
  expect(project(name)).toMatchObject({ contract_known: false, contract_amount: '0.00', paid_amount: '0.00', cost_amount: '0.00' })
  expect(proof().financial.filter(row => row.project_id === project(name)?.id)).toHaveLength(0)
}

test.skip(process.env.MAZORY_E2E_REAL_MODEL === '1', 'Isolated external HTTP provider required')
test.setTimeout(360000)
test('missing sale price is asked in WhatsApp, answered once, delivered with readback, and protected against ambiguity and stale replies', async ({ request, page, context }) => {
  await control({ action: 'start' })
  const name = 'БЦ Лазурный'
  const source = `${name}: получено техническое задание, стоимость предложения пока не названа.`
  expect((await webhook(request, 'sale-question-new-deal', source, group, {
    participant: '79990000002@c.us', notifyName: 'Боб', timestamp: Math.floor(Date.now() / 1000) - 30,
  })).status()).toBe(202)
  const { q, sent } = await awaiting(name)
  expect(q.kind).toBe('sale_amount')
  expect(sent.chatId).toBe(lead)
  expect(sent.text).toContain(name)
  expect(sent.text).toContain(`MZ-${q.id}`)
  expect(sent.text).toMatch(/цен|сумм/)
  expect(sent.text).toContain('не знаю')
  expect(deal(name)).toMatchObject({ OPPORTUNITY: '0.00', ORIGINATOR_ID: 'MAZORY' })
  unchangedFinance(name)

  const session: { refresh: string; cookie_name: string } = JSON.parse(readFileSync(join(dir, 'sale-clarifications-session.json'), 'utf8'))
  await context.addCookies([{ name: session.cookie_name, value: session.refresh,
    url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  await page.goto('/')
  await page.getByTitle('Уведомления', { exact: true }).click()
  await expect(page.getByText('Нужна сумма сделки Bitrix', { exact: true }).first()).toBeVisible()
  await expect(page.getByText(/БЦ Лазурный/).first()).toBeVisible()
  await expect(page.getByText('Ожидается ответ в WhatsApp', { exact: true }).first()).toBeVisible()
  await page.screenshot({ path: join(dir, 'sale-clarification-question.png'), fullPage: true })

  expect((await webhook(request, 'sale-question-wrong-author', `MZ-${q.id} 50 млн тенге`, '79990000002@c.us')).status()).toBe(403)
  expect(reply('sale-question-wrong-author')).toBeUndefined()
  await control({ action: 'pause_reply' })
  await control({ action: 'discard_write' })
  const quoted = { fromMe: true, to: lead, timestamp: Math.floor(Date.now() / 1000), _data: { quotedStanzaID: sent.id.split('_').at(-1) } }
  expect((await webhook(request, 'sale-question-quoted-answer', '50 млн тенге', lead, quoted)).status()).toBe(202)
  expect((await webhook(request, 'sale-question-quoted-answer', '50 млн тенге', lead, quoted)).status()).toBe(202)
  await expect.poll(() => reply('sale-question-quoted-answer')?.state).toBe('queued')
  expect(proof().replies.filter(row => row.provider_message_id === 'sale-question-quoted-answer')).toHaveLength(1)
  writeFileSync(join(dir, 'sale-clarification-restart-required'), 'restart at persisted price reply')
  await expect.poll(() => existsSync(join(dir, 'sale-clarification-restart-done')), { timeout: 60000 }).toBe(true)
  await control({ action: 'resume_reply' })
  await expect.poll(() => reply('sale-question-quoted-answer')?.state).toBe('applied')
  await expect.poll(() => proof().deliveries.some(row => row.fact_event__project_id === project(name)?.id && row.error_code === 'crm_readback_mismatch')).toBe(true)
  expect(question(name)?.state).toBe('answered')
  expect(project(name)?.sale_amount).toBe('50000000.00')
  expect(deal(name)?.OPPORTUNITY).toBe('0.00')
  const rawId = reply('sale-question-quoted-answer')!.raw_message_id
  expect(rawId).toBeTruthy()
  expect(proof().sources.find(row => row.id === rawId)).toMatchObject({ content: '50 млн тенге' })
  expect(proof().answer_candidates.find(row => row.project_id === project(name)?.id)).toMatchObject({
    status: 'approved', trace__model_version: 'human-whatsapp-answer', trace__raw_message_id: rawId,
    decisions__actor_kind: 'human', decisions__reason_code: 'sale_amount_clarified',
    evidence__raw_message_id: rawId, evidence__quote: '50 млн тенге',
  })
  await control({ action: 'retry' })
  await expect.poll(() => question(name)?.state).toBe('resolved')
  expect(deal(name)?.OPPORTUNITY).toBe('50000000.00')
  unchangedFinance(name)
  expect(proof().questions.filter(row => row.project_id === project(name)?.id)).toHaveLength(1)
  expect(provider().waha_messages!.filter(row => row.id === sent.id)).toHaveLength(1)
  expect(proof().assertions.filter(row => row.project_id === project(name)?.id && row.value === '50000000.00')).toHaveLength(1)
  const delivery = proof().deliveries.find(row => row.fact_event__project_id === project(name)?.id && row.patch.OPPORTUNITY === '50000000.00' && row.state === 'delivered')!
  expect(delivery).toBeTruthy()
  expect(Object.keys(delivery.patch).sort()).toEqual(['CURRENCY_ID', 'OPPORTUNITY'])
  const receipt = () => proof().journal.filter(row => row.method === 'crm.deal.update' && row.status === 'accepted' && row.external_id === project(name)?.bitrix_id && row.payload.fields?.OPPORTUNITY === '50000000.00').at(-1)
  await expect.poll(() => receipt()?.delivery_id).toBe(delivery.id)
  await authenticateShell(page.context())
  await page.goto(`/crm-changes?receipt=${receipt()!.id}`)
  await expect(page.locator('[data-crm-id]').filter({ hasText: 'OPPORTUNITY' }).first()).toContainText('50000000.00')
  await page.screenshot({ path: join(dir, 'sale-clarification-journal.png'), fullPage: true })
  const repeated = await answer(request, q, 'resolved-repeat', '51 млн тенге')
  await expect.poll(() => reply(repeated)?.error_code).toBe('clarification_stale')
  expect(deal(name)?.OPPORTUNITY).toBe('50000000.00')

  const offers = await seed('БЦ Топаз', '4101', true)
  expect(offers.sent.text).toContain('18 000 000')
  expect(offers.sent.text).toContain('20 000 000')
  for (const [suffix, body, error] of [
    ['ambiguous', '18 млн или 20 млн тенге', 'clarification_answer_unclear'],
    ['currency', '20 млн долларов', 'clarification_currency_mismatch'],
    ['profit', 'прибыль 3 млн тенге', 'clarification_answer_unclear'],
    ['advance', 'аванс 5 млн тенге', 'clarification_answer_unclear'],
    ['not-own', 'не наша цена 12 млн тенге', 'clarification_answer_unclear'],
    ['approximate', 'приблизительная цена 12 млн тенге', 'clarification_answer_unclear'],
  ] as const) {
    const id = await answer(request, offers.q, suffix, body)
    await expect.poll(() => reply(id)?.error_code).toBe(error)
    expect(question('БЦ Топаз')?.state).toBe('awaiting')
    expect(project('БЦ Топаз')?.sale_amount).toBeNull()
    expect(deal('БЦ Топаз')?.OPPORTUNITY).toBe('0.00')
  }
  expect((await webhook(request, 'sale-question-offer-choice', `MZ-${offers.q.id} 2`, group,
    { participant: lead, notifyName: 'Проверяющий' })).status()).toBe(202)
  await expect.poll(() => question('БЦ Топаз')?.state).toBe('resolved')
  expect(project('БЦ Топаз')?.sale_amount).toBe('20000000.00')
  expect(deal('БЦ Топаз')?.OPPORTUNITY).toBe('20000000.00')
  unchangedFinance('БЦ Топаз')
  const offeredRawId = reply('sale-question-offer-choice')!.raw_message_id!
  const originalTraces = proof().reply_traces.filter(row => row.raw_message_id === offeredRawId)
  expect(originalTraces).toHaveLength(1)
  expect(originalTraces[0]!.model_version).toBe('human-whatsapp-answer')
  // WAHA HTTP history pages pass the same group answer through the genuine import job.
  await control({ action: 'history_replay', message_id: 'sale-question-offer-choice' })
  await expect.poll(() => proof().history?.state).toBe('completed')
  expect(proof().history).toMatchObject({ scheduled_count: 0, existing_count: 1, imported_count: 0, error_code: '' })
  expect(readFileSync(join(dir, 'sale-clarification-history-requests.jsonl'), 'utf8').trim().split('\n').length).toBeGreaterThanOrEqual(4)
  expect(proof().reply_raws.filter(row => row.message_id === 'sale-question-offer-choice')).toEqual([
    { id: offeredRawId, message_id: 'sale-question-offer-choice', processing_state: 'clarification_reply' },
  ])
  expect(proof().replies.filter(row => row.provider_message_id === 'sale-question-offer-choice')).toHaveLength(1)
  expect(proof().reply_traces.filter(row => row.raw_message_id === offeredRawId)).toEqual(originalTraces)
  expect(proof().reply_work_items).not.toContain(offeredRawId)
  expect(proof().reply_thread_links).not.toContain(offeredRawId)
  expect(proof().reply_extract_events.some(row => row.payload__raw_id === offeredRawId)).toBe(false)
  expect(question('БЦ Топаз')?.state).toBe('resolved')
  expect(deal('БЦ Топаз')?.OPPORTUNITY).toBe('20000000.00')

  const unknown = await seed('БЦ Аметист', '4102')
  await answer(request, unknown.q, 'unknown', 'не знаю')
  await expect.poll(() => question('БЦ Аметист')?.state).toBe('unconfirmed')
  const unknownCount = provider().waha_messages!.length
  await control({ action: 'replan', project_ids: [project('БЦ Аметист')!.id] })
  expect(proof().questions.filter(row => row.project_id === project('БЦ Аметист')?.id)).toHaveLength(1)
  expect(question('БЦ Аметист')?.state).toBe('unconfirmed')
  expect(provider().waha_messages!.length).toBe(unknownCount)
  expect(deal('БЦ Аметист')?.OPPORTUNITY).toBe('0.00')

  const crmChanged = await seed('БЦ Обсидиан', '4103')
  await control({ action: 'crm_changed', project_id: project('БЦ Обсидиан')!.id })
  const staleCrm = await answer(request, crmChanged.q, 'stale-crm', '50 млн тенге')
  await expect.poll(() => reply(staleCrm)?.error_code).toBe('clarification_crm_changed')
  expect(question('БЦ Обсидиан')?.state).toBe('superseded')
  expect(project('БЦ Обсидиан')?.sale_amount).toBeNull()
  expect(deal('БЦ Обсидиан')?.OPPORTUNITY).toBe('61000000.00')

  const localChanged = await seed('БЦ Жемчужный', '4104')
  await control({ action: 'local_changed', project_id: project('БЦ Жемчужный')!.id })
  const staleLocal = await answer(request, localChanged.q, 'stale-local', '50 млн тенге')
  await expect.poll(() => reply(staleLocal)?.error_code).toBe('clarification_stale')
  expect(question('БЦ Жемчужный')?.state).toBe('superseded')
  expect(project('БЦ Жемчужный')?.sale_amount).toBe('62000000.00')
  expect(deal('БЦ Жемчужный')?.OPPORTUNITY).toBe('0.00')

  const race = await seed('БЦ Бирюзовый', '4105')
  await control({ action: 'pause_delivery' })
  await answer(request, race.q, 'delivery-race', '50 млн тенге')
  await expect.poll(() => question('БЦ Бирюзовый')?.state).toBe('answered')
  expect(project('БЦ Бирюзовый')?.sale_amount).toBe('50000000.00')
  expect(deal('БЦ Бирюзовый')?.OPPORTUNITY).toBe('0.00')
  await control({ action: 'crm_changed', project_id: project('БЦ Бирюзовый')!.id })
  await control({ action: 'resume_delivery' })
  await expect.poll(() => proof().deliveries.find(row => row.fact_event__project_id === project('БЦ Бирюзовый')?.id)?.error_code).toBe('crm_price_changed')
  await expect.poll(() => question('БЦ Бирюзовый')?.state).toBe('superseded')
  expect(deal('БЦ Бирюзовый')?.OPPORTUNITY).toBe('61000000.00')
  expect(proof().deliveries.find(row => row.fact_event__project_id === project('БЦ Бирюзовый')?.id)?.state).toBe('blocked')
  unchangedFinance('БЦ Бирюзовый')
  const afterConflict = project('БЦ Бирюзовый')!.version
  const update = 'БЦ Бирюзовый: согласовали график монтажа, бригада приступает завтра.'
  expect((await webhook(request, 'sale-question-status-after-conflict', update, group,
    { participant: '79990000002@c.us', notifyName: 'Боб' })).status()).toBe(202)
  await expect.poll(() => project('БЦ Бирюзовый')?.version).toBeGreaterThan(afterConflict)
  expect(project('БЦ Бирюзовый')?.current_action).toBe(update)
  const statusDelivery = () => proof().deliveries.find(row => row.fact_event__project_id === project('БЦ Бирюзовый')?.id && row.target_version === project('БЦ Бирюзовый')?.version)
  await expect.poll(() => statusDelivery()?.state).toBe('delivered')
  expect(statusDelivery()!.patch.OPPORTUNITY).toBeUndefined()
  expect(deal('БЦ Бирюзовый')?.OPPORTUNITY).toBe('61000000.00')

  await control({ action: 'seed', name: 'БЦ Селенит', external_id: '4106', no_writes: true })
  await expect.poll(() => question('БЦ Селенит')?.state).toBe('unconfirmed')
  const held = question('БЦ Селенит')!
  expect(held.notification_id).toBeNull()
  const heldReply = await answer(request, held, 'preserved-identity-hold', '50 млн тенге')
  await expect.poll(() => reply(heldReply)?.error_code).toBe('clarification_stale')
  expect(question('БЦ Селенит')?.state).toBe('unconfirmed')
  expect(project('БЦ Селенит')?.sale_amount).toBeNull()
  expect(deal('БЦ Селенит')?.OPPORTUNITY).toBe('0.00')

  const historyFirst = await seed('БЦ Опал', '4107')
  const pendingBody = `MZ-${historyFirst.q.id} 73 млн тенге`
  const pendingPayload = { id: 'sale-question-history-first', body: pendingBody, from: group,
    participant: lead, notifyName: 'Проверяющий', fromMe: false, timestamp: Math.floor(Date.now() / 1000) }
  await control({ action: 'history_first', question_id: historyFirst.q.id, payload: pendingPayload })
  await expect.poll(() => proof().history?.state).toBe('completed')
  expect(proof().history).toMatchObject({ imported_count: 1, existing_count: 0, scheduled_count: 0, error_code: '' })
  expect(proof().history_sources).toHaveLength(1)
  const parkedRaw = proof().history_sources[0]!
  expect(parkedRaw.processing_state).toBe('clarification_reply')
  expect(reply(pendingPayload.id)).toBeUndefined()
  expect(project('БЦ Опал')?.sale_amount).toBeNull()
  expect(deal('БЦ Опал')?.OPPORTUNITY).toBe('0.00')
  expect((await webhook(request, pendingPayload.id, pendingBody, group, pendingPayload)).status()).toBe(202)
  await expect.poll(() => question('БЦ Опал')?.state).toBe('resolved')
  expect(reply(pendingPayload.id)?.raw_message_id).toBe(parkedRaw.id)
  expect(proof().reply_raws.filter(row => row.message_id === pendingPayload.id)).toHaveLength(1)
  expect(proof().reply_traces.filter(row => row.raw_message_id === parkedRaw.id)).toEqual([
    expect.objectContaining({ raw_message_id: parkedRaw.id, model_version: 'human-whatsapp-answer' }),
  ])
  expect(project('БЦ Опал')?.sale_amount).toBe('73000000.00')
  expect(deal('БЦ Опал')?.OPPORTUNITY).toBe('73000000.00')
  unchangedFinance('БЦ Опал')

  const statusRace = await seed('БЦ Авантюрин', '4108')
  const beforeAnswerVersion = project('БЦ Авантюрин')!.version
  const pendingStatus = 'БЦ Авантюрин: получен уточнённый график работ от заказчика.'
  expect((await webhook(request, 'sale-question-status-before-answer', pendingStatus, group,
    { participant: '79990000002@c.us', notifyName: 'Боб' })).status()).toBe(202)
  await expect.poll(() => project('БЦ Авантюрин')?.version).toBeGreaterThan(beforeAnswerVersion)
  await expect.poll(() => proof().deliveries.find(row => row.fact_event__project_id === project('БЦ Авантюрин')?.id && row.target_version === project('БЦ Авантюрин')?.version)?.state).toBe('delivered')
  expect(question('БЦ Авантюрин')?.state).toBe('awaiting')
  await control({ action: 'pause_delivery' })
  await answer(request, statusRace.q, 'status-version-race', '80 млн тенге')
  await expect.poll(() => question('БЦ Авантюрин')?.state).toBe('answered')
  const acceptedPriceVersion = project('БЦ Авантюрин')!.version
  const status = 'БЦ Авантюрин: уточнили дату выезда, бригада приедет завтра.'
  expect((await webhook(request, 'sale-question-status-before-price-delivery', status, group,
    { participant: '79990000002@c.us', notifyName: 'Боб' })).status()).toBe(202)
  await expect.poll(() => project('БЦ Авантюрин')?.version).toBeGreaterThan(acceptedPriceVersion)
  expect(deal('БЦ Авантюрин')?.OPPORTUNITY).toBe('0.00')
  const genericDelivery = () => proof().deliveries.find(row => row.fact_event__project_id === project('БЦ Авантюрин')?.id && row.target_version === project('БЦ Авантюрин')?.version)
  await expect.poll(() => genericDelivery()?.id).toBeTruthy()
  expect(genericDelivery()!.patch.OPPORTUNITY).toBeUndefined()
  await control({ action: 'resume_delivery' })
  await expect.poll(() => question('БЦ Авантюрин')?.state).toBe('resolved')
  await expect.poll(() => genericDelivery()?.state).toBe('delivered')
  expect(deal('БЦ Авантюрин')?.OPPORTUNITY).toBe('80000000.00')
  expect(project('БЦ Авантюрин')?.current_action).toBe(status)
  expect(proof().deliveries.find(row => row.fact_event__project_id === project('БЦ Авантюрин')?.id && row.target_version === acceptedPriceVersion)?.state).toBe('delivered')
  unchangedFinance('БЦ Авантюрин')

  // The existing 21-deal inventory goes through actual Q2 planning and CRM reads twice.
  await control({ action: 'inventory' })
  const historical = proof().projects.filter(row => row.name.startsWith('БЦ История '))
  expect(historical).toHaveLength(21)
  const historicalIds = historical.map(row => row.id)
  const historicalQuestions = () => proof().questions.filter(row => historicalIds.includes(row.project_id))
  await expect.poll(() => historicalQuestions().length).toBe(21)
  const originalQuestionIds = historicalQuestions().map(row => row.id).sort((a, b) => a-b)
  await control({ action: 'replan', project_ids: historicalIds })
  expect(historicalQuestions().map(row => row.id).sort((a, b) => a-b)).toEqual(originalQuestionIds)
  await expect.poll(() => historicalQuestions().filter(row => row.state === 'awaiting').length).toBe(1)
  expect(historicalQuestions().filter(row => row.state === 'queued')).toHaveLength(20)
  expect(historical.every(row => row.sale_amount === null && deal(row.name)?.OPPORTUNITY === '0.00')).toBe(true)
  expect(proof().financial).toHaveLength(0)
  await control({ action: 'finish' })
})
