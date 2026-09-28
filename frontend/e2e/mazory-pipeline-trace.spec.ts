import { test, expect } from '@playwright/test'
import { execSync } from 'child_process'

test.describe('WhatsApp Pipeline Trace — E2E Сквозные сценарии Django Admin (4 этапа)', () => {
  test.beforeAll(() => {
    // Подготовка тестовых данных: администратор, RawMessage, Project и MessageProcessingTrace
    const pyCode = `
from django.contrib.auth.models import User
from django.utils import timezone
from decimal import Decimal
from api.models import UserProfile, Company, Project, RawMessage, MessageProcessingTrace

admin_user, _ = User.objects.get_or_create(username='admin', defaults={'is_staff': True, 'is_superuser': True})
admin_user.is_staff = True
admin_user.is_superuser = True
admin_user.set_password('Mazory2026Admin!')
admin_user.save()

company, _ = Company.objects.get_or_create(name='ТОО СтройХолдинг E2E')
u, _ = User.objects.get_or_create(username='77015556677')
manager, _ = UserProfile.objects.get_or_create(user=u, defaults={'phone': '+77015556677', 'full_name': 'Ерлан Асанов E2E'})

Project.objects.filter(bitrix_id='999555').delete()
Project.objects.filter(name='ЖК Highvill E2E Pipeline').delete()
p = Project.objects.create(
    name='ЖК Highvill E2E Pipeline',
    normalized_name='highvill e2e pipeline',
    contract_number='HV-2026',
    status='in_execution',
    contract_amount=Decimal('65000000.00'),
    company=company,
    manager=manager,
    bitrix_id='999555',
    needs_bitrix_sync=False,
    is_verified=True,
)

RawMessage.objects.filter(message_id='e2e_trace_msg_01').delete()
msg = RawMessage.objects.create(
    message_id='e2e_trace_msg_01',
    sender_name='Ерлан Асанов',
    sender_phone='+77015556677',
    content='По ЖК Highvill E2E Pipeline подтвердили договор на 65 000 000 ₸',
    timestamp=timezone.now(),
    processed=True,
    raw_payload={
        'id': 'e2e_trace_msg_01',
        'from': '77015556677@c.us',
        'senderName': 'Ерлан Асанов',
        'body': 'По ЖК Highvill E2E Pipeline подтвердили договор на 65 000 000 ₸',
    }
)

MessageProcessingTrace.objects.filter(whatsapp_message_id='e2e_trace_msg_01').delete()
trace = MessageProcessingTrace.objects.create(
    raw_message=msg,
    project=p,
    whatsapp_message_id=msg.message_id,
    whatsapp_sender_name=msg.sender_name,
    whatsapp_sender_phone=msg.sender_phone,
    whatsapp_chat_id='77015556677@c.us',
    whatsapp_content=msg.content,
    whatsapp_raw_payload=msg.raw_payload,
    earlier_messages_context=[
        {
            'author': 'Менеджер Азамат',
            'text': 'Ждем подтверждения сметы по блоку А',
            'score': 0.89,
            'timestamp': '2026-09-27T12:00:00Z',
        }
    ],
    earlier_messages_count=1,
    bitrix_search_query='ЖК Highvill E2E',
    bitrix_matched_deal_id='999555',
    bitrix_deal_title='ЖК Highvill E2E Pipeline',
    bitrix_deal_stage='EXECUTING',
    bitrix_deal_opportunity=Decimal('65000000.00'),
    bitrix_company_data={'TITLE': 'ТОО СтройХолдинг E2E'},
    bitrix_raw_deal={'ID': '999555', 'TITLE': 'ЖК Highvill E2E Pipeline', 'STAGE_ID': 'EXECUTING'},
    ai_extracted_facts={
        'object_name': 'ЖК Highvill E2E Pipeline',
        'contract_amount': 65000000.0,
        'stage': 'in_execution',
        'direction': 'БТП',
        'next_action': 'Подписание акта приема',
    },
    pipeline_action='created_deal',
    status='success',
    result_summary='✓ Этап 1: Получено входящее сообщение WhatsApp от Ерлан Асанов\\n✓ Этап 2: Найдено 1 контекстное сообщение (сходство 0.89)\\n✓ Этап 3: Найдена связанная сделка Bitrix24 #999555\\n✓ Этап 4: Зарегистрирована сделка ЖК Highvill E2E Pipeline (65 000 000 ₸)',
)
print('TRACE_SEED_SUCCESS_ID:', trace.id)
`
    const cmds = [
      'docker compose -f ../docker-compose.yml exec -T backend python manage.py shell',
      'docker compose exec -T backend python manage.py shell',
      'ssh a_belianskii@192.168.0.193 "cd ~/projects/kz-mazory && docker compose exec -T backend python manage.py shell"',
    ]
    for (const cmd of cmds) {
      try {
        execSync(cmd, { input: pyCode, stdio: ['pipe', 'pipe', 'pipe'] })
        break
      } catch {
        // try next
      }
    }
  })

  test.afterAll(() => {
    const cleanupPy = `
from api.models import Project, MessageProcessingTrace, RawMessage
MessageProcessingTrace.objects.filter(whatsapp_message_id='e2e_trace_msg_01').delete()
RawMessage.objects.filter(message_id='e2e_trace_msg_01').delete()
Project.objects.filter(bitrix_id='999555').delete()
`
    const cmds = [
      'docker compose -f ../docker-compose.yml exec -T backend python manage.py shell',
      'docker compose exec -T backend python manage.py shell',
    ]
    for (const cmd of cmds) {
      try {
        execSync(cmd, { input: cleanupPy, stdio: ['pipe', 'pipe', 'pipe'] })
        break
      } catch {
        // ignore
      }
    }
  })

  test('1. Раздел трассировки в Django Admin: список, бейджи 4 этапов и фильтры', async ({ page }) => {
    // 1. Переходим на страницу логина админки с редиректом на список трассировок
    await page.goto('/admin/login/?next=/admin/api/messageprocessingtrace/')

    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('Mazory2026Admin!')
      await page.locator('button[type="submit"], input[type="submit"]').click()
      await page.waitForTimeout(1000)
      if (page.url().includes('/admin/login/')) {
        await page.locator('input[name="username"]').fill('admin')
        await page.locator('input[name="password"]').fill('admin2026')
        await page.locator('button[type="submit"], input[type="submit"]').click()
      }
    }

    // 2. Ожидаем загрузки списка /admin/api/messageprocessingtrace/
    await page.waitForURL('**/admin/api/messageprocessingtrace/**', { timeout: 15_000 })

    // Проверяем отсутствие 500 и ошибок форматирования
    await expect(page.locator('text=TypeError')).toHaveCount(0)
    await expect(page.locator('text=Server Error (500)')).toHaveCount(0)

    // 3. Проверяем колонки и отображение 4 этапов в таблице
    await expect(page.locator('text=Ерлан Асанов').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=🔍 1 сондай').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=CRM #999555').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=✓ Создана сделка').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Успешно').first()).toBeVisible({ timeout: 10_000 })
  })

  test('2. Детальная карточка трассировки: визуальный 4-шаговый пайплайн и подробные блоки', async ({ page }) => {
    await page.goto('/admin/api/messageprocessingtrace/')

    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('admin2026')
      await page.locator('button[type="submit"], input[type="submit"]').click()
      await page.waitForURL('**/admin/api/messageprocessingtrace/**', { timeout: 15_000 })
    }

    // Кликаем по первой записи трассировки
    const rowLink = page.locator('table tr:has-text("Ерлан Асанов") a[href*="/change/"]').first()
    await rowLink.click()

    await page.waitForURL('**/admin/api/messageprocessingtrace/*/change/**', { timeout: 15_000 })

    // Проверяем интерактивный 4-шаговый баннер пайплайна
    await expect(page.locator('text=1. Входные данные WhatsApp').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=2. Зависимые данные из сообщений ранее').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=3. Зависимые данные из Bitrix24').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=4. Итоговая запись').first()).toBeVisible({ timeout: 10_000 })

    // Проверяем детальные карточки 4 этапов:
    // Этап 1: Отправитель, телефон, текст
    await expect(page.locator('text=+77015556677').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=По ЖК Highvill E2E Pipeline подтвердили договор').first()).toBeVisible({ timeout: 10_000 })

    // Этап 2: Семантический контекст
    await expect(page.locator('text=Менеджер Азамат').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Ждем подтверждения сметы').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=89.0%').first()).toBeVisible({ timeout: 10_000 })

    // Этап 3: CRM данные Bitrix24
    await expect(page.locator('text=#999555').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=ТОО СтройХолдинг E2E').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=65,000,000').first()).toBeVisible({ timeout: 10_000 })

    // Этап 4: Итоговая сделка и факты
    await expect(page.locator('text=ЖК Highvill E2E Pipeline').first()).toBeVisible({ timeout: 10_000 })
  })

  test('3. Связка из списка RawMessage: отображение ссылки на цепочку трассировки', async ({ page }) => {
    await page.goto('/admin/api/rawmessage/')

    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      await page.locator('input[name="password"]').fill('admin2026')
      await page.locator('button[type="submit"], input[type="submit"]').click()
      await page.waitForURL('**/admin/api/rawmessage/**', { timeout: 15_000 })
    }

    // Проверяем наличие кнопки/ссылки на цепочку трассировки в таблице
    const traceBtn = page.locator('a:has-text("Цепочка")').first()
    await expect(traceBtn).toBeVisible({ timeout: 10_000 })

    // Кликаем по ссылке перехода в трассировку
    await traceBtn.click()
    await page.waitForURL('**/admin/api/messageprocessingtrace/*/change/**', { timeout: 15_000 })

    // Должна открыться детальная страница трассировки
    await expect(page.locator('text=1. Входные данные WhatsApp').first()).toBeVisible({ timeout: 10_000 })
  })
})
