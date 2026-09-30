import { createHmac, randomUUID } from 'node:crypto'
import { test, expect, type APIRequestContext, type Page } from '@playwright/test'
import { adminLogin, isolatedCommand, isolatedCompose, login } from './auth-helper'

const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
const appBaseUrl = process.env.ANTHROPIC_E2E_BASE_URL || process.env.E2E_BASE_URL || 'http://localhost:5173'
const guard = "from django.conf import settings; assert settings.INTEGRATION_TEST_MODE; assert str(settings.DATABASES['default']['NAME']).endswith('_e2e'); "
const anthropicModel = 'claude-e2e-native'
const openAIModel = 'openai-e2e-admin'
const anthropicUrl = 'http://test-provider:9000/anthropic'
const openAIUrl = 'http://test-provider:9000/v1'
const anthropicKey = 'isolated-test-anthropic'
const invalidAnthropicKey = 'isolated-test-anthropic-invalid'
const openAIKey = 'isolated-test-provider'
const embeddingKey = 'isolated-test-embedding'

test.use({ baseURL: appBaseUrl })

interface TextBlock { type: string; text?: string }
interface NativeMessage { role: string; content: string | TextBlock[] }
interface NativePayload {
  model: string
  system?: string | TextBlock[]
  messages: NativeMessage[]
  max_tokens?: number
  [key: string]: unknown
}
interface NativeCapture {
  path: string
  payload: NativePayload
  auth_valid: boolean
  version_valid: boolean
  content_type_valid: boolean
  authorization_header_absent: boolean
  client_ip: string
}
interface OpenAICapture {
  path: string
  payload: Record<string, unknown>
  auth_valid: boolean
  content_type_valid: boolean
  x_api_key_absent: boolean
  client_ip: string
}
interface ProviderCaptures {
  openai_chats: OpenAICapture[]
  openai_embeddings: OpenAICapture[]
  anthropic: NativeCapture[]
  anthropic_counts: NativeCapture[]
}
interface CredentialState {
  chat: string
  embedding: string
  embedding_url: string
  has_chat: boolean
  has_embedding: boolean
}
interface UsageState {
  api_format: string
  operation: string
  model_name: string
  succeeded: boolean
  input_tokens: number | null
  output_tokens: number | null
  cost_usd: string | null
  error_code: string
}
interface OperationState {
  status: string
  error: string
  usages: UsageState[]
}
interface ExtractionState {
  trace_status: string
  error: string
  raw_state: string
  event_state: string
  event_id: number | null
  attempt_count: number
  retry_delay: number | null
  candidate_id: number | null
  candidate_count: number
  usages: UsageState[]
  metadata: {
    api_format?: string
    effective_provider_url?: string
    token_counter?: string
    token_count_requests?: number
    input_tokens_actual?: number | null
    payload_sha256?: string
  }
}

function inspect<T>(code: string): T {
  const output = isolatedCommand('shell', ['--verbosity', '0', '-c', guard + 'import json\n' + code])
  return JSON.parse(output.split('\n').at(-1)!) as T
}

function adminSection(page: Page, heading: string) {
  return page.getByRole('group').filter({
    has: page.getByRole('heading', { name: heading, exact: true }),
  }).first()
}

function operationState(prompt: string): OperationState {
  return inspect<OperationState>(`
import json
from api.models import AsyncOperation,OutboxEvent,ProviderUsage
op=AsyncOperation.objects.filter(request__prompt=${JSON.stringify(prompt)}).order_by('-id').first()
event=OutboxEvent.objects.filter(payload__operation_id=op.id).first() if op else None
usages=list(ProviderUsage.objects.filter(outbox_event=event).order_by('id').values('api_format','operation','model_name','succeeded','input_tokens','output_tokens','cost_usd','error_code')) if event else []
print(json.dumps({'status':op.status if op else '', 'error':op.error_code if op else '', 'usages':usages}, default=str))
`)
}

