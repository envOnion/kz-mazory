import { readFileSync } from './helpers/files'
import { authenticateShell } from './helpers/session'
import { test, expect, type APIRequestContext } from '@playwright/test'
import { chmodSync, existsSync, renameSync, writeFileSync } from 'node:fs'
import { createHmac, randomUUID } from 'node:crypto'
import { join } from 'node:path'
import type { Candidate } from '../src/types/platform'

const dir = process.env.MAZORY_E2E_DIR!
const group = 'continuous-sale-prices@g.us'
const amber = 'БЦ Янтарный'
const granite = 'БЦ Гранитный'
interface SaleProject {
  id: number; name: string; sale_amount: string | null; contract_known: boolean
  contract_amount: string; paid_amount: string; bitrix_id: string | null
}
interface Proof {
  control_nonce: string | null
  sale_projects: SaleProject[]
  sale_financial_records: { id: number; project_id: number; amount: string; status: string }[]
  sale_candidates: { id: number; fact_type: string; status: string; trace__raw_message__message_id: string; proposed_changes: Record<string, unknown> }[]
  sale_assertions: { id: number; project_id: number; value: string; fact_event__decision__candidate_id: number | null }[]
  sale_decisions: { candidate_id: number; validation: { discarded_fields?: { sale_amount?: string } } }[]
  deliveries: { id: number; state: string; patch: Record<string, unknown>; external_object_link__external_id: string | null }[]
  journal: { id: number; method: string; status: string; external_id: string; payload: { fields?: { OPPORTUNITY?: string } } }[]
}
interface Provider { deals?: Record<string, { ID: string; TITLE: string; OPPORTUNITY?: string; CURRENCY_ID?: string }> }
function proof(): Proof { return JSON.parse(readFileSync(join(dir, 'continuous-proof.json'), 'utf8')) }
function provider(): Provider { return JSON.parse(readFileSync(join(dir, 'provider-state.json'), 'utf8')) }
function project(name: string) { return proof().sale_projects.find(row => row.name === name) }
function deal(name: string) { return Object.values(provider().deals || {}).find(row => row.TITLE === name) }

async function start() {
  const nonce = randomUUID(), target = join(dir, 'continuous-control.json'), temporary = `${target}.${nonce}`
  writeFileSync(temporary, JSON.stringify({ nonce, action: 'start', group }))
  chmodSync(temporary, 0o666)
  renameSync(temporary, target)
  await expect.poll(() => existsSync(join(dir, 'continuous-applied.json')) ? JSON.parse(readFileSync(join(dir, 'continuous-applied.json'), 'utf8')).nonce : null).toBe(nonce)
  await expect.poll(() => proof().control_nonce).toBe(nonce)
}

async function message(request: APIRequestContext, suffix: string, body: string, timestamp: number) {
  const data = JSON.stringify({ event: 'message', session: 'default', payload: {
    id: `continuous-sale-${suffix}`, body, from: group,
    participant: '79990000002@c.us', notifyName: 'Боб', timestamp,
  } })
  const response = await request.post('/api/whatsapp/webhook/', { data, headers: {
    'Content-Type': 'application/json',
    'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex'),
  } })
  expect([200, 202]).toContain(response.status())
}

test.skip(process.env.MAZORY_E2E_REAL_MODEL === '1', 'Portable isolated external HTTP provider required')
test.setTimeout(240000)

