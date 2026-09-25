import { test, expect } from '@playwright/test'
import { loginWithToken } from './auth-helper'

test.describe('Mazory AI Business OS — E2E Сквозные сценарии', () => {
  test.beforeEach(async ({ page }) => {
    // Авторизуем пользователя перед прогоном дашборда
    await loginWithToken(page)
    // Открываем главную страницу приложения
    await page.goto('/')
    await expect(page).toHaveTitle(/Mazory/i)
  })

  test('1. Начальный экран: заголовок, фоновый канвас и плашки подсказок', async ({ page }) => {
    // Проверяем наличие логотипа Mazory и приветственного экрана
    await expect(page.getByRole('heading', { name: 'Mazory' })).toBeVisible()
    
    // Проверяем наличие саджестов
    const promptButtons = page.locator('button:has-text("↗")')
    await expect(promptButtons.first()).toBeVisible()
  })

  test('2. Запрос графика продаж: монтирование Chart.js пресета (Canvas)', async ({ page }) => {
    // Вводим запрос на график продаж в поле ввода
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.fill('Выведи график продаж по менеджерам')
    await chatInput.press('Enter')

    // Ожидаем ответа оркестратора и отрисовки графика
    await expect(page.locator('text=План-факт продаж по менеджерам').first()).toBeVisible({ timeout: 10000 })
    
    // Проверяем наличие Chart.js canvas элемента (Chart.js рендерит canvas с role="img")
    const chartCanvas = page.locator('canvas[role="img"]')
    await expect(chartCanvas).toBeVisible()
  })

  test('3. Запрос обещаний и дедлайнов: монтирование пресета SLA обязательств', async ({ page }) => {
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.fill('Какие обещания и дедлайны горят?')
    await chatInput.press('Enter')

    // Ожидаем появления виджета контроля обещаний
    await expect(page.locator('text=Контроль обещаний и дедлайнов (SLA)')).toBeVisible({ timeout: 10000 })
    
    // Проверяем наличие счетчиков SLA
    await expect(page.locator('text=Выполнено:').first()).toBeVisible()
    await expect(page.locator('text=Просрочено:').first()).toBeVisible()
  })

  test('4. Запрос воронки проектов: отображение таблицы объектов и контроля маржи', async ({ page }) => {
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.fill('Покажи воронку проектов и сделок')
    await chatInput.press('Enter')

    // Ожидаем появления таблицы проектов
    await expect(page.locator('text=Воронка проектов и контроль экономики сделок')).toBeVisible({ timeout: 10000 })
    
    // Проверяем отображение ключевых объектов Aqua Kip
    await expect(page.locator('text=ПСЭМ-01-2026').first()).toBeVisible()
  })

  test('5. Запрос KPI команды: отображение карточек менеджеров в тенге ₸', async ({ page }) => {
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.fill('Покажи KPI менеджеров')
    await chatInput.press('Enter')

    // Проверяем карточки реальных менеджеров Aqua Kip
    await expect(page.locator('text=Жанат Бейсбаев').first()).toBeVisible({ timeout: 10000 })
    await expect(page.locator('text=Самат Ерланулы').first()).toBeVisible()
    await expect(page.locator('text=Улугбек').first()).toBeVisible()
    await expect(page.locator('text=Камиль').first()).toBeVisible()
  })
})
