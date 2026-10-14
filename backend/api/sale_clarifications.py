"""Human price answers received through the authenticated WhatsApp inbox.

Provider reads and delivery enqueueing run only from the existing Q2 reply job.
An answer is a human source, not a fabricated response from the extraction model.
"""

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import (
    AISettings, AuditEvent, BitrixSettings, ClarificationReply, CrmDelivery,
    FactCandidate, FactClarification, FactDecision, FactEvent, FactEvidence,
    FieldAssertion, MessageProcessingTrace, OutboxEvent, Project, RawMessage,
    WhatsAppConfig,
)
from .providers import ProviderUnavailable


CURRENCY_PATTERNS = {
    "KZT": r"\bKZT\b|тенге|\bтг\b|₸",
    "USD": r"\bUSD\b|доллар|\$",
    "EUR": r"\bEUR\b|евро|€",
    "RUB": r"\bRUB\b|\bруб\b|рубл|₽",
}
UNKNOWN = re.compile(r"\b(?:не знаю|не уверен[а]?|не могу уточнить|пока неизвестно|оставь(?:те)?(?: без подтверждения| неподтвержд[её]нным| не подтвержд[её]нным)|без подтверждения|не подтверждаю|позже уточню|цен[ауы](?: ещ[её])? не (?:определена|согласована|знаю)|кп(?: ещ[её])? нет|это дубль)\b", re.I)
OTHER_AMOUNT = re.compile(r"прибыл|марж|себестоим|затрат|закуп|аванс|предоплат|оплат|плат[её]ж|поступ|перечисл|остаток|долг|рассрочк|бюджет|конкурент|процент|мвт|квт|мощност|телефон|номер|%", re.I)
APPROXIMATE = re.compile(r"\b(?:около|порядка|примерн\w*|приблизительн\w*|почти|ориентировочн\w*|от|до|более|менее|больше|меньше|неточн\w*)\b|\bне\s+точн\w*|[~<>]", re.I)
PRICE_LABEL = re.compile(r"цен[ауы]|сумм[аыуе]|стоимост|\bкп\b|предложени", re.I)
UNCERTAIN = re.compile(r"\b(?:если|возможно|наверное|предполож\w*|неверно|неактуаль\w*|ошибоч\w*|чуж\w*)\b|\bне\s+(?:наш\w*|мо[яйе]|цен\w*|стоимост\w*|сумм\w*|надо|нужно|пиши\w*|записыва\w*|вноси\w*|применя\w*|\d)|\b(?:цена|стоимость|сумма)\s+не\b", re.I)
MAX_AMOUNT = Decimal("999999999999.99")


def baseline_matches(current, baseline):
    """Ignore decimal formatting, but never a change in identity or price."""
    if not isinstance(current, dict) or not isinstance(baseline, dict):
        return False
    for key in ("ID", "TITLE", "CURRENCY_ID", "ORIGINATOR_ID", "ORIGIN_ID"):
        if key not in baseline or str(current.get(key) or "") != str(baseline.get(key) or ""):
            return False
    try:
        actual, expected = Decimal(str(current.get("OPPORTUNITY") or 0)), Decimal(str(baseline.get("OPPORTUNITY") or 0))
        return actual.is_finite() and expected.is_finite() and actual == expected
    except (InvalidOperation, ValueError, TypeError):
        return False


