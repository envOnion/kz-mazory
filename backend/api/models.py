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

    class Meta:
        verbose_name = 'Профиль пользователя'
        verbose_name_plural = 'Профили пользователей'

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
    bitrix_company_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
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
    Объекты / сделки компании Aqua Kip (соответствуют crm_deal).
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

    name = models.CharField('Название объекта', max_length=255, db_index=True)
    bitrix_id = models.CharField('Bitrix24 ID сделки', max_length=64, blank=True, null=True, unique=True)
    contract_number = models.CharField('Номер договора', max_length=255, blank=True, default='')
    deal_period = models.CharField('Период сделки', max_length=128, blank=True, default='')
    
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name='projects', verbose_name='Компания')
    manager = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='projects', verbose_name='Менеджер')
    project_type = models.CharField('Тип заказчика', max_length=32, choices=PROJECT_TYPE_CHOICES, default='private')
    status = models.CharField('Статус сделки', max_length=64, choices=STATUS_CHOICES, default='qualification')
    
    # Финансовые показатели (тенге ₸)
    contract_amount = models.DecimalField('Сумма Договора ₸', max_digits=14, decimal_places=2, default=0.00)
    cost_amount = models.DecimalField('Себестоимость ₸', max_digits=14, decimal_places=2, default=0.00)
    target_margin_percent = models.DecimalField('Плановая маржа %', max_digits=5, decimal_places=2, default=16.80)
    actual_margin_percent = models.DecimalField('Фактическая маржа %', max_digits=5, decimal_places=2, default=0.00)
    paid_amount = models.DecimalField('Оплачено ₸', max_digits=14, decimal_places=2, default=0.00)
    due_amount = models.DecimalField('Остаток / Дебиторка ₸', max_digits=14, decimal_places=2, default=0.00)
    guarantee_amount = models.DecimalField('Гарантийные оплаты ₸', max_digits=14, decimal_places=2, default=0.00)
    barter_amount = models.DecimalField('Сумма бартера ₸', max_digits=14, decimal_places=2, default=0.00)
    avr_status = models.CharField('Накладные / АВР', max_length=64, default='Не закрыт')

    equipment_type = models.CharField('Оборудование', max_length=255, blank=True, default='БТП')
    priority = models.CharField('Приоритет', max_length=16, choices=PRIORITY_CHOICES, default='standard')
    
    # Процессные атрибуты
    current_action = models.TextField('Последнее действие', blank=True, default='')
    next_action = models.TextField('Следующий шаг', blank=True, default='')
    next_action_at = models.DateTimeField('Срок следующего действия', null=True, blank=True)
    decision_maker = models.CharField('ЛПР', max_length=255, blank=True, default='')
    blocker = models.TextField('Блокер / Проблема', blank=True, default='')
    notes = models.TextField('Заметки / История', blank=True, default='')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Проект / Объект'
        verbose_name_plural = 'Проекты / Объекты'
        ordering = ['-contract_amount']

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def profit_amount(self):
        return (self.contract_amount or 0) - (self.cost_amount or 0)

    def save(self, *args, **kwargs):
        # Автоматический пересчет маржи и дебиторки
        if self.contract_amount and self.contract_amount > 0:
            if self.cost_amount is not None:
                profit = self.contract_amount - self.cost_amount
                self.actual_margin_percent = round((profit / self.contract_amount) * 100, 2)
            self.due_amount = max(0, self.contract_amount - (self.paid_amount or 0))
        super().save(*args, **kwargs)


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
        verbose_name = 'Сырое сообщение WhatsApp'
        verbose_name_plural = 'Сырые сообщения WhatsApp'
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

    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True, related_name='commitments', verbose_name='Объект')
    manager = models.ForeignKey(UserProfile, on_delete=models.CASCADE, null=True, blank=True, related_name='commitments', verbose_name='Менеджер')
    source_message = models.ForeignKey(RawMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name='commitments')
    
    counterparty_person = models.CharField('Кому обещано / ЛПР', max_length=255, blank=True, default='')
    commitment_text = models.TextField('Суть обещания')
    promised_at = models.DateTimeField(auto_now_add=True)
    deadline = models.DateField('Дедлайн', null=True, blank=True)
    status = models.CharField('Статус', max_length=32, choices=STATUS_CHOICES, default='pending')
    severity = models.CharField('Срочность', max_length=16, choices=SEVERITY_CHOICES, default='medium')
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
        ('barter', 'Взаимозачёт / Бартер'),
        ('debt_collection', 'Взыскание задолженности'),
    ]
    STATUS_CHOICES = [
        ('expected', 'Ожидается к сбору'),
        ('received', 'Получено на расчетный счет'),
        ('delayed', 'Задержка платежа'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='financial_records', verbose_name='Объект')
    amount = models.DecimalField('Сумма ₸', max_digits=14, decimal_places=2)
    payment_date = models.DateField('Дата платежа')
    payment_type = models.CharField('Тип платежа', max_length=32, choices=PAYMENT_TYPE_CHOICES, default='milestone')
    status = models.CharField('Статус', max_length=32, choices=STATUS_CHOICES, default='received')
    notes = models.TextField('Комментарий / Основание', blank=True, default='')
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
    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True, related_name='events', verbose_name='Объект')
    manager = models.ForeignKey(UserProfile, on_delete=models.CASCADE, null=True, blank=True, related_name='events', verbose_name='Менеджер')
    title = models.CharField('Заголовок', max_length=255)
    description = models.TextField('Описание', blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)
    severity = models.CharField('Важность', max_length=16, choices=SEVERITY_CHOICES, default='info')

    class Meta:
        verbose_name = 'Бизнес-событие'
        verbose_name_plural = 'Бизнес-события'
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.severity}] {self.title}"


