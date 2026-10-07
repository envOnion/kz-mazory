from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.contrib.auth.models import User
from django.views.decorators.debug import sensitive_variables
from .deduplication import normalize_deal_name


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.PROTECT, related_name="profile")
    full_name = models.CharField(max_length=255, default="")
    role = models.CharField(max_length=255, default="")
    department = models.CharField(max_length=255, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    avatar_url = models.URLField(blank=True, default="")

    timezone = models.CharField(max_length=64, default="Asia/Almaty")
    notification_preferences = models.JSONField(default=dict, blank=True)

    # Legacy display fields; all new calculations use the approved ledger.
    # Personal Sales KPI
    bitrix_user_id = models.CharField(
        "Bitrix24 User ID", max_length=64, blank=True, null=True, db_index=True
    )
    monthly_target = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    current_sales = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    deals_count = models.IntegerField(default=0)
    rank_in_team = models.IntegerField(default=0)
    conversion_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # WhatsApp Notifications via WAHA
    whatsapp_daily_digest = models.BooleanField(default=True)
    whatsapp_stalled_deals = models.BooleanField(default=True)
    whatsapp_critical_kpi = models.BooleanField(default=True)

    # AI Mazory Settings
    AI_MODE_CHOICES = [
        ("detailed", "Подробный с аналитикой и инсайтами"),
        ("concise", "Лаконичный (Bullet points)"),
        ("finance", "Финансовый (Только цифры и таблицы)"),
    ]
    ai_response_mode = models.CharField(
        max_length=32, choices=AI_MODE_CHOICES, default="detailed"
    )
    ai_auto_suggest_next_actions = models.BooleanField(default=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Профиль пользователя"
        verbose_name_plural = "Профили пользователей"

    def __str__(self):
        return f"{self.full_name} ({self.user.username})"

    @property
    def kpi_percent(self):
        if self.monthly_target and self.monthly_target > 0:
            return round(
                (float(self.current_sales) / float(self.monthly_target)) * 100, 1
            )
        return 0.0


class Company(models.Model):
    """
    Контрагенты: застройщики, девелоперы, генподрядчики, проектные институты.
    """

    CLIENT_TYPE_CHOICES = [
        ("private", "Частный девелопер"),
        ("state", "Государственный заказчик"),
        ("quasi_state", "Квазигосударственный сектор"),
        ("contractor", "Генподрядчик / Монтажники"),
        ("designer", "Проектная организация"),
        ("other", "Прочее"),
    ]
    name = models.CharField(max_length=255)
    team = models.ForeignKey("Team", null=True, blank=True, on_delete=models.PROTECT)
    bitrix_company_id = models.CharField(
        max_length=64, blank=True, null=True, db_index=True
    )
    client_type = models.CharField(
        max_length=32, choices=CLIENT_TYPE_CHOICES, default="private"
    )
    contact_person = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=64, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Компания"
        verbose_name_plural = "Компании"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["bitrix_company_id"],
                condition=models.Q(bitrix_company_id__isnull=False)
                & ~models.Q(bitrix_company_id=""),
                name="company_bitrix_id_unique",
            )
        ]

    def __str__(self):
        return self.name


class Project(models.Model):
    """
    Объекты / сделки компании Aqua Kip (соответствуют crm_deal).
    """

    STATUS_CHOICES = [
        ("lead", "Лид / Первичный контакт"),
        ("qualification", "Квалификация / Сбор ТЗ"),
        ("design", "Проектирование / Экспертиза"),
        ("proposal_sent", "КП отправлено"),
        ("contract_signing", "Согласование / Договор"),
        ("in_execution", "В исполнении / Производство / Монтаж"),
        ("completed", "Завершен / Сдан"),
        ("stalled", "Завис / Требует внимания"),
        ("lost", "Проигран / Архив"),
    ]
    PROJECT_TYPE_CHOICES = [
        ("private", "Частный"),
        ("state", "Государственный"),
        ("quasi_state", "Квазигосударственный"),
    ]
    PRIORITY_CHOICES = [
        ("A+++", "A+++ Стратегический"),
        ("A++", "A++ Высокий"),
        ("A+", "A+ Приоритетный"),
        ("standard", "Стандартный"),
    ]

    SOURCE_CHOICES = [
        ("chat", "WhatsApp Чат"),
        ("bitrix_crm", "Bitrix24 CRM"),
        ("manual", "Ручной ввод"),
    ]

    name = models.CharField("Название объекта", max_length=255, db_index=True)
    normalized_name = models.CharField(
        "Каноническое название для дедупликации",
        max_length=255,
        db_index=True,
        blank=True,
        default="",
    )
    source = models.CharField(
        "Источник сделки", max_length=32, choices=SOURCE_CHOICES, default="chat"
    )
    identity_confirmed = models.BooleanField(default=False, db_index=True)
    bitrix_id = models.CharField(
        "Bitrix24 ID сделки", max_length=64, blank=True, null=True, unique=True
    )
    contract_number = models.CharField(
        "Номер договора", max_length=255, blank=True, default=""
    )
    deal_period = models.CharField(
        "Период сделки", max_length=128, blank=True, default=""
    )

    company = models.ForeignKey(
        Company,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="projects",
        verbose_name="Компания",
    )
    manager = models.ForeignKey(
        UserProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="projects",
        verbose_name="Менеджер",
    )
    project_type = models.CharField(
        "Тип заказчика", max_length=32, choices=PROJECT_TYPE_CHOICES, default="private"
    )
    status = models.CharField(
        "Статус сделки", max_length=64, choices=STATUS_CHOICES, default="qualification"
    )

    team = models.ForeignKey("Team", on_delete=models.PROTECT, null=True, blank=True)
    version = models.PositiveIntegerField(default=0)
    contract_known = models.BooleanField(default=False)
    whatsapp_fields = models.JSONField(default=list, blank=True)
    cost_confirmed = models.BooleanField(default=False)
    currency = models.CharField(max_length=3, default="KZT")
    archived = models.BooleanField(default=False)

    # Синхронизация с Bitrix24
    needs_bitrix_sync = models.BooleanField(
        "Требует синхронизации с Bitrix24", default=False, db_index=True
    )
    last_chat_activity_at = models.DateTimeField(
        "Время последней активности в чате", null=True, blank=True
    )
    last_bitrix_synced_at = models.DateTimeField(
        "Время последней синхронизации с Bitrix24", null=True, blank=True
    )
    is_verified = models.BooleanField(
        "Проверено",
        default=False,
        db_index=True,
        help_text="Подтверждена ли достоверность данных сделки. Непроверенные сделки исключаются из аналитики и синхронизации с CRM.",
    )

    # Финансовые показатели (тенге ₸)
    contract_amount = models.DecimalField(
        "Сумма Договора ₸", max_digits=14, decimal_places=2, default=0.00
    )
    cost_amount = models.DecimalField(
        "Себестоимость ₸", max_digits=14, decimal_places=2, default=0.00
    )
    target_margin_percent = models.DecimalField(
        "Плановая маржа %", max_digits=10, decimal_places=2, default=0
    )
    actual_margin_percent = models.DecimalField(
        "Фактическая маржа %", max_digits=20, decimal_places=2, default=0.00
    )
    paid_amount = models.DecimalField(
        "Оплачено ₸", max_digits=14, decimal_places=2, default=0.00
    )
    due_amount = models.DecimalField(
        "Остаток / Дебиторка ₸", max_digits=14, decimal_places=2, default=0.00
    )
    guarantee_amount = models.DecimalField(
        "Гарантийные оплаты ₸", max_digits=14, decimal_places=2, default=0.00
    )
    barter_amount = models.DecimalField(
        "Сумма бартера ₸", max_digits=14, decimal_places=2, default=0.00
    )
    avr_status = models.CharField("Накладные / АВР", max_length=64, default="Не закрыт")

    equipment_type = models.CharField(
        "Оборудование", max_length=255, blank=True, default="БТП"
    )
    priority = models.CharField(
        "Приоритет", max_length=16, choices=PRIORITY_CHOICES, default="standard"
    )

    # Процессные атрибуты
    current_action = models.TextField("Последнее действие", blank=True, default="")
    next_action = models.TextField("Следующий шаг", blank=True, default="")
    next_action_at = models.DateTimeField(
        "Срок следующего действия", null=True, blank=True
    )
    decision_maker = models.CharField("ЛПР", max_length=255, blank=True, default="")
    blocker = models.TextField("Блокер / Проблема", blank=True, default="")
    notes = models.TextField("Заметки / История", blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Проект / Объект"
        verbose_name_plural = "Проекты / Объекты"
        ordering = ["-contract_amount"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "normalized_name"],
                condition=models.Q(team__isnull=False, archived=False)
                & ~models.Q(normalized_name=""),
                name="project_team_identity_unique",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def profit_amount(self):
        return (self.contract_amount or 0) - (self.cost_amount or 0)

    def save(self, *args, **kwargs):
        if self.name and not self.normalized_name:
            self.normalized_name = normalize_deal_name(self.name)
        contract = Decimal(str(self.contract_amount or 0))
        cost = Decimal(str(self.cost_amount or 0))
        self.actual_margin_percent = (
            round((contract - cost) / contract * 100, 2)
            if self.cost_confirmed and contract
            else Decimal("0.00")
        )
        self.due_amount = contract - Decimal(str(self.paid_amount or 0))
        super().save(*args, **kwargs)


class CrmProjectSnapshot(models.Model):
    """Current CRM metadata, separate from accepted business facts."""

    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="crm_snapshot")
    external_stage_id = models.CharField(max_length=128, blank=True, default="")
    external_stage_name = models.CharField(max_length=255, blank=True, default="")
    external_manager_id = models.CharField(max_length=64, blank=True, default="")
    external_manager_name = models.CharField(max_length=255, blank=True, default="")
    opportunity = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="KZT")
    synced_at = models.DateTimeField(default=timezone.now)


