"""Audited replacement of unsent projections. Call only under the source lock."""
from decimal import Decimal
from django.db.models import Q,Sum
from django.utils import timezone
from .models import AuditEvent,Commitment,CrmDelivery,FactCandidate,FactEvent,FinancialRecord,OutboxEvent,PaymentAllocation,PaymentScheduleItem,Project,ProjectParty
from .providers import ProviderUnavailable

LABELS={
 'source_revision_changed':'Получена более новая редакция оригинала; старый снимок не публикуется повторно.',
 'skipped_crm_delivered':'Сообщение уже использовано в подтверждённой доставке CRM; повторный AI не требуется.',
 'blocked_delivery':'Имеется начатая, частичная или неопределённая доставка CRM; замена результатов заблокирована.',
 'deduplication_ambiguous':'Не удалось доказать, что сообщение экспорта и сохранённое сообщение являются одним оригиналом.',
 'projection_changed':'Локальные данные изменились после принятия факта; автоматическая замена не может сохранить все изменения.',
 'projection_snapshot_missing':'Для старого принятого факта нет достаточной истории локальной проекции; она сохранена.'}


def candidate_scope(ids):
    return FactCandidate.objects.filter(Q(trace__raw_message_id__in=ids)|Q(evidence__raw_message_id__in=ids)).distinct()


def delivery_guard(ids,*,lock=False):
    candidates=candidate_scope(ids)
    deliveries=CrmDelivery.objects.filter(fact_event__decision__candidate__in=candidates)
    if lock:
        # Claiming a delivery also locks its outbox row. A pending row cannot
        # become processing between this check and cancellation/replacement.
        list(OutboxEvent.objects.select_for_update().filter(crmdelivery__in=deliveries).order_by('id'))
        deliveries=deliveries.select_for_update(of=('self',))
    rows=list(deliveries.select_related('external_object_link','outbox_event'))
    if any(row.state=='delivered' and row.external_object_link.external_id for row in rows):return 'skipped_crm_delivered'
    if any(row.state not in ('pending','failed','superseded') or row.outbox_event.state in ('processing','unknown') for row in rows):return 'blocked_delivery'
    projects=list(candidates.filter(status='approved').exclude(project=None).values_list('project_id',flat=True))
    legacy=OutboxEvent.objects.filter(event_type='crm_sync',payload__project_id__in=projects,state__in=['processing','unknown','done'])
    if lock:list(legacy.select_for_update().order_by('id'))
    if legacy.exists():return 'blocked_delivery'
    return None


def row_snapshot(row):
    from .facts import json_value
    return json_value({field.attname:getattr(row,field.attname) for field in row._meta.concrete_fields}) if row else {}


def projection_guard(ids):
    for candidate in FactCandidate.objects.filter(trace__raw_message_id__in=ids,status='approved').order_by('-id'):
        saved=candidate.materialization_snapshot
        payment=FinancialRecord.objects.filter(candidate=candidate,is_verified=True).first()
        if payment and (payment.reverses_id or payment.financialrecord_set.exclude(event_kind='reanalysis_reversal').exists()):
            return 'projection_changed'
        if candidate.fact_type=='project' and not saved:return 'projection_snapshot_missing'
        task_id=saved.get('commitment_after',{}).get('id')
        if task_id:
            task=Commitment.objects.filter(pk=task_id).first()
            if not task or task.version!=saved['commitment_after']['version']:return 'projection_changed'
        elif hasattr(candidate,'accepted_commitment') and candidate.accepted_commitment.version!=1:return 'projection_changed'
        after=saved.get('project_after',{});before=saved.get('project_before',{})
        if candidate.project_id and candidate.fact_type=='project' and after:
            project=Project.objects.get(pk=candidate.project_id)
            for name in ('contract_amount','cost_amount','contract_known','cost_confirmed','current_action','next_action','status','currency'):
                if before.get(name)!=after.get(name) and getattr(project,name)!=project._meta.get_field(name).to_python(after.get(name)):return 'projection_changed'
    return None


def guard(raw,*,lock=False):
    if raw.processing_state in ('superseded','deleted'):return 'source_revision_changed'
    if raw.processing_state=='deduplication_ambiguous' or raw.raw_payload.get('deduplication_ambiguous'):return 'deduplication_ambiguous'
    return delivery_guard([raw.id],lock=lock) or projection_guard([raw.id])


