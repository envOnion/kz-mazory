import { test, expect } from '@playwright/test'
import { createHmac, randomUUID } from 'node:crypto'
import { adminLogin, isolatedCommand, login } from './auth-helper'

const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
const guard = "from django.conf import settings; assert settings.INTEGRATION_TEST_MODE; assert str(settings.DATABASES['default']['NAME']).endswith('_e2e'); "
function readState(messageId: string) {
  return JSON.parse(isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
import json, uuid
from api.models import RawMessage, OutboxEvent
from api.qdrant_service import qdrant_service
raw = RawMessage.objects.get(message_id='${messageId}')
trace = raw.traces.order_by('-id').first()
event = OutboxEvent.objects.filter(deduplication_key=f'index:{raw.id}').first()
points = qdrant_service.client.retrieve(qdrant_service.collection_name, [str(uuid.uuid5(uuid.NAMESPACE_URL, f'mazory:{raw.id}'))], with_payload=True, with_vectors=False) if qdrant_service.client.collection_exists(qdrant_service.collection_name) else []
print(json.dumps({'raw_id': raw.id, 'raw_content': raw.content, 'trace_id': trace.id if trace else None, 'context': trace.earlier_messages_context if trace else [], 'index_state': event.state if event else None, 'payload': points[0].payload if points else None}))
`])) as { raw_id: number; raw_content: string; trace_id: number; context: { raw_message_id: number; content: string; sender_name: string }[]; index_state: string; payload: { content: string; sender_name: string } | null }
}

test('HTML webhook → worker → clean embedding/Qdrant and readable historical context', async ({ page, request }, testInfo) => {
  test.setTimeout(120000)
  const token = `E2E-clean-${randomUUID()}`
  const original = `<p>${token} Құжат&nbsp;№7&#x20;— 125 000 ₸</p><p>2 &lt; 3; https://example.com/a?x=1&amp;y=2&not=3<br>Поставка</p><ul><li>Насос</li><li>Клапан</li></ul><script>window.__xss=1</script><style>.hidden{display:none}</style><!--noise-->`
  const clean = `${token} Құжат №7 — 125 000 ₸\n\n2 < 3; https://example.com/a?x=1&y=2&not=3\nПоставка\n\nНасос\n\nКлапан`
  const encoded = `&amp;lt;div&amp;gt;${token}-encoded&amp;lt;br&amp;gt;Срок&amp;nbsp;10 дней&amp;lt;/div&amp;gt;`
  const encodedClean = `${token}-encoded\nСрок 10 дней`
  const empty = '<script>window.__xss=1</script><style>body{display:none}</style><img src=x onerror="window.__xss=1"><![broken]>'
  const ids = [randomUUID(), randomUUID(), randomUUID(), randomUUID()]
  const bodies = [original, encoded, empty, `${token}-followup`]
  const states: ReturnType<typeof readState>[] = []
  for (let i = 0; i < bodies.length; i++) {
    const body = JSON.stringify({ event: 'message', session: 'default', payload: { id: ids[i], from: 'e2e@g.us', participant: '77000000002@c.us', notifyName: '<b>Тест</b><script>window.__xss=1</script>', timestamp: Math.floor(Date.now() / 1000) - 100 + i, body: bodies[i] } })
    const signature = createHmac('sha512', 'isolated-test-webhook').update(body).digest('hex')
    expect((await request.post('/api/messages/ingest/', { data: body, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': signature } })).status()).toBe(202)
    await expect.poll(() => readState(ids[i]!).index_state, { timeout: 30000 }).toBe('done')
    states.push(readState(ids[i]!))
  }
  expect(states[0]!.raw_content).toBe(original)
  expect(states[0]!.payload?.content).toBe(clean)
  expect(states[0]!.payload?.sender_name).toBe('Тест')
  expect(states[1]!.payload?.content).toBe(encodedClean)
  expect(states[2]!.payload).toBeNull()
  const context = states[3]!.context
  expect(context.find(item => item.raw_message_id === states[0]!.raw_id)?.content).toBe(clean)
  expect(context.find(item => item.raw_message_id === states[1]!.raw_id)?.content).toBe(encodedClean)
  expect(context.some(item => item.raw_message_id === states[2]!.raw_id)).toBe(false)
  const captured = await (await request.get(`${provider}/test/ai-requests`)).json()
  expect(captured.embeddings).toContain(clean)
  expect(captured.embeddings).toContain(encodedClean)
  expect(captured.embeddings).not.toContain(original)
  expect(captured.embeddings).not.toContain(empty)
  expect(captured.embeddings).not.toContain('')
  const extraction = captured.chats.find((item: { content?: string }) => item.content === bodies[3])
  expect(extraction.context.find((item: { raw_message_id: number }) => item.raw_message_id === states[0]!.raw_id).content).toBe(clean)

  // Exercise retrieval too: it reloads RawMessage rather than returning payload text.
  const session = await login(page)
  const response = await request.post('/api/chat/query/', { headers: { Authorization: `Bearer ${session.access}` }, data: { prompt: original, idempotency_key: randomUUID() } })
  expect(response.status()).toBe(202)
  const operation = await response.json()
  await expect.poll(async () => (await (await request.get(operation.status_url, { headers: { Authorization: `Bearer ${session.access}` } })).json()).status, { timeout: 30000 }).toBe('succeeded')
  const search = await (await request.get(operation.status_url, { headers: { Authorization: `Bearer ${session.access}` } })).json()
  expect(search.result.quotes.find((quote: { id: number }) => quote.id === states[0]!.raw_id)?.content).toBe(clean)
  for (const quote of search.result.quotes) {
    expect(quote.content).not.toMatch(/<\/?(?:p|div|script|style|br|b|li|ul)\b|&(?:amp;)?lt;/i)
    expect(quote.sender_name).not.toContain('<')
  }

  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await adminLogin(page)
  await page.goto(`/admin/api/messageprocessingtrace/${states[3]!.trace_id}/change/`)
  const stage = page.getByRole('group').filter({ has: page.getByRole('heading', { name: 'Этап 2: История сообщений', exact: true }) })
  await expect(stage.getByTestId('context-message').filter({ hasText: token })).toHaveCount(2)
  await expect(stage).not.toContainText('<div')
  await expect(stage).not.toContainText('window.__xss')
  await expect(stage).toContainText('Құжат №7')
  await expect(stage.locator('[data-testid=context-message] .font-sans').filter({ hasText: `${token} Құжат №7` })).toHaveCSS('white-space', 'pre-wrap')
  expect(await page.evaluate(() => '__xss' in window)).toBe(false)
  await stage.screenshot({ path: testInfo.outputPath('stage-2-clean.png') })

  // A legacy trace may still hold raw HTML. Rendering must clean it without rewriting it.
  const fixture = Buffer.from(JSON.stringify([{ content: original, sender_name: '<b>Тест</b>', sent_at: '2026-09-01T12:00:00Z', score: 0.8 }, { text: encoded, author: '<i>Автор</i>', score: 0.7 }, { content: empty }])).toString('base64')
  isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `import base64,json; from api.models import MessageProcessingTrace; MessageProcessingTrace.objects.filter(pk=${states[3]!.trace_id}).update(context_metadata={}, earlier_messages_context=json.loads(base64.b64decode('${fixture}')))`])
  await page.reload()
  await expect(stage.getByTestId('context-message')).toHaveCount(2)
  await expect(stage).toContainText('2026-09-01T12:00:00Z')
  await expect(stage).not.toContainText('<div')
  await expect(stage).not.toContainText('&lt;')
  await expect(stage).not.toContainText('window.__xss')
  expect(await page.evaluate(() => '__xss' in window)).toBe(false)
  expect(readState(ids[3]!).context[0]!.content).toBe(original)
  isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `from api.models import MessageProcessingTrace; MessageProcessingTrace.objects.filter(pk=${states[3]!.trace_id}).update(earlier_messages_context=[{'content': '<script>window.__xss=1</script>'}])`])
  await page.reload()
  await expect(stage.getByTestId('context-message')).toHaveCount(0)
  await expect(stage).toContainText('В записи нет читаемых фрагментов истории')
  expect(errors).toEqual([])
})
