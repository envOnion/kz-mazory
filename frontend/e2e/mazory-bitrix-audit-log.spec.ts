import { test, expect } from '@playwright/test'
import { execSync } from 'child_process'

test.describe('Bitrix24 Deal Audit Log — E2E Сквозные сценарии Django Admin', () => {
  test.beforeAll(() => {
    // Подготовка тестовых данных: администратор, объект Project и логи изменений BitrixDealChangeLog
    const pyCode = `
from django.contrib.auth.models import User
from api.models import Project, BitrixDealChangeLog, BitrixSettings
from decimal import Decimal

admin_user, _ = User.objects.get_or_create(username='admin', defaults={'is_staff': True, 'is_superuser': True})
admin_user.is_staff = True
admin_user.is_superuser = True
admin_user.set_password('admin2026')
admin_user.save()

Project.objects.filter(bitrix_id='889900').delete()
p = Project.objects.create(
    name='ЖК Наурыз Резиденс',
    contract_number='NR-2026',
    status='in_execution',
    contract_amount=Decimal('92000000.00'),
    bitrix_id='889900',
    needs_bitrix_sync=False,
)
BitrixDealChangeLog.objects.filter(bitrix_deal_id='889900').delete()
BitrixDealChangeLog.objects.create(
    project=p,
    bitrix_deal_id='889900',
    action='create',
    status='success',
    payload={'TITLE': 'ЖК Наурыз Резиденс', 'OPPORTUNITY': 92000000.0, 'STAGE_ID': 'NEW'},
    response_data={'result': 889900},
    changed_fields=['TITLE', 'OPPORTUNITY', 'STAGE_ID'],
    duration_ms=135,
    triggered_by='qcluster_create_deal_task'
)
BitrixDealChangeLog.objects.create(
    project=p,
    bitrix_deal_id='889900',
    action='update',
    status='error',
    payload={'STAGE_ID': 'INVALID_STAGE'},
    response_data={'error': 'ERROR_CORE', 'error_description': 'Stage not allowed'},
    error_message='Bitrix24 API error (crm.deal.update): Stage not allowed',
    changed_fields=['STAGE_ID'],
    duration_ms=85,
    triggered_by='qcluster_sync_single_deal_task'
)
print('AUDIT_LOG_SEED_DONE')
`
    try {
      execSync('docker compose exec -T backend python manage.py shell', { input: pyCode, stdio: ['pipe', 'pipe', 'pipe'] })
    } catch {
      execSync('cd ../ && USE_SQLITE=1 uv run --project backend python backend/manage.py shell', { input: pyCode, stdio: ['pipe', 'pipe', 'pipe'] })
    }
  })

  test('1. Раздел аудита сделок Bitrix24 в Django Admin: список, бейджи и фильтры', async ({ page }) => {
    // Переход на страницу списка логов изменений
    await page.goto('/admin/login/?next=/admin/api/bitrixdealchangelog/')
    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('admin2026')
      await page.locator('button[type="submit"], input[type="submit"]').click()
    }

    await page.waitForURL('**/admin/api/bitrixdealchangelog/**', { timeout: 15000 })

    // Проверяем наличие ключевых записей и объектов
    await expect(page.locator('body')).toContainText('Логи изменений сделок Bitrix24')
    await expect(page.locator('body')).toContainText('889900')
    await expect(page.locator('body')).toContainText('ЖК Наурыз Резиденс')

    // Проверяем отображение цветных бейджей действий и статусов
    await expect(page.locator('text=Создание сделки').first()).toBeVisible()
    await expect(page.locator('text=Обновление сделки').first()).toBeVisible()
    await expect(page.locator('text=Успешно').first()).toBeVisible()
    await expect(page.locator('text=Ошибка').first()).toBeVisible()

    // Проверяем отображение длительности и инициатора
    await expect(page.locator('text=135 мс').first()).toBeVisible()
    await expect(page.locator('text=qcluster_create_deal_task').first()).toBeVisible()
  })

  test('2. Детальный просмотр карточки лога: форматированный JSON payload и ответ', async ({ page }) => {
    // Вход в карточку первого лога
    await page.goto('/admin/api/bitrixdealchangelog/')
    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('admin2026')
      await page.locator('button[type="submit"], input[type="submit"]').click()
      await page.waitForURL('**/admin/api/bitrixdealchangelog/**', { timeout: 15000 })
    }

    // Переходим в карточку первого лога (клик по ссылке на запись)
    const logLink = page.locator('a[href*="/admin/api/bitrixdealchangelog/"]:has-text("889900"), tr:has-text("889900") a').first()
    await logLink.click()
    await page.waitForURL('**/admin/api/bitrixdealchangelog/**/change/**', { timeout: 15000 })

    // Проверяем отображение форматированного JSON payload в <pre><code>
    await expect(page.locator('pre code').first()).toBeVisible()
    await expect(page.locator('body')).toContainText('Отправленные данные (Payload)')
    await expect(page.locator('body')).toContainText('Ответ Bitrix24 REST API')
  })

  test('3. Карточка объекта Project в админке: встроенный Inline с историей аудита', async ({ page }) => {
    // Переходим в список проектов
    await page.goto('/admin/api/project/')
    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('admin2026')
      await page.locator('button[type="submit"], input[type="submit"]').click()
      await page.waitForURL('**/admin/api/project/**', { timeout: 15000 })
    }

    // Кликаем по проекту "ЖК Наурыз Резиденс"
    const projectLink = page.locator('a:has-text("ЖК Наурыз Резиденс")').first()
    await projectLink.click()
    await page.waitForURL('**/admin/api/project/**/change/**', { timeout: 15000 })

    // Проверяем наличие встроенного блока (Inline) с логами изменений Bitrix24
    await expect(page.locator('body')).toContainText('Логи изменений сделок Bitrix24')
    await expect(page.locator('body')).toContainText('889900')
    await expect(page.locator('body')).toContainText('135 мс')
  })
})
