import { test, expect } from '@playwright/test'
import { getOrCreateE2EToken, loginWithToken } from './auth-helper'

test.describe('Prompt Input & Buttons Auth Guard (JWT 401 & Phone/SMS Modal)', () => {
  test('1. Неавторизованный запрос через Prompt Input bar перехватывает 401 и открывает Phone/SMS Auth Modal', async ({ page }) => {
    // Чистая сессия без токена
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Mazory' })).toBeVisible()

    // Вводим запрос в поле ввода
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.fill('Покажи график продаж')

    // Ожидаем запрос к бэкенду и проверяем возврат статуса 401
    const responsePromise = page.waitForResponse(
      (resp) => resp.url().includes('/api/chat/query/') && resp.status() === 401
    )
    await chatInput.press('Enter')
    const response = await responsePromise
    expect(response.status()).toBe(401)

    // Проверяем, что появилось окно Phone / SMS Auth Modal
    const modalHeading = page.getByRole('heading', { name: 'Вход в Mazory' })
    await expect(modalHeading).toBeVisible({ timeout: 5000 })
    await expect(page.locator('input[type="tel"]')).toBeVisible()
    await expect(page.locator('button:has-text("Получить СМС-код")')).toBeVisible()
  })

  test('2. Клик по кнопке саджеста неавторизованным пользователем перехватывает 401 и открывает AuthModal', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Mazory' })).toBeVisible()

    // Нажимаем на саджест-кнопку
    const suggestionBtn = page.locator('button:has-text("Покажи график продаж и выполнения плана")')
    await expect(suggestionBtn).toBeVisible()

    const responsePromise = page.waitForResponse(
      (resp) => resp.url().includes('/api/chat/query/') && resp.status() === 401
    )
    await suggestionBtn.click()
    const response = await responsePromise
    expect(response.status()).toBe(401)

    // Проверяем открытие модального окна
    await expect(page.getByRole('heading', { name: 'Вход в Mazory' })).toBeVisible({ timeout: 5000 })
  })

  test('3. Клик по кнопке прикрепления файла без авторизации вызывает AuthModal', async ({ page }) => {
    await page.goto('/')
    const attachBtn = page.locator('button[title="Прикрепить файл"]')
    await attachBtn.click()

    await expect(page.getByRole('heading', { name: 'Вход в Mazory' })).toBeVisible({ timeout: 5000 })
    await expect(page.locator('text=Для прикрепления файлов необходимо войти в систему')).toBeVisible()
  })

  test('4. Клик по кнопке голосового ввода без авторизации вызывает AuthModal', async ({ page }) => {
    await page.goto('/')
    const micBtn = page.locator('button[title="Голосовой ввод"]')
    await micBtn.click()

    await expect(page.getByRole('heading', { name: 'Вход в Mazory' })).toBeVisible({ timeout: 5000 })
    await expect(page.locator('text=Для голосового ввода необходимо войти в систему')).toBeVisible()
  })

  test('5. Авторизованный пользователь с валидным JWT токеном успешно выполняет запросы', async ({ page }) => {
    const token = getOrCreateE2EToken()
    await loginWithToken(page, token)

    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Mazory' })).toBeVisible()

    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.fill('Выведи график продаж по менеджерам')

    const responsePromise = page.waitForResponse(
      (resp) => resp.url().includes('/api/chat/query/') && resp.status() === 200
    )
    await chatInput.press('Enter')
    const response = await responsePromise
    expect(response.status()).toBe(200)

    // График отрисовался, модалка не открыта
    await expect(page.locator('text=План-факт продаж по менеджерам').first()).toBeVisible({ timeout: 10000 })
    await expect(page.locator('canvas[role="img"]')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Вход в Mazory' })).not.toBeVisible()
  })
})
