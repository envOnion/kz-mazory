"""System decisions from literal WhatsApp evidence; no HTTP privilege bypass."""

import hashlib
from datetime import timedelta
import json
import logging
import re
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .deduplication import normalize_deal_name
from .facts import FactSchema, _apply_candidate, fact_identity, json_value
from .message_context import source_scope
from .message_time import source_time, source_zone
from .models import (
    AISettings, AuditEvent, CandidateCrmMatch, Commitment, Company, CompanyAlias,
    FactCandidate, FactDecision, FactEvent, FieldAssertion, FinancialRecord,
    OutboxEvent, Participant, ParticipantIdentity, PaymentScheduleItem,
    Project, ProjectAlias, ProjectParty, RawMessage, SourceCheckpoint, SourceWorkItem,
)
from .providers import ProviderUnavailable
from .security import Conflict

logger = logging.getLogger(__name__)
APPROXIMATE = re.compile(r"\b(около|порядка|примерно|почти|практически|ориентировочно)\b", re.I)
MONEY = re.compile(r"(?<![\w.,])(?P<number>\d{1,3}(?:\.\d{3}){2,}(?:,\d+)?|\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)\s*(?P<scale>млн|миллион\w*|тыс\w*|млрд|миллиард\w*)?(?![\d.,])", re.I)
ACTUAL = re.compile(r"\b(?:поступил[аои]?|получил[аи]?|получен[аоы]?|оплатил[аи]?|оплачено|перечислил[аи]?|перечислен[аоы]?|зачислен[аоы]?|перевел[аи]?|перевёл[аи]?|перевели)\b", re.I)
FUTURE = re.compile(r"не\s+(?:поступ|получ|оплат|перечисл)|(?:оплатим|оплатят|оплатить|поступит|поступят|получим|получат|перечислим|перечислят|перечислит|переведем|переведём|переведут|зачислят|ожидаем|планируем|ожидается|будет\s+оплачен)", re.I)


class Deferred(Exception):
    def __init__(self, code, explanation, rejected=False):
        self.code, self.explanation, self.rejected = code, explanation, rejected


def enqueue_decision(candidate_id, trigger="extracted"):
    cfg = AISettings.get_active()
    if not cfg.autonomous_enabled:
        return
    # Each external update has its own generation; unchanged decisions are also
    # deduplicated against an immutable input fingerprint inside decide().
    OutboxEvent.objects.get_or_create(
        deduplication_key=f"decide:{candidate_id}:{cfg.autonomous_policy_version}:{trigger}",
        defaults={"event_type": "decide_fact", "payload": {"candidate_id": candidate_id}},
    )


def _money_matches(value, text):
    for match in MONEY.finditer(text):
        number = match["number"].replace(" ", "").replace("\u00a0", "")
        if number.count(".") >= 2:
            number = number.replace(".", "")
        amount = Decimal(number.replace(",", "."))
        scale = (match["scale"] or "").casefold()
        multiplier = 1_000_000_000 if scale.startswith(("млрд", "миллиард")) else 1_000_000 if scale.startswith(("млн", "миллион")) else 1000 if scale.startswith("тыс") else 1
        if amount * multiplier == abs(value):
            return True
    return False


def _evidence(candidate):
    from .pipeline import _source_quote
    raw = candidate.trace.raw_message
    if not raw or raw.source not in ("waha", "whatsapp", "whatsapp_export", "chat"):
        raise Deferred("unsupported_source", "Для автоматического учета нужен источник WhatsApp.")
    if candidate.team_id != (raw.config.team_id if raw.config_id else raw.team_id):
        raise Deferred("source_team_conflict", "Команда кандидата не совпадает с командой источника.")
    scope = source_scope(raw)
    refs = list(candidate.evidence.select_related("raw_message"))
    if not refs:
        raise Deferred("unsupported_quote", "Нет сохраненной цитаты источника.", True)
    ids = {ref.raw_message_id for ref in refs}
    if ids != set(scope.filter(pk__in=ids).exclude(processing_state__in=("deleted", "superseded")).values_list("pk", flat=True)):
        raise Deferred("source_unavailable", "Доказательства недоступны в текущем источнике.")
    for ref in refs:
        if _source_quote(ref.quote, ref.raw_message.content) != ref.quote:
            raise Deferred("unsupported_quote", "Цитата не соответствует сохраненному оригиналу.", True)
        if "\ufffd" in ref.quote:
            raise Deferred("damaged_source", "Поврежденная кодировка в доказательстве.")
    data = candidate.proposed_changes
    anchor = data.get("evidence_message_id") or data.get("promise_message_id") or raw.id
    primary = next((ref for ref in refs if ref.raw_message_id == anchor and ref.quote == data.get("evidence")), None)
    if primary is None:
        raise Deferred("unsupported_quote", "Основная цитата отсутствует среди доказательств.", True)
    return raw, primary.raw_message, refs, "\n".join(ref.quote for ref in refs)


