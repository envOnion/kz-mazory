import { createHmac } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { test, expect, type Page, type APIRequestContext } from '@playwright/test'
import { adminLogin, isolatedCommand } from './auth-helper'

const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
interface Source { id: number; name: string; session: string; chat: string; stamp: number }
interface Message { id: string; timestamp: number; body: string; from: string; to: string; participant: string; notifyName: string }
function inspect<T>(code: string): T {
  return JSON.parse(isolatedCommand('shell', ['-c', `import json\n${code}`]).split('\n').at(-1)!) as T
}
function fixture(label: string, seeded = true): Source {
  const name = `live-${label}-${Date.now()}`
  return inspect<Source>(`from django.conf import settings
from django.utils import timezone
from datetime import timedelta
import hashlib
from api.models import Team, WhatsAppConfig, RawMessage
assert settings.INTEGRATION_TEST_MODE and settings.DATABASES['default']['NAME'].endswith('_e2e')
t=Team.objects.create(name='${name}')
c=WhatsAppConfig.objects.create(name='${name}',session_name='${name}',group_jid='${name}@g.us',team=t)
stamp=timezone.now().replace(microsecond=0)-timedelta(seconds=600)
if ${seeded ? 'True' : 'False'}:
    body='Earlier context for the same project'
    RawMessage.objects.create(config=c,team=t,source='waha',session_name=c.session_name,chat_id=c.group_jid,message_id='${name}-seed',source_revision=hashlib.sha256(body.encode()).hexdigest(),timestamp=stamp,content=body,processed=True)
print(json.dumps({'id':c.id,'name':c.name,'session':c.session_name,'chat':c.group_jid,'stamp':int(stamp.timestamp())}))`)
}
function message(source: Source, id: string, stamp = Math.floor(Date.now() / 1000) - 1, body = 'A new project message'): Message {
  return { id: `${source.session}-${id}`, timestamp: stamp, body, from: source.chat, to: 'fixture@c.us', participant: '77000000002@c.us', notifyName: 'E2E manager' }
}
async function configure(request: APIRequestContext, source: Source, items: Message[], faults: Record<string, object> = {}, status = 'WORKING') {
  expect((await request.post(`${provider}/test/waha`, { data: { name: source.session, status, messages: items, faults, authenticated: true, group_title: source.name } })).ok()).toBeTruthy()
}
async function save(page: Page) {
  const destination = new URL(page.url()).pathname.replace(/(?:add|\d+\/change)\/$/, '')
  await page.locator('[name=_save]').first().click()
  await page.waitForURL(url => url.pathname === destination)
}
async function pauseAI(page: Page, paused: boolean) {
  const id = inspect<number>('from api.models import AISettings; print(json.dumps(AISettings.get_active().id))')
  await page.goto(`/admin/api/aisettings/${id}/change/`)
  await page.locator('#id_message_processing_paused').setChecked(paused)
  await save(page)
}
async function createJob(page: Page, source: Source, analyze = true): Promise<number> {
  await page.goto('/admin/api/whatsapphistoryjob/add/')
  await page.locator('#id_config').selectOption(String(source.id))
  await page.locator('#id_only_new').check()
  await expect(page.locator('[data-new-messages-help]')).toBeVisible()
  await expect(page.locator('#id_new_message_poll_seconds')).toHaveValue('5')
  await page.locator('#id_page_size').fill('2')
  await page.locator('#id_analyze_after_import').setChecked(analyze)
  await save(page)
  await expect(page.locator('.errorlist')).toHaveCount(0)
  return inspect<number>(`from api.models import WhatsAppHistoryJob; print(json.dumps(WhatsAppHistoryJob.objects.get(config_id=${source.id}).id))`)
}
function rawCount(source: Source) {
  return inspect<number>(`from api.models import RawMessage; print(json.dumps(RawMessage.objects.filter(config_id=${source.id}).count()))`)
}
async function runId(job: number): Promise<number> {
  await expect.poll(() => inspect<number>(`from api.models import WhatsAppHistoryRun; print(json.dumps(WhatsAppHistoryRun.objects.filter(job_id=${job}).count()))`)).toBe(1)
  return inspect<number>(`from api.models import WhatsAppHistoryRun; print(json.dumps(WhatsAppHistoryRun.objects.get(job_id=${job}).pk))`)
}
async function stop(page: Page, job: number) {
  await page.goto(`/admin/api/whatsapphistoryjob/${job}/change/`)
  await page.locator('#id_enabled').uncheck()
  await save(page)
}
async function webhook(request: APIRequestContext, source: Source, item: Message, event = 'message') {
  const body = JSON.stringify({ event, session: source.session, payload: { ...item, fromMe: false } })
  return request.post('/api/whatsapp/webhook/', { data: body, headers: { 'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-test-webhook').update(body).digest('hex') } })
}
async function reads(request: APIRequestContext, source: Source) {
  const data: { requests: { name: string; action: string; path: string }[] } = await (await request.get(`${provider}/test/waha`)).json()
  return data.requests.filter(r => r.name === source.session && r.action === 'messages')
}