def valid(question):
    """Database-only validity for the pump and the locked reply handler."""
    from . import access

    if question.kind != "sale_amount" or not question.project_id:
        return False
    project, context = question.project, question.price_context
    if not isinstance(context, dict) or not AISettings.get_active().autonomous_enabled:
        return False
    if question.resolution.get("preserved_hold") or context.get("no_writes"):
        return False
    if project.archived or project.team_id != question.config.team_id or question.candidate.team_id != project.team_id:
        return False
    # The original version is audit context. Status/action updates do not
    # invalidate a still-unanswered question about this object's missing price.
    if type(context.get("project_version")) is not int:
        return False
    baseline = context.get("baseline")
    if not isinstance(baseline, dict) or str(baseline.get("ID") or "") != str(project.bitrix_id or "") or not project.bitrix_id:
        return False
    if not baseline_matches(baseline, baseline):
        return False
    if baseline.get('TITLE') != project.name:
        return False
    if baseline.get("CURRENCY_ID") not in CURRENCY_PATTERNS or baseline["CURRENCY_ID"] != project.currency:
        return False
    from .commercial_amounts import commercial_amount
    if commercial_amount(project) is not None:
        return False
    if not access.projects_for(question.recipient).filter(pk=project.pk).exists():
        return False
    # A price answer cannot resolve an independent identity/source-review hold.
    # The price question's own anchor/answer are excluded so a later explicit
    # price may legitimately clarify an earlier "не знаю" about price alone.
    from .deduplication import normalize_deal_name
    labels = {normalize_deal_name(project.name), *project.aliases.values_list('normalized_name', flat=True)}
    sources = {item.get('source_id') for item in context.get('evidence', []) if isinstance(item, dict)}
    originals = project.candidates.filter(fact_type='project').exclude(source_key__startswith="price-question:").exclude(source_key__startswith="sale-clarification-reply:")
    for candidate in originals:
        relevant_object = normalize_deal_name(candidate.proposed_changes.get('object_name', '')) in labels
        if not relevant_object:
            continue
        review = candidate.decisions.filter(validation__has_key='review_hold').order_by('-id').first()
        if review and review.validation.get('review_hold') and (candidate.trace.raw_message_id in sources or candidate.evidence.filter(raw_message_id__in=sources).exists()):
            return False
        declined = candidate.decisions.filter(actor_kind='human', outcome='deferred', reason_code='user_requested_unconfirmed').order_by('-id').first()
        answered = candidate.clarifications.filter(kind='identity', state__in=('answered', 'resolved')).order_by('-updated_at').first()
        if declined and (not answered or declined.created_at > answered.updated_at):
            return False
    for identity in FactClarification.objects.filter(kind='identity', config_id=question.config_id,
            state__in=('queued', 'awaiting', 'unconfirmed'), candidate__status='pending', candidate__fact_type='project').select_related('candidate'):
        names = [identity.candidate.proposed_changes.get('object_name', ''), *identity.source_snapshot.get('object_aliases', [])]
        if any(normalize_deal_name(name) in labels for name in names if isinstance(name, str) and name):
            return False
    if FieldAssertion.objects.filter(project=project, field_name__in=("sale_amount", "contract_amount"),
            fact_event__is_superseded=False, created_at__gt=question.created_at).exists():
        return False
    evidence = context.get("evidence", [])
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if not isinstance(item, dict) or type(item.get("source_id")) is not int or not isinstance(item.get("quote"), str):
            return False
        raw = RawMessage.objects.filter(pk=item["source_id"], config_id=question.config_id).exclude(processing_state__in=("deleted", "superseded")).first()
        if not raw or not item["quote"].strip() or item["quote"] not in raw.content:
            return False
        display_quote = item.get("display_quote", item["quote"])
        if not isinstance(display_quote, str) or display_quote and display_quote not in item["quote"]:
            return False
    offers = context.get("offers", [])
    if not isinstance(offers, list):
        return False
    for offer in offers:
        if not isinstance(offer, dict) or type(offer.get("source_id")) is not int or not isinstance(offer.get("quote"), str):
            return False
        raw = RawMessage.objects.filter(pk=offer["source_id"], config_id=question.config_id).exclude(processing_state__in=("deleted", "superseded")).first()
        if not raw or not offer["quote"].strip() or offer["quote"] not in raw.content or offer.get("currency") != project.currency:
            return False
        from .autonomous import _money_matches
        try:
            amount = Decimal(str(offer.get("amount")))
            if not amount.is_finite() or not Decimal("0.01") <= amount <= MAX_AMOUNT or amount != amount.quantize(Decimal("0.01")) or not _money_matches(amount, offer["quote"], monetary_unit=True):
                return False
        except (InvalidOperation, ValueError, TypeError):
            return False
    return True


def _money(value, currency):
    try:
        return f"{Decimal(str(value)):,.2f}".replace(",", " ").removesuffix(".00") + " " + currency
    except (InvalidOperation, ValueError, TypeError):
        return "сумма не указана"


