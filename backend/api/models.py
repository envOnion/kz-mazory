from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    full_name = models.CharField(max_length=255, default='Камиль')
    role = models.CharField(max_length=255, default='Ведущий менеджер по продажам')
    department = models.CharField(max_length=255, default='Отдел продаж Aqua Kip')
    email = models.EmailField(blank=True, default='kamil@aquakip.kz')
    phone = models.CharField(max_length=32, blank=True, default='+7 (701) 123-45-67')
    avatar_url = models.URLField(
        blank=True,
        default='https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=250&q=80'
    )
    
    # Personal Sales KPI
    monthly_target = models.DecimalField(max_digits=14, decimal_places=2, default=50000000.00)
    current_sales = models.DecimalField(max_digits=14, decimal_places=2, default=31790000.00)
    deals_count = models.IntegerField(default=15)
    rank_in_team = models.IntegerField(default=1)
    conversion_rate = models.DecimalField(max_digits=5, decimal_places=2, default=35.00)

    # WhatsApp Notifications via WAHA
    whatsapp_daily_digest = models.BooleanField(default=True)
    whatsapp_stalled_deals = models.BooleanField(default=True)
    whatsapp_critical_kpi = models.BooleanField(default=True)

    # AI Mazory Settings
    AI_MODE_CHOICES = [
        ('detailed', 'Подробный с аналитикой и инсайтами'),
        ('concise', 'Лаконичный (Bullet points)'),
        ('finance', 'Финансовый (Только цифры и таблицы)'),
    ]
    ai_response_mode = models.CharField(max_length=32, choices=AI_MODE_CHOICES, default='detailed')
    ai_auto_suggest_next_actions = models.BooleanField(default=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.full_name} ({self.user.username})"

    @property
    def kpi_percent(self):
        if self.monthly_target and self.monthly_target > 0:
            return round((float(self.current_sales) / float(self.monthly_target)) * 100, 1)
        return 0.0


class Company(models.Model):
    """
    Контрагенты: застройщики, девелоперы, генподрядчики, проектные институты.
    """
    CLIENT_TYPE_CHOICES = [
        ('private', 'Частный девелопер'),
        ('state', 'Государственный заказчик'),
        ('quasi_state', 'Квазигосударственный сектор'),
        ('contractor', 'Генподрядчик / Монтажники'),
        ('designer', 'Проектная организация'),
        ('other', 'Прочее'),
    ]
    name = models.CharField(max_length=255, unique=True)
    client_type = models.CharField(max_length=32, choices=CLIENT_TYPE_CHOICES, default='private')
    contact_person = models.CharField(max_length=255, blank=True, default='')
    phone = models.CharField(max_length=64, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Компания'
        verbose_name_plural = 'Компании'
        ordering = ['name']

    def __str__(self):
        return self.name


class Project(models.Model):
    """
    Объекты / сделки компании Aqua Kip.
    """
    STATUS_CHOICES = [
        ('lead', 'Лид / Первичный контакт'),
        ('qualification', 'Квалификация / Сбор ТЗ'),
        ('design', 'Проектирование / Экспертиза'),
        ('proposal_sent', 'КП отправлено'),
        ('contract_signing', 'Согласование / Договор'),
        ('in_execution', 'В исполнении / Производство / Монтаж'),
        ('completed', 'Завершен / Сдан'),
        ('stalled', 'Завис / Требует внимания'),
        ('lost', 'Проигран / Архив'),
    ]
    PROJECT_TYPE_CHOICES = [
        ('private', 'Частный'),
        ('state', 'Государственный'),
        ('quasi_state', 'Квазигосударственный'),
    ]
    PRIORITY_CHOICES = [
        ('A+++', 'A+++ Стратегический'),
        ('A++', 'A++ Высокий'),
        ('A+', 'A+ Приоритетный'),
        ('standard', 'Стандартный'),
    ]

    name = models.CharField(max_length=255)
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name='projects')
    manager = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='projects')
    project_type = models.CharField(max_length=32, choices=PROJECT_TYPE_CHOICES, default='private')
    status = models.CharField(max_length=64, choices=STATUS_CHOICES, default='qualification')
    
    # Финансовые показатели (тенге ₸)
    contract_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    cost_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    target_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=16.80)
    actual_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    due_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)

    equipment_type = models.CharField(max_length=128, blank=True, default='БТП')
    priority = models.CharField(max_length=16, choices=PRIORITY_CHOICES, default='standard')
    notes = models.TextField(blank=True, default='')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Проект / Объект'
        verbose_name_plural = 'Проекты / Объекты'
        ordering = ['-contract_amount']

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"


