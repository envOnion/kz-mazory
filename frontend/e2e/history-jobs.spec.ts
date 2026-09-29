import { test, expect, type Page, type APIRequestContext } from '@playwright/test'
import { adminLogin, isolatedCommand } from './auth-helper'

const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
interface Source { id: number; name: string; session: string; chat: string }
interface HistoryMessage { id: string; timestamp: number; body: string; from: string; to: string; participant: string; notifyName: string }
interface ProviderFault { http_status?: number; delay?: number; repeated_page?: boolean; page_cap?: number }
function inspect<T>(code: string): T {
  return JSON.parse(isolatedCommand('shell', ['-c', `import json\n${code}`]).split('\n').at(-1)!) as T
}
function fixture(label: string): Source {
  const name = `history-${label}-${Date.now()}`
  return inspect<Source>(`from django.conf import settings
from api.models import Team, WhatsAppConfig
assert settings.INTEGRATION_TEST_MODE and settings.DATABASES['default']['NAME'].endswith('_e2e')
t = Team.objects.create(name='${name}')
c = WhatsAppConfig.objects.create(name='${name}', session_name='${name}', group_jid='${name}@g.us', team=t)
print(json.dumps({'id':c.id,'name':c.name,'session':c.session_name,'chat':c.group_jid}))`)
}
function history(source: Source, count = 26): HistoryMessage[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `${source.session}-${i}`, timestamp: 1700000000 + i,
    body: i === 0 ? 'Толық мәтін история без сокращений. '.repeat(500) : i === count - 1 ? 'E2E payment: amount=4567 date=2023-11-14' : i === 7 ? '' : `Историческое сообщение ${i}`,
    from: source.chat, to: 'fixture@c.us', participant: '77000000002@c.us', notifyName: 'E2E manager',
  }))
}
async function configure(request: APIRequestContext, source: Source, status: string, items: HistoryMessage[], faults: Record<string, ProviderFault> = {}) {
  expect((await request.post(`${provider}/test/waha`, { data: { name: source.session, status, messages: items, faults, authenticated: true, group_title: source.name } })).ok()).toBeTruthy()
}
async function save(page: Page) {
  await page.locator('[name=_save]').first().click()
  await expect(page.locator('.errorlist')).toHaveCount(0)
}
async function createJob(page: Page, source: Source, analyze = true, interval = 0): Promise<number> {
  await page.goto('/admin/api/whatsapphistoryjob/add/')
  await page.locator('#id_config').selectOption(String(source.id))
  await page.locator('#id_page_size').fill('7')
  await page.locator('#id_initial_wait_seconds').fill('0')
  await page.locator('#id_poll_seconds').fill('1')
  await page.locator('#id_stable_scans_required').fill('2')
  await page.locator('#id_interval_minutes').fill(String(interval))
  await page.locator('#id_analyze_after_import').setChecked(analyze)
  await save(page)
  return inspect<number>(`from api.models import WhatsAppHistoryJob; print(json.dumps(WhatsAppHistoryJob.objects.get(config_id=${source.id}).id))`)
}
async function start(page: Page, job: number): Promise<number> {
  await page.goto(`/admin/api/whatsapphistoryjob/${job}/change/`)
  await page.getByTestId('history-start').click()
  await page.waitForURL(/\/admin\/api\/whatsapphistoryrun\/\d+\/change\//)
  return Number(page.url().match(/whatsapphistoryrun\/(\d+)/)![1])
}
async function analysisPause(page: Page, paused: boolean) {
  const id = inspect<number>('from api.models import AISettings; print(json.dumps(AISettings.get_active().id))')
  await page.goto(`/admin/api/aisettings/${id}/change/`)
  await page.locator('#id_message_processing_paused').setChecked(paused)
  await save(page)
}

test('History jobs import every page and full text, expose progress, pause AI and deduplicate a repeat', async ({ page, request }) => {
  test.setTimeout(180000)
  const source = fixture('complete')
  const items = history(source)
  await configure(request, source, 'WORKING', items, { messages: { page_cap: 3 } })
  await adminLogin(page)
  await analysisPause(page, true)
  try {
    const job = await createJob(page, source)
    const run = await start(page, job)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'analyzing', { timeout: 60000 })
    const saved = inspect<{ count: number; content: string; traces: number }>(`from api.models import RawMessage, MessageProcessingTrace
q = RawMessage.objects.filter(config_id=${source.id})
print(json.dumps({'count':q.count(),'content':q.order_by('timestamp').first().content,'traces':MessageProcessingTrace.objects.filter(raw_message__config_id=${source.id}).count()}))`)
    expect(saved.count).toBe(items.length)
    expect(saved.content).toBe(items[0]!.body)
    expect(saved.content.length).toBeGreaterThan(10000)
    expect(saved.traces).toBe(0)
    await page.getByRole('link', { name: 'Сообщения', exact: true }).click()
    await expect(page.locator('#result_list')).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`history_run=${run}`))
    await analysisPause(page, false)
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'completed', { timeout: 60000 })
    const evidence = inspect<{ earlier: number; first: string; traces: number; scoped: number }>(`from api.models import MessageProcessingTrace,RawMessage
t = MessageProcessingTrace.objects.filter(raw_message__config_id=${source.id}).order_by('-raw_message__timestamp').first()
print(json.dumps({'earlier':t.earlier_messages_count,'first':t.earlier_messages_context[0]['content'],'traces':MessageProcessingTrace.objects.filter(raw_message__config_id=${source.id}).count(),'scoped':RawMessage.objects.filter(config_id=${source.id},team_id__isnull=False).count()}))`)
    expect(evidence.earlier).toBe(items.length - 2)
    expect(evidence.first).toBe(items[0]!.body.trim())
    expect(evidence.scoped).toBe(items.length)
    const again = await start(page, job)
    expect(again).not.toBe(run)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'completed', { timeout: 60000 })
    const duplicate = inspect<{ fresh: number; existing: number; raw: number; traces: number }>(`from api.models import WhatsAppHistoryRun,RawMessage,MessageProcessingTrace
r = WhatsAppHistoryRun.objects.get(pk=${again})
print(json.dumps({'fresh':r.imported_count,'existing':r.existing_count,'raw':RawMessage.objects.filter(config_id=${source.id}).count(),'traces':MessageProcessingTrace.objects.filter(raw_message__config_id=${source.id}).count()}))`)
    expect(duplicate).toEqual({ fresh: 0, existing: items.length, raw: items.length, traces: evidence.traces })
    await page.getByRole('link', { name: 'Шаги в очереди', exact: true }).click()
    await expect(page.locator('#result_list')).toContainText('Импорт истории WhatsApp')
    await page.goto(`/admin/api/outboxevent/?history_run=${run}&event_type__exact=extract_message`)
    await expect(page.locator('#result_list')).toContainText('Анализ сообщения')
  } finally {
    await analysisPause(page, false)
  }
})

