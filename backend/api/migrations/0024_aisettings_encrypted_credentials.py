import json

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import migrations, models
from django.views.decorators.debug import sensitive_variables


@sensitive_variables()
def migrate_legacy_credentials(apps, schema_editor):
    AISettings = apps.get_model("api", "AISettings")
    rows = AISettings.objects.filter(
        models.Q(chat_api_key__gt="") | models.Q(embedding_api_key__gt="")
    )
    if not rows.exists():
        return

    key = getattr(settings, "AI_CREDENTIAL_ENCRYPTION_KEY", "")
    try:
        cipher = Fernet(key.encode("ascii"))
    except (AttributeError, UnicodeEncodeError, ValueError, TypeError):
        raise RuntimeError(
            "Legacy AI credentials exist, but AI_CREDENTIAL_ENCRYPTION_KEY "
            "is unavailable or invalid; migration was stopped before data changes."
        ) from None

    for config in rows.iterator():
        if config.chat_api_key and config.chat_api_key_encrypted:
            raise RuntimeError(
                "AISettings contains both legacy and encrypted Chat credentials; "
                "migration was stopped before clearing legacy data."
            )
        if config.embedding_api_key and config.embedding_api_key_encrypted:
            raise RuntimeError(
                "AISettings contains both legacy and encrypted Embeddings credentials; "
                "migration was stopped before clearing legacy data."
            )

        changed = []
        for purpose, api_format, legacy_field, encrypted_field in (
            (
                "chat",
                config.chat_api_format,
                "chat_api_key",
                "chat_api_key_encrypted",
            ),
            (
                "embedding",
                "openai_compatible",
                "embedding_api_key",
                "embedding_api_key_encrypted",
            ),
        ):
            value = getattr(config, legacy_field)
            if not value:
                continue
            payload = json.dumps(
                {"api_format": api_format, "purpose": purpose, "value": value},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            token = cipher.encrypt(payload).decode("ascii")
            setattr(config, encrypted_field, f"fernet:v1:{token}")
            setattr(config, legacy_field, "")
            changed.extend((encrypted_field, legacy_field))
        config.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [("api", "0023_aisettings_chat_api_format_providerusage_api_format")]

    operations = [
        migrations.AddField(
            model_name="aisettings",
            name="chat_api_key_encrypted",
            field=models.TextField(
                blank=True,
                default="",
                editable=False,
                verbose_name="Зашифрованный Chat API Key",
            ),
        ),
        migrations.AddField(
            model_name="aisettings",
            name="embedding_api_key_encrypted",
            field=models.TextField(
                blank=True,
                default="",
                editable=False,
                verbose_name="Зашифрованный Embeddings API Key",
            ),
        ),
        # Deliberately irreversible: restoring provider credentials to plaintext
        # would violate the post-migration storage contract.  Omitting a reverse
        # callable makes Django stop before dropping the encrypted columns.
        migrations.RunPython(migrate_legacy_credentials),
        migrations.AlterField(
            model_name="aisettings",
            name="chat_api_key",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                max_length=255,
                verbose_name="Chat API Key",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="chat_api_format",
            field=models.CharField(
                choices=[
                    ("openai_compatible", "OpenAI-compatible"),
                    ("anthropic_messages", "Anthropic Messages"),
                ],
                default="openai_compatible",
                help_text=(
                    "Определяет протокол запросов и проверку разрешённых хостов "
                    "для редактируемого Chat Base URL."
                ),
                max_length=32,
                verbose_name="Формат Chat API",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="embedding_api_key",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                max_length=255,
                verbose_name="Embeddings API Key",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="tokenizer_id",
            field=models.CharField(
                default="nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16",
                help_text=(
                    "Словарь для предварительного подсчёта контекста "
                    "OpenAI-compatible. Для Anthropic Messages не используется."
                ),
                max_length=255,
                verbose_name="Токенизатор",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="tokenizer_revision",
            field=models.CharField(
                default="77df655d5e9f8362164ed14dd8b48f8bce657498",
                help_text=(
                    "Зафиксированная версия токенизатора OpenAI-compatible. "
                    "Для Anthropic Messages не используется."
                ),
                max_length=64,
                verbose_name="Версия токенизатора",
            ),
        ),
    ]
