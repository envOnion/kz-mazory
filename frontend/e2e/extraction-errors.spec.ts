import { test, expect, type Page } from '@playwright/test'
import { adminLogin, isolatedCommand } from './auth-helper'

const guard = "from django.conf import settings; assert settings.INTEGRATION_TEST_MODE; assert str(settings.DATABASES['default']['NAME']).endswith('_e2e'); "
const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
interface Seed { trace_id: number; raw_id: number; target: string }
interface State {
  status: string; error: string; proposals: number; processed: boolean;
  quotes: string[];
  event: { id: number; state: string; attempt_count: number; lease_until: string | null; next_attempt_at: string }; history_run: number;
  retry_delay: number; provider_requests: number; payload_hash: string;
  diagnostics: { finish_reason?: string; output_tokens?: number; response_characters?: number; field_errors?: unknown; source_whitespace_restored?: number[]; evidence_match?: string }
}
function state(id: number): State {
  return JSON.parse(isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
import json
from api.models import MessageProcessingTrace,OutboxEvent,ProviderUsage
t=MessageProcessingTrace.objects.get(pk=${id})
e=OutboxEvent.objects.get(payload__trace_id=t.id)
u=ProviderUsage.objects.filter(outbox_event=e,operation='chat').order_by('-id').first()
print(json.dumps({'status':t.status,'error':t.error_code,'proposals':t.candidates.count(),'quotes':list(t.candidates.values_list('evidence__quote',flat=True)),'processed':t.raw_message.processed,'event':{'id':e.id,'state':e.state,'attempt_count':e.attempt_count,'next_attempt_at':e.next_attempt_at,'lease_until':e.lease_until},'diagnostics':t.context_metadata.get('response_diagnostics',{}),'history_run':t.raw_message.history_import_runs.first().id,'retry_delay':(e.next_attempt_at-u.created_at).total_seconds() if u else 0,'provider_requests':ProviderUsage.objects.filter(outbox_event=e,operation='chat').count(),'payload_hash':t.context_metadata.get('payload_sha256','')},default=str))
`])) as State
}
function releaseRetry(id: number) {
  isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
from django.utils import timezone
from api.models import OutboxEvent
e=OutboxEvent.objects.get(payload__trace_id=${id}); assert e.state=='pending'
e.next_attempt_at=timezone.now(); e.save(update_fields=['next_attempt_at'])
`])
}
async function deliverDuplicate(id: number) {
  const task = isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
from django_q.tasks import async_task
from api.models import OutboxEvent
print(async_task('api.tasks.run_outbox',OutboxEvent.objects.get(payload__trace_id=${id}).id,cluster='ai'))
`]).split('\n').at(-1)!
  await expect.poll(() => isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
from django_q.models import Task
print(Task.objects.filter(pk='${task}',success=True).exists())
`]), { timeout: 30000 }).toBe('True')
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

