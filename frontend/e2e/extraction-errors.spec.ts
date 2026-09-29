import { test, expect, type Page } from '@playwright/test'
import { adminLogin, isolatedCommand } from './auth-helper'

const guard = "from django.conf import settings; assert settings.INTEGRATION_TEST_MODE; assert str(settings.DATABASES['default']['NAME']).endswith('_e2e'); "
const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
interface Seed { trace_id: number; raw_id: number; target: string }
interface State {
  status: string; error: string; proposals: number; processed: boolean;
  quotes: string[];
  event: { state: string; attempt_count: number }; history_run: number;
  diagnostics: { finish_reason?: string; output_tokens?: number; response_characters?: number; field_errors?: unknown; source_whitespace_restored?: number[]; evidence_match?: string }
}
function state(id: number): State {
  return JSON.parse(isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
import json
from api.models import MessageProcessingTrace,OutboxEvent
t=MessageProcessingTrace.objects.get(pk=${id})
print(json.dumps({'status':t.status,'error':t.error_code,'proposals':t.candidates.count(),'quotes':list(t.candidates.values_list('evidence__quote',flat=True)),'processed':t.raw_message.processed,'event':OutboxEvent.objects.filter(payload__trace_id=t.id).values('state','attempt_count').get(),'diagnostics':t.context_metadata.get('response_diagnostics',{}),'history_run':t.raw_message.history_import_runs.first().id}))
`])) as State
}
async function start(page: Page, scenario: string): Promise<{ id: number; seed: Seed }> {
  const seed = JSON.parse(isolatedCommand('seed_context_e2e', [scenario, '--count', '3'])) as Seed
  // A real failed history source awaiting an explicit new attempt.
  isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
from api.models import RawMessage,WhatsAppHistoryJob,WhatsAppHistoryRun
r=RawMessage.objects.get(pk=${seed.raw_id}); r.processed=False; r.processing_state='failed'; r.save()
j=WhatsAppHistoryJob.objects.create(config=r.config)
h=WhatsAppHistoryRun.objects.create(job=j,state='completed_with_errors'); h.messages.add(r)
`])
  await page.goto(`/admin/api/messageprocessingtrace/${seed.trace_id}/change/`)
  await page.getByRole('button', { name: 'Повторить анализ с полной историей', exact: true }).click()
  await expect(page).not.toHaveURL(new RegExp(`/messageprocessingtrace/${seed.trace_id}/change/`))
  const match = page.url().match(/messageprocessingtrace\/(\d+)\/change/)
  if (!match) throw new Error('Missing new trace URL')
  return { id: parseInt(match[1]!, 10), seed }
}

test('target follows full reference history; fenced JSON and absent optional fields reach review safely', async ({ page, request }) => {
  test.setTimeout(120000)
  await adminLogin(page)
  const { id, seed } = await start(page, 'fenced')
  await expect.poll(() => state(id).status, { timeout: 60000 }).toBe('success')
  expect(state(id).proposals).toBe(1)
  expect(state(id).processed).toBe(true)
  const captured = await (await request.get(`${provider}/test/ai-requests`)).json() as { payloads: { messages: { content: string }[] }[] }
  const payload = captured.payloads.find(p => JSON.parse(p.messages[1]!.content).content === seed.target)!
  const input = JSON.parse(payload.messages[1]!.content) as { context: unknown[]; content: string }
  expect(input.context).toHaveLength(3)
  expect(Object.keys(input).at(-1)).toBe('content')
  expect(payload.messages[0]!.content).toContain('ТОЛЬКО из одного целевого сообщения')
  await page.reload()
  const result = page.getByTestId('trace-result')
  await expect(result).toContainText('Предложено фактов: 1')
  await expect(result).toContainText('требуют проверки человеком')
  await result.locator('summary').click()
  await expect(result.locator('pre')).toContainText('<img src=x')
  await expect(result.locator('img')).toHaveCount(0)
  expect(await page.evaluate(() => Reflect.get(window, '__xss'))).toBeUndefined()
  await expect(result).not.toContainText('<em class=')
})

