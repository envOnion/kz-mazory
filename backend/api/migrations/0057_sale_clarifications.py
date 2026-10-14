import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0056_project_sale_amount")]
    operations = [
        migrations.AddField(
            model_name="factclarification", name="kind",
            field=models.CharField(max_length=32, default="identity", choices=[("identity", "Идентичность сделки"), ("sale_amount", "Сумма продажи")]),
        ),
        migrations.AddField(
            model_name="factclarification", name="project",
            field=models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, related_name="price_clarifications", to="api.project"),
        ),
        migrations.AddField(
            model_name="factclarification", name="price_context",
            field=models.JSONField(default=dict, blank=True),
        ),
        migrations.AddField(
            model_name="clarificationreply", name="raw_message",
            field=models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, related_name="clarification_replies", to="api.rawmessage"),
        ),
    ]
