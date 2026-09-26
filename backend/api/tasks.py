import re
import logging
import requests
from decimal import Decimal
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django.db.models import Q
from django_q.tasks import async_task
from .deduplication import normalize_deal_name, get_deal_lock

logger = logging.getLogger(__name__)

def clean_phone_number(phone: str) -> str:
    """Нормализация телефонного номера к цифрам без знаков."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits

def send_waha_whatsapp_message_task(phone_or_group: str, text: str, session: str = "default"):
    """
    Отправка WhatsApp сообщения через локальный контейнер WAHA (devlikeapro/waha).
    """
    from .models import WhatsAppConfig
    cfg = WhatsAppConfig.get_active()
    
    clean_target = phone_or_group
    if not clean_target.endswith('@g.us') and not clean_target.endswith('@c.us'):
        clean_target = f"{clean_phone_number(phone_or_group)}@c.us"
        
    waha_url = f"{cfg.waha_api_url.rstrip('/')}/api/sendText"
    payload = {
        "chatId": clean_target,
        "text": text,
        "session": cfg.session_name or session or "default"
    }
    
    headers = {"Content-Type": "application/json"}
    if cfg.waha_api_key:
        headers["X-Api-Key"] = cfg.waha_api_key

    try:
        response = requests.post(waha_url, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            return {"status": "delivered", "response": response.json()}
        return {"status": "error", "code": response.status_code, "detail": response.text}
    except Exception as exc:
        logger.error("Failed to send WhatsApp message via WAHA: %s", exc)
        return {"status": "error", "detail": str(exc)}

def send_sms_verification_code_task(phone: str, code: str):
    """
    Фоновый воркер Django Q2: Отправка случайного 4-значного OTP-кода подтверждения через WhatsApp (WAHA).
    """
    clean = clean_phone_number(phone)
    text = f"Ваш код подтверждения для входа в Mazory AI: {code}\nКод действителен 5 минут."
    logger.info("Отправка OTP-кода подтверждения на номер %s через WAHA", clean)
    return send_waha_whatsapp_message_task(clean, text)

def create_bitrix_deal_task(project_id: int):
    """
    Асинхронный воркер Django Q2: Регистрация новой сделки в Bitrix24.
    Выполняется изолированно, исключая блокировку вебхуков.
    """
    from .models import Project, BusinessEvent
    from .bitrix_service import BitrixService

    try:
        project = Project.objects.get(id=project_id)
    except Project.DoesNotExist:
        return {"status": "not_found", "project_id": project_id}

    if not project.is_verified:
        logger.info("Project #%d '%s' is not verified yet. Postponing Bitrix deal creation.", project.id, project.name)
        return {"status": "unverified_skipped", "project_id": project.id}

    if project.bitrix_id:
        return {"status": "already_has_bitrix_id", "bitrix_id": project.bitrix_id}

    bitrix_id = BitrixService.create_deal({
        "name": project.name,
        "contract_amount": project.contract_amount,
        "direction": project.equipment_type,
        "deal_period": project.deal_period,
        "status": project.status,
        "current_action": project.current_action,
        "next_action": project.next_action,
        "assigned_by_id": project.manager.bitrix_user_id if project.manager and project.manager.bitrix_user_id else None
    }, project=project, triggered_by="qcluster_create_deal_task")

    if bitrix_id:
        project.bitrix_id = bitrix_id
        project.last_bitrix_synced_at = timezone.now()
        project.needs_bitrix_sync = False
        project.save(update_fields=['bitrix_id', 'last_bitrix_synced_at', 'needs_bitrix_sync'])
        logger.info("Project #%d '%s' linked to Bitrix24 deal #%s", project.id, project.name, bitrix_id)
        return {"status": "created", "bitrix_id": bitrix_id}

    return {"status": "failed_to_create_in_bitrix"}

def sync_single_deal_to_bitrix_task(project_id: int):
    """
    Атомарная асинхронная задача в очереди Redis воркеров qcluster:
    1. Обновляет сделку в Bitrix24 (crm.deal.update).
    2. Добавляет саммари в таймлайн (crm.timeline.comment.add).
    3. Создает задачи по выявленным дедлайнам (tasks.task.add).
    4. Сбрасывает флаг needs_bitrix_sync.
    """
    from .models import Project, BitrixSettings
    from .bitrix_service import BitrixService

    cfg = BitrixSettings.get_active()
    if not cfg.is_active:
        return {"status": "bitrix_integration_disabled"}

    try:
        project = Project.objects.get(id=project_id)
    except Project.DoesNotExist:
        return {"status": "project_not_found", "project_id": project_id}

    if not project.is_verified:
        logger.info("Project #%d '%s' is not verified yet. Postponing Bitrix sync.", project.id, project.name)
        return {"status": "unverified_skipped", "project_id": project.id}

    # Если сделка еще не зарегистрирована в Bitrix24, создаем её
    if not project.bitrix_id:
        return create_bitrix_deal_task(project_id)

    deal_id = project.bitrix_id

    # 1. Обновление полей сделки (R3: санитизация HTML в текстовых полях)
    clean_action = BitrixService._strip_html(project.current_action or '')
    clean_next = BitrixService._strip_html(project.next_action or '')
    clean_blocker = BitrixService._strip_html(project.blocker or '')
    update_fields = {
        "OPPORTUNITY": float(project.contract_amount or 0.0),
        "STAGE_ID": BitrixService.status_to_stage(project.status),
        "UF_CRM_1731131779572": project.name,
        "UF_CRM_1778166248543": project.equipment_type or "",
        "UF_CRM_1778164670507": project.deal_period or "",
        "COMMENTS": (
            f"<b>Актуальное состояние от Mazory AI:</b><br>"
            f"Статус: {project.get_status_display()}<br>"
            f"Текущее действие: {clean_action or '—'}<br>"
            f"Следующий шаг: {clean_next or '—'}<br>"
            f"Блокер: {clean_blocker or '—'}"
        )
    }
    BitrixService.update_deal(deal_id, update_fields, project=project, triggered_by="qcluster_sync_single_deal_task")

    # 2. Публикация сводки в таймлайн
    if cfg.sync_timeline_comments and (project.current_action or project.next_action or project.blocker):
        next_deadline_str = f" (срок: {project.next_action_at.strftime('%d.%m.%Y')})" if project.next_action_at else ""
        comment = (
            f"🤖 <b>[Mazory AI] Сводка из WhatsApp за прошедший час:</b><br>"
            f"• <b>Последнее действие:</b> {project.current_action or '—'}<br>"
            f"• <b>Следующий шаг:</b> {project.next_action or '—'}{next_deadline_str}<br>"
            f"• <b>Блокер / Риск:</b> {project.blocker or 'Отсутствует'}<br>"
            f"• <b>Оплачено:</b> {project.paid_amount:,.2f} ₸ (Остаток к сбору: {project.due_amount:,.2f} ₸)"
        )
        BitrixService.add_timeline_comment(deal_id, comment)

    # 3. Постановка задач в Bitrix24 по несинхронизированным обязательствам (только проверенные)
    created_tasks_count = 0
    if cfg.auto_create_tasks:
        unassigned_commitments = project.commitments.filter(is_verified=True, bitrix_task_id__isnull=True, status='pending')
        for comm in unassigned_commitments:
            deadline_iso = comm.deadline.isoformat() + "T18:00:00+05:00" if comm.deadline else None
            resp_id = None
            if comm.manager and comm.manager.bitrix_user_id and comm.manager.bitrix_user_id.isdigit():
                resp_id = int(comm.manager.bitrix_user_id)

            task_title = f"[Mazory] {comm.commitment_text[:90]}"
            task_desc = (
                f"<b>Обязательство зафиксировано из переписки WhatsApp</b><br>"
                f"Объект: {project.name}<br>"
                f"Суть задачи: {comm.commitment_text}<br>"
                f"Дедлайн: {comm.deadline or 'Не указан'}<br>"
                f"Срочность: {comm.get_severity_display()}"
            )
            task_id = BitrixService.create_task(
                title=task_title,
                description=task_desc,
                deadline_iso=deadline_iso,
                responsible_id=resp_id,
                deal_id=deal_id
            )
            if task_id:
                comm.bitrix_task_id = task_id
                comm.save(update_fields=['bitrix_task_id'])
                created_tasks_count += 1

    # 4. Сброс флага
    project.needs_bitrix_sync = False
    project.last_bitrix_synced_at = timezone.now()
    project.save(update_fields=['needs_bitrix_sync', 'last_bitrix_synced_at'])

    return {
        "status": "synced",
        "project_id": project.id,
        "bitrix_id": deal_id,
        "created_tasks_count": created_tasks_count
    }

def import_single_deal_from_bitrix_task(deal_id: str):
    """
    Асинхронный воркер импорта сделки из Bitrix24 (например, по вебхуку ONCRMDEALADD).
    """
    from .bitrix_service import BitrixService
    deal_data = BitrixService.get_deal(deal_id)
    if deal_data:
        proj = BitrixService.import_or_update_deal_from_bitrix(deal_data)
        return {"status": "imported", "project_id": proj.id if proj else None, "deal_id": deal_id}
    return {"status": "not_found", "deal_id": deal_id}

def enqueue_hourly_bitrix_sync_task():
    """
    Диспетчер периодической синхронизации (раз в час через Schedule.HOURLY):
    1. Reconciliation loop: сверка и подтягивание новых/измененных сделок из Bitrix24.
    2. Поиск всех Project с needs_bitrix_sync=True и раскладка по очереди Redis.
    """
    from .models import Project, BitrixSettings
    from .bitrix_service import BitrixService

    cfg = BitrixSettings.get_active()
    if not cfg.is_active or not cfg.hourly_sync_enabled:
        logger.info("Bitrix hourly sync is disabled in settings. Skipping.")
        return {"status": "disabled"}

    logger.info("Starting hourly Bitrix CRM sync dispatcher...")

    # 1. Страховочный PULL новых/измененных сделок из CRM
    imported_from_crm = 0
    if cfg.auto_import_deals:
        try:
            # Получаем свежие сделки из Bitrix24 (последние 50)
            deals = BitrixService.fetch_all_paged("crm.deal.list", {
                "order": {"DATE_MODIFY": "DESC"},
                "select": ["ID", "TITLE", "OPPORTUNITY", "STAGE_ID", "COMPANY_ID", "ASSIGNED_BY_ID",
                           "UF_CRM_1778164670507", "UF_CRM_1731131779572", "UF_CRM_1778166248543",
                           "DATE_CREATE", "MODIFY_BY_ID"],
                "limit": 50
            })
            for d in deals:
                bx_id = str(d.get("ID"))
                # Если такой сделки нет у нас в базе - импортируем
                if not Project.objects.filter(bitrix_id=bx_id).exists():
                    BitrixService.import_or_update_deal_from_bitrix(d)
                    imported_from_crm += 1
        except Exception as e:
            logger.error("Failed to pull modified deals from Bitrix24: %s", e)

    # 2. PUSH накопленных обновлений из чата в Bitrix24 (только проверенные сделки)
    pending_ids = list(Project.objects.filter(needs_bitrix_sync=True, is_verified=True).values_list('id', flat=True))
    for pid in pending_ids:
        async_task('api.tasks.sync_single_deal_to_bitrix_task', pid)

    cfg.last_hourly_sync_at = timezone.now()
    cfg.last_sync_status = f"Успешно: импортировано {imported_from_crm} сделок из CRM, отправлено {len(pending_ids)} задач в очередь Redis"
    cfg.save(update_fields=['last_hourly_sync_at', 'last_sync_status'])

    logger.info("Hourly sync dispatcher completed. Enqueued %d deals, imported %d from CRM.", len(pending_ids), imported_from_crm)
    return {
        "status": "enqueued",
        "pending_deals_count": len(pending_ids),
        "imported_from_crm_count": imported_from_crm
    }

def deduplicate_bitrix_deals_task(dry_run: bool = False):
    """
    Фоновая задача очистки дубликатов в Bitrix24.
    """
    from .bitrix_service import BitrixService
    return BitrixService.clean_duplicate_deals(dry_run=dry_run)

def setup_hourly_schedule():
    """
    Автоматическая регистрация расписаний запуска в Django Q2.
    """
    try:
        from django_q.models import Schedule
        sched, created = Schedule.objects.get_or_create(
            name="hourly_bitrix_crm_sync",
            defaults={
                "func": "api.tasks.enqueue_hourly_bitrix_sync_task",
                "schedule_type": Schedule.HOURLY,
                "repeats": -1,
            }
        )
        if created:
            logger.info("Schedule 'hourly_bitrix_crm_sync' successfully registered in Django Q2.")

        sched_alerts, created_alerts = Schedule.objects.get_or_create(
            name="hourly_kpi_risk_monitoring",
            defaults={
                "func": "api.tasks.monitor_kpi_risks_and_anomalies_task",
                "schedule_type": Schedule.HOURLY,
                "repeats": -1,
            }
        )
        if created_alerts:
            logger.info("Schedule 'hourly_kpi_risk_monitoring' successfully registered in Django Q2.")
    except Exception as exc:
        logger.warning("Could not auto-register hourly schedule: %s", exc)


def monitor_kpi_risks_and_anomalies_task():
    """
    Фоновая периодическая задача Django Q2:
    Предиктивный мониторинг финансовых и процессных рисков:
    1. Дебиторская задолженность свыше 50 млн ₸.
    2. Фактическая маржинальность ниже критического порога (< 15.0%).
    3. Просроченные обязательства и дедлайны по контрольным точкам.
    
    Для исключения спама используется Redis-дедупликация на 24 часа.
    Алерты доставляются в NotificationsPopover.vue через push_notification_to_redis.
    """
    from django.core.cache import cache
    from django.contrib.auth.models import User
    from .models import Project, Commitment, BusinessEvent
    from .notifications import push_notification_to_redis

    today = timezone.now().date()
    today_str = today.isoformat()
    generated_alerts = []

    all_users = list(User.objects.values_list('username', flat=True))
    if not all_users:
        logger.warning("No users found to dispatch risk alerts.")
        return {"status": "no_users", "alerts_count": 0}

    # 1. Проверка крупных задолженностей (> 50 млн ₸) (только проверенные)
    high_debt_projects = Project.objects.filter(
        is_verified=True,
        due_amount__gte=Decimal('50000000.00')
    ).select_related('company', 'manager')

    for proj in high_debt_projects:
        cache_key = f"mazory:risk_alert:debt:{proj.id}:{today_str}"
        if not cache.get(cache_key):
            title = f"⚠️ Высокая дебиторка: {proj.name}"
            msg = (
                f"Задолженность по объекту составляет {float(proj.due_amount):,.0f} ₸. "
                f"Менеджер: {proj.manager.full_name if proj.manager else 'Не назначен'}. "
                f"Требуется согласование плана платежей."
            ).replace(',', ' ')
            
            BusinessEvent.objects.create(
                event_type="high_debt",
                project=proj,
                manager=proj.manager,
                title=title,
                description=msg,
                severity="warning"
            )

            for phone in all_users:
                push_notification_to_redis(phone, title, msg, notif_type="warning")

            cache.set(cache_key, True, timeout=86400)
            generated_alerts.append(title)

    # 2. Проверка проектов с низкой маржинальностью (< 15%) (только проверенные)
    low_margin_projects = Project.objects.filter(
        is_verified=True,
        actual_margin_percent__lt=Decimal('15.00'),
        contract_amount__gt=Decimal('0.00')
    ).select_related('company', 'manager')

    for proj in low_margin_projects:
        cache_key = f"mazory:risk_alert:margin:{proj.id}:{today_str}"
        if not cache.get(cache_key):
            title = f"📉 Низкая маржинальность: {proj.name}"
            msg = (
                f"Фактическая маржа {float(proj.actual_margin_percent):.1f}% упала ниже порога 15%. "
                f"Сумма договора: {float(proj.contract_amount):,.0f} ₸, себестоимость: {float(proj.cost_amount):,.0f} ₸. "
                f"Любые допработы требуют визы генерального директора."
            ).replace(',', ' ')

            BusinessEvent.objects.create(
                event_type="low_margin",
                project=proj,
                manager=proj.manager,
                title=title,
                description=msg,
                severity="warning"
            )

            for phone in all_users:
                push_notification_to_redis(phone, title, msg, notif_type="warning")

            cache.set(cache_key, True, timeout=86400)
            generated_alerts.append(title)

    # 3. Просроченные обязательства и дедлайны (только проверенные)
    overdue_commitments = Commitment.objects.filter(
        is_verified=True
    ).filter(
        Q(status='overdue') | Q(status='pending', deadline__lt=today)
    ).select_related('manager', 'project')

    for comm in overdue_commitments[:10]:
        cache_key = f"mazory:risk_alert:commitment:{comm.id}:{today_str}"
        if not cache.get(cache_key):
            proj_name = comm.project.name if comm.project else "Общая задача"
            title = f"⏰ Срыв дедлайна: {proj_name}"
            msg = (
                f"Обязательство '{comm.commitment_text}' "
                f"({comm.counterparty_person or 'Контрагент'}) "
                f"просрочено. Ответственный: {comm.manager.full_name if comm.manager else 'Отдел продаж'}."
            )

            BusinessEvent.objects.create(
                event_type="overdue_deadline",
                project=comm.project,
                manager=comm.manager,
                title=title,
                description=msg,
                severity="urgent"
            )

            for phone in all_users:
                push_notification_to_redis(phone, title, msg, notif_type="urgent")

            cache.set(cache_key, True, timeout=86400)
            generated_alerts.append(title)

    logger.info("KPI risk monitoring task completed. Generated alerts: %d", len(generated_alerts))
    return {
        "status": "success",
        "generated_alerts_count": len(generated_alerts),
        "alerts": generated_alerts
    }

def process_incoming_message_task(message_data: dict):
    """
    Фоновый воркер Django Q2 (Event: Новое сообщение WhatsApp):
    1. Сохраняет RawMessage в PostgreSQL.
    2. Векторизует через embeddings-модель и сохраняет в Qdrant.
    3. Выполняет семантический RAG-поиск в Qdrant.
    4. Запускает чат-модель с контекстом.
    5. Квалифицирует сделку с ОБЯЗАТЕЛЬНОЙ защитой от дублей:
       - Redis Lock по нормализованному названию.
       - Поиск в локальной БД Mazory.
       - ОБЯЗАТЕЛЬНЫЙ поиск в Bitrix24 CRM перед созданием новой!
       - Если найдена в CRM — связывает и обновляет, НОВУЮ НЕ СОЗДАЕТ!
    6. Обновляет обязательства (Commitment) и платежи (FinancialRecord).
    """
    from .models import (
        RawMessage, Project, Commitment, FinancialRecord,
        UserProfile, Company, BusinessEvent
    )
    from .qdrant_service import qdrant_service
    from .ai_service import AIService
    from .bitrix_service import BitrixService
    from .notifications import push_notification_to_redis

    message_id = message_data.get('message_id') or f"waha-{int(timezone.now().timestamp() * 1000)}"
    content = message_data.get('content', '').strip()
    sender_name = message_data.get('sender_name', 'Коллега')
    sender_phone = clean_phone_number(message_data.get('sender_phone', ''))
    chat_id = message_data.get('chat_id', 'aquakip-sales')

    if not content:
        return {"status": "empty_content"}

    # 1. Сохранение сырого сообщения
    raw_msg, _ = RawMessage.objects.get_or_create(
        message_id=message_id,
        defaults={
            "chat_id": chat_id,
            "sender_phone": sender_phone,
            "sender_name": sender_name,
            "timestamp": timezone.now(),
            "content": content,
            "raw_payload": message_data,
            "processed": False
        }
    )

    # 2. Векторизация и сохранение в Qdrant
    point_id = qdrant_service.upsert_message(
        message_id=message_id,
        content=content,
        payload={
            "sender_name": sender_name,
            "sender_phone": sender_phone,
            "chat_id": chat_id,
            "timestamp": timezone.now().isoformat()
        }
    )
    if point_id:
        raw_msg.qdrant_point_id = point_id

    # 3. Семантический RAG-поиск близких сообщений в Qdrant
    similar_messages = qdrant_service.search_similar(content, limit=5)
    
    # Сводка существующих сделок для контекста LLM
    existing_deals = list(Project.objects.values('name', 'status', 'contract_amount', 'company__name')[:30])
    known_deals_summary = "\n".join([
        f"- {d['name']} (Компания: {d['company__name'] or 'Не указана'}, Сумма: {d['contract_amount']} ₸, Статус: {d['status']})"
        for d in existing_deals
    ]) if existing_deals else "(База сделок пока пуста)"

    # 4. Анализ большой чат-моделью OpenRouter
    facts = AIService.analyze_message_with_context(
        content=content,
        sender_name=sender_name,
        context_messages=similar_messages,
        known_deals_summary=known_deals_summary
    )

    logger.info("AI Analysis for message %s: %s", message_id, facts)

    # 5. Обработка сущностей и обновление CRM с защитой от дублей
    object_name = facts.get("object_name")
    company_name = facts.get("company_name")
    responsible_name = facts.get("responsible_name") or sender_name

    # Менеджер
    manager = None
    if responsible_name:
        manager = UserProfile.objects.filter(
            Q(full_name__icontains=responsible_name) |
            Q(phone__icontains=sender_phone)
        ).first()

    # Компания
    company = None
    if company_name and company_name.strip():
        company, _ = Company.objects.get_or_create(
            name=company_name.strip(),
            defaults={"client_type": "private"}
        )

    # Проект / Сделка
    project = None
    if object_name and object_name.strip():
        clean_obj_name = object_name.strip()
        core_name = normalize_deal_name(clean_obj_name) or clean_obj_name

        # Блокировка Redis для предотвращения race condition
        with get_deal_lock(core_name):
            # Шаг 1: Поиск в локальной БД Mazory
            project = Project.objects.filter(normalized_name=core_name).first()
            if not project:
                project = Project.objects.filter(name__icontains=clean_obj_name).first()

            # Шаг 2: Если в локальной БД сделки нет — ОБЯЗАТЕЛЬНО проверяем в CRM Bitrix24!
            if not project:
                crm_deal = BitrixService.find_deal_by_name(clean_obj_name)
                if crm_deal:
                    logger.info("Anti-Duplicate: Found existing deal #%s in Bitrix24 for '%s'. Importing.", crm_deal.get("ID"), clean_obj_name)
                    project = BitrixService.import_or_update_deal_from_bitrix(crm_deal)

            can_create = facts.get("can_create_deal") or (facts.get("confidence", 0) >= 0.7)

            # R2: Валидация обязательных полей перед созданием сделки
            contract_amount_raw = facts.get("contract_amount")
            has_valid_amount = (
                contract_amount_raw is not None
                and float(contract_amount_raw) > 0
            )
            if can_create and not has_valid_amount:
                logger.info(
                    "Deal creation skipped for '%s': contract_amount is %s (must be > 0)",
                    clean_obj_name, contract_amount_raw
                )
                can_create = False

            if not project and can_create:
                # Сделки гарантированно нет ни в локальной базе, ни в CRM Bitrix24
                project = Project.objects.create(
                    name=clean_obj_name,
                    normalized_name=core_name,
                    source='chat',
                    company=company,
                    manager=manager,
                    equipment_type=facts.get("direction") or facts.get("equipment_type") or "БТП",
                    contract_number=facts.get("contract_number") or "",
                    deal_period=facts.get("deal_period") or "",
                    status=facts.get("stage") or "qualification",
                    contract_amount=Decimal(str(facts.get("contract_amount") or 0.0)),
                    cost_amount=Decimal(str(facts.get("cost_amount") or 0.0)),
                    paid_amount=Decimal(str(facts.get("paid_amount") or 0.0)),
                    barter_amount=Decimal(str(facts.get("barter_amount") or 0.0)),
                    guarantee_amount=Decimal(str(facts.get("guarantee_amount") or 0.0)),
                    avr_status=facts.get("avr_status") or "Не закрыт",
                    current_action=facts.get("current_action") or content[:200],
                    next_action=facts.get("next_action") or "",
                    decision_maker=facts.get("decision_maker") or "",
                    blocker=facts.get("blocker") or "",
                    priority=facts.get("priority") or "standard",
                    needs_bitrix_sync=True,
                    is_verified=False
                )

                event_title = f"Извлечена новая сделка: {project.name} (ожидает проверки)"
                BusinessEvent.objects.create(
                    event_type="new_deal",
                    project=project,
                    manager=manager,
                    title=event_title,
                    description=f"Сумма: {project.contract_amount:,.2f} ₸. Менеджер: {responsible_name}. Ожидает верификации.",
                    severity="info"
                )
                push_notification_to_redis(sender_phone, event_title, f"Сделка {project.name} сохранена из переписки WhatsApp и ожидает проверки перед отправкой в Bitrix24 и учетом в аналитике", "deal")

            elif project:
                # Обновление существующей сделки и выставление флагов needs_bitrix_sync и is_verified=False
                if facts.get("contract_amount"):
                    project.contract_amount = Decimal(str(facts["contract_amount"]))
                if facts.get("cost_amount"):
                    project.cost_amount = Decimal(str(facts["cost_amount"]))
                if facts.get("paid_amount"):
                    project.paid_amount += Decimal(str(facts["paid_amount"]))
                if facts.get("stage"):
                    project.status = facts["stage"]
                if facts.get("current_action"):
                    project.current_action = facts["current_action"]
                if facts.get("next_action"):
                    project.next_action = facts["next_action"]
                if facts.get("blocker"):
                    project.blocker = facts["blocker"]
                if company and not project.company:
                    project.company = company
                if manager and not project.manager:
                    project.manager = manager
                
                project.is_verified = False
                project.needs_bitrix_sync = True
                project.last_chat_activity_at = timezone.now()
                project.save()
                logger.info("Project #%d '%s' updated from chat (is_verified=False, needs_bitrix_sync=True)", project.id, project.name)

    # Обязательства / Обещания менеджеров (Commitment)
    next_action = facts.get("next_action")
    if next_action and manager:
        next_action_at = facts.get("next_action_at")
        deadline = None
        if next_action_at:
            try:
                deadline = timezone.datetime.fromisoformat(next_action_at).date()
            except Exception:
                deadline = timezone.now().date() + timedelta(days=2)
        else:
            deadline = timezone.now().date() + timedelta(days=2)

        Commitment.objects.create(
            project=project,
            manager=manager,
            source_message=raw_msg,
            commitment_text=next_action,
            deadline=deadline,
            status='pending',
            severity='critical' if ('срочно' in content.lower() or 'договор' in next_action.lower()) else 'medium',
            bitrix_task_id=None,
            is_verified=False
        )

    # Финансовые записи (FinancialRecord)
    paid_amt = facts.get("paid_amount")
    if paid_amt and float(paid_amt) > 0 and project:
        FinancialRecord.objects.create(
            project=project,
            amount=Decimal(str(paid_amt)),
            payment_date=timezone.now().date(),
            payment_type='final' if 'окончательн' in content.lower() else 'milestone',
            status='received',
            notes=f"Извлечено из отчета {sender_name}: {content[:100]}",
            is_verified=False
        )

    raw_msg.processed = True
    raw_msg.save(update_fields=['processed'])
    
    return {
        "status": "processed",
        "message_id": message_id,
        "deal": project.name if project else None,
        "bitrix_id": project.bitrix_id if project else None
    }
