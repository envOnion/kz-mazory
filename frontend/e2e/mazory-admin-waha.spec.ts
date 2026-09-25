import { test, expect } from '@playwright/test'

test.describe('Django Admin — WAHA Control Dashboard', () => {
  test('Рендеринг /admin/waha-dashboard/ с кнопками управления и списком групп', async ({ page }) => {
    // 1. Переходим на страницу дашборда WAHA
    await page.goto('/admin/login/?next=/admin/waha-dashboard/')

    // 2. Если требуется логин — авторизуемся под admin
    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('mazory2026')
      await page.locator('button[type="submit"], input[type="submit"]').click()
    }

    // 3. Ожидаем загрузку дашборда WAHA
    await page.waitForURL('**/admin/waha-dashboard/**', { timeout: 15_000 })

    // Проверяем заголовок страницы
    await expect(page.locator('text=WAHA Центр управления WhatsApp')).toBeVisible()

    // Проверяем наличие всех 4 кнопок управления
    await expect(page.locator('button:has-text("Запустить сессию")')).toBeVisible()
    await expect(page.locator('button:has-text("Перезапустить")')).toBeVisible()
    await expect(page.locator('button:has-text("Остановить")')).toBeVisible()
    await expect(page.locator('button:has-text("Выйти (Logout)")')).toBeVisible()

    // Проверяем статусную карточку и блок групп
    await expect(page.locator('text=Статус сессии')).toBeVisible()
    await expect(page.locator('text=Подключенный аккаунт')).toBeVisible()
    await expect(page.locator('text=Доступные чаты и группы')).toBeVisible()

    // Проверяем, что отображается подключенный пользователь и обнаруженные группы WhatsApp
    await expect(page.locator('text=Anton')).toBeVisible()
    await expect(page.locator('text=General')).toBeVisible()
    await expect(page.locator('button:has-text("Копировать ID")').first()).toBeVisible()

    // Проверяем отсутствие критических ошибок сервера
    await expect(page.locator('text=Internal Server Error')).toHaveCount(0)
    await expect(page.locator('text=Traceback')).toHaveCount(0)
  })
})
