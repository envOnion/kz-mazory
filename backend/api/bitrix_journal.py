"""Durable provider mutation receipts and read-only historical reconstruction."""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

from django.conf import settings
from django.db import connections
from django.db.models import Q
from django.utils import timezone

from .models import BitrixDealChangeLog, CrmDelivery, Project, Commitment

WRITES = {
    'crm.deal.add': ('deal', 'create'), 'crm.deal.update': ('deal', 'update'),
    'crm.deal.delete': ('deal', 'delete'),
    'crm.company.add': ('company', 'add'), 'crm.company.update': ('company', 'change'),
    'crm.company.delete': ('company', 'delete'),
    'crm.timeline.comment.add': ('timeline', 'add'),
    'crm.timeline.comment.update': ('timeline', 'change'),
    'crm.timeline.comment.delete': ('timeline', 'delete'),
    'tasks.task.add': ('task', 'add'), 'tasks.task.update': ('task', 'change'),
    'tasks.task.delete': ('task', 'delete'), 'tasks.task.complete': ('task', 'complete'),
    'tasks.task.defer': ('task', 'defer'),
}
_context = ContextVar('bitrix_journal_context', default={})


@contextmanager
def project_context(project_id, origin):
    token = _context.set({'project_id': project_id, 'origin': origin})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def delivery_context(delivery):
    project_id = delivery.fact_event.project_id if delivery.fact_event_id else None
    token = _context.set({'delivery_id': delivery.id, 'project_id': project_id,
                          'origin': f'autonomous_delivery:{delivery.id}'})
    try:
        yield
    finally:
        _context.reset(token)


