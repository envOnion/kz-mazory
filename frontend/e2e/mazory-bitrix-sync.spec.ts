import { test, expect } from '@playwright/test'
import { execSync } from 'child_process'
import { loginWithToken } from './auth-helper'

test.describe('Bitrix24 CRM Sync & Deduplication — E2E Сквозные сценарии', () => {
  test.beforeAll(() => {
    // Подготовка тестовых данных в базе: создание админа и уникального проекта без дублей
    const setupCmd = `docker compose exec backend python manage.py shell -c "
from django.contrib.auth.models import User
from api.models import Project, BitrixSettings
from decimal import Decimal

# Суперпользователь для проверки админки
admin_user, _ = User.objects.get_or_create(username='admin', defaults={'is_staff': True, 'is_superuser': True})
admin_user.is_staff = True
admin_user.is_superuser = True
admin_user.set_password('admin2026')
admin_user.save()

# Настройки Битрикс
BitrixSettings.objects.get_or_create(
    id=1,
    defaults={
        'webhook_url': 'https://b24-test.bitrix24.kz/rest/1/testkey/',
        'hourly_sync_enabled': True,
        'auto_import_deals': True,
        'auto_create_tasks': True,
    }
)

# Проект ЖК Медео для проверки дедупликации и отображения
Project.objects.filter(normalized_name='медео').delete()
Project.objects.create(
    name='ЖК Медео',
    contract_number='MEDEO-2026',
    status='deal_won',
    contract_amount=Decimal('85000000.00'),
    cost_amount=Decimal('62000000.00'),
    source='chat',
    bitrix_id='100500',
    needs_bitrix_sync=False,
)
"`
    execSync(setupCmd, { stdio: 'inherit' })
  })

  test('1. Воронка проектов в UI: рендеринг сделки и отсутствие дубликатов', async ({ page }) => {
    await loginWithToken(page)
    await page.goto('/')

    const chatInput = page.locator('input[placeholder*="Спросите"]')
    await chatInput.fill('Покажи воронку проектов и сделок')
    await chatInput.press('Enter')

    // Ожидаем появления виджета воронки
    await expect(page.locator('text=Воронка проектов и контроль экономики сделок').first()).toBeVisible({ timeout: 15000 })

    // Проверяем наличие карточки ЖК Медео
    const medeoCards = page.locator('text=ЖК Медео')
    await expect(medeoCards.first()).toBeVisible()
    
    // Гарантируем, что нет 3 дублей ЖК Медео (ровно 1 проект в базе и на карточке)
    await expect(medeoCards).toHaveCount(1)

    // Проверяем отображение суммы сделки (85 000 000 ₸)
    await expect(page.locator('text=85 000 000 ₸').first()).toBeVisible()
  })

  test('2. Django Admin: отображение параметров BitrixSettings и Project', async ({ page }) => {
    // Вход в админку
    await page.goto('/admin/login/?next=/admin/api/bitrixsettings/')
    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('admin2026')
      await page.locator('button[type="submit"], input[type="submit"]').click()
    }

    await page.waitForURL('**/admin/api/bitrixsettings/**', { timeout: 15000 })
    await expect(page.locator('body')).toContainText('AquaKip')

    // Проверяем список проектов в админке
    await page.goto('/admin/api/project/')
    await page.waitForURL('**/admin/api/project/**', { timeout: 15000 })

    // Проверяем наличие колонок и проекта Медео
    await expect(page.locator('body')).toContainText('ЖК Медео')
    await expect(page.locator('body')).toContainText('WhatsApp Чат')
  })

  test('3. Эндпоинт входящего вебхука Bitrix24 (/api/bitrix/webhook/)', async ({ request }) => {
    // Отправляем симуляцию вебхука создания сделки из CRM Bitrix24
    const response = await request.post('/api/bitrix/webhook/', {
      form: {
        event: 'ONCRMDEALADD',
        'data[FIELDS][ID]': '99999',
      },
    })

    expect(response.status()).toBe(200)
    const json = await response.json()
    expect(json.status).toBe('queued')
    expect(json.deal_id).toBe('99999')
  })
})
