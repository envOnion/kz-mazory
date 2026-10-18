from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0061_schedule_analytics_conversation_cleanup")]

    operations = [
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["target_type", "target_id", "-id"], name="audit_target_order_idx"),
        )
    ]
