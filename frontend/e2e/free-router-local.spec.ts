import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync, existsSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { createHmac } from 'node:crypto'

test.skip(process.env.MAZORY_E2E_ROUTER !== '1', 'Requires the isolated router stack')
test.setTimeout(240000)
const directory = process.env.MAZORY_E2E_DIR!
interface Proof {
  settings: { minute_request_limit: number; daily_request_limit: number; chat_model_name: string; autonomous_enabled: boolean }[]
  messages: { id: number; message_id: string; processed: boolean }[]
  events: { state: string; error_code: string; attempt_count: number; event_type: string }[]
  usage: { model_name: string; succeeded: boolean; error_code: string }[]
  probes: { status: string; worker_pid: number }[]
  traces: { id: number; raw_message_id: number; status: string }[]
  facts: { trace_id: number; fact_type: string; proposed_changes: Record<string, unknown> }[]
  operations: { id: number; status: string; error_code: string }[]
}
const proof = (): Proof => JSON.parse(readFileSync(join(directory, 'router-proof.json'), 'utf8'))
interface ProviderCall { at: number; provider: { require_parameters: boolean }; model: string; status: number; has_tools: boolean; response_format?: string }
const providerCalls = (): ProviderCall[] => readFileSync(join(directory, 'router-provider.jsonl'), 'utf8').trim().split('\n').map(line => JSON.parse(line))