class RawMessage(models.Model):
    project = models.ForeignKey(
        "Project", null=True, blank=True, on_delete=models.PROTECT
    )
    """
    Сырые сообщения из WhatsApp-чатов для аудита, векторизации в Qdrant и извлечения сущностей.
    """
    team = models.ForeignKey("Team", null=True, blank=True, on_delete=models.PROTECT)
    message_id = models.CharField(max_length=128, db_index=True)
    source = models.CharField(max_length=32, default="waha")
    session_name = models.CharField(max_length=64, default="default")
    source_revision = models.CharField(max_length=64, default="0")
    received_at = models.DateTimeField(default=timezone.now)
    processing_state = models.CharField(
        max_length=32, default="received", db_index=True
    )
    sent_at_known = models.BooleanField(default=True)
    config = models.ForeignKey(
        "WhatsAppConfig", null=True, blank=True, on_delete=models.PROTECT
    )
    chat_id = models.CharField(max_length=128, blank=True, default="")
    sender_phone = models.CharField(max_length=64, blank=True, default="")
    sender_name = models.CharField(max_length=255, blank=True, default="")
    timestamp = models.DateTimeField()
    content = models.TextField()
    raw_payload = models.JSONField(default=dict, blank=True)
    qdrant_point_id = models.CharField(max_length=64, blank=True, default="")
    processed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "session_name", "message_id", "source_revision"],
                name="message_source_revision_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["config", "timestamp", "id"], name="raw_chat_time_idx"
            ),
            models.Index(
                fields=["team", "source", "project", "timestamp", "id"],
                name="raw_project_time_idx",
            ),
        ]
        verbose_name = "Сырое сообщение WhatsApp"
        verbose_name_plural = "Сырые сообщения WhatsApp"
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.sender_name} [{self.timestamp}]: {self.content[:40]}..."


class Commitment(models.Model):
    """
    Обещания, дедлайны и поручения, зафиксированные в коммуникациях.
    """

    STATUS_CHOICES = [
        ("pending", "В работе"),
        ("fulfilled", "Выполнено"),
        ("overdue", "Просрочено"),
        ("cancelled", "Отменено"),
    ]
    SEVERITY_CHOICES = [
        ("critical", "Критический"),
        ("medium", "Средний"),
        ("low", "Низкий"),
    ]

    team = models.ForeignKey("Team", on_delete=models.PROTECT, null=True, blank=True, related_name="commitments")
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="commitments",
        verbose_name="Объект",
    )
    manager = models.ForeignKey(
        UserProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="commitments",
        verbose_name="Менеджер",
    )
    source_message = models.ForeignKey(
        RawMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="commitments",
    )

    candidate = models.OneToOneField(
        "FactCandidate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="accepted_commitment",
    )
    participant = models.ForeignKey("Participant", null=True, blank=True, on_delete=models.PROTECT)
    version = models.PositiveIntegerField(default=1)
    deadline_at = models.DateTimeField(null=True, blank=True)
    deadline_precision = models.CharField(max_length=16, default="unknown")
    original_deadline_at = models.DateTimeField(null=True, blank=True)
    postponed_reason = models.TextField(blank=True)

    counterparty_person = models.CharField(
        "Кому обещано / ЛПР", max_length=255, blank=True, default=""
    )
    responsible_name = models.CharField("Исполнитель по переписке", max_length=255, blank=True, default="")
    commitment_text = models.TextField("Суть обещания")
    promised_at = models.DateTimeField(auto_now_add=True)
    deadline = models.DateField("Дедлайн", null=True, blank=True)
    status = models.CharField(
        "Статус", max_length=32, choices=STATUS_CHOICES, default="pending"
    )
    severity = models.CharField(
        "Срочность", max_length=16, choices=SEVERITY_CHOICES, default="medium"
    )
    bitrix_task_id = models.CharField(
        "ID задачи в Bitrix24", max_length=64, blank=True, null=True, db_index=True
    )
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(
        "Проверено",
        default=False,
        db_index=True,
        help_text="Подтверждено ли обязательство. Непроверенные задачи не учитываются в SLA и просрочках.",
    )

    class Meta:
        verbose_name = "Обязательство / Обещание"
        verbose_name_plural = "Обязательства / Обещания"
        ordering = ["deadline", "id"]

    def __str__(self):
        return f"[{self.status}] {self.commitment_text[:50]} (до {self.deadline})"


class FinancialRecord(models.Model):
    """
    Факты оплат и финансовые движения по объектам.
    """

    PAYMENT_TYPE_CHOICES = [
        ("advance", "Аванс"),
        ("milestone", "Промежуточный платёж"),
        ("final", "Окончательный расчет"),
        ("barter", "Взаимозачёт / Бартер"),
        ("debt_collection", "Взыскание задолженности"),
    ]
    STATUS_CHOICES = [
        ("expected", "Ожидается к сбору"),
        ("received", "Получено на расчетный счет"),
        ("delayed", "Задержка платежа"),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        related_name="financial_records",
        verbose_name="Объект",
    )
    candidate = models.OneToOneField(
        "FactCandidate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="accepted_payment",
    )
    source_key = models.CharField(max_length=255, null=True, blank=True, unique=True)
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT
    )
    direction = models.CharField(max_length=16, default="income")
    amount_precision = models.CharField(max_length=16, default="exact")
    event_kind = models.CharField(max_length=32, default="payment")
    credited_profile = models.ForeignKey(
        UserProfile,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credited_payments",
    )
    currency = models.CharField(max_length=3, default="KZT")
    amount = models.DecimalField("Сумма ₸", max_digits=14, decimal_places=2)
    payment_date = models.DateField("Дата платежа")
    payment_type = models.CharField(
        "Тип платежа", max_length=32, choices=PAYMENT_TYPE_CHOICES, default="milestone"
    )
    status = models.CharField(
        "Статус", max_length=32, choices=STATUS_CHOICES, default="received"
    )
    notes = models.TextField("Комментарий / Основание", blank=True, default="")
    is_verified = models.BooleanField(
        "Проверено",
        default=False,
        db_index=True,
        help_text="Подтвержден ли факт оплаты. Непроверенные оплаты не учитываются в сборе денег и выполнении планов.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Финансовая запись"
        verbose_name_plural = "Финансовые записи"
        ordering = ["-payment_date"]

    def __str__(self):
        return f"{self.project.name}: {self.amount:,.2f} ₸ ({self.status})"


class BusinessEvent(models.Model):
    """
    Бизнес-события: изменение цены, срыв сроков, критические инциденты.
    """

    SEVERITY_CHOICES = [
        ("info", "Инфо"),
        ("warning", "Внимание"),
        ("critical", "Критично"),
    ]

    deduplication_key = models.CharField(
        max_length=255, null=True, blank=True, unique=True
    )
    event_type = models.CharField(max_length=64)
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
        verbose_name="Объект",
    )
    manager = models.ForeignKey(
        UserProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
        verbose_name="Менеджер",
    )
    title = models.CharField("Заголовок", max_length=255)
    description = models.TextField("Описание", blank=True, default="")
    timestamp = models.DateTimeField(auto_now_add=True)
    severity = models.CharField(
        "Важность", max_length=16, choices=SEVERITY_CHOICES, default="info"
    )

    class Meta:
        verbose_name = "Бизнес-событие"
        verbose_name_plural = "Бизнес-события"
        ordering = ["-timestamp"]

    def __str__(self):
        return f"[{self.severity}] {self.title}"


