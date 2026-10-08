"""Ground contextual obligations in accessible messages, not model-generated dates."""

import re
from datetime import datetime, time, timedelta
from .message_context import source_scope
from .message_time import EXPORT_HEADER, grounded_dates, source_time, source_zone
from .providers import ProviderUnavailable


def validate_commitment(fact, raw, snapshot_id, quote_match, threaded=False, autonomous=None):
    from .models import AISettings
    if autonomous is None:
        autonomous = AISettings.get_active().autonomous_enabled
    scope = source_scope(raw).filter(id__lte=snapshot_id)
    refs = fact.get("evidence_messages") or [
        {"raw_message_id": raw.id, "quote": fact["evidence"], "role": "promise"}
    ]
    ids = {item["raw_message_id"] for item in refs}
    rows = {item.id: item for item in scope.filter(id__in=ids).select_related("config")}
    if set(rows) != ids or raw.id not in ids:
        raise ProviderUnavailable("commitment_evidence_unavailable")
    for item in refs:
        item["quote"] = quote_match(item["quote"], rows[item["raw_message_id"]].content)
    fact["evidence_messages"] = refs
    if fact.get("evidence_messages") and not any(
        ref["raw_message_id"] == raw.id and ref["role"] == "promise" for ref in refs
    ):
        raise ProviderUnavailable("commitment_promise_evidence_unavailable")
    body = EXPORT_HEADER.sub("", raw.content).strip().casefold().rstrip(".! ")
    short_reply = body in {
        "тогда завтра",
        "принято",
        "ок",
        "понял",
        "ага",
        "да",
        "хорошо",
        "спасибо",
    }
    if short_reply and not (threaded and any(ref["role"] == "request" and ref["raw_message_id"] != raw.id for ref in refs) and len(fact.get("commitment_text", "").strip()) >= 12):
        return False
    if threaded and (body.endswith("?") or not fact.get("commitment_text", "").strip()):
        return False
    if fact.get("promise_message_id", raw.id) != raw.id:
        return False
    if fact.get("assignment_kind", "promise") == "promise":
        fact["responsible_name"] = source_time(raw)[1] or fact.get(
            "responsible_name", ""
        )
    deadline_id = fact.get("deadline_message_id") or raw.id
    dated_changes = [rows[item['raw_message_id']] for item in refs if item['role'] == 'deadline']
    if threaded and dated_changes:
        # The model has explicitly linked these originals as deadline events.
        # Use the latest linked event, never an earlier superseded promise date.
        latest = max(dated_changes, key=lambda message: (message.timestamp if message.sent_at_known else message.received_at, message.id))
        deadline_id = latest.id
        fact['deadline_message_id'] = deadline_id
    # Never trust an arbitrary source ID or a model supplied fulfillment timestamp.
    if deadline_id not in rows:
        raise ProviderUnavailable("commitment_deadline_evidence_unavailable")
    date_message = rows[deadline_id]
    sent, _, _ = source_time(date_message)
    deadline_text = " ".join(
        item["quote"] for item in refs if item["raw_message_id"] == deadline_id
    )
    deadline_text = EXPORT_HEADER.sub("", deadline_text)
    relative = re.search(r"\b(завтра|сегодня|послезавтра)\b", deadline_text, re.I)
    morning = bool(re.search(r"\b(утром|с\s+утра)\b", deadline_text, re.I))
    explicit_clock = re.search(r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b|\b(?:к|в|до)\s+(?:[01]?\d|2[0-3])\s*(?:час|ч\b)", deadline_text, re.I)
    if autonomous and fact.get("deadline_at") and not explicit_clock:
        fact["deadline_precision"] = "date"
        fact["deadline_at"] = fact["deadline_at"].astimezone(source_zone(date_message)).replace(hour=23, minute=59, second=59, microsecond=0)
    if sent is None and (relative or morning):
        fact["deadline_at"], fact["deadline_precision"] = None, "unknown"
        fact["uncertainties"].append(
            "Исходная дата отправки неизвестна: относительный срок требует уточнения."
        )
    elif sent and relative:
        days = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[relative[1].lower()]
        day = sent.date() + timedelta(days=days)
        predicted = fact.get("deadline_at")
        clock = (
            time(23, 59, 59)
            if autonomous and (morning or fact["deadline_precision"] == "date" or not predicted)
            else time(9)
            if morning
            else time(18)
            if fact["deadline_precision"] == "date" or not predicted
            else predicted.astimezone(source_zone(date_message)).time()
        )
        fact["deadline_at"] = datetime.combine(
            day, clock, tzinfo=source_zone(date_message)
        )
        fact["deadline_precision"] = (
            "date"
            if autonomous and (morning or not predicted or fact["deadline_precision"] == "date")
            else "datetime"
            if morning
            else fact["deadline_precision"]
            if predicted
            else "date"
        )
    elif sent and morning and fact.get("deadline_at") and not autonomous:
        fact["deadline_at"] = (
            fact["deadline_at"]
            .astimezone(source_zone(date_message))
            .replace(hour=9, minute=0, second=0, microsecond=0)
        )
    if morning and fact.get("deadline_at") and not autonomous:
        fact["deadline_basis"] = "morning_default"
        note = "Время 09:00 уточнено по правилу для утреннего срока (UTC+6)."
        if note not in fact["uncertainties"]:
            fact["uncertainties"].append(note)
    if autonomous and fact.get("deadline_at"):
        supported = grounded_dates(deadline_text, date_message, default_to_source=bool(explicit_clock))
        if fact["deadline_at"].astimezone(source_zone(date_message)).date() not in supported:
            fact["deadline_at"], fact["deadline_precision"] = None, "unknown"
            fact["uncertainties"].append("Срок не подтвержден календарной датой или относительным днем в цитате WhatsApp.")
    fact["fulfilled_at"] = None
    if fact.get('commitment_status') == 'cancelled':
        cancellations = [item for item in refs if item['role'] == 'cancellation']
        promised = source_time(raw)[0]
        if not cancellations or any(source_time(rows[item['raw_message_id']])[0] is None
                                    or (promised and source_time(rows[item['raw_message_id']])[0] < promised)
                                    for item in cancellations):
            raise ProviderUnavailable('commitment_cancellation_evidence_unavailable')
        text = ' '.join(item['quote'] for item in cancellations).casefold()
        if text.strip().rstrip('.! ') in {'принято', 'ок', 'спасибо', 'понял', 'да'} or text.rstrip().endswith('?') or re.fullmatch(r'https?://\S+', text.strip()):
            raise ProviderUnavailable('commitment_cancellation_ambiguous')
    if fact.get("commitment_status") == "fulfilled":
        fulfilled_id = fact.get("fulfillment_message_id")
        fulfillment = [
            item
            for item in refs
            if item["role"] == "fulfillment" and item["raw_message_id"] == fulfilled_id
        ]
        if fulfilled_id not in rows or not fulfillment:
            raise ProviderUnavailable("commitment_fulfillment_evidence_unavailable")
        text = " ".join(item["quote"] for item in fulfillment).strip().casefold()
        if text in {"принято", "ок", "спасибо", "понял", "да"} or re.fullmatch(
            r"https?://\S+", text
        ):
            raise ProviderUnavailable("commitment_fulfillment_ambiguous")
        completed = source_time(rows[fulfilled_id])[0]
        promised = source_time(raw)[0]
        if completed is None or (promised and completed < promised):
            raise ProviderUnavailable("commitment_fulfillment_date_invalid")
        fact["fulfilled_at"] = completed
    fact["uncertainties"] = list(dict.fromkeys(fact["uncertainties"]))
    return True
