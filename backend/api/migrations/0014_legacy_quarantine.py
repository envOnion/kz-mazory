from django.db import migrations


def preserve_legacy(apps, schema_editor):
    Project = apps.get_model("api", "Project")
    Revision = apps.get_model("api", "ProjectRevision")
    Audit = apps.get_model("api", "AuditEvent")
    for project in Project.objects.filter(version=0).iterator():
        state = {
            key: str(getattr(project, key))
            for key in (
                "name",
                "contract_amount",
                "cost_amount",
                "paid_amount",
                "due_amount",
                "status",
                "is_verified",
            )
        }
        Revision.objects.get_or_create(
            project=project,
            version=0,
            defaults={"snapshot": state, "source": "legacy_unreviewed"},
        )
        Audit.objects.create(
            target_type="Project",
            target_id=project.id,
            action="legacy_quarantine",
            before_after={
                "snapshot": state,
                "reason": "Legacy approvals lack provenance and team mapping; excluded until review.",
            },
        )
        Project.objects.filter(pk=project.pk).update(is_verified=False)
    for model_name in ("FinancialRecord", "Commitment"):
        Model = apps.get_model("api", model_name)
        for record in Model.objects.filter(is_verified=True).iterator():
            Audit.objects.create(
                target_type=model_name,
                target_id=record.id,
                action="legacy_quarantine",
                before_after={
                    "previous_is_verified": True,
                    "reason": "Approval provenance must be reviewed.",
                },
            )
        Model.objects.filter(is_verified=True).update(is_verified=False)
    # Keep history intact, but do not infer payment dates, costs, roles or team mappings.
    apps.get_model("api", "WhatsAppConfig").objects.update(waha_api_key="")
    apps.get_model("api", "AISettings").objects.update(
        chat_api_key="", embedding_api_key=""
    )
    apps.get_model("api", "BitrixSettings").objects.update(
        webhook_url="", inbound_token=""
    )


class Migration(migrations.Migration):
    dependencies = [("api", "0013_platform_source_guards")]
    operations = [migrations.RunPython(preserve_legacy, migrations.RunPython.noop)]
