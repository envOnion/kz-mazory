import { test, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { adminLogin, isolatedCommand } from './auth-helper'

for (const theme of ['light', 'dark'] as const) {
  test(`Admin lists stay readable in ${theme} theme at desktop and mobile widths`, async ({ page }, testInfo) => {
    const token = `layout-${randomUUID()}`
    const fixture: { trace: number; project: number; config: number } = JSON.parse(isolatedCommand('shell', ['--verbosity', '0', '-c', `
import json
from django.conf import settings
from api.models import MessageProcessingTrace, Project, BitrixSettings
assert settings.INTEGRATION_TEST_MODE and settings.DATABASES['default']['NAME'].endswith('_e2e')
p = Project.objects.create(name='Алматы — длинное название объекта ' + 'БизнесЦентр' * 12, contract_amount='233000000')
t = MessageProcessingTrace.objects.create(project=p, whatsapp_message_id='${token}', whatsapp_sender_name='Вячеслав Медведев — команда поддержки продаж', whatsapp_sender_phone='77000000000@s.whatsapp.net', whatsapp_content='Коллеги, добрый день! Подскажите по объекту: ' + 'ДлинноеСлово' * 70 + '<script>window.__layout_xss=1</script>', earlier_messages_count=3, bitrix_matched_deal_id='1498', pipeline_action='updated_deal', status='warning')
c = BitrixSettings.objects.create(name='${token} Интеграция с длинным названием', is_active=False)
print(json.dumps({'trace': t.id, 'project': p.id, 'config': c.id}))
`]))
    try {
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await page.addInitScript(value => localStorage.setItem('adminTheme', JSON.stringify(value)), theme)
      await adminLogin(page)
      await page.goto(`/admin/api/messageprocessingtrace/?q=${token}`)
      await expect(page.locator('#result_list .data-row')).toHaveCount(1)
      await expect(page.locator('#result_list thead th')).toHaveCount(5)
      await expect(page.locator('#result_list')).toContainText('Контекст: 3')
      await expect(page.locator('#result_list')).toContainText('Требует внимания')
      await expect(page.locator('.mazory-message-preview')).toHaveCSS('color', theme === 'dark' ? 'rgb(241, 245, 249)' : 'rgb(15, 23, 42)')
      for (const width of [1920, 1366, 1024, 390]) {
        await page.setViewportSize({ width, height: 1000 })
        if (width < 1280) await expect(page.locator('#nav-sidebar')).toBeHidden()
        await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
        expect(await page.locator('#result_list').evaluate(el => el.parentElement!.scrollWidth - el.parentElement!.clientWidth)).toBeLessThanOrEqual(1)
        for (const cell of await page.locator('#result_list .data-row > th, #result_list .data-row > td').all()) {
          expect(await cell.evaluate(el => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(1)
        }
        for (const badge of await page.locator('#result_list .mazory-admin-badge').all()) {
          expect(await badge.evaluate(el => el.getClientRects().length)).toBe(1)
          expect(await badge.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1)
          await expect(badge).toBeVisible()
        }
        await page.screenshot({ path: testInfo.outputPath(`trace-${theme}-${width}.png`), fullPage: true })
      }
      expect(await page.evaluate(() => '__layout_xss' in window)).toBe(false)
      await page.getByRole('link', { name: /Открыть трассировку/ }).click()
      await expect(page).toHaveURL(new RegExp(`/messageprocessingtrace/${fixture.trace}/change/`))
      await expect(page.getByText('1. Входные данные WhatsApp', { exact: false }).first()).toBeVisible()
      await page.goto(`/admin/api/messageprocessingtrace/?q=${token}&status__exact=success`)
      await expect(page.locator('#result_list .data-row')).toHaveCount(0)
      await page.goto(`/admin/api/bitrixsettings/?q=${token}`)
      const row = page.locator('#result_list .data-row').filter({ hasText: token })
      await expect(row).toBeVisible()
      for (const width of [1920, 1366, 390]) {
        await page.setViewportSize({ width, height: 1000 })
        if (width < 1280) await expect(page.locator('#nav-sidebar')).toBeHidden()
        await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
        expect(await page.locator('#result_list').evaluate(el => el.parentElement!.scrollWidth - el.parentElement!.clientWidth)).toBeLessThanOrEqual(1)
        for (const cell of await page.locator('#result_list .data-row > th, #result_list .data-row > td').all()) {
          expect(await cell.evaluate(el => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(1)
        }
        await page.screenshot({ path: testInfo.outputPath(`bitrix-${theme}-${width}.png`), fullPage: true })
      }
      await row.getByRole('link', { name: 'Изменить Webhook →' }).click()
      await expect(page.getByLabel('Новый REST Webhook URL')).toBeVisible()
      expect(errors).toEqual([])
    } finally {
      isolatedCommand('shell', ['-c', `from api.models import MessageProcessingTrace, Project, BitrixSettings; MessageProcessingTrace.objects.filter(pk=${fixture.trace}).delete(); Project.objects.filter(pk=${fixture.project}).delete(); BitrixSettings.objects.filter(pk=${fixture.config}).delete()`])
    }
  })
}
