"""Durable analytical conversations, access checked for every persisted snapshot."""
from datetime import timedelta
from decimal import Decimal
from copy import deepcopy
from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, Throttled
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from .. import access
from ..models import AnalyticsConversation, AnalyticsTurn, AnalyticsArtifact, AsyncOperation, OutboxEvent
from ..security import Conflict
from ..views import Filters
from .answers import answer
from .data import QUERY_SCHEMA, validate, fail
from .presentation import build
from .combine import combine

ERRORS = {
    'parent_unavailable': 'Уточнение остановлено: исходный запрос не завершился. Выберите готовый результат и повторите просьбу.',
    'cancelled': 'Запрос отменён.', 'access_or_lifetime_changed': 'Срок хранения результата истёк или доступ изменился. Выполните расчёт заново.',
}


def require_analyst(user):
    if access.is_client(user) or not user.is_active:
        raise PermissionDenied()


def usable(artifact, user):
    return artifact.expires_at > timezone.now() and artifact.access_fingerprint == access.fingerprint(user)


def serialized_turn(turn, user):
    op = turn.operation
    state = turn.state if turn.state == 'succeeded' else op.status if op else turn.state
    artifacts = list(turn.artifacts.all())
    available = bool(op and op.access_fingerprint == access.fingerprint(user)) and all(usable(a, user) for a in artifacts)
    if state == 'succeeded' and not artifacts:
        available = available and turn.created_at > timezone.now() - timedelta(hours=24)
    if state == 'succeeded' and not available:
        state = 'expired'
    return {'id': turn.id, 'sequence': turn.sequence, 'parent_turn_id': turn.parent_turn_id,
            'operation_id': turn.operation_id, 'user_text': turn.user_text, 'state': state,
            'answer_document': turn.answer_document if state == 'succeeded' else None,
            'error': ERRORS.get(op.error_code if op else '', 'Не удалось выполнить запрос. Уточните условия и попробуйте ещё раз.') if state in ['failed', 'cancelled', 'expired'] else '',
            'artifacts': [{'id': a.id, 'parent_artifact_id': a.parent_artifact_id, 'title': a.presentation.get('title', 'Результат'),
                           'available': usable(a, user), 'generated_at': a.generated_at} for a in artifacts],
            'created_at': turn.created_at}


def conversation_for(user, pk):
    require_analyst(user)
    return get_object_or_404(AnalyticsConversation, pk=pk, owner=user, updated_at__gte=timezone.now()-timedelta(days=30))