# ============================================================================
# Dynamic Configurations Managed via Django Admin
# ============================================================================


class WhatsAppConfig(models.Model):
    """
    Настройки интеграции с WAHA и отслеживаемой группы WhatsApp.
    """

    team = models.ForeignKey("Team", on_delete=models.PROTECT, null=True, blank=True)
    snapshot = models.JSONField(default=dict, blank=True)
    name = models.CharField(
        "Название чата / группы", max_length=255, default="КОМАНДА ПОДДЕРЖКИ ПРОДАЖ"
    )
    group_jid = models.CharField(
        "WhatsApp Group JID",
        max_length=128,
        default="",
        help_text="Например: 120363024823904923@g.us",
    )
    session_name = models.CharField(
        "Имя сессии в WAHA", max_length=64, default="default"
    )
    waha_api_url = models.CharField(
        "URL WAHA API", max_length=255, default="http://waha:3000"
    )
    waha_api_key = models.CharField(
        "WAHA API Key", max_length=128, default="", blank=True
    )
    is_active = models.BooleanField("Мониторинг активен", default=True)
    status = models.CharField("Статус подключения", max_length=32, default="WORKING")
    last_qr_code = models.TextField("QR-код (base64)", blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройка WhatsApp (WAHA)"
        verbose_name_plural = "Настройки WhatsApp (WAHA)"

    def __str__(self):
        return f"{self.name} ({'Активен' if self.is_active else 'Выключен'})"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and any(
                getattr(previous, key) != getattr(self, key)
                for key in ("team_id", "group_jid", "session_name")
            ):
                from django.db import connection

                if connection.in_atomic_block:
                    list(
                        WhatsAppHistoryJob.objects.select_for_update().filter(
                            config_id=self.pk
                        )
                    )
                if WhatsAppHistoryRun.objects.filter(
                    job__config_id=self.pk,
                    job__only_new=True,
                    state__in=HISTORY_ACTIVE_STATES,
                ).exists():
                    raise ValidationError(
                        "Сначала отмените мониторинг новых сообщений, затем измените источник."
                    )

    @classmethod
    def get_active(cls):
        return cls.objects.filter(is_active=True).first() or cls(is_active=False)


class AISettings(models.Model):
    analysis_input_token_limit = models.PositiveIntegerField("Вход анализа, токены", default=16384)
    analysis_target_message_limit = models.PositiveIntegerField("Сообщений в пакете анализа", default=8)
    analysis_output_token_limit = models.PositiveIntegerField("Ответ анализа, токены", default=4096)
    autonomous_daily_token_limit = models.PositiveBigIntegerField(default=1000000)
    autonomous_max_in_flight = models.PositiveIntegerField(default=2)
    autonomous_reconcile_cursor = models.PositiveBigIntegerField(default=0)
    autonomous_enabled = models.BooleanField(default=False)
    autonomous_crm_enabled = models.BooleanField(default=False)
    autonomous_policy_version = models.CharField(max_length=64, default="whatsapp-autonomous-v1")
    autonomous_context_messages = models.PositiveIntegerField(default=30)
    autonomous_input_tokens = models.PositiveIntegerField(default=12000)

    """
    Конфигурация нейросетевых моделей для embeddings и Chat/Reasoning.
    """

    class ChatApiFormat(models.TextChoices):
        OPENAI_COMPATIBLE = "openai_compatible", "OpenAI-compatible"
        ANTHROPIC_MESSAGES = "anthropic_messages", "Anthropic Messages"

    name = models.CharField(
        "Конфигурация", max_length=128, default="Основная конфигурация OpenRouter"
    )

    # Embeddings
    embedding_provider_url = models.CharField(
        "Embeddings Base URL", max_length=255, default="https://openrouter.ai/api/v1"
    )
    embedding_model_name = models.CharField(
        "Embeddings Model", max_length=128, default="liquid/lfm-2.5-embedding-350m:free"
    )
    embedding_api_key = models.CharField(
        "Embeddings API Key",
        max_length=255,
        blank=True,
        default="",
        editable=False,
    )
    embedding_api_key_encrypted = models.TextField(
        "Зашифрованный Embeddings API Key",
        blank=True,
        default="",
        editable=False,
    )
    embedding_dimension = models.IntegerField("Размерность вектора", default=1024)

    # Chat & Reasoning
    chat_api_format = models.CharField(
        "Формат Chat API",
        max_length=32,
        choices=ChatApiFormat.choices,
        default=ChatApiFormat.OPENAI_COMPATIBLE,
        help_text=(
            "Определяет протокол запросов к редактируемому Chat Base URL."
        ),
    )
    chat_provider_url = models.CharField(
        "Chat Base URL", max_length=255, default="https://openrouter.ai/api/v1"
    )
    chat_model_name = models.CharField(
        "Chat LLM Model",
        max_length=128,
        default="nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    chat_api_key = models.CharField(
        "Chat API Key",
        max_length=255,
        blank=True,
        default="",
        editable=False,
    )
    chat_api_key_encrypted = models.TextField(
        "Зашифрованный Chat API Key",
        blank=True,
        default="",
        editable=False,
    )
    chat_temperature = models.FloatField("Temperature", default=0.2)
    context_window_tokens = models.PositiveIntegerField(
        "Окно контекста, токены", default=256000
    )
    max_completion_tokens = models.PositiveIntegerField(
        "Резерв ответа, токены", default=8192
    )
    context_safety_tokens = models.PositiveIntegerField(
        "Технический запас, токены", default=2048
    )
    daily_request_limit = models.PositiveIntegerField(
        "Лимит запросов AI в сутки",
        default=0,
        help_text=(
            "0 — без ограничений. Учитываются все внешние AI-запросы: "
            "embeddings, chat и подсчёт токенов."
        ),
    )
    daily_budget_usd = models.DecimalField(
        "Бюджет AI в сутки, USD",
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="0 — без ограничений.",
    )
    tokenizer_id = models.CharField(
        "Токенизатор",
        max_length=255,
        default="nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16",
        help_text=(
            "Словарь для предварительного подсчёта контекста OpenAI-compatible. "
            "Для Anthropic Messages не используется."
        ),
    )
    tokenizer_revision = models.CharField(
        "Версия токенизатора",
        max_length=64,
        default="77df655d5e9f8362164ed14dd8b48f8bce657498",
        help_text=(
            "Зафиксированная версия токенизатора OpenAI-compatible. "
            "Для Anthropic Messages не используется."
        ),
    )

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if not 1 <= self.analysis_target_message_limit <= 30:
            raise ValidationError({"analysis_target_message_limit": "Допустимо от 1 до 30 сообщений."})
        if not self.analysis_input_token_limit or not self.analysis_output_token_limit:
            raise ValidationError("Лимиты анализа должны быть положительными.")
        if self.analysis_output_token_limit > self.max_completion_tokens:
            raise ValidationError({"analysis_output_token_limit": "Ответ анализа не должен превышать резерв ответа модели."})
        if self.analysis_input_token_limit + self.analysis_output_token_limit + self.context_safety_tokens > self.context_window_tokens:
            raise ValidationError("Вход, ответ анализа и технический запас должны помещаться в окно модели.")
        if self.daily_budget_usd is not None and self.daily_budget_usd < 0:
            raise ValidationError(
                {"daily_budget_usd": "Бюджет не может быть отрицательным."}
            )
        if (
            not self.context_window_tokens
            or not self.max_completion_tokens
            or self.max_completion_tokens + self.context_safety_tokens
            >= self.context_window_tokens
        ):
            raise ValidationError(
                "Окно должно превышать сумму резерва ответа и технического запаса."
            )

    system_prompt_worker = models.TextField(
        "Промпт извлечения сделок из чата",
        default="""Ты эксперт-аналитик рабочей группы продаж AquaKip.
Твоя задача — извлечь из сообщения КОНКРЕТНЫЕ коммерческие факты по сделкам.

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
1. НИКОГДА не копируй текст сообщения целиком ни в одно поле.
2. Каждое текстовое поле — краткий факт до 120 символов максимум.
3. Не включай приветствия, обращения, подписи в значения полей.
4. Если в сообщении упоминается несколько объектов/сделок — извлеки ОДИН с наибольшей коммерческой значимостью (наибольшая сумма или наличие конкретного договора). Для остальных верни is_deal_fact: false.
5. can_create_deal: true ТОЛЬКО если одновременно присутствуют: object_name (конкретный объект) И contract_amount > 0 (числовая сумма).
6. Если нет конкретной суммы в тексте — contract_amount должен быть null, а can_create_deal: false.
7. Не выдумывай данные. Если чего-то нет в тексте — возвращай null.

Формат ответа — строго валидный JSON:
{
  "is_deal_fact": true|false,
  "confidence": 0.0-1.0,
  "object_name": "Краткое название объекта (до 120 символов) или null",
  "company_name": "Компания заказчика или null",
  "direction": "Оборудование: БМК, БТП, НС, Котлы и т.д. или null",
  "contract_number": "Номер договора или null",
  "deal_period": "Период сделки или null",
  "stage": "lead|qualification|proposal_sent|contract_signing|in_execution|completed|stalled",
  "contract_amount": число или null,
  "cost_amount": число или null,
  "paid_amount": число или null,
  "barter_amount": число или null,
  "guarantee_amount": число или null,
  "avr_status": "Закрыт|Не закрыт|null",
  "responsible_name": "Имя менеджера (только имя, без приветствий) или null",
  "current_action": "Краткое резюме конкретного выполненного действия до 120 символов. НЕ копируй сырой текст сообщения. Пример: 'Отправлено КП на тепловые пункты для ЖК Diamond' или null",
  "next_action": "Краткое конкретное обязательство на будущее до 120 символов. Пример: 'Подготовить КП до 30.09', 'Согласовать спецификацию с заказчиком'. НЕ копируй сырой текст. null если нет",
  "next_action_at": "ISO 8601 дата или null",
  "decision_maker": "ЛПР или null",
  "blocker": "Блокер или null",
  "priority": "A+++|A++|A+|standard",
  "can_create_deal": true|false
}""",
    )

    system_prompt_assistant = models.TextField(
        "Промпт чат-ассистента для пользователей",
        default="""Ты аналитический ассистент Mazory AI платформы продаж AquaKip.
Отвечай точно, структурированно, опираясь на факты из базы данных и сообщений.
Используй тенге (₸) для сумм. Если пользователь просит графики или сравнения, возвращай данные в понятной форме.""",
    )

    is_active = models.BooleanField("Активная конфигурация", default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройки моделей AI"
        verbose_name_plural = "Настройки моделей AI"

    def __str__(self):
        return f"{self.name} ({self.chat_model_name})"

    @property
    def has_chat_api_key(self):
        return bool(self.chat_api_key_encrypted)

    @property
    def has_embedding_api_key(self):
        return bool(self.embedding_api_key_encrypted)

    @sensitive_variables("value")
    def set_chat_api_key(self, value):
        from .ai_credentials import encrypt_credential

        self.chat_api_key_encrypted = encrypt_credential(
            value, purpose="chat", api_format=self.chat_api_format
        )
        self.chat_api_key = ""

    @sensitive_variables("self")
    def get_chat_api_key(self):
        from .ai_credentials import decrypt_credential

        return decrypt_credential(
            self.chat_api_key_encrypted,
            purpose="chat",
            api_format=self.chat_api_format,
        )

    def clear_chat_api_key(self):
        self.chat_api_key_encrypted = ""
        self.chat_api_key = ""

    @sensitive_variables("value")
    def set_embedding_api_key(self, value):
        from .ai_credentials import encrypt_credential

        self.embedding_api_key_encrypted = encrypt_credential(
            value,
            purpose="embedding",
            api_format=self.ChatApiFormat.OPENAI_COMPATIBLE,
        )
        self.embedding_api_key = ""

    @sensitive_variables("self")
    def get_embedding_api_key(self):
        from .ai_credentials import decrypt_credential

        return decrypt_credential(
            self.embedding_api_key_encrypted,
            purpose="embedding",
            api_format=self.ChatApiFormat.OPENAI_COMPATIBLE,
        )

    def clear_embedding_api_key(self):
        self.embedding_api_key_encrypted = ""
        self.embedding_api_key = ""

    @classmethod
    def get_active(cls):
        return cls.objects.filter(is_active=True).first() or cls(is_active=False)


class BitrixSettings(models.Model):
    """
    Настройки интеграции с Bitrix24 REST API.
    """

    name = models.CharField(
        "Название интеграции", max_length=128, default="AquaKip Bitrix24 Portal"
    )
    webhook_url = models.CharField("REST Webhook URL", max_length=255, default="")
    inbound_token = models.CharField(
        "Секретный токен входящего вебхука", max_length=128, blank=True, default=""
    )
    is_active = models.BooleanField("Синхронизация активна", default=True)
    crm_matching_enabled = models.BooleanField(
        "Read-only сопоставление с CRM", default=False
    )
    auto_create_deals = models.BooleanField(
        "Авто-создание сделок в Bitrix24", default=True
    )
    auto_import_deals = models.BooleanField(
        "Авто-импорт сделок из CRM в Mazory", default=True
    )
    hourly_sync_enabled = models.BooleanField(
        "Ежечасная фоновая синхронизация", default=True
    )
    auto_create_tasks = models.BooleanField(
        "Создавать задачи в Bitrix24 по дедлайнам", default=True
    )
    sync_timeline_comments = models.BooleanField(
        "Публиковать саммари в таймлайн сделки", default=True
    )
    deal_category_id = models.IntegerField("ID воронки сделок", default=0)
    deal_object_field_code = models.CharField(
        "Код поля объекта сделки",
        max_length=64,
        blank=True,
        default="",
        help_text="Необязательный код пользовательского поля UF_CRM_* для read-only поиска объекта.",
    )
    default_assigned_by_id = models.IntegerField(
        "ID ответственного по умолчанию", default=1
    )
    last_sync_at = models.DateTimeField(
        "Последняя синхронизация", null=True, blank=True
    )
    last_hourly_sync_at = models.DateTimeField(
        "Время последнего запуска диспетчера", null=True, blank=True
    )
    last_sync_status = models.TextField(
        "Статус последней операции", blank=True, default=""
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройки Bitrix24"
        verbose_name_plural = "Настройки Bitrix24"
        constraints = [
            models.UniqueConstraint(
                models.Value(1),
                name="bitrix_settings_singleton",
                violation_error_message=(
                    "Допускается только одна конфигурация Bitrix24."
                ),
            )
        ]

    def clean(self):
        super().clean()
        if type(self).objects.exclude(pk=self.pk).exists():
            raise ValidationError(
                "Допускается только одна конфигурация Bitrix24."
            )

    def __str__(self):
        return f"{self.name} ({'Активен' if self.is_active else 'Выключен'})"

    @classmethod
    def get_active(cls):
        return cls.objects.filter(is_active=True).first() or cls(is_active=False)


class BitrixDealChangeLog(models.Model):
    """
    Журнал аудита всех изменений по сделкам, отправленных в Bitrix24 CRM.
    """

    ACTION_CHOICES = [
        ("create", "Создание сделки (crm.deal.add)"),
        ("update", "Обновление сделки (crm.deal.update)"),
    ]
    STATUS_CHOICES = [
        ("success", "Успешно"),
        ("error", "Ошибка"),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bitrix_change_logs",
        verbose_name="Объект / Сделка",
    )
    bitrix_deal_id = models.CharField(
        "ID сделки в Bitrix24", max_length=64, db_index=True, blank=True, default=""
    )
    action = models.CharField(
        "Действие", max_length=32, choices=ACTION_CHOICES, db_index=True
    )
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=STATUS_CHOICES,
        default="success",
        db_index=True,
    )
    payload = models.JSONField(
        "Отправленные данные (Payload)", default=dict, blank=True
    )
    response_data = models.JSONField("Ответ Bitrix24", default=dict, blank=True)
    changed_fields = models.JSONField("Измененные поля", default=list, blank=True)
    error_message = models.TextField("Текст ошибки", blank=True, default="")
    duration_ms = models.IntegerField("Длительность (мс)", default=0)
    triggered_by = models.CharField(
        "Инициатор / Источник", max_length=128, default="system", blank=True
    )
    created_at = models.DateTimeField(
        "Дата и время отправки", auto_now_add=True, db_index=True
    )

    class Meta:
        verbose_name = "Лог изменения сделки Bitrix24"
        verbose_name_plural = "Логи изменений сделок Bitrix24"
        ordering = ["-created_at"]

    def __str__(self):
        created_str = (
            self.created_at.strftime("%d.%m.%Y %H:%M:%S") if self.created_at else ""
        )
        return f"[{self.get_action_display()}] Сделка #{self.bitrix_deal_id} — {self.get_status_display()} ({created_str})"


class MessageProcessingTrace(models.Model):
    """
    Сквозная трассировка пайплайна обработки сообщений:
    «Входные данные WhatsApp» -> «Зависимые данные из сообщений ранее» -> «Зависимые данные из Bitrix24» -> «Итоговая запись»
    """

    PIPELINE_ACTION_CHOICES = [
        ("created_deal", "Создана новая сделка"),
        ("updated_deal", "Обновлена существующая сделка"),
        ("matched_bitrix_imported", "Импортирована сделка из Bitrix24"),
        ("commitment_created", "Зафиксировано обязательство"),
        ("financial_record_created", "Зафиксирована оплата"),
        ("non_commercial", "Информационное / Некоммерческое сообщение"),
        ("error", "Ошибка обработки"),
    ]
    STATUS_CHOICES = [
        ("success", "Успешно"),
        ("warning", "Требует внимания"),
        ("error", "Ошибка"),
    ]

    # Связи
    raw_message = models.ForeignKey(
        RawMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="traces",
        verbose_name="Сырое сообщение WhatsApp",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_traces",
        verbose_name="Объект / Сделка",
    )
    commitment = models.ForeignKey(
        Commitment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_traces",
        verbose_name="Созданное обязательство",
    )
    financial_record = models.ForeignKey(
        FinancialRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_traces",
        verbose_name="Созданная запись оплаты",
    )

    attempt_no = models.PositiveIntegerField(default=1)
    model_version = models.CharField(max_length=128, blank=True)
    prompt_version = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=64, blank=True)

    # 1. Входные данные WhatsApp
    whatsapp_message_id = models.CharField(
        "ID сообщения WhatsApp", max_length=128, db_index=True
    )
    whatsapp_chat_id = models.CharField(
        "Чат / Группа WhatsApp", max_length=128, blank=True, default=""
    )
    whatsapp_sender_phone = models.CharField(
        "Телефон отправителя", max_length=64, blank=True, default=""
    )
    whatsapp_sender_name = models.CharField(
        "Имя отправителя", max_length=255, blank=True, default=""
    )
    whatsapp_timestamp = models.DateTimeField(
        "Время сообщения WhatsApp", null=True, blank=True
    )
    whatsapp_content = models.TextField("Текст сообщения WhatsApp")
    whatsapp_raw_payload = models.JSONField(
        "Сырой payload WAHA", default=dict, blank=True
    )

    # 2. Зависимые данные из сообщений ранее
    earlier_messages_context = models.JSONField(
        "Найденные сообщения из истории (Qdrant RAG / Чат)", default=list, blank=True
    )
    earlier_messages_count = models.IntegerField(
        "Количество зависимых сообщений", default=0
    )
    context_metadata = models.JSONField(
        "Полнота и бюджет контекста", default=dict, blank=True
    )
    operation_key = models.CharField(
        max_length=128, unique=True, null=True, blank=True, editable=False
    )

    # 3. Зависимые данные из Bitrix24
    bitrix_matched_deal_id = models.CharField(
        "ID сделки в Bitrix24", max_length=64, blank=True, default="", db_index=True
    )
    bitrix_deal_title = models.CharField(
        "Название сделки в Bitrix24", max_length=255, blank=True, default=""
    )
    bitrix_deal_stage = models.CharField(
        "Стадия в Bitrix24", max_length=64, blank=True, default=""
    )
    bitrix_deal_opportunity = models.DecimalField(
        "Сумма сделки в Bitrix24 ₸",
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
    )
    bitrix_search_query = models.CharField(
        "Поисковый запрос в Bitrix24", max_length=255, blank=True, default=""
    )
    bitrix_company_data = models.JSONField(
        "Данные компании в Bitrix24", default=dict, blank=True
    )
    bitrix_raw_deal = models.JSONField(
        "Полные данные сделки из Bitrix24", default=dict, blank=True
    )
    bitrix_known_deals_summary = models.TextField(
        "Сводка сделок компании, переданная в AI", blank=True, default=""
    )

    # 4. Итоговая запись
    ai_extracted_facts = models.JSONField(
        "Извлеченные факты AI", default=dict, blank=True
    )
    ai_confidence = models.FloatField("Уверенность AI (0.0 - 1.0)", default=0.0)
    pipeline_action = models.CharField(
        "Действие пайплайна",
        max_length=64,
        choices=PIPELINE_ACTION_CHOICES,
        default="non_commercial",
        db_index=True,
    )
    status = models.CharField(
        "Статус обработки",
        max_length=32,
        choices=STATUS_CHOICES,
        default="success",
        db_index=True,
    )
    result_summary = models.TextField("Резюме итоговой записи", blank=True, default="")
    created_at = models.DateTimeField(
        "Время создания трассировки", auto_now_add=True, db_index=True
    )

    class Meta:
        verbose_name = "Трассировка пайплайна"
        verbose_name_plural = "Трассировки пайплайна"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["raw_message", "attempt_no"],
                condition=models.Q(raw_message__isnull=False),
                name="trace_source_attempt_unique",
            )
        ]

    def __str__(self):
        created_str = (
            self.created_at.strftime("%d.%m.%Y %H:%M:%S") if self.created_at else ""
        )
        return f"[{self.get_pipeline_action_display()}] {self.whatsapp_sender_name} ({created_str})"