test('upstream overload in HTTP 200 is retried with the same saved request and then succeeds', async ({ page }) => {
  test.setTimeout(150000)
  await adminLogin(page)
  const { id } = await start(page, 'upstream_overloaded')
  await expect.poll(() => state(id).error, { timeout: 45000 }).toBe('provider_overloaded')
  expect(state(id).event.state).toBe('pending')
  expect(state(id).proposals).toBe(0)
  await page.reload()
  await expect(page.getByTestId('trace-result')).toContainText('Провайдер AI перегружен')
  await expect.poll(() => state(id).event.state, { timeout: 100000 }).toBe('done')
  expect(state(id).status).toBe('success')
  expect(state(id).event.attempt_count).toBe(2)
  expect(state(id).proposals).toBe(1)
})

test('whitespace-folded evidence is restored to the exact unique source substring', async ({ page }) => {
  test.setTimeout(120000)
  await adminLogin(page)
  const { id } = await start(page, 'whitespace_evidence')
  await expect.poll(() => state(id).status, { timeout: 60000 }).toBe('success')
  const result = state(id)
  expect(result.proposals).toBe(1)
  expect(result.quotes).toEqual(['Объект «Алматы»:\nоплачено\t125\u00a0000 ₸.'])
  expect(result.diagnostics.source_whitespace_restored).toEqual([0])
  await page.reload()
  await expect(page.getByTestId('trace-result')).toContainText('Предложено фактов: 1')
})

for (const [scenario, code, reason] of [
  ['wrong_evidence', 'evidence_not_in_source', 'Цитату модели не удалось'],
  ['truncated', 'provider_output_truncated', 'Ответ модели не поместился'],
  ['missing_amount', 'invalid_schema', 'Поля фактов не соответствуют'],
  ['invalid_json', 'invalid_extraction_schema', 'Модель вернула некорректный JSON'],
  ['ambiguous_evidence', 'evidence_not_in_source', 'Цитату модели не удалось'],
  ['changed_evidence', 'evidence_not_in_source', 'Цитату модели не удалось'],
] as const) {
  test(`${scenario}: visible failure, no facts, one final queue attempt`, async ({ page }) => {
    test.setTimeout(120000)
    await adminLogin(page)
    const { id, seed } = await start(page, scenario)
    await expect.poll(() => state(id).event.state, { timeout: 60000 }).toBe('failed')
    const result = state(id)
    expect(result.error).toBe(code)
    expect(result.event.attempt_count).toBe(1)
    expect(result.proposals).toBe(0)
    expect(result.processed).toBe(false)
    expect(result.diagnostics.output_tokens).toBe(32)
    expect(result.diagnostics.response_characters).toBeGreaterThan(0)
    if (scenario === 'truncated') expect(result.diagnostics.finish_reason).toBe('length')
    if (scenario === 'missing_amount') expect(JSON.stringify(result.diagnostics.field_errors)).toContain('сумма')
    if (scenario === 'ambiguous_evidence') expect(result.diagnostics.evidence_match).toBe('ambiguous')
    if (scenario === 'changed_evidence') expect(result.diagnostics.evidence_match).toBe('not_found')
    await page.reload()
    const card = page.getByTestId('trace-result')
    await expect(card).toContainText(reason)
    await expect(card).toContainText('В этой попытке новые факты не сохранены')
    await expect(card).not.toContainText('Уверенность AI')
    await expect(card).not.toContainText('<em class=')
    await page.goto(`/admin/api/messageprocessingtrace/?q=${encodeURIComponent(seed.target.split(' ')[2]!)}`)
    const row = page.locator('tr').filter({ has: page.locator(`a[href^="/admin/api/messageprocessingtrace/${id}/change/"]`) })
    await expect(row.locator('.field-trace_result').getByText('Ошибка', { exact: true })).toHaveCount(1)
    await expect(row.locator('.field-trace_result')).toContainText(reason)
    await page.goto(`/admin/api/whatsapphistoryrun/${result.history_run}/change/`)
    await expect(page.getByTestId('history-analysis-errors')).toContainText(code)
    await expect(page.getByTestId('history-analysis-errors')).toContainText(reason)
  })
}