class ConversationList(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_analyst(request.user)
        AnalyticsConversation.objects.filter(owner=request.user, updated_at__lt=timezone.now()-timedelta(days=30)).delete()
        return Response(list(AnalyticsConversation.objects.filter(owner=request.user).order_by('-updated_at').values('id', 'title', 'updated_at')[:50]))

    def post(self, request):
        require_analyst(request.user)
        schema = Filters(data=request.data)
        schema.is_valid(raise_exception=True)
        conversation = AnalyticsConversation.objects.create(owner=request.user, default_scope=schema.validated_data)
        return Response({'id': conversation.id, 'title': conversation.title}, status=201)


class ConversationDetail(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        conversation = conversation_for(request.user, pk)
        turns = conversation.turns.select_related('operation').prefetch_related('artifacts')
        return Response({'id': conversation.id, 'title': conversation.title, 'default_scope': conversation.default_scope,
                         'turns': [serialized_turn(t, request.user) for t in turns]})


class QueryPatch(serializers.Serializer):
    date_axis = serializers.CharField(max_length=64, required=False)
    grouping = serializers.ChoiceField(choices=['day', 'week', 'month', 'quarter', 'year', 'manager', 'status', 'project'], required=False)
    sort = serializers.ChoiceField(choices=['date_asc', 'date_desc', 'value_asc', 'value_desc'], required=False)


class TurnInput(Filters):
    period = serializers.ChoiceField(choices=['this_month', 'last_month', 'quarter', 'year'], required=False)
    currency = serializers.ChoiceField(choices=['KZT', 'USD', 'EUR', 'RUB'], required=False)
    prompt = serializers.CharField(max_length=4000)
    idempotency_key = serializers.CharField(max_length=64)
    parent_turn_id = serializers.IntegerField(min_value=1, required=False)
    artifact_id = serializers.IntegerField(min_value=1, required=False)
    patch = QueryPatch(required=False)
    clear_filters = serializers.ListField(child=serializers.ChoiceField(choices=['team_id', 'manager_id', 'project_id']), required=False, max_length=3)


class TurnList(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        schema = TurnInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = dict(schema.validated_data)
        with transaction.atomic():
            type(request.user).objects.select_for_update().get(pk=request.user.pk)
            conversation = conversation_for(request.user, pk)
            conversation = AnalyticsConversation.objects.select_for_update().get(pk=conversation.id)
            existing = AnalyticsTurn.objects.filter(conversation=conversation, operation__idempotency_key=values['idempotency_key']).select_related('operation').first()
            if existing:
                if existing.operation.request.get('input') != values:
                    raise Conflict('Этот ключ уже использован для другого запроса.')
                return Response({'turn_id': existing.id, 'operation_id': existing.operation_id, 'status': existing.operation.status}, status=202)
            if AsyncOperation.objects.filter(requested_by=request.user, idempotency_key=values['idempotency_key']).exists():
                raise Conflict('Этот ключ уже использован в другом диалоге.')
            if AsyncOperation.objects.filter(requested_by=request.user, status__in=['queued', 'running']).count() >= 5:
                raise Throttled(wait=15)
            parent = None
            artifact = None
            if values.get('parent_turn_id'):
                parent = get_object_or_404(conversation.turns.select_related('operation'), pk=values['parent_turn_id'])
            if values.get('artifact_id'):
                artifact = get_object_or_404(AnalyticsArtifact.objects.select_related('turn__operation'), pk=values['artifact_id'], turn__conversation=conversation)
                if not usable(artifact, request.user):
                    # Recalculation uses the normalized plan, never expired numerical snapshots.
                    if artifact.access_fingerprint != access.fingerprint(request.user):
                        raise Conflict('Доступ изменился. Начните новый расчёт с текущими фильтрами.')
                if parent and artifact.turn_id != parent.id:
                    raise Conflict('Уточнение и результат относятся к разным сообщениям.')
                parent = artifact.turn
            if values.get('patch') and not parent:
                raise Conflict('Сначала выберите результат для изменения.')
            explicit_scope = {k: v for k, v in values.items() if k in ['period', 'currency', 'team_id', 'manager_id', 'project_id']}
            scope = {**conversation.default_scope, **explicit_scope}
            if parent:
                if parent.operation is None:
                    raise Conflict('Исходный запрос недоступен. Начните новый расчёт.')
                if parent.operation.access_fingerprint != access.fingerprint(request.user):
                    raise Conflict('Доступ изменился. Начните новый расчёт.')
                if parent.state != 'succeeded' and parent.operation.status in ['failed', 'cancelled', 'expired']:
                    raise Conflict('Исходный запрос недоступен. Выберите готовый результат.')
                scope = {**parent.effective_scope, **explicit_scope}
            for key in values.get('clear_filters', []):
                scope.pop(key, None)
            sequence = (conversation.turns.order_by('-sequence').values_list('sequence', flat=True).first() or 0) + 1
            op = AsyncOperation.objects.create(requested_by=request.user, operation_type='chat',
                request={**scope, 'prompt': values['prompt'], 'idempotency_key': values['idempotency_key'],
                         'input': values, 'dialogue': True, 'artifact_id': artifact.id if artifact else None},
                idempotency_key=values['idempotency_key'], expires_at=timezone.now()+timedelta(hours=1), access_fingerprint=access.fingerprint(request.user))
            turn = AnalyticsTurn.objects.create(conversation=conversation, parent_turn=parent, operation=op, sequence=sequence,
                user_text=values['prompt'], effective_scope=scope, resolved_intent={'kind': 'refine' if parent else 'new_analysis', 'patch': values.get('patch', {})})
            if not parent or parent.state == 'succeeded' or parent.operation.status == 'succeeded':
                enqueue(op)
            conversation.title = conversation.title if sequence > 1 else values['prompt'][:200]
            conversation.last_activity_at = timezone.now()
            conversation.save(update_fields=['title', 'last_activity_at'])
        return Response({'turn_id': turn.id, 'operation_id': op.id, 'status': op.status}, status=202)


class ArtifactDetail(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        require_analyst(request.user)
        artifact = get_object_or_404(AnalyticsArtifact, pk=pk, turn__conversation__owner=request.user,
                                    turn__conversation__updated_at__gte=timezone.now()-timedelta(days=30))
        if not usable(artifact, request.user):
            return Response({'id': artifact.id, 'available': False, 'message': ERRORS['access_or_lifetime_changed']})
        return Response({'id': artifact.id, 'available': True, 'presentation': artifact.presentation, 'query_plan': artifact.query_plan,
                         'parent_artifact_id': artifact.parent_artifact_id, 'expires_at': artifact.expires_at})


def enqueue(op):
    OutboxEvent.objects.get_or_create(event_type='operation', deduplication_key=f'operation:{op.id}', defaults={'payload': {'operation_id': op.id}})


def settle(operation_id):
    """Publish dependent work only once its explicit parent is terminal."""
    with transaction.atomic():
        turn = AnalyticsTurn.objects.select_related('operation').filter(operation_id=operation_id).first()
        if not turn:
            return
        if turn.state != 'succeeded':
            turn.state = turn.operation.status
            turn.save(update_fields=['state'])
        for child in AnalyticsTurn.objects.select_related('operation').filter(parent_turn=turn, operation__status='queued'):
            if turn.state == 'succeeded':
                enqueue(child.operation)
            elif turn.state in ['failed', 'cancelled', 'expired']:
                AsyncOperation.objects.filter(pk=child.operation_id, status='queued').update(status='cancelled', error_code='parent_unavailable')
                settle(child.operation_id)


def prepare(context, operation):
    turn = AnalyticsTurn.objects.select_related('parent_turn__operation').get(operation=operation)
    history = []
    ancestor = turn.parent_turn
    chain = []
    while ancestor and len(chain) < 4:
        if ancestor.operation.access_fingerprint != context.fingerprint or ancestor.state != 'succeeded':
            fail('parent_unavailable')
        chain.append(ancestor)
        ancestor = ancestor.parent_turn
    for item in reversed(chain):
        previous = item.answer_document
        history.extend([{'role': 'user', 'content': item.user_text},
                        {'role': 'assistant', 'content': previous.get('markdown', '') if previous.get('kind') in ['text', 'clarification'] and not previous.get('facts') else 'Предыдущий запрос выполнен. Числа нужно заново получить из инструментов.'}])
    artifact = None
    if operation.request.get('artifact_id'):
        artifact = AnalyticsArtifact.objects.get(pk=operation.request['artifact_id'], turn__conversation=turn.conversation)
    elif turn.parent_turn:
        artifact = next((a for item in chain if (a := item.artifacts.order_by('-id').first())), None)
    if artifact:
        explicit = operation.request['input']
        changed = set(explicit) & {'team_id', 'manager_id', 'project_id'} | set(explicit.get('clear_filters', []))
        def update_query(query):
            if query.get('dataset') == 'combined':
                query['sources'] = [update_query(q) for q in query['sources']]
            query['filters'] = [f for f in query.get('filters', []) if f['field'] not in changed]
            if 'period' in explicit:
                query.pop('date_range', None)
            if 'currency' in explicit:
                query.pop('currency', None)
            return query
        plan = deepcopy(artifact.query_plan)
        for block in plan['blocks']:
            block['query'] = update_query(block['query'])
        artifact.query_plan = plan
        history.append({'role': 'user', 'content': 'Контекст выбранного результата (только план; источники заново проверить): ' + __import__('json').dumps(artifact.query_plan, ensure_ascii=False)})
    return turn, artifact, history


def query_spec(query):
    return {k: v for k, v in query.items() if k in QUERY_SCHEMA['properties'] and v is not None}


def recalculate(context, plan, patch):
    def calculate(query):
        if query.get('dataset') == 'combined':
            children = [calculate(q) for q in query['sources']]
            transform = {**query['transformation'], 'dataset_ids': [ds['dataset_id'] for ds in children]}
            ds = combine(context, transform)
            if patch.get('sort'):
                field = next((c['name'] for c in ds['columns'] if c['type'] == 'date'), None) if patch['sort'].startswith('date') else next((c['name'] for c in ds['columns'] if c['type'] in ['money', 'count']), None)
                if not field:
                    fail('unsupported_query')
                kind = next(c['type'] for c in ds['columns'] if c['name'] == field)
                present = [r for r in ds['rows'] if r[field] is not None]
                absent = [r for r in ds['rows'] if r[field] is None]
                present.sort(key=lambda r: Decimal(str(r[field])) if kind in ['money', 'count'] else r[field], reverse=patch['sort'].endswith('desc'))
                ds['rows'] = present + absent
                ds['normalized_query']['order_by'] = [{'field': field, 'direction': patch['sort'].rsplit('_', 1)[-1]}]
            return ds
        if query.get('dataset') == 'commitment_records':
            arguments = {k: v for k, v in query.items() if k in ['overdue_only', 'limit', 'group_by']}
            if patch.get('grouping'):
                group = {'manager': 'responsible', 'project': 'project', 'status': 'status', 'day': 'deadline_day'}.get(patch['grouping'])
                if not group:
                    fail('unsupported_query')
                arguments['group_by'] = group
            ds = context.records(arguments)
            if patch.get('sort'):
                field = arguments.get('group_by') if patch['sort'].startswith('date') else 'commitment_count'
                if field not in {c['name'] for c in ds['columns']}:
                    fail('unsupported_query')
                ds['rows'].sort(key=lambda row: (row[field] is None, row[field] if row[field] is not None else ''), reverse=patch['sort'].endswith('desc'))
            return ds
        spec = query_spec(query)
        dims = list(spec['dimensions'])
        group = patch.get('grouping')
        source = spec['dataset']
        from .catalog import FIELDS, CATALOG
        dated = lambda d: FIELDS[source].get(d, ('', ''))[1] == 'date'
        if patch.get('date_axis'):
            if patch['date_axis'] not in CATALOG[source].dates: fail('unsupported_query')
            spec['date_axis'] = patch['date_axis']
            grain = next((d.rsplit('_',1)[-1] for d in dims if dated(d) and d.rsplit('_',1)[-1] in ['day','week','month','quarter','year']), 'month')
            dims = [d for d in dims if not dated(d)]
            dims.insert(0, f"{spec['date_axis']}_{grain}")
            spec['order_by'] = [{**order, 'field':dims[0]} if dated(order['field']) else order
                                for order in spec.get('order_by',[])]
        if group in ['day', 'week', 'month', 'quarter', 'year']:
            if source in ['targets', 'plan_fact_monthly'] and group != 'month': fail('plan_requires_months')
            axis = spec.get('date_axis') or CATALOG[source].default_date or 'source_created_at'
            if axis not in CATALOG[source].dates: fail('unsupported_query')
            spec['date_axis'] = axis
            if 'grain' in spec: spec['grain'] = group
            dims = [d for d in dims if not dated(d)]
            dims.insert(0, 'month' if axis == 'month' and group == 'month' else f'{axis}_{group}')
        elif group:
            mapping = {'manager': {'payments': 'credited_manager', 'projects': 'project_manager', 'crm_projects': 'crm_manager', 'commitments': 'responsible_manager', 'targets': 'manager'}, 'status': {'crm_projects': 'status', 'projects': 'status', 'commitments': 'status'}, 'project': {'payments': 'project_id', 'projects': 'project_id', 'crm_projects': 'project_id', 'commitments': 'project_id'}}
            replacement = mapping.get(group, {}).get(source)
            if not replacement:
                fail('unsupported_query')
            dims = [replacement]
            spec.pop('grain',None)
        spec['dimensions'] = dims
        sort = patch.get('sort')
        if sort:
            field = next((d for d in dims if dated(d)), None) if sort.startswith('date') else spec['measures'][0]
            if not field:
                fail('unsupported_query')
            spec['order_by'] = [{'field': field, 'direction': sort.rsplit('_', 1)[-1]}]
        elif group:
            spec.pop('order_by', None)
        validate(QUERY_SCHEMA, spec)
        return context.query(spec)
    blocks = []
    for i, item in enumerate(plan['blocks']):
        ds = calculate(item['query'])
        block = {**item['block'], 'dataset_id': ds['dataset_id']}
        dimensions = [c['name'] for c in ds['columns'] if c['type'] in ['date', 'text', 'id']]
        if block['kind'] == 'table':
            block['columns'] = [c['name'] for c in ds['columns']]
        elif block['kind'] in ['bar', 'line', 'area', 'donut', 'funnel']:
            block['encoding'] = {'category': dimensions[0], 'value': next(c['name'] for c in ds['columns'] if c['type'] in ['money', 'count', 'percent'])}
            if len(dimensions) > 1:
                block['encoding']['series'] = dimensions[1]
                if block['kind'] in ['donut', 'funnel']:
                    block['kind'] = 'bar'
            if patch.get('grouping') == 'manager' or patch.get('sort') == 'date_desc':
                block['kind'] = 'bar'
        blocks.append(block)
    title=plan['title']
    if title.startswith('Количество') and len(blocks)==1:
        from .answers import column_label
        title=' / '.join(column_label(m,ds['normalized_query']['dataset']) for m in ds['normalized_query'].get('measures',[]))
        dated_column=next((c['name'] for c in ds['columns'] if c['type']=='date'),None)
        if dated_column: title += {'month':' по месяцам','week':' по неделям','day':' по дням','quarter':' по кварталам','year':' по годам'}.get(dated_column.rsplit('_',1)[-1],'')
        interval=ds['normalized_query'].get('date_range')
        if interval and interval['start'].endswith('-01-01') and interval['end_exclusive'].endswith('-01-01'): title += ' за '+interval['start'][:4]+' год'
    document = build(context, {'version': '1.0', 'title': title, 'blocks': blocks})
    grounded = answer(context, document)
    return {'text': grounded['markdown'], 'answer_document': grounded, 'presentation': document, 'widget': None, 'quotes': [], 'analytics_trace':context.trace, 'resolved_intent':context.intent}


def persist(operation, result, parent_artifact=None):
    turn = AnalyticsTurn.objects.get(operation=operation)
    doc = result.get('presentation')
    grounded = result.get('answer_document') or {'version': '1.0', 'kind': 'text', 'markdown': result['text'], 'facts': {}, 'artifact_ids': [], 'suggested_actions': []}
    if doc:
        artifact_ids = []
        for block in doc['blocks']:
            single = {**doc, 'title': block.get('title') or doc['title'], 'blocks': [block],
                      'datasets': {block['dataset_id']: doc['datasets'][block['dataset_id']]}}
            plan = {'title': single['title'], 'blocks': [{'block': {k: v for k, v in block.items() if k not in ['dataset_id', 'steps']},
                                                        'query': single['datasets'][block['dataset_id']]['normalized_query']}]}
            artifact = AnalyticsArtifact.objects.create(turn=turn, parent_artifact=parent_artifact, query_plan=plan, presentation=single,
                coverage={pk: ds['coverage'] for pk, ds in single['datasets'].items()}, access_fingerprint=operation.access_fingerprint,
                expires_at=timezone.now()+timedelta(hours=24))
            artifact_ids.append(artifact.id)
        grounded['artifact_ids'] = artifact_ids
        result['artifact_id'] = artifact_ids[0]
        if len(artifact_ids) == 1:
            grounded['suggested_actions'] = [{**action, 'artifact_id': artifact_ids[0]} for action in grounded.get('suggested_actions', [])]
    turn.answer_document = grounded
    turn.save(update_fields=['answer_document'])
    result['answer_document'] = grounded
    result['turn_id'] = turn.id
