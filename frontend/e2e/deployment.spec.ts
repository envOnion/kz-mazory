import { test, expect } from '@playwright/test'

const expectSafeRelativeRedirect = (status: number, location: string | undefined, expectedPath: string) => {
  expect([301, 302, 307, 308]).toContain(status)
  expect(location).toBe(expectedPath)
  expect(location).not.toMatch(/(?:https?:)?\/\//)
  expect(location).not.toContain(':8080')
  expect(location).not.toContain('backend')
}

test('Deployment: gateway serves Vue, Django and protected routes on one origin', async ({ page, request, baseURL }) => {
  const origin = process.env.MAZORY_SMOKE_URL || baseURL!
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text())
  })
  await page.goto(origin, { waitUntil: 'networkidle' })
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  expect(await page.locator('link[href*="fonts.googleapis.com"], link[href*="fonts.gstatic.com"]').count()).toBe(0)
  expect(await page.locator('script[src="/@vite/client"]').count()).toBe(0)

  const frontendAsset = await page.locator('script[type="module"][src^="/assets/"]').first().getAttribute('src')
  if (!frontendAsset) throw new Error('Production page did not reference a built frontend asset')
  const frontendAssetResponse = await request.get(`${origin}${frontendAsset}`)
  expect(frontendAssetResponse.status()).toBe(200)
  expect(frontendAssetResponse.headers()['content-type']).toContain('javascript')

  expect(errors).toEqual([])
  const ready = await request.get(`${origin}/api/health/ready/`)
  expect(ready.status()).toBe(200)
  expect(await ready.json()).toEqual({ status: 'ready' })
  expect((await request.get(`${origin}/api/projects/`)).status()).toBe(401)

  const adminRedirect = await request.get(`${origin}/admin`, { maxRedirects: 0 })
  expectSafeRelativeRedirect(adminRedirect.status(), adminRedirect.headers().location, '/admin/')
  const apiRedirect = await request.get(`${origin}/api`, { maxRedirects: 0 })
  expectSafeRelativeRedirect(apiRedirect.status(), apiRedirect.headers().location, '/api/')

  const adminPage = await request.get(`${origin}/admin/`)
  expect(adminPage.status()).toBe(200)
  expect(adminPage.url()).toContain('/admin/login/')
  const adminStatic = await request.get(`${origin}/static/admin/css/base.css`)
  expect(adminStatic.status()).toBe(200)
  expect(adminStatic.headers()['content-type']).toContain('text/css')

  expect((await request.get(`${origin}/media/probe.txt`)).status()).toBe(404)
  expect((await request.get(`${origin}/private-media/probe.txt`)).status()).toBe(404)
})