test('Only new reads bounded pages forever, keeps context and ingests while AI is paused', async ({ page, request }) => {
  test.setTimeout(150000)
  const source = fixture('context')
  const old = message(source, 'old', source.stamp - 100)
  const a = message(source, 'a')
  const b = message(source, 'b', a.timestamp, 'E2E payment: amount=4567 date=2023-11-14')
  const empty = message(source, 'empty', a.timestamp, '')
  await configure(request, source, [old, a, b, empty], { messages: { page_cap: 1 } })
  await adminLogin(page)
  await pauseAI(page, true)
  const job = await createJob(page, source)
  try {
    const run = await runId(job)
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await expect.poll(() => rawCount(source), { timeout: 45000 }).toBe(4)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'watching', { timeout: 30000 })
    expect(inspect<number>(`from api.models import MessageProcessingTrace; print(json.dumps(MessageProcessingTrace.objects.filter(raw_message__config_id=${source.id}).count()))`)).toBe(0)
    const firstReads = (await reads(request, source)).length
    await expect.poll(async () => (await reads(request, source)).length, { timeout: 30000 }).toBeGreaterThan(firstReads)
    expect(rawCount(source)).toBe(4)
    const c = message(source, 'c')
    await configure(request, source, [old, a, b, empty, c])
    await expect.poll(() => rawCount(source), { timeout: 30000 }).toBe(5)
    expect((await webhook(request, source, a)).status()).toBe(200)
    const d = message(source, 'webhook-target')
    expect((await webhook(request, source, d)).status()).toBe(202)
    expect((await webhook(request, source, d)).status()).toBe(200)
    await configure(request, source, [old, a, b, empty, c, d])
    await expect.poll(() => rawCount(source), { timeout: 30000 }).toBe(6)
    const paths = await reads(request, source)
    expect(paths.length).toBeGreaterThan(3)
    for (const read of paths) {
      const query = new URL(read.path, provider).searchParams
      expect(Number(query.get('filter.timestamp.gte'))).toBeGreaterThanOrEqual(source.stamp)
      expect(query.get('sortOrder')).toBe('asc')
    }
    await page.goto(`/admin/api/whatsapphistoryjob/${job}/change/`)
    await page.getByTestId('history-start').click()
    await page.waitForURL(new RegExp(`/whatsapphistoryrun/${run}/change/`))
    await pauseAI(page, false)
    await expect.poll(() => inspect<number>(`from api.models import MessageProcessingTrace; print(json.dumps(MessageProcessingTrace.objects.filter(raw_message__config_id=${source.id},status='success').count()))`), { timeout: 45000 }).toBe(4)
    const context = inspect<{ text: string[]; own: boolean }>(`from api.models import MessageProcessingTrace
q=MessageProcessingTrace.objects.get(raw_message__message_id='${b.id}')
print(json.dumps({'text':[v['content'] for v in q.earlier_messages_context],'own':all(v['message_id'].startswith('${source.session}') for v in q.earlier_messages_context)}))`)
    expect(context.text).toContain('Earlier context for the same project')
    expect(context.text).toContain(a.body)
    expect(context.own).toBe(true)
    expect(await runId(job)).toBe(run)
  } finally {
    await stop(page, job)
    await pauseAI(page, false)
  }
})