def message(question):
    context = question.price_context
    baseline = context["baseline"]
    currency = baseline["CURRENCY_ID"]
    label = baseline.get("TITLE") or question.project.name
    current = Decimal(str(baseline.get("OPPORTUNITY") or 0))
    lines = [f"По сделке «{label}» №{baseline['ID']} в Bitrix " +
        (f"сейчас указано {_money(current, currency)}." if current else "сумма продажи не указана."),
        "Почему спрашиваю: " + str(context.get("reason") or "В доступной переписке не удалось однозначно определить цену продажи этого объекта.")]
    if context.get("current_scope"):
        lines.append("Состав этой сделки: " + str(context["current_scope"]))
    for item in context.get("evidence", [])[:3]:
        quote = item.get("display_quote", item["quote"]).strip()
        if not quote:
            continue
        if len(quote) > 550:
            quote = quote[:550].rstrip() + "…"
        date = str(item.get("source_date") or "дата неизвестна")
        lines.append(f"В переписке от {date}: «{quote}»")
    offers = context.get("offers", [])
    if offers:
        lines.append("В источниках есть такие варианты цены:")
        for index, offer in enumerate(offers, 1):
            lines.append(f"{index}. {offer.get('label') or 'Предложение'} — {_money(offer.get('amount'), offer.get('currency') or currency)}")
    lines.append(str(context.get("question") or f"Какую сумму продажи или нашего КП записать в эту сделку ({currency})?"))
    lines.append(f"Нужна полная цена именно этой продажи в {currency}, отдельно от аванса, прибыли и оплаты. Подписания договора ждать не нужно.")
    if context.get("issue_kind") == "possible_duplicate":
        lines.append("Если это дубль другой сделки, ответьте «это дубль»: сумму оставлю без подтверждения; сделки автоматически не объединяю.")
    lines.append(("Ответьте номером варианта или точной суммой с единицами, " if offers else "Ответьте точной суммой с единицами, ") +
        f"например «12 млн {currency}». Если цена неизвестна — «не знаю»: вопрос останется без подтверждения и повторно отправляться не будет.")
    lines.append(f"Ответьте на это сообщение или укажите код: MZ-{question.id}.")
    return "\n\n".join(lines)


