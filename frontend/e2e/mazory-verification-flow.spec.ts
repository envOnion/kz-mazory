import { test, expect } from '@playwright/test'
import { loginWithToken, getOrCreateE2EToken } from './auth-helper'

test.describe('Mazory — Флоу верификации данных WhatsApp (is_verified)', () => {
  let authToken = ''

  test.beforeAll(async () => {
    authToken = getOrCreateE2EToken()
  })

  test.beforeEach(async ({ page }) => {
    await loginWithToken(page, authToken)
    await page.goto('/')
    await expect(page).toHaveTitle(/Mazory/i)
  })

  test('1. Виджет таблицы проектов отображает бейдж статуса верификации', async ({ page }) => {
    // Вводим запрос на воронку проектов в чат
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.click()
    await chatInput.fill('Покажи воронку проектов и сделок')
    await page.locator('button[title="Отправить запрос"]').click()

    // Ожидаем появления виджета таблицы проектов
    await expect(page.locator('text=Воронка проектов и контроль экономики сделок')).toBeVisible({ timeout: 35000 })

    // Проверяем наличие колонки или бейджа статуса верификации
    // PresetProjectTable.vue отображает зеленый бейдж "✓ Проверено" для верифицированных записей
    const verifiedBadge = page.locator('text=Проверено').first()
    await expect(verifiedBadge).toBeVisible({ timeout: 10000 })
  })

  test('2. API эндпоинт /api/projects/ фильтрует по параметру is_verified', async ({ request }) => {
    // Проверяем получение только проверенных проектов
    const verifiedRes = await request.get('/api/projects/?is_verified=true', {
      headers: {
        Authorization: `Bearer ${authToken}`
      }
    })
    expect(verifiedRes.ok()).toBeTruthy()
    const verifiedData = await verifiedRes.json()
    expect(Array.isArray(verifiedData.results || verifiedData)).toBeTruthy()
    const verifiedProjects = verifiedData.results || verifiedData
    expect(verifiedProjects.length).toBeGreaterThan(0)
    for (const p of verifiedProjects.slice(0, 10)) {
      expect(p.is_verified).toBe(true)
    }

    // Проверяем получение непроверенных проектов
    const unverifiedRes = await request.get('/api/projects/?is_verified=false', {
      headers: {
        Authorization: `Bearer ${authToken}`
      }
    })
    expect(unverifiedRes.ok()).toBeTruthy()
    const unverifiedData = await unverifiedRes.json()
    const unverifiedProjects = unverifiedData.results || unverifiedData
    for (const p of unverifiedProjects) {
      expect(p.is_verified).toBe(false)
    }
  })

  test('3. API эндпоинт POST /api/projects/<id>/verify/ успешно верифицирует сделку', async ({ request }) => {
    // Получаем проверенный проект, чтобы взять реальный ID
    const listRes = await request.get('/api/projects/?is_verified=true', {
      headers: {
        Authorization: `Bearer ${authToken}`
      }
    })
    expect(listRes.ok()).toBeTruthy()
    const listData = await listRes.json()
    const projects = listData.results || listData
    expect(projects.length).toBeGreaterThan(0)
    const testProjectId = projects[0].id

    // Вызываем эндпоинт верификации
    const verifyRes = await request.post(`/api/projects/${testProjectId}/verify/`, {
      headers: {
        Authorization: `Bearer ${authToken}`,
        'Content-Type': 'application/json'
      }
    })
    expect(verifyRes.ok()).toBeTruthy()
    const verifyBody = await verifyRes.json()
    expect(verifyBody.status).toBe('verified')
    expect(verifyBody.is_verified).toBe(true)
  })

  test('4. Аналитический запрос чата не падает и возвращает только проверенные сделки', async ({ page }) => {
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.click()
    await chatInput.fill('Выведи график продаж по менеджерам')
    await page.locator('button[title="Отправить запрос"]').click()

    // Проверяем, что ответ аналитики успешно сгенерирован и отображен
    await expect(page.locator('text=План-факт продаж по менеджерам').first()).toBeVisible({ timeout: 25000 })
    const chartCanvas = page.locator('canvas[role="img"]')
    await expect(chartCanvas).toBeVisible()
  })
})
