from django.db import migrations, models
from django.db.models import OuterRef, Subquery
import django.db.models.deletion


def choose_existing_primary(apps, schema_editor):
    AISettings = apps.get_model("api", "AISettings")
    alias = schema_editor.connection.alias
    first_active = (AISettings.objects.using(alias).filter(is_active=True)
                    .order_by("pk").values_list("pk", flat=True).first())
    if first_active is not None:
        AISettings.objects.using(alias).filter(pk=first_active).update(is_primary=True)


def attribute_proven_usage(apps, schema_editor):
    ProviderReservation = apps.get_model("api", "ProviderReservation")
    ProviderUsage = apps.get_model("api", "ProviderUsage")
    alias = schema_editor.connection.alias
    proven = ProviderReservation.objects.using(alias).filter(usage_id=OuterRef("pk"))
    usage_ids = ProviderReservation.objects.using(alias).filter(usage_id__isnull=False).values("usage_id")
    ProviderUsage.objects.using(alias).filter(pk__in=Subquery(usage_ids), config_id__isnull=True).update(
        config_id=Subquery(proven.values("config_id")[:1])
    )


class Migration(migrations.Migration):
    dependencies = [("api", "0064_migrate_decision_connection")]

    operations = [
        migrations.AddField(
            model_name="aisettings",
            name="is_primary",
            field=models.BooleanField(default=False, verbose_name="Основная конфигурация"),
        ),
        migrations.AddField(
            model_name="providerusage",
            name="config",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="provider_usage", to="api.aisettings"),
        ),
        migrations.RunPython(attribute_proven_usage, migrations.RunPython.noop),
        migrations.RunPython(choose_existing_primary, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="aisettings",
            constraint=models.UniqueConstraint(condition=models.Q(("is_primary", True)),
                                               fields=("is_primary",), name="one_primary_ai_settings"),
        ),
        migrations.AddConstraint(
            model_name="aisettings",
            constraint=models.CheckConstraint(condition=models.Q(("is_primary", False), ("is_active", True), _connector="OR"),
                                              name="primary_ai_settings_active"),
        ),
    ]