test('Webhook and polling deduplicate in both orders and honor disabled analysis', async ({ page, request }) => {
  test.setTimeout(120000)
  const source = fixture('webhook')
  await configure(request, source, [])
  await adminLogin(page)
  const job = await createJob(page, source, false)
  try {
    await runId(job)
    const a = message(source, 'webhook-first')
    expect((await webhook(request, source, a)).status()).toBe(202)
    const b = message(source, 'poll-first')
    await configure(request, source, [a, b])
    await expect.poll(() => rawCount(source), { timeout: 30000 }).toBe(3)
    expect((await webhook(request, source, b)).status()).toBe(200)
    expect((await webhook(request, source, a)).status()).toBe(200)
    const revised = { ...a, body: 'Edited project detail' }
    expect((await webhook(request, source, revised, 'message.edited')).status()).toBe(202)
    await configure(request, source, [revised, b])
    const count = (await reads(request, source)).length
    await expect.poll(async () => (await reads(request, source)).length, { timeout: 30000 }).toBeGreaterThan(count)
    expect(rawCount(source)).toBe(4)
    expect(inspect<number>(`from api.models import OutboxEvent,RawMessage
ids=list(RawMessage.objects.filter(config_id=${source.id}).values_list('id',flat=True))
print(json.dumps(OutboxEvent.objects.filter(event_type='extract_message',payload__raw_id__in=ids).count()))`)).toBe(0)
    expect(inspect<number>(`from api.models import MessageProcessingTrace; print(json.dumps(MessageProcessingTrace.objects.filter(raw_message__config_id=${source.id}).count()))`)).toBe(0)
  } finally { await stop(page, job) }
})

test('An empty source starts now, pauses durably, resumes and validates live settings', async ({ page, request }) => {
  test.setTimeout(120000)
  const source = fixture('controls', false)
  const old = message(source, 'old', source.stamp)
  await configure(request, source, [old])
  await adminLogin(page)
  const job = await createJob(page, source, false)
  try {
    const run = await runId(job)
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'watching', { timeout: 30000 })
    expect(rawCount(source)).toBe(0)
    await page.getByRole('button', { name: 'Приостановить импорт', exact: true }).click()
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'paused')
    const stoppedReads = (await reads(request, source)).length
    const fresh = message(source, 'fresh', Math.floor(Date.now() / 1000))
    await configure(request, source, [old, fresh])
    await page.goto(`/admin/api/whatsapphistoryjob/${job}/change/`)
    await page.locator('#id_new_message_poll_seconds').fill('1')
    await page.locator('[name=_save]').first().click()
    await expect(page.locator('.errorlist')).toContainText('Допустимо от 5 до 3600')
    await page.locator('#id_new_message_poll_seconds').fill('5')
    await page.locator('#id_only_new').uncheck()
    await page.locator('[name=_save]').first().click()
    await expect(page.locator('.errorlist')).toContainText('Сначала отмените активный запуск')
    expect((await reads(request, source)).length).toBe(stoppedReads)
    expect(rawCount(source)).toBe(0)
    await page.goto(`/admin/api/whatsappconfig/${source.id}/change/`)
    await page.locator('#id_group_jid').fill('another@g.us')
    await page.locator('[name=_save]').first().click()
    await expect(page.locator('.errorlist')).toContainText('Сначала отмените мониторинг')
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await page.getByRole('button', { name: 'Продолжить импорт', exact: true }).click()
    await expect.poll(() => rawCount(source), { timeout: 30000 }).toBe(1)
    await stop(page, job)
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'paused')
    await page.goto(`/admin/api/whatsapphistoryjob/${job}/change/`)
    await page.locator('#id_enabled').check()
    await save(page)
    const second = message(source, 'second', Math.floor(Date.now() / 1000))
    await configure(request, source, [old, fresh, second])
    await expect.poll(() => rawCount(source), { timeout: 30000 }).toBe(2)
    expect(await runId(job)).toBe(run)
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await page.getByRole('button', { name: 'Отменить импорт', exact: true }).click()
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'cancelled')
    expect(inspect<boolean>(`from api.models import WhatsAppHistoryJob; print(json.dumps(WhatsAppHistoryJob.objects.get(pk=${job}).enabled))`)).toBe(false)
  } finally { await stop(page, job) }
})

