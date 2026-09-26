import { test, expect } from '@playwright/test'
import { loginWithToken } from './auth-helper'

test.describe('Mazory Analytical Queries — Funnel, Margins & SLA Commitments Quality', () => {
  test.beforeEach(async ({ page }) => {
    await loginWithToken(page)
    await page.goto('/')
    await expect(page).toHaveTitle(/Mazory/i)
  })

  test('1. Запрос "Покажи воронку проектов и контроль маржи": реальные сделки, ранжирование по сумме, классификация оборудования и контроль рентабельности', async ({ page }) => {
    test.setTimeout(30000)
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.click()
    await chatInput.fill('Покажи воронку проектов и контроль маржи')
    await page.locator('button[title="Отправить запрос"]').click()

    // 1. Ожидаем появления виджета воронки
    await expect(page.locator('text=Воронка проектов и контроль экономики сделок')).toBeVisible({ timeout: 15000 })

    // 2. Проверяем предупреждение о низкой маржинальности (<15%)
    await expect(page.locator('text=сделок с маржой < 15%')).toBeVisible()

    // 3. Проверяем отсутствие чернового мусора (Ташкент, Узбекистан)
    const tableContainer = page.locator('table')
    await expect(tableContainer).toBeVisible()
    const tableText = await tableContainer.innerText()
    expect(tableText).not.toContain('Узбекистан')
    expect(tableText).not.toContain('Ташкент')
    expect(tableText).not.toContain('Закрытие задолженности и квартира по бартеру')

    // 4. Проверяем наличие ключевых промышленных объектов Aqua Kip с суммами в ₸
    expect(tableText).toContain('₸')
    await expect(page.locator('text=БМК / Котельная').or(page.locator('text=БТП')).first()).toBeVisible()

    // 5. Проверяем инсайты в ответе ассистента
    await expect(page.locator('text=Любые допработы требуют согласования').or(page.locator('text=марж')).first()).toBeVisible()
  })

  test('2. Запрос "Какие обещания и дедлайны горят?": вывод SLA реестра с горящими обязательствами и красными бейджами Просрочено', async ({ page }) => {
    test.setTimeout(30000)
    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.click()
    await chatInput.fill('Какие обещания и дедлайны горят?')
    await page.locator('button[title="Отправить запрос"]').click()

    // 1. Ожидаем появления виджета контроля обещаний
    await expect(page.locator('text=Контроль обещаний и дедлайнов (SLA)')).toBeVisible({ timeout: 15000 })

    // 2. Проверяем счетчик просроченных обязательств
    const overdueBadge = page.locator('text=Просрочено:')
    await expect(overdueBadge.first()).toBeVisible()

    // 3. Проверяем наличие красных плашек статуса "Просрочено" в строках обязательств
    const overdueStatusItems = page.locator('span:has-text("Просрочено")')
    await expect(overdueStatusItems.first()).toBeVisible()

    // 4. Проверяем наличие реальных менеджеров (Камиль, Жанат, Самат или Улугбек) и конкретных контрагентов
    const commitmentsContainer = page.locator('text=Контроль обещаний и дедлайнов (SLA)').locator('xpath=ancestor::div[contains(@class, "rounded-2xl")]')
    await expect(commitmentsContainer).toBeVisible()
    const commitmentsText = await commitmentsContainer.innerText()
    expect(commitmentsText).toMatch(/Камиль|Жанат|Самат|Улугбек/)

    // 5. Проверяем, что нет мусорных технических сообщений из чатов вроде "ок", "добрый день"
    expect(commitmentsText).not.toMatch(/^ок$/m)
    expect(commitmentsText).not.toMatch(/^добрый день$/m)
  })
})