test('An import waits for WhatsApp, supports pause/resume/cancel and rejects a changed source', async ({ page, request }) => {
  test.setTimeout(120000)
  const source = fixture('control')
  const items = history(source, 3)
  await configure(request, source, 'STOPPED', items)
  await adminLogin(page)
  const job = await createJob(page, source, false)
  const run = await start(page, job)
  await expect(page.getByTestId('history-progress')).toContainText('STOPPED', { timeout: 15000 })
  await page.getByRole('button', { name: 'Приостановить импорт', exact: true }).click()
  await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'paused')
  const csrf = await page.locator('[name=csrfmiddlewaretoken]').first().inputValue()
  const duplicate = await page.request.post(`/admin/api/whatsapphistoryjob/${job}/start/`, { form: { csrfmiddlewaretoken: csrf } })
  expect(await duplicate.text()).toContain('уже есть незавершённый запуск')
  await configure(request, source, 'WORKING', items)
  await page.getByRole('button', { name: 'Продолжить импорт', exact: true }).click()
  await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'completed', { timeout: 45000 })
  expect(inspect<number>(`from api.models import MessageProcessingTrace; print(json.dumps(MessageProcessingTrace.objects.filter(raw_message__config_id=${source.id}).count()))`)).toBe(0)
  await configure(request, source, 'STOPPED', items)
  const cancelled = await start(page, job)
  await page.getByRole('button', { name: 'Отменить импорт', exact: true }).click()
  await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'cancelled')
  expect(cancelled).not.toBe(run)
  const changed = await start(page, job)
  await expect(page.getByTestId('history-progress')).toContainText('STOPPED', { timeout: 15000 })
  await page.goto(`/admin/api/whatsappconfig/${source.id}/change/`)
  await page.locator('#id_group_jid').fill('changed-source@g.us')
  await save(page)
  await page.goto(`/admin/api/whatsapphistoryrun/${changed}/change/`)
  await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'failed', { timeout: 15000 })
  await expect(page.getByTestId('history-progress')).toContainText('history_source_changed')
})

