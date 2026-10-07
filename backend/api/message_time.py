"""Source dates are distinct from ingestion dates; export parsing is opt-in."""

import re
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

KAZAKHSTAN_OFFSET = dt_timezone(timedelta(hours=6))
EXPORT_HEADER = re.compile(
    r"\A\[(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2}(?::\d{2})?)\]\s*([^\n:]{1,255}):\s*\n"
)


def source_zone(raw):
    if raw.source == "whatsapp_export" and raw.raw_payload.get("export_timezone"):
        from .whatsapp_export_parser import export_zone
        return export_zone(raw.raw_payload["export_timezone"])
    snapshot = raw.config.snapshot if raw.config_id else {}
    name = snapshot.get("timezone", "UTC+06:00")
    match = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", name)
    if match:
        hours, minutes = int(match[2]), int(match[3])
        if hours <= 14 and minutes < 60 and (hours < 14 or minutes == 0):
            offset = timedelta(hours=hours, minutes=minutes)
            return dt_timezone(offset if match[1] == "+" else -offset)
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return KAZAKHSTAN_OFFSET


def source_time(raw):
    zone = source_zone(raw)
    if raw.source == "whatsapp_export":
        return raw.timestamp.astimezone(zone) if raw.sent_at_known else None, raw.sender_name, "export_header"
    snapshot = raw.config.snapshot if raw.config_id else {}
    cutoff = parse_datetime(snapshot.get("export_imported_until", ""))
    imported = snapshot.get("message_format") == "whatsapp_export" and (
        cutoff is None or (timezone.is_aware(cutoff) and raw.timestamp <= cutoff)
    )
    if imported:
        match = EXPORT_HEADER.match(raw.content)
        if match:
            try:
                fmt = "%d.%m.%Y %H:%M:%S" if len(match[2]) == 8 else "%d.%m.%Y %H:%M"
                sent = datetime.strptime(f"{match[1]} {match[2]}", fmt).replace(
                    tzinfo=zone
                )
                if sent <= raw.received_at + timedelta(days=1):
                    return sent, match[3].strip(), "export_header"
            except ValueError:
                pass
        # A configured export chat can also contain native messages fetched from
        # WAHA history. Their provider timestamp remains a send date, not the
        # export ingestion date. Require matching immutable transport evidence.
        payload = raw.raw_payload.get("payload", {})
        stamp = payload.get("timestamp") if isinstance(payload, dict) else None
        native = (
            not re.match(r"\A\[\d{2}\.\d{2}\.\d{4}", raw.content)
            and raw.source == "waha" and raw.sent_at_known
            and raw.raw_payload.get("event") in ("message", "message.any", "history.import")
            and raw.raw_payload.get("session") == raw.session_name
            and isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
            and payload.get("id") == raw.message_id and payload.get("body") == raw.content
            and abs(stamp - raw.timestamp.timestamp()) < 1
        )
        if native:
            return raw.timestamp.astimezone(zone), raw.sender_name, "message_metadata"
        # An export without its source header has no proven send date.
        return None, raw.sender_name, "unknown_export_date"
    sent = raw.timestamp.astimezone(zone) if raw.sent_at_known else None
    return sent, raw.sender_name, "message_metadata" if sent else "unknown"


def source_metadata(raw):
    sent, sender, basis = source_time(raw)
    offset = (sent or timezone.now().astimezone(source_zone(raw))).strftime("%z")
    return {
        "sent_at": sent.isoformat() if sent else None,
        "sender": sender,
        "time_basis": basis,
        "transport_sent_at": raw.timestamp.isoformat() if raw.sent_at_known else None,
        "received_at": raw.received_at.isoformat(),
        "timezone": "UTC" + offset[:3] + ":" + offset[3:],
    }


def grounded_dates(text, raw, *, default_to_source=False):
    """Only explicit calendar dates and source-relative days are evidence."""
    from datetime import date
    text = EXPORT_HEADER.sub("", text)
    sent = source_time(raw)[0]
    dates = set()
    for year, month, day in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        try:
            dates.add(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    for day, month, year in re.findall(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?!\d)", text):
        if not year and not sent:
            continue
        try:
            numeric_year = (2000 + int(year) if len(year) == 2 else int(year)) if year else sent.year
            dates.add(date(numeric_year, int(month), int(day)))
        except ValueError:
            pass
    months = 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
    for day, month, year in re.findall(r"\b(\d{1,2})\s+(" + '|'.join(months) + r")(?:\s+(\d{4}))?\b", text.casefold()):
        if year or sent:
            try:
                dates.add(date(int(year) if year else sent.year, months.index(month) + 1, int(day)))
            except ValueError:
                pass
    if sent:
        relative = {'сегодня': 0, 'вчера': -1, 'позавчера': -2, 'завтра': 1, 'послезавтра': 2}
        words = set(re.findall(r'\w+', text.casefold()))
        dates.update(sent.date() + timedelta(days=offset) for word, offset in relative.items() if word in words)
        if default_to_source and not dates:
            dates.add(sent.date())
    return dates