class Team(models.Model):
    name = models.CharField(max_length=128, unique=True)
    is_active = models.BooleanField(default=True)
    history_complete_from = models.DateField(null=True, blank=True)
    stalled_days = models.PositiveSmallIntegerField(default=3)
    low_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=15)
    rules_version = models.PositiveIntegerField(default=1)

    def __str__(self):
        return self.name


class TeamMembership(models.Model):
    ROLES = [
        (role, label)
        for role, label in (
            ("manager", "Менеджер"),
            ("team_lead", "Руководитель"),
            ("finance", "Финансист"),
        )
    ]
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="memberships")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=16, choices=ROLES)
    status = models.CharField(
        max_length=16,
        default="invited",
        choices=[(s, s) for s in ("invited", "active", "revoked")],
    )
    invited_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "team", "role"], name="membership_unique"
            )
        ]


class ClientProjectAccess(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="client_access"
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="client_access"
    )
    status = models.CharField(
        max_length=16,
        default="invited",
        choices=[(s, s) for s in ("invited", "active", "revoked")],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "project"], name="client_project_unique"
            )
        ]


class ChatAccess(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    config = models.ForeignKey(
        WhatsAppConfig, on_delete=models.PROTECT, related_name="access_grants"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "config"], name="chat_access_unique"
            )
        ]


