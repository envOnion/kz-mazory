import { test, expect } from '@playwright/test'
import { execSync } from 'child_process'

function clearOtpCache(phone = '77774581111') {
  try {
    const cmd = `docker compose exec backend python manage.py shell -c "from django.core.cache import cache; cache.delete('otp:${phone}'); cache.delete('otp_cooldown:${phone}'); cache.delete('otp_attempts:${phone}')"`
    execSync(cmd)
  } catch (e) {
    console.error('Failed to clear OTP cache:', e)
  }
}

function getCachedOtp(phone = '77774581111'): string | null {
  try {
    const cmd = `docker compose exec backend python manage.py shell -c "from django.core.cache import cache; print('CACHED_OTP:' + str(cache.get('otp:${phone}')))"`
    const out = execSync(cmd).toString()
    const match = out.match(/^CACHED_OTP:(\d{4})$/m)
    return match ? match[1].trim() : null
  } catch (e) {
    console.error('Failed to get cached OTP:', e)
    return null
  }
}

function getLatestWahaMessage(phone = '77774581111'): { body: string; fromMe: boolean } | null {
  try {
    const cmd = `docker compose exec backend python manage.py shell -c "import requests, json; r = requests.get('http://waha:3000/api/default/chats/${phone}@c.us/messages?limit=3', headers={'X-Api-Key': 'mazory-waha-key-2026'}); msgs = r.json() if r.status_code == 200 else []; print('WAHA_TOP:' + json.dumps(msgs[0] if msgs else None))"`
    const out = execSync(cmd).toString()
    const match = out.match(/^WAHA_TOP:(.+)$/m)
    if (match) {
      return JSON.parse(match[1])
    }
  } catch (e) {
    console.error('Failed to query WAHA message:', e)
  }
  return null
}

test.describe('WhatsApp OTP Verification via WAHA E2E', () => {
  test.beforeEach(async () => {
    clearOtpCache('77774581111')
  })

  test('Запрос СМС-кода через фронтенд генерирует случайный 4-значный код и отправляет сообщение в WhatsApp через WAHA', async ({ page }) => {
    // 1. Открываем приложение
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Mazory' })).toBeVisible()

    // 2. Открываем AuthModal через кнопку прикрепления файла (неавторизованный доступ)
    const attachBtn = page.locator('button[title="Прикрепить файл"]')
    await attachBtn.click()

    // 3. Проверяем модальное окно авторизации
    const modalHeading = page.getByRole('heading', { name: 'Вход в Mazory' })
    await expect(modalHeading).toBeVisible({ timeout: 5000 })

    const phoneInput = page.locator('input[type="tel"]')
    await expect(phoneInput).toBeVisible()

    // Вводим номер пользователя +77774581111
    await phoneInput.fill('+7 (777) 458-11-11')

    // 4. Отправляем запрос кода и перехватываем ответ бэкенда
    const sendCodePromise = page.waitForResponse(
      (resp) => resp.url().includes('/api/auth/send-code/') && resp.status() === 200
    )

    const submitBtn = page.locator('button:has-text("Получить СМС-код")')
    await submitBtn.click()

    const sendCodeResponse = await sendCodePromise
    const respData = await sendCodeResponse.json()

    // Проверяем успешный статус ответа бэкенда
    expect(respData.status).toBe('success')
    expect(respData.phone).toBe('77774581111')

    // 5. Проверяем переход интерфейса ко 2-му шагу (ввод кода)
    await expect(page.getByRole('heading', { name: 'Код подтверждения' })).toBeVisible({ timeout: 5000 })
    await expect(page.locator('text=+7 (777) 458-11-11')).toBeVisible()

    // 6. Проверяем, что в Redis сохранен случайный 4-значный код (1000..9999)
    const generatedCode = getCachedOtp('77774581111')
    expect(generatedCode).not.toBeNull()
    expect(generatedCode?.length).toBe(4)
    expect(/^\d{4}$/.test(generatedCode || '')).toBe(true)
    const codeNum = parseInt(generatedCode || '0', 10)
    expect(codeNum).toBeGreaterThanOrEqual(1000)
    expect(codeNum).toBeLessThanOrEqual(9999)

    // 7. Ожидаем завершения фоновой задачи в Django Q и проверяем WAHA
    // Даем до 5 секунд на обработку очереди воркером qcluster
    await page.waitForTimeout(2500)

    const wahaMsg = getLatestWahaMessage('77774581111')
    expect(wahaMsg).not.toBeNull()
    expect(wahaMsg?.fromMe).toBe(true)
    expect(wahaMsg?.body).toContain(`Ваш код подтверждения для входа в Mazory AI: ${generatedCode}`)
    expect(wahaMsg?.body).toContain('Код действителен 5 минут.')

    // 8. Вводим полученный код в интерфейсе и завершаем вход
    const codeInput = page.locator('input[placeholder="• • • •"]')
    await codeInput.fill(generatedCode!)

    const verifyPromise = page.waitForResponse(
      (resp) => resp.url().includes('/api/auth/verify-code/') && resp.status() === 200
    )
    const enterBtn = page.locator('button:has-text("Войти в систему")')
    await enterBtn.click()

    const verifyResponse = await verifyPromise
    const verifyData = await verifyResponse.json()
    expect(verifyData.status).toBe('success')
    expect(verifyData.access).toBeTruthy()

    // 9. Проверяем, что модальное окно закрылось после успешной авторизации
    await expect(modalHeading).not.toBeVisible({ timeout: 5000 })
  })
})
