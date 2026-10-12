import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync, mkdirSync, unlinkSync, renameSync } from 'node:fs'
import { createHmac, createHash, randomUUID } from 'node:crypto'
import { join } from 'node:path'
import type { Candidate, Page } from '../src/types/platform'
import type { DialogueThread } from '../src/types/factReview'

const enabled = process.env.MAZORY_E2E_LAYA === '1'
test.skip(!enabled, 'Private file verification requires the explicit isolated read-only profile')
test.describe.configure({ mode: 'serial' })
test.setTimeout(600000)
const dir = process.env.MAZORY_E2E_DIR!
interface Input { id: string; title: string; messages: { ordinal: number; content: string; sender: string; timestamp: number; date: string }[] }
interface Proof { messages: { id: number; message_id: string; processed: boolean; processing_state: string }[]; traces: { id: number; raw_message_id: number; status: string; error_code: string }[]; events: { id: number; event_type: string; state: string; error_code: string }[] }
interface OracleCase { id: string; expected: { commitment?: { responsible: string; deadline_date: string; deadline_precision: string; status: string }; payment?: { amount: string; currency: string; date: string; kind: string; company: string }; project?: string; crm_id?: string; capacity_mw?: number; contract_signed?: boolean; invoice_issued?: boolean; context_invoice_amount?: string; responsible?: string; status?: string; deadline_date?: string; fulfillment_date?: string; contract_amount?: null } }
let session: { access: string; refresh: string; cookie_name: string; configs: Record<string, number> }
let inputs: Input[]
test.beforeAll(() => {
  if (!enabled) return
  session = JSON.parse(readFileSync(join(dir, 'laya-session.json'), 'utf8'))
  inputs = JSON.parse(readFileSync(join(dir, 'laya-inputs.json'), 'utf8'))
  expect(JSON.parse(readFileSync(join(dir, 'laya-parser.json'), 'utf8')).messages).toBe(586)
})