def _resolve_project(candidate, data):
    if candidate.project_id:
        if candidate.project.team_id != candidate.team_id or candidate.project.archived:
            raise Deferred("ambiguous_project", "Связь проекта выходит за пределы команды или архивна.")
        candidate.project = Project.objects.select_for_update().get(pk=candidate.project_id)
        candidate.base_project_version = candidate.project.version
        if candidate.fact_type == "project" and candidate.crm_match_state == "matched" and not candidate.crm_matches.filter(selection_state="selected", crm_match_revision=candidate.crm_match_revision).exists():
            candidate.crm_match_state = "not_requested"
        candidate.save(update_fields=["base_project_version", "crm_match_state"])
        return
    if candidate.thread_revision_id:
        thread_project = candidate.thread_revision.thread.project
        if thread_project and thread_project.team_id == candidate.team_id and not thread_project.archived:
            candidate.project = thread_project
    if not candidate.project_id and data.get("object_name"):
        normalized = normalize_deal_name(data["object_name"])
        matches = list(Project.objects.filter(team_id=candidate.team_id, archived=False).filter(
            Q(normalized_name=normalized) | Q(name__iexact=data["object_name"]) | Q(aliases__normalized_name=normalized)
        ).distinct()[:2])
        if len(matches) > 1:
            raise Deferred("ambiguous_project", "Несколько объектов с одинаковым обозначением.")
        if matches:
            candidate.project = matches[0]
    if not candidate.project_id:
        # CRM scores alone never authorize a binding. Only an exact object label
        # from the offered current generation can resolve an existing mirror.
        options = list(CandidateCrmMatch.objects.filter(candidate=candidate, crm_match_revision=candidate.crm_match_revision))
        exact = [item for item in options if normalize_deal_name(item.object_label or item.deal_title) == normalize_deal_name(data.get("object_name", "")) and data.get("object_name")]
        if len(exact) == 1:
            option = exact[0]
            if not option.project_id:
                # Materialize only the technical CRM identity. Opportunity and
                # stage are deliberately not imported as business facts.
                from .facts import _company_from_crm_match
                company = _company_from_crm_match(option, data) if option.bitrix_company_id and option.company_name else None
                project, _ = Project.objects.get_or_create(bitrix_id=option.bitrix_deal_id, defaults={"team_id": candidate.team_id, "name": option.deal_title or option.object_label, "company": company, "currency": option.currency or "KZT", "source": "bitrix_crm", "identity_confirmed": True})
                if project.team_id != candidate.team_id or project.archived:
                    raise Deferred("ambiguous_project", "CRM-идентичность уже относится к другой команде или архиву.")
                option.project = project
                option.save(update_fields=["project"])
            if option.project.team_id != candidate.team_id or option.project.archived:
                raise Deferred("ambiguous_project", "CRM-объект не относится к доступной команде.")
            if candidate.fact_type == "project":
                candidate.crm_matches.update(selection_state="suggested")
                option.selection_state = "selected"
                option.save(update_fields=["selection_state"])
                candidate.crm_match_state = "matched"
            candidate.project = option.project
        elif options or candidate.crm_match_state == "ambiguous":
            raise Deferred("ambiguous_project", "CRM-варианты не доказывают идентичность объекта.")
    if candidate.project_id:
        candidate.base_project_version = candidate.project.version
        if candidate.fact_type == "project" and candidate.crm_match_state == "matched" and not candidate.crm_matches.filter(selection_state="selected", crm_match_revision=candidate.crm_match_revision).exists():
            # The already established local identity is authoritative; a weak
            # suggested lookup must not override it.
            candidate.crm_match_state = "not_requested"
        candidate.save(update_fields=["project", "base_project_version", "crm_match_state"])


