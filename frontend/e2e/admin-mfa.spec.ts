import { test as base, expect } from '@playwright/test'
import type { Page } from '@playwright/test'
import { createHmac, randomUUID } from 'node:crypto'
import { isolatedCommand } from './auth-helper'

const security = '/admin/security/'
const challenge = '/admin/security/mfa/'
const password = 'test-only-mfa-password'
const test = base.extend<{ account: string }>({
  account: async ({}, use) => {
    const username = `e2e-mfa-${randomUUID()}`
    isolatedCommand('shell', ['-c', `from django.conf import settings; from django.contrib.auth.models import User; assert settings.INTEGRATION_TEST_MODE; assert str(settings.DATABASES['default']['NAME']).endswith('_e2e'); User.objects.create_user('${username}', password='${password}', is_staff=True)`])
    try { await use(username) } finally {
      isolatedCommand('shell', ['-c', `from django.conf import settings; from django.contrib.auth.models import User; assert settings.INTEGRATION_TEST_MODE; assert str(settings.DATABASES['default']['NAME']).endswith('_e2e'); User.objects.filter(username='${username}').delete()`])
    }
  },
})

function code(secret: string, counter = Math.floor(Date.now() / 30000)): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  const bits = [...secret].map(char => alphabet.indexOf(char).toString(2).padStart(5, '0')).join('')
  const bytes = Buffer.from(bits.match(/.{8}/g)!.map(byte => parseInt(byte, 2)))
  const time = Buffer.alloc(8)
  time.writeBigUInt64BE(BigInt(counter))
  const digest = createHmac('sha1', bytes).update(time).digest()
  const offset = digest[digest.length - 1]! & 15
  return ((digest.readUInt32BE(offset) & 0x7fffffff) % 1000000).toString().padStart(6, '0')
}

function wrongCode(secret: string): string {
  const now = Math.floor(Date.now() / 30000)
  const accepted = [-1, 0, 1].map(offset => code(secret, now + offset))
  return ['000000', '111111', '222222', '333333'].find(value => !accepted.includes(value))!
}

