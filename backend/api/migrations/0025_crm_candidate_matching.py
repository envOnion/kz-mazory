import re
import unicodedata

from django.db import migrations, models
import django.db.models.deletion


def normalize_project_name(value):
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    text = re.sub(r"[«»\"'()\[\]{}]", " ", text)
    for prefix in (
        r"\bжк\b",
        r"\bбц\b",
        r"\bтоо\b",
        r"\btoo\b",
        r"\bао\b",
        r"\bмжд\b",
        r"\bобъект\b",
        r"\bмкр\b",
        r"\bмикрорайон\b",
    ):
        text = re.sub(prefix, " ", text)
    text = re.sub(r"[^\w\-]", " ", text, flags=re.UNICODE).replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()[:255]


def renormalize_project_names(apps, schema_editor):
    Project = apps.get_model("api", "Project")
    alias = schema_editor.connection.alias
    projects = list(
        Project.objects.using(alias)
        .order_by("id")
        .values("id", "team_id", "archived", "name", "normalized_name")
    )
    groups = {}
    affected = []
    for project in projects:
        normalized = normalize_project_name(project["name"])
        if normalized != project["normalized_name"]:
            affected.append((project["id"], normalized))
        if project["team_id"] is not None and not project["archived"] and normalized:
            groups.setdefault((project["team_id"], normalized), []).append(
                project["id"]
            )
    conflicts = [
        (team_id, ids)
        for (team_id, _normalized), ids in groups.items()
        if len(ids) > 1
    ]
    if conflicts:
        details = "; ".join(
            f"team_id={team_id}: project_ids={ids}"
            for team_id, ids in sorted(conflicts)
        )
        raise RuntimeError(
            "Project identity collisions after Unicode normalization: " + details
        )
    affected_ids = [project_id for project_id, _normalized in affected]
    if affected_ids:
        Project.objects.using(alias).filter(pk__in=affected_ids).update(
            normalized_name=""
        )
        for project_id, normalized in affected:
            Project.objects.using(alias).filter(pk=project_id).update(
                normalized_name=normalized
            )


def require_single_bitrix_settings(apps, schema_editor):
    BitrixSettings = apps.get_model("api", "BitrixSettings")
    alias = schema_editor.connection.alias
    ids = list(
        BitrixSettings.objects.using(alias)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if len(ids) > 1:
        raise RuntimeError(
            "Multiple BitrixSettings rows must be consolidated before CRM "
            f"matching rollout. Internal IDs: {ids}"
        )


def require_unique_company_external_ids(apps, schema_editor):
    Company = apps.get_model("api", "Company")
    alias = schema_editor.connection.alias
    duplicates = (
        Company.objects.using(alias)
        .exclude(bitrix_company_id__isnull=True)
        .exclude(bitrix_company_id="")
        .values("bitrix_company_id")
        .annotate(total=models.Count("id"))
        .filter(total__gt=1)
        .order_by("bitrix_company_id")
    )
    conflicts = []
    for duplicate in duplicates:
        external_id = duplicate["bitrix_company_id"]
        ids = list(
            Company.objects.using(alias)
            .filter(bitrix_company_id=external_id)
            .order_by("id")
            .values_list("id", flat=True)
        )
        conflicts.append(f"{external_id}: {ids}")
    if conflicts:
        raise RuntimeError(
            "Duplicate Company.bitrix_company_id values must be resolved before "
            "CRM matching rollout: " + "; ".join(conflicts)
        )


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0024_aisettings_encrypted_credentials"),
    ]

    operations = [
        migrations.RunPython(
            renormalize_project_names, migrations.RunPython.noop
        ),
        migrations.RunPython(
            require_single_bitrix_settings, migrations.RunPython.noop
        ),
        migrations.AddConstraint(
            model_name="bitrixsettings",
            constraint=models.UniqueConstraint(
                models.Value(1),
                name="bitrix_settings_singleton",
                violation_error_message=(
                    "Допускается только одна конфигурация Bitrix24."
                ),
            ),
        ),
        migrations.AddField(
            model_name="bitrixsettings",
            name="crm_matching_enabled",
            field=models.BooleanField(
                default=False, verbose_name="Read-only сопоставление с CRM"
            ),
        ),
        migrations.AddField(
            model_name="bitrixsettings",
            name="deal_object_field_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Необязательный код пользовательского поля UF_CRM_* для read-only поиска объекта.",
                max_length=64,
                verbose_name="Код поля объекта сделки",
            ),
        ),
        migrations.AddField(
            model_name="factcandidate",
            name="crm_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="factcandidate",
            name="crm_match_error_code",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="factcandidate",
            name="crm_match_query",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="factcandidate",
            name="crm_match_revision",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="factcandidate",
            name="crm_match_state",
            field=models.CharField(
                choices=[
                    ("not_requested", "Не запускалось"),
                    ("queued", "В очереди"),
                    ("matched", "Сопоставлено"),
                    ("ambiguous", "Требуется выбор"),
                    ("not_found", "Совпадений нет"),
                    ("disabled", "Сопоставление отключено"),
                    ("error", "Ошибка сопоставления"),
                ],
                db_index=True,
                default="not_requested",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="CandidateCrmMatch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("crm_match_revision", models.PositiveIntegerField()),
                ("bitrix_deal_id", models.CharField(max_length=64)),
                (
                    "bitrix_company_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "deal_title",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "normalized_deal_title",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=255
                    ),
                ),
                (
                    "company_name",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "normalized_company_name",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=255
                    ),
                ),
                (
                    "object_label",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "stage_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "opportunity",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=14, null=True
                    ),
                ),
                (
                    "currency",
                    models.CharField(blank=True, default="", max_length=3),
                ),
                ("score", models.PositiveSmallIntegerField(default=0)),
                ("match_reasons", models.JSONField(blank=True, default=list)),
                (
                    "selection_state",
                    models.CharField(
                        choices=[
                            ("suggested", "Предложено"),
                            ("selected", "Выбрано"),
                            ("dismissed", "Отклонено"),
                        ],
                        db_index=True,
                        default="suggested",
                        max_length=16,
                    ),
                ),
                ("captured_at", models.DateTimeField(auto_now_add=True)),
                (
                    "candidate",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="crm_matches",
                        to="api.factcandidate",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="candidate_crm_matches",
                        to="api.project",
                    ),
                ),
            ],
            options={
                "ordering": ["-crm_match_revision", "-score", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="candidatecrmmatch",
            constraint=models.UniqueConstraint(
                fields=("candidate", "crm_match_revision", "bitrix_deal_id"),
                name="candidate_crm_match_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="candidatecrmmatch",
            constraint=models.UniqueConstraint(
                condition=models.Q(("selection_state", "selected")),
                fields=("candidate",),
                name="candidate_crm_selected_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="candidatecrmmatch",
            constraint=models.CheckConstraint(
                condition=models.Q(("score__gte", 0), ("score__lte", 100)),
                name="candidate_crm_score_range",
            ),
        ),
        migrations.RunPython(
            require_unique_company_external_ids, migrations.RunPython.noop
        ),
        migrations.AddConstraint(
            model_name="company",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("bitrix_company_id__isnull", False),
                    models.Q(("bitrix_company_id", ""), _negated=True),
                ),
                fields=("bitrix_company_id",),
                name="company_bitrix_id_unique",
            ),
        ),
    ]