def _restore(row,before,after,fields):
    for name in fields:
        if name not in before:continue
        field=row._meta.get_field(name)
        if field.to_python(before[name])==field.to_python(after.get(name)):continue
        if getattr(row,name)!=field.to_python(after.get(name)):raise ProviderUnavailable('projection_changed')
        setattr(row,name,field.to_python(before[name]))


def replace_unsent(ids,trace):
    code=delivery_guard(ids,lock=True) or projection_guard(ids)
    if code:raise ProviderUnavailable(code)
    old=list(FactCandidate.objects.select_for_update(of=('self',)).filter(trace__raw_message_id__in=ids,status__in=['pending','rejected','approved']).exclude(trace=trace).order_by('-id'))
    projects=set()
    for candidate in old:
        events=FactEvent.objects.filter(decision__candidate=candidate)
        deliveries=CrmDelivery.objects.filter(fact_event__in=events)
        OutboxEvent.objects.filter(crmdelivery__in=deliveries,state__in=['pending','enqueued','failed']).update(state='cancelled',error_code='replay_superseded',lease_until=None)
        deliveries.update(state='superseded')
        if candidate.status=='approved':
            saved=candidate.materialization_snapshot
            if candidate.project_id:
                project=Project.objects.select_for_update().get(pk=candidate.project_id);projects.add(project.id)
                if candidate.fact_type=='project':
                    _restore(project,saved.get('project_before',{}),saved.get('project_after',{}),('contract_amount','cost_amount','contract_known','cost_confirmed','current_action','next_action','status','currency','whatsapp_fields'))
                    other=FactCandidate.objects.filter(project=project,status='approved').exclude(pk__in=[row.id for row in old]).exists()
                    if not other:
                        for name in ('is_verified','identity_confirmed'):
                            if name in saved.get('project_before',{}):setattr(project,name,saved['project_before'][name])
                    project.version+=1;project.save()
                # Cancel legacy unsent patches for this accepted version too.
                version=saved.get('project_after',{}).get('version')
                if version:OutboxEvent.objects.filter(event_type='crm_sync',payload__project_id=project.id,payload__version=version,state__in=['pending','enqueued','failed']).update(state='cancelled',error_code='replay_superseded')
            payment=FinancialRecord.objects.filter(candidate=candidate).first()
            if payment and payment.is_verified:
                reversal,created=FinancialRecord.objects.get_or_create(source_key=f'replay-reversal:{candidate.id}',defaults={'project_id':payment.project_id,'reverses':payment,'amount':-payment.amount,'currency':payment.currency,'payment_date':payment.payment_date,'direction':payment.direction,'amount_precision':payment.amount_precision,'credited_profile':payment.credited_profile,'is_verified':True,'status':'received','event_kind':'reanalysis_reversal','notes':'Неотправленный факт заменён полным анализом; оригинал сохранён для аудита.'})
                if created:
                    for allocation in payment.allocations.all():PaymentAllocation.objects.create(financial_record=reversal,schedule_item=allocation.schedule_item,amount=-allocation.amount)
                projects.add(payment.project_id)
            task_id=saved.get('commitment_after',{}).get('id')
            task=Commitment.objects.select_for_update().filter(pk=task_id).first() if task_id else Commitment.objects.select_for_update().filter(candidate=candidate).first()
            if task:
                before=saved.get('commitment_before',{})
                if before:
                    _restore(task,before,saved['commitment_after'],('status','fulfilled_at','deadline_at','deadline','deadline_precision','postponed_reason'))
                else:task.is_verified=False;task.status='cancelled'
                task.version+=1;task.save()
            PaymentScheduleItem.objects.filter(fact_event__in=events).update(state='superseded',is_verified=False)
            ProjectParty.objects.filter(decision__candidate=candidate).delete()
        events.update(is_superseded=True)
        candidate.status='superseded';candidate.save(update_fields=['status'])
        AuditEvent.objects.create(target_type='FactCandidate',target_id=candidate.id,action='replay_superseded',before_after={'replacement_trace_id':trace.id})
    for project in Project.objects.select_for_update().filter(pk__in=projects):
        project.paid_amount=project.financial_records.filter(is_verified=True,status='received',direction='income',amount_precision='exact').aggregate(value=Sum('amount'))['value'] or Decimal(0)
        project.version+=1;project.save()
    return old