class AuthSession(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="access_sessions"
    )
    refresh_jti_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    device = models.CharField(max_length=256, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)


class AdminMFA(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    encrypted_secret = models.TextField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_counter = models.BigIntegerField(default=-1)


class DialogueThread(models.Model):
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    config = models.ForeignKey(WhatsAppConfig, null=True, blank=True, on_delete=models.PROTECT)
    source_key = models.CharField(max_length=64, db_index=True)
    identity = models.CharField(max_length=64, unique=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT)
    topic = models.CharField(max_length=255)
    summary = models.TextField(blank=True)
    state = models.CharField(max_length=16, choices=[(s, s) for s in ("open", "ready", "unknown", "superseded")], default="open", db_index=True)
    version = models.PositiveIntegerField(default=0)
    snapshot_max_id = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class ThreadMessage(models.Model):
    thread = models.ForeignKey(DialogueThread, on_delete=models.PROTECT, related_name="message_links")
    raw_message = models.ForeignKey(RawMessage, on_delete=models.PROTECT, related_name="thread_links")
    thought_state = models.CharField(max_length=16, choices=[(s, s) for s in ("intermediate", "final", "unknown")])
    relation = models.CharField(max_length=16, choices=[(s, s) for s in ("discusses", "answers", "clarifies", "cancels", "fulfills")], default="discusses")
    rationale = models.CharField(max_length=1000, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["thread", "raw_message"], name="thread_message_unique")]


class ThreadRevision(models.Model):
    thread = models.ForeignKey(DialogueThread, on_delete=models.PROTECT, related_name="revisions")
    version = models.PositiveIntegerField()
    state = models.CharField(max_length=16)
    topic = models.CharField(max_length=255, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    message_snapshot = models.JSONField(default=list)
    extraction = models.JSONField(default=list)
    completion_reason = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["thread", "version"], name="thread_revision_unique")]


class ThreadSubscription(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="thread_subscriptions")
    thread = models.ForeignKey(DialogueThread, on_delete=models.CASCADE, related_name="subscriptions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "thread"], name="unique_user_thread_subscription")
        ]


