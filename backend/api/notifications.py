"""Durable notifications and versioned reminders; Redis is never the source of truth."""

from datetime import datetime, timedelta, time, date
from decimal import Decimal
from zoneinfo import ZoneInfo
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.contrib.auth.models import User
from .models import (
    Notification,
    NotificationDelivery,
    OutboxEvent,
    ReminderOccurrence,
    Commitment,
    UserProfile,
    TeamMembership,
    AuditEvent,
    FactCandidate,
)
from . import access


def preferences(user):
    profile = UserProfile.objects.filter(user=user).first()
    value = dict(profile.notification_preferences) if profile else {}
    value.setdefault("timezone", profile.timezone if profile else "Asia/Almaty")
    value.setdefault("quiet_start", "21:00")
    value.setdefault("quiet_end", "09:00")
    value.setdefault("digest_time", "09:00")
    value.setdefault("whatsapp", True)
    for name in ("daily_digest", "stalled_deals", "critical_kpi"):
        value.setdefault(name, getattr(profile, f"whatsapp_{name}", False))
    return value


def next_delivery_time(user, now=None):
    now = now or timezone.now()
    prefs = preferences(user)
    local = now.astimezone(ZoneInfo(prefs["timezone"]))
    start, end = (
        time.fromisoformat(prefs["quiet_start"]),
        time.fromisoformat(prefs["quiet_end"]),
    )
    if start == end:
        return now
    quiet = (
        (
            local.time().replace(tzinfo=None) >= start
            or local.time().replace(tzinfo=None) < end
        )
        if start > end
        else start <= local.time().replace(tzinfo=None) < end
    )
    if not quiet:
        return now
    day = local.date() + (
        timedelta(days=1)
        if start > end and local.time().replace(tzinfo=None) >= start
        else timedelta()
    )
    return datetime.combine(day, end, tzinfo=local.tzinfo)


def create_notification(
    user,
    title,
    message,
    category,
    key,
    project=None,
    commitment=None,
    whatsapp=False,
    event=None,
):
    with transaction.atomic():
        notification, created = Notification.objects.get_or_create(
            deduplication_key=f"{key}:{user.id}",
            defaults={
                "recipient": user,
                "title": title[:255],
                "message": message,
                "category": category,
                "project": project,
                "commitment": commitment,
                "business_event": event,
            },
        )
        prefs = preferences(user)
        enabled = prefs.get(category, True)
        if created and whatsapp and prefs["whatsapp"] and enabled:
            delivery = NotificationDelivery.objects.create(
                notification=notification, next_attempt_at=next_delivery_time(user)
            )
            OutboxEvent.objects.create(
                event_type="notification",
                deduplication_key=f"notification:{notification.id}:whatsapp",
                payload={
                    "delivery_id": delivery.id,
                    "commitment_version": commitment.version if commitment else None,
                },
                next_attempt_at=delivery.next_attempt_at,
            )
        return notification


def visible_notifications(user):
    return (
        Notification.objects.filter(recipient=user)
        .filter(
            Q(project__isnull=True)
            | Q(project__in=access.projects_for(user, include_client=True))
        )
        .order_by("-created_at", "-id")
    )


def serialize_notification(n):
    return {
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "type": n.category,
        "created_at": n.created_at.isoformat(),
        "is_read": n.read_at is not None,
        "acknowledged": n.acknowledged_at is not None,
        "project_id": n.project_id,
        "commitment_id": n.commitment_id,
        "deliveries": list(
            n.deliveries.values("state", "channel", "updated_at", "error_code")
        ),
    }


def push_notification_to_redis(phone, title, message, notif_type="info"):
    # Compatibility entry point. Writes only PostgreSQL and only existing users.
    import uuid

    user = User.objects.filter(username=phone, is_active=True).first()
    if not user or not access.has_access(user):
        return None, 0
    n = create_notification(
        user, title, message, notif_type, f"legacy-call:{uuid.uuid4().hex}"
    )
    return serialize_notification(n), visible_notifications(user).filter(
        read_at__isnull=True
    ).count()


def fetch_user_notifications(phone):
    user = User.objects.filter(username=phone).first()
    if not user:
        return [], 0
    qs = visible_notifications(user)
    return [serialize_notification(n) for n in qs[:50]], qs.filter(
        read_at__isnull=True
    ).count()


def mark_all_notifications_as_read(phone):
    user = User.objects.filter(username=phone).first()
    if user:
        visible_notifications(user).filter(read_at__isnull=True).update(
            read_at=timezone.now()
        )
    return fetch_user_notifications(phone)[0]


