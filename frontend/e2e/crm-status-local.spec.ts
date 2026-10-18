import { readFileSync } from './helpers/files'
import { authenticateShell } from './helpers/session'
import { test, expect } from '@playwright/test'
import { existsSync, renameSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { AutonomousOverview, CrmDisabledReason } from '../src/types/autonomous'
import type { Directory } from '../src/types/platform'

const directory = process.env.MAZORY_E2E_DIR
if (!directory) throw new Error('Use the isolated Docker Compose E2E stack')
const session: { access: string; refresh: string; cookie_name: string } = JSON.parse(readFileSync(join(directory, 'crm-status-session.json'), 'utf8'))
const headers = { Authorization: `Bearer ${session.access}` }
const controlFile = join(directory, 'provider-state.json')
const proofFile = join(directory, 'crm-status-proof.json')
interface Proof {
  observed_at: string
  case: string
  ai_id: number
  bitrix_id: number
  settings: { ai_active: boolean; processing: boolean; writing: boolean; integration: boolean; webhook_configured: boolean; ai_updated_at: string; bitrix_updated_at: string }
  records: Record<string, number>
}
function proof(): Proof { return JSON.parse(readFileSync(proofFile, 'utf8')) }
function setCase(name: string) {
  const state: Record<string, unknown> = JSON.parse(readFileSync(controlFile, 'utf8'))
  state.crm_status_case = name
  const temporary = `${controlFile}.crm.tmp`
  writeFileSync(temporary, JSON.stringify(state))
  renameSync(temporary, controlFile)
}
function crmRequests(): string {
  const path = join(directory, 'crm-requests.jsonl')
  return existsSync(path) ? readFileSync(path, 'utf8') : ''
}

test('cabinet and admin distinguish integration and automatic WhatsApp writes using real settings', async ({ page, context, request }) => {
  test.setTimeout(120000)
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  await authenticateShell(context)
  await page.goto('/settings?section=bitrix')
  await expect(page.getByTestId('crm-settings-status')).toBeVisible()
  expect((await request.get('/api/autonomous/overview/')).status()).toBe(401)
  // Opening the workspace also starts its independent initial CRM catalog import.
  // Finish that real Q2 operation before measuring side effects of status reads.
  await expect.poll(async () => {
    const response = await request.get('/api/directory/', { headers })
    expect(response.status()).toBe(200)
    const value: Directory = await response.json()
    return value.crm_catalog?.some(row => row.state === 'succeeded')
  }).toBe(true)
  const cases: { name: string; reason: CrmDisabledReason | null; integration: boolean; writing: boolean; processing: boolean; aiActive: boolean; webhook: boolean }[] = [
    { name: 'write_paused', reason: 'autonomous_crm_disabled', integration: true, writing: false, processing: false, aiActive: true, webhook: true },
    { name: 'write_enabled', reason: null, integration: true, writing: true, processing: false, aiActive: true, webhook: true },
    { name: 'processing_enabled', reason: null, integration: true, writing: true, processing: true, aiActive: true, webhook: true },
    { name: 'integration_disabled', reason: 'integration_disabled', integration: false, writing: true, processing: false, aiActive: true, webhook: true },
    { name: 'webhook_missing', reason: 'webhook_missing', integration: true, writing: true, processing: false, aiActive: true, webhook: false },
    { name: 'ai_missing', reason: 'autonomous_crm_disabled', integration: true, writing: true, processing: false, aiActive: false, webhook: true },
  ]
  const reasons: Record<CrmDisabledReason, string> = {
    integration_disabled: 'Интеграция Bitrix выключена.',
    webhook_missing: 'Не задан Webhook для подключения к Bitrix.',
    autonomous_crm_disabled: 'Автоматическая запись результатов WhatsApp в CRM выключена в настройках ИИ.',
  }
  try {
    for (const item of cases) {
      setCase(item.name)
      await expect.poll(() => existsSync(proofFile) && proof().case).toBe(item.name)
      const before = proof()
      expect(before.settings).toMatchObject({ ai_active: item.aiActive, processing: item.processing, writing: item.writing, integration: item.integration, webhook_configured: item.webhook })
      const requestsBefore = crmRequests()
      const response = await request.get('/api/autonomous/overview/', { headers })
      expect(response.status()).toBe(200)
      const overview: AutonomousOverview = await response.json()
      expect(overview.enabled).toBe(item.aiActive && item.processing)
      expect(overview.crm_enabled).toBe(item.aiActive && item.writing)
      expect(overview.crm_status).toEqual({ integration_enabled: item.integration, webhook_configured: item.webhook, autonomous_write_allowed: item.aiActive && item.writing, effective_autonomous_write_enabled: item.reason === null, disabled_reason: item.reason })
      await page.goto('/analytics?view=projects')
      await page.getByRole('button', { name: 'Отчеты по WhatsApp', exact: true }).click()
      const modes = page.getByRole('region', { name: 'Режимы обработки и интеграции' })
      await expect(modes.getByText(`Автоматическая обработка WhatsApp ${overview.enabled ? 'включена' : 'выключена'}`, { exact: true })).toBeVisible()
      await expect(modes.getByText(`Интеграция Bitrix ${item.integration ? 'включена' : 'выключена'}`, { exact: true })).toBeVisible()
      await expect(modes.getByText(`Автоматическая запись из WhatsApp в CRM ${item.reason === null ? 'включена' : 'выключена'}`, { exact: true })).toBeVisible()
      if (item.reason) await expect(modes.getByText(reasons[item.reason], { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Обновить', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Обновить', exact: true })).toBeEnabled()
      if (item.name === 'write_paused') await page.screenshot({ path: join(directory, 'crm-status-cabinet.png'), fullPage: true })
      await page.goto('/settings?section=bitrix')
      const row = page.getByTestId('crm-settings-status').first()
      await expect(row.getByText(`Интеграция: ${item.integration ? 'включена' : 'выключена'}`, { exact: true })).toBeVisible()
      await expect(row.getByText(`Автоматическая запись WhatsApp: ${item.reason === null ? 'включена' : 'выключена'}`, { exact: true })).toBeVisible()
      await expect(row.getByText('Создание сделок: вкл.', { exact: true })).toBeVisible()
      if (item.reason) await expect(row.getByText(reasons[item.reason], { exact: true })).toBeVisible()
      const link = row.getByRole('link', { name: 'Настройки записи WhatsApp →', exact: true })
      if (item.aiActive) await expect(link).toHaveAttribute('href', '/settings?section=ai')
      else await expect(link).toHaveCount(0)
      if (item.name === 'write_paused') await page.screenshot({ path: join(directory, 'crm-status-admin.png'), fullPage: true })
      await expect.poll(() => proof().observed_at).not.toBe(before.observed_at)
      expect(proof().settings).toEqual(before.settings)
      expect(proof().records).toEqual(before.records)
      expect(crmRequests()).toBe(requestsBefore)
    }
    setCase('write_paused')
    await expect.poll(() => proof().case).toBe('write_paused')
    const integrator = JSON.parse(readFileSync(join(directory, 'messenger-session.json'),'utf8')) as {integrator:{access:string};viewer:{access:string}}
    const viewHeaders = {Authorization:`Bearer ${integrator.viewer.access}`}
    const readOnly = await request.get('/api/settings/bitrix/', {headers:viewHeaders})
    expect(readOnly.status()).toBe(200)
    const viewData = await readOnly.json() as {items:{ai_settings_href:string|null}[]}
    expect(viewData.items[0]!.ai_settings_href).toBeNull()
    expect((await request.patch('/api/settings/bitrix/',{headers:viewHeaders,data:{is_active:true}})).status()).toBe(403)
    expect((await request.get('/api/settings/ai/', {headers:{Authorization:`Bearer ${integrator.integrator.access}`}})).status()).toBe(403)

  } finally {
    setCase('restore')
    await expect.poll(() => proof().case).toBe('restore')
    const state: Record<string, unknown> = JSON.parse(readFileSync(controlFile, 'utf8'))
    delete state.crm_status_case
    writeFileSync(`${controlFile}.crm.tmp`, JSON.stringify(state))
    renameSync(`${controlFile}.crm.tmp`, controlFile)
  }
})