class RawMessage(models.Model):
    """
    Сырые сообщения из WhatsApp-чатов для аудита, векторизации в Qdrant и извлечения сущностей.
    """
    message_id = models.CharField(max_length=128, unique=True, db_index=True)
    chat_id = models.CharField(max_length=128, blank=True, default='')
    sender_phone = models.CharField(max_length=64, blank=True, default='')
    sender_name = models.CharField(max_length=255, blank=True, default='')
    timestamp = models.DateTimeField()
    content = models.TextField()
    raw_payload = models.JSONField(default=dict, blank=True)
    qdrant_point_id = models.CharField(max_length=64, blank=True, default='')
    processed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Сырое сообщение'
        verbose_name_plural = 'Сырые сообщения'
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.sender_name} [{self.timestamp}]: {self.content[:40]}..."


class Commitment(models.Model):
    """
    Обещания, дедлайны и поручения, зафиксированные в коммуникациях.
    """
    STATUS_CHOICES = [
        ('pending', 'В работе'),
        ('fulfilled', 'Выполнено'),
        ('overdue', 'Просрочено'),
        ('cancelled', 'Отменено'),
    ]
    SEVERITY_CHOICES = [
        ('critical', 'Критический'),
        ('medium', 'Средний'),
        ('low', 'Низкий'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True, related_name='commitments')
    manager = models.ForeignKey(UserProfile, on_delete=models.CASCADE, null=True, blank=True, related_name='commitments')
    source_message = models.ForeignKey(RawMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name='commitments')
    
    counterparty_person = models.CharField(max_length=255, blank=True, default='')
    commitment_text = models.TextField()
    promised_at = models.DateTimeField(auto_now_add=True)
    deadline = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='pending')
    severity = models.CharField(max_length=16, choices=SEVERITY_CHOICES, default='medium')
    fulfilled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Обязательство / Обещание'
        verbose_name_plural = 'Обязательства / Обещания'
        ordering = ['deadline', 'id']

    def __str__(self):
        return f"[{self.status}] {self.commitment_text[:50]} (до {self.deadline})"


class FinancialRecord(models.Model):
    """
    Факты оплат и финансовые движения по объектам.
    """
    PAYMENT_TYPE_CHOICES = [
        ('advance', 'Аванс'),
        ('milestone', 'Промежуточный платёж'),
        ('final', 'Окончательный расчет'),
        ('debt_collection', 'Взыскание задолженности'),
    ]
    STATUS_CHOICES = [
        ('expected', 'Ожидается к сбору'),
        ('received', 'Получено на расчетный счет'),
        ('delayed', 'Задержка платежа'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='financial_records')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_date = models.DateField()
    payment_type = models.CharField(max_length=32, choices=PAYMENT_TYPE_CHOICES, default='milestone')
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='received')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Финансовая запись'
        verbose_name_plural = 'Финансовые записи'
        ordering = ['-payment_date']

    def __str__(self):
        return f"{self.project.name}: {self.amount:,.2f} ₸ ({self.status})"


class BusinessEvent(models.Model):
    """
    Бизнес-события: изменение цены, срыв сроков, критические инциденты.
    """
    SEVERITY_CHOICES = [
        ('info', 'Инфо'),
        ('warning', 'Внимание'),
        ('critical', 'Критично'),
    ]

    event_type = models.CharField(max_length=64)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True, related_name='events')
    manager = models.ForeignKey(UserProfile, on_delete=models.CASCADE, null=True, blank=True, related_name='events')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)
    severity = models.CharField(max_length=16, choices=SEVERITY_CHOICES, default='info')

    class Meta:
        verbose_name = 'Бизнес-событие'
        verbose_name_plural = 'Бизнес-события'
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.severity}] {self.title}"