function extractionState(messageId: string): ExtractionState {
  return inspect<ExtractionState>(`
import json
from api.models import RawMessage,OutboxEvent,ProviderUsage
raw=RawMessage.objects.filter(message_id=${JSON.stringify(messageId)}).first()
trace=raw.traces.order_by('-id').first() if raw else None
event=OutboxEvent.objects.filter(event_type='extract_message',payload__raw_id=raw.id).first() if raw else None
candidate=trace.candidates.order_by('id').first() if trace else None
usages=list(ProviderUsage.objects.filter(outbox_event=event).order_by('id').values('api_format','operation','model_name','succeeded','input_tokens','output_tokens','cost_usd','error_code')) if event else []
generation=ProviderUsage.objects.filter(outbox_event=event,operation='chat').order_by('-id').first() if event else None
metadata=trace.context_metadata if trace else {}
print(json.dumps({
  'trace_status':trace.status if trace else '',
  'error':trace.error_code if trace else '',
  'raw_state':raw.processing_state if raw else '',
  'event_state':event.state if event else '',
  'event_id':event.id if event else None,
  'attempt_count':event.attempt_count if event else 0,
  'retry_delay':(event.next_attempt_at-generation.created_at).total_seconds() if event and event.next_attempt_at and generation else None,
  'candidate_id':candidate.id if candidate else None,
  'candidate_count':trace.candidates.count() if trace else 0,
  'usages':usages,
  'metadata':{key:metadata.get(key) for key in ('api_format','effective_provider_url','token_counter','token_count_requests','input_tokens_actual','payload_sha256')},
}, default=str))
`)
}

function credentialState(settingsId: number): CredentialState {
  return inspect<CredentialState>(`
from api.models import AISettings
s=AISettings.objects.get(pk=${settingsId})
print(json.dumps({
  'chat':s.chat_api_key_encrypted,
  'embedding':s.embedding_api_key_encrypted,
  'embedding_url':s.embedding_provider_url,
  'has_chat':s.has_chat_api_key,
  'has_embedding':s.has_embedding_api_key,
}))
`)
}