class CrmCatalogSync(models.Model):
    team = models.OneToOneField(Team, on_delete=models.PROTECT)
    generation = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=16, default="idle")
    cursor = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    last_success_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class FactCandidate(models.Model):
    materialization_snapshot = models.JSONField(default=dict, blank=True)
    thread_revision = models.ForeignKey(ThreadRevision, null=True, blank=True, on_delete=models.PROTECT, related_name="candidates")
    CRM_MATCH_STATE_CHOICES = [
        ("not_requested", "Не запускалось"),
        ("queued", "В очереди"),
        ("matched", "Сопоставлено"),
        ("ambiguous", "Требуется выбор"),
        ("not_found", "Совпадений нет"),
        ("disabled", "Сопоставление отключено"),
        ("error", "Ошибка сопоставления"),
    ]

    trace = models.ForeignKey(
        MessageProcessingTrace, on_delete=models.PROTECT, related_name="candidates"
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="candidates",
    )
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    manager = models.ForeignKey(
        UserProfile, on_delete=models.PROTECT, null=True, blank=True
    )
    fact_type = models.CharField(
        max_length=32, choices=[(s, s) for s in ("project", "payment", "commitment")]
    )
    proposed_changes = models.JSONField(default=dict)
    base_project_version = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        default="pending",
        db_index=True,
        choices=[(s, s) for s in ("pending", "approved", "rejected", "superseded")],
    )
    source_key = models.CharField(max_length=255, unique=True)
    confidence = models.FloatField(default=0)
    uncertainties = models.JSONField(default=list, blank=True)
    crm_match_state = models.CharField(
        max_length=16,
        choices=CRM_MATCH_STATE_CHOICES,
        default="not_requested",
        db_index=True,
    )
    crm_match_revision = models.PositiveIntegerField(default=0)
    crm_match_query = models.JSONField(default=dict, blank=True)
    crm_match_error_code = models.CharField(max_length=64, blank=True, default="")
    crm_checked_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CandidateCrmMatch(models.Model):
    SELECTION_STATE_CHOICES = [
        ("suggested", "Предложено"),
        ("selected", "Выбрано"),
        ("dismissed", "Отклонено"),
    ]

    candidate = models.ForeignKey(
        FactCandidate, on_delete=models.PROTECT, related_name="crm_matches"
    )
    crm_match_revision = models.PositiveIntegerField()
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="candidate_crm_matches",
    )
    bitrix_deal_id = models.CharField(max_length=64)
    bitrix_company_id = models.CharField(max_length=64, blank=True, default="")
    deal_title = models.CharField(max_length=255, blank=True, default="")
    normalized_deal_title = models.CharField(
        max_length=255, blank=True, default="", db_index=True
    )
    company_name = models.CharField(max_length=255, blank=True, default="")
    normalized_company_name = models.CharField(
        max_length=255, blank=True, default="", db_index=True
    )
    object_label = models.CharField(max_length=255, blank=True, default="")
    stage_id = models.CharField(max_length=64, blank=True, default="")
    opportunity = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    currency = models.CharField(max_length=3, blank=True, default="")
    score = models.PositiveSmallIntegerField(default=0)
    match_reasons = models.JSONField(default=list, blank=True)
    selection_state = models.CharField(
        max_length=16,
        choices=SELECTION_STATE_CHOICES,
        default="suggested",
        db_index=True,
    )
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-crm_match_revision", "-score", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "crm_match_revision", "bitrix_deal_id"],
                name="candidate_crm_match_unique",
            ),
            models.UniqueConstraint(
                fields=["candidate"],
                condition=models.Q(selection_state="selected"),
                name="candidate_crm_selected_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=0, score__lte=100),
                name="candidate_crm_score_range",
            ),
        ]


class FactEvidence(models.Model):
    candidate = models.ForeignKey(
        FactCandidate, on_delete=models.PROTECT, related_name="evidence"
    )
    raw_message = models.ForeignKey(RawMessage, on_delete=models.PROTECT)
    quote = models.TextField()
    field_name = models.CharField(max_length=64, blank=True)


class ProjectRevision(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="revisions"
    )
    version = models.PositiveIntegerField()
    approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL
    )
    approved_at = models.DateTimeField(default=timezone.now)
    snapshot = models.JSONField(default=dict)
    source = models.CharField(max_length=32, default="review")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "version"], name="project_version_unique"
            )
        ]


class PaymentScheduleItem(models.Model):
    fact_event = models.ForeignKey("FactEvent", null=True, blank=True, on_delete=models.PROTECT)
    version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=16, default="active")
    direction = models.CharField(max_length=16, default="income")
    amount_precision = models.CharField(max_length=16, default="exact")
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="payment_schedule"
    )
    due_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="KZT")
    is_verified = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="schedule_positive"
            )
        ]


class PaymentAllocation(models.Model):
    financial_record = models.ForeignKey(
        FinancialRecord, on_delete=models.PROTECT, related_name="allocations"
    )
    schedule_item = models.ForeignKey(
        PaymentScheduleItem, on_delete=models.PROTECT, related_name="allocations"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(amount=0), name="allocation_nonzero"
            ),
            models.UniqueConstraint(
                fields=["financial_record", "schedule_item"], name="allocation_unique"
            ),
        ]


class SalesTarget(models.Model):
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.PROTECT)
    profile = models.ForeignKey(
        UserProfile, on_delete=models.PROTECT, related_name="targets"
    )
    month = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="KZT")
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    approved_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "profile", "month", "currency", "version"],
                name="target_version_unique",
            ),
            models.UniqueConstraint(
                fields=["team", "profile", "month", "currency"],
                condition=models.Q(is_active=True),
                name="target_active_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="target_nonnegative"
            ),
        ]


class StageTransition(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="stage_history"
    )
    project_revision = models.OneToOneField(ProjectRevision, on_delete=models.PROTECT)
    from_stage = models.CharField(max_length=64, blank=True)
    to_stage = models.CharField(max_length=64)
    effective_at = models.DateTimeField(default=timezone.now)


class Notification(models.Model):
    recipient = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="notifications"
    )
    business_event = models.ForeignKey(
        BusinessEvent, null=True, blank=True, on_delete=models.PROTECT
    )
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT
    )
    commitment = models.ForeignKey(
        Commitment, null=True, blank=True, on_delete=models.PROTECT
    )
    deduplication_key = models.CharField(max_length=255, unique=True)
    category = models.CharField(max_length=32, default="info")
    title = models.CharField(max_length=255)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)


class NotificationDelivery(models.Model):
    notification = models.ForeignKey(
        Notification, on_delete=models.PROTECT, related_name="deliveries"
    )
    channel = models.CharField(max_length=16, default="whatsapp")
    attempt_no = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=16, default="queued")
    provider_message_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "channel", "attempt_no"],
                name="delivery_attempt_unique",
            )
        ]


class ReminderOccurrence(models.Model):
    commitment = models.ForeignKey(Commitment, on_delete=models.PROTECT)
    notification = models.ForeignKey(
        Notification, null=True, blank=True, on_delete=models.PROTECT
    )
    recipient = models.ForeignKey(User, on_delete=models.PROTECT)
    commitment_version = models.PositiveIntegerField()
    rule_code = models.CharField(max_length=32)
    rule_version = models.PositiveIntegerField(default=1)
    scheduled_at = models.DateTimeField()
    state = models.CharField(max_length=16, default="queued")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "commitment",
                    "commitment_version",
                    "rule_code",
                    "rule_version",
                    "recipient",
                    "scheduled_at",
                ],
                name="reminder_unique",
            )
        ]


class OutboxEvent(models.Model):
    analysis_source_key = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    analysis_position = models.PositiveBigIntegerField(null=True, blank=True)
    business_event = models.ForeignKey(
        BusinessEvent, null=True, blank=True, on_delete=models.PROTECT
    )
    event_type = models.CharField(max_length=32)
    deduplication_key = models.CharField(max_length=255, unique=True)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=16, default="pending", db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["analysis_source_key", "state", "next_attempt_at", "analysis_position"], name="analysis_dispatch_idx")]


class AsyncOperation(models.Model):
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT)
    operation_type = models.CharField(max_length=32, default="chat")
    status = models.CharField(max_length=16, default="queued", db_index=True)
    request = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    idempotency_key = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    access_fingerprint = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["requested_by", "idempotency_key"],
                name="operation_request_unique",
            )
        ]


class AuditEvent(models.Model):
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    target_id = models.BigIntegerField(null=True, blank=True)
    target_type = models.CharField(max_length=64)
    action = models.CharField(max_length=64)
    before_after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        default_permissions = ("view",)


