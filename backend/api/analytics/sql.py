"""Read-only SQL compiler over the registered views, with ACLs before aggregation."""
import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import connections, transaction
from django.db.models import Q

from .. import access
from ..datamart import period_bounds
from ..models import (Company, Project, CrmProjectSnapshot, FinancialRecord, PaymentScheduleItem,
    SalesTarget, FactCandidate, BusinessEvent, TemporalEntityRevision, SourceCheckpoint, Team)
from ..providers import ProviderUnavailable
from .catalog import CATALOG, FIELDS, MEASURES, TIME_LABELS, VERSION, GRAINS
from .answers import collect_facts, column_label

logger = logging.getLogger(__name__)
ALIAS = 'analytics_readonly'


def failure(code='unsupported_query', message=''):
    raise ProviderUnavailable(code, diagnostics={'validation_errors': [message]} if message else None)


def projects(user, currency, historical=False):
    return access.projects_for(user, include_archived=historical).using(ALIAS).filter(
        Q(is_verified=True) | Q(identity_confirmed=True) |
        Q(financial_records__is_verified=True, financial_records__status='received',
          financial_records__direction='income', financial_records__amount_precision='exact')
    ).filter(currency=currency).distinct()


def scope(user, ds, currency, historical):
    ps = projects(user, currency, historical)
    if ds.scope == 'projects':
        return ps
    if ds.scope == 'crm_projects':
        return CrmProjectSnapshot.objects.using(ALIAS).filter(project__in=ps, currency=currency)
    if ds.scope == 'payments':
        return FinancialRecord.objects.using(ALIAS).filter(project__in=ps, is_verified=True,
            status='received', direction='income', amount_precision='exact', currency=currency)
    if ds.scope == 'commitments':
        return access.commitments_for(user, include_archived=historical).using(ALIAS).filter(is_verified=True)
    if ds.scope == 'messages':
        return access.messages_for(user).using(ALIAS)
    if ds.scope == 'companies':
        return Company.objects.using(ALIAS).filter(pk__in=ps.values('company_id')) if not user.is_superuser else Company.objects.using(ALIAS).all()
    if ds.scope == 'targets':
        qs = SalesTarget.objects.using(ALIAS).filter(is_active=True, currency=currency, profile__in=access.profiles_for(user))
        return qs if user.is_superuser else qs.filter(team_id__in=access.team_ids(user))
    if ds.scope == 'payment_schedule':
        return PaymentScheduleItem.objects.using(ALIAS).filter(project__in=ps, is_verified=True, state='active', currency=currency)
    if ds.scope == 'fact_review':
        return access.candidates_for(user).using(ALIAS)
    if ds.scope == 'business_events':
        return BusinessEvent.objects.using(ALIAS).filter(project__in=ps)
    if ds.scope == 'history':
        qs = TemporalEntityRevision.objects.using(ALIAS)
        if user.is_superuser:
            return qs
        return qs.filter(Q(project__in=ps) |
            Q(project__isnull=True, entity_type='commitment', entity_id__in=access.commitments_for(user, include_archived=historical).values('id')) |
            Q(project__isnull=True, entity_type='company', entity_id__in=ps.values('company_id')) |
            Q(project__isnull=True, entity_type='salestarget', entity_id__in=scope(user, CATALOG['targets'], currency, historical).values('id')))
    if ds.scope == 'coverage':
        qs = SourceCheckpoint.objects.using(ALIAS)
        return qs if user.is_superuser else qs.filter(team_id__in=access.team_ids(user,['team_lead','finance']))
    return ps  # Cashflow/plan-fact have a separate explicit scope below.


def date_axis(ds, arguments):
    selected = arguments.get('date_axis')
    dated = [name for name in arguments['dimensions'] if FIELDS[arguments['dataset']].get(name, ('', ''))[1] == 'date']
    axes = {FIELDS[arguments['dataset']][name][0] for name in dated}
    if len(axes) > 1:
        failure(message='В одной временной выборке используйте одну ось дат; разные оси сравниваются отдельными сериями.')
    if selected and selected not in ds.dates:
        failure(message=f'Допустимые оси: {list(ds.dates)}')
    if selected and axes and axes != {selected}:
        failure(message='Выбранная ось дат расходится с временной группировкой.')
    return selected or next(iter(axes), None) or ds.default_date


def quote(name):
    return connections[ALIAS].ops.quote_name(name)


