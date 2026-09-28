import { test, expect, type Page, type APIRequestContext } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { adminLogin, isolatedCommand } from './auth-helper'

const provider = process.env.E2E_PROVIDER_URL || 'http://127.0.0.1:18090'
const guard = "from django.conf import settings; assert settings.INTEGRATION_TEST_MODE and settings.DATABASES['default']['NAME'].endswith('_e2e'); "
interface Fixture { config: number; project: number; deal: string }
function fixture(): Fixture {
  return JSON.parse(isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `
import json, time
from api.models import BitrixSettings, Project, Team
assert not BitrixSettings.objects.filter(is_active=True).exists()
c = BitrixSettings.objects.create(name='E2E CRM editing')
deal = str(time.time_ns())
p = Project.objects.create(name='E2E CRM editing ' + deal, bitrix_id=deal, team=Team.objects.filter(is_active=True).first())
print(json.dumps({'config': c.id, 'project': p.id, 'deal': deal}))
`]))
}
async function save(page: Page) {
  const response = page.waitForResponse(res => res.request().method() === 'POST' && res.url().includes('/bitrixsettings/'))
  await page.locator('[name=_continue]').click()
  expect((await response).status()).toBe(302)
  await expect(page.getByText(/был изменен успешно/)).toBeVisible()
}
async function importDeal(request: APIRequestContext, row: Fixture, credential: string) {
  async function received() {
    const calls: { requests: { credential: string; deal_id: string }[] } = await (await request.get(`${provider}/test/crm`)).json()
    return calls.requests.filter(call => call.deal_id === row.deal)
  }
  const before = (await received()).length
  const response = await request.post('/api/bitrix/webhook/', {
    headers: { 'X-Bitrix-Token': 'isolated-test-bitrix-inbound' },
    data: { event: 'ONCRMDEALUPDATE', data: { FIELDS: { ID: row.deal } }, test_nonce: randomUUID() },
  })
  expect(response.status()).toBe(202)
  await expect.poll(async () => {
    const calls = await received()
    return calls.length > before && calls.at(-1)?.credential === credential
  }, { timeout: 30000 }).toBe(true)
  await expect.poll(() => isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `from api.models import OutboxEvent; print(OutboxEvent.objects.filter(event_type='crm_import', payload__deal_id='${row.deal}').order_by('-id').values_list('state', flat=True).first())`]), { timeout: 30000 }).toBe('done')
}

test('Bitrix Webhook can be replaced through admin and is used by the CRM worker', async ({ page, request }) => {
  test.setTimeout(120000)
  const row = fixture()
  const credential = `e2e-${randomUUID()}`
  const url = `http://test-provider:9000/rest/1/${credential}/`
  try {
    await adminLogin(page)
    await page.goto('/admin/api/bitrixsettings/')
    const configRow = page.locator('#result_list .data-row').filter({ hasText: 'E2E CRM editing' })
    await expect(configRow).toContainText('Из окружения сервера')
    await expect(configRow).toContainText('http://test-provider/rest/•••/•••/')
    expect(await page.content()).not.toContain('e2e-environment')
    await importDeal(request, row, 'e2e-environment')
    await configRow.getByRole('link', { name: 'Изменить Webhook →' }).click()
    const input = page.getByLabel('Новый REST Webhook URL')
    await expect(input).toHaveValue('')
    await expect(input).toHaveAttribute('type', 'password')
    await input.fill(url)
    await save(page)
    await expect(input).toHaveValue('')
    await expect(page.getByText('Сохранён в настройках', { exact: true })).toBeVisible()
    expect(await page.content()).not.toContain(credential)
    await importDeal(request, row, credential)
    await save(page)
    await importDeal(request, row, credential)
    for (const invalid of ['http://127.0.0.1/rest/1/secret/', 'https://aquakip.bitrix24.kz/rest/1/secret/crm.deal.get.json', 'https://aquakip.bitrix24.kz/rest/1/secret/?leak=yes']) {
      await input.fill(invalid)
      await page.locator('[name=_continue]').click()
      await expect(page.getByText(/Используйте HTTPS и разрешённый портал|Укажите полный REST Webhook URL вида/)).toBeVisible()
      await expect(input).toHaveValue('')
      await expect(page.getByText('Сохранён в настройках', { exact: true })).toBeVisible()
      expect(await page.content()).not.toContain(credential)
    }
    await importDeal(request, row, credential)
    expect((await page.request.post(`/admin/api/bitrixsettings/${row.config}/change/`, { form: { new_webhook_url: url } })).status()).toBe(403)
    await page.goto(`/admin/api/bitrixsettings/${row.config}/history/`)
    expect(await page.content()).not.toContain(credential)
    const audit = isolatedCommand('shell', ['--verbosity', '0', '-c', guard + `import json; from api.models import AuditEvent; print(json.dumps(list(AuditEvent.objects.filter(target_type='BitrixSettings', target_id=${row.config}).values('before_after'))))`])
    expect(audit).toContain('new_webhook_url')
    expect(audit).not.toContain(credential)
    await page.goto(`/admin/api/messageprocessingtrace/?q=${row.deal}`)
    await expect(page.locator('#result_list')).toContainText(`E2E CRM ${row.deal}`)
  } finally {
    isolatedCommand('shell', ['-c', guard + `from api.models import BitrixSettings; BitrixSettings.objects.filter(pk=${row.config}).delete()`])
  }
})

test('Bitrix Webhook editing requires integration and model permissions', async ({ page }) => {
  const row = fixture()
  const username = `crm-staff-${randomUUID()}`
  isolatedCommand('shell', ['-c', guard + `from django.contrib.auth.models import User, Permission; u=User.objects.create_user(username='${username}', password='isolated-staff-password', is_staff=True); u.user_permissions.add(*Permission.objects.filter(codename__in=['view_bitrixsettings', 'change_bitrixsettings']))`])
  try {
    await page.goto('/admin/login/?next=/admin/')
    await page.locator('[name=username]').fill(username)
    await page.locator('[name=password]').fill('isolated-staff-password')
    await page.locator('button[type=submit], input[type=submit]').click()
    await page.waitForURL('**/admin/')
    const csrf = await page.locator('[name=csrfmiddlewaretoken]').first().inputValue()
    expect((await page.goto(`/admin/api/bitrixsettings/${row.config}/change/`))?.status()).toBe(403)
    expect((await page.request.post(`/admin/api/bitrixsettings/${row.config}/change/`, { form: { csrfmiddlewaretoken: csrf, new_webhook_url: 'http://test-provider:9000/rest/1/e2e-denied/' } })).status()).toBe(403)
    isolatedCommand('shell', ['-c', guard + `from django.contrib.auth.models import User, Permission; u=User.objects.get(username='${username}'); u.user_permissions.add(Permission.objects.get(codename='change_whatsappconfig')); u.user_permissions.remove(Permission.objects.get(codename='change_bitrixsettings'))`])
    await page.goto(`/admin/api/bitrixsettings/${row.config}/change/`)
    await expect(page.getByLabel('Новый REST Webhook URL')).toHaveCount(0)
    expect((await page.request.post(`/admin/api/bitrixsettings/${row.config}/change/`, { form: { csrfmiddlewaretoken: csrf, new_webhook_url: 'http://test-provider:9000/rest/1/e2e-denied/' } })).status()).toBe(403)
  } finally {
    isolatedCommand('shell', ['-c', guard + `from django.contrib.auth.models import User; from api.models import BitrixSettings; BitrixSettings.objects.filter(pk=${row.config}).delete(); User.objects.filter(username='${username}').delete()`])
  }
})