test('Transient provider failures retry beyond three attempts and recover the same monitor', async ({ page, request }) => {
  test.setTimeout(180000)
  const source = fixture('retry')
  const fresh = message(source, 'fresh')
  await configure(request, source, [fresh], { messages: { http_status: 503 } })
  await adminLogin(page)
  const job = await createJob(page, source, false)
  try {
    const run = await runId(job)
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await expect.poll(async () => (await reads(request, source)).length, { timeout: 90000 }).toBeGreaterThanOrEqual(4)
    await expect(page.getByTestId('history-progress')).toContainText('прогресс сохранён')
    expect(rawCount(source)).toBe(1)
    await configure(request, source, [fresh])
    await expect.poll(() => rawCount(source), { timeout: 65000 }).toBe(2)
    expect(await runId(job)).toBe(run)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'watching', { timeout: 15000 })
    await expect(page.locator('[data-history-error]')).toBeEmpty()
  } finally { await stop(page, job) }
})

test('A stale page cannot commit after pause, and a killed worker resumes from its durable step', async ({ page, request }) => {
  test.setTimeout(150000)
  const source = fixture('restart')
  const first = message(source, 'first')
  await configure(request, source, [first], { messages: { delay: 10 } })
  await adminLogin(page)
  const job = await createJob(page, source, false)
  try {
    const run = await runId(job)
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await expect.poll(async () => (await reads(request, source)).length, { timeout: 20000 }).toBeGreaterThan(0)
    await page.getByRole('button', { name: 'Приостановить импорт', exact: true }).click()
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'paused')
    // Wait for the real in-flight task to return, without sleeping the test.
    await expect.poll(() => inspect<number>(`from api.models import OutboxEvent
print(json.dumps(OutboxEvent.objects.filter(event_type='history_import',payload__history_run_id=${run},state='processing').count()))`), { timeout: 20000 }).toBe(0)
    expect(rawCount(source)).toBe(1)
    await configure(request, source, [first])
    await page.getByRole('button', { name: 'Продолжить импорт', exact: true }).click()
    await expect.poll(() => rawCount(source), { timeout: 20000 }).toBe(2)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'watching', { timeout: 20000 })
    const checkpoint = inspect<string>(`from api.models import WhatsAppHistoryJob; print(json.dumps(WhatsAppHistoryJob.objects.get(pk=${job}).new_messages_since.isoformat()))`)
    const second = message(source, 'second')
    const previous = (await reads(request, source)).length
    await configure(request, source, [first, second], { messages: { delay: 10 } })
    await expect.poll(async () => (await reads(request, source)).length, { timeout: 20000 }).toBeGreaterThan(previous)
    const composeFile = new URL('../../compose.e2e.yml', import.meta.url).pathname
    execFileSync('docker', ['compose', '-f', composeFile, '-p', process.env.E2E_COMPOSE_PROJECT || 'mazory-platform-e2e', 'restart', '-t', '1', 'history'], { timeout: 30000, stdio: 'pipe' })
    // Accelerate only the abandoned test lease; production uses its normal lease.
    isolatedCommand('shell', ['-c', `from api.models import OutboxEvent; from django.utils import timezone; from datetime import timedelta
OutboxEvent.objects.filter(event_type='history_import',payload__history_run_id=${run},state__in=['processing','enqueued']).update(lease_until=timezone.now()-timedelta(seconds=1))`])
    await configure(request, source, [first, second])
    await expect.poll(() => rawCount(source), { timeout: 30000 }).toBe(3)
    expect(await runId(job)).toBe(run)
    const current = inspect<string>(`from api.models import WhatsAppHistoryJob; print(json.dumps(WhatsAppHistoryJob.objects.get(pk=${job}).new_messages_since.isoformat()))`)
    expect(current >= checkpoint).toBe(true)
  } finally { await stop(page, job) }
})