def _validate(candidate, data, primary, text):
    company = normalize_deal_name(data.get("company_name", ""))
    if company and not re.search(r"(?<!\w)" + re.escape(company) + r"(?!\w)", normalize_deal_name(text)):
        data["company_name"], data["party_role"] = "", "unknown"
        candidate._discarded_fields = {"company_name": "Компания не подтверждена цитатами; основной факт проверен отдельно."}
    role_patterns = {"customer": r"заказчик", "supplier": r"поставщик", "contractor": r"подрядчик", "designer": r"проектировщик|проектн(?:ая|ой)\s+организаци", "payer": r"оплата\s+от|плательщик"}
    role = data.get("party_role", "unknown")
    if role != "unknown" and not re.search(role_patterns[role], text, re.I):
        data["party_role"] = "unknown"
        candidate._discarded_fields = {**getattr(candidate, "_discarded_fields", {}), "party_role": "Роль стороны не доказана цитатой; упоминание сохранено без подмены юридического заказчика."}
    if candidate.project_id and data.get("object_name"):
        normalized = normalize_deal_name(text)
        labels = {normalize_deal_name(candidate.project.name), normalize_deal_name(candidate.project.normalized_name)} | set(candidate.project.aliases.values_list("normalized_name", flat=True))
        labels |= {normalize_deal_name(option.object_label or option.deal_title) for option in candidate.crm_matches.filter(project_id=candidate.project_id, crm_match_revision=candidate.crm_match_revision)}
        if normalize_deal_name(data["object_name"]) not in labels:
            raise Deferred("ambiguous_project", "Название в источнике не соответствует выбранному проекту или его доказанным обозначениям.")
        named = any(label and re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", normalized) for label in labels)
        established = bool(candidate.thread_revision_id and FactCandidate.objects.filter(thread_revision__thread_id=candidate.thread_revision.thread_id, project_id=candidate.project_id, status="approved").exists())
        if not named and not established and primary.project_id != candidate.project_id:
            raise Deferred("ambiguous_project", "Цитаты не подтверждают связь события с выбранным объектом.")
    if candidate.fact_type == "project":
        if not data.get("object_name") and not candidate.project_id:
            raise Deferred("ambiguous_project", "Объект не определен.")
        if data.get("object_name") and not candidate.project_id and normalize_deal_name(data["object_name"]) not in normalize_deal_name(text):
            raise Deferred("ambiguous_project", "Название нового объекта не доказано цитатой.")
        for field in ("contract_amount", "cost_amount"):
            if field in data and (not _money_matches(data[field], text) or APPROXIMATE.search(text)):
                raise Deferred("unknown_amount", "Точная сумма договора/себестоимости не доказана цитатой.")
        occurred = source_time(primary)[0]
        if candidate.project_id and occurred and FieldAssertion.objects.filter(project_id=candidate.project_id, field_name__in=[key for key in ("contract_amount", "cost_amount", "stage") if key in data], fact_event__occurred_at__gt=occurred).exists():
            raise Deferred("older_source_revision", "Более поздняя переписка уже уточнила значение; старый факт не заменяет текущий.")
        if "contract_amount" in data and not re.search(r"договор|контракт", text, re.I):
            raise Deferred("plan_not_contract", "Сумма без договорного основания не является суммой договора.")
        if "cost_amount" in data and not re.search(r"себестоим|затрат|стоимость\s+(?:закуп|материал|изготов|оборудован)", text, re.I):
            raise Deferred("unsupported_cost", "Поступление или договорная сумма не доказывают себестоимость.")
        if "stage" in data:
            # Business stages can be extracted, but external mapping is distinct.
            words = {"lead": "контакт|лид", "qualification": "тз|задани|сбор", "design": "проектир|экспертиз", "proposal_sent": "кп|предложени", "contract_signing": "договор|контракт", "in_execution": "производств|монтаж|исполнени", "completed": "заверш|сдан|выполнен", "stalled": "приостан|завис|задерж", "lost": "проигр|отказ|отмен"}
            if data["stage"] == "completed" and (not re.search(r"\b(?:заверш[её]н[аоы]?|сдан[аоы]?|выполнен[аоы]?|закрыт[аоы]?|завершили)\b", text, re.I) or re.search(r"не\s+(?:заверш|сдан|выполн|закрыт)", text, re.I)):
                raise Deferred("unsupported_stage", "План завершения или отрицание не подтверждают завершенный объект.")
            if not re.search(words[data["stage"]], text, re.I):
                raise Deferred("unsupported_stage", "Стадия не имеет основания в цитатах.")
    elif candidate.fact_type == "payment":
        currencies = {code for code, pattern in {"KZT": r"тенге|\bтг\b|₸|\bKZT\b", "USD": r"доллар|\bUSD\b|\$", "EUR": r"евро|\bEUR\b|€", "RUB": r"рубл|\bRUB\b|₽"}.items() if re.search(pattern, data["evidence"], re.I)}
        chosen = data.get("currency", candidate.project.currency if candidate.project_id else "KZT")
        if len(currencies) == 1 and chosen not in currencies:
            raise Deferred("currency_conflict", "Валюта извлеченного значения противоречит источнику.")
        if data.get("amount") and not _money_matches(data["amount"], text):
            raise Deferred("unknown_amount", "Сумма финансового показателя не подтверждена цитатой.")
        if APPROXIMATE.search(text) or primary.raw_payload.get("artifact_id"):
            data["amount_precision"] = "approximate"
        if data["payment_kind"] in ("cumulative", "balance", "debt", "invoice", "transfer"):
            return "observation"
        if data["payment_kind"] == "promise":
            if not candidate.project_id:
                raise Deferred("ambiguous_project", "Ожидание оплаты сохранено; объект графика еще не определен.")
            if data.get("payment_date") and data.get("amount"):
                prior_schedule = PaymentScheduleItem.objects.filter(project=candidate.project, due_date=data["payment_date"], amount=data["amount"], currency=data.get("currency", candidate.project.currency), direction=data["direction"], state="active").first()
                if prior_schedule:
                    raise Deferred("possible_duplicate_schedule", f"Есть совпадающий пункт графика №{prior_schedule.id}; второй платеж не доказан.")
            if data.get("amount") and not _money_matches(data["amount"], text):
                raise Deferred("unknown_amount", "Сумма ожидания не подтверждена цитатой.")
            if APPROXIMATE.search(text):
                data["amount_precision"] = "approximate"
            return "schedule"
        if re.search(r"всего\s+(?:оплачено|поступило)|итого\s+оплачено|накопительн", text, re.I):
            data["payment_kind"] = "cumulative"
            return "observation"
        if re.search(r"(?:получ\w*|выстав\w*)\s+(?:сч[её]т|кп|коммерческ\w*\s+предложени\w*|смету)|игнорируй\s+правила|создай\s+(?:фиктивн|плат[её]ж)", text, re.I):
            raise Deferred("not_actual_payment", "Счет или инструкция системе не доказывают движение.", True)
        if data["payment_kind"] == "reversal":
            original = FinancialRecord.objects.filter(pk=data.get("reverses_id"), project_id=candidate.project_id, is_verified=True).select_related("candidate__trace__raw_message").first()
            if not original or data.get("amount", 0) >= 0 or not re.search(r"отмен|ошиб|сторнир|корректир", primary.content, re.I):
                raise Deferred("reversal_unproven", "Нет явного исправления конкретного исходного платежа.")
            origin = original.candidate.trace.raw_message_id if original.candidate_id else None
            if not origin or not candidate.evidence.filter(raw_message_id=origin).exists():
                raise Deferred("reversal_unproven", "Исправление не связано доказательствами с исходным движением.")
            data["direction"], data["amount_precision"], data["currency"] = original.direction, original.amount_precision, original.currency
            return "apply"
        if data["direction"] == "income" and re.search(r"(?:оплатили|перечислили|перевели|оплатил)\s+(?:поставщик|подрядчик)", text, re.I):
            raise Deferred("direction_conflict", "Перечисление поставщику не является поступлением от клиента.")
        if FUTURE.search(text) or not ACTUAL.search(text):
            raise Deferred("plan_not_actual", "Нет сообщения о фактически выполненном движении.", True)
        if re.search(r"на\s+сч[её]т[еу]|остаток|задолжен|план\s+сбор", text, re.I) and not re.search(r"поступил|зачислен|перечислил", text, re.I):
            raise Deferred("balance_not_payment", "Остаток/долг/план не является новым платежом.", True)
        if "amount" not in data or not _money_matches(data["amount"], text):
            raise Deferred("unknown_amount", "Сумма движения не установлена по источнику.")
        if APPROXIMATE.search(text) or primary.raw_payload.get("artifact_id"):
            data["amount_precision"] = "approximate"
        if data["amount_precision"] in ("range", "unknown"):
            raise Deferred("unknown_amount", "Нельзя учесть диапазон/неизвестное значение как точную сумму.")
        if not candidate.project_id:
            raise Deferred("ambiguous_project", "Движение сохранено в переписке; объект учета не определен.")
        if not data.get("payment_date"):
            raise Deferred("unknown_source_time", "Дата платежа не установлена.")
        if data["payment_date"] > timezone.now().astimezone(source_zone(primary)).date():
            raise Deferred("plan_not_actual", "Будущая дата не подтверждает поступивший платеж.", True)
        prior = FinancialRecord.objects.filter(project=candidate.project, is_verified=True, amount=data["amount"], currency=data.get("currency", candidate.project.currency), payment_date=data["payment_date"], direction=data["direction"]).select_related("candidate__trace__raw_message").first()
        if prior:
            origin = prior.candidate.trace.raw_message if prior.candidate_id else None
            if origin and origin.source == primary.source and origin.session_name == primary.session_name and origin.message_id == primary.message_id:
                raise Deferred("duplicate_event", f"Это движение уже учтено в записи №{prior.id}.", True)
            raise Deferred("possible_duplicate", f"Есть совпадающая запись №{prior.id}; переписка не доказывает вторую операцию.")
    else:
        if not data.get("commitment_id") and data["commitment_status"] == "pending":
            promise_text = " ".join(ref["quote"] for ref in data.get("evidence_messages", []) if ref["role"] == "promise") or data["evidence"]
            if data["assignment_kind"] == "assignment":
                if not re.search(r"прошу|поруч|нужно|необходимо|должен|требуется|сделай|найди|уточни|подготовь|please|must", promise_text, re.I):
                    raise Deferred("not_an_assignment", "Нет явного назначения действия; обсуждение не создает обязанность.")
                name_parts = [word for word in re.findall(r"\w+", data["responsible_name"].casefold()) if len(word) >= 3]
                if not name_parts or not any(word in promise_text.casefold() for word in name_parts):
                    raise Deferred("unknown_participant", "Назначенный исполнитель не подтвержден в цитате поручения.")
            elif data["assignment_kind"] == "reported_promise":
                if not re.search(r"обещал|обещали|обязал(?:ся|ись)|подтвердил", promise_text, re.I):
                    raise Deferred("promise_unproven", "Сообщенное обещание другой стороны не подтверждено цитатой.")
                words = [word for word in re.findall(r"\w+", data["responsible_name"].casefold()) if len(word) >= 3]
                if not words or not any(word in text.casefold() for word in words):
                    raise Deferred("unknown_participant", "Сторона сообщенного обещания не установлена по источнику.")
            elif not re.search(r"обещаю|сделаю|отправлю|предоставлю|проверю|позвоню|подготовлю|найду|уточню|узнаю|проведу|займусь|скину|вышлю|передам|согласую|беру|буду|жіберемін|тексеремін|I(?:'ll| will)", promise_text, re.I):
                raise Deferred("promise_unproven", "Нет явного обещания автора; необходим связанный контекст принятия поручения.")
        if data.get("commitment_id"):
            existing = Commitment.objects.filter(pk=data["commitment_id"], team=candidate.team).first()
            if not existing or existing.source_message_id != data.get("promise_message_id"):
                raise Deferred("ambiguous_commitment", "Изменение не связано с исходным поручением.")
            if data.get("base_commitment_version") != existing.version:
                raise Deferred("commitment_version_conflict", "Контекст обязательства устарел; требуется новая обработка.")
            if data["commitment_status"] == "cancelled":
                cancellation = [ref for ref in data.get("evidence_messages", []) if ref["role"] == "cancellation"]
                if not cancellation or not any(re.search(r"отмен|не\s+нужно|не\s+требуется", ref["quote"], re.I) for ref in cancellation):
                    raise Deferred("cancellation_unproven", "Нет сообщения об отмене данного действия.")
            elif data["commitment_status"] == "fulfilled" and any(re.search(r"частично|не\s+все|только\s+часть", ref["quote"], re.I) for ref in data.get("evidence_messages", []) if ref["role"] == "fulfillment"):
                raise Deferred("partial_execution", "Частичное выполнение не закрывает все исходное обязательство.")
            elif data["commitment_status"] == "fulfilled" and any(re.search(r"не\s+(?:выполн|готов|найден|отправ|передан|получ|сделал|сделано|закрыт|заверш|поставлен|доставлен|провед)|не\s+удалось|еще\s+(?:ищем|ждем|ждём)|ожидаем", ref["quote"], re.I) for ref in data.get("evidence_messages", []) if ref["role"] == "fulfillment"):
                raise Deferred("fulfillment_unproven", "Сообщение отрицает или еще ожидает выполнение; закрытие не доказано.")
            elif data["commitment_status"] == "fulfilled" and not any(re.search(r"выполн|готов|найден|отправ|передан|получ|сделал|сделано|закрыт|заверш|поставлен|доставлен|провед", ref["quote"], re.I) for ref in data.get("evidence_messages", []) if ref["role"] == "fulfillment"):
                raise Deferred("fulfillment_unproven", "Нет явного сообщения о результате конкретного действия.")
            elif data["commitment_status"] == "fulfilled" and any(re.search(r"частично|не\s+все|только\s+часть", ref["quote"], re.I) for ref in data.get("evidence_messages", []) if ref["role"] == "fulfillment"):
                raise Deferred("partial_execution", "Частичное выполнение не закрывает все исходное обязательство.")
            elif data["commitment_status"] == "pending":
                deadlines = [ref for ref in data.get("evidence_messages", []) if ref["role"] == "deadline" and ref["raw_message_id"] != existing.source_message_id]
                if not deadlines or not data.get("deadline_at"):
                    raise Deferred("reschedule_unproven", "Новый срок должен быть указан в отдельном уточняющем сообщении.")
        elif data["commitment_status"] == "cancelled":
            raise Deferred("historical_action", "Отмена без исходного открытого поручения не создает новую задачу.", True)
        # Past work reports must not become new obligations. Completion updates
        # are validated separately by the existing evidence resolver.
        promise = " ".join(ref["quote"] for ref in data.get("evidence_messages", []) if ref["role"] == "promise") or data["evidence"]
        if data["commitment_status"] == "pending" and re.search(r"\b(вчера|провел|провёл|обсудил|согласовал|отправил)\b", promise, re.I) and not re.search(r"обещ|завтра|нужно|необходимо|прошу|поруч|должен", promise, re.I):
            raise Deferred("historical_action", "Сообщение описывает прошлое действие, а не новое обязательство.", True)
        if not data.get("responsible_name"):
            raise Deferred("unknown_participant", "Исполнитель не указан и автор обещания не установлен.")
        if data.get("deadline_at") and data["deadline_precision"] == "datetime" and data["deadline_basis"] == "morning_default":
            data["deadline_precision"] = "date"
            data["deadline_basis"] = "unknown"
    return "apply"


def _participant(candidate, data, raw):
    if not data.get("responsible_name"):
        return None
    name = data["responsible_name"]
    if "," in name or " и " in name.casefold():
        return None  # Preserve a group label; never invent one combined person.
    technical = raw.sender_phone if data.get("assignment_kind") == "promise" else ""
    namespace = f"team:{candidate.team_id}:phone" if technical else f"team:{candidate.team_id}:source:{raw.config_id}:{raw.chat_id}:name"
    value = technical or name.casefold()
    identity = ParticipantIdentity.objects.filter(namespace=namespace, value=value).select_related("participant").first()
    if identity:
        return identity.participant
    # Only a source phone can bind a business participant to an application
    # account; the extraction's suggested manager is not identity evidence.
    from .models import UserProfile, TeamMembership
    profiles = list(UserProfile.objects.filter(phone=technical, user_id__in=TeamMembership.objects.filter(team=candidate.team, status="active").values("user_id"))[:2]) if technical else []
    profile = profiles[0] if len(profiles) == 1 else None
    participant = Participant.objects.create(team=candidate.team, display_name=name, user_profile=profile)
    ParticipantIdentity.objects.create(participant=participant, namespace=namespace, value=value, resolution_state="resolved" if technical else "scoped_name")
    return participant


def _publish(candidate, decision, data, raw, mode):
    event = FactEvent.objects.create(
        decision=decision, team=candidate.team, project=candidate.project,
        event_key=f"decision:{decision.id}", event_type="payment_schedule" if mode == "schedule" else f"reported_{data['payment_kind']}" if mode == "observation" else candidate.fact_type,
        occurred_at=source_time(raw)[0], payload=json_value(data),
    )
    if mode == "observation":
        return event
    if mode == "schedule":
        if candidate.project_id and data.get("payment_date") and data.get("amount", 0) > 0:
            PaymentScheduleItem.objects.create(project=candidate.project, fact_event=event, due_date=data["payment_date"], amount=data["amount"], currency=data.get("currency", candidate.project.currency), direction=data["direction"], amount_precision=data["amount_precision"], is_verified=True)
        return event
    if candidate.fact_type == "commitment":
        task = Commitment.objects.filter(candidate=candidate).first()
        if task:
            task.participant = _participant(candidate, data, raw)
            task.save(update_fields=["participant"])
    if candidate.project_id:
        project = candidate.project
        for label in {project.name, data.get("object_name", "")}:
            normalized = normalize_deal_name(label)
            if normalized:
                ProjectAlias.objects.get_or_create(project=project, normalized_name=normalized, defaults={"decision": decision})
        for field in ("contract_amount", "cost_amount", "current_action", "next_action", "stage"):
            if field in data and data[field] != "":
                FieldAssertion.objects.create(fact_event=event, project=project, field_name=field, value=json_value(data[field]))
        # Mentioning a party does not authorize replacing the legal customer.
        if data.get("company_name"):
            parties = list(Company.objects.filter(name__iexact=data["company_name"]).filter(Q(team=candidate.team) | Q(team__isnull=True))[:2])
            if not parties:
                parties = [Company.objects.create(team=candidate.team, name=data["company_name"])]
            if len(parties) == 1:
                company = parties[0]
                CompanyAlias.objects.get_or_create(company=company, normalized_name=normalize_deal_name(data["company_name"]), defaults={"decision": decision})
                ProjectParty.objects.get_or_create(project=project, company=company, role=data["party_role"], defaults={"decision": decision})
    return event


def decide(candidate_id, *, preview=False):
    cfg = AISettings.get_active()
    if not cfg.autonomous_enabled and not preview:
        return None
    with transaction.atomic():
        from .dialogue_threads import lock_candidate_source
        lock_candidate_source(FactCandidate.objects.select_related("trace__raw_message__config").get(pk=candidate_id))
        candidate = FactCandidate.objects.select_for_update(of=("self",)).select_related("project", "team", "trace__raw_message__config", "thread_revision__thread__project").get(pk=candidate_id)
        if candidate.status != "pending":
            return None
        fingerprint = hashlib.sha256(json.dumps([candidate.proposed_changes, candidate.project.version if candidate.project_id else None, candidate.crm_match_revision, candidate.crm_match_state, list(candidate.evidence.values_list("raw_message_id", "quote"))], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if not preview:
            existing = candidate.decisions.filter(policy_version=cfg.autonomous_policy_version, input_fingerprint=fingerprint).first()
            if existing:
                return existing
        outcome, code, explanation, mode, data = "accepted", "source_validated", "Проверены цитаты WhatsApp, связь объекта, тип события и повторность.", "apply", {}
        try:
            raw, primary, refs, text = _evidence(candidate)
            schema = FactSchema(data=candidate.proposed_changes)
            schema.is_valid(raise_exception=True)
            data = schema.validated_data
            _resolve_project(candidate, data)
            if not preview and not candidate.project_id and data.get("object_name") and candidate.crm_match_state == "not_requested":
                from .tasks import enqueue_crm_match
                enqueue_crm_match(candidate.id)
                candidate.refresh_from_db()
            mode = _validate(candidate, data, primary, text)
            if candidate.fact_type == "project" and not candidate.project_id and candidate.crm_match_state in ("queued", "not_requested", "error"):
                raise Deferred("crm_search_pending", "Для исключения дубля объекта ожидается завершение поиска CRM.")
        except Deferred as exc:
            outcome = "rejected" if exc.rejected else "deferred"
            code, explanation = exc.code, exc.explanation
        except (Conflict, ValidationError, ProviderUnavailable) as exc:
            outcome, code, explanation = "deferred", "validation_failed", f"Проверка источника/схемы: {str(exc)[:500]}"
        previous = candidate.decisions.order_by("-id").first()
        try:
            with transaction.atomic():
                decision = FactDecision.objects.create(candidate=candidate, supersedes=previous, outcome=outcome, policy_version=cfg.autonomous_policy_version, input_fingerprint=fingerprint, reason_code=code, explanation=explanation, validation={"trace_id": candidate.trace_id, "model": candidate.trace.model_version, "coverage": candidate.trace.context_metadata.get("history_complete_in_request", False), "mode": mode, "discarded_fields": getattr(candidate, "_discarded_fields", {})})
                if outcome == "accepted":
                    if mode == "apply":
                        candidate = _apply_candidate(candidate.id, None, "approve", explanation, changes=json_value(data), base_version=candidate.project.version if candidate.project_id else 0, system=True)
                    else:
                        candidate.status, candidate.review_reason, candidate.reviewed_at = "approved", explanation, timezone.now()
                        candidate.save(update_fields=["status", "review_reason", "reviewed_at"])
                    if mode == "apply":
                        data = candidate.proposed_changes
                    event = _publish(candidate, decision, data, primary, mode)
                    from .autonomous_crm import enqueue_effects, enqueue_project
                    if not preview:
                        if candidate.project_id:
                            enqueue_project(candidate.project, event, cfg)
                        enqueue_effects(candidate, event, cfg)
                elif outcome == "rejected":
                    _apply_candidate(candidate.id, None, "reject", explanation, system=True)
                AuditEvent.objects.create(target_type="FactDecision", target_id=decision.id, action="autonomous_decision", before_after={"outcome": outcome, "reason_code": code, "candidate_id": candidate.id, "policy_version": cfg.autonomous_policy_version})
                if preview:
                    transaction.set_rollback(True)
                    return {"candidate_id": candidate.id, "outcome": outcome, "reason_code": code, "explanation": explanation}
                return decision
        except (Conflict, ValidationError) as exc:
            # Failed domain application is rolled back completely, then recorded
            # as deferred. No accepted decision may remain without its projection.
            if preview:
                transaction.set_rollback(True)
                return {"candidate_id": candidate.id, "outcome": "deferred", "reason_code": "application_conflict", "explanation": str(exc)[:1000]}
            return FactDecision.objects.create(candidate=candidate, supersedes=previous, outcome="deferred", policy_version=cfg.autonomous_policy_version, input_fingerprint=fingerprint, reason_code="application_conflict", explanation=str(exc)[:1000], validation={"trace_id": candidate.trace_id})


def refresh_checkpoint(raw):
    from .dialogue_threads import source_key
    rows = list(source_scope(raw).select_related("config"))
    dated = [(item, source_time(item)[0]) for item in rows]
    dated.sort(key=lambda pair: (pair[1] is not None, pair[1] or pair[0].timestamp, pair[0].id))
    unavailable_media = set(source_scope(raw).filter(artifacts__isnull=False).exclude(artifacts__state="ready").values_list("pk", flat=True))
    counts, gaps, complete, blocked = {}, [], None, False
    for item, sent_at in dated:
        state = item.processing_state
        if state in ("deleted", "superseded"):
            counts[state] = counts.get(state, 0) + 1
            continue
        if item.id in unavailable_media:
            state = "missing_media"
        elif sent_at is None:
            state = "unknown_source_time"
        counts[state] = counts.get(state, 0) + 1
        ok = item.processed and state not in ("failed", "received", "missing_media", "unknown_source_time")
        if not ok:
            blocked = True
            if len(gaps) < 100:
                gaps.append({"raw_message_id": item.id, "state": state})
        elif not blocked:
            complete = sent_at
    SourceCheckpoint.objects.update_or_create(team_id=raw.config.team_id if raw.config_id else raw.team_id, source_scope=source_key(raw), defaults={"complete_through": complete, "gaps": gaps, "counts": counts})


def reconcile(limit=100):
    """Bounded periodic repair. Identical inputs never re-run model calls."""
    cfg = AISettings.get_active()
    if not cfg.autonomous_enabled:
        return
    cursor = cfg.autonomous_reconcile_cursor
    candidates = list(FactCandidate.objects.filter(status="pending", team__is_active=True, pk__gt=cursor).order_by("id")[:limit])
    if not candidates:
        candidates = list(FactCandidate.objects.filter(status="pending", team__is_active=True).order_by("id")[:limit])
    for candidate in candidates:
        decide(candidate.id)
    if candidates:
        AISettings.objects.filter(pk=cfg.pk).update(autonomous_reconcile_cursor=candidates[-1].pk)
    for raw in RawMessage.objects.filter(processed=False, team__is_active=True).order_by("id")[:limit]:
        active = OutboxEvent.objects.filter(event_type="extract_message", payload__raw_id=raw.id, state__in=("pending", "enqueued", "processing")).exists()
        if not active and not OutboxEvent.objects.filter(event_type="extract_message", payload__raw_id=raw.id, state="failed").exists():
            OutboxEvent.objects.get_or_create(deduplication_key=f"recover:{raw.id}:{cfg.autonomous_policy_version}", defaults={"event_type": "extract_message", "payload": {"raw_id": raw.id}})

    active_ids = set()
    for payload in OutboxEvent.objects.filter(event_type="extract_message", state__in=("pending", "enqueued", "processing")).values_list("payload", flat=True):
        active_ids.add(payload.get("raw_id"))
        active_ids.update(payload.get("batch_ids", []))
    for item in SourceWorkItem.objects.filter(processing_version=cfg.autonomous_policy_version, state="pending").order_by("id")[:limit]:
        if item.raw_message_id in active_ids:
            continue
        OutboxEvent.objects.get_or_create(deduplication_key=f"work-recover:{item.id}:{cfg.autonomous_policy_version}", defaults={"event_type": "extract_message", "payload": {"raw_id": item.raw_message_id, "reanalyze": True, "batch_ids": [item.raw_message_id], "priority": "history"}})
    # Transient outages are probed again after quarantine; schema/auth failures
    # stay visible rather than looping forever. Daily reservations bound cost.
    from .ai_retries import RETRYABLE
    transient = RETRYABLE - {"invalid_extraction_schema", "invalid_schema", "fact_thread_missing", "batch_classification_missing", "thread_classification_invalid", "invalid_embedding"}
    OutboxEvent.objects.filter(state="failed", event_type="extract_message", error_code__in=transient, next_attempt_at__lt=timezone.now() - timedelta(hours=1)).update(state="pending", attempt_count=0, next_attempt_at=timezone.now(), lease_until=None)
    from django.conf import settings
    from .models import CrmDelivery
    for delivery in CrmDelivery.objects.filter(error_code="crm_stage_mapping_missing", outbox_event__state="failed").select_related("external_object_link", "outbox_event")[:limit]:
        project = Project.objects.filter(pk=delivery.external_object_link.local_id).first()
        if project and settings.BITRIX_STAGE_MAP.get(project.status):
            OutboxEvent.objects.filter(pk=delivery.outbox_event_id).update(state="pending", attempt_count=0, next_attempt_at=timezone.now(), lease_until=None)
    Commitment.objects.filter(is_verified=True, status="pending", deadline_at__lt=timezone.now()).update(status="overdue")