def effective_deadline(commitment):
    if commitment.deadline_at:
        return commitment.deadline_at
    if commitment.deadline:
        from .message_time import KAZAKHSTAN_OFFSET
        zone = KAZAKHSTAN_OFFSET
        return datetime.combine(commitment.deadline, time(18), tzinfo=zone)
    return None


def plan_reminders(now=None):
    now = now or timezone.now()
    count = 0
    qs = Commitment.objects.filter(
        is_verified=True, status__in=["pending", "overdue"], manager__isnull=False
    ).select_related("manager__user", "project")
    for c in qs:
        deadline = effective_deadline(c)
        if deadline is None or not (c.team_id or (c.project and c.project.team_id)):
            continue
        user = c.manager.user
        if not access.commitments_for(user).filter(pk=c.pk).exists():
            continue
        rules = [
            ("before_24h", deadline - timedelta(hours=24), user),
            ("before_2h", deadline - timedelta(hours=2), user),
            ("due", deadline, user),
        ]
        if now >= deadline + timedelta(hours=24):
            leaders = User.objects.filter(
                memberships__team_id=c.team_id or c.project.team_id,
                memberships__role="team_lead",
                memberships__status="active",
                is_active=True,
            ).distinct()
            rules += [
                ("escalation", deadline + timedelta(hours=24), leader)
                for leader in leaders
            ]
        # Merge elapsed reminders per recipient into the most relevant occurrence.
        due = {}
        for code, scheduled, recipient in rules:
            if scheduled <= now:
                previous = due.get(recipient.id)
                if previous is None or scheduled > previous[1]:
                    due[recipient.id] = (code, scheduled, recipient)
        for code, scheduled, recipient in due.values():
            with transaction.atomic():
                locked = Commitment.objects.select_for_update(of=("self",)).get(pk=c.pk)
                if locked.version != c.version or locked.status not in (
                    "pending",
                    "overdue",
                ):
                    continue
                occurrence, created = ReminderOccurrence.objects.get_or_create(
                    commitment=c,
                    commitment_version=c.version,
                    rule_code=code,
                    rule_version=1,
                    recipient=recipient,
                    scheduled_at=scheduled,
                )
                if not created:
                    continue
                if preferences(recipient).get("reminder") is False:
                    occurrence.state = "cancelled"
                    occurrence.save(update_fields=["state"])
                    continue
                # Coalesce queued earlier reminders delayed by quiet hours.
                older_deliveries = NotificationDelivery.objects.filter(
                    notification__commitment=c,
                    notification__recipient=recipient,
                    notification__category="reminder",
                    state="queued",
                )
                old_ids = list(older_deliveries.values_list("id", flat=True))
                older_deliveries.update(state="cancelled")
                OutboxEvent.objects.filter(
                    event_type="notification",
                    payload__delivery_id__in=old_ids,
                    state__in=["pending", "enqueued"],
                ).update(state="cancelled")
                n = create_notification(
                    recipient,
                    "Срок обязательства",
                    c.commitment_text,
                    "reminder",
                    f"reminder:{occurrence.id}",
                    project=c.project,
                    commitment=c,
                    whatsapp=True,
                )
                occurrence.notification = n
                occurrence.state = "created"
                occurrence.save()
                count += 1
    return count