# ============================================================================
# Dynamic Configurations Managed via Django Admin
# ============================================================================

class WhatsAppConfig(models.Model):
    """
    Настройки интеграции с WAHA и отслеживаемой группы WhatsApp.
    """
    name = models.CharField('Название чата / группы', max_length=255, default='КОМАНДА ПОДДЕРЖКИ ПРОДАЖ')
    group_jid = models.CharField('WhatsApp Group JID', max_length=128, default='120363024823904923@g.us', help_text='Например: 120363024823904923@g.us')
    session_name = models.CharField('Имя сессии в WAHA', max_length=64, default='default')
    waha_api_url = models.CharField('URL WAHA API', max_length=255, default='http://waha:3000')
    waha_api_key = models.CharField('WAHA API Key', max_length=128, default='mazory-waha-key-2026', blank=True)
    is_active = models.BooleanField('Мониторинг активен', default=True)
    status = models.CharField('Статус подключения', max_length=32, default='WORKING')
    last_qr_code = models.TextField('QR-код (base64)', blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Настройка WhatsApp (WAHA)'
        verbose_name_plural = 'Настройки WhatsApp (WAHA)'

    def __str__(self):
        return f"{self.name} ({'Активен' if self.is_active else 'Выключен'})"

    @classmethod
    def get_active(cls):
        cfg = cls.objects.filter(is_active=True).first()
        if not cfg:
            cfg = cls.objects.create()
        return cfg


class AISettings(models.Model):
    """
    Конфигурация нейросетевых моделей (Embeddings и Chat/Reasoning) через OpenRouter.
    """
    name = models.CharField('Конфигурация', max_length=128, default='Основная конфигурация OpenRouter')
    
    # Embeddings
    embedding_provider_url = models.CharField('Embeddings Base URL', max_length=255, default='https://openrouter.ai/api/v1')
    embedding_model_name = models.CharField('Embeddings Model', max_length=128, default='liquid/lfm-2.5-embedding-350m:free')
    embedding_api_key = models.CharField('Embeddings API Key', max_length=255, blank=True, default='')
    embedding_dimension = models.IntegerField('Размерность вектора', default=1024)
    
    # Chat & Reasoning
    chat_provider_url = models.CharField('Chat Base URL', max_length=255, default='https://openrouter.ai/api/v1')
    chat_model_name = models.CharField('Chat LLM Model', max_length=128, default='nvidia/nemotron-3-ultra-550b-a55b:free')
    chat_api_key = models.CharField('Chat API Key', max_length=255, blank=True, default='')
    chat_temperature = models.FloatField('Temperature', default=0.2)
    
    system_prompt_worker = models.TextField('Промпт извлечения сделок из чата', default="""Ты эксперт-аналитик рабочей группы продаж AquaKip.
Твоя задача — извлечь из сообщения факты по коммерческим сделкам.
Не выдумывай данные. Если чего-то нет в тексте — возвращай null.
Формат ответа — строго валидный JSON:
{
  "is_deal_fact": true|false,
  "confidence": 0.0-1.0,
  "object_name": "Название объекта",
  "company_name": "Компания заказчика или null",
  "direction": "Оборудование: БМК, БТП, НС, Котлы и т.д.",
  "contract_number": "Номер договора или null",
  "deal_period": "Период сделки или null",
  "stage": "lead|qualification|proposal_sent|contract_signing|in_execution|completed|stalled",
  "contract_amount": число или null,
  "cost_amount": число или null,
  "paid_amount": число или null,
  "barter_amount": число или null,
  "guarantee_amount": число или null,
  "avr_status": "Закрыт|Не закрыт|null",
  "responsible_name": "Имя менеджера",
  "current_action": "Что сделано",
  "next_action": "Следующий шаг",
  "next_action_at": "ISO 8601 дата или null",
  "decision_maker": "ЛПР или null",
  "blocker": "Блокер или null",
  "priority": "A+++|A++|A+|standard",
  "can_create_deal": true|false
}""")
    
    system_prompt_assistant = models.TextField('Промпт чат-ассистента для пользователей', default="""Ты аналитический ассистент Mazory AI платформы продаж AquaKip.
Отвечай точно, структурированно, опираясь на факты из базы данных и сообщений.
Используй тенге (₸) для сумм. Если пользователь просит графики или сравнения, возвращай данные в понятной форме.""")

    is_active = models.BooleanField('Активная конфигурация', default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Настройки моделей AI'
        verbose_name_plural = 'Настройки моделей AI'

    def __str__(self):
        return f"{self.name} ({self.chat_model_name})"

    @classmethod
    def get_active(cls):
        cfg = cls.objects.filter(is_active=True).first()
        if not cfg:
            cfg = cls.objects.create()
        return cfg


class BitrixSettings(models.Model):
    """
    Настройки интеграции с Bitrix24 REST API.
    """
    name = models.CharField('Название интеграции', max_length=128, default='AquaKip Bitrix24 Portal')
    webhook_url = models.CharField('REST Webhook URL', max_length=255, default='https://aquakip.bitrix24.kz/rest/148/71vwif5ivu5f4abk/')
    is_active = models.BooleanField('Синхронизация активна', default=True)
    auto_create_deals = models.BooleanField('Авто-создание сделок в Bitrix24', default=True)
    deal_category_id = models.IntegerField('ID воронки сделок', default=0)
    default_assigned_by_id = models.IntegerField('ID ответственного по умолчанию', default=1)
    last_sync_at = models.DateTimeField('Последняя синхронизация', null=True, blank=True)
    last_sync_status = models.TextField('Статус последней операции', blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Настройки Bitrix24'
        verbose_name_plural = 'Настройки Bitrix24'

    def __str__(self):
        return f"{self.name} ({'Активен' if self.is_active else 'Выключен'})"

    @classmethod
    def get_active(cls):
        cfg = cls.objects.filter(is_active=True).first()
        if not cfg:
            cfg = cls.objects.create()
        return cfg