async function signIn(page: Page, username: string, target = '/admin/') {
  await page.goto(`/admin/login/?next=${encodeURIComponent(target)}`)
  await page.locator('[name=username]').fill(username)
  await page.locator('[name=password]').fill(password)
  await page.locator('button[type=submit], input[type=submit]').click()
  await expect(page).not.toHaveURL(/\/admin\/login\//)
}

async function start(page: Page): Promise<string> {
  await page.goto(security)
  await page.getByRole('button', { name: 'Включить MFA', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Подключение MFA', exact: true })).toBeVisible()
  const qr = page.getByRole('img', { name: 'QR-код для подключения MFA' })
  await expect(qr).toBeVisible()
  expect(await qr.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true)
  expect(await qr.getAttribute('src')).toMatch(/^data:image\/svg\+xml;base64,/)
  return (await page.getByTestId('mfa-secret').innerText()).trim()
}

async function enable(page: Page) {
  const secret = await start(page)
  // Use the real server clock and avoid submitting the previous-window code
  // at the exact boundary where it would correctly expire in transit.
  let serverTime = 0
  await expect.poll(async () => {
    const response = await page.request.get('/api/health/ready/')
    serverTime = Date.parse(response.headers().date!)
    return serverTime % 30000
  }, { intervals: [250], timeout: 7000 }).toBeLessThan(25000)
  const counter = Math.floor(serverTime / 30000) - 1
  await page.getByLabel('Шестизначный код').fill(code(secret, counter))
  await page.getByRole('button', { name: 'Подтвердить и включить MFA' }).click()
  await expect(page.getByRole('heading', { name: 'MFA включена', exact: true })).toBeVisible()
  await expect(page.getByTestId('mfa-secret')).toHaveCount(0)
  await expect(page.getByRole('img', { name: 'QR-код для подключения MFA' })).toHaveCount(0)
  return { secret, counter }
}

async function csrf(page: Page): Promise<string> {
  return page.locator('input[name=csrfmiddlewaretoken]').first().inputValue()
}

async function assertGate(page: Page, target = security) {
  await page.goto(target)
  await expect(page).toHaveURL(/\/admin\/security\/mfa\//)
  await expect(page.getByRole('button', { name: 'Подтвердить вход' })).toBeVisible()
  await expect(page.getByTestId('mfa-secret')).toHaveCount(0)
}

test('MFA is optional; GET never enrolls; cancellation and pending-session isolation work', async ({ page, browser, account }, testInfo) => {
  await signIn(page, account)
  await expect(page).toHaveURL(/\/admin\/$/)
  const settingsLink = page.locator('#nav-sidebar a[href="/admin/security/"]')
  if (!await settingsLink.isVisible()) await page.getByText('dock_to_right', { exact: true }).click()
  await settingsLink.click()
  await expect(page.getByRole('heading', { name: 'MFA выключена' })).toBeVisible()
  const response = await page.goto(`${security}?action=start`)
  expect(response?.headers()['cache-control']).toContain('no-store')
  await expect(page.getByTestId('mfa-secret')).toHaveCount(0)
  await page.goto(challenge)
  await expect(page).toHaveURL(/\/admin\/$/)
  const secret = await start(page)
  await page.screenshot({ path: testInfo.outputPath('mfa-setup.png'), fullPage: true })
  await page.getByRole('img', { name: 'QR-код для подключения MFA' }).screenshot({ path: testInfo.outputPath('mfa-qr.png') })
  const other = await browser.newContext({ baseURL: 'http://localhost:5173' })
  try {
    const tab = await other.newPage()
    await signIn(tab, account)
    await expect(tab).toHaveURL(/\/admin\/$/)
    await tab.goto(security)
    await expect(tab.getByTestId('mfa-secret')).toHaveCount(0)
    await expect(tab.getByRole('heading', { name: 'MFA выключена' })).toBeVisible()
    await page.getByLabel('Шестизначный код').fill(wrongCode(secret))
    await page.getByRole('button', { name: 'Подтвердить и включить MFA' }).click()
    await expect(page.getByRole('alert')).toContainText('Неверный')
    await page.getByRole('button', { name: 'Отменить подключение' }).click()
    await expect(page.getByRole('heading', { name: 'MFA выключена' })).toBeVisible()
    expect(await start(page)).not.toBe(secret)
    await page.getByRole('button', { name: 'Отменить подключение' }).click()
  } finally { await other.close() }
})

test('MFA full lifecycle: enroll, require code, reject replay, return to destination, disable and invalidate old sessions', async ({ page, browser, account }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  await signIn(page, account)
  const { secret, counter } = await enable(page)
  await expect(page.locator('#modal-overlay')).toBeHidden()
  await page.keyboard.press('Shift+?')
  await expect(page.locator('[x-show="shortcutsOpen"]')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.locator('#modal-overlay')).toBeHidden()
  const other = await browser.newContext({ baseURL: 'http://localhost:5173' })
  try {
    const tab = await other.newPage()
    const target = '/admin/password_change/?from=mfa'
    await signIn(tab, account, target)
    await expect(tab).toHaveURL(/\/admin\/security\/mfa\//)
    await tab.getByLabel('Шестизначный код').fill(code(secret, counter))
    await tab.getByRole('button', { name: 'Подтвердить вход' }).click()
    await expect(tab.getByRole('alert')).toContainText('Неверный')
    const loginCounter = Math.floor(Date.now() / 30000)
    await tab.getByLabel('Шестизначный код').fill(code(secret, loginCounter))
    await tab.getByRole('button', { name: 'Подтвердить вход' }).click()
    await expect(tab).toHaveURL(new RegExp('/admin/password_change/\\?from=mfa$'))
    await page.getByLabel('Текущий пароль').fill(password)
    await page.getByLabel('Шестизначный код').fill(code(secret, loginCounter))
    await page.getByRole('button', { name: 'Отключить MFA' }).click()
    await expect(page.getByRole('alert')).toContainText('Неверный')
    const disableCode = code(secret, Math.max(loginCounter + 1, Math.floor(Date.now() / 30000)))
    await page.getByLabel('Текущий пароль').fill('wrong-password')
    await page.getByLabel('Шестизначный код').fill(disableCode)
    await page.getByRole('button', { name: 'Отключить MFA' }).click()
    await expect(page.getByRole('alert')).toContainText('Неверный')
    await page.getByLabel('Текущий пароль').fill(password)
    await page.getByLabel('Шестизначный код').fill(disableCode)
    await page.getByRole('button', { name: 'Отключить MFA' }).click()
    await expect(page.getByRole('heading', { name: 'MFA выключена' })).toBeVisible()
    await page.getByRole('button', { name: 'Выйти из аккаунта' }).click()
    await signIn(page, account)
    await expect(page).toHaveURL(/\/admin\/$/)
    const renewed = await enable(page)
    expect(renewed.secret).not.toBe(secret)
    await assertGate(tab)
    await tab.getByLabel('Шестизначный код').fill(wrongCode(renewed.secret))
    await tab.getByRole('button', { name: 'Подтвердить вход' }).click()
    await expect(tab.getByRole('alert')).toContainText('Неверный')
    await tab.getByRole('button', { name: 'Выйти из аккаунта' }).click()
    await tab.goto('/admin/')
    await expect(tab).toHaveURL(/\/admin\/login\//)
    expect(errors).toEqual([])
  } finally { await other.close() }
})

test('MFA gate protects direct settings actions and never redirects outside Admin', async ({ page, browser, account }) => {
  await signIn(page, account)
  const { secret } = await enable(page)
  const other = await browser.newContext({ baseURL: 'http://localhost:5173' })
  try {
    const tab = await other.newPage()
    await signIn(tab, account)
    for (const target of [security, '/admin/auth/user/', '/admin/password_change/']) {
      await assertGate(tab, target)
      expect(new URL(tab.url()).searchParams.get('next')).toBe(target)
    }
    for (const action of ['start', 'cancel', 'confirm', 'disable']) {
      const response = await tab.request.post(security, { form: { action, csrfmiddlewaretoken: await csrf(tab), password, code: code(secret) }, maxRedirects: 0 })
      expect(response.status()).toBe(302)
      expect(response.headers().location).toContain(challenge)
    }
    for (const destination of ['https://example.com/admin/', '//example.com/admin/', '/admin/%2e%2e/api/', 'http://[', '/api/', challenge]) {
      await tab.goto(`${challenge}?next=${encodeURIComponent(destination)}`)
      await expect(tab.locator('input[name=next]')).toHaveValue('/admin/')
    }
    await tab.getByLabel('Шестизначный код').fill(code(secret))
    await tab.getByRole('button', { name: 'Подтвердить вход' }).click()
    await expect(tab).toHaveURL('http://localhost:5173/admin/')
    await page.goto(security)
    await expect(page.getByRole('heading', { name: 'MFA включена' })).toBeVisible()
  } finally { await other.close() }
})

test('MFA rejects CSRF and unauthenticated actions; active secret stays private to its owner', async ({ page, browser, account }) => {
  const anon = await browser.newContext({ baseURL: 'http://localhost:5173' })
  try {
    const tab = await anon.newPage()
    await tab.goto(security)
    await expect(tab).toHaveURL(/\/admin\/login\//)
    expect((await anon.request.post(security, { form: { action: 'start' } })).status()).toBe(403)
    await signIn(page, account)
    expect((await page.request.post(security, { form: { action: 'start' } })).status()).toBe(403)
    await page.goto(security)
    await expect(page.getByRole('heading', { name: 'MFA выключена' })).toBeVisible()
    const { secret } = await enable(page)
    expect((await page.request.post(security, { form: { action: 'disable', password, code: code(secret) } })).status()).toBe(403)
    await tab.goto('/admin/login/?next=/admin/security/')
    await tab.locator('[name=username]').fill('77000000001')
    await tab.locator('[name=password]').fill('test-only-admin-password')
    await tab.locator('button[type=submit], input[type=submit]').click()
    await expect(tab).toHaveURL(/\/admin\/security\/$/)
    await expect(tab.getByTestId('mfa-secret')).toHaveCount(0)
    expect(await tab.content()).not.toContain(secret)
    await expect(tab.getByRole('heading', { name: 'MFA выключена' })).toBeVisible()
  } finally { await anon.close() }
})

test('MFA throttles enrollment across setup restarts and browser sessions', async ({ page, browser, account }) => {
  await signIn(page, account)
  let secret = await start(page)
  for (let attempt = 0; attempt < 10; attempt++) {
    await page.getByLabel('Шестизначный код').fill(wrongCode(secret))
    await page.getByRole('button', { name: 'Подтвердить и включить MFA' }).click()
    await expect(page.getByRole('alert')).toContainText('Неверный')
  }
  await page.getByRole('button', { name: 'Отменить подключение' }).click()
  secret = await start(page)
  await page.getByLabel('Шестизначный код').fill(code(secret))
  await page.getByRole('button', { name: 'Подтвердить и включить MFA' }).click()
  await expect(page.getByRole('alert')).toContainText('Слишком много попыток')
  const other = await browser.newContext({ baseURL: 'http://localhost:5173' })
  try {
    const tab = await other.newPage()
    await signIn(tab, account)
    await expect(tab).toHaveURL(/\/admin\/$/)
    secret = await start(tab)
    await tab.getByLabel('Шестизначный код').fill(code(secret))
    await tab.getByRole('button', { name: 'Подтвердить и включить MFA' }).click()
    await expect(tab.getByRole('alert')).toContainText('Слишком много попыток')
  } finally { await other.close() }
})

test('MFA consumes a fresh code only once under concurrent HTTP verification', async ({ page, browser, account }) => {
  await signIn(page, account)
  const { secret } = await enable(page)
  const contexts = await Promise.all([browser.newContext({ baseURL: 'http://localhost:5173' }), browser.newContext({ baseURL: 'http://localhost:5173' })])
  try {
    const pages = await Promise.all(contexts.map(context => context.newPage()))
    await Promise.all(pages.map(tab => signIn(tab, account)))
    const tokens = await Promise.all(pages.map(tab => csrf(tab)))
    const freshCode = code(secret)
    const replies = await Promise.all(pages.map((tab, index) => tab.request.post(challenge, { form: { code: freshCode, csrfmiddlewaretoken: tokens[index]! }, maxRedirects: 0 })))
    expect(replies.map(response => response.status()).sort()).toEqual([200, 302])
    for (const [index, tab] of pages.entries()) {
      await tab.goto(security)
      if (replies[index]!.status() === 302) await expect(tab.getByRole('heading', { name: 'MFA включена' })).toBeVisible()
      else await expect(tab).toHaveURL(/\/admin\/security\/mfa\//)
    }
  } finally { await Promise.all(contexts.map(context => context.close())) }
})

test('MFA login limit is shared across sessions and also protects disabling', async ({ page, browser, account }) => {
  await signIn(page, account)
  const { secret } = await enable(page)
  const other = await browser.newContext({ baseURL: 'http://localhost:5173' })
  try {
    const tab = await other.newPage()
    await signIn(tab, account)
    // Enrollment has already used the first of ten attempts in this window.
    for (let attempt = 0; attempt < 9; attempt++) {
      await tab.getByLabel('Шестизначный код').fill(wrongCode(secret))
      await tab.getByRole('button', { name: 'Подтвердить вход' }).click()
      await expect(tab.getByRole('alert')).toContainText('Неверный')
    }
    await tab.getByLabel('Шестизначный код').fill(code(secret))
    await tab.getByRole('button', { name: 'Подтвердить вход' }).click()
    await expect(tab.getByRole('alert')).toContainText('Слишком много попыток')
    await page.getByLabel('Текущий пароль').fill(password)
    await page.getByLabel('Шестизначный код').fill(code(secret))
    await page.getByRole('button', { name: 'Отключить MFA' }).click()
    await expect(page.getByRole('alert')).toContainText('Слишком много попыток')
    await expect(page.getByRole('heading', { name: 'MFA включена' })).toBeVisible()
  } finally { await other.close() }
})