for (const [scenario, code, minimumDelay] of [
  ['rate_limited', 'provider_rate_limited', 120],
  ['retry_date', 'provider_overloaded', 89],
  ['disconnected', 'provider_connection_failed', 60],
  ['http_timeout', 'provider_timeout', 60],
  ['bad_json_once', 'invalid_extraction_schema', 60],
  ['in_flight_budget', 'provider_in_flight_budget', 120],
] as const) {
  test(`${scenario}: delayed retry is visible and uses the identical full request`, async ({ page, request }) => {
    test.setTimeout(150000)
    await adminLogin(page)
    const { id, seed } = await start(page, scenario)
    await expect.poll(() => state(id).event.state, { timeout: 45000 }).toBe('pending')
    const waiting = state(id)
    expect(waiting.error).toBe(code)
    expect(waiting.status).toBe('warning')
    expect(waiting.event.attempt_count).toBe(1)
    expect(waiting.event.lease_until).toBeNull()
    expect(waiting.retry_delay).toBeGreaterThanOrEqual(minimumDelay)
    expect(waiting.retry_delay).toBeLessThan(minimumDelay + 20)
    expect(waiting.proposals).toBe(0)
    await page.reload()
    await expect(page.getByTestId('trace-retry')).toContainText('Повтор запланирован')
    await expect(page.getByTestId('trace-retry')).toContainText('1 из 5')
    await expect(page.getByTestId('trace-retry')).toContainText('не ранее')
    if (scenario === 'rate_limited') {
      await deliverDuplicate(id)
      expect(state(id).provider_requests).toBe(1)
      expect(state(id).event.attempt_count).toBe(1)
      expect(state(id).event.next_attempt_at).toBe(waiting.event.next_attempt_at)
      await page.goto(`/admin/api/messageprocessingtrace/?q=${encodeURIComponent(seed.target.split(' ')[2]!)}`)
      const row = page.locator('tr').filter({ has: page.locator(`a[href^="/admin/api/messageprocessingtrace/${id}/change/"]`) })
      await expect(row.locator('.field-trace_result')).toContainText('Ожидает повтора')
    }
    releaseRetry(id)
    await expect.poll(() => state(id).event.state, { timeout: 45000 }).toBe('done')
    const done = state(id)
    expect(done.status).toBe('success')
    expect(done.event.attempt_count).toBe(2)
    expect(done.proposals).toBe(1)
    expect(done.payload_hash).toBe(waiting.payload_hash)
    const captured = await (await request.get(`${provider}/test/ai-requests`)).json() as { payloads: { messages: { content: string }[] }[] }
    const sent = captured.payloads.filter(p => JSON.parse(p.messages[1]!.content).content === seed.target)
    expect(sent).toHaveLength(2)
    expect(sent[1]).toEqual(sent[0])
    await page.goto(`/admin/api/messageprocessingtrace/${id}/change/`)
    await expect(page.getByTestId('trace-retry')).toHaveCount(0)
    await expect(page.getByTestId('trace-result')).toContainText('Предложено фактов: 1')
  })
}

for (const [scenario, code] of [
  ['always_rate_limited', 'provider_rate_limited'],
  ['invalid_json', 'invalid_extraction_schema'],
  ['missing_amount', 'invalid_schema'],
] as const) {
  test(`${scenario}: exactly five attempts, then final error without a sixth provider call`, async ({ page }) => {
    test.setTimeout(240000)
    await adminLogin(page)
    const { id } = await start(page, scenario)
    for (let attempt = 1; attempt <= 5; attempt++) {
      await expect.poll(() => {
        const s = state(id)
        return `${s.event.attempt_count}:${s.event.state}`
      }, { timeout: 45000 }).toBe(`${attempt}:${attempt === 5 ? 'failed' : 'pending'}`)
      const s = state(id)
      expect(s.error).toBe(code)
      expect(s.proposals).toBe(0)
      expect(s.provider_requests).toBe(attempt)
      if (attempt < 5) {
        expect(s.retry_delay).toBeGreaterThanOrEqual(60 * 2 ** (attempt - 1))
        releaseRetry(id)
      }
    }
    await deliverDuplicate(id)
    expect(state(id).provider_requests).toBe(5)
    expect(state(id).event.attempt_count).toBe(5)
    expect(state(id).status).toBe('error')
    await page.reload()
    await expect(page.getByTestId('trace-result')).toContainText('Исчерпаны все 5 попыток')
    await expect(page.getByTestId('trace-retry')).toHaveCount(0)
  })
}

for (const [scenario, code, reason] of [
  ['wrong_evidence', 'evidence_not_in_source', 'Цитату модели не удалось'],
  ['truncated', 'provider_output_truncated', 'Ответ модели не поместился'],
  ['ambiguous_evidence', 'evidence_not_in_source', 'Цитату модели не удалось'],
  ['changed_evidence', 'evidence_not_in_source', 'Цитату модели не удалось'],
  ['unauthorized', 'provider_authentication_failed', 'Провайдер AI отклонил ключ'],
  ['no_credits', 'provider_insufficient_credits', 'Недостаточно средств'],
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
    if (!['unauthorized', 'no_credits'].includes(scenario)) {
      expect(result.diagnostics.output_tokens).toBe(32)
      expect(result.diagnostics.response_characters).toBeGreaterThan(0)
    }
    if (scenario === 'truncated') expect(result.diagnostics.finish_reason).toBe('length')
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
