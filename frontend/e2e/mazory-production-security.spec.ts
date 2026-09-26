import { test, expect } from '@playwright/test'
import { getOrCreateE2EToken } from './auth-helper'

test.describe('Production Security & Zero-Trust Route Lockdown (WhatsApp, Bitrix, KPI, Projects)', () => {
  test('1. Анонимный вызов POST /api/whatsapp/send/ блокируется с 401 Unauthorized', async ({ request }) => {
    const res = await request.post('/api/whatsapp/send/', {
      data: { phone: '77011234567', message: 'Unauthorized message attempt' },
      headers: { 'Content-Type': 'application/json' }
    })
    expect(res.status()).toBe(401)
  })

  test('2. Анонимный вызов GET /api/whatsapp/status/ блокируется с 401 Unauthorized', async ({ request }) => {
    const res = await request.get('/api/whatsapp/status/')
    expect(res.status()).toBe(401)
  })

  test('3. Анонимный вызов GET /api/whatsapp/qr/ блокируется с 401 Unauthorized', async ({ request }) => {
    const res = await request.get('/api/whatsapp/qr/')
    expect(res.status()).toBe(401)
  })

  test('4. Анонимный вызов GET /api/kpi/summary/ блокируется с 401 Unauthorized', async ({ request }) => {
    const res = await request.get('/api/kpi/summary/')
    expect(res.status()).toBe(401)
  })

  test('5. Анонимный вызов GET /api/projects/ блокируется с 401 Unauthorized', async ({ request }) => {
    const res = await request.get('/api/projects/')
    expect(res.status()).toBe(401)
  })

  test('6. Входящий вебхук WAHA без ключа API отклоняется с 401 Unauthorized', async ({ request }) => {
    const res = await request.post('/api/whatsapp/webhook/', {
      data: {
        event: 'message',
        payload: { body: 'Malicious fake payload without key' }
      },
      headers: { 'Content-Type': 'application/json' }
    })
    expect(res.status()).toBe(401)
  })

  test('7. Аутентифицированный пользователь с JWT токеном успешно получает KPI сводку', async ({ request }) => {
    const token = getOrCreateE2EToken()
    const res = await request.get('/api/kpi/summary/', {
      headers: {
        'Authorization': `Bearer ${token}`
      }
    })
    expect(res.status()).toBe(200)
    const data = await res.json()
    expect(data.category_badge).toBeDefined()
    expect(data.summary_metrics).toBeInstanceOf(Array)
  })

  test('8. Аутентифицированный пользователь с JWT токеном успешно получает реестр проектов', async ({ request }) => {
    const token = getOrCreateE2EToken()
    const res = await request.get('/api/projects/', {
      headers: {
        'Authorization': `Bearer ${token}`
      }
    })
    expect(res.status()).toBe(200)
    const data = await res.json()
    expect(Array.isArray(data)).toBe(true)
  })
})
