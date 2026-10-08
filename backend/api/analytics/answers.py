"""Grounded narrative facts: amounts never pass through model arithmetic."""
import re
from decimal import Decimal

LABELS = {
    'event_count': 'Количество событий', 'revision_count': 'Количество версий', 'company_count': 'Количество компаний',
    'duration_days': 'Средний срок исполнения', 'overdue_days': 'Средняя просрочка', 'on_time_percent': 'Исполнено в срок',
    'schedule_count': 'Количество пунктов графика', 'scheduled_amount': 'По графику', 'outstanding_amount': 'Остаток оплаты',
    'target_count': 'Количество планов', 'candidate_count': 'Количество предложений', 'gap_count': 'Пробелы истории', 'scope_count': 'Количество источников',
    'project_count': 'Количество проектов', 'payment_count': 'Количество платежей',
    'received_amount': 'Поступления', 'target_amount': 'План', 'crm_amount': 'Сумма сделок CRM',
    'contract_amount': 'Сумма договоров', 'confirmed_cost': 'Подтверждённая стоимость',
    'contract_margin_percent': 'Расчётная маржа', 'message_count': 'Количество сообщений',
    'commitment_count': 'Количество обязательств', 'overdue_count': 'Просроченные обязательства',
    'project_id': 'Проект', 'status': 'Стадия', 'team': 'Команда', 'project_type': 'Тип проекта',
    'project_status': 'Статус проекта', 'crm_manager': 'Ответственный CRM',
    'credited_manager': 'Менеджер поступления', 'project_manager': 'Менеджер проекта',
    'responsible_manager': 'Ответственный', 'manager': 'Менеджер', 'month': 'Месяц',
    'chat': 'Чат', 'team_id': 'Команда', 'profile_id': 'Сотрудник', 'manager_id': 'Менеджер', 'commitment_id': 'Обязательство', 'text': 'Текст', 'project': 'Проект',
    'responsible': 'Ответственный', 'deadline': 'Срок', 'series': 'Показатель', 'value': 'Значение',
}
for prefix, noun in [('payment', 'оплаты'), ('message', 'сообщения'), ('effective_deadline', 'срока')]:
    for suffix, label in [('day', 'День'), ('week', 'Неделя'), ('month', 'Месяц'), ('quarter', 'Квартал'), ('year', 'Год')]:
        LABELS[f'{prefix}_{suffix}'] = f'{label} {noun}'


def column_label(name, source):
    if name == 'project_count' and source == 'crm_projects':
        return 'Количество сделок'
    return LABELS.get(name, 'Показатель')


def formatted(value, kind, unit=None):
    if value is None:
        return 'нет данных'
    if kind in ['money', 'percent', 'count', 'number']:
        d = Decimal(str(value))
        text = format(d, ',.0f' if kind == 'count' else ',.2f').replace(',', '\u202f').replace('.', ',')
    else:
        text = str(value)
    return text + (' ' + unit if unit else '')


def collect_facts(dataset, rows=None):
    rows = dataset['rows'] if rows is None else rows
    facts = {}
    query = dataset['normalized_query']
    additive = all(q.get('aggregation', 'default') in ('default', 'sum')
                   for q in [query, *query.get('sources', [])])
    if dataset['normalized_query'].get('mode') == 'series':
        if not additive:
            return facts
        labels = list(dict.fromkeys(r['series'] for r in rows))
        totals = []
        col = next(c for c in dataset['columns'] if c['name'] == 'value')
        for index, label in enumerate(labels):
            values = [r['value'] for r in rows if r['series'] == label]
            if values and all(v is not None for v in values):
                total = sum((Decimal(str(v)) for v in values), Decimal(0))
                value = int(total) if col['type'] == 'count' else str(total.quantize(Decimal('.01')))
                facts[f'{dataset["dataset_id"]}:series{index}:total'] = {
                    'dataset_id': dataset['dataset_id'], 'measure': 'value', 'formula': 'sum', 'series': label,
                    'value': value, 'display': formatted(value, col['type'], col['unit']), 'label': label,
                    'row_count': len(values), 'scope': dataset['normalized_query']}
                totals.append((label, total))
        if len(totals) == 2:
            delta = totals[0][1] - totals[1][1]
            value = str(delta.quantize(Decimal('.01')))
            facts[f'{dataset["dataset_id"]}:difference'] = {
                'dataset_id': dataset['dataset_id'], 'measure': 'value', 'formula': 'difference',
                'value': value, 'display': formatted(value, col['type'], col['unit']),
                'label': f'{totals[0][0]} минус {totals[1][0]}', 'scope': dataset['normalized_query']}
        for fact in facts.values():
            fact.update(type=col['type'], unit=col['unit'])
        return facts
    dims = [c['name'] for c in dataset['columns'] if c['type'] in ['text', 'date', 'id']]
    for col in dataset['columns']:
        if col['type'] not in ['count', 'money', 'percent', 'number']:
            continue
        values = [(i, r[col['name']]) for i, r in enumerate(rows) if r[col['name']] is not None]
        name = col['name']
        unknown = len(rows) - len(values)
        if unknown:
            facts[f'{dataset["dataset_id"]}:{name}:unknown'] = {
                'dataset_id': dataset['dataset_id'], 'measure': name, 'formula': 'unknown_count',
                'value': unknown, 'display': formatted(unknown, 'count'), 'type': 'count', 'unit': None,
                'label': f'Группы с неизвестным значением: {col.get("label") or column_label(name, dataset["normalized_query"]["dataset"])}',
                'scope': dataset['normalized_query']}
        if not values:
            continue
        # Unknown components are never turned into a complete total; ratios aren't additive.
        if additive and col['type'] not in ('percent', 'number') and len(values) == len(rows):
            total = sum((Decimal(str(v)) for _, v in values), Decimal(0))
            value = int(total) if col['type'] == 'count' else str(total.quantize(Decimal('.01')))
            facts[f'{dataset["dataset_id"]}:{name}:total'] = {
                'dataset_id': dataset['dataset_id'], 'measure': name, 'formula': 'sum',
                'value': value, 'display': formatted(value, col['type'], col['unit']),
                'label': col.get('label') or column_label(name, dataset['normalized_query']['dataset']),
                'row_count': len(rows), 'scope': dataset['normalized_query'],
                'type': col['type'], 'unit': col['unit'],
            }
        if dims:
            for operation, choose in [('max', max), ('min', min)]:
                index, value = choose(values, key=lambda iv: Decimal(str(iv[1])))
                facts[f'{dataset["dataset_id"]}:{name}:{operation}'] = {
                    'dataset_id': dataset['dataset_id'], 'measure': name, 'formula': operation,
                    'value': value, 'display': formatted(value, col['type'], col['unit']),
                    'label': col.get('label') or column_label(name, dataset['normalized_query']['dataset']),
                    'row_key': {d: rows[index][d] for d in dims}, 'scope': dataset['normalized_query'],
                    'type': col['type'], 'unit': col['unit'],
                }
    return facts