def _answer(question, body):
    """Parse one price or an explicit offered choice, never infer a total."""
    from .autonomous import MONEY

    text = re.sub(r"\bMZ-\d+\b", "", body, flags=re.I).strip(" .,:;")
    currency = question.price_context["baseline"]["CURRENCY_ID"]
    if UNKNOWN.search(text):
        return {"unknown": True}
    if question.price_context.get('issue_kind') == 'possible_duplicate':
        if re.search(r'\b(?:дубл\w*|та же (?:продажа|поставка|сделка)|то же|не отдельн\w*|не самостоятельн\w*)\b', text, re.I):
            return {'unknown': True}
        if not re.search(r'\bотдельная (?:поставка|продажа)\b', text, re.I):
            return {'error': 'clarification_answer_unclear'}
    offers = question.price_context.get("offers", [])
    index = re.fullmatch(r"(?:(?:выбираю\s+)?вариант\s*|№\s*)?(\d+)", text, re.I)
    selected = int(index[1]) if index and offers and len(index[1]) <= len(str(len(offers))) else None
    if index and offers and (not selected or not 0 < selected <= len(offers)):
        return {'error': 'clarification_answer_unclear'}
    names = [i for i, offer in enumerate(offers, 1) if str(offer.get('label') or '').casefold() == text.casefold()]
    if not selected and len(names) == 1:
        selected = names[0]
    if selected and 0 < selected <= len(offers):
        offer = offers[selected - 1]
        try:
            amount = Decimal(str(offer["amount"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return {"error": "clarification_answer_unclear"}
        if offer.get("currency") != currency:
            return {"error": "clarification_currency_mismatch"}
        if not amount.is_finite() or not Decimal("0.01") <= amount <= MAX_AMOUNT or amount != amount.quantize(Decimal("0.01")):
            return {"error": "clarification_answer_unclear"}
        return {"amount": amount, "currency": currency, "offer_index": selected, "offer": offer}
    currencies = {code for code, pattern in CURRENCY_PATTERNS.items() if re.search(pattern, text, re.I)}
    other_currencies = re.findall(r"\b(?:[A-Z]{3}|фунт\w*|юан\w*|дирхам\w*|сом(?:ов|а)?|сум(?:ов|ы)?)\b", text, re.I)
    scope_labels = ' '.join([str(question.price_context.get('current_scope') or ''), *[str(offer.get('label') or '') for offer in offers]])
    scope_names = {value.upper() for value in re.findall(r'\b[A-Z]{3}\b', scope_labels, re.I)} - set(CURRENCY_PATTERNS)
    if currencies and currencies != {currency} or any(value.upper() not in CURRENCY_PATTERNS and value.upper() not in scope_names for value in other_currencies):
        return {"error": "clarification_currency_mismatch"}
    if OTHER_AMOUNT.search(text) or APPROXIMATE.search(text) or UNCERTAIN.search(text) or "?" in text or re.search(r"[-−–—]\s*\d", text):
        return {"error": "clarification_answer_unclear"}
    numbers = list(MONEY.finditer(text))
    if len(numbers) != 1:
        return {"error": "clarification_answer_unclear"}
    token = numbers[0]
    number = token["number"].replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    if number.count(".") >= 2:
        number = number.replace(".", "")
    scale = (token["scale"] or "").casefold()
    multiplier = 1_000_000_000 if scale.startswith(("млрд", "миллиард")) else 1_000_000 if scale.startswith(("млн", "миллион")) else 1000 if scale.startswith("тыс") else 1
    bare = text == token.group().strip()
    if not (bare or scale or currencies or PRICE_LABEL.search(text)):
        return {"error": "clarification_answer_unclear"}
    try:
        amount = Decimal(number.replace(",", ".")) * multiplier
    except InvalidOperation:
        return {"error": "clarification_answer_unclear"}
    if not amount.is_finite() or not Decimal("0.01") <= amount <= MAX_AMOUNT or amount != amount.quantize(Decimal("0.01")):
        return {"error": "clarification_answer_unclear"}
    return {"amount": amount, "currency": currency}


def _reply_source(reply, question):
    raw = reply.raw_message
    if not raw or raw.source != "waha" or raw.config_id != question.config_id or raw.team_id != question.config.team_id:
        return None
    if raw.session_name != reply.session or raw.message_id != reply.provider_message_id or raw.content.strip() != reply.body.strip() or raw.sender_phone != question.source_snapshot.get("phone"):
        return None
    if not raw.sent_at_known or raw.processing_state in ("deleted", "superseded"):
        return None
    if raw.raw_payload.get("clarification_question_id") != question.id:
        return None
    # This timestamp is transport evidence; ingestion time is not substituted.
    from .message_time import source_time
    stamp = source_time(raw)[0]
    if stamp is None or stamp < question.created_at.replace(microsecond=0) or stamp > timezone.now() + timedelta(minutes=5):
        return None
    return raw


def _audit_reply(reply):
    AuditEvent.objects.create(actor=reply.sender, target_type="ClarificationReply", target_id=reply.id,
        action="clarification_answer", before_after={"question_id": reply.question_id, "kind": "sale_amount", "state": reply.state, "reason": reply.error_code})
    OutboxEvent.objects.get_or_create(deduplication_key=f"clarification-pump:reply:{reply.id}", defaults={"event_type": "clarification_pump", "payload": {}})


def resolve(reply_id):
    """Apply a single authenticated answer; called by the Q2 reply task only."""
    from .bitrix_service import BitrixService
    from .clarifications import authorized, digest

    initial = ClarificationReply.objects.select_related("question").get(pk=reply_id)
    if initial.state != "queued" or initial.question.kind != "sale_amount":
        return
    with transaction.atomic():
        # Ingestion/replay take the same source-scope lock before project locks.
        # This prevents source edits during the bounded CRM precondition read.
        WhatsAppConfig.objects.select_for_update().get(pk=initial.question.config_id)
        project = Project.objects.select_for_update().filter(pk=initial.question.project_id).first()
        question = FactClarification.objects.select_for_update(of=("self",)).select_related("recipient", "config__team", "candidate").get(pk=initial.question_id)
        question.project = project
        reply = ClarificationReply.objects.select_for_update(of=("self",)).select_related("raw_message", "sender").get(pk=reply_id)
        if reply.state != "queued":
            return
        if reply.sender_id != question.recipient_id or question.state not in ("awaiting", "unconfirmed") or question.expires_at <= timezone.now() or not authorized(question) or not valid(question):
            reply.state, reply.error_code = "ignored", "clarification_stale"
            if question.state in ("awaiting", "queued"):
                question.state = "superseded"
                question.save(update_fields=["state"])
        else:
            raw = _reply_source(reply, question)
            answer = _answer(question, reply.body) if raw else {"error": "clarification_stale"}
            if answer.get("unknown"):
                question.state = "unconfirmed"
                question.resolution = {"reply_id": reply.id, "user_answer": reply.body, "external_writes_allowed": False}
                question.save(update_fields=["state", "resolution"])
                FactDecision.objects.create(candidate=question.candidate, supersedes=question.candidate.decisions.order_by("-id").first(),
                    actor_kind="human", outcome="deferred", policy_version=AISettings.get_active().autonomous_policy_version,
                    input_fingerprint=digest(["sale-clarification-unknown", question.id, reply.id]), reason_code="user_requested_unconfirmed",
                    explanation=reply.body, validation={"clarification_id": question.id, "reply_id": reply.id, "kind": "sale_amount", "external_writes_allowed": False})
                reply.state = "applied"
            elif answer.get("error"):
                reply.state, reply.error_code = "ignored", answer["error"]
                if reply.error_code != "clarification_stale":
                    from .notifications import create_notification
                    create_notification(question.recipient, f"Уточнение суммы MZ-{question.id}",
                        f"Не удалось однозначно определить цену продажи. Укажите одну полную сумму в {project.currency}, номер варианта из вопроса или «не знаю». Запись удержана.",
                        "crm_clarification", f"clarification-answer:{reply.id}", whatsapp=True, project=project)
            else:
                cfg = AISettings.get_active()
                if not cfg.autonomous_crm_enabled or not BitrixSettings.get_active().is_active:
                    raise ProviderUnavailable("autonomous_crm_paused")
                baseline = question.price_context["baseline"]
                current = BitrixService.call("crm.deal.get", {"id": baseline["ID"]}).get("result")
                if not baseline_matches(current, baseline):
                    question.state = "superseded"
                    question.save(update_fields=["state"])
                    reply.state, reply.error_code = "ignored", "clarification_crm_changed"
                elif FieldAssertion.objects.filter(project=project, field_name__in=("sale_amount", "contract_amount"),
                        fact_event__is_superseded=False, fact_event__occurred_at__gt=raw.timestamp).exists():
                    question.state = "superseded"
                    question.save(update_fields=["state"])
                    reply.state, reply.error_code = "ignored", "clarification_stale"
                else:
                    _apply_answer(question, reply, raw, answer, project, cfg)
        reply.save(update_fields=["state", "error_code"])
        if reply.error_code in ('clarification_stale', 'clarification_crm_changed'):
            from .notifications import create_notification
            create_notification(question.recipient, f"Ответ на вопрос MZ-{question.id} сохранён",
                'Запись удержана: вопрос или данные сделки уже изменились. Ваш ответ сохранён в журнале, сумма в Bitrix по нему не перезаписана.',
                'crm_clarification', f'clarification-answer:{reply.id}', whatsapp=True, project=project)
        _audit_reply(reply)


def _apply_answer(question, reply, raw, answer, project, cfg):
    from .autonomous_crm import enqueue_project
    from .clarifications import digest
    from .facts import json_value, record_revision
    from .message_time import source_time
    from .replay_replacement import row_snapshot
    from .temporal import provenance

    amount = answer["amount"].quantize(Decimal("0.01"))
    data = {"fact_type": "project", "object_name": project.name, "sale_amount": str(amount),
        "currency": answer["currency"], "evidence_message_id": raw.id, "evidence": raw.content,
        "evidence_messages": [{"raw_message_id": raw.id, "quote": raw.content, "role": "amount"}]}
    context = {"actor_kind": "human", "clarification_id": question.id, "reply_id": reply.id,
        "question_text": message(question), "price_context": question.price_context,
        "selection": {key: value for key, value in answer.items() if key in ("offer_index", "offer")}}
    trace = MessageProcessingTrace.objects.create(raw_message=raw, project=project,
        operation_key=f"sale-clarification-reply:{reply.id}", model_version="human-whatsapp-answer", prompt_version="sale-clarification-v1",
        whatsapp_message_id=raw.message_id, whatsapp_chat_id=raw.chat_id, whatsapp_sender_phone=raw.sender_phone,
        whatsapp_sender_name=raw.sender_name, whatsapp_timestamp=raw.timestamp, whatsapp_content=raw.content,
        whatsapp_raw_payload=raw.raw_payload, pipeline_action="updated_deal", status="success", context_metadata=context)
    candidate = FactCandidate.objects.create(trace=trace, project=project, team=project.team, manager=project.manager,
        fact_type="project", proposed_changes=data, source_key=f"sale-clarification-reply:{reply.id}",
        status="approved", confidence=1, base_project_version=project.version, crm_match_state="matched",
        reviewed_by=reply.sender, reviewed_at=timezone.now(), review_reason=f"Ответ получателя на WhatsApp MZ-{question.id}")
    FactEvidence.objects.create(candidate=candidate, raw_message=raw, quote=raw.content, field_name="amount")
    decision = FactDecision.objects.create(candidate=candidate, actor_kind="human", outcome="accepted",
        policy_version=cfg.autonomous_policy_version, input_fingerprint=digest(["sale-answer", question.id, reply.id, data]),
        reason_code="sale_amount_clarified", explanation=f"Получатель вопроса MZ-{question.id} явно указал цену продажи.",
        validation={**context, "external_writes_allowed": True, "crm_baseline_checked": True})
    event = FactEvent.objects.create(team=project.team, project=project, decision=decision,
        event_key=f"sale-clarification-reply:{reply.id}", event_type="project", occurred_at=source_time(raw)[0],
        payload={**data, "clarification_id": question.id, "reply_id": reply.id, "question_context": question.price_context,
            **({"selected_offer": answer["offer"]} if answer.get("offer") else {})})
    before = row_snapshot(project)
    with provenance(actor_id=reply.sender_id, raw_message_id=raw.id, fact_event_id=event.id):
        project.sale_amount = amount
        project.whatsapp_fields = sorted(set(project.whatsapp_fields) | {"sale_amount"})
        project.version += 1
        project.needs_bitrix_sync = True
        project.save(update_fields=["sale_amount", "whatsapp_fields", "version", "needs_bitrix_sync"])
        FieldAssertion.objects.create(fact_event=event, project=project, field_name="sale_amount", value=str(amount))
        record_revision(project, reply.sender, project.status)
    candidate.materialization_snapshot = {"project_before": before, "project_after": row_snapshot(project)}
    candidate.save(update_fields=["materialization_snapshot"])
    enqueue_project(project, event, cfg, price_only=True, crm_baseline=question.price_context["baseline"])
    delivery = CrmDelivery.objects.get(outbox_event__deduplication_key=f"autonomous-crm:{project.id}:{project.version}")
    if delivery.patch != {"OPPORTUNITY": str(amount), "CURRENCY_ID": answer["currency"]}:
        raise ProviderUnavailable("clarification_price_patch_invalid")
    if question.candidate.status == "pending" and question.candidate.source_key.startswith("price-question:"):
        question.candidate.status = "superseded"
        question.candidate.save(update_fields=["status"])
    question.candidates.add(candidate)
    question.state = "answered"
    question.resolution = {"reply_id": reply.id, "raw_message_id": raw.id, "deal_id": project.bitrix_id,
        "amount": str(amount), "currency": answer["currency"], "applied_candidate_ids": [candidate.id],
        "fact_event_id": event.id, "delivery_id": delivery.id, "project_version": project.version,
        "external_writes_allowed": True, **({"offer_index": answer["offer_index"]} if "offer_index" in answer else {})}
    question.save(update_fields=["state", "resolution"])
    reply.state = "applied"
    AuditEvent.objects.create(actor=reply.sender, target_type="Project", target_id=project.id,
        action="clarification_sale_amount", before_after=json_value({"question_id": question.id, "reply_id": reply.id,
            "before": {"sale_amount": before.get("sale_amount"), "version": before["version"]},
            "after": {"sale_amount": str(amount), "version": project.version}, "delivery_id": delivery.id}))


def finish(question):
    """True only after the precise price command completed provider readback."""
    if question.kind != "sale_amount" or question.state != "answered":
        return False
    result = question.resolution
    delivery = CrmDelivery.objects.filter(pk=result.get("delivery_id"),
        fact_event_id=result.get("fact_event_id"), state="delivered").select_related("external_object_link").first()
    if not delivery or str(delivery.external_object_link.external_id) != str(result.get("deal_id")):
        return False
    if delivery.target_version != result.get("project_version") or delivery.patch.get("CURRENCY_ID") != result.get("currency"):
        return False
    try:
        return Decimal(str(delivery.patch.get("OPPORTUNITY"))) == Decimal(str(result.get("amount")))
    except (InvalidOperation, ValueError, TypeError):
        return False