test('Periodic jobs run without a click; provider errors and empty history are visible', async ({ page, request }) => {
  test.setTimeout(120000)
  const source = fixture('schedule')
  await configure(request, source, 'WORKING', [])
  await adminLogin(page)
  const job = await createJob(page, source, false, 1)
  try {
    await expect.poll(() => inspect<number>(`from api.models import WhatsAppHistoryRun; print(json.dumps(WhatsAppHistoryRun.objects.filter(job_id=${job}).count()))`), { timeout: 15000 }).toBe(1)
    const run = inspect<number>(`from api.models import WhatsAppHistoryRun; print(json.dumps(WhatsAppHistoryRun.objects.get(job_id=${job}).id))`)
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'empty', { timeout: 30000 })
    await expect(page.getByTestId('history-progress')).toContainText('WAHA не вернул сообщений')
    await page.goto(`/admin/api/whatsapphistoryjob/${job}/change/`)
    await page.locator('#id_interval_minutes').fill('0')
    await save(page)
    await configure(request, source, 'WORKING', history(source, 5), { messages: { http_status: 403 } })
    await start(page, job)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'failed', { timeout: 30000 })
    await expect(page.getByTestId('history-progress')).toContainText('history_waha_http_403')
    await configure(request, source, 'WORKING', history(source, 10), { messages: { repeated_page: true } })
    await start(page, job)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'failed', { timeout: 30000 })
    await expect(page.getByTestId('history-progress')).toContainText('history_repeated_page')
    expect(inspect<number>(`from api.models import RawMessage; print(json.dumps(RawMessage.objects.filter(config_id=${source.id}).count()))`)).toBe(0)
  } finally {
    await page.goto(`/admin/api/whatsapphistoryjob/${job}/change/`)
    await page.locator('#id_enabled').uncheck()
    await save(page)
  }
})

test('Job controls enforce CSRF and integration permissions; invalid settings are rejected', async ({ page }) => {
  const source = fixture('permissions')
  await adminLogin(page)
  const job = await createJob(page, source, false)
  expect((await page.request.get(`/admin/api/whatsapphistoryjob/${job}/start/`)).status()).toBe(405)
  expect((await page.request.post(`/admin/api/whatsapphistoryjob/${job}/start/`)).status()).toBe(403)
  await page.goto(`/admin/api/whatsapphistoryjob/${job}/change/`)
  await page.locator('#id_page_size').fill('0')
  await page.locator('[name=_save]').first().click()
  await expect(page.locator('.errorlist')).toContainText('Допустимо от 1 до 1000')
  isolatedCommand('shell', ['-c', `from api.models import WhatsAppConfig; WhatsAppConfig.objects.filter(pk=${source.id}).update(is_active=False)`])
  await page.locator('#id_page_size').fill('7')
  await page.locator('#id_enabled').uncheck()
  await save(page)
  expect(inspect<boolean>(`from api.models import WhatsAppHistoryJob; print(json.dumps(WhatsAppHistoryJob.objects.get(pk=${job}).enabled))`)).toBe(false)
  const username = `history-staff-${Date.now()}`
  isolatedCommand('shell', ['-c', `from django.contrib.auth.models import User; User.objects.create_user(username='${username}', password='isolated-staff-password', is_staff=True)`])
  await page.context().clearCookies()
  await page.goto('/admin/login/?next=/admin/')
  await page.locator('[name=username]').fill(username)
  await page.locator('[name=password]').fill('isolated-staff-password')
  await page.locator('button[type=submit], input[type=submit]').click()
  await page.waitForURL('**/admin/')
  const csrf = await page.locator('[name=csrfmiddlewaretoken]').first().inputValue()
  expect((await page.request.get(`/admin/api/whatsapphistoryjob/${job}/change/`)).status()).toBe(403)
  expect((await page.request.post(`/admin/api/whatsapphistoryjob/${job}/start/`, { form: { csrfmiddlewaretoken: csrf } })).status()).toBe(403)
  expect(inspect<number>(`from api.models import WhatsAppHistoryRun; print(json.dumps(WhatsAppHistoryRun.objects.filter(job_id=${job}).count()))`)).toBe(0)
})

test('Daily AI budget defers analysis without losing retries and resumes when the admin raises it', async ({ page, request }) => {
  test.setTimeout(90000)
  const source = fixture('budget')
  await configure(request, source, 'WORKING', history(source, 2))
  isolatedCommand('shell', ['-c', `from api.models import ProviderUsage; ProviderUsage.objects.create(operation='e2e_fixture', model_name='fixture', duration_ms=0, succeeded=True, cost_usd=0)`])
  await adminLogin(page)
  const ai = inspect<number>('from api.models import AISettings; print(json.dumps(AISettings.get_active().id))')
  async function setLimit(value: string) {
    await page.goto(`/admin/api/aisettings/${ai}/change/`)
    await page.locator('#id_daily_request_limit').fill(value)
    await save(page)
  }
  await setLimit('1')
  try {
    const job = await createJob(page, source)
    const run = await start(page, job)
    await expect.poll(() => inspect<number>(`from api.models import OutboxEvent,RawMessage
ids=list(RawMessage.objects.filter(config_id=${source.id}).values_list('id',flat=True))
print(json.dumps(OutboxEvent.objects.filter(event_type='extract_message',payload__raw_id__in=ids,state='pending',error_code='ai_daily_budget_exhausted',attempt_count=0).count()))`), { timeout: 45000 }).toBe(2)
    await setLimit('0')
    await page.goto(`/admin/api/whatsapphistoryrun/${run}/change/`)
    await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'completed', { timeout: 30000 })
    expect(inspect<number>(`from api.models import RawMessage; print(json.dumps(RawMessage.objects.filter(config_id=${source.id},processed=True).count()))`)).toBe(2)
  } finally {
    await setLimit('0')
  }
})