def _safe(value):
    if isinstance(value, dict):
        return {key: '[скрыто]' if any(secret in key.casefold() for secret in
                ('token', 'secret', 'password', 'webhook', 'authorization')) else _safe(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def mutation_call(method, params, request):
    """No GET/LIST/no-op receipts. Never store URL, credentials or exceptions."""
    kind, action = WRITES[method]
    context = _context.get()
    external = str(params.get('taskId') or params.get('id') or '')
    project_id = context.get('project_id')
    deal_id = external if kind == 'deal' else str(params.get('fields', {}).get('ENTITY_ID') or '') if kind == 'timeline' else ''
    if not project_id and deal_id:
        project_id = Project.objects.filter(bitrix_id=deal_id).values_list('id', flat=True).first()
    start = monotonic()
    manager = BitrixDealChangeLog.objects.using('bitrix_journal')
    try:
        # Insert intent BEFORE the external call; failed journal persistence
        # must never allow an untracked outbound mutation.
        row = manager.create(journal_key='request:'+uuid.uuid4().hex, object_type=kind,
            external_id=external, bitrix_deal_id=deal_id, method=method, action=action,
            status='sending', payload=_safe(params), changed_fields=list(params.get('fields', {})),
            source_project_id=project_id, delivery_id=context.get('delivery_id'),
            triggered_by=context.get('origin', 'bitrix_worker'),
            response_data={'environment': settings.MAZORY_ENV})
        try:
            result = request()
        except Exception as exc:
            response = getattr(exc, 'response', None)
            code = getattr(response, 'status_code', None)
            state = 'error' if (code and 400 <= code < 500) or getattr(exc, 'code', None) == 'crm_rejected' else 'unknown'
            manager.filter(pk=row.pk).update(status=state,
                error_message=getattr(exc, 'code', None) or type(exc).__name__,
                duration_ms=round((monotonic()-start)*1000))
            raise
        value = result.get('result')
        if method.endswith('.add'):
            ident = value.get('task', {}).get('id') if kind == 'task' and isinstance(value, dict) else value
            if isinstance(ident, (str, int)) and not isinstance(ident, bool):
                external = str(ident)
        manager.filter(pk=row.pk).update(status='accepted', external_id=external,
            bitrix_deal_id=external if kind == 'deal' else deal_id,
            response_data={'environment': settings.MAZORY_ENV, 'provider_result': _safe(value)},
            duration_ms=round((monotonic()-start)*1000))
        return result
    finally:
        connections['bitrix_journal'].close()


def reconstruct_history():
    """Q2 only. Never invokes a provider mutation or resends a delivery."""
    from .bitrix_service import BitrixService
    result = {'confirmed': 0, 'unavailable': 0, 'skipped': 0}
    deals, tasks, companies, timelines = {}, {}, {}, {}
    rows = CrmDelivery.objects.filter(Q(state__in=['delivered', 'partial']) |
        Q(error_code='crm_stage_mapping_missing')).select_related(
        'external_object_link', 'fact_event__project').order_by('id')
    for delivery in rows.iterator():
        key = f'history-delivery:{delivery.id}'
        if BitrixDealChangeLog.objects.filter(journal_key=key).exclude(status='unavailable').exists() or BitrixDealChangeLog.objects.filter(
                delivery_id=delivery.id, reconstructed=False, status__in=['accepted', 'success']).exists():
            result['skipped'] += 1
            continue
        link = delivery.external_object_link
        ident = link.external_id
        if not ident:
            continue
        project = delivery.fact_event.project if delivery.fact_event_id else None
        deal_id = ident if link.object_type == 'deal' else project.bitrix_id if project else ''
        proof, error = {}, ''
        try:
            if link.object_type == 'deal':
                if ident not in deals:
                    deals[ident] = BitrixService.call('crm.deal.get', {'id':ident}).get('result', {})
                proof = {k:deals[ident].get(k) for k in ['ID','TITLE','ORIGINATOR_ID','ORIGIN_ID','DATE_CREATE']}
                assert str(proof.get('ID')) == ident
            elif link.object_type == 'task':
                if ident not in tasks:
                    tasks[ident] = BitrixService.call('tasks.task.get', {'taskId':int(ident)}).get('result', {}).get('task', {})
                task = Commitment.objects.get(pk=link.local_id)
                assert str(tasks[ident].get('id')) == ident and tasks[ident].get('title','').startswith(f'[MAZORY-TASK:{task.team_id}:{task.id}]')
                proof = {k:tasks[ident].get(k) for k in ['id','title','responsibleId','ufCrmTask']}
            elif link.object_type == 'timeline':
                assert deal_id
                if deal_id not in timelines:
                    found = []
                    for page in range(10):
                        response = BitrixService.call('crm.timeline.comment.list', {'filter':{'ENTITY_ID':deal_id,'ENTITY_TYPE':'deal'},'order':{'ID':'DESC'},'start':page*50})
                        found.extend(response.get('result', []))
                        if not response.get('next'):
                            break
                    else:
                        raise ValueError('incomplete_readback')
                    timelines[deal_id] = found
                proof = next((r for r in timelines[deal_id] if str(r.get('ID')) == ident), {})
                assert proof and f'[MAZORY-EVENT:{delivery.fact_event_id}]' in proof.get('COMMENT','')
            elif link.object_type == 'company':
                if ident not in companies:
                    companies[ident] = BitrixService.call('crm.company.get', {'id':ident}).get('result', {})
                proof = {k:companies[ident].get(k) for k in ['ID','TITLE','ORIGINATOR_ID','ORIGIN_ID']}
                assert str(proof.get('ID')) == ident
            else:
                continue
        except Exception as exc:
            error = type(exc).__name__
        state = 'unavailable' if error else 'confirmed'
        row, created = BitrixDealChangeLog.objects.update_or_create(journal_key=key, defaults={
            'project':project,'source_project_id':project.id if project else None,
            'delivery_id':delivery.id,'object_type':link.object_type,'external_id':ident,
            'bitrix_deal_id':deal_id or '', 'action':'restored','status':state,
            'reconstructed':True,'payload':delivery.patch,
            'triggered_by':f'history_delivery:{delivery.id}', 'error_message':error,
            'response_data':{'readback_at':timezone.now().isoformat(),'proof':_safe(proof),
                'delivery_state':delivery.state, 'delivery_error':delivery.error_code,
                'environment':settings.MAZORY_ENV, 'timestamp_source':'CrmDelivery.updated_at',
                'note':'Восстановленная команда. Объект проверен отдельно. Команда могла завершиться без изменения Bitrix; факт, метод и число старых запросов записи неизвестны. Payload — снимок команды, а не отправленные поля. Время события — последнее локальное обновление доставки.'}})
        BitrixDealChangeLog.objects.filter(pk=row.pk).update(created_at=delivery.updated_at)
        result[state] += 1
    from .bitrix_journal_audit import reconstruct_audit_history
    audit_result = reconstruct_audit_history()
    for state, count in audit_result.items():
        result[state] += count
    return result
