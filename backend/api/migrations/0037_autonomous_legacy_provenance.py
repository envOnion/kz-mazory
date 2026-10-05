from django.db import migrations
import re
import unicodedata


def normalized_label(value):
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"\b(?:жк|бц|тоо|too|ао|мжд|объект|мкр|микрорайон)\b", " ", text)
    text = re.sub(r"[^\w\-]", " ", text).replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()[:255]


def backfill(apps, schema_editor):
    Project = apps.get_model("api", "Project")
    Candidate = apps.get_model("api", "FactCandidate")
    Decision = apps.get_model("api", "FactDecision")
    Event = apps.get_model("api", "FactEvent")
    Alias = apps.get_model("api", "ProjectAlias")

    for candidate in Candidate.objects.filter(status__in=("approved", "rejected")).select_related("trace__raw_message").iterator(chunk_size=200):
        source = candidate.trace.raw_message if candidate.trace_id else None
        whatsapp = bool(source and source.source in ("waha", "whatsapp", "whatsapp_export", "chat"))
        outcome = "accepted" if candidate.status == "approved" else "rejected"
        decision, _ = Decision.objects.get_or_create(candidate_id=candidate.pk, policy_version="legacy-human-review", input_fingerprint=f"legacy:{candidate.pk}", defaults={"outcome": outcome, "actor_kind": "human" if candidate.reviewed_by_id else "legacy", "reason_code": "legacy_review", "explanation": candidate.review_reason or "Сохранено существующее решение до автономного режима.", "validation": {"reviewed_by_id": candidate.reviewed_by_id}})
        if outcome == "accepted":
            Event.objects.get_or_create(event_key=f"legacy-candidate:{candidate.pk}", defaults={"team_id": candidate.team_id, "project_id": candidate.project_id, "decision_id": decision.pk, "event_type": candidate.fact_type, "payload": candidate.proposed_changes})
            if whatsapp and candidate.project_id and candidate.reviewed_by_id and candidate.proposed_changes.get("object_name"):
                label = candidate.proposed_changes["object_name"]
                for name in [label, *label.split("/")]:
                    normalized = normalized_label(name)
                    if normalized:
                        Alias.objects.get_or_create(project_id=candidate.project_id, normalized_name=normalized, defaults={"decision_id": decision.pk})
            if whatsapp and candidate.fact_type == "project" and candidate.project_id:
                project = Project.objects.get(pk=candidate.project_id)
                data = candidate.proposed_changes
                fields = set(project.whatsapp_fields) | {key for key in ("contract_amount", "cost_amount", "stage", "current_action", "next_action") if key in data and data[key] != ""}
                project.whatsapp_fields = sorted(fields)
                if "contract_amount" in data:
                    project.contract_known = True
                project.save(update_fields=["whatsapp_fields", "contract_known"])


class Migration(migrations.Migration):
    dependencies = [("api", "0036_aisettings_autonomous_daily_token_limit_and_more")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
