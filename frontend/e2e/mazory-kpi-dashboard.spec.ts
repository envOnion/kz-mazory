import { test, expect } from '@playwright/test'
import { loginWithToken } from './auth-helper'

test.describe('Mazory AI — Витрина KPI, периоды, график и Drill-Down по менеджерам', () => {
  test.beforeEach(async ({ page }) => {
    await loginWithToken(page)
    await page.goto('/')
    await expect(page).toHaveTitle(/Mazory/i)

    // Кликаем по кнопке подсказки "Покажи KPI команды", чтобы перейти в режим дашборда
    const kpiBtn = page.locator('button:has-text("Покажи KPI команды")')
    await expect(kpiBtn).toBeVisible({ timeout: 10000 })
    await kpiBtn.click()

    // Дожидаемся появления заголовка витрины KPI
    await expect(page.locator('h1').filter({ hasText: /KPI/i })).toBeVisible({ timeout: 15000 })
  })

  test('1. Начальный сводный экран: 3 сводные карточки, встроенный график Chart.js и карточки менеджеров', async ({ page }) => {
    // 2. Проверяем наличие 3 сводных карточек
    await expect(page.locator('text=Фактический сбор оплат')).toBeVisible()
    await expect(page.locator('text=Выполнение плана сбора')).toBeVisible()
    await expect(page.locator('text=Активных договоров и сделок')).toBeVisible()

    // 3. Проверяем наличие сумм в тенге (₸) в сводных карточках
    const tengeCards = page.locator('span:has-text("₸")')
    await expect(tengeCards.first()).toBeVisible()

    // 4. Проверяем наличие встроенного графика Chart.js (canvas)
    const chartCanvas = page.locator('canvas')
    await expect(chartCanvas.first()).toBeVisible()

    // 5. Проверяем наличие карточек менеджеров
    const managerCards = page.locator('.relative.flex.flex-col.p-4.rounded-2xl')
    await expect(managerCards.first()).toBeVisible()
    const count = await managerCards.count()
    expect(count).toBeGreaterThanOrEqual(4)

    // 6. Проверяем, что проценты выполнения отображаются
    const kpiPercentElements = page.locator('text=/\\d+%/')
    await expect(kpiPercentElements.first()).toBeVisible()
  })

  test('2. Переключение временных периодов дашборда', async ({ page }) => {
    await expect(page.locator('h1').filter({ hasText: /KPI/i })).toBeVisible({ timeout: 15000 })

    // Проверяем наличие кнопок выбора периода
    const thisMonthBtn = page.locator('button:has-text("Этот месяц")')
    const lastMonthBtn = page.locator('button:has-text("Прошлый месяц")')
    const quarterBtn = page.locator('button:has-text("Квартал")')
    const yearBtn = page.locator('button:has-text("С начала года")')

    await expect(thisMonthBtn).toBeVisible()
    await expect(lastMonthBtn).toBeVisible()
    await expect(quarterBtn).toBeVisible()
    await expect(yearBtn).toBeVisible()

    // Кликаем на «Прошлый месяц»
    await lastMonthBtn.click()
    // Проверяем, что кнопка стала активной (получила класс bg-indigo-600)
    await expect(lastMonthBtn).toHaveClass(/bg-indigo-600/)

    // Кликаем на «Квартал»
    await quarterBtn.click()
    await expect(quarterBtn).toHaveClass(/bg-indigo-600/)

    // Возвращаемся на «Этот месяц»
    await thisMonthBtn.click()
    await expect(thisMonthBtn).toHaveClass(/bg-indigo-600/)
  })

  test('3. Интерактивный Drill-down: открытие модального окна с объектами менеджера', async ({ page }) => {
    await expect(page.locator('h1').filter({ hasText: /KPI/i })).toBeVisible({ timeout: 15000 })

    // Кликаем на первую карточку менеджера
    const firstManagerCard = page.locator('.relative.flex.flex-col.p-4.rounded-2xl').first()
    await expect(firstManagerCard).toBeVisible()
    await firstManagerCard.click()

    // Проверяем появление модального окна Drill-down
    const drillDownModal = page.locator('.fixed.inset-0.z-50')
    await expect(drillDownModal).toBeVisible()

    // Проверяем наличие ключевых финансовых блоков в модалке
    await expect(drillDownModal.locator('text=Факт сбора')).toBeVisible()
    await expect(drillDownModal.locator('text=План сбора')).toBeVisible()
    await expect(drillDownModal.locator('text=KPI план')).toBeVisible()
    await expect(drillDownModal.locator('text=Ср. маржа')).toBeVisible()

    // Проверяем наличие кнопки быстрого действия с AI
    const askAiBtn = drillDownModal.locator('button:has-text("Спросить AI")')
    await expect(askAiBtn).toBeVisible()

    // Закрываем модальное окно по кнопке «Закрыть»
    const closeBtn = drillDownModal.locator('button:has-text("Закрыть")')
    await closeBtn.click()
    await expect(drillDownModal).not.toBeVisible()
  })
})