def change_commitment(
    user, commitment_id, version, action, reason="", deadline_at=None
):
    from rest_framework.exceptions import ValidationError
    from .security import Conflict

    with transaction.atomic():
        c = (
            access.commitments_for(user)
            .select_for_update(of=("self",))
            .get(pk=commitment_id)
        )
        if c.version != version:
            raise Conflict()
        if c.manager_id is None or c.manager_id != getattr(getattr(user, "profile", None), "id", None):
            access.require_team_role(
                user, c.team_id or (c.project.team_id if c.project else None), ["team_lead"]
            )
        before = {
            "status": c.status,
            "deadline_at": c.deadline_at.isoformat() if c.deadline_at else None,
        }
        if action == "fulfill":
            c.status, c.fulfilled_at = "fulfilled", timezone.now()
        elif action == "postpone":
            if not reason.strip() or deadline_at is None:
                raise ValidationError("Нужны новый срок и причина.")
            access.require_team_role(
                user, c.team_id or (c.project.team_id if c.project else None), ["team_lead"]
            )
            c.original_deadline_at = c.original_deadline_at or effective_deadline(c)
            c.deadline_at, c.deadline, c.deadline_precision = (
                deadline_at,
                timezone.localtime(deadline_at).date(),
                "datetime",
            )
            c.postponed_reason, c.status = reason, "pending"
        elif action == "help":
            if not reason.strip():
                raise ValidationError("Опишите, какая помощь нужна.")
            if c.team_id or c.project:
                for leader in User.objects.filter(
                    memberships__team_id=c.team_id or c.project.team_id,
                    memberships__role="team_lead",
                    memberships__status="active",
                    is_active=True,
                ).distinct():
                    create_notification(
                        leader,
                        "Нужна помощь",
                        reason,
                        "help",
                        f"help:{c.id}:{c.version}",
                        project=c.project,
                        commitment=c,
                        whatsapp=True,
                    )
        else:
            raise ValidationError("Неизвестное действие.")
        if action != "help":
            c.version += 1
        c.save()
        ReminderOccurrence.objects.filter(commitment=c, state="queued").update(
            state="cancelled"
        )
        AuditEvent.objects.create(
            actor=user,
            target_type="Commitment",
            target_id=c.id,
            action=action,
            before_after={"before": before, "reason": reason, "version": c.version},
        )
        return c


def plan_digests(now=None):
    from .datamart import datamart

    now = now or timezone.now()
    count = 0
    for user in User.objects.filter(
        is_active=True, memberships__status="active"
    ).distinct():
        prefs = preferences(user)
        if not prefs.get("daily_digest", False):
            continue
        local = now.astimezone(ZoneInfo(prefs["timezone"]))
        if local.time().replace(tzinfo=None) < time.fromisoformat(prefs["digest_time"]):
            continue
        mart = datamart.get_sales_kpi_mart(user)
        create_notification(
            user,
            "Ежедневная сводка",
            f"Поступления: {mart['fact']} {mart['currency']}. {mart['coverage']['message']}",
            "daily_digest",
            f"digest:{local.date().isoformat()}",
            whatsapp=True,
        )
        count += 1
    return count


def plan_risks(now=None):
    from .models import AISettings, Project, Team, ProviderUsage
    from .datamart import datamart
    from django.db.models import Sum

    now = now or timezone.now()
    for project in Project.objects.filter(
        is_verified=True, version__gt=0, team__is_active=True, archived=False
    ).select_related("team", "manager__user"):
        risk = []
        if project.status == "proposal_sent" and project.updated_at <= now - timedelta(
            days=project.team.stalled_days
        ):
            risk.append(
                (
                    "stalled_deals",
                    "Нет подтверждённой активности по КП",
                    f"stalled:{project.id}:{project.version}:{project.team.rules_version}",
                )
            )
        margin = (
            (
                (project.contract_amount - project.cost_amount)
                / project.contract_amount
                * 100
            )
            if project.cost_confirmed and project.contract_amount
            else None
        )
        if margin is not None and margin < project.team.low_margin_percent:
            risk.append(
                (
                    "critical_kpi",
                    "Маржа ниже установленного порога",
                    f"margin:{project.id}:{project.version}:{project.team.rules_version}",
                )
            )
        for category, title, key in risk:
            users = User.objects.filter(
                memberships__team=project.team,
                memberships__role__in=["finance"]
                if category == "critical_kpi"
                else ["team_lead"],
                memberships__status="active",
                is_active=True,
            )
            recipients = {u.id: u for u in users}
            if (
                project.manager
                and access.projects_for(project.manager.user)
                .filter(pk=project.pk)
                .exists()
            ):
                recipients[project.manager.user.id] = project.manager.user
            for user in recipients.values():
                if preferences(user).get(category, False):
                    create_notification(
                        user,
                        title,
                        project.name,
                        category,
                        key,
                        project=project,
                        whatsapp=True,
                    )
    for user in User.objects.filter(
        is_active=True, memberships__status="active"
    ).distinct():
        if not preferences(user).get("critical_kpi", False):
            continue
        mart = datamart.get_sales_kpi_mart(user)
        for manager in mart["managers"]:
            for threshold in (50, 80, 100):
                if (
                    manager["kpiPercent"] is not None
                    and manager["kpiPercent"] >= threshold
                ):
                    create_notification(
                        user,
                        f"Выполнено {threshold}% плана",
                        manager["name"],
                        "critical_kpi",
                        f"target:{mart['period_start']}:{manager['id']}:{manager['targetAmount']}:{threshold}",
                        whatsapp=True,
                    )
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    usage = ProviderUsage.objects.filter(created_at__gte=day)
    spent = usage.aggregate(total=Sum("cost_usd"))["total"] or 0
    cfg = AISettings.get_active()
    if (
        (cfg.daily_request_limit > 0 and usage.count() >= cfg.daily_request_limit * 0.8)
        or (cfg.daily_budget_usd > 0 and spent >= cfg.daily_budget_usd * Decimal("0.8"))
        or usage.filter(succeeded=False).count() >= 10
    ):
        for user in User.objects.filter(is_staff=True, is_active=True):
            if access.integration_allowed(user):
                create_notification(
                    user,
                    "Требуется проверка AI-интеграции",
                    "Приближение к лимиту запросов/расходов или повторяющиеся ошибки.",
                    "technical",
                    f"ai-alert:{day.date()}",
                    whatsapp=False,
                )


