import { test, expect, type APIRequestContext, type Page } from '@playwright/test'
import { adminLogin, isolatedCommand } from './auth-helper'

const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
interface Config { id: number; name: string }
interface ProviderCall { method: string; path: string; name: string; action: string }
interface Fault { http_status?: number; delay?: number; disconnect?: boolean }
function fixture(suffix: string): Config {
  const name = `waha-e2e-${suffix}-${Date.now()}/тест`
  const output = isolatedCommand('shell', ['-c', `
import json
from django.conf import settings
from api.models import WhatsAppConfig
assert settings.INTEGRATION_TEST_MODE and settings.DATABASES['default']['NAME'].endswith('_e2e')
c = WhatsAppConfig.objects.create(name=${JSON.stringify(name)}, session_name=${JSON.stringify(name)}, status='STOPPED', waha_api_url='http://must-not-be-used.invalid', waha_api_key='must-not-be-used')
print(json.dumps({'id': c.id, 'name': c.session_name}))
`])
  return JSON.parse(output.split('\n').at(-1)!)
}
async function configure(request: APIRequestContext, config: Config, status = 'STOPPED', authenticated = true, faults: Record<string, Fault> = {}) {
  expect((await request.post(`${provider}/test/waha`, { data: { name: config.name, status, authenticated, faults } })).ok()).toBeTruthy()
}
async function calls(request: APIRequestContext, config: Config): Promise<ProviderCall[]> {
  const result: { requests: ProviderCall[] } = await (await request.get(`${provider}/test/waha`)).json()
  return result.requests.filter(call => call.name === config.name)
}
async function open(page: Page, config: Config) {
  await page.goto(`/admin/waha-dashboard/?config_id=${config.id}`)
}
async function command(page: Page, action: string, outcome = 'done') {
  const response = page.waitForResponse(res => res.url().endsWith(`/admin/waha-action/${action}/`) && res.request().method() === 'POST')
  await page.locator(`[data-waha-action="${action}"] button`).click()
  expect((await response).status()).toBe(302)
  await expect(page.getByTestId('waha-operation')).toHaveAttribute('data-state', outcome, { timeout: 25000 })
}
function eventCount(config: Config): number {
  return JSON.parse(isolatedCommand('shell', ['-c', `from api.models import OutboxEvent; print(OutboxEvent.objects.filter(event_type='waha_control', payload__config_id=${config.id}).count())`]).split('\n').at(-1)!)
}

test('WAHA controls reach the worker, preserve stop auth, confirm logout and render a fresh QR', async ({ page, request }) => {
  test.setTimeout(120000)
  const config = fixture('cycle')
  await configure(request, config, 'STOPPED', true, { start: { delay: 2 } })
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  await adminLogin(page)
  await open(page, config)
  await page.setViewportSize({ width: 1920, height: 1080 })
  expect(await page.locator('#waha-dashboard').evaluate(element => element.getBoundingClientRect().width)).toBeGreaterThan(1500)
  await command(page, 'start')
  await expect(page.getByTestId('waha-status')).toHaveText('WORKING')
  await expect(page.getByText('E2E WhatsApp', { exact: true })).toBeVisible()
  await command(page, 'restart')
  await command(page, 'stop')
  await expect(page.getByTestId('waha-status')).toHaveText('STOPPED')
  await command(page, 'start')
  await expect(page.getByTestId('waha-status')).toHaveText('WORKING')
  const beforeCancel = eventCount(config)
  page.once('dialog', dialog => dialog.dismiss())
  await page.getByRole('button', { name: 'Выйти (Logout)', exact: true }).click()
  expect(eventCount(config)).toBe(beforeCancel)
  page.once('dialog', dialog => dialog.accept())
  await command(page, 'logout')
  await expect(page.getByTestId('waha-status')).toHaveText('STOPPED')
  await command(page, 'start')
  await expect(page.getByTestId('waha-status')).toHaveText('SCAN_QR_CODE')
  const qr = page.getByRole('img', { name: 'QR-код WhatsApp' })
  await expect(qr).toBeVisible()
  expect(await qr.evaluate((image: HTMLImageElement) => image.naturalWidth > 0)).toBeTruthy()
  await command(page, 'qr')
  const count = eventCount(config)
  await page.reload()
  expect(eventCount(config)).toBe(count)
  await configure(request, config, 'WORKING')
  await command(page, 'status')
  await expect(qr).toHaveCount(0)
  const received = await calls(request, config)
  expect(received.filter(call => call.method === 'POST').map(call => call.action)).toEqual(['start', 'restart', 'stop', 'start', 'logout', 'start'])
  expect(received.some(call => call.path.includes(encodeURIComponent(config.name)))).toBeTruthy()
  expect(errors).toEqual([])
})

