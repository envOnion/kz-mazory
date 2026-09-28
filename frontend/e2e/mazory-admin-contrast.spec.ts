import { test, expect } from '@playwright/test'
import { execSync } from 'child_process'

test.describe('Admin Text Contrast — E2E Верификация контрастности блоков трассировки', () => {
  let seededTraceId: number | null = null

  test.beforeAll(() => {
    // Подготовка тестовых данных: суперпользователь admin и трассировка
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

company, _ = Company.objects.get_or_create(name='ТОО СтройКонтраст E2E')
u, _ = User.objects.get_or_create(username='77019998877')
manager, _ = UserProfile.objects.get_or_create(user=u, defaults={'phone': '+77019998877', 'full_name': 'Тестовый Менеджер'})

Project.objects.filter(bitrix_id='888777').delete()
p = Project.objects.create(
    name='ЖК Контраст-Тест БМК',
    contract_number='KT-2026',
    status='in_execution',
    contract_amount=Decimal('45000000.00'),
    company=company,
    manager=manager,
    bitrix_id='888777',
    needs_bitrix_sync=False,
    is_verified=True,
)

RawMessage.objects.filter(message_id='e2e_contrast_msg_01').delete()
msg = RawMessage.objects.create(
    message_id='e2e_contrast_msg_01',
    sender_name='Тестовый Заказчик',
    sender_phone='+77019998877',
    content='Прошу подтвердить поставку котельного оборудования на объекте ЖК Контраст-Тест',
    timestamp=timezone.now(),
    processed=True,
    raw_payload={
        'id': 'e2e_contrast_msg_01',
        'from': '77019998877@c.us',
        'body': 'Прошу подтвердить поставку котельного оборудования на объекте ЖК Контраст-Тест',
    }
)

MessageProcessingTrace.objects.filter(whatsapp_message_id='e2e_contrast_msg_01').delete()
trace = MessageProcessingTrace.objects.create(
    raw_message=msg,
    project=p,
    whatsapp_message_id=msg.message_id,
    whatsapp_sender_name=msg.sender_name,
    whatsapp_sender_phone=msg.sender_phone,
    whatsapp_chat_id='77019998877@c.us',
    whatsapp_content=msg.content,
    whatsapp_raw_payload=msg.raw_payload,
    earlier_messages_context=[
        {
            'author': 'Менеджер Тест',
            'text': 'Согласовали предварительные ТУ по ЖК Контраст-Тест',
            'score': 0.92,
            'timestamp': '2026-09-28T01:00:00Z',
        }
    ],
    earlier_messages_count=1,
    bitrix_search_query='ЖК Контраст-Тест',
    bitrix_matched_deal_id='888777',
    bitrix_deal_title='ЖК Контраст-Тест БМК',
    bitrix_deal_stage='EXECUTING',
    bitrix_deal_opportunity=Decimal('45000000.00'),
    bitrix_company_data={'TITLE': 'ТОО СтройКонтраст E2E'},
    bitrix_raw_deal={'ID': '888777', 'TITLE': 'ЖК Контраст-Тест БМК'},
    ai_extracted_facts={
        'object_name': 'ЖК Контраст-Тест БМК',
        'contract_amount': 45000000.0,
        'stage': 'in_execution',
        'direction': 'БТП',
        'next_action': 'Контроль отгрузки',
    },
    pipeline_action='created_deal',
    status='success',
    result_summary='✓ Этап 1: Получено входящее сообщение WhatsApp\\n✓ Этап 2: Найдено 1 контекстное сообщение\\n✓ Этап 3: Найдена связанная сделка Bitrix24 #888777\\n✓ Этап 4: Зарегистрирована сделка ЖК Контраст-Тест БМК',
)
print('SEEDED_TRACE_ID:', trace.id)
`
    const cmds = [
      'docker compose -f ../docker-compose.yml exec -T backend python manage.py shell',
      'docker compose exec -T backend python manage.py shell',
      'ssh a_belianskii@79.108.164.63 "docker exec -i mazory-backend python manage.py shell"',
    ]
    for (const cmd of cmds) {
      try {
        const out = execSync(cmd, { input: pyCode, stdio: ['pipe', 'pipe', 'pipe'] }).toString()
        const m = out.match(/SEEDED_TRACE_ID:\s*(\d+)/)
        if (m) {
          seededTraceId = parseInt(m[1], 10)
        }
        break
      } catch {
        // try next
      }
    }
  })

  test.afterAll(() => {
    const cleanupPy = `
from api.models import Project, MessageProcessingTrace, RawMessage
MessageProcessingTrace.objects.filter(whatsapp_message_id='e2e_contrast_msg_01').delete()
RawMessage.objects.filter(message_id='e2e_contrast_msg_01').delete()
Project.objects.filter(bitrix_id='888777').delete()
`
    const cmds = [
      'docker compose -f ../docker-compose.yml exec -T backend python manage.py shell',
      'docker compose exec -T backend python manage.py shell',
      'ssh a_belianskii@79.108.164.63 "docker exec -i mazory-backend python manage.py shell"',
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

  test('1. Текст исходного сообщения в блоке трассировки имеет высокую контрастность и четкую читаемость', async ({ page }) => {
    // Авторизация
    const targetUrl = seededTraceId
      ? `/admin/api/messageprocessingtrace/${seededTraceId}/change/`
      : '/admin/api/messageprocessingtrace/'
    await page.goto(`/admin/login/?next=${targetUrl}`)

    if (page.url().includes('/admin/login/')) {
      await page.locator('input[name="username"]').fill('admin')
      // Пробуем актуальный пароль
      await page.locator('input[name="password"]').fill('Mazory2026Admin!')
      await page.locator('button[type="submit"], input[type="submit"]').click()
      await page.waitForTimeout(1000)

      if (page.url().includes('/admin/login/')) {
        // fallback на admin2026
        await page.locator('input[name="username"]').fill('admin')
        await page.locator('input[name="password"]').fill('admin2026')
        await page.locator('button[type="submit"], input[type="submit"]').click()
      }
    }

    // Если перешли на список, переходим на первую запись
    if (page.url().includes('/admin/api/messageprocessingtrace/') && !page.url().includes('/change/')) {
      const firstRowLink = page.locator('tbody tr th a, tbody tr td a').first()
      await firstRowLink.click()
    }

    await page.waitForURL('**/admin/api/messageprocessingtrace/*/change/**', { timeout: 15_000 })

    // Проверяем наличие блока исходного сообщения WhatsApp
    const whatsappBlock = page.locator('.mazory-trace-content').first()
    await expect(whatsappBlock).toBeVisible({ timeout: 10_000 })

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
})