def dimension(ds, dataset, name, zone):
    column, kind = FIELDS[dataset][name]
    if kind != 'date':
        return f'v.{quote(column)}', []
    grain = next((g for g in GRAINS if name.endswith('_' + g)), 'month' if column == 'month' else 'day')
    if column in ('payment_date', 'month', 'source_begin_date', 'source_close_date', 'due_date'):
        value, params = f'v.{quote(column)}::timestamp', []
    else:
        value, params = f'v.{quote(column)} AT TIME ZONE %s', [zone]
        if ds.scope == 'history' and column in ('effective_at','event_at'):
            value=f"CASE WHEN v.time_precision='date' THEN v.{quote(column)} AT TIME ZONE 'Asia/Almaty' ELSE {value} END"
    return f'date_trunc(%s, {value})::date', [grain, *params]


def numeric(expression, kind, value):
    if value is None:
        return None
    if kind == 'money':
        return str(Decimal(value).quantize(Decimal('.01')))
    if kind == 'count':
        return int(value)
    if kind in ('percent', 'number'):
        return float(Decimal(value).quantize(Decimal('.01')))
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _where(user, ds, dataset, currency, historical, filters, zone):
    if ds.scope == 'cashflow':
        allowed = projects(user, currency, historical).order_by().values('id')
        sql, params = allowed.query.get_compiler(using=ALIAS).as_sql()
        clauses = [f'v.project_id IN ({sql})', 'v.currency=%s']
        params = [*params, currency]
    elif ds.scope == 'plan_fact':
        profiles = access.profiles_for(user).order_by().values('id')
        sql, params = profiles.query.get_compiler(using=ALIAS).as_sql()
        authorized_sql, authorized_params = projects(user,currency,historical).order_by().values('id').query.get_compiler(using=ALIAS).as_sql()
        clauses = [f'((v.project_id IS NULL AND v.profile_id IN ({sql})) OR (v.project_id IN ({authorized_sql})))', 'v.currency=%s']
        params = [*params,*authorized_params,currency]
        if not user.is_superuser:
            sql, p = access.team_ids(user).query.get_compiler(using=ALIAS).as_sql()
            clauses.append(f'v.team_id IN ({sql})'); params.extend(p)
    else:
        allowed = scope(user, ds, currency, historical).order_by().values('id')
        sql, params = allowed.query.get_compiler(using=ALIAS).as_sql()
        clauses, params = [f'v.id IN ({sql})'], list(params)
    for condition in filters:
        name, op, value = condition['field'], condition['op'], condition['value']
        if name == 'manager_id': name = 'profile_id'
        if name not in ds.dimensions and name not in ds.dates:
            failure(message=f'Не поддерживается фильтр {name} для {dataset}.')
        column, kind = FIELDS[dataset][name]
        values = value if op == 'in' else [value]
        if (op == 'in') != isinstance(value, list): failure('invalid_tool_arguments')
        if kind == 'id' and (op not in ('eq', 'in') or any(type(v) is not int or v < 1 for v in values)):
            failure('invalid_tool_arguments')
        if kind == 'text' and any(not isinstance(v, str) for v in values): failure('invalid_tool_arguments')
        if kind == 'date':
            try:
                values = [date.fromisoformat(v) for v in values]
            except (TypeError, ValueError): failure('invalid_tool_arguments')
        field = f'v.{quote(column)}'
        field_params = []
        if kind == 'date' and column not in ('payment_date','month','due_date','source_begin_date','source_close_date'):
            field = f'({field} AT TIME ZONE %s)::date'
            field_params = [zone]
            if ds.scope == 'history' and column in ('effective_at','event_at'):
                field = f"CASE WHEN v.time_precision='date' THEN (v.{quote(column)} AT TIME ZONE 'Asia/Almaty')::date ELSE {field} END"
        operators = {'eq': '=', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}
        if op == 'in':
            clauses.append(f'{field}=ANY(%s)'); params.extend([*field_params,values])
        else:
            clauses.append(f'{field} {operators[op]} %s'); params.extend([*field_params,values[0]])
    return clauses, params


