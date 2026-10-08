import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Profile } from '../src/types/platform'

const directory = process.env.MAZORY_E2E_DIR
if (!directory) throw new Error('Run the isolated Docker Compose E2E stack')
const recreated = process.env.MAZORY_AVATAR_RECREATED === '1'
type AvatarSession = { access: string; refresh: string; cookie_name: string; profile_id: number }
const session: AvatarSession = JSON.parse(readFileSync(join(directory, 'avatar-session-200.json'), 'utf8'))

test('profile avatar upload, validation, replacement and deletion use the real API and storage', async ({ page, context, request }) => {
  test.skip(recreated)
  test.setTimeout(120000)
  let access = session.access
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  context.on('response', async response => {
    if (response.url().includes('/api/auth/refresh/') && response.ok()) access = (await response.json()).access
  })
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  const headers = () => ({ Authorization: `Bearer ${access}` })
  const profile = async (): Promise<Profile> => {
    const response = await request.get('/api/profile/', { headers: headers() })
    expect(response.status()).toBe(200)
    return response.json()
  }
  const open = async () => {
    await page.getByTestId('nav-profile').click()
    await expect(page.getByLabel('ФИО', { exact: true })).toHaveValue('Антон E2E')
    await expect(page.getByRole('button', { name: 'Сохранить изменения', exact: true })).toBeEnabled()
  }
  const input = page.getByLabel('Выбрать фото профиля', { exact: true })
  const preview = page.getByRole('img', { name: 'Предпросмотр фото профиля', exact: true })
  const save = async (): Promise<Profile> => {
    const response = page.waitForResponse(response => response.url().endsWith('/api/profile/') && response.request().method() === 'PUT')
    await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
    const result = await response
    expect(result.status()).toBe(200)
    await expect(page.getByRole('status').filter({ hasText: 'Изменения сохранены' })).toBeVisible()
    return result.json()
  }
  const choose = async (name: string) => {
    await input.setInputFiles(join(directory, name))
    await expect(page.getByText('Новое фото появится после сохранения.', { exact: true })).toBeVisible()
    await expect(preview).toHaveAttribute('src', /^data:image\//)
  }
  const checkImage = async (url: string) => {
    expect(url).toMatch(/^\/avatars\/[0-9a-f]{32}\.webp$/)
    await expect(page.getByTestId('profile-avatar')).toHaveAttribute('src', url)
    await expect(page.getByTestId('header-avatar')).toHaveAttribute('src', url)
    await expect.poll(() => page.getByTestId('profile-avatar').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBe(512)
    const response = await request.get(url)
    expect(response.status()).toBe(200)
    expect(response.headers()['content-type']).toBe('image/webp')
    expect(response.headers()['x-content-type-options']).toBe('nosniff')
    return response.body()
  }

  await page.goto('/')
  await open()
  await expect(page.getByRole('img', { name: 'Фото профиля не добавлено', exact: true })).toHaveCount(2)
  await choose('avatar.png')
  expect((await profile()).avatar_url).toBe('')
  await expect(page.getByTestId('header-avatar')).toHaveCount(0)
  await page.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
  await expect(preview).toHaveCount(0)
  await choose('avatar.png')
  await page.getByLabel('Email', { exact: true }).fill('avatar@example.test')
  const first = await save()
  expect(first.id).toBe(session.profile_id)
  expect(first.email).toBe('avatar@example.test')
  expect(first.full_name).toBe('Антон E2E')
  await checkImage(first.avatar_url)
  expect((await profile()).avatar_url).toBe(first.avatar_url)
  await page.reload()
  await expect(page.getByTestId('header-avatar')).toHaveAttribute('src', first.avatar_url)
  await open()

  await input.setInputFiles({ name: 'bad.svg', mimeType: 'image/svg+xml', buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>') })
  await expect(page.getByRole('alert')).toContainText('JPEG, PNG или WebP')
  await expect(page.getByRole('button', { name: 'Сохранить изменения', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
  await input.setInputFiles({ name: 'large.png', mimeType: 'image/png', buffer: Buffer.alloc(5 * 1024 * 1024 + 1) })
  await expect(page.getByRole('alert')).toContainText('5 МиБ')
  await page.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
  await input.setInputFiles({ name: 'broken.png', mimeType: 'image/png', buffer: Buffer.from('not an image') })
  await expect(page.getByRole('alert')).toContainText('Не удалось прочитать фото')
  await page.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
  await input.setInputFiles(join(directory, 'avatar-too-many-pixels.png'))
  await expect(page.getByRole('alert')).toContainText('20 миллионов пикселей')
  await page.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
  expect((await profile()).avatar_url).toBe(first.avatar_url)

  // A GIF disguised as PNG passes browser decoding; the actual server rejects its content.
  await input.setInputFiles({ name: 'disguised.png', mimeType: 'image/png', buffer: readFileSync(join(directory, 'avatar.gif')) })
  await expect(page.getByText('Новое фото появится после сохранения.', { exact: true })).toBeVisible()
  const invalidResponse = page.waitForResponse(response => response.url().endsWith('/api/profile/') && response.request().method() === 'PUT')
  await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
  expect((await invalidResponse).status()).toBe(400)
  await expect(page.getByRole('status').filter({ hasText: 'Фото профиля' })).toContainText('JPEG, PNG или WebP')
  await expect(preview).toHaveAttribute('src', /^data:image\//)
  await expect(page.getByLabel('Email', { exact: true })).toHaveValue('avatar@example.test')
  await page.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
  await checkImage(first.avatar_url)

  const invalidUploads = [
    { name: 'broken.png', mimeType: 'image/png', buffer: Buffer.from('invalid') },
    { name: 'bad.svg', mimeType: 'image/png', buffer: Buffer.from('<svg/>') },
    { name: 'large.png', mimeType: 'image/png', buffer: Buffer.alloc(5 * 1024 * 1024 + 1) },
    ...['avatar.gif', 'avatar-animated.png', 'avatar-too-many-pixels.png'].map(name => ({ name, mimeType: 'image/png', buffer: readFileSync(join(directory, name)) })),
  ]
  for (const avatar of invalidUploads) {
    const response = await request.put('/api/profile/', { headers: headers(), multipart: { avatar, full_name: 'Не сохранять' } })
    expect(response.status()).toBe(400)
    const unchanged = await profile()
    expect(unchanged.avatar_url).toBe(first.avatar_url)
    expect(unchanged.full_name).toBe('Антон E2E')
  }
  expect((await request.put('/api/profile/', { data: { remove_avatar: true } })).status()).toBe(401)
  for (const data of [{ avatar_url: first.avatar_url }, { id: session.profile_id }, { user: session.profile_id }]) {
    expect((await request.put('/api/profile/', { headers: headers(), data })).status()).toBe(400)
  }
  const peer: AvatarSession = JSON.parse(readFileSync(join(directory, 'avatar-session-201.json'), 'utf8'))
  expect((await request.put('/api/profile/', { headers: { Authorization: `Bearer ${peer.access}` }, data: { id: session.profile_id, full_name: 'Другой' } })).status()).toBe(400)
  expect((await profile()).avatar_url).toBe(first.avatar_url)
  expect((await request.put('/api/profile/', { headers: headers(), multipart: { avatar: { name: 'avatar.png', mimeType: 'image/png', buffer: readFileSync(join(directory, 'avatar.png')) }, remove_avatar: 'true' } })).status()).toBe(400)
  for (const path of ['/private-media/avatar.png', '/media/avatar.png', '/avatars/', '/avatars/not-an-avatar.webp']) expect((await request.get(path)).status()).toBe(404)

  expect(readFileSync(join(directory, 'avatar-large-valid.png')).length).toBeGreaterThan(1024 * 1024)
  await choose('avatar-large-valid.png')
  const large = await save()
  await checkImage(large.avatar_url)
  expect((await request.get(first.avatar_url)).status()).toBe(404)
  await choose('avatar.jpg')
  const second = await save()
  expect(second.avatar_url).not.toBe(first.avatar_url)
  const normalized = await checkImage(second.avatar_url)
  expect(normalized.includes(Buffer.from('private-avatar-metadata'))).toBe(false)
  writeFileSync(join(directory, 'avatar-normalized.webp'), normalized)
  expect((await request.get(first.avatar_url)).status()).toBe(404)
  expect((await request.get(large.avatar_url)).status()).toBe(404)
  await choose('avatar.webp')
  const third = await save()
  await checkImage(third.avatar_url)
  expect((await request.get(second.avatar_url)).status()).toBe(404)
  await page.getByRole('button', { name: 'Удалить фото', exact: true }).click()
  await expect(page.getByText('Фото будет удалено после сохранения.', { exact: true })).toBeVisible()
  expect((await profile()).avatar_url).toBe(third.avatar_url)
  await page.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
  await expect(preview).toHaveAttribute('src', third.avatar_url)
  await page.getByRole('button', { name: 'Удалить фото', exact: true }).click()
  const removed = await save()
  expect(removed.avatar_url).toBe('')
  await expect(page.getByTestId('header-avatar')).toHaveCount(0)
  await expect(page.getByTestId('profile-avatar')).toHaveCount(0)
  expect((await request.get(third.avatar_url)).status()).toBe(404)

  await choose('avatar.png')
  const final = await save()
  await checkImage(final.avatar_url)
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.screenshot({ path: join(directory, 'playwright/profile-avatar-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: join(directory, 'playwright/profile-avatar-mobile.png'), fullPage: true })
  expect(errors).toEqual([])
  const refresh = (await context.cookies()).find(cookie => cookie.name === session.cookie_name)!.value
  writeFileSync(join(directory, 'avatar-persistence.json'), JSON.stringify({ ...session, access, refresh, avatar_url: final.avatar_url }))
})

test('profile avatar persists after backend and nginx containers are recreated', async ({ page, context, request }) => {
  test.skip(!recreated)
  const saved: AvatarSession & { avatar_url: string } = JSON.parse(readFileSync(join(directory, 'avatar-persistence.json'), 'utf8'))
  await context.addCookies([{ name: saved.cookie_name, value: saved.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  await page.goto('/')
  await expect(page.getByTestId('header-avatar')).toHaveAttribute('src', saved.avatar_url)
  await page.getByTestId('nav-profile').click()
  await expect(page.getByTestId('profile-avatar')).toHaveAttribute('src', saved.avatar_url)
  await expect.poll(() => page.getByTestId('profile-avatar').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBe(512)
  expect((await request.get(saved.avatar_url)).status()).toBe(200)
  await page.screenshot({ path: join(directory, 'playwright/profile-avatar-after-recreate.png'), fullPage: true })
})
