"""Versioned semantic catalog. Names here, never model-provided SQL, compile queries."""
from dataclasses import dataclass, field

VERSION = '2.0'
GRAINS = ('day', 'week', 'month', 'quarter', 'year')
TIME_LABELS = {
    'created_at': 'Создание записи в Mazory', 'updated_at': 'Последнее изменение записи в Mazory',
    'source_created_at': 'Создание в источнике', 'source_updated_at': 'Изменение в источнике',
    'payment_date': 'Дата оплаты', 'promised_at': 'Постановка обязательства',
    'effective_deadline': 'Текущий срок', 'original_deadline_at': 'Исходный срок',
    'fulfilled_at': 'Исполнение', 'timestamp': 'Время отправки / события',
    'received_at': 'Получение сообщения', 'month': 'Месяц плана', 'approved_at': 'Утверждение',
    'source_stage_changed_at': 'Последний переход стадии в CRM', 'source_begin_date': 'Начало сделки',
    'source_close_date': 'Плановое окончание сделки', 'synced_at': 'Наблюдение CRM',
    'effective_at': 'Доказанное время события', 'event_at': 'Доказанное время события',
    'recorded_at': 'Регистрация события', 'due_date': 'Срок оплаты', 'reviewed_at': 'Проверка факта',
    'complete_through': 'История подтверждена до',
}


@dataclass(frozen=True)
class Dataset:
    view: str
    label: str
    scope: str
    dimensions: dict
    measures: dict
    dates: tuple = ('created_at', 'updated_at')
    default_date: str | None = None
    aliases: dict = field(default_factory=dict)
    definition: str = ''

    def fields(self):
        fields = {name: (name, kind) for name, kind in self.dimensions.items()}
        for axis in self.dates:
            fields[axis] = (axis, 'date')
            for grain in GRAINS:
                fields[f'{axis}_{grain}'] = (axis, 'date')
        for prefix, axis in self.aliases.items():
            for grain in GRAINS:
                fields[f'{prefix}_{grain}'] = (axis, 'date')
        return fields