test('reported sale and own quotation update Bitrix without a signed contract, preserve payments, and reject stale or unrelated prices', async ({ request, page }) => {
  await start()
  const session: { access: string } = JSON.parse(readFileSync(join(dir, 'continuous-session.json'), 'utf8'))
  const headers = { Authorization: `Bearer ${session.access}` }
  const base = Math.floor(Date.now() / 1000) - 120
  async function fact(suffix: string, name: string, reason?: string, kind = 'project') {
    const row = () => proof().sale_candidates.find(item => item.trace__raw_message__message_id === `continuous-sale-${suffix}` && item.fact_type === kind && item.proposed_changes.object_name === name)
    await expect.poll(() => row()?.id).toBeTruthy()
    const response = () => request.get(`/api/candidates/${row()!.id}/`, { headers })
    await expect.poll(async () => {
      const value: Candidate = await (await response()).json()
      return reason ? value.automatic_decision?.reason_code : value.status
    }).toBe(reason || 'approved')
    if (!reason) await expect.poll(() => row()?.status).toBe('approved')
    return row()!
  }
  async function amount(name: string, value: string) {
    await expect.poll(() => deal(name)?.OPPORTUNITY).toBe(value)
    await expect.poll(() => project(name)?.sale_amount).toBe(value)
    await expect.poll(() => proof().deliveries.some(row => row.state === 'delivered' && row.external_object_link__external_id === deal(name)?.ID && row.patch.OPPORTUNITY === value)).toBe(true)
  }

  await message(request, 'unsigned', 'БЦ Янтарный: договор передан на подпись. Сумма продажи: 29 428 679 тг. Планируемая прибыль: 3 998 607 тг.', base)
  await fact('unsigned', amber)
  await amount(amber, '29428679.00')
  expect(deal(amber)?.CURRENCY_ID).toBe('KZT')
  expect(project(amber)).toMatchObject({ contract_known: false, contract_amount: '0.00', paid_amount: '0.00' })
  expect(proof().sale_financial_records).toHaveLength(0)

  await message(request, 'quotation', 'БЦ Гранитный: наше КП направлено заказчику на сумму 18 000 000 тг.', base + 1)
  await fact('quotation', granite)
  await amount(granite, '18000000.00')
  expect(project(granite)).toMatchObject({ contract_known: false, contract_amount: '0.00', paid_amount: '0.00' })
  // A commercial offer establishes the deal's sale value, not an invoice or receipt.
  expect(proof().sale_financial_records).toHaveLength(0)

  const ownQuoteWordings = [
    ['quote-sum', 'БЦ Гранитный: Сумма коммерческого предложения: 18 млн тг.'],
    ['quote-original', 'БЦ Гранитный: Стоимость по первоначальному коммерческому предложению: 18 млн тг.'],
    ['quote-tender', 'БЦ Гранитный: Тендер выигран нами с предложением на сумму 18 млн тг. Конкурент предложил 17.5 млн тг.'],
  ] as const
  for (const [suffix, text] of ownQuoteWordings) {
    await message(request, suffix, text, base + 1)
    await fact(suffix, granite)
    await amount(granite, '18000000.00')
    expect(project(granite)).toMatchObject({ contract_known: false, contract_amount: '0.00', paid_amount: '0.00' })
    expect(proof().sale_financial_records).toHaveLength(0)
  }

  await message(request, 'inline-profit', 'БЦ Янтарный: сумма продажи 29 428 679 тг, планируемая прибыль 3 998 607 тг', base + 1)
  await fact('inline-profit', amber)
  await amount(amber, '29428679.00')
  expect(project(amber)).toMatchObject({ contract_known: false, contract_amount: '0.00', paid_amount: '0.00' })
  expect(proof().sale_financial_records).toHaveLength(0)

  const exactAssertions = proof().sale_assertions.filter(row => row.project_id === project(amber)?.id).length
  await message(request, 'rounded', 'БЦ Янтарный: сумма продажи 29.43 млн тг. Договор на подписи.', base + 2)
  const rounded = await fact('rounded', amber)
  expect(project(amber)?.sale_amount).toBe('29428679.00')
  expect(deal(amber)?.OPPORTUNITY).toBe('29428679.00')
  expect(proof().sale_assertions.filter(row => row.project_id === project(amber)?.id)).toHaveLength(exactAssertions)
  expect(proof().sale_decisions.some(row => row.candidate_id === rounded.id && row.validation.discarded_fields?.sale_amount?.includes('Округлённый'))).toBe(true)
  await message(request, 'explicit-rounded', 'БЦ Янтарный: теперь новая сумма продажи 29.43 млн тг.', base + 3)
  await fact('explicit-rounded', amber)
  await amount(amber, '29430000.00')

  await message(request, 'lower', 'БЦ Янтарный: пересмотрели предложение. Сумма продажи: 27 000 000 тг.', base + 4)
  await fact('lower', amber)
  await amount(amber, '27000000.00')
  await message(request, 'older', 'БЦ Янтарный: первоначальная сумма продажи: 31 000 000 тг.', base - 86400)
  await fact('older', amber, 'older_source_revision')
  expect(deal(amber)?.OPPORTUNITY).toBe('27000000.00')

  const unrelated = [
    ['competitor', amber, 'БЦ Янтарный: конкурент предложил котельную за 22 000 000 тг.'],
    ['payment', amber, 'БЦ Янтарный: поступила оплата 3 000 000 тг.'],
    ['profit', amber, 'БЦ Янтарный: планируемая прибыль 3 998 607 тг.'],
    ['invoice', granite, 'БЦ Гранитный: выставлен счёт на аванс 5 000 000 тг.'],
    ['installment', granite, 'БЦ Гранитный: наше КП на сумму 18 млн тг, ежемесячный платёж 3 млн тг.'],
  ] as const
  for (const [index, [suffix, name, text]] of unrelated.entries()) {
    await message(request, suffix, text, base + 5 + index)
    if (suffix === 'payment') {
      // Extraction already removes the erroneous project duplicate when the
      // same source supplies a genuine payment. Its receipt must still apply.
      await fact(suffix, name, undefined, 'payment')
      expect(proof().sale_candidates.some(row => row.trace__raw_message__message_id === 'continuous-sale-payment' && row.fact_type === 'project')).toBe(false)
      expect(proof().sale_assertions.some(row => row.project_id === project(amber)?.id && row.value === '3000000.00')).toBe(false)
    } else {
      await fact(suffix, name, 'unsupported_sale_amount')
    }
    expect(deal(amber)?.OPPORTUNITY).toBe('27000000.00')
    expect(deal(granite)?.OPPORTUNITY).toBe('18000000.00')
  }
  await fact('payment', amber, undefined, 'payment')
  await expect.poll(() => proof().sale_financial_records.filter(row => row.project_id === project(amber)?.id && row.status === 'received').map(row => row.amount)).toEqual(['3000000.00'])
  expect(proof().sale_financial_records.filter(row => row.project_id === project(granite)?.id)).toHaveLength(0)

  await message(request, 'unknown', 'БЦ Янтарный: заказчик рассматривает наше предложение, новая цена не названа.', base + 10)
  await fact('unknown', amber)
  expect(project(amber)?.sale_amount).toBe('27000000.00')
  expect(deal(amber)?.OPPORTUNITY).toBe('27000000.00')

  await message(request, 'wrong-section', 'Отчёт по стоимости БЦ:\nБЦ Янтарный:\nСумма продажи: 26 000 000 тг.\nБЦ Гранитный:\nСумма продажи: 19 000 000 тг.', base + 11)
  await fact('wrong-section', amber, 'unsupported_sale_amount')
  await fact('wrong-section', granite)
  await amount(granite, '19000000.00')
  expect(project(amber)?.sale_amount).toBe('27000000.00')
  expect(deal(amber)?.OPPORTUNITY).toBe('27000000.00')

  await message(request, 'right-section', 'Отчёт по стоимости БЦ: актуальные предложения\nБЦ Янтарный:\nСумма продажи: 26 000 000 тг.\nПланируемая прибыль: 2 000 000 тг.\nБЦ Гранитный:\nСумма продажи: 19 000 000 тг.', base + 12)
  await fact('right-section', amber)
  await fact('right-section', granite)
  await amount(amber, '26000000.00')
  await amount(granite, '19000000.00')
  expect(project(amber)).toMatchObject({ contract_known: false, contract_amount: '0.00', paid_amount: '3000000.00' })
  expect(project(granite)).toMatchObject({ contract_known: false, contract_amount: '0.00', paid_amount: '0.00' })
  expect(proof().sale_financial_records).toHaveLength(1)
  expect(Object.values(provider().deals || {}).filter(row => [amber, granite].includes(row.TITLE))).toHaveLength(2)

  // The real journal UI exposes the final request after Q2 and readback finish.
  const receipt = () => proof().journal.find(row => row.method === 'crm.deal.update' && row.status === 'accepted' && row.external_id === deal(amber)?.ID && row.payload.fields?.OPPORTUNITY === '26000000.00')
  await expect.poll(() => receipt()?.id).toBeTruthy()
  await authenticateShell(page.context())
  await page.goto(`/crm-changes?receipt=${receipt()!.id}`)
  await expect(page.locator('[data-crm-id]').filter({ hasText: 'OPPORTUNITY' }).first()).toContainText('26000000.00')
  await expect(page.getByText('crm.deal.update', { exact: true })).toBeVisible()
  await page.screenshot({ path: join(dir, 'sale-amount-journal.png'), fullPage: true })
})
