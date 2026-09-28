import { test, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { adminLogin, isolatedCommand } from './auth-helper'

for (const theme of ['light', 'dark'] as const) {
  test(`WAHA select follows the ${theme} theme and remains usable on narrow screens`, async ({ page }, testInfo) => {
    const name = `Команда поддержки продаж — длинное название конфигурации ${randomUUID()}`
    const id: number = JSON.parse(isolatedCommand('shell', ['-c', `
from django.conf import settings
from api.models import WhatsAppConfig
assert settings.INTEGRATION_TEST_MODE and settings.DATABASES['default']['NAME'].endswith('_e2e')
print(WhatsAppConfig.objects.create(name=${JSON.stringify(name)}, session_name='default').id)
`]).split('\n').at(-1)!)
    try {
      await page.addInitScript(value => localStorage.setItem('adminTheme', JSON.stringify(value)), theme)
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await adminLogin(page)
      await page.goto('/admin/waha-dashboard/')
      const select = page.getByRole('combobox', { name: 'Конфигурация', exact: true })
      const show = page.getByRole('button', { name: 'Показать', exact: true })
      await expect(select).toHaveCSS('appearance', 'none')
      await expect(select).toHaveCSS('background-color', theme === 'dark' ? 'rgb(30, 41, 59)' : 'rgb(255, 255, 255)')
      await expect(select).toHaveCSS('color', theme === 'dark' ? 'rgb(248, 250, 252)' : 'rgb(15, 23, 42)')
      const selectBox = (await select.boundingBox())!
      const buttonBox = (await show.boundingBox())!
      expect(selectBox.height).toBeGreaterThanOrEqual(44)
      expect(Math.abs(selectBox.height - buttonBox.height)).toBeLessThanOrEqual(2)
      await select.focus()
      await page.keyboard.press('Tab')
      await page.keyboard.press('Shift+Tab')
      await expect(select).toBeFocused()
      await expect(select).toHaveCSS('outline-width', '2px')
      await select.selectOption({ label: `${name} — default` })
      await show.click()
      await expect(page).toHaveURL(`/admin/waha-dashboard/?config_id=${id}`)
      await expect(select.locator('option:checked')).toHaveText(`${name} — default`)
      await page.screenshot({ path: testInfo.outputPath(`select-${theme}-desktop.png`), fullPage: true })
      await page.setViewportSize({ width: 390, height: 844 })
      await expect(select).toBeVisible()
      const mobileBox = (await select.boundingBox())!
      expect(mobileBox.x + mobileBox.width).toBeLessThanOrEqual(390)
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
      await page.screenshot({ path: testInfo.outputPath(`select-${theme}-mobile.png`), fullPage: true })
      expect(errors).toEqual([])
    } finally {
      isolatedCommand('shell', ['-c', `from api.models import WhatsAppConfig; WhatsAppConfig.objects.filter(pk=${id}).delete()`])
    }
  })
}