def query(context, arguments):
    from .data import QUERY_SCHEMA, validate, date_bucket
    from .intent import validate_query
    started=time.monotonic()
    user = context.check()
    validate(QUERY_SCHEMA, arguments)
    context.queries += 1
    if context.queries > 8: failure('query_limit_exceeded')
    dataset = arguments['dataset']; ds = CATALOG[dataset]
    dims, measures = list(arguments['dimensions']), arguments['measures']
    if not set(dims) <= FIELDS[dataset].keys() or not set(measures) <= ds.measures.keys():
        failure(message=f'{dataset}: dimensions={list(FIELDS[dataset])}; measures={list(ds.measures)}')
    axis = date_axis(ds, arguments)
    grain = arguments.get('grain')
    if grain:
        if not axis: failure(message='Укажите date_axis для временной группировки.')
        dated = [d for d in dims if FIELDS[dataset][d][1] == 'date']
        dims = [d for d in dims if d not in dated]
        dims.insert(0, 'month' if axis == 'month' and grain == 'month' else f'{axis}_{grain}')
    if dataset in ('targets', 'plan_fact_monthly') and any(FIELDS[dataset][d][0] == 'month' and not (d == 'month' or d.endswith('_month')) for d in dims):
        failure('plan_requires_months')
    zone = context.zone()
    start, end, today = period_bounds(context.defaults.get('period', 'this_month'), zone)
    if arguments.get('date_range'):
        try:
            start, end = (date.fromisoformat(arguments['date_range'][k]) for k in ('start', 'end_exclusive'))
        except (ValueError, TypeError): failure('invalid_tool_arguments')
        if not axis:
            failure(message='Для временной выборки требуется date_axis: created_at, updated_at или дата источника.')
    if start >= end or (end-start).days > 3660: failure('unsupported_query')
    if dataset in ('targets', 'plan_fact_monthly') and axis == 'month' and (start.day != 1 or end.day != 1):
        failure('plan_requires_months')
    currency = arguments.get('currency', context.defaults.get('currency', 'KZT'))
    filters = list(arguments.get('filters', []))
    for key in ('team_id', 'manager_id', 'project_id'):
        target = 'profile_id' if key == 'manager_id' else key
        if target in ds.dimensions and context.defaults.get(key) and not any(f['field'] in (key, target) for f in filters):
            filters.append({'field': key, 'op': 'eq', 'value': context.defaults[key]})
    normalized = {**arguments, 'dimensions': dims, 'filters': filters, 'date_axis': axis,
        'currency': currency if any(ds.measures[m][1] == 'money' for m in measures) or ds.scope in ('projects', 'payments', 'crm_projects', 'targets', 'cashflow', 'plan_fact', 'payment_schedule') else None,
        'date_range': {'start': start.isoformat(), 'end_exclusive': end.isoformat()} if axis else None,
        'catalog_version': VERSION}
    validate_query(getattr(context, 'intent', {}), normalized)
    if ALIAS not in connections: failure('analytics_not_configured')
    connection = connections[ALIAS]
    if connection.vendor != 'postgresql': failure('analytics_not_configured')
    select, select_params = [], []
    for name in dims:
        sql, params = dimension(ds, dataset, name, zone)
        select.append(f'{sql} AS {quote(name)}'); select_params.extend(params)
    aggregation = arguments.get('aggregation', 'default')
    # Additive metrics retain their registered COUNT/SUM expressions and
    # unknown-component guards rather than losing those rules to raw SUM.
    if aggregation == 'sum' and all(ds.measures[name][1] in ('count', 'money') for name in measures):
        aggregation = 'default'
        normalized['aggregation'] = aggregation
    expressions = []
    for name in measures:
        expression, kind = ds.measures[name]
        if aggregation != 'default':
            if dataset in ('cashflow','plan_fact_monthly') and aggregation != 'sum': failure(message='Предварительно агрегированные витрины поддерживают только суммы.')
            if kind == 'count' or name == 'contract_margin_percent' or kind == 'percent':
                failure(message='Этот показатель имеет собственное правило агрегации.')
            expression = f'{aggregation.upper()}(v.{quote(name)})'
        expressions.append(expression)
        select.append(f'{expression} AS {quote(name)}')
    clauses, params = _where(user, ds, dataset, currency, bool(axis) or ds.scope == 'history', filters, zone)
    base_where = ' AND '.join(clauses)
    base_params = list(params)
    actual_axis = axis in ("created_at","updated_at","source_created_at","source_updated_at","source_stage_changed_at","synced_at","reviewed_at","approved_at","received_at","promised_at","fulfilled_at","payment_date","timestamp","effective_at","event_at","recorded_at")
    effective_end = min(end, today+timedelta(days=1)) if actual_axis else end
    unknown_dates = 0
    evidence = []
    try:
        with transaction.atomic(using=ALIAS), connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute("SELECT set_config('statement_timeout',%s,true)", [str(max(1,min(5000,int((context.deadline-time.monotonic())*1000))))])
            if axis:
                unknown_clause = f'v.{quote(axis)} IS NULL'
                if dataset == 'messages' and axis == 'timestamp': unknown_clause += ' OR NOT v.sent_at_known'
                cursor.execute(f'SELECT COUNT(*) FROM analytics.{quote(ds.view)} v WHERE {base_where} AND ({unknown_clause})', base_params)
                unknown_dates = cursor.fetchone()[0]
                if axis in ('payment_date','month','due_date','source_begin_date','source_close_date'):
                    clauses.extend([f'v.{quote(axis)}>=%s', f'v.{quote(axis)}<%s']); params.extend([start,effective_end])
                else:
                    lower=datetime.combine(start,datetime.min.time(),ZoneInfo(zone))
                    upper=datetime.combine(effective_end,datetime.min.time(),ZoneInfo(zone))
                    if ds.scope=='history' and axis in ('effective_at','event_at'):
                        clauses.append(f"((v.time_precision='date' AND (v.{quote(axis)} AT TIME ZONE 'Asia/Almaty')::date>=%s AND (v.{quote(axis)} AT TIME ZONE 'Asia/Almaty')::date<%s) OR (v.time_precision IS DISTINCT FROM 'date' AND v.{quote(axis)}>=%s AND v.{quote(axis)}<%s))")
                        params.extend([start,effective_end,lower,upper])
                    else:
                        clauses.extend([f'v.{quote(axis)}>=%s', f'v.{quote(axis)}<%s']); params.extend([lower,upper])
                if dataset == 'messages' and axis == 'timestamp': clauses.append('v.sent_at_known')
            where = ' AND '.join(clauses)
            grouped = f'SELECT {", ".join(select)} FROM analytics.{quote(ds.view)} v WHERE {where}'
            if dims: grouped += ' GROUP BY ' + ','.join(str(i+1) for i in range(len(dims)))
            orders = arguments.get('order_by') or ([{'field': d, 'direction': 'asc'} for d in dims if FIELDS[dataset][d][1] == 'date'] or ([{'field': measures[0], 'direction': 'desc'}] if dims else []))
            if any(o['field'] not in dims+measures for o in orders): failure('invalid_tool_arguments')
            cursor.execute(f'SELECT COUNT(*) FROM ({grouped}) AS grouped', [*select_params,*params])
            total_groups = cursor.fetchone()[0]
            limit = arguments.get('limit',1000)
            if total_groups > limit and not arguments.get('top_n',False): failure('dataset_limit_use_filters')
            ordering = ','.join(f'{quote(o["field"])} {o["direction"].upper()} NULLS LAST' for o in orders)
            cursor.execute(grouped + (' ORDER BY ' + ordering if ordering else '') + ' LIMIT %s', [*select_params,*params,limit])
            raw_rows = cursor.fetchall()
            cursor.execute(f'SELECT {", ".join(expressions)} FROM analytics.{quote(ds.view)} v WHERE {where}', params)
            totals = cursor.fetchone()
            if ds.scope in ('history','projects','payments','commitments','messages'):
                # Sample only contributing, already scoped rows, then recheck
                # message permissions. This is evidence, never a second total.
                cursor.execute(f'SELECT v.id FROM analytics.{quote(ds.view)} v WHERE {where} ORDER BY v.id DESC LIMIT 20', params)
                contributing_ids = [row[0] for row in cursor.fetchall()]
                if ds.scope == 'history':
                    source_ids = TemporalEntityRevision.objects.using(ALIAS).filter(pk__in=contributing_ids).values('raw_message_id')
                elif ds.scope == 'commitments':
                    source_ids = access.commitments_for(user, include_archived=bool(axis)).using(ALIAS).filter(pk__in=contributing_ids).values('source_message_id')
                elif ds.scope == 'payments':
                    source_ids = FinancialRecord.objects.using(ALIAS).filter(pk__in=contributing_ids).values('candidate__trace__raw_message_id')
                elif ds.scope == 'projects':
                    source_ids = FactCandidate.objects.using(ALIAS).filter(project_id__in=contributing_ids,status='approved').values('trace__raw_message_id')
                else:
                    source_ids = contributing_ids
                evidence = list(access.messages_for(user).using(ALIAS).filter(id__in=source_ids).order_by('-id').values('id','sender_name')[:20])
            coverage_teams = Team.objects.using(ALIAS).all() if user.is_superuser else Team.objects.using(ALIAS).filter(pk__in=access.team_ids(user))
            for condition in filters:
                if condition['field'] == 'team_id':
                    coverage_teams = coverage_teams.filter(pk__in=condition['value'] if condition['op']=='in' else [condition['value']])
            starts = list(coverage_teams.values_list('history_complete_from',flat=True))
            complete = not unknown_dates and bool(starts) and all(v and v<=start for v in starts) and dataset in ('payments','messages','cashflow')
            if axis in ('created_at','updated_at'): complete = not unknown_dates
            if SourceCheckpoint.objects.using(ALIAS).filter(team__in=coverage_teams).exclude(gaps=[]).exists(): complete=False
    except ProviderUnavailable: raise
    except Exception:
        logger.exception('analytics_sql_failed dataset=%s operation_id=%s',dataset,context.operation_id)
        failure('analytics_query_failed')
    columns = [{'name': d,'type': FIELDS[dataset][d][1],'unit': None,
        'label': TIME_LABELS.get(FIELDS[dataset][d][0],column_label(d,dataset)) if FIELDS[dataset][d][1]=='date' else column_label(d,dataset),
        'source': dataset,'semantic_role':'dimension'} for d in dims]
    columns += [{'name': m,'type': ds.measures[m][1],'unit': currency if ds.measures[m][1]=='money' else '%' if ds.measures[m][1]=='percent' else 'дней' if m.endswith('_days') else None,
        'label':{'avg':'Среднее: ', 'min':'Минимум: ', 'max':'Максимум: '}.get(aggregation,'') + column_label(m,dataset),'source':dataset,'semantic_role':'measure'} for m in measures]
    rows = [{c['name']:numeric('',c['type'],value) for c,value in zip(columns,row)} for row in raw_rows]
    if len(dims)==1 and FIELDS[dataset][dims[0]][1]=='date' and not arguments.get('top_n'):
        dimension_name=dims[0]; existing={row[dimension_name]:row for row in rows}; calendar=[]; day=start
        while day<end:
            bucket=date_bucket(day,dimension_name)
            if bucket not in calendar: calendar.append(bucket)
            day+=timedelta(days=1)
        if len(calendar)>1000: failure('dataset_limit_use_filters')
        rows=[existing.get(bucket,{dimension_name:bucket,**{m:('0.00' if ds.measures[m][1]=='money' else 0) if aggregation=='default' and complete and date.fromisoformat(bucket)<=today and ds.measures[m][1] in ('count','money') and dataset not in ('targets','plan_fact_monthly') else None for m in measures}}) for bucket in calendar]
        if orders and orders[0]['direction']=='desc': rows.reverse()
        total_groups=len(rows)
    normalized['order_by']=orders
    notes=[]
    if unknown_dates: notes.append(f'У {unknown_dates} записей неизвестна выбранная дата; они исключены из временной выборки.')
    if axis and end>today+timedelta(days=1): notes.append(f'Данные по состоянию на {today.isoformat()}; будущие интервалы неизвестны.')
    if axis and not complete: notes.append('Полнота истории по выбранной оси не подтверждена.')
    if not axis: notes.append('Текущий срез доступных сущностей.')
    output={'dataset_id':str(uuid.uuid4()),'columns':columns,'rows':rows,'normalized_query':normalized,'timezone':zone,
        'effective_end_exclusive':min(end,today+timedelta(days=1)).isoformat() if axis else None,
        'coverage':{'status':'complete' if complete and end<=today+timedelta(days=1) else 'partial','message':' '.join(notes) or 'Полная известная выборка.',
            'unknown_dates':unknown_dates,'date_axis':axis,'date_axes':{key:TIME_LABELS[key] for key in ds.dates},'future_values':'unknown'},
        'returned_count':len(rows),'total_groups':total_groups,'truncated':total_groups>len(rows),
        'definition':ds.definition or ds.label,'evidence':evidence}
    if len(json.dumps(output,ensure_ascii=False).encode())>256*1024: failure('dataset_limit_use_filters')
    context.facts.update(collect_facts(output))
    if total_groups>len(rows) and totals:
        summary={m:numeric('',ds.measures[m][1],v) for m,v in zip(measures,totals)}
        summary_dataset={**output,'columns':[c for c in columns if c['semantic_role']=='measure'],'rows':[summary]}
        context.facts = {k:v for k,v in context.facts.items() if v.get('dataset_id') != output['dataset_id'] or v['formula'] not in ('min','max')}
        context.facts.update({k:{**v,'row_count':total_groups} for k,v in collect_facts(summary_dataset).items() if v['formula']=='sum'})
    context.registry[output['dataset_id']]=output
    context.trace.append({'tool':'query_dataset','query':normalized,'rows':len(rows),'unknown_dates':unknown_dates,'elapsed_ms':round((time.monotonic()-started)*1000,2)})
    context.check()
    return output
