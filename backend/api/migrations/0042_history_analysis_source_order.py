from datetime import datetime, timezone
from django.db import migrations


def source_order(apps, schema_editor):
    Event = apps.get_model('api', 'OutboxEvent')
    Raw = apps.get_model('api', 'RawMessage')
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    batch = []
    for event in Event.objects.filter(event_type='extract_message', state__in=['pending', 'enqueued', 'processing']).iterator():
        raw_id = event.payload.get('raw_id') if isinstance(event.payload, dict) else None
        if type(raw_id) is not int:
            continue
        raw = Raw.objects.filter(pk=raw_id).only('timestamp', 'received_at', 'sent_at_known').first()
        if raw is None:
            continue
        at = raw.timestamp if raw.sent_at_known else raw.received_at
        delta = at.astimezone(timezone.utc) - epoch
        event.analysis_position = max(0, delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds)
        batch.append(event)
        if len(batch) == 500:
            Event.objects.bulk_update(batch, ['analysis_position']); batch = []
    if batch:
        Event.objects.bulk_update(batch, ['analysis_position'])


class Migration(migrations.Migration):
    dependencies = [('api', '0041_history_analysis_packets')]
    operations = [migrations.RunPython(source_order, migrations.RunPython.noop)]