function releaseExtractionRetry(messageId: string) {
  isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
from django.utils import timezone
from api.models import RawMessage,OutboxEvent
raw=RawMessage.objects.get(message_id=${JSON.stringify(messageId)})
event=OutboxEvent.objects.get(event_type='extract_message',payload__raw_id=raw.id)
assert event.state=='pending' and event.error_code=='provider_overloaded'
event.next_attempt_at=timezone.now()
event.save(update_fields=['next_attempt_at'])
`])
}

async function providerCaptures(request: APIRequestContext): Promise<ProviderCaptures> {
  const response = await request.get(`${provider}/test/ai-requests`)
  expect(response.ok()).toBeTruthy()
  return await response.json() as ProviderCaptures
}

function contentText(content: string | TextBlock[]): string {
  if (typeof content === 'string') return content
  return content
    .filter(block => block.type === 'text' && typeof block.text === 'string')
    .map(block => block.text || '')
    .join('')
}

function userText(capture: NativeCapture): string {
  return capture.payload.messages
    .filter(message => message.role === 'user')
    .map(message => contentText(message.content))
    .join('\n')
}

function findCapture(captures: NativeCapture[], marker: string): NativeCapture {
  const capture = captures.findLast(item => userText(item).includes(marker))
  if (!capture) throw new Error(`Native Anthropic capture is missing for ${marker}`)
  return capture
}

function assertNativeRequest(capture: NativeCapture, countTokens = false, authValid = true) {
  expect(capture.path).toBe(countTokens
    ? '/anthropic/v1/messages/count_tokens'
    : '/anthropic/v1/messages')
  expect(capture.auth_valid).toBe(authValid)
  expect(capture.version_valid).toBe(true)
  expect(capture.content_type_valid).toBe(true)
  expect(capture.authorization_header_absent).toBe(true)
  expect(capture.payload.model).toBe(anthropicModel)
  expect(typeof capture.payload.system).toBe('string')
  expect(capture.payload.messages.length).toBeGreaterThan(0)
  expect(capture.payload.messages.every(message => ['user', 'assistant'].includes(message.role))).toBe(true)
  expect(capture.payload.messages.some(message => message.role === 'system')).toBe(false)
  const expectedKeys = countTokens
    ? ['messages', 'model', 'system']
    : ['max_tokens', 'messages', 'model', 'system']
  expect(Object.keys(capture.payload).sort()).toEqual(expectedKeys.sort())
  if (countTokens) {
    expect(capture.payload).not.toHaveProperty('max_tokens')
  } else {
    expect(typeof capture.payload.max_tokens).toBe('number')
    expect(capture.payload.max_tokens).toBeGreaterThan(0)
  }
  for (const field of ['provider', 'plugins', 'reasoning', 'response_format', 'temperature']) {
    expect(capture.payload).not.toHaveProperty(field)
  }
}

interface AISettingsUpdate {
  apiFormat?: string
  providerUrl?: string
  model?: string
  newChatKey?: string
  clearChatKey?: boolean
  embeddingProviderUrl?: string
  newEmbeddingKey?: string
  clearEmbeddingKey?: boolean
}

async function saveAISettings(page: Page, id: number, update: AISettingsUpdate) {
  await page.goto(`/admin/api/aisettings/${id}/change/`)
  if (update.apiFormat !== undefined) await page.locator('#id_chat_api_format').selectOption(update.apiFormat)
  if (update.providerUrl !== undefined) await page.locator('#id_chat_provider_url').fill(update.providerUrl)
  if (update.model !== undefined) await page.locator('#id_chat_model_name').fill(update.model)
  if (update.newChatKey !== undefined) await page.locator('#id_new_chat_api_key').fill(update.newChatKey)
  if (update.clearChatKey !== undefined) await page.locator('#id_clear_chat_api_key').setChecked(update.clearChatKey)
  if (update.embeddingProviderUrl !== undefined) await page.locator('#id_embedding_provider_url').fill(update.embeddingProviderUrl)
  if (update.newEmbeddingKey !== undefined) await page.locator('#id_new_embedding_api_key').fill(update.newEmbeddingKey)
  if (update.clearEmbeddingKey !== undefined) await page.locator('#id_clear_embedding_api_key').setChecked(update.clearEmbeddingKey)
  const response = page.waitForResponse(res =>
    res.request().method() === 'POST'
    && new URL(res.url()).pathname === `/admin/api/aisettings/${id}/change/`)
  await page.locator('[name=_continue]').first().click()
  expect((await response).status()).toBe(302)
  await page.waitForURL(url => url.pathname === `/admin/api/aisettings/${id}/change/`)
  await expect(page.locator('.errorlist')).toHaveCount(0)
  await expect(page.getByText(/был изменен успешно/)).toBeVisible()
}

async function expectRejectedAISettings(page: Page, id: number, update: AISettingsUpdate) {
  await page.goto(`/admin/api/aisettings/${id}/change/`)
  if (update.apiFormat !== undefined) await page.locator('#id_chat_api_format').selectOption(update.apiFormat)
  if (update.providerUrl !== undefined) await page.locator('#id_chat_provider_url').fill(update.providerUrl)
  if (update.model !== undefined) await page.locator('#id_chat_model_name').fill(update.model)
  if (update.newChatKey !== undefined) await page.locator('#id_new_chat_api_key').fill(update.newChatKey)
  if (update.clearChatKey !== undefined) await page.locator('#id_clear_chat_api_key').setChecked(update.clearChatKey)
  if (update.embeddingProviderUrl !== undefined) await page.locator('#id_embedding_provider_url').fill(update.embeddingProviderUrl)
  if (update.newEmbeddingKey !== undefined) await page.locator('#id_new_embedding_api_key').fill(update.newEmbeddingKey)
  if (update.clearEmbeddingKey !== undefined) await page.locator('#id_clear_embedding_api_key').setChecked(update.clearEmbeddingKey)
  const response = page.waitForResponse(res =>
    res.request().method() === 'POST'
    && new URL(res.url()).pathname === `/admin/api/aisettings/${id}/change/`)
  await page.locator('[name=_continue]').first().click()
  expect((await response).status()).toBe(200)
  await expect(page.locator('.errorlist')).not.toHaveCount(0)
}

async function submitChat(page: Page, prompt: string) {
  const input = page.getByPlaceholder('Спросите Mazory...')
  await input.fill(prompt)
  const submit = page.getByTitle('Отправить запрос')
  await expect(submit).toBeEnabled()
  const responsePromise = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/chat/query/'
    && response.request().method() === 'POST')
  await submit.click()
  expect((await responsePromise).status()).toBe(202)
}

async function sendWebhook(request: APIRequestContext, messageId: string, content: string) {
  const body = JSON.stringify({
    event: 'message',
    session: 'default',
    payload: {
      id: messageId,
      from: 'e2e@g.us',
      to: 'fixture@c.us',
      participant: '77000000002@c.us',
      notifyName: 'Anthropic E2E',
      timestamp: Math.floor(Date.now() / 1000) - 1,
      body: content,
      fromMe: false,
    },
  })
  return request.post('/api/whatsapp/webhook/', {
    data: body,
    headers: {
      'Content-Type': 'application/json',
      'X-Webhook-Hmac': createHmac('sha512', 'isolated-test-webhook').update(body).digest('hex'),
    },
  })
}

test('Admin-managed AI credentials → live provider rotation → UI chat and webhook extraction', async ({ page, request, browser }) => {
  test.setTimeout(360_000)
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()) })

  let settingsId = 0
  let originalFormat = ''
  let originalUrl = ''
  let originalModel = ''
  const qclusterContainer = isolatedCompose(['ps', '-q', 'qcluster']).trim()
  expect(qclusterContainer).not.toBe('')
  const legacyProviderEnv = JSON.parse(isolatedCompose([
    'exec', '-T', 'qcluster', 'python', '-c',
    "import json,os; print(json.dumps({name: bool(os.getenv(name)) for name in ('OPENROUTER_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL')}))",
  ])) as Record<string, boolean>
  expect(legacyProviderEnv).toEqual({
    OPENROUTER_API_KEY: false,
    ANTHROPIC_API_KEY: false,
    ANTHROPIC_BASE_URL: false,
  })
  const adminContext = await browser.newContext({ baseURL: appBaseUrl })
  const adminPage = await adminContext.newPage()
  try {
    await adminLogin(adminPage)
    settingsId = inspect<number>('from api.models import AISettings; print(json.dumps(AISettings.get_active().id))')
    await adminPage.goto(`/admin/api/aisettings/${settingsId}/change/`)
    originalFormat = await adminPage.locator('#id_chat_api_format').inputValue()
    originalUrl = await adminPage.locator('#id_chat_provider_url').inputValue()
    originalModel = await adminPage.locator('#id_chat_model_name').inputValue()

    await saveAISettings(adminPage, settingsId, {
      apiFormat: 'anthropic_messages',
      providerUrl: anthropicUrl,
      model: anthropicModel,
      newChatKey: anthropicKey,
      newEmbeddingKey: embeddingKey,
    })
    await expect(adminPage.locator('#id_chat_api_format')).toHaveValue('anthropic_messages')
    await expect(adminPage.locator('#id_chat_provider_url')).toHaveValue(anthropicUrl)
    await expect(adminPage.locator('#id_chat_model_name')).toHaveValue(anthropicModel)
    const chatKeyInput = adminPage.locator('#id_new_chat_api_key')
    const embeddingKeyInput = adminPage.locator('#id_new_embedding_api_key')
    await expect(chatKeyInput).toHaveAttribute('type', 'password')
    await expect(chatKeyInput).toHaveAttribute('autocomplete', 'new-password')
    await expect(chatKeyInput).toHaveValue('')
    await expect(embeddingKeyInput).toHaveAttribute('type', 'password')
    await expect(embeddingKeyInput).toHaveValue('')
    await expect(adminSection(adminPage, 'Chat / Reasoning').getByText('Настроен', { exact: true })).toBeVisible()
    await expect(adminSection(adminPage, 'Embeddings (OpenAI-compatible)').getByText('Настроен', { exact: true })).toBeVisible()
    await expect(adminPage.getByRole('heading', { name: 'Расширенные настройки контекста (OpenAI-compatible)' })).toBeVisible()
    await expect(adminPage.getByText(/Токенизатор — это словарь/)).toHaveCount(1)
    await expect(adminPage.locator('[name="chat_api_key_encrypted"], [name="embedding_api_key_encrypted"]')).toHaveCount(0)

    const configured = credentialState(settingsId)
    expect(configured).toMatchObject({ has_chat: true, has_embedding: true })
    expect(configured.chat).toMatch(/^fernet:v1:/)
    expect(configured.embedding).toMatch(/^fernet:v1:/)
    expect(configured.chat).not.toContain(anthropicKey)
    expect(configured.embedding).not.toContain(embeddingKey)
    expect(configured.embedding_url).toBe(openAIUrl)
    let adminHtml = await adminPage.content()
    expect(adminHtml).not.toContain(anthropicKey)
    expect(adminHtml).not.toContain(embeddingKey)
    expect(adminHtml).not.toContain(configured.chat)
    expect(adminHtml).not.toContain(configured.embedding)

    await expectRejectedAISettings(adminPage, settingsId, {
      embeddingProviderUrl: 'https://openrouter.ai/api/v1',
    })
    await expect(adminPage.getByText(/При смене hostname Embeddings/)).toBeVisible()
    expect(credentialState(settingsId)).toMatchObject({
      embedding: configured.embedding,
      embedding_url: openAIUrl,
      has_embedding: true,
    })

    await expectRejectedAISettings(adminPage, settingsId, {
      apiFormat: 'openai_compatible',
      providerUrl: openAIUrl,
    })
    expect(credentialState(settingsId).chat).toBe(configured.chat)

    const conflictingKey = `isolated-test-conflict-${randomUUID()}`
    await expectRejectedAISettings(adminPage, settingsId, {
      newChatKey: conflictingKey,
      clearChatKey: true,
    })
    await expect(adminPage.locator('#id_new_chat_api_key')).toHaveValue('')
    expect(await adminPage.content()).not.toContain(conflictingKey)
    expect(credentialState(settingsId).chat).toBe(configured.chat)

    const rejectedKey = `isolated-test-rejected-${randomUUID()}`
    await expectRejectedAISettings(adminPage, settingsId, {
      providerUrl: 'https://127.0.0.1/anthropic',
      newChatKey: rejectedKey,
    })
    await expect(adminPage.locator('#id_new_chat_api_key')).toHaveValue('')
    expect(await adminPage.content()).not.toContain(rejectedKey)
    expect(credentialState(settingsId).chat).toBe(configured.chat)

    await saveAISettings(adminPage, settingsId, {})
    expect(credentialState(settingsId).chat).toBe(configured.chat)

    await saveAISettings(adminPage, settingsId, { newChatKey: invalidAnthropicKey })
    const invalidConfigured = credentialState(settingsId)
    expect(invalidConfigured.chat).toMatch(/^fernet:v1:/)
    expect(invalidConfigured.chat).not.toBe(configured.chat)
    expect(invalidConfigured.chat).not.toContain(invalidAnthropicKey)

    await login(page)
    await page.getByRole('button', { name: 'KPI и чат', exact: true }).click()
    await expect(page.getByText('Полная история', { exact: true })).toBeVisible()

    const rejectedPrompt = `ANTHROPIC_ADMIN_BAD_KEY ${randomUUID()}`
    await submitChat(page, rejectedPrompt)
    await expect.poll(() => operationState(rejectedPrompt).error, { timeout: 45_000 }).toBe('provider_authentication_failed')
    let captures = await providerCaptures(request)
    const rejectedCapture = findCapture(captures.anthropic, rejectedPrompt)
    assertNativeRequest(rejectedCapture, false, false)
    expect(JSON.stringify(rejectedCapture)).not.toContain(invalidAnthropicKey)

    await saveAISettings(adminPage, settingsId, { newChatKey: anthropicKey })
    expect(isolatedCompose(['ps', '-q', 'qcluster']).trim()).toBe(qclusterContainer)

    const chatPrompt = `ANTHROPIC_E2E_DELAY ${randomUUID()}`
    await submitChat(page, chatPrompt)
    await expect(page.getByRole('status').filter({ hasText: 'Обрабатываю запрос' })).toBeVisible()
    await expect(page.getByTestId('chat-response')).toHaveText(
      'Ответ через Anthropic Messages по разрешённым источникам.',
      { timeout: 45_000 },
    )

    const chatState = operationState(chatPrompt)
    expect(chatState.status).toBe('succeeded')
    expect(chatState.error).toBe('')
    const chatUsage = chatState.usages.find(usage => usage.operation === 'chat')
    const embeddingUsage = chatState.usages.find(usage => usage.operation === 'embedding')
    expect(chatUsage).toMatchObject({
      api_format: 'anthropic_messages',
      model_name: anthropicModel,
      succeeded: true,
      output_tokens: 32,
      cost_usd: null,
      error_code: '',
    })
    expect(typeof chatUsage?.input_tokens).toBe('number')
    expect(chatUsage!.input_tokens!).toBeGreaterThan(0)
    expect(embeddingUsage?.api_format).toBe('openai_compatible')

    captures = await providerCaptures(request)
    const chatCapture = findCapture(captures.anthropic, chatPrompt)
    assertNativeRequest(chatCapture)
    expect(contentText(chatCapture.payload.system!)).toContain('Ты аналитик Mazory')
    expect(JSON.stringify(chatCapture)).not.toContain('isolated-test-anthropic')
    const embeddingCapture = captures.openai_embeddings.findLast(item =>
      JSON.stringify(item.payload).includes(chatPrompt))
    expect(embeddingCapture).toMatchObject({
      path: '/v1/embeddings',
      auth_valid: true,
      content_type_valid: true,
      x_api_key_absent: true,
    })
    const qclusterIps = isolatedCompose(['exec', '-T', 'qcluster', 'hostname', '-i']).trim().split(/\s+/)
    const backendIps = isolatedCompose(['exec', '-T', 'backend', 'hostname', '-i']).trim().split(/\s+/)
    expect(qclusterIps).toContain(chatCapture.client_ip)
    expect(qclusterIps).toContain(embeddingCapture!.client_ip)
    expect(backendIps).not.toContain(chatCapture.client_ip)

    const errorScenarios = [
      ['ANTHROPIC_E2E_401', 'provider_authentication_failed'],
      ['ANTHROPIC_E2E_429', 'provider_rate_limited'],
      ['ANTHROPIC_E2E_529', 'provider_overloaded'],
      ['ANTHROPIC_E2E_MALFORMED', 'provider_invalid_response'],
      ['ANTHROPIC_E2E_MAX_TOKENS', 'provider_output_truncated'],
    ] as const
    for (const [marker, errorCode] of errorScenarios) {
      const prompt = `${marker} ${randomUUID()}`
      await submitChat(page, prompt)
      await expect.poll(() => operationState(prompt).error, { timeout: 45_000 }).toBe(errorCode)
      await expect(page.getByRole('alert')).toContainText('Не удалось обработать запрос')
      captures = await providerCaptures(request)
      assertNativeRequest(findCapture(captures.anthropic, prompt))
    }

    const extractionMarker = `E2E extraction:anthropic ${randomUUID()}`
    const extractionMessageId = `anthropic-${randomUUID()}`
    expect((await sendWebhook(request, extractionMessageId, extractionMarker)).status()).toBe(202)
    await expect.poll(
      () => extractionState(extractionMessageId).trace_status,
      { timeout: 60_000 },
    ).toBe('success')
    const extraction = extractionState(extractionMessageId)
    expect(extraction).toMatchObject({
      error: '',
      raw_state: 'needs_review',
      event_state: 'done',
      candidate_count: 1,
      metadata: {
        api_format: 'anthropic_messages',
        effective_provider_url: 'http://test-provider:9000/anthropic',
        token_counter: 'anthropic_count_tokens',
      },
    })
    expect(extraction.candidate_id).not.toBeNull()
    expect(extraction.metadata.token_count_requests).toBeGreaterThan(0)
    expect(extraction.metadata.input_tokens_actual).toBeGreaterThan(0)
    const extractionUsage = extraction.usages.find(usage => usage.operation === 'chat')
    const tokenUsages = extraction.usages.filter(usage => usage.operation === 'token_count')
    expect(extractionUsage).toMatchObject({
      api_format: 'anthropic_messages',
      model_name: anthropicModel,
      succeeded: true,
      output_tokens: 32,
      cost_usd: null,
      error_code: '',
    })
    expect(typeof extractionUsage?.input_tokens).toBe('number')
    expect(extractionUsage!.input_tokens!).toBeGreaterThan(0)
    expect(tokenUsages.length).toBeGreaterThan(0)
    for (const usage of tokenUsages) {
      expect(usage).toMatchObject({
        api_format: 'anthropic_messages',
        operation: 'token_count',
        model_name: anthropicModel,
        succeeded: true,
        output_tokens: null,
        cost_usd: null,
        error_code: '',
      })
      expect(typeof usage.input_tokens).toBe('number')
      expect(usage.input_tokens!).toBeGreaterThan(0)
    }

    captures = await providerCaptures(request)
    const extractionCapture = findCapture(captures.anthropic, extractionMarker)
    const countCapture = findCapture(captures.anthropic_counts, extractionMarker)
    assertNativeRequest(extractionCapture)
    assertNativeRequest(countCapture, true)
    expect(contentText(extractionCapture.payload.system!)).toContain('ТОЛЬКО из одного целевого сообщения')
    expect(contentText(countCapture.payload.system!)).toContain('ТОЛЬКО из одного целевого сообщения')
    expect(JSON.stringify({ extractionCapture, countCapture })).not.toContain('isolated-test-anthropic')

    await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).click()
    await page.getByRole('button', { name: 'Проверка фактов', exact: true }).click()
    const candidate = page.getByTestId(`candidate-${extraction.candidate_id}`)
    await expect(candidate).toBeVisible()
    await expect(candidate).toContainText('Anthropic E2E project')
    await expect(candidate).toContainText(extractionMarker)
    await candidate.getByLabel('Основание / причина').fill('Anthropic E2E проверка завершена')
    await candidate.getByRole('button', { name: 'Отклонить', exact: true }).click()
    await expect(candidate).toHaveCount(0)

    const retryMarker = `ANTHROPIC_E2E_TRANSIENT_529 ${randomUUID()}`
    const retryMessageId = `anthropic-retry-${randomUUID()}`
    expect((await sendWebhook(request, retryMessageId, retryMarker)).status()).toBe(202)
    await expect.poll(() => {
      const state = extractionState(retryMessageId)
      return `${state.error}:${state.event_state}:${state.attempt_count}`
    }, { timeout: 60_000 }).toBe('provider_overloaded:pending:1')
    const retryWaiting = extractionState(retryMessageId)
    expect(retryWaiting).toMatchObject({
      trace_status: 'warning',
      raw_state: 'received',
      candidate_count: 0,
    })
    expect(retryWaiting.retry_delay).toBeGreaterThanOrEqual(60)
    expect(retryWaiting.retry_delay).toBeLessThan(80)
    expect(retryWaiting.metadata.payload_sha256).toMatch(/^[a-f0-9]{64}$/)
    captures = await providerCaptures(request)
    const firstRetryCaptures = captures.anthropic.filter(item => userText(item).includes(retryMarker))
    expect(firstRetryCaptures).toHaveLength(1)
    assertNativeRequest(firstRetryCaptures[0]!)

    releaseExtractionRetry(retryMessageId)
    await expect.poll(
      () => extractionState(retryMessageId).event_state,
      { timeout: 60_000 },
    ).toBe('done')
    const retryDone = extractionState(retryMessageId)
    expect(retryDone).toMatchObject({
      trace_status: 'success',
      error: '',
      raw_state: 'needs_review',
      attempt_count: 2,
      candidate_count: 1,
    })
    expect(retryDone.metadata.payload_sha256).toBe(retryWaiting.metadata.payload_sha256)
    const retryUsages = retryDone.usages.filter(usage => usage.operation === 'chat')
    expect(retryUsages).toHaveLength(2)
    expect(retryUsages[0]).toMatchObject({
      api_format: 'anthropic_messages',
      model_name: anthropicModel,
      succeeded: false,
      error_code: 'provider_overloaded',
    })
    expect(retryUsages[1]).toMatchObject({
      api_format: 'anthropic_messages',
      model_name: anthropicModel,
      succeeded: true,
      error_code: '',
    })
    captures = await providerCaptures(request)
    const completedRetryCaptures = captures.anthropic.filter(item => userText(item).includes(retryMarker))
    expect(completedRetryCaptures).toHaveLength(2)
    assertNativeRequest(completedRetryCaptures[1]!)
    expect(completedRetryCaptures[1]!.payload).toEqual(completedRetryCaptures[0]!.payload)
    await page.getByRole('button', { name: 'Проверка фактов', exact: true }).click()
    const retryCandidate = page.getByTestId(`candidate-${retryDone.candidate_id}`)
    await expect(retryCandidate).toContainText(retryMarker)
    await retryCandidate.getByLabel('Основание / причина').fill('Anthropic E2E retry проверен')
    await retryCandidate.getByRole('button', { name: 'Отклонить', exact: true }).click()
    await expect(retryCandidate).toHaveCount(0)

    const countFailureMarker = `ANTHROPIC_E2E_COUNT_MALFORMED ${randomUUID()}`
    const countFailureMessageId = `anthropic-count-${randomUUID()}`
    expect((await sendWebhook(request, countFailureMessageId, countFailureMarker)).status()).toBe(202)
    await expect.poll(
      () => extractionState(countFailureMessageId).error,
      { timeout: 60_000 },
    ).toBe('context_token_count_unavailable')
    const countFailure = extractionState(countFailureMessageId)
    expect(countFailure).toMatchObject({
      trace_status: 'error',
      raw_state: 'failed',
      event_state: 'failed',
      candidate_count: 0,
    })
    captures = await providerCaptures(request)
    assertNativeRequest(findCapture(captures.anthropic_counts, countFailureMarker), true)
    expect(captures.anthropic.some(item => userText(item).includes(countFailureMarker))).toBe(false)
    expect(JSON.stringify(captures)).not.toContain('isolated-test-anthropic')

    await saveAISettings(adminPage, settingsId, {
      apiFormat: 'openai_compatible',
      providerUrl: openAIUrl,
      model: openAIModel,
      newChatKey: openAIKey,
    })
    await expect(adminPage.locator('#id_chat_api_format')).toHaveValue('openai_compatible')
    await expect(adminPage.locator('#id_chat_provider_url')).toHaveValue(openAIUrl)
    await expect(adminPage.locator('#id_chat_model_name')).toHaveValue(openAIModel)
    await expect(adminSection(adminPage, 'Chat / Reasoning').getByText('Настроен', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: 'KPI и чат', exact: true }).click()
    await expect(page.getByText('Полная история', { exact: true })).toBeVisible()
    const openAIPrompt = `OPENAI_ADMIN_SWITCH ${randomUUID()}`
    await submitChat(page, openAIPrompt)
    await expect(page.getByTestId('chat-response')).toHaveText(
      'Ответ по разрешённым источникам тестового провайдера.',
      { timeout: 45_000 },
    )
    await expect.poll(() => operationState(openAIPrompt).status, { timeout: 45_000 }).toBe('succeeded')
    const openAIState = operationState(openAIPrompt)
    expect(openAIState.usages.find(usage => usage.operation === 'chat')).toMatchObject({
      api_format: 'openai_compatible',
      model_name: openAIModel,
      succeeded: true,
      error_code: '',
    })
    captures = await providerCaptures(request)
    const openAICapture = captures.openai_chats.findLast(item =>
      JSON.stringify(item.payload).includes(openAIPrompt))
    expect(openAICapture).toMatchObject({
      path: '/v1/chat/completions',
      auth_valid: true,
      content_type_valid: true,
      x_api_key_absent: true,
    })
    expect(openAICapture?.payload.model).toBe(openAIModel)
    expect(qclusterIps).toContain(openAICapture!.client_ip)
    expect(JSON.stringify(openAICapture)).not.toContain(openAIKey)
    expect(captures.anthropic.some(item => userText(item).includes(openAIPrompt))).toBe(false)

    const openAICountBeforeCorruption = captures.openai_chats.length
    isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
from api.models import AISettings
AISettings.objects.filter(pk=${settingsId}).update(chat_api_key_encrypted='fernet:v1:synthetic-corrupted-ciphertext')
`])
    const corruptedPrompt = `OPENAI_ADMIN_CORRUPTED ${randomUUID()}`
    await submitChat(page, corruptedPrompt)
    await expect.poll(() => operationState(corruptedPrompt).error, { timeout: 45_000 }).toBe('credential_decryption_failed')
    captures = await providerCaptures(request)
    expect(captures.openai_chats).toHaveLength(openAICountBeforeCorruption)

    await saveAISettings(adminPage, settingsId, { newChatKey: openAIKey })
    expect(isolatedCompose(['ps', '-q', 'qcluster']).trim()).toBe(qclusterContainer)
    const recoveredPrompt = `OPENAI_ADMIN_RECOVERED ${randomUUID()}`
    await submitChat(page, recoveredPrompt)
    await expect.poll(() => operationState(recoveredPrompt).status, { timeout: 45_000 }).toBe('succeeded')
    captures = await providerCaptures(request)
    const recoveredCapture = captures.openai_chats.findLast(item =>
      JSON.stringify(item.payload).includes(recoveredPrompt))
    expect(recoveredCapture).toMatchObject({ auth_valid: true, client_ip: openAICapture!.client_ip })

    const openAIChatCount = captures.openai_chats.length
    const clearedCiphertext = credentialState(settingsId).chat
    await saveAISettings(adminPage, settingsId, { clearChatKey: true })
    await expect(adminSection(adminPage, 'Chat / Reasoning').getByText('Не настроен', { exact: true })).toBeVisible()
    const cleared = credentialState(settingsId)
    expect(cleared).toMatchObject({ chat: '', has_chat: false, has_embedding: true })
    expect(clearedCiphertext).toMatch(/^fernet:v1:/)

    const clearedPrompt = `OPENAI_ADMIN_CLEARED ${randomUUID()}`
    await submitChat(page, clearedPrompt)
    await expect.poll(() => operationState(clearedPrompt).error, { timeout: 45_000 }).toBe('ai_not_configured')
    captures = await providerCaptures(request)
    expect(captures.openai_chats).toHaveLength(openAIChatCount)
    expect(JSON.stringify(captures)).not.toContain(clearedCiphertext)

    await adminPage.goto(`/admin/api/aisettings/${settingsId}/history/`)
    adminHtml = await adminPage.content()
    for (const secret of [anthropicKey, invalidAnthropicKey, openAIKey, embeddingKey, configured.chat, configured.embedding, clearedCiphertext]) {
      expect(adminHtml).not.toContain(secret)
    }
    const audit = inspect<unknown[]>(`
from api.models import AuditEvent
print(json.dumps(list(AuditEvent.objects.filter(target_type='AISettings',target_id=${settingsId}).values_list('before_after',flat=True))))
`)
    const auditJson = JSON.stringify(audit)
    expect(auditJson).toContain('new_chat_api_key')
    expect(auditJson).toContain('clear_chat_api_key')
    for (const secret of [anthropicKey, invalidAnthropicKey, openAIKey, embeddingKey, configured.chat, configured.embedding, clearedCiphertext]) {
      expect(auditJson).not.toContain(secret)
    }
    const logs = isolatedCompose([
      'logs', '--no-color',
      'backend', 'qcluster', 'outbox', 'history', 'delivery', 'crm', 'test-provider',
    ])
    for (const secret of [anthropicKey, invalidAnthropicKey, openAIKey, embeddingKey, configured.chat, configured.embedding, clearedCiphertext]) {
      expect(logs).not.toContain(secret)
    }

    expect(pageErrors).toEqual([])
    expect(consoleErrors).toEqual([])
  } finally {
    try {
      if (settingsId && originalFormat && originalUrl && originalModel) {
        await adminPage.goto('/admin/')
        if (new URL(adminPage.url()).pathname === '/admin/login/') await adminLogin(adminPage)
        await saveAISettings(adminPage, settingsId, {
          apiFormat: originalFormat,
          providerUrl: originalUrl,
          model: originalModel,
          newChatKey: originalFormat === 'anthropic_messages' ? anthropicKey : openAIKey,
          newEmbeddingKey: embeddingKey,
        })
        await expect(adminPage.locator('#id_chat_api_format')).toHaveValue(originalFormat)
        await expect(adminPage.locator('#id_chat_provider_url')).toHaveValue(originalUrl)
        await expect(adminPage.locator('#id_chat_model_name')).toHaveValue(originalModel)
      }
    } finally {
      await adminContext.close()
    }
  }
})
