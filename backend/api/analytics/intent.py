"""Explicit user constraints, independently checked against the generated plan."""
import re
from datetime import date
from ..providers import ProviderUnavailable

ENTITIES = {'projects':r'проект', 'crm_projects':r'сделк|\bcrm\b|\bcrm_',
    'commitments':r'обязательств|обещан', 'payments':r'платеж|платёж|поступлен|оплат',
    'messages':r'сообщени', 'companies':r'компани|контрагент'}
COUNTS = {'projects':'project_count','crm_projects':'project_count','commitments':'commitment_count',
    'payments':'payment_count','messages':'message_count','companies':'company_count'}
MONTHS = ('январ', 'феврал', 'март', 'апрел', 'ма[йяе]', 'июн', 'июл',
          'август', 'сентябр', 'октябр', 'ноябр', 'декабр')


def explicit_period(text):
    """Resolve a named calendar range before the less specific calendar year."""
    years = set(re.findall(r'\b(20\d{2})\b', text))
    # ISO endpoints are already the compiler's half-open range contract.
    endpoints = re.findall(r'\b20\d{2}-\d{2}-\d{2}\b', text)
    if len(endpoints) == 2:
        try:
            start, end = map(date.fromisoformat, endpoints)
        except ValueError:
            return None
        if start < end:
            return {'start':start.isoformat(), 'end_exclusive':end.isoformat()}
    if len(years) != 1 or endpoints:
        return None
    year = int(next(iter(years)))
    months = sorted({i + 1 for i, stem in enumerate(MONTHS)
                     if re.search(r'\b' + stem + r'[а-я]*\b', text)})
    if months:
        first, last = months[0], months[-1]
        # Disjoint month selections cannot safely be represented as one interval.
        if len(months) > 2 or (len(months) == 2 and last-first > 1 and not re.search(r'с\s+.*\s+по\s+|[–—-]', text)):
            return None
        return {'start':f'{year}-{first:02d}-01',
                'end_exclusive':f'{year+1}-01-01' if last == 12 else f'{year}-{last+1:02d}-01'}
    quarter = re.search(r'\b([1-4])\s*(?:-?й\s*)?квартал', text)
    if quarter:
        first = (int(quarter[1])-1)*3+1
        return {'start':f'{year}-{first:02d}-01',
                'end_exclusive':f'{year+1}-01-01' if first == 10 else f'{year}-{first+3:02d}-01'}
    if re.search(r'год|году|year|месяц|недел|квартал|created_at|updated_at|(?:за|в)\s+20\d{2}',text):
        return {'start':f'{year}-01-01','end_exclusive':f'{year+1}-01-01'}
    return None


def normalize(prompt):
    text = prompt.lower()
    found = [name for name, pattern in ENTITIES.items() if re.search(pattern, text)]
    if 'crm_projects' in found and 'projects' in found and re.search(r'проект.*crm|crm.*проект',text):
        found.remove('projects')
    intent = {'entities':found, 'chart':bool(re.search(r'график|диаграмм|chart|визуализ',text))}
    period = explicit_period(text)
    if period:
        intent['date_range'] = period
    if len(found)==1:
        name=found[0]
        if not re.search(r'план|два графика|договор|сравни|сравнен',text): intent['dataset']=name
        if re.search(r'количеств|сколько|число|count',text) and not re.search(r'сумм|стоимост|марж|доход|выруч',text):
            intent['measure']=COUNTS[name]
        if 'updated_at' in text: intent['date_axis']='updated_at'
        elif 'created_at' in text or re.search(r'создан.*mazory|встав.*mazory',text): intent['date_axis']='created_at'
        elif name in ('projects','crm_projects') and (intent.get('date_range') or re.search(r'новых|новые|пришло|появил|создан',text)):
            intent['date_axis']='source_created_at'
    for grain,pattern in [('month',r'месяц'),('week',r'недел'),('quarter',r'квартал'),('day',r'по дням|ежеднев|дням')]:
        if re.search(pattern,text): intent['grain']=grain; break
    if re.search(r'изменений|переносов|переходов|история стадий|историю стадий',text):
        intent.pop('dataset',None); intent.pop('measure',None)
        intent['history']=True
    return intent


def validate_query(intent, query):
    if not intent or query.get('dataset') in ('combined','commitment_records'): return
    errors=[]
    dataset=query.get('dataset')
    # Multi-source comparisons may legitimately need companion plans or payments.
    if intent.get('dataset') and len(intent.get('entities',[]))==1 and dataset != intent['dataset']:
        errors.append(f"Запрошена сущность {intent['dataset']}, получена {dataset}.")
    if intent.get('measure') and intent['measure'] not in query.get('measures',[]):
        errors.append(f"Нужен показатель {intent['measure']}; количество нельзя подменять суммой.")
    if intent.get('date_range') and query.get('date_range') != intent['date_range']:
        errors.append(f"Явный период вопроса: {intent['date_range']}; получен {query.get('date_range')}.")
    if intent.get('date_axis') and query.get('date_axis') != intent['date_axis']:
        errors.append(f"Запрошенная ось: {intent['date_axis']}; получена {query.get('date_axis')}.")
    if intent.get('grain') and intent.get('chart'):
        grain = query.get('grain') or next((g for g in ('day','week','month','quarter','year')
            if any(d == g or d.endswith('_'+g) for d in query.get('dimensions',[]))), None)
        if grain != intent['grain']:
            errors.append(f"Запрошена группировка {intent['grain']}; получена {grain}.")
    if intent.get('history') and dataset not in ('entity_events','project_stage_events','commitment_events','financial_events','project_activity'):
        errors.append('Число изменений считается по журналу событий, а не по последнему updated_at.')
    if errors:
        raise ProviderUnavailable('intent_mismatch',diagnostics={'validation_errors':errors})
