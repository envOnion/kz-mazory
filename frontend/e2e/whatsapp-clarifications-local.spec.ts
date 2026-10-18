import { readFileSync } from './helpers/files'
import { test, expect, type APIRequestContext } from '@playwright/test'
import { createHmac, randomUUID } from 'node:crypto'
import { writeFileSync, renameSync, chmodSync, existsSync } from 'node:fs'
import { join } from 'node:path'

const dir = process.env.MAZORY_E2E_DIR!
test.skip(process.env.MAZORY_E2E_REAL_MODEL === '1', 'Isolated provider scenario')
interface Question { id: number; state: string; notification_id: number; candidate_id: number; candidate__trace__raw_message__message_id: string; options: { deal_id: string; title: string }[] }
interface Proof { questions: Question[]; replies: { provider_message_id: string; state: string; error_code: string }[]; decisions: {id: number; status: string; decisions__reason_code: string}[]; candidates: {id: number; status: string; trace__raw_message__message_id: string; decisions__reason_code: string}[]; raw_ids: string[]; financial: { candidate_id: number; project__bitrix_id: string; amount: string; amount_precision: string }[]; deliveries: { state: string; fact_event__decision__candidate_id: number }[] }
interface Provider { waha_messages?: { id: string; chatId: string; text: string }[]; comments?: { ENTITY_ID: string; COMMENT: string }[]; deals?: Record<string, { OPPORTUNITY: string; TITLE: string }>; [key: string]: unknown }
const proof = (): Proof => JSON.parse(readFileSync(join(dir, 'clarification-proof.json'), 'utf8'))
interface Proof { notification_deliveries: {id: number; notification_id: number; state: string; provider_message_id: string}[]; notification_events: {state: string; payload__delivery_id: number}[] }
interface Proof { matches: {candidate_id: number; crm_match_revision: number; bitrix_deal_id: string}[] }
interface Proof { deadlines: {trace__raw_message__message_id: string; proposed_changes__deadline_at: string | null; proposed_changes__deadline_precision: string}[] }
const provider = (): Provider => JSON.parse(readFileSync(join(dir, 'provider-state.json'), 'utf8'))
const question = (id: string) => proof().questions.find(row => row.candidate__trace__raw_message__message_id === id)
function write(path: string, value: Record<string, unknown>) { const tmp = path+randomUUID(); writeFileSync(tmp,JSON.stringify(value)); chmodSync(tmp,0o666); renameSync(tmp,path) }
async function control(value: Record<string, unknown>) { const nonce = randomUUID(); write(join(dir,'clarification-control.json'),{nonce,...value}); await expect.poll(() => existsSync(join(dir,'clarification-applied.json')) ? JSON.parse(readFileSync(join(dir,'clarification-applied.json'),'utf8')).nonce : null).toBe(nonce) }
async function webhook(request: APIRequestContext, id: string, body: string, from: string, extra: Record<string, unknown> = {}) {
  const data = JSON.stringify({event:'message.any', session:'default', payload:{id,body,from,fromMe:false,timestamp:Math.floor(Date.now()/1000)-5,...extra}})
  return request.post('/api/whatsapp/webhook/', {data,headers:{'Content-Type':'application/json','X-Webhook-Hmac':createHmac('sha512','isolated-local-webhook-secret').update(data).digest('hex')}})
}
test.setTimeout(300000)
test('WhatsApp clarification survives Q2 restart, validates answers, reads CRM back and retains unknown facts', async ({ request, page, context }) => {
  const group = 'clarification-e2e@g.us'
  await control({action:'start',group})
  const session: { refresh: string; cookie_name: string } = JSON.parse(readFileSync(join(dir,'clarification-session.json'),'utf8'))
  expect((await webhook(request,'clarification-parus','Парус: оплата 12 млн тенге поступила сегодня',group,{participant:'79990000002@c.us',notifyName:'Боб'})).status()).toBe(202)
  await expect.poll(() => question('clarification-parus')?.state).toBe('awaiting')
  const q = question('clarification-parus')!
  await expect.poll(() => provider().waha_messages?.find(row => row.text.includes(`MZ-${q.id} `))?.id).toBeTruthy()
  const sent = provider().waha_messages!.find(row => row.text.includes(`MZ-${q.id} `))!
  await expect.poll(() => proof().notification_deliveries.find(row => row.notification_id === q.notification_id)?.provider_message_id).toBe(sent.id)
  const sentDelivery = proof().notification_deliveries.find(row => row.notification_id === q.notification_id)!
  await expect.poll(() => proof().notification_events.find(row => row.payload__delivery_id === sentDelivery.id)?.state).toBe('done')
  expect(provider().waha_messages!.filter(row => row.text.includes(`MZ-${q.id} `))).toHaveLength(1)
  expect(sent.chatId).toBe('79990000001@c.us')
  expect(sent.text).toContain('12 000 000 KZT')
  expect(sent.text).toContain('Почему нужен ответ:')
  expect(sent.text).toContain('№701')
  expect(sent.text).toContain('№702')
  expect(proof().financial.some(row => row.candidate_id === q.candidate_id)).toBe(false)
  await context.addCookies([{name:session.cookie_name,value:session.refresh,url:`${process.env.MAZORY_E2E_URL}/api/auth/`,httpOnly:true,sameSite:'Lax'}])
  await page.goto('/')
  await page.getByTitle('Уведомления', {exact:true}).click()
  await expect(page.getByText('Нужно уточнить сделку Bitrix', {exact:true})).toBeVisible()
  await expect(page.getByText('Ожидается ответ в WhatsApp', {exact:true})).toBeVisible()
  await page.screenshot({path:join(dir,'clarification-question.png'),fullPage:true})
  expect((await webhook(request,'clarification-unknown','Маяк: оплата 12 млн тенге поступила сегодня',group,{participant:'79990000002@c.us'})).status()).toBe(202)
  await expect.poll(() => question('clarification-unknown')?.state).toBe('queued')
  expect(provider().waha_messages!.filter(row => row.text.includes('MZ-'))).toHaveLength(1)
  expect((await webhook(request,'clarification-echo',sent.text,'79990000001@c.us',{fromMe:true,to:'79990000001@c.us'})).status()).toBe(200)
  expect((await webhook(request,'clarification-attacker',`MZ-${q.id} 2`,'79990000002@c.us')).status()).toBe(403)
  expect((await webhook(request,'clarification-forged-own',`MZ-${q.id} 2`,'79990000002@c.us',{fromMe:true,to:'79990000001@c.us'})).status()).toBe(403)
  expect((await webhook(request,'clarification-opaque',`MZ-${q.id} 2`,group,{participant:'79990000001@lid'})).status()).toBe(403)
  expect(proof().replies.some(row => row.provider_message_id === 'clarification-attacker')).toBe(false)
  await control({action:'pause_reply'})
  const answer = {fromMe:true,to:'79990000001@c.us',_data:{quotedStanzaID:sent.id.split('_').at(-1)}}
  expect((await webhook(request,'clarification-answer','2','79990000001@c.us',answer)).status()).toBe(202)
  expect((await webhook(request,'clarification-answer','2','79990000001@c.us',answer)).status()).toBe(202)
  await expect.poll(() => proof().replies.find(row => row.provider_message_id === 'clarification-answer')?.state).toBe('queued')
  writeFileSync(join(dir,'clarification-restart-required'),'restart at persisted reply')
  await expect.poll(() => existsSync(join(dir,'clarification-restart-done')), {timeout:60000}).toBe(true)
  await control({action:'resume_reply'})
  await expect.poll(() => question('clarification-parus')?.state).toBe('resolved')
  expect(proof().financial.filter(row => row.candidate_id === q.candidate_id)).toEqual([{candidate_id:q.candidate_id,project__bitrix_id:'702',amount:'12000000.00',amount_precision:'exact'}])
  expect(proof().deliveries.filter(row => row.fact_event__decision__candidate_id === q.candidate_id).every(row => ['delivered','superseded'].includes(row.state))).toBe(true)
  expect(provider().deals?.['702']?.OPPORTUNITY).toBe('75000000.00')
  expect(provider().comments?.filter(row => row.ENTITY_ID === '702').length).toBe(1)
  expect((await webhook(request,'clarification-repeat-answer',`MZ-${q.id} 1`,'79990000001@c.us')).status()).toBe(202)
  await expect.poll(() => proof().replies.find(row => row.provider_message_id === 'clarification-repeat-answer')?.error_code).toBe('clarification_stale')
  expect(proof().financial.filter(row => row.candidate_id === q.candidate_id)).toHaveLength(1)
  expect(proof().raw_ids.some(id => id.startsWith('clarification-answer') || id === 'clarification-echo' || id === 'clarification-repeat-answer')).toBe(false)
  await expect.poll(() => question('clarification-unknown')?.state).toBe('awaiting')
  const unknown = question('clarification-unknown')!
  expect((await webhook(request,'clarification-dont-know',`MZ-${unknown.id} оставь без подтверждения, я не знаю`,'79990000001@c.us')).status()).toBe(202)
  await expect.poll(() => question('clarification-unknown')?.state).toBe('unconfirmed')
  await control({action:'unknown_exact',question_id:unknown.id})
  expect(provider().deals?.['740']?.TITLE).toBe('Маяк')
  expect((await webhook(request,'clarification-unknown-followup','Маяк: оплата 12 млн тенге поступила сегодня, повторный отчёт',group,{participant:'79990000002@c.us'})).status()).toBe(202)
  await expect.poll(() => proof().raw_ids.includes('clarification-unknown-followup')).toBe(true)
  await expect.poll(() => proof().candidates.find(row => row.trace__raw_message__message_id === 'clarification-unknown-followup' && row.decisions__reason_code === 'user_requested_unconfirmed')?.status).toBe('pending')
  const repeatedUnknown = proof().candidates.find(row => row.trace__raw_message__message_id === 'clarification-unknown-followup')!
  expect(proof().financial.some(row => row.candidate_id === repeatedUnknown.id)).toBe(false)
  await expect.poll(() => proof().questions.length).toBe(2)
  expect(proof().financial.some(row => row.candidate_id === unknown.candidate_id)).toBe(false)
  expect((await webhook(request,'clarification-stale','Риф: оплата 12 млн тенге поступила сегодня',group,{participant:'79990000002@c.us'})).status()).toBe(202)
  await expect.poll(() => question('clarification-stale')?.state).toBe('awaiting')
  const stale = question('clarification-stale')!
  await control({action:'stale',question_id:stale.id})
  expect((await webhook(request,'clarification-stale-answer',`MZ-${stale.id} 1`,'79990000001@c.us')).status()).toBe(202)
  await expect.poll(() => question('clarification-stale')?.state).toBe('superseded')
  expect(proof().financial.some(row => row.candidate_id === stale.candidate_id)).toBe(false)
  await control({action:'review_hold',question_id:stale.id})
  await expect.poll(() => proof().decisions.filter(row => row.id === stale.candidate_id).length).toBeGreaterThanOrEqual(2)
  expect(proof().decisions.filter(row => row.id === stale.candidate_id).every(row => row.status === 'pending')).toBe(true)
  expect(proof().deliveries.some(row => row.fact_event__decision__candidate_id === stale.candidate_id)).toBe(false)
  expect((await webhook(request,'clarification-delivery-note','Накладная: закрыли накладную на 144 млн тенге',group,{participant:'79990000002@c.us'})).status()).toBe(202)
  await expect.poll(() => proof().candidates.find(row => row.trace__raw_message__message_id === 'clarification-delivery-note' && row.decisions__reason_code === 'delivery_note_not_invoice')?.status).toBe('pending')
  const note = proof().candidates.find(row => row.trace__raw_message__message_id === 'clarification-delivery-note')!
  expect(proof().financial.some(row => row.candidate_id === note.id)).toBe(false)
  expect(proof().deliveries.some(row => row.fact_event__decision__candidate_id === note.id)).toBe(false)
  expect((await webhook(request,'clarification-clinic','Интегра — поликлиника Шугла: оплата 12 млн тенге поступила сегодня',group,{participant:'79990000002@c.us'})).status()).toBe(202)
  await expect.poll(() => question('clarification-clinic')?.state).toBe('awaiting')
  const clinic = question('clarification-clinic')!
  expect(clinic.options.map(row => row.deal_id)).toEqual(['731'])
  await expect.poll(() => provider().waha_messages?.find(row => row.text.includes(`MZ-${clinic.id} `))?.text).toContain('№731')
  expect(provider().waha_messages!.find(row => row.text.includes(`MZ-${clinic.id} `))!.text).not.toContain('№732')
  expect((await webhook(request,'clarification-clinic-unknown',`MZ-${clinic.id} не знаю`,'79990000001@c.us')).status()).toBe(202)
  await expect.poll(() => question('clarification-clinic')?.state).toBe('unconfirmed')
  const previousRevision = Math.max(...proof().matches.map(row => row.crm_match_revision))
  await control({action:'clinic_prelink',question_id:clinic.id})
  await expect.poll(() => proof().matches.some(row => row.crm_match_revision > previousRevision && row.bitrix_deal_id === '731')).toBe(true)
  expect(proof().financial.some(row => row.candidate_id === clinic.candidate_id)).toBe(false)
  expect((await webhook(request,'clarification-thursday','Срок четверг: подготовлю сравнительную таблицу до четверга',group,{participant:'79990000002@c.us',notifyName:'Боб',timestamp:1791878400})).status()).toBe(202)
  await expect.poll(() => proof().deadlines.find(row=>row.trace__raw_message__message_id==='clarification-thursday')?.proposed_changes__deadline_at?.slice(0,10)).toBe('2026-10-15')
  expect(proof().deadlines.find(row=>row.trace__raw_message__message_id==='clarification-thursday')?.proposed_changes__deadline_precision).toBe('date')
  await control({action:'finish'})
})