def escape(text):
    return re.sub(r'([\\`*_{}\[\]<>])', r'\\\1', str(text))


def answer(context, document=None, model_text=''):
    used = document['datasets'] if document else context.registry
    facts = {k: v for k, v in getattr(context, "facts", {}).items() if v['dataset_id'] in used}
    if not facts:
        for ds in used.values():
            facts.update(collect_facts(ds))
    text = model_text.strip()
    refs = re.findall(r'\{\{fact:([^}]+)\}\}', text)
    # Prose may contain dates, but all analytical numeric claims must use facts.
    without_refs = re.sub(r'\{\{fact:[^}]+\}\}', '', text)
    without_dates = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', without_refs)
    unsafe = bool(re.search(r'\d', without_dates)) or any(key not in facts for key in refs)
    clarification = bool(used and not document and text and not unsafe and not refs and '?' in text)
    if clarification:
        used, facts = {}, {}
    if used and (not text or unsafe or not refs):
        parts = []
        for ds in used.values():
            ds_facts = [(k, v) for k, v in facts.items() if v['dataset_id'] == ds['dataset_id']]
            totals = [(k, v) for k, v in ds_facts if v['formula'] in ['sum', 'difference']]
            if totals:
                parts.append('По выбранным условиям: ' + '; '.join(f'{escape(v["label"])} — {{{{fact:{k}}}}}' for k, v in totals) + '.')
            maxima = [(k, v) for k, v in ds_facts if v['formula'] == 'max']
            if maxima:
                k, v = maxima[0]
                category = ', '.join(escape(value if value is not None else 'Не назначен') for value in v['row_key'].values())
                prefix = 'Наибольшее из известных значений' if ds['coverage']['status'] == 'partial' else 'Наибольшее значение'
                parts.append(f'{prefix}: {category} — {{{{fact:{k}}}}}.')
            if ds['truncated']:
                parts.append('График показывает выбранный рейтинг; итоги рассчитаны по всей выборке.')
            if not ds['rows']:
                parts.append('По выбранным условиям записей нет. Можно расширить период или снять фильтр.')
            if ds['coverage']['status'] == 'partial':
                parts.append(escape(ds['coverage']['message'].rstrip('.')) + '.')
        text = '\n\n'.join(dict.fromkeys(parts)) or 'Данные подготовлены. Подробности доступны в таблице и условиях расчёта.'
    rendered = re.sub(r'\{\{fact:([^}]+)\}\}', lambda m: escape(facts[m[1]]['display']), text)
    source = next(iter(used.values()))['normalized_query']['dataset'] if used else ''
    for key in LABELS:
        rendered = re.sub(r'(?<![\w])' + re.escape(key) + r'(?![\w])', column_label(key, source), rendered)
    return {'version': '1.0', 'kind': 'clarification' if clarification else 'empty' if used and all(not ds['rows'] for ds in used.values()) else 'analysis' if used else 'text', 'markdown': rendered,
            'template': text, 'facts': facts, 'artifact_ids': [],
            'applied_scope': getattr(context, 'defaults', {}), 'coverage': [ds['coverage'] for ds in used.values()],
            'sources': list(getattr(context, "sources", {}).values()), 'suggested_actions': suggestions(used)}


def suggestions(datasets):
    sources = {ds['normalized_query']['dataset'] for ds in datasets.values()}
    if sources == {'payments'}:
        return [{'label': 'По неделям', 'prompt': 'Разбей выбранные поступления по неделям', 'patch': {'grouping': 'week'}},
                {'label': 'По менеджерам', 'prompt': 'Разбей выбранные поступления по менеджерам', 'patch': {'grouping': 'manager'}},
                {'label': 'Добавить план', 'prompt': 'Добавь месячный план к выбранному графику'}]
    if sources == {'crm_projects'}:
        return [{'label': 'По ответственным', 'prompt': 'Разбей текущие сделки по ответственным CRM', 'patch': {'grouping': 'manager'}},
                {'label': 'По убыванию', 'prompt': 'Отсортируй выбранные сделки по убыванию количества', 'patch': {'sort': 'value_desc'}}]
    return []