test('router settings, cross-worker quota, persisted wait and local upstream 429 use real browser/API/DB/Q2', async ({ page, request }) => {
  await page.goto('/admin/login/')
  await page.locator('#id_username').fill('79990000001')
  await page.locator('#id_password').fill('local-e2e-only')
  await page.locator('button[type=submit], input[type=submit]').click()
  await page.goto('/admin/api/aisettings/1/change/')
  await page.locator('#id_chat_api_format').selectOption('openai_compatible')
  await page.getByRole('heading', { name: 'Расширенные настройки контекста (OpenAI-compatible)', exact: true }).click()
  for (const [field, value] of Object.entries({
    chat_model_name: 'openrouter/free', chat_provider_url: 'https://localhost', new_chat_api_key: 'fixture-key',
    tokenizer_id: 'openrouter/free', tokenizer_revision: 'router-utf8-bound-v1', context_window_tokens: '200000',
    analysis_input_token_limit: '65536', daily_request_limit: '1000', minute_request_limit: '3',
  })) await page.locator(`#id_${field}`).fill(value)
  await page.locator('button[name=_save], input[name=_save]').click()
  await expect(page.locator('.errorlist')).toHaveCount(0)
  await expect.poll(() => proof().settings[0]).toMatchObject({ minute_request_limit: 3, daily_request_limit: 1000, chat_model_name: 'openrouter/free', autonomous_enabled: false })
  await page.goto('/admin/api/aisettings/1/change/')
  await expect(page.locator('#id_minute_request_limit')).toHaveValue('3')
  writeFileSync(join(directory, 'router-burst'), 'go')
  await expect.poll(() => proof().probes.length).toBe(12)
  expect(proof().probes.filter(row => row.status === 'ok')).toHaveLength(3)
  expect(proof().probes.filter(row => row.status === 'ai_minute_limit_exhausted')).toHaveLength(9)
  expect(new Set(proof().probes.map(row => row.worker_pid)).size).toBeGreaterThan(1)
  expect(proof().usage).toHaveLength(3)
  expect(proof().usage.every(row => row.model_name === 'fixture/routed-model:free')).toBe(true)
  expect(providerCalls()).toHaveLength(3)
  const session: { access: string } = JSON.parse(readFileSync(join(directory, 'session.json'), 'utf8'))
  const headers = { Authorization: `Bearer ${session.access}` }
  const operationResponse = await request.post('/api/chat/query/', { headers, data: { prompt: 'Привет', idempotency_key: 'router-quota-operation' } })
  expect(operationResponse.status()).toBe(202)
  const operation: { operation_id: number; status_url: string } = await operationResponse.json()
  await expect.poll(() => proof().events.some(row => row.event_type === 'operation' && row.state === 'pending' && row.error_code === 'ai_minute_limit_exhausted')).toBe(true)
  expect(proof().operations.find(row => row.id === operation.operation_id)).toMatchObject({ status: 'queued', error_code: '' })
  const sentAt = Math.floor(Date.now() / 1000)
  const envelope = { event: 'message', session: 'default', payload: {
    id: 'router-after-burst', body: 'БЦ Восток: историческое КП подготовлено', from: 'fixture@g.us',
    participant: '79990000002@c.us', notifyName: 'Боб', timestamp: sentAt,
  } }
  const data = JSON.stringify(envelope)
  const response = await request.post('/api/whatsapp/webhook/', { data, headers: {
    'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex'),
  } })
  expect(response.ok()).toBe(true)
  await expect.poll(() => proof().events.some(row => row.event_type !== 'operation' && row.state === 'pending' && row.error_code === 'ai_minute_limit_exhausted')).toBe(true)
  expect(proof().events.filter(row => row.error_code === 'ai_minute_limit_exhausted').every(row => row.attempt_count === 0)).toBe(true)
  writeFileSync(join(directory, 'router-restart-required'), 'go')
  await expect.poll(() => existsSync(join(directory, 'router-restart-done')), { timeout: 90000 }).toBe(true)
  await expect.poll(() => proof().messages.find(row => row.message_id === 'router-after-burst')?.processed, { timeout: 120000 }).toBe(true)
  await expect.poll(async () => (await (await request.get(operation.status_url, { headers })).json()).status, { timeout: 90000 }).toBe('succeeded')
  const operationResult = await (await request.get(operation.status_url, { headers })).json()
  expect(operationResult.result.text).toContain('Привет!')
  await page.goto('/admin/api/aisettings/1/change/')
  await page.locator('#id_minute_request_limit').fill('0')
  await page.locator('button[name=_save], input[name=_save]').click()
  await expect.poll(() => proof().settings[0]?.minute_request_limit).toBe(0)
  const control = JSON.parse(readFileSync(join(directory, 'provider-state.json'), 'utf8')) as Record<string, unknown>
  writeFileSync(join(directory, 'router-control.tmp'), JSON.stringify({ ...control, router_429_once: true }))
  renameSync(join(directory, 'router-control.tmp'), join(directory, 'provider-state.json'))
  const retryData = JSON.stringify({ ...envelope, payload: { ...envelope.payload, id: 'router-after-429', timestamp: sentAt + 1 } })
  expect((await request.post('/api/whatsapp/webhook/', { data: retryData, headers: {
    'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(retryData).digest('hex'),
  } })).ok()).toBe(true)
  await expect.poll(() => proof().usage.some(row => row.error_code === 'provider_rate_limited')).toBe(true)
  await expect.poll(() => proof().messages.find(row => row.message_id === 'router-after-429')?.processed, { timeout: 150000 }).toBe(true)
  for (const message of proof().messages) {
    const traces = proof().traces.filter(row => row.raw_message_id === message.id && row.status === 'success')
    expect(traces.length).toBeGreaterThan(0)
    expect(proof().facts.some(fact => traces.some(trace => trace.id === fact.trace_id) && fact.fact_type === 'project')).toBe(true)
  }
  const calls = providerCalls()
  expect(calls.every(call => call.model === 'openrouter/free' && JSON.stringify(call.provider) === JSON.stringify({ require_parameters: true }))).toBe(true)
  expect(calls.some(call => call.response_format === 'json_object' || call.response_format === 'json_schema')).toBe(true)
  expect(calls.some(call => call.has_tools)).toBe(true)
  const firstAfterWindow = calls[3]!
  expect(firstAfterWindow.at - calls[0]!.at).toBeGreaterThanOrEqual(59)
  const limited = calls.findIndex(call => call.status === 429)
  expect(limited).toBeGreaterThanOrEqual(0)
  expect(calls[limited + 1]!.status).toBe(200)
  expect(calls[limited + 1]!.at - calls[limited]!.at).toBeGreaterThanOrEqual(2)
  await page.screenshot({ path: join(directory, 'playwright/router-admin.png'), fullPage: true })
})
