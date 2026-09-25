import { test, expect } from '@playwright/test'
import { execSync } from 'child_process'

test.describe('Django Admin — RawMessage Changelist View', () => {
  test.beforeAll(() => {
    // Гарантируем наличие суперпользователя admin и тестовых сообщений RawMessage
    const setupCmd = `docker compose exec backend python manage.py shell -c "
from django.contrib.auth.models import User
from django.utils import timezone
from api.models import RawMessage

admin_user, _ = User.objects.get_or_create(username='admin', defaults={'is_staff': True, 'is_superuser': True})
admin_user.is_staff = True
admin_user.is_superuser = True
admin_user.set_password('admin2026')
admin_user.save()

now = timezone.now()
RawMessage.objects.get_or_create(
    message_id='e2e_msg_processed_01',
    defaults={
        'sender_name': 'E2E Тест Обработано',
        'sender_phone': '+77011112233',
        'content': 'Тестовое обработанное сообщение для проверки админки',
        'timestamp': now,
        'processed': True,
    }
)
RawMessage.objects.get_or_create(
    message_id='e2e_msg_pending_02',
    defaults={
        'sender_name': 'E2E Тест Ожидает',
        'sender_phone': '+77014445566',
        'content': 'Тестовое ожидающее сообщение для проверки админки',
        'timestamp': now,
        'processed': False,
    }
)
"`
    execSync(setupCmd, { stdio: 'inherit' })
  })

  test('Рендеринг /admin/api/rawmessage/ без ошибки 500 / TypeError format_html', async ({ page }) => {
    // 1. Переходим на страницу логина админки
    await page.goto('/admin/login/?next=/admin/api/rawmessage/')
    
    // Если требуется логин — заполняем учетные данные admin
    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('admin2026')
      await page.locator('button[type="submit"], input[type="submit"]').click()
    }

    // 2. Ожидаем загрузки страницы /admin/api/rawmessage/
    await page.waitForURL('**/admin/api/rawmessage/**', { timeout: 15_000 })
    
    // Проверяем, что нет ошибки 500 или TypeError
    await expect(page.locator('text=TypeError')).toHaveCount(0)
    await expect(page.locator('text=args or kwargs must be provided')).toHaveCount(0)

    // 3. Проверяем наличие таблицы и статусов обработки
    await expect(page.locator('text=✓ Обработано').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Ожидает').first()).toBeVisible({ timeout: 10_000 })
  })
})
