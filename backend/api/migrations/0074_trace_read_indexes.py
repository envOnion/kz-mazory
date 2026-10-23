from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("api", "0073_objectidentity_version")]

    operations = [
        AddIndexConcurrently(
            model_name="messageprocessingtrace",
            index=models.Index(
                fields=["raw_message"],
                condition=models.Q(status="error") | models.Q(context_metadata__request_state="needs_review"),
                name="trace_attention_source_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="messageprocessingtrace",
            index=models.Index(fields=["raw_message", "-id"], name="trace_source_latest_idx"),
        ),
    ]
