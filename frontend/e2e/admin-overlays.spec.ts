import { test, expect } from '@playwright/test'
import { adminLogin } from './auth-helper'

const modal = '[x-show="openModal"]'
const shortcuts = '[x-show="shortcutsOpen"]'

async function assertOverlaysClosed(page: import('@playwright/test').Page) {
  await expect(page.locator(modal)).toBeHidden()
  await expect(page.locator(shortcuts)).toBeHidden()
  await expect(page.locator('#modal-overlay')).toBeHidden()
}

test('Admin login fields are accessible without blocking overlays or CSP errors', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text())
  })
  const origin = process.env.MAZORY_ADMIN_SMOKE_URL || ''
  const response = await page.goto(`${origin}/admin/login/?next=/admin/`)
  expect(response?.headers()['content-security-policy']).toContain("'unsafe-eval'")
  await assertOverlaysClosed(page)
  const username = page.locator('[name="username"]')
  await username.fill('display-check')
  await expect(username).toHaveValue('display-check')
  await page.locator('[name="password"]').fill('not-submitted')
  expect(errors).toEqual([])
})

test('Authenticated Admin hides overlays and dismisses shortcuts with Escape or outside click', async ({ page }) => {
  test.skip(Boolean(process.env.MAZORY_ADMIN_SMOKE_URL), 'Production smoke never signs in as a real user')
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text())
  })
  await adminLogin(page)
  await page.goto('/admin/api/project/')
  await assertOverlaysClosed(page)
  await page.keyboard.press('Shift+?')
  await expect(page.locator(shortcuts)).toBeVisible()
  await expect(page.locator('#modal-overlay')).toBeVisible()
  await page.keyboard.press('Escape')
  await assertOverlaysClosed(page)
  await page.keyboard.press('Shift+?')
  await expect(page.locator(shortcuts)).toBeVisible()
  await page.locator(shortcuts).click({ position: { x: 5, y: 5 } })
  await assertOverlaysClosed(page)
  await expect(page.locator('#content')).toBeVisible()
  expect(errors).toEqual([])
})