class PrivateAttachment(models.Model):
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    file = models.FileField(upload_to="private/%Y/%m/")
    content_type = models.CharField(max_length=128)
    sha256 = models.CharField(max_length=64)
    published_to_client = models.BooleanField(default=False)
    transcript = models.TextField(blank=True)
    state = models.CharField(max_length=16, default="queued")
    created_at = models.DateTimeField(auto_now_add=True)


class ProviderUsage(models.Model):
    outbox_event = models.ForeignKey(
        OutboxEvent, null=True, on_delete=models.PROTECT, related_name="provider_usage"
    )
    api_format = models.CharField(
        max_length=32,
        choices=AISettings.ChatApiFormat.choices,
        default=AISettings.ChatApiFormat.OPENAI_COMPATIBLE,
    )
    operation = models.CharField(max_length=32)
    model_name = models.CharField(max_length=128)
    duration_ms = models.PositiveIntegerField()
    succeeded = models.BooleanField()
    input_tokens = models.PositiveIntegerField(null=True)
    output_tokens = models.PositiveIntegerField(null=True)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=8, null=True)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


HISTORY_ACTIVE_STATES = (
    "waiting_connection",
    "waiting_sync",
    "watching",
    "collecting",
    "importing",
    "analyzing",
    "paused",
)
HISTORY_STATE_CHOICES = (
    ("waiting_connection", "Ожидает WhatsApp"),
    ("waiting_sync", "Ожидает синхронизацию истории"),
    ("watching", "Ожидаем новые сообщения"),
    ("collecting", "Собирает историю"),
    ("importing", "Сохраняет сообщения"),
    ("analyzing", "Анализирует сообщения"),
    ("paused", "Приостановлено"),
    ("completed", "Завершено"),
    ("completed_with_errors", "Завершено с ошибками анализа"),
    ("empty", "WAHA не вернул сообщений"),
    ("failed", "Ошибка"),
    ("cancelled", "Отменено"),
)


class WhatsAppHistoryJob(models.Model):
    config = models.OneToOneField(
        WhatsAppConfig,
        on_delete=models.PROTECT,
        related_name="history_job",
        verbose_name="Группа WhatsApp",
    )
    enabled = models.BooleanField("Задание включено", default=True)
    only_new = models.BooleanField(
        "Только новые",
        default=False,
        help_text="Постоянно получать новые сообщения с сохранённой точки. Старую историю можно загрузить полным импортом.",
    )
    new_message_poll_seconds = models.PositiveIntegerField(
        "Интервал проверки новых сообщений, секунды",
        default=5,
        help_text="5–3600 секунд. Работает постоянно, пока задание включено; период в минутах не используется.",
    )
    new_messages_since = models.DateTimeField(
        "Проверено по", null=True, blank=True, editable=False
    )
    new_messages_started_at = models.DateTimeField(
        "Начальная точка новых сообщений", null=True, blank=True, editable=False
    )
    checkpoint_source = models.JSONField(default=dict, blank=True, editable=False)
    last_checked_at = models.DateTimeField(
        "Последняя успешная проверка", null=True, blank=True, editable=False
    )
    interval_minutes = models.PositiveIntegerField(
        "Период запуска, минуты",
        default=0,
        help_text="Для полного импорта: 0 — только вручную, больше 0 — автоматические запуски. В режиме «Только новые» используется интервал в секундах.",
    )
    analyze_after_import = models.BooleanField(
        "Анализировать после импорта", default=True
    )
    page_size = models.PositiveIntegerField(
        "Сообщений в одной странице WAHA",
        default=250,
        help_text="Размер одного запроса (1–1000), не ограничение всей истории.",
    )
    initial_wait_seconds = models.PositiveIntegerField(
        "Ожидание начальной синхронизации, секунды", default=180
    )
    poll_seconds = models.PositiveIntegerField("Интервал проверки, секунды", default=30)
    stable_scans_required = models.PositiveIntegerField(
        "Стабильных проходов истории",
        default=3,
        help_text="Количество одинаковых полных проходов перед импортом (2–10).",
    )
    next_run_at = models.DateTimeField(
        "Следующий автоматический запуск", null=True, blank=True
    )
    updated_at = models.DateTimeField("Изменено", auto_now=True)

    class Meta:
        verbose_name = "настройка импорта WhatsApp"
        verbose_name_plural = "Настройки импорта WhatsApp"

    def __str__(self):
        return f"Импорт: {self.config.name}"

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        for name, low, high in (
            ("new_message_poll_seconds", 5, 3600),
            ("page_size", 1, 1000),
            ("initial_wait_seconds", 0, 86400),
            ("poll_seconds", 1, 3600),
            ("stable_scans_required", 2, 10),
        ):
            value = getattr(self, name)
            if value is None or not low <= value <= high:
                errors[name] = f"Допустимо от {low} до {high}."
        if (
            self.enabled
            and self.config_id
            and (
                not self.config.is_active
                or not self.config.team_id
                or not self.config.team.is_active
            )
        ):
            errors["config"] = "Нужна активная группа с активной командой."
        if self.pk:
            from django.db import connection

            query = type(self).objects.filter(pk=self.pk)
            previous = (
                query.select_for_update() if connection.in_atomic_block else query
            ).first()
            if previous:
                for field in (
                    "new_messages_since",
                    "new_messages_started_at",
                    "checkpoint_source",
                    "last_checked_at",
                    "next_run_at",
                ):
                    setattr(self, field, getattr(previous, field))
            if previous and (
                previous.only_new != self.only_new
                or previous.config_id != self.config_id
            ):
                if self.runs.filter(state__in=HISTORY_ACTIVE_STATES).exists():
                    errors["only_new"] = (
                        "Сначала отмените активный запуск, затем измените режим или источник."
                    )
        if errors:
            raise ValidationError(errors)


class WhatsAppHistoryRun(models.Model):
    job = models.ForeignKey(
        WhatsAppHistoryJob,
        on_delete=models.PROTECT,
        related_name="runs",
        verbose_name="Задание",
    )
    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Запустил"
    )
    import_kind = models.CharField(max_length=16, default="waha", choices=[("waha", "WhatsApp (WAHA)"), ("file", "TXT-экспорт"), ("saved", "Сохранённая история")])
    run_kind = models.CharField(max_length=16, default="full", choices=[("full", "Полный проход"), ("monitor", "Монитор новых сообщений")])
    export_upload = models.ForeignKey("WhatsAppExportUpload", null=True, blank=True, on_delete=models.PROTECT, related_name="runs")
    state = models.CharField(
        "Состояние",
        max_length=32,
        choices=HISTORY_STATE_CHOICES,
        default="waiting_connection",
        db_index=True,
    )
    resume_state = models.CharField(max_length=32, blank=True)
    source_snapshot = models.JSONField("Источник на момент запуска", default=dict)
    settings_snapshot = models.JSONField("Параметры на момент запуска", default=dict)
    incremental_state = models.JSONField(default=dict, blank=True)
    step = models.PositiveIntegerField(default=0)
    offset = models.PositiveIntegerField(default=0)
    scan_number = models.PositiveIntegerField(default=1)
    stable_scans = models.PositiveIntegerField(default=0)
    last_digest = models.CharField(max_length=64, blank=True)
    cutoff_at = models.DateTimeField("История по момент времени", null=True, blank=True)
    stage_ready_at = models.DateTimeField(null=True, blank=True)
    last_page_signature = models.CharField(max_length=64, blank=True)
    fetched_count = models.PositiveIntegerField("Получено из WAHA", default=0)
    imported_count = models.PositiveIntegerField("Новых сообщений", default=0)
    existing_count = models.PositiveIntegerField("Уже были в базе", default=0)
    no_text_count = models.PositiveIntegerField("Без текста", default=0)
    scheduled_count = models.PositiveIntegerField("Направлено на анализ", default=0)
    error_code = models.CharField("Код ошибки", max_length=64, blank=True)
    status_message = models.TextField("Подробности", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Последнее обновление", auto_now=True)
    finished_at = models.DateTimeField("Завершено", null=True, blank=True)
    messages = models.ManyToManyField(
        RawMessage, related_name="history_import_runs", blank=True
    )

    class Meta:
        verbose_name = "запуск импорта WhatsApp"
        verbose_name_plural = "Запуски импорта WhatsApp"
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "run_kind"],
                condition=models.Q(state__in=HISTORY_ACTIVE_STATES),
                name="one_active_whatsapp_run_kind",
            )
        ]

    def __str__(self):
        return f"Импорт #{self.pk}: {self.get_state_display()}"