test('WAHA shows provider refusal and ambiguous outcomes without repeating commands', async ({ page, request }) => {
  test.setTimeout(120000)
  const config = fixture('failures')
  await adminLogin(page)
  await open(page, config)
  for (const http_status of [403, 404, 503]) {
    await configure(request, config, 'WORKING', true, { restart: { http_status } })
    await command(page, 'restart', 'failed')
    await expect(page.getByTestId('waha-operation')).toContainText(`waha_http_${http_status}`)
  }
  await configure(request, config, 'WORKING', true, { restart: { delay: 18 } })
  await command(page, 'restart', 'unknown')
  await expect(page.getByTestId('waha-operation')).toContainText('автоматически не повторяется')
  await expect(page.getByTestId('waha-status')).toHaveText('UNKNOWN')
  expect((await calls(request, config)).filter(call => call.action === 'restart')).toHaveLength(4)
  await configure(request, config, 'WORKING')
  await command(page, 'status')
  await expect(page.getByTestId('waha-status')).toHaveText('WORKING')
  await configure(request, config, 'WORKING', true, { status: { http_status: 503 } })
  await command(page, 'stop', 'unknown')
  await expect(page.getByTestId('waha-operation')).toContainText('Результат неизвестен')
  await configure(request, config, 'STOPPED', true, { status: { disconnect: true } })
  await command(page, 'status', 'failed')
  await expect(page.getByTestId('waha-operation')).toContainText('Не удалось подключиться')
  expect((await calls(request, config)).filter(call => call.method === 'POST')).toHaveLength(5)
})

