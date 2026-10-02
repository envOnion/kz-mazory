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
        # In an explicitly imported chat, the transport timestamp is an import date.
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