test('five original file examples pass through the browser, genuine API and Q2 with live CRM reads', async ({ page, context, request }) => {
  const control = join(dir, 'laya-control.json'), temporary = `${control}.${randomUUID()}`
  writeFileSync(temporary, JSON.stringify({ nonce: randomUUID(), mode: 'laya', live_crm: true }))
  renameSync(temporary, control)
  const headers = { Authorization: `Bearer ${session.access}` }
  context.on('response', async response => {
    if (response.url().includes('/api/auth/refresh/') && response.request().method() === 'POST' && response.ok()) {
      const body: { access: string } = await response.json()
      const cookie = (await context.cookies()).find(c => c.name === session.cookie_name)
      if (cookie) {
        session.access = body.access
        session.refresh = cookie.value
        headers.Authorization = `Bearer ${body.access}`
        writeFileSync(join(dir, 'laya-session.json'), JSON.stringify(session))
      }
    }
  })
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  mkdirSync(join(dir, 'laya-results'), { recursive: true })
  const oracle: { cases: OracleCase[] } = JSON.parse(readFileSync(join(dir, 'laya-oracle.json'), 'utf8'))
  for (const input of inputs) {
    writeFileSync(join(dir, 'laya-pause'), 'ingesting a bounded input window')
    for (const message of input.messages) {
      const data = JSON.stringify({ event: 'message', session: 'default', payload: { id: `laya-file-${input.id}-${message.ordinal}`, body: message.content,
        from: `laya-${input.id}@g.us`, participant: `${createHash('sha256').update(message.sender).digest().readUInt32BE(0)}@lid`, notifyName: message.sender, timestamp: message.timestamp } })
      const response = await request.post('/api/whatsapp/webhook/', { data, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex') } })
      expect([200, 202]).toContain(response.status())
    }
    unlinkSync(join(dir, 'laya-pause'))
    await expect.poll(() => {
      const proof: Proof = JSON.parse(readFileSync(join(dir, 'laya-proof.json'), 'utf8'))
      const messages = proof.messages.filter(m => m.message_id.startsWith(`laya-file-${input.id}-`))
      return messages.length === input.messages.length && messages.every(m => m.processed)
    }, { timeout: 540000, intervals: [1000, 2000, 5000] }).toBe(true)
    await expect.poll(async () => {
      const health = await (await request.get('/api/operations-health/', { headers })).json()
      return health.outbox.filter((row: { event_type: string; state: string; count: number }) => row.event_type === 'crm_match' && ['pending','enqueued','processing'].includes(row.state)).reduce((sum: number, row: { count: number }) => sum + row.count, 0)
    }, { timeout: 300000 }).toBe(0)
    const threads: Page<DialogueThread> = await (await request.get('/api/threads/?page_size=100', { headers })).json()
    const selected = threads.results.filter(t => t.chat_name === input.title)
    const details: DialogueThread[] = []
    for (const thread of selected) details.push(await (await request.get(`/api/threads/${thread.id}/`, { headers })).json())
    const candidatesById = new Map<number, Candidate>()
    for (const status of ['pending','approved','rejected']) {
      let next: string | null = `/api/candidates/?status=${status}`
      while (next) {
        const result: Page<Candidate> = await (await request.get(next, { headers })).json()
        for (const candidate of result.results.filter(c => c.chat_name === input.title)) candidatesById.set(candidate.id, candidate)
        next = result.next
      }
    }
    const candidates = [...candidatesById.values()]
    const proof: Proof = JSON.parse(readFileSync(join(dir, 'laya-proof.json'), 'utf8'))
    writeFileSync(join(dir, `laya-results/${input.id}.json`), JSON.stringify({ input, threads: details, candidates, proof }, null, 2))
    const expected = oracle.cases.find(c => c.id === input.id)!.expected
    const normalize = (value: string) => value.toLocaleLowerCase().replace(/[«»/]/g, ' ').replace(/\s+/g, ' ').trim()
    const facts = candidates.filter(c => ['pending','approved'].includes(c.status))
    if (expected.commitment) {
      const obligations = facts.filter(c => c.fact_type === 'commitment')
      expect.soft(obligations, input.id).toHaveLength(1)
      const fact = obligations[0]?.proposed_changes
      expect.soft(fact, input.id).toMatchObject({ responsible_name: expected.commitment.responsible, commitment_status: expected.commitment.status, deadline_precision: expected.commitment.deadline_precision })
      expect.soft(String(fact?.deadline_at).slice(0,10), input.id).toBe(expected.commitment.deadline_date)
      expect.soft(/таблиц/i.test(String(fact?.commitment_text)+' '+String(fact?.object_name)),input.id).toBe(true)
      expect.soft(obligations[0]?.source_metadata?.sender,input.id).toBe(expected.commitment.responsible)
      expect.soft(facts.filter(c => c.fact_type === 'project'), input.id).toHaveLength(0)
    }
    if (expected.payment) {
      const receipts = facts.filter(c => c.fact_type === 'payment' && c.proposed_changes.payment_kind === 'increment')
      expect.soft(receipts, input.id).toHaveLength(1)
      expect.soft(receipts[0]?.proposed_changes, input.id).toMatchObject({ amount: expected.payment.amount, currency: expected.payment.currency, payment_date: expected.payment.date, payment_kind: expected.payment.kind })
      expect.soft(receipts[0]?.proposed_changes.company_name, input.id).toBe(expected.payment.company)
      expect.soft(receipts.every(c=>c.project_id===null||normalize(String(c.current_values?.company_name))===normalize(expected.payment!.company)),input.id).toBe(true)
    }
    if (expected.project && !expected.status) {
      const projects = facts.filter(c => c.fact_type === 'project' && normalize(String(c.proposed_changes.object_name)).includes(normalize(expected.project!)))
      expect.soft(projects.length, input.id).toBeGreaterThan(0)
      if(expected.crm_id) expect.soft(projects.some(c=>c.crm_resolution.options.some(o=>o.bitrix_deal_id===expected.crm_id)),input.id).toBe(true)
      if (expected.contract_signed) expect.soft(projects.some(c => /подпис/i.test(String(c.proposed_changes.current_action))), input.id).toBe(true)
      if (expected.capacity_mw) expect.soft(projects.some(c => new RegExp(`\\b${expected.capacity_mw}\\s*МВт`, 'i').test(String(c.proposed_changes.current_action))), input.id).toBe(true)
      if (expected.contract_amount === null) expect.soft(projects.every(c => c.proposed_changes.contract_amount == null), input.id).toBe(true)
      if(expected.invoice_issued) {
        const invoices = facts.filter(c=>c.fact_type==='payment'&&c.proposed_changes.payment_kind==='invoice')
        expect.soft(invoices.length,input.id).toBeGreaterThan(0)
        expect.soft(invoices.every(c=>normalize(String(c.proposed_changes.object_name)).includes(normalize(expected.project!))),input.id).toBe(true)
        const invoiceDate = input.messages.at(-1)!.date.split('.').reverse().join('-')
        expect.soft(invoices.every(c=>c.proposed_changes.direction==='income' && c.proposed_changes.payment_date===invoiceDate),input.id).toBe(true)
        if(expected.crm_id) expect.soft(invoices.some(c=>c.crm_resolution.options.some(o=>o.bitrix_deal_id===expected.crm_id)),input.id).toBe(true)
        // The frozen oracle labels this amount as context from a question.
        // The answer confirms issuance, but need not confirm the amount.
        expect.soft(invoices.every(c=>c.proposed_changes.amount == null || String(c.proposed_changes.amount)===expected.context_invoice_amount),input.id).toBe(true)
      }
      expect.soft(facts.some(c => c.fact_type === 'payment' && c.proposed_changes.payment_kind === 'increment'), input.id).toBe(false)
    }
    if (expected.status) {
      const obligations = facts.filter(c => c.fact_type === 'commitment' && c.proposed_changes.responsible_name === expected.responsible)
      const fulfilled = obligations.filter(c => c.proposed_changes.commitment_status === expected.status)
      expect.soft(fulfilled, input.id).toHaveLength(1)
      expect.soft(String(fulfilled[0]?.proposed_changes.deadline_at).slice(0,10), input.id).toBe(expected.deadline_date)
      expect.soft(String(fulfilled[0]?.proposed_changes.fulfilled_at).slice(0,10), input.id).toBe(expected.fulfillment_date)
      expect.soft(fulfilled[0]?.proposed_changes.deadline_precision, input.id).toBe('date')
      expect.soft(facts.some(c => c.fact_type === 'payment' && c.proposed_changes.payment_kind === 'increment'), input.id).toBe(false)
    }
    await page.goto('/')
    await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
    await page.getByRole('button', { name: 'Темы переписки', exact: true }).click()
    await page.getByPlaceholder('Поиск по теме, компании, сделке...').fill(selected[0]?.topic || input.title)
    await page.getByRole('button', { name: 'Найти', exact: true }).click()
    if (selected[0]) await page.getByText(`#${selected[0].id}: ${selected[0].topic}`, { exact: true }).click()
    await expect(page.getByTestId('thread-routing-status')).toBeVisible()
    await page.screenshot({ path: join(dir, `laya-results/${input.id}.png`), fullPage: true })
  }
})
