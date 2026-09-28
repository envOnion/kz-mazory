import { test, expect } from '@playwright/test'

test('Deployment: login renders, API is ready and private data stays protected', async ({ page, request, baseURL }) => {
  const origin = process.env.MAZORY_SMOKE_URL || baseURL!
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text())
  })
  await page.goto(origin, { waitUntil: 'networkidle' })
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  expect(await page.locator('link[href*="fonts.googleapis.com"], link[href*="fonts.gstatic.com"]').count()).toBe(0)
  expect(errors).toEqual([])
  const ready = await request.get(`${origin}/api/health/ready/`)
  expect(ready.status()).toBe(200)
  expect(await ready.json()).toEqual({ status: 'ready' })
  expect((await request.get(`${origin}/api/projects/`)).status()).toBe(401)
  if (process.env.MAZORY_SMOKE_URL) {
    expect(await page.locator('script[src="/@vite/client"]').count()).toBe(0)
    expect((await request.get(`${origin}/media/probe.txt`)).status()).toBe(404)
  }
})