COMMON = {'project_id': 'id', 'team_id': 'id', 'profile_id': 'id'}
COUNT = ('COUNT(DISTINCT v.id)', 'count')
EVENT_COUNT = {'event_count': COUNT}
PROJECT_MEASURES = {
    'project_count': COUNT,
    'contract_amount': ('CASE WHEN COUNT(v.contract_amount)=COUNT(*) THEN SUM(v.contract_amount) END', 'money'),
    'confirmed_cost': ('CASE WHEN COUNT(v.confirmed_cost)=COUNT(*) AND COUNT(*)>0 THEN SUM(v.confirmed_cost) END', 'money'),
    'contract_margin_percent': ('ROUND((SUM(v.margin_contract)-SUM(v.margin_cost))/NULLIF(SUM(v.margin_contract),0)*100,2)', 'percent'),
}
COMMITMENT_MEASURES = {
    'commitment_count': COUNT,
    'overdue_count': ("COUNT(DISTINCT v.id) FILTER (WHERE v.status IN ('pending','overdue') AND v.effective_deadline<now())", 'count'),
}
CATALOG = {
    'projects': Dataset('projects', 'Проекты', 'projects', {**COMMON, 'company_id': 'id', 'project_manager': 'text', 'team': 'text', 'status': 'text', 'project_type': 'text'}, PROJECT_MEASURES,
        ('created_at', 'updated_at', 'source_created_at', 'source_updated_at'), None,
        {'project': 'source_created_at'}, 'Канонические проекты. Создание в источнике и вставка в Mazory — разные даты. Договорные суммы только по известным данным.'),
    'crm_projects': Dataset('crm_deals', 'Сделки CRM', 'crm_projects', {**COMMON, 'company_id': 'id', 'status': 'text', 'crm_manager': 'text', 'team': 'text'},
        {'project_count': ('COUNT(DISTINCT v.project_id)', 'count'), 'crm_amount': ('CASE WHEN COUNT(v.crm_amount)=COUNT(*) THEN SUM(v.crm_amount) END', 'money')},
        ('created_at', 'updated_at', 'source_created_at', 'source_updated_at', 'source_stage_changed_at', 'source_begin_date', 'source_close_date', 'synced_at'), None,
        {'deal': 'source_created_at'}, 'Сделки CRM: opportunity не является поступлениями. CLOSEDATE — плановое окончание. Текущая стадия не доказывает прошлую историю.'),
    'payments': Dataset('payments', 'Платежи', 'payments', {**COMMON, 'credited_manager': 'text', 'team': 'text', 'status': 'text', 'project_type': 'text'},
        {'payment_count': COUNT, 'received_amount': ('COALESCE(SUM(v.received_amount),0)', 'money')},
        ('created_at', 'updated_at', 'payment_date'), 'payment_date', {'payment': 'payment_date'}, 'Подтверждённые поступления с отрицательными корректировками. Менеджер — credited manager.'),
    'commitments': Dataset('commitments', 'Обязательства', 'commitments', {**COMMON, 'responsible_manager': 'text', 'team': 'text', 'project_status': 'text', 'status': 'text', 'severity': 'text'},
        COMMITMENT_MEASURES, ('created_at', 'updated_at', 'promised_at', 'effective_deadline', 'original_deadline_at', 'fulfilled_at'), 'effective_deadline',
        {'effective_deadline': 'effective_deadline'}, 'Подтверждённые обязательства. Постановка, срок и исполнение — разные оси; просрочки включают старые сроки.'),
    'targets': Dataset('sales_targets', 'Планы продаж', 'targets', {'team_id': 'id', 'profile_id': 'id', 'manager': 'text', 'team': 'text'},
        {'target_amount': ('SUM(v.target_amount)', 'money'), 'target_count': COUNT}, ('created_at', 'updated_at', 'month', 'approved_at'), 'month', {}, 'Активные утверждённые месячные планы. Отсутствующий план неизвестен, не ноль.'),
    'messages': Dataset('messages', 'Сообщения', 'messages', {'team_id': 'id', 'project_id': 'id', 'chat': 'text', 'team': 'text', 'source': 'text'},
        {'message_count': COUNT}, ('created_at', 'updated_at', 'timestamp', 'received_at'), 'timestamp', {'message': 'timestamp'}, 'Канонические сообщения; неизвестное время отправки не заменяется временем импорта.'),
    'companies': Dataset('companies', 'Компании', 'companies', {'company_id': 'id', 'name': 'text', 'client_type': 'text'}, {'company_count': COUNT}, definition='Доступные контрагенты. Технические даты описывают запись Mazory.'),
    'payment_schedule': Dataset('payment_schedule', 'График оплат', 'payment_schedule', {**COMMON, 'status': 'text'},
        {'schedule_count': COUNT, 'scheduled_amount': ('SUM(v.scheduled_amount)', 'money'), 'outstanding_amount': ('SUM(v.outstanding_amount)', 'money')}, ('created_at', 'updated_at', 'due_date'), 'due_date', {}, 'Подтверждённые пункты графика с остатком после агрегации распределённых платежей.'),
    'fact_review': Dataset('fact_review', 'Проверка предложений', 'fact_review', {**COMMON, 'fact_type': 'text', 'status': 'text'}, {'candidate_count': COUNT}, ('created_at', 'updated_at', 'reviewed_at'), 'created_at', {}, 'Предложения отдельно от подтверждённых бизнес-фактов.'),
    'business_events': Dataset('business_events', 'Бизнес-события', 'business_events', {**COMMON, 'event_type': 'text', 'severity': 'text'}, EVENT_COUNT, ('created_at', 'updated_at', 'timestamp'), 'timestamp'),
    'entity_revisions': Dataset('entity_revisions', 'Версии сущностей', 'history', {'project_id': 'id', 'team_id': 'id', 'entity_id': 'id', 'entity_type': 'text', 'event_kind': 'text', 'time_precision': 'text'},
        {'revision_count': COUNT}, ('created_at', 'updated_at', 'effective_at', 'recorded_at'), 'recorded_at', {}, 'Версии, включая baseline. recorded_at — наблюдение, effective_at — доказанное время.'),
    'entity_events': Dataset('entity_events', 'Изменения сущностей', 'history', {'project_id': 'id', 'team_id': 'id', 'entity_id': 'id', 'entity_type': 'text', 'event_kind': 'text', 'time_precision': 'text'}, EVENT_COUNT,
        ('created_at', 'updated_at', 'event_at', 'recorded_at'), 'recorded_at', {}, 'Каждое значимое изменение, а не только последнее updated_at. Baseline исключён.'),
    'project_stage_events': Dataset('project_stage_events', 'Переходы стадий', 'history', {'project_id': 'id', 'team_id': 'id', 'entity_type': 'text', 'status': 'text', 'from_stage': 'text', 'time_precision': 'text'}, EVENT_COUNT,
        ('created_at', 'updated_at', 'event_at', 'recorded_at'), 'recorded_at', {}, 'Известные переходы. Наблюдение и точное время CRM различаются; пропущенные стадии неизвестны.'),
    'commitment_events': Dataset('commitment_events', 'История обязательств', 'history', {'project_id': 'id', 'team_id': 'id', 'entity_id': 'id', 'entity_type': 'text', 'event_kind': 'text', 'time_precision': 'text'}, EVENT_COUNT,
        ('created_at', 'updated_at', 'event_at', 'recorded_at'), 'recorded_at'),
    'financial_events': Dataset('financial_events', 'Финансовые изменения', 'history', {'project_id': 'id', 'team_id': 'id', 'entity_id': 'id', 'entity_type': 'text', 'event_kind': 'text', 'time_precision': 'text'}, EVENT_COUNT,
        ('created_at', 'updated_at', 'event_at', 'recorded_at'), 'recorded_at'),
    'project_activity': Dataset('project_activity', 'Активность проектов', 'history', {'project_id': 'id', 'team_id': 'id', 'entity_id': 'id', 'entity_type': 'text', 'event_kind': 'text', 'time_precision': 'text'}, EVENT_COUNT,
        ('created_at', 'updated_at', 'event_at', 'recorded_at'), 'recorded_at'),
    'cashflow': Dataset('cashflow', 'Денежный поток', 'cashflow', COMMON,
        {'received_amount': ('SUM(v.received_amount)', 'money'), 'payment_count': ('SUM(v.payment_count)', 'count')}, ('payment_date',), 'payment_date', {'payment': 'payment_date'}),
    'commitment_performance': Dataset('commitment_performance', 'Исполнение обязательств', 'commitments', {**COMMON, 'status': 'text', 'responsible_manager': 'text', 'team': 'text'},
        {**COMMITMENT_MEASURES, 'duration_days': ('AVG(v.duration_days)', 'number'), 'overdue_days': ('AVG(v.overdue_days)', 'number'), 'on_time_percent': ('ROUND(AVG(v.on_time)*100,2)', 'percent')},
        ('created_at', 'updated_at', 'promised_at', 'effective_deadline', 'fulfilled_at'), 'effective_deadline'),
    'plan_fact_monthly': Dataset('plan_fact_monthly', 'План и факт по месяцам', 'plan_fact', {'team_id': 'id', 'profile_id': 'id'},
        {'target_amount': ('CASE WHEN COUNT(DISTINCT ROW(v.team_id,v.profile_id)) FILTER (WHERE v.target_amount IS NOT NULL)=COUNT(DISTINCT ROW(v.team_id,v.profile_id)) THEN SUM(v.target_amount) END', 'money'), 'received_amount': ('SUM(v.received_amount)', 'money')}, ('month',), 'month', {}, 'Месячный план и факт после раздельной агрегации по стабильным ID. NULL не становится нулём.'),
    'source_coverage': Dataset('source_coverage', 'Покрытие источников', 'coverage', {'team_id': 'id', 'source_scope': 'text'},
        {'gap_count': ('SUM(v.gap_count)', 'count'), 'scope_count': COUNT}, ('created_at', 'updated_at', 'complete_through'), None),
}
# One physical company row, while public joins always use company_id.
COMPANY_FIELDS = {'company_id': ('id', 'id')}
FIELDS = {name: ds.fields() for name, ds in CATALOG.items()}
FIELDS['companies'].update(COMPANY_FIELDS)
MEASURES = {name: {key: expression[1] for key, expression in ds.measures.items()} for name, ds in CATALOG.items()}


def describe(name, detailed=False):
    ds = CATALOG[name]
    result = {'label': ds.label, 'grain': 'one authorized entity/event before SQL aggregation',
        'measures': MEASURES[name], 'date_axes': {key: TIME_LABELS[key] for key in ds.dates},
        'default_date_axis': ds.default_date, 'definition': ds.definition or ds.label,
        'dimensions': list(ds.dimensions), 'joins': [key for key in ds.dimensions if key.endswith('_id')],
        'filters': list(ds.dimensions), 'catalog_version': VERSION}
    if detailed:
        result['dimensions'] = list(FIELDS[name])
    return result
