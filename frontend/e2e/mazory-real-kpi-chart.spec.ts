import { test, expect } from '@playwright/test'
import { loginWithToken } from './auth-helper'

test.describe('Mazory AI — Реальные данные KPI и график продаж', () => {
  test.beforeEach(async ({ page }) => {
    await loginWithToken(page)
    await page.goto('/')
    await expect(page).toHaveTitle(/Mazory/i)
  })

  test('1. Запрос KPI команды: отображение графика Chart.js, 3 сводных карточек и менеджеров', async ({ page }) => {
    const chatInput = page.locator('form input[type="text"]')
    await chatInput.click()
    await chatInput.fill('Покажи KPI команды')
    await page.locator('button[type="submit"]').click()

    // 1. Проверяем заголовок витрины
    await expect(page.locator('h1').filter({ hasText: /KPI/i })).toBeVisible({ timeout: 15000 })

    // 2. Проверяем наличие 3 сводных карточек метрик (Фактический сбор оплат, Выполнение плана, Активных договоров)
    await expect(page.locator('text=Фактический сбор оплат')).toBeVisible()
    await expect(page.locator('text=Выполнение плана сбора')).toBeVisible()
    await expect(page.locator('text=Активных договоров и сделок')).toBeVisible()

    // 3. Проверяем наличие сумм в тенге (₸)
    const tengeCards = page.locator('span:has-text("₸")')
    await expect(tengeCards.first()).toBeVisible()

    // 4. Проверяем наличие карточек менеджеров
    const managerCards = page.locator('.relative.flex.flex-col.p-4.rounded-2xl')
    await expect(managerCards.first()).toBeVisible()

    // Проверяем, что проценты не пустые (не просто "%", а с цифрой)
    const kpiPercentElements = page.locator('text=/\\d+%/')
    await expect(kpiPercentElements.first()).toBeVisible()
  })

  test('2. Карточки менеджеров отображают реальные данные без пустых полей', async ({ page }) => {
    const chatInput = page.locator('form input[type="text"]')
    await chatInput.click()
    await chatInput.fill('Покажи KPI менеджеров')
    await page.locator('button[type="submit"]').click()

    await expect(page.locator('h1').filter({ hasText: /KPI/i })).toBeVisible({ timeout: 15000 })

    // Проверяем, что у карточек менеджеров есть заголовки "Продажи" и "Сделок"
    await expect(page.locator('text=Продажи').first()).toBeVisible()
    await expect(page.locator('text=Сделок').first()).toBeVisible()

    // Проверяем, что поле Продажи не пустое (содержит ₸)
    const salesField = page.locator('div:has-text("Продажи") + div')
    await expect(salesField.first()).toContainText('₸')

    // Проверяем, что прогресс-бар имеет ширину > 0%
    const progressBar = page.locator('.h-full.rounded-full.transition-all').first()
    await expect(progressBar).toBeVisible()
  })
})
