"""Transactional provenance and bounded, replay-safe history recovery."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import logging
import time
from django.apps import apps
from django.db import connection, transaction, OperationalError
from django.utils import timezone
from .models import (AsyncOperation, OutboxEvent, TemporalEntityRevision, ProjectRevision,
                     StageTransition, FactCandidate, FactEvent, Project)

logger = logging.getLogger(__name__)

DOMAIN = ('company','project','crmprojectsnapshot','commitment','financialrecord','paymentscheduleitem','paymentallocation','salestarget')


@contextmanager
def provenance(actor_id=None, raw_message_id=None, fact_event_id=None):
    if not connection.in_atomic_block: raise RuntimeError('Provenance requires a transaction')
    values={'mazory.actor_id':actor_id,'mazory.raw_message_id':raw_message_id,'mazory.fact_event_id':fact_event_id}
    with connection.cursor() as cursor:
        old={}
        for key,value in values.items():
            cursor.execute('SELECT current_setting(%s,true)',[key]); old[key]=cursor.fetchone()[0] or ''
            cursor.execute('SELECT set_config(%s,%s,true)',[key,str(value) if value else ''])
    try: yield
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                for key,value in old.items(): cursor.execute('SELECT set_config(%s,%s,true)',[key,value])


def enqueue(user):
    if not user.is_active or not user.is_superuser: raise PermissionError('Administrator required')
    with transaction.atomic():
        existing=AsyncOperation.objects.filter(operation_type='temporal_backfill',status__in=['queued','running']).first()
        if existing: return existing
        from django.db.migrations.recorder import MigrationRecorder
        cutoff=MigrationRecorder.Migration.objects.get(app='api',name='0050_temporal_sql_views').applied
        op=AsyncOperation.objects.create(requested_by=user,operation_type='temporal_backfill',request={'legacy_cutoff':cutoff.isoformat()},
            expires_at=timezone.now()+timedelta(days=7),idempotency_key=f'temporal:{timezone.now().isoformat()}')
        queue(op.id,0,0)
        return op


def queue(op_id,stage,cursor):
    OutboxEvent.objects.get_or_create(deduplication_key=f'temporal:{op_id}:{stage}:{cursor}',
        defaults={'event_type':'temporal_backfill','payload':{'operation_id':op_id,'stage':stage,'cursor':cursor}})


def _backfill(payload):
    from .migrations._temporal_v1 import HISTORY_FIELDS
    with transaction.atomic():
        op=AsyncOperation.objects.select_for_update().get(pk=payload['operation_id'])
        if op.status in ('succeeded','cancelled','failed'): return
        stage=payload['stage']; cursor=payload['cursor']
        if op.result.get('stage',stage)!=stage or op.result.get('cursor',cursor)!=cursor: return
        op.status='running'
        # Recover prior accepted project revisions and transitions before adding baselines.
        if stage==0:
            records=list(ProjectRevision.objects.filter(id__gt=cursor,approved_at__lte=op.request['legacy_cutoff']).select_related('project').order_by('id')[:200])
            for revision in records:
                # UPDATE obtains its row lock before the journal trigger's advisory
                # lock. Match that order; deferred project FKs otherwise deadlock.
                project=Project.objects.select_for_update(no_key=True).get(pk=revision.project_id)
                transition=StageTransition.objects.filter(project_revision=revision).first()
                snapshot={k:v for k,v in revision.snapshot.items() if k in HISTORY_FIELDS['project']}
                candidates=list(FactCandidate.objects.filter(project_id=revision.project_id,status='approved',
                    materialization_snapshot__project_after__version=revision.version).select_related('trace__raw_message')[:2])
                candidate=candidates[0] if len(candidates)==1 else None
                event=FactEvent.objects.filter(decision__candidate=candidate,is_superseded=False).order_by('-id').first() if candidate else None
                changes={}
                raw=candidate.trace.raw_message if candidate else None
                effective=event.occurred_at if event else (raw.timestamp if raw and raw.sent_at_known else None)
                if candidate:
                    before=candidate.materialization_snapshot.get('project_before') or {}
                    after=candidate.materialization_snapshot.get('project_after') or {}
                    changes={key:{'before':before.get(key),'after':after.get(key)} for key in HISTORY_FIELDS['project']
                             if before.get(key)!=after.get(key)}
                    # Accepted assertions provide source values, never invented prior values.
                    if event:
                        for assertion in event.fieldassertion_set.all():
                            key='status' if assertion.field_name=='stage' else assertion.field_name
                            if key in HISTORY_FIELDS['project'] and key in changes:
                                changes[key]['after']=assertion.value
                if transition:
                    changes['status']={'before':transition.from_stage,'after':transition.to_stage}
                with connection.cursor() as lock:
                    lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",[f'project:{revision.project_id}'])
                from django.db.models import Max
                seq=(TemporalEntityRevision.objects.filter(entity_type='project',entity_id=revision.project_id).aggregate(value=Max('revision'))['value'] or 0)+1
                TemporalEntityRevision.objects.get_or_create(source_key=f'legacy:project-revision:{revision.id}',defaults={
                    'entity_type':'project','entity_id':revision.project_id,'project_id':revision.project_id,
                    'team_id':project.team_id,'actor_id':revision.approved_by_id,'revision':seq,
                    'raw_message':raw,'fact_event':event,
                    'recorded_at':revision.approved_at,'effective_at':effective,
                    'event_kind':'stage_changed' if transition else 'changed' if changes else 'baseline',
                    'time_precision':'exact' if effective else 'observed',
                    'snapshot':snapshot,'changes':changes})
        else:
            kind=DOMAIN[stage-1]; model=apps.get_model('api',kind)
            records=list(model.objects.filter(id__gt=cursor).order_by('id').values_list('id',flat=True)[:200])
            for pk in records:
                row=model.objects.select_for_update(no_key=True).filter(pk=pk).first()
                if row is None: continue  # A concurrent deletion has its own journal event.
                with connection.cursor() as lock:
                    lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",[f"{kind}:{row.id}"])
                if TemporalEntityRevision.objects.filter(entity_type=kind,entity_id=row.id).exists(): continue
                project=row if kind=='project' else getattr(row,'project',None)
                if kind=='paymentallocation': project=row.financial_record.project
                values={k:getattr(row,k,None) for k in HISTORY_FIELDS[kind]}
                snapshot=json.loads(json.dumps(values,default=str))
                effective=None; precision='observed'; event_kind='baseline'
                if kind=='financialrecord' and row.is_verified and row.status=='received':
                    effective=datetime.combine(row.payment_date,datetime.min.time(),ZoneInfo('Asia/Almaty'))
                    precision='date'; event_kind='correction' if row.reverses_id else 'created'
                elif kind=='commitment' and row.is_verified and row.promised_at:
                    effective=row.promised_at; precision='exact'; event_kind='created'
                TemporalEntityRevision.objects.get_or_create(source_key=f'legacy:baseline:{kind}:{row.id}',defaults={
                    'entity_type':kind,'entity_id':row.id,'revision':1,'project':project,
                    'team_id':getattr(row,'team_id',None) or (project.team_id if project else None),
                    'effective_at':effective,'event_kind':event_kind,'time_precision':precision,
                    'raw_message_id':getattr(row,'source_message_id',None),'snapshot':snapshot,'changes':{}})
        next_cursor=(records[-1].id if stage==0 else records[-1]) if records else 0
        next_stage=stage if len(records)==200 else stage+1
        if next_stage!=stage: next_cursor=0
        op.result={'stage':next_stage,'cursor':next_cursor,'processed':op.result.get('processed',0)+len(records)}
        if next_stage>len(DOMAIN):
            op.status='succeeded'
        else: queue(op.id,next_stage,next_cursor)
        op.save()


def backfill(payload):
    try:
        for attempt in range(3):
            try:
                return _backfill(payload)
            except OperationalError as exc:
                cause=exc.__cause__
                code=getattr(cause,'sqlstate',None) or getattr(cause,'pgcode',None)
                if code not in ('40P01','40001') or attempt==2: raise
                # The whole atomic chunk has rolled back, including its cursor
                # and next outbox event. Retrying preserves exactly-once keys.
                logger.warning('temporal_backfill_retry operation=%s sqlstate=%s attempt=%s',
                               payload['operation_id'],code,attempt+1)
                time.sleep(0.1*(2**attempt))
    except Exception:
        AsyncOperation.objects.filter(pk=payload['operation_id'],status__in=['queued','running']).update(status='failed',error_code='temporal_backfill_failed')
        raise
