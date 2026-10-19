"""One-time import of legacy deployment parameters; runtime reads only the DB."""
from decimal import Decimal
import json

from django.conf import settings
from django.db import migrations


def import_connection(apps, schema_editor):
    from cryptography.fernet import Fernet
    AISettings = apps.get_model('api', 'AISettings')
    url = getattr(settings, 'LAYA_API_URL', '')
    key = getattr(settings, 'LAYA_API_KEY', '')
    model = getattr(settings, 'LAYA_MODEL', 'multilingual')
    revision = getattr(settings, 'LAYA_MODEL_REVISION', '')
    calibration = getattr(settings, 'LAYA_CALIBRATION_REVISION', '')
    encrypted = ''
    if key:
        envelope = json.dumps({'api_format': 'systemone', 'purpose': 'decision', 'value': key}, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
        encrypted = 'fernet:v1:' + Fernet(settings.AI_CREDENTIAL_ENCRYPTION_KEY.encode('ascii')).encrypt(envelope).decode('ascii')
    for cfg in AISettings.objects.using(schema_editor.connection.alias).all().iterator():
        cfg.decision_enabled = cfg.thread_routing_mode != 'llm'
        cfg.decision_provider_url = url
        cfg.decision_model_name = model
        cfg.decision_model_revision = revision
        cfg.decision_calibration_revision = calibration
        cfg.decision_api_key_encrypted = encrypted
        cfg.decision_quality_confirmed = bool(revision and calibration and
            getattr(settings, 'LAYA_ROUTING_VALIDATED_POLICY', '') == cfg.routing_policy_version and
            getattr(settings, 'LAYA_VALIDATED_MODEL', '') == model and
            getattr(settings, 'LAYA_VALIDATED_REVISION', '') == revision and
            getattr(settings, 'LAYA_VALIDATED_CALIBRATION', '') == calibration and
            cfg.laya_match_threshold >= Decimal(getattr(settings, 'LAYA_VALIDATED_THRESHOLD', '0.90')) and
            cfg.laya_match_margin >= Decimal(getattr(settings, 'LAYA_VALIDATED_MARGIN', '0.15')))
        cfg.save(update_fields=['decision_enabled', 'decision_provider_url', 'decision_model_name', 'decision_model_revision', 'decision_calibration_revision', 'decision_api_key_encrypted', 'decision_quality_confirmed'])


class Migration(migrations.Migration):
    dependencies = [('api', '0063_independent_model_connections')]
    operations = [migrations.RunPython(import_connection, migrations.RunPython.noop)]
