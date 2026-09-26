import { test, expect } from '@playwright/test'
import { loginWithToken } from './auth-helper'

test.describe('Mazory AI Chat Quality & UX Tests (D4 & D5)', () => {
  test.beforeEach(async ({ page }) => {
    await loginWithToken(page)
    await page.goto('/')
    await expect(page).toHaveTitle(/Mazory/i)
  })

  test('1. Loading state & input disabled during query processing (R5)', async ({ page }) => {
    let unblockResponse: () => void = () => {}
    const responseBlocked = new Promise<void>((resolve) => {
      unblockResponse = resolve
    })

    // Блокируем ответ API до проверки UI-состояний загрузки
    await page.route('**/api/chat/query/', async (route) => {
      await responseBlocked
      await route.continue()
    })

    const chatInput = page.locator('form input[type="text"]')
    await expect(chatInput).toBeVisible()
    await expect(chatInput).toBeEnabled()

    await chatInput.fill('Покажи воронку проектов и сделок')

    const submitBtn = page.locator('button[title="Отправить запрос"]')
    await expect(submitBtn).toBeEnabled()
    await submitBtn.click()

    // Пока запрос висит в ожидании — инпут заблокирован и показывает текст ожидания
    await expect(chatInput).toBeDisabled()
    await expect(chatInput).toHaveAttribute('placeholder', /Mazory думает\.\.\./)

    // Проверяем наличие спиннера (Loader2 с классом animate-spin)
    const spinner = page.locator('button[title="Отправить запрос"] .animate-spin')
    await expect(spinner).toBeVisible()

    // Разблокируем ответ бэкенда
    unblockResponse()

    // Ожидаем появления дашборда воронки
    await expect(page.locator('text=Воронка проектов и контроль экономики сделок')).toBeVisible({ timeout: 25000 })
  })

  test('2. Markdown rendering in AI response without raw markdown syntax (R4)', async ({ page }) => {
    test.setTimeout(60000)
    const chatInput = page.locator('form input[type="text"]')
    await chatInput.click()
    await chatInput.fill('Посчитай общую сумму по всем договорам')
    await page.locator('button[title="Отправить запрос"]').click()

    // Ожидаем завершения ответа и монтирования prose контейнера
    const proseContainer = page.locator('.prose')
    await expect(proseContainer).toBeVisible({ timeout: 45000 })

    // Проверяем, что в контейнере нет сырой разметки `**`
    const contentText = await proseContainer.innerText()
    expect(contentText).not.toContain('**')

    // Проверяем, что теги HTML рендерятся как DOM-элементы, а не сырой текст вида <div> или <p>
    expect(contentText).not.toContain('<div>')
    expect(contentText).not.toContain('<!--')
  })

  test('3. Protection against duplicate submissions while generating (R5)', async ({ page }) => {
    let queryRequestCount = 0
    page.on('request', (req) => {
      if (req.url().includes('/api/chat/query/')) {
        queryRequestCount++
      }
    })

    const chatInput = page.locator('form input[type="text"]')
    await chatInput.click()
    await chatInput.fill('Выведи график продаж по менеджерам')

    const submitBtn = page.locator('button[title="Отправить запрос"]')
    // Быстрый двойной клик
    await submitBtn.click()
    // Пытаемся нажать повторно
    await submitBtn.click({ force: true }).catch(() => {})

    // Ожидаем завершения
    await expect(page.locator('text=План-факт продаж по менеджерам').first()).toBeVisible({ timeout: 25000 })

    // Проверяем, что был отправлен ровно 1 сетевой запрос
    expect(queryRequestCount).toBe(1)
  })
})
