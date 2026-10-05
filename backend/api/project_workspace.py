"""Financial completeness and explanations for the project workspace."""

from django.db.models import Case, Count, IntegerField, Q, Sum, Value, When
from rest_framework import serializers

from . import access
from .datamart import ZERO, project_row, scoped_projects
from .models import Project


class ProjectFilters(serializers.Serializer):
    team_id = serializers.IntegerField(min_value=1, required=False)
    manager_id = serializers.IntegerField(min_value=1, required=False)
    project_id = serializers.IntegerField(min_value=1, required=False)
    currency = serializers.ChoiceField(choices=['KZT', 'USD', 'EUR', 'RUB'], default='KZT')
    search = serializers.CharField(max_length=255, required=False, allow_blank=True)
    stage = serializers.ChoiceField(choices=Project.STATUS_CHOICES, required=False)
    group = serializers.ChoiceField(choices=['all', 'with_data', 'without_data'], default='all')
    completeness = serializers.ChoiceField(choices=['all', 'complete', 'partial', 'missing'], default='all')


def workspace_projects(user, filters):
    qs, _ = scoped_projects(user, filters)
    if filters.get('search'):
        qs = qs.filter(Q(name__icontains=filters['search']) | Q(company__name__icontains=filters['search']))
    if filters.get('stage'):
        qs = qs.filter(status=filters['stage'])
    received = Q(financial_records__is_verified=True, financial_records__status='received')
    qs = qs.annotate(workspace_paid_total=Sum('financial_records__amount', filter=received), workspace_payment_count=Count('financial_records', distinct=True, filter=Q(
        financial_records__is_verified=True, financial_records__status='received',
    )))
    with_data = Q(contract_amount__gt=0) | Q(workspace_payment_count__gt=0)
    complete = Q(contract_amount__gt=0, workspace_payment_count__gt=0)
    state = filters['completeness']
    if state == 'complete':
        qs = qs.filter(complete)
    elif state == 'partial':
        qs = qs.filter(with_data & ~complete)
    elif state == 'missing':
        qs = qs.filter(~with_data)
    counts = qs.aggregate(with_data=Count('pk', filter=with_data), without_data=Count('pk', filter=~with_data))
    group = filters['group']
    if group != 'all':
        qs = qs.filter(with_data if group == 'with_data' else ~with_data)
    return qs.annotate(workspace_has_data=Case(When(with_data, then=Value(1)), default=Value(0), output_field=IntegerField())).order_by('-workspace_has_data', 'name', 'id'), counts


def workspace_rows(user, projects):
    """Read explanations in batches, using only explicitly linked accessible sources."""
    projects = list(projects)
    ids = [p.id for p in projects]
    candidates = list(access.candidates_for(user).filter(project_id__in=ids).filter(Q(fact_type='payment') | Q(fact_type='project', proposed_changes__has_key='contract_amount')).select_related('trace__raw_message'))
    source_ids = {c.trace.raw_message_id for c in candidates}
    sources = list(access.messages_for(user).filter(Q(project_id__in=ids) | Q(id__in=source_ids)))
    accessible = {r.id for r in sources}
    by_project = {pk: [] for pk in ids}
    for c in candidates:
        if c.trace.raw_message_id in accessible and c.trace.raw_message.source != 'bitrix':
            by_project[c.project_id].append(c)
    errors = {r.project_id for r in sources if r.project_id in ids and r.processing_state == 'failed'}
    rows = []
    for p in projects:
        row = project_row(p, paid_total=p.workspace_paid_total or ZERO)
        contract = p.contract_amount > 0
        payments = p.workspace_payment_count > 0
        state = 'complete' if contract and payments else 'partial' if contract or payments else 'missing'
        reasons = []
        def reason(code, message):
            reasons.append({'code': code, 'message': message})
        if not contract:
            reason('contract_missing', 'Сумма договора не зарегистрирована.')
        if not payments:
            reason('payments_missing', 'Нет подтверждённых записей о поступлениях. Это не означает, что оплаты не было.')
        if not (contract and payments):
            reason('balance_unknown', 'Остаток не определён: нужны сведения о договоре и поступлениях.')
            linked = by_project[p.id]
            pending = [c for c in linked if c.status == 'pending']
            rejected = [c for c in linked if c.status == 'rejected' and c.review_reason]
            failed = p.id in errors or any(c.trace.status == 'error' for c in linked)
            if pending:
                reason('review_pending', 'Связанные финансовые предложения ожидают проверки.')
            for c in rejected:
                reason('fact_rejected', 'Связанный факт отклонён: ' + c.review_reason)
            if failed:
                reason('processing_failed', 'Обработка связанного сообщения завершилась ошибкой.')
            if not (pending or rejected or failed):
                reason('cause_unknown', 'Конкретная причина отсутствия данных пока не установлена.')
        row.update(contract_known=contract, payments_known=payments, balance_known=contract and payments,
                   data_completeness=state, missing_data_reasons=reasons,
                   review_available=any(c.status == 'pending' for c in by_project[p.id]))
        rows.append(row)
    return rows
