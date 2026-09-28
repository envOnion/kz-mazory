import { test, expect } from '@playwright/test'
import { adminLogin } from './auth-helper'

test('Trace cards preserve contrast and escape source HTML', async ({ page }) => {
    await adminLogin(page)
    await page.goto('/admin/api/messageprocessingtrace/1/change/')
    const whatsappBlock = page.locator('.mazory-trace-content').first()
    await expect(whatsappBlock).toBeVisible()
    expect(await page.evaluate(() => Reflect.get(window, '__xss'))).toBeUndefined()
    // Вычисляем фактические стили текста и фона через getComputedStyle
    const styles = await whatsappBlock.evaluate((el) => {
      const comp = window.getComputedStyle(el)
      return {
        color: comp.color,
        backgroundColor: comp.backgroundColor,
        fontSize: comp.fontSize,
      }
    })

    // Цвет текста должен быть определен (rgb или rgba)
    expect(styles.color).toBeTruthy()
    // Парсим RGB компоненты цвета текста
    const rgbMatch = styles.color.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/)
    expect(rgbMatch).not.toBeNull()

    if (rgbMatch) {
      const r = parseInt(rgbMatch[1], 10)
      const g = parseInt(rgbMatch[2], 10)
      const b = parseInt(rgbMatch[3], 10)

      // В светлой теме цвет текста должен быть глубоким темным (r, g, b < 60)
      // Наш цвет #0f172a это rgb(15, 23, 42)
      // Ранее нестилизованный цвет был серым (> 100)
      const isDarkBackground = styles.backgroundColor.includes('15, 23, 42') || styles.backgroundColor.includes('30, 41, 59')
      if (!isDarkBackground) {
        // Светлый фон: текст должен быть темным (r, g, b < 80)
        expect(r).toBeLessThan(80)
        expect(g).toBeLessThan(80)
        expect(b).toBeLessThan(80)
      } else {
        // Темный фон: текст должен быть светлым (r, g, b > 200)
        expect(r).toBeGreaterThan(200)
        expect(g).toBeGreaterThan(200)
        expect(b).toBeGreaterThan(200)
      }
    }

    // Проверяем наличие всех 4 этапов трассировки на странице
    await expect(page.locator('text=1. Входные данные WhatsApp').first()).toBeVisible()
    await expect(page.locator('text=2. Зависимые данные из сообщений ранее').first()).toBeVisible()
    await expect(page.locator('text=3. Зависимые данные из Bitrix24').first()).toBeVisible()
    await expect(page.locator('text=4. Итоговая запись').first()).toBeVisible()
})
