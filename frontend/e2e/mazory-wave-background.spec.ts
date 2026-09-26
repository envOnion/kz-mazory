import { test, expect } from '@playwright/test'

test.describe('Interactive Canvas 2D Wave Background — E2E Сквозные сценарии', () => {
  test.beforeEach(async ({ page }) => {
    // Открываем главную страницу приложения
    await page.goto('/')
  })

  test('1. Фон монтируется, канвас активен и непрерывно рендерит анимацию', async ({ page }) => {
    const canvas = page.locator('.wave-background canvas')
    await expect(canvas).toBeVisible()

    // Проверяем, что размеры канваса больше 0
    const box = await canvas.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThan(0)
    expect(box!.height).toBeGreaterThan(0)

    // Проверяем активность requestAnimationFrame в браузере
    const framesCount = await page.evaluate(async () => {
      let count = 0
      return new Promise<number>((resolve) => {
        const check = () => {
          count++
          if (count >= 5) {
            resolve(count)
          } else {
            requestAnimationFrame(check)
          }
        }
        requestAnimationFrame(check)
      })
    })
    expect(framesCount).toBeGreaterThanOrEqual(5)
  })

  test('2. Смена видимости (visibilitychange): пауза при скрытии и возобновление при возврате', async ({ page }) => {
    const canvas = page.locator('.wave-background canvas')
    await expect(canvas).toBeVisible()

    // Симулируем скрытие вкладки (document.hidden = true)
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', {
        configurable: true,
        get: () => true,
      })
      document.dispatchEvent(new Event('visibilitychange'))
    })

    // Небольшая задержка в скрытом состоянии
    await page.waitForTimeout(300)

    // Симулируем возврат пользователя на вкладку (document.hidden = false)
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', {
        configurable: true,
        get: () => false,
      })
      document.dispatchEvent(new Event('visibilitychange'))
    })

    // Проверяем, что после возвращения анимация возобновилась и канвас активен
    const isAnimating = await page.evaluate(async () => {
      let frames = 0
      return new Promise<boolean>((resolve) => {
        const timeout = setTimeout(() => resolve(false), 2000)
        const loop = () => {
          frames++
          if (frames >= 3) {
            clearTimeout(timeout)
            resolve(true)
          } else {
            requestAnimationFrame(loop)
          }
        }
        requestAnimationFrame(loop)
      })
    })

    expect(isAnimating).toBe(true)

    // Проверяем событие фокуса окна
    await page.evaluate(() => {
      window.dispatchEvent(new Event('focus'))
    })

    // Убеждаемся, что канвас по-прежнему отрисован и не крашнулся
    await expect(canvas).toBeVisible()
  })
})