class HistoryAnalysisItem(models.Model):
    run = models.ForeignKey(WhatsAppHistoryRun, on_delete=models.PROTECT, related_name="analysis_items")
    raw_message = models.ForeignKey(RawMessage, on_delete=models.PROTECT)
    outbox_event = models.ForeignKey(OutboxEvent, null=True, blank=True, on_delete=models.PROTECT)
    trace = models.ForeignKey(MessageProcessingTrace, null=True, blank=True, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, default="queued", db_index=True)
    disposition = models.CharField(max_length=32, blank=True)
    reason_code = models.CharField(max_length=64, blank=True)
    reason_description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "raw_message"], name="history_analysis_coverage_unique")]
        verbose_name = "результат анализа сообщения"
        verbose_name_plural = "Результаты анализа сообщений"


class WhatsAppHistoryItem(models.Model):
    run = models.ForeignKey(
        WhatsAppHistoryRun, on_delete=models.CASCADE, related_name="items"
    )
    message_id = models.CharField(max_length=128)
    timestamp = models.DateTimeField()
    payload = models.JSONField(default=dict)
    seen_scan = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "message_id"], name="history_run_message_unique"
            )
        ]
        indexes = [
            models.Index(
                fields=["run", "timestamp", "id"], name="history_item_time_idx"
            )
        ]


class WhatsAppExportUpload(models.Model):
    job = models.ForeignKey(WhatsAppHistoryJob, on_delete=models.PROTECT, related_name="exports")
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    file = models.FileField(upload_to="whatsapp_exports/%Y/%m/")
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    source_key = models.CharField(max_length=64)
    timezone = models.CharField(max_length=64)
    date_order = models.CharField(max_length=3, default="DMY")
    source_snapshot = models.JSONField(default=dict)
    state = models.CharField(max_length=16, default="queued")
    diagnostics = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "source_key", "sha256", "timezone", "date_order"], name="whatsapp_export_upload_unique")]
        verbose_name = "экспорт WhatsApp"
        verbose_name_plural = "Экспорты WhatsApp"


class WhatsAppExportEntry(models.Model):
    upload = models.ForeignKey(WhatsAppExportUpload, on_delete=models.PROTECT, related_name="entries")
    raw_message = models.ForeignKey(RawMessage, null=True, blank=True, on_delete=models.PROTECT)
    ordinal = models.PositiveIntegerField()
    line_start = models.PositiveIntegerField()
    line_end = models.PositiveIntegerField()
    sent_at = models.DateTimeField()
    time_precision = models.CharField(max_length=8)
    kind = models.CharField(max_length=16)
    fingerprint = models.CharField(max_length=64, db_index=True)
    resolution_state = models.CharField(max_length=32)
    reason_code = models.CharField(max_length=64, blank=True)
    reason_description = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["upload", "ordinal"], name="whatsapp_export_entry_unique")]
        verbose_name = "запись TXT-экспорта"
        verbose_name_plural = "Записи TXT-экспорта и причины"


class WhatsAppMessageAlias(models.Model):
    config = models.ForeignKey(WhatsAppConfig, on_delete=models.PROTECT)
    raw_message = models.ForeignKey(RawMessage, on_delete=models.PROTECT, related_name="transport_aliases")
    session_name = models.CharField(max_length=64)
    chat_id = models.CharField(max_length=128)
    namespace = models.CharField(max_length=16)
    external_id = models.CharField(max_length=128)
    source_revision = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["config", "session_name", "chat_id", "namespace", "external_id", "source_revision"], name="whatsapp_transport_alias_unique")]


class McpToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mcp_tokens")
    name = models.CharField(max_length=128, default="default")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    purpose = models.CharField(max_length=16, default="external", choices=[("external", "External"), ("portal", "Portal")])
    token_encrypted = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["user"], condition=models.Q(purpose="portal", is_active=True), name="one_active_portal_mcp_token")]



class Participant(models.Model):
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    user_profile = models.ForeignKey(UserProfile, null=True, blank=True, on_delete=models.PROTECT)
    display_name = models.CharField(max_length=255)


class ParticipantIdentity(models.Model):
    participant = models.ForeignKey(Participant, on_delete=models.PROTECT, related_name="identities")
    namespace = models.CharField(max_length=255)
    value = models.CharField(max_length=255)
    resolution_state = models.CharField(max_length=16, default="resolved")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["namespace", "value"], name="participant_identity_unique")]


class CompanyAlias(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="aliases")
    normalized_name = models.CharField(max_length=255, db_index=True)
    decision = models.ForeignKey("FactDecision", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "normalized_name"], name="company_alias_unique")]


class ProjectAlias(models.Model):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="aliases")
    normalized_name = models.CharField(max_length=255, db_index=True)
    decision = models.ForeignKey("FactDecision", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "normalized_name"], name="project_alias_unique")]


class ProjectParty(models.Model):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="parties")
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    role = models.CharField(max_length=32)
    decision = models.ForeignKey("FactDecision", on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "company", "role"], name="project_party_unique")]


class FactDecision(models.Model):
    candidate = models.ForeignKey(FactCandidate, on_delete=models.PROTECT, related_name="decisions")
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=16, choices=[(x, x) for x in ("accepted", "deferred", "rejected", "superseded")])
    actor_kind = models.CharField(max_length=16, default="system")
    policy_version = models.CharField(max_length=64)
    input_fingerprint = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=64)
    explanation = models.TextField()
    validation = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["candidate", "policy_version", "input_fingerprint"], name="fact_decision_input_unique")]


class FactEvent(models.Model):
    is_superseded = models.BooleanField(default=False, db_index=True)
    decision = models.ForeignKey(FactDecision, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT, related_name="fact_events")
    event_key = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ("view",)


class FieldAssertion(models.Model):
    fact_event = models.ForeignKey(FactEvent, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT, related_name="assertions")
    commitment = models.ForeignKey(Commitment, null=True, blank=True, on_delete=models.PROTECT)
    financial_record = models.ForeignKey(FinancialRecord, null=True, blank=True, on_delete=models.PROTECT)
    field_name = models.CharField(max_length=64)
    value_state = models.CharField(max_length=16, default="known")
    value = models.JSONField(null=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(project__isnull=False, commitment__isnull=True, financial_record__isnull=True) | models.Q(project__isnull=True, commitment__isnull=False, financial_record__isnull=True) | models.Q(project__isnull=True, commitment__isnull=True, financial_record__isnull=False)), name="assertion_one_target")]


class SourceWorkItem(models.Model):
    raw_message = models.ForeignKey(RawMessage, on_delete=models.PROTECT, related_name="work_items")
    processing_version = models.CharField(max_length=64)
    state = models.CharField(max_length=32, default="pending", db_index=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["raw_message", "processing_version"], name="source_work_version_unique")]


class SourceCheckpoint(models.Model):
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    source_scope = models.CharField(max_length=64)
    complete_through = models.DateTimeField(null=True, blank=True)
    gaps = models.JSONField(default=list)
    counts = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["team", "source_scope"], name="source_checkpoint_unique")]


class MessageArtifact(models.Model):
    raw_message = models.OneToOneField(RawMessage, on_delete=models.PROTECT, related_name="artifacts")
    checksum = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=32, default="unavailable")
    extracted_text = models.TextField(blank=True)
    error_code = models.CharField(max_length=64, blank=True)


class ExternalObjectLink(models.Model):
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    integration_key = models.CharField(max_length=64)
    object_type = models.CharField(max_length=32)
    local_type = models.CharField(max_length=32)
    local_id = models.PositiveBigIntegerField()
    external_id = models.CharField(max_length=64, blank=True, null=True)
    origin_key = models.CharField(max_length=255, unique=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["team", "integration_key", "object_type", "external_id"], condition=models.Q(external_id__isnull=False), name="external_object_namespace_unique")]


class CrmDelivery(models.Model):
    outbox_event = models.OneToOneField(OutboxEvent, on_delete=models.PROTECT)
    external_object_link = models.ForeignKey(ExternalObjectLink, on_delete=models.PROTECT)
    fact_event = models.ForeignKey(FactEvent, null=True, blank=True, on_delete=models.PROTECT)
    target_version = models.PositiveIntegerField()
    state = models.CharField(max_length=32, default="pending")
    patch = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ProviderReservation(models.Model):
    purpose = models.CharField(max_length=16, default="live")
    config = models.ForeignKey(AISettings, on_delete=models.PROTECT)
    usage = models.OneToOneField(ProviderUsage, null=True, blank=True, on_delete=models.PROTECT)
    operation = models.CharField(max_length=32)
    reserved_tokens = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16, default="reserved")
    lease_until = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