def _format_candidate_amount(val):
    if val is None or val == "":
        return None
    s = str(val).strip().rstrip("₸").strip()
    try:
        dec = Decimal(s.replace(" ", "").replace(",", "."))
        if dec <= 0:
            return None
        if dec == dec.to_integral_value():
            formatted = f"{int(dec):,}".replace(",", " ")
        else:
            formatted = f"{dec:,.2f}".replace(",", " ")
        return f"{formatted} ₸"
    except Exception:
        return f"{s} ₸" if s else None


def _format_candidate_deadline(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return (
            val.strftime("%d.%m.%Y %H:%M")
            if (val.hour or val.minute)
            else val.strftime("%d.%m.%Y")
        )
    if isinstance(val, date):
        return val.strftime("%d.%m.%Y")
    s = str(val).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return (
            dt.strftime("%d.%m.%Y %H:%M")
            if (dt.hour or dt.minute)
            else dt.strftime("%d.%m.%Y")
        )
    except Exception:
        return s


def _extract_candidate_sender_name(candidate):
    if getattr(candidate, "trace", None) and candidate.trace.raw_message:
        raw = candidate.trace.raw_message
        if raw.sender_name:
            return raw.sender_name
        if raw.sender_phone:
            return raw.sender_phone
    evidence = getattr(candidate, "evidence", None)
    if evidence:
        first_ev = evidence.select_related("raw_message").first()
        if first_ev and first_ev.raw_message:
            if first_ev.raw_message.sender_name:
                return first_ev.raw_message.sender_name
            if first_ev.raw_message.sender_phone:
                return first_ev.raw_message.sender_phone
    changes = candidate.proposed_changes or {}
    return changes.get("sender_name") or changes.get("responsible_name") or None


def _format_candidate_message(candidate):
    changes = candidate.proposed_changes or {}
    lines = []

    # Контрагент (Bitrix): <company_name> (from candidate.project.company or proposed_changes.company_name)
    company_name = None
    if (
        candidate.project_id
        and candidate.project
        and getattr(candidate.project, "company", None)
    ):
        company_name = candidate.project.company.name
    if not company_name:
        company_name = changes.get("company_name")
    if company_name:
        lines.append(f"Контрагент (Bitrix): {company_name}")

    # Сделка / Объект: <project_name> (from candidate.project or proposed_changes.object_name)
    project_name = None
    if candidate.project_id and candidate.project and candidate.project.name:
        project_name = candidate.project.name
    if not project_name:
        project_name = (
            changes.get("object_name")
            or changes.get("project_name")
            or changes.get("deal_name")
        )
    if project_name:
        lines.append(f"Сделка / Объект: {project_name}")

    # Суть: <commitment_text / description>
    description = (
        changes.get("commitment_text")
        or changes.get("description")
        or changes.get("current_action")
        or changes.get("next_action")
    )
    if description:
        lines.append(f"Суть: {description}")

    # Сумма: <amount> ₸ (if present)
    amount_raw = (
        changes.get("amount")
        or changes.get("contract_amount")
        or (
            getattr(candidate.project, "contract_amount", None)
            if candidate.project_id
            else None
        )
    )
    amount_formatted = _format_candidate_amount(amount_raw)
    if amount_formatted:
        lines.append(f"Сумма: {amount_formatted}")

    # Срок: <deadline> (if present)
    deadline_raw = changes.get("deadline") or changes.get("deadline_at")
    deadline_formatted = _format_candidate_deadline(deadline_raw)
    if deadline_formatted:
        lines.append(f"Срок: {deadline_formatted}")

    # Отправитель WhatsApp: <sender_name>
    sender_name = _extract_candidate_sender_name(candidate)
    if sender_name:
        lines.append(f"Отправитель WhatsApp: {sender_name}")

    return "\n".join(lines)


def notify_on_new_candidate(candidate):
    if not candidate or not candidate.id:
        return []

    if candidate.fact_type == "commitment":
        title = "Новое обязательство из чата"
    elif candidate.fact_type == "project":
        title = "Выявлена новая сделка"
    elif candidate.fact_type == "payment":
        title = "Договоренность об оплате"
    else:
        title = "Новое обязательство из чата"

    message = _format_candidate_message(candidate)
    if not message:
        message = title

    recipients = {}
    manager_profile = candidate.manager or (
        candidate.project.manager if candidate.project_id else None
    )
    if manager_profile and manager_profile.user and manager_profile.user.is_active:
        recipients[manager_profile.user.id] = manager_profile.user

    team_id = candidate.team_id or (
        candidate.project.team_id if candidate.project_id else None
    )
    if team_id:
        leads = User.objects.filter(
            memberships__team_id=team_id,
            memberships__role="team_lead",
            memberships__status="active",
            is_active=True,
        ).distinct()
        for lead in leads:
            recipients[lead.id] = lead

    notifications = []
    for user in recipients.values():
        recipient_project = (
            candidate.project
            if (
                candidate.project_id
                and candidate.project
                and access.projects_for(user, include_client=True)
                .filter(pk=candidate.project_id)
                .exists()
            )
            else None
        )
        notif = create_notification(
            user=user,
            title=title,
            message=message,
            category="commitment_detected",
            key=f"candidate_detected:{candidate.id}",
            project=recipient_project,
            whatsapp=True,
        )
        notifications.append(notif)
    return notifications


def notify_on_candidate_approved(candidate):
    if not candidate or not candidate.id:
        return []

    if candidate.fact_type == "project":
        title = "Подтверждена сделка"
    elif candidate.fact_type == "payment":
        title = "Подтверждена договоренность об оплате"
    else:
        title = "Подтверждено обязательство"

    message = _format_candidate_message(candidate)
    if not message:
        message = title

    recipients = {}
    manager_profile = candidate.manager or (
        candidate.project.manager if candidate.project_id else None
    )
    if manager_profile and manager_profile.user and manager_profile.user.is_active:
        recipients[manager_profile.user.id] = manager_profile.user

    team_id = candidate.team_id or (
        candidate.project.team_id if candidate.project_id else None
    )
    if team_id:
        leads = User.objects.filter(
            memberships__team_id=team_id,
            memberships__role="team_lead",
            memberships__status="active",
            is_active=True,
        ).distinct()
        for lead in leads:
            recipients[lead.id] = lead

    notifications = []
    for user in recipients.values():
        recipient_project = (
            candidate.project
            if (
                candidate.project_id
                and candidate.project
                and access.projects_for(user, include_client=True)
                .filter(pk=candidate.project_id)
                .exists()
            )
            else None
        )
        notif = create_notification(
            user=user,
            title=title,
            message=message,
            category="commitment_detected",
            key=f"candidate_approved:{candidate.id}",
            project=recipient_project,
            whatsapp=True,
        )
        notifications.append(notif)
    return notifications


def notify_thread_subscribers(thread, old_state, new_state):
    """
    Уведомляет подписчиков темы переписки об изменении её статуса.
    """
    from .models import ThreadSubscription

    subs = list(ThreadSubscription.objects.filter(thread=thread).select_related("user"))
    if not subs:
        return []

    state_names = {
        "open": "В процессе",
        "ready": "Завершена",
        "unknown": "Требует уточнения",
        "superseded": "Заменена",
    }
    old_str = state_names.get(old_state, old_state)
    new_str = state_names.get(new_state, new_state)

    title = f"Изменен статус темы: {thread.topic[:60]}"
    lines = [
        f"Тема #{thread.id}: «{thread.topic}»",
        f"Статус: {old_str} ➔ {new_str}",
    ]
    if thread.summary:
        lines.append(f"Резюме: {thread.summary[:300]}")

    message = "\n".join(lines)
    notifications = []
    for sub in subs:
        notif = create_notification(
            user=sub.user,
            title=title,
            message=message,
            category="thread_status",
            key=f"thread_status:{thread.id}:{new_state}:{thread.version}",
            project=thread.project,
            whatsapp=True,
        )
        notifications.append(notif)
    return notifications

