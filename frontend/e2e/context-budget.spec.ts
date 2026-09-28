import { test, expect, type Page } from '@playwright/test'
import { adminLogin, isolatedCommand } from './auth-helper'

const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
const guard = "from django.conf import settings; assert settings.INTEGRATION_TEST_MODE; assert str(settings.DATABASES['default']['NAME']).endswith('_e2e'); "
interface Fixture { trace_id: number; raw_id: number; count: number; token: string; target: string; expected_ids: number[]; first_content: string; denied_username: string }
interface ContextMessage { raw_message_id: number; content: string; partial: boolean; original_characters?: number; included_character_range?: [number, number] }
interface Metadata {
  input_tokens_preflight: number; input_tokens_actual: number | null; completion_reserve_tokens: number; safety_tokens: number; window_tokens: number;
  available_messages_count: number; included_messages_count: number; omitted_messages_count: number; partial_messages_count: number;
  empty_messages_count: number; history_complete_in_request: boolean; request_state: string; time_basis: string; payload_sha256: string;
}
interface State { status: string; error: string; context: ContextMessage[]; metadata: Metadata; attempt: number; proposals: number; pending: number; total_attempts: number; legacy_context: unknown }
interface Payload { model: string; messages: { role: string; content: string }[]; max_tokens: number; provider: { allow_fallbacks: boolean }; plugins: { id: string; enabled: boolean }[] }
function fixture(scenario = 'full', count = 50): Fixture {
  return JSON.parse(isolatedCommand('seed_context_e2e', [scenario, '--count', String(count)])) as Fixture
}
function state(traceId: number): State {
  return JSON.parse(isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
import json
from api.models import MessageProcessingTrace
t=MessageProcessingTrace.objects.get(pk=${traceId})
meta={k:v for k,v in t.context_metadata.items() if k!='request_envelope'}
print(json.dumps({'status':t.status,'error':t.error_code,'context':t.earlier_messages_context,'metadata':meta,'attempt':t.attempt_no,'proposals':t.candidates.count(),'pending':sum(a.candidates.filter(status='pending').count() for a in t.raw_message.traces.all()),'total_attempts':t.raw_message.traces.count(),'legacy_context':t.raw_message.traces.order_by('attempt_no').first().earlier_messages_context}))
`])) as State
}
async function start(page: Page, seed: Fixture): Promise<number> {
  await page.goto(`/admin/api/messageprocessingtrace/${seed.trace_id}/change/`)
  await page.getByRole('button', { name: 'Повторить анализ с полной историей', exact: true }).click()
  await expect(page).not.toHaveURL(new RegExp(`/messageprocessingtrace/${seed.trace_id}/change/`))
  const match = page.url().match(/messageprocessingtrace\/(\d+)\/change/)
  if (!match) throw new Error('No new trace URL')
  return parseInt(match[1]!, 10)
}

for (const count of [50, 100, 1000]) {
  test(`full ${count}-message history reaches AI and UI without count or character caps`, async ({ page, request }, info) => {
    test.setTimeout(180000)
    const seed = fixture('full', count)
    await adminLogin(page)
    await page.goto(`/admin/api/messageprocessingtrace/${seed.trace_id}/change/`)
    const legacy = state(seed.trace_id).context
    const stage = page.getByTestId('message-context')
    await expect(stage).toContainText('Исторический контекст')
    await expect(stage).not.toContainText('72%')
    await expect(stage).not.toContainText('Qdrant')
    const id = await start(page, seed)
    await expect.poll(() => state(id).status, { timeout: 90000 }).toBe('success')
    const result = state(id)
    expect(result.context.map(item => item.raw_message_id)).toEqual(seed.expected_ids)
    expect(result.context[0]!.content).toBe(seed.first_content.replace(/ {2,}/g, ' ').trim())
    expect(seed.first_content.length).toBeGreaterThan(10000)
    expect(result.metadata.available_messages_count).toBe(count)
    expect(result.metadata.included_messages_count).toBe(count)
    expect(result.metadata.empty_messages_count).toBe(1)
    expect(result.metadata.history_complete_in_request).toBe(true)
    expect(result.metadata.input_tokens_actual).toBe(result.metadata.input_tokens_preflight)
    expect(result.metadata.input_tokens_preflight + result.metadata.completion_reserve_tokens + result.metadata.safety_tokens).toBeLessThanOrEqual(256000)
    expect(result.attempt).toBe(2)
    expect(result.legacy_context).toEqual(legacy)
    const captured = await (await request.get(`${provider}/test/ai-requests`)).json() as { payloads: Payload[] }
    const payload = captured.payloads.find(item => JSON.parse(item.messages[1]!.content).content === seed.target)
    expect(payload).toBeDefined()
    expect(JSON.parse(payload!.messages[1]!.content).context).toEqual(result.context)
    expect(payload!.max_tokens).toBe(8192)
    expect(payload!.provider.allow_fallbacks).toBe(false)
    expect(payload!.plugins).toContainEqual({ id: 'context-compression', enabled: false })
    await page.reload()
    await expect(stage).toContainText(`Включено ${count} из ${count}`)
    await expect(stage).not.toContainText('сондай')
    await page.getByRole('link', { name: `Все сообщения и исходники (${count})`, exact: true }).click()
    await expect(page.getByTestId('context-message')).toHaveCount(25)
    await page.goto(`/admin/api/messageprocessingtrace/${id}/context/?page=${Math.ceil(count / 25)}`)
    await expect(page.getByTestId('context-message').last()).toContainText('Последняя редакция')
    if (count === 1000) await page.screenshot({ path: info.outputPath('full-history.png'), fullPage: false })
  })
}

test('native 256k boundary preserves newest history and marks a partial Unicode message', async ({ page }, info) => {
  test.setTimeout(180000)
  const seed = fixture('overflow', 50)
  await adminLogin(page)
  const id = await start(page, seed)
  await expect.poll(() => state(id).status, { timeout: 120000 }).toBe('success')
  const result = state(id)
  const total = result.metadata.input_tokens_preflight + result.metadata.completion_reserve_tokens + result.metadata.safety_tokens
  expect(total).toBeLessThanOrEqual(256000)
  expect(total).toBeGreaterThan(255950)
  expect(result.metadata.partial_messages_count).toBe(1)
  expect(result.metadata.history_complete_in_request).toBe(false)
  expect(result.context[0]!.partial).toBe(true)
  expect(result.context[0]!.content).not.toContain('\uFFFD')
  expect(result.context[0]!.included_character_range![0]).toBeGreaterThan(0)
  expect(result.context.slice(1).map(item => item.raw_message_id)).toEqual(seed.expected_ids.slice(1))
  await page.reload()
  await expect(page.getByTestId('message-context')).toContainText('достигнуто окно модели')
  await expect(page.getByTestId('context-message').first()).toContainText('Показан фрагмент')
  await page.getByTestId('message-context').evaluate(el => el.scrollIntoView({ block: 'start' }))
  await page.screenshot({ path: info.outputPath('context-budget-boundary.png'), fullPage: false })
  await page.evaluate(() => document.documentElement.classList.add('dark'))
  await expect(page.getByTestId('message-context')).toHaveCSS('background-color', 'rgb(30, 41, 59)')
  await page.screenshot({ path: info.outputPath('context-budget-dark.png'), fullPage: false })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByTestId('message-context')).toBeVisible()
  await page.getByTestId('message-context').evaluate(el => el.scrollIntoView({ block: 'start' }))
  await page.screenshot({ path: info.outputPath('context-budget-mobile.png'), fullPage: false })
})

test('oversized target and provider overflow are explicit failures, without silent clipping', async ({ page }) => {
  test.setTimeout(180000)
  await adminLogin(page)
  for (const [scenario, error] of [['huge_target', 'context_fixed_input_too_large'], ['provider_overflow', 'provider_context_overflow']] as const) {
    const seed = fixture(scenario, 3)
    const id = await start(page, seed)
    await expect.poll(() => state(id).error, { timeout: 90000 }).toBe(error)
    expect(state(id).proposals).toBe(0)
    expect(state(id).total_attempts).toBe(2)
    if (scenario === 'provider_overflow') expect(state(id).pending).toBe(1)
    await page.reload()
    await expect(page.getByTestId('message-context').getByRole('alert')).toBeVisible()
    if (scenario === 'huge_target') {
      expect(state(id).metadata.request_state).toBe('not_sent')
      await expect(page.getByTestId('message-context')).toContainText('Запрос не отправлен')
    }
  }
})

test('receipt chronology, unavailable tokenizer and smaller provider window are visible', async ({ page, request }) => {
  test.setTimeout(180000)
  await adminLogin(page)
  const seed = fixture('unknown_time', 50)
  let id = await start(page, seed)
  await expect.poll(() => state(id).status, { timeout: 60000 }).toBe('success')
  expect(state(id).metadata.time_basis).toBe('received_at')
  expect(state(id).metadata.included_messages_count).toBe(50)
  await request.post(`${provider}/test/context-options`, { data: { context_length: 128000 } })
  try {
    id = await start(page, seed)
    await expect.poll(() => state(id).error, { timeout: 60000 }).toBe('context_model_window_unavailable')
  } finally {
    await request.post(`${provider}/test/context-options`, { data: {} })
  }
  isolatedCommand('shell', ['--verbosity', '0', '-c', guard + "from api.models import AISettings; AISettings.objects.update(tokenizer_revision='unsupported')"])
  try {
    id = await start(page, seed)
    await expect.poll(() => state(id).error, { timeout: 60000 }).toBe('context_tokenizer_unavailable')
  } finally {
    isolatedCommand('shell', ['--verbosity', '0', '-c', guard + "from api.models import AISettings; from api.context_tokens import MANIFEST; AISettings.objects.update(tokenizer_revision=MANIFEST['revision'])"])
  }
})

test('double submission and retry do not duplicate attempts or previously approved payment facts', async ({ page, request }) => {
  test.setTimeout(120000)
  const seed = fixture('payment', 3)
  await adminLogin(page)
  await page.goto(`/admin/api/messageprocessingtrace/${seed.trace_id}/change/`)
  const key = await page.locator('[name=request_key]').inputValue()
  const csrf = await page.locator('form[action$="/reanalyse/"] [name=csrfmiddlewaretoken]').inputValue()
  await request.post(`${provider}/test/context-options`, { data: { delay_seconds: 3 } })
  try {
    await page.getByRole('button', { name: 'Повторить анализ с полной историей', exact: true }).click()
    const match = page.url().match(/messageprocessingtrace\/(\d+)\/change/)
    if (!match) throw new Error('No trace URL')
    const id = parseInt(match[1]!, 10)
    await page.request.post(`/admin/api/messageprocessingtrace/${seed.trace_id}/reanalyse/`, { form: { request_key: key, csrfmiddlewaretoken: csrf } })
    await expect.poll(() => state(id).status, { timeout: 60000 }).toBe('success')
    expect(state(id).total_attempts).toBe(2)
    expect(state(id).proposals).toBe(0)
    // Re-deliver the same outbox event through its real queue, retaining attempt identity.
    isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `from api.models import OutboxEvent; from django.utils import timezone; OutboxEvent.objects.filter(payload__trace_id=${id}).update(state='pending', next_attempt_at=timezone.now())`])
    await expect.poll(() => isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `from api.models import OutboxEvent; print(OutboxEvent.objects.get(payload__trace_id=${id}).state)`]), { timeout: 30000 }).toBe('done')
    expect(state(id).total_attempts).toBe(2)
    expect(state(id).proposals).toBe(0)
    const captured = await (await request.get(`${provider}/test/ai-requests`)).json() as { payloads: Payload[] }
    expect(captured.payloads.filter(item => JSON.parse(item.messages[1]!.content).content === seed.target)).toHaveLength(1)
  } finally {
    await request.post(`${provider}/test/context-options`, { data: {} })
  }
})

test('context pages and reanalysis enforce source access and CSRF', async ({ page, request }) => {
  const seed = fixture('full', 3)
  expect((await request.get(`/admin/api/messageprocessingtrace/${seed.trace_id}/context/`, { maxRedirects: 0 })).status()).toBe(302)
  await adminLogin(page)
  expect((await page.request.post(`/admin/api/messageprocessingtrace/${seed.trace_id}/reanalyse/`, { form: { request_key: '00000000-0000-0000-0000-000000000000' } })).status()).toBe(403)
  expect(state(seed.trace_id).total_attempts).toBe(1)
  await page.context().clearCookies()
  await page.goto('/admin/login/?next=/admin/')
  await page.locator('[name=username]').fill(seed.denied_username)
  await page.locator('[name=password]').fill('test-only-admin-password')
  await page.locator('button[type=submit], input[type=submit]').click()
  await page.waitForURL('**/admin/')
  const csrf = (await page.context().cookies()).find(item => item.name === 'csrftoken')!.value
  expect((await page.request.get(`/admin/api/messageprocessingtrace/${seed.trace_id}/context/`)).status()).toBe(404)
  expect((await page.request.post(`/admin/api/messageprocessingtrace/${seed.trace_id}/reanalyse/`, { form: { request_key: '00000000-0000-0000-0000-000000000000', csrfmiddlewaretoken: csrf } })).status()).toBe(404)
  expect(state(seed.trace_id).total_attempts).toBe(1)
})