test('WAHA validates config and CSRF, rejects parallel commands, and handles an empty configuration', async ({ page, request }) => {
  test.setTimeout(60000)
  const config = fixture('validation')
  const alias: Config = JSON.parse(isolatedCommand('shell', ['-c', `
import json
from api.models import WhatsAppConfig
c = WhatsAppConfig.objects.get(pk=${config.id})
c.pk = None
c.name = 'Another group sharing the session'
c.save()
print(json.dumps({'id': c.id, 'name': c.session_name}))
`]).split('\n').at(-1)!)
  await configure(request, config, 'WORKING', true, { stop: { delay: 6 } })
  await adminLogin(page)
  await open(page, config)
  const csrf = await page.locator('[name=csrfmiddlewaretoken]').first().inputValue()
  for (const config_id of ['', 'abc', '-1', '1.5', '999999999999999999999999']) {
    const response = await page.request.post('/admin/waha-action/stop/', { form: { config_id, csrfmiddlewaretoken: csrf } })
    expect(response.status()).toBe(400)
    expect(await response.text()).toContain('положительный целочисленный ID')
  }
  expect((await page.request.post('/admin/waha-action/stop/', { form: { config_id: config.id } })).status()).toBe(403)
  expect((await page.request.get('/admin/waha-action/stop/')).status()).toBe(405)
  expect((await page.request.post('/admin/waha-action/mystery/', { form: { config_id: config.id, csrfmiddlewaretoken: csrf } })).status()).toBe(400)
  expect(eventCount(config)).toBe(0)
  const staleTab = await page.context().newPage()
  await open(staleTab, config)
  await page.locator('[data-waha-action="stop"] button').click()
  await expect(page.locator('[data-waha-action="restart"] button')).toBeDisabled()
  const duplicate = await page.request.post('/admin/waha-action/restart/', { form: { config_id: config.id, csrfmiddlewaretoken: csrf } })
  expect(duplicate.status()).toBe(400)
  expect(await duplicate.text()).toContain('уже выполняется команда')
  expect((await page.request.post('/admin/waha-action/restart/', { form: { config_id: alias.id, csrfmiddlewaretoken: csrf } })).status()).toBe(400)
  await staleTab.locator('[data-waha-action="restart"] button').click()
  await expect(staleTab.getByRole('alert')).toContainText('уже выполняется команда')
  await expect(staleTab).toHaveURL(`/admin/waha-dashboard/?config_id=${config.id}`, { timeout: 20000 })
  await staleTab.close()
  await expect(page.getByTestId('waha-operation')).toHaveAttribute('data-state', 'done', { timeout: 20000 })
  expect(eventCount(config)).toBe(1)
  await open(page, alias)
  await expect(page.getByTestId('waha-status')).toHaveText('STOPPED')
  isolatedCommand('shell', ['-c', `from api.models import WhatsAppConfig; WhatsAppConfig.objects.filter(pk=${config.id}).update(is_active=False)`])
  const disabled = await page.request.post('/admin/waha-action/stop/', { form: { config_id: config.id, csrfmiddlewaretoken: csrf } })
  expect(disabled.status()).toBe(404)
  const activeIds: number[] = JSON.parse(isolatedCommand('shell', ['-c', "import json; from api.models import WhatsAppConfig; print(json.dumps(list(WhatsAppConfig.objects.filter(is_active=True).values_list('id', flat=True))))"]).split('\n').at(-1)!)
  try {
    isolatedCommand('shell', ['-c', 'from api.models import WhatsAppConfig; WhatsAppConfig.objects.update(is_active=False)'])
    expect((await page.goto('/admin/waha-dashboard/'))?.status()).toBe(200)
    await expect(page.getByText('Нет выбранной активной конфигурации WhatsApp.')).toBeVisible()
    await expect(page.locator('[data-waha-action]')).toHaveCount(0)
  } finally {
    isolatedCommand('shell', ['-c', `from api.models import WhatsAppConfig; WhatsAppConfig.objects.filter(id__in=${JSON.stringify(activeIds)}).update(is_active=True)`])
  }
})

test('WAHA requires the integration permission for page, commands and saved state', async ({ page }) => {
  const config = fixture('permissions')
  const username = `waha-staff-${Date.now()}`
  isolatedCommand('shell', ['-c', `from django.contrib.auth.models import User; User.objects.create_user(username='${username}', password='isolated-staff-password', is_staff=True)`])
  await page.goto('/admin/login/?next=/admin/')
  await page.locator('[name=username]').fill(username)
  await page.locator('[name=password]').fill('isolated-staff-password')
  await page.locator('button[type=submit], input[type=submit]').click()
  await page.waitForURL('**/admin/')
  const csrf = await page.locator('[name=csrfmiddlewaretoken]').first().inputValue()
  expect((await page.goto(`/admin/waha-dashboard/?config_id=${config.id}`))?.status()).toBe(403)
  expect((await page.request.get(`/admin/waha-state/?config_id=${config.id}`)).status()).toBe(403)
  expect((await page.request.post('/admin/waha-action/stop/', { form: { config_id: config.id, csrfmiddlewaretoken: csrf } })).status()).toBe(403)
  expect(eventCount(config)).toBe(0)
  await page.context().clearCookies()
  await page.goto(`/admin/waha-dashboard/?config_id=${config.id}`)
  await expect(page).toHaveURL(/\/admin\/login\//)
})
