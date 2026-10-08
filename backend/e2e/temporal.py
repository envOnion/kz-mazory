"""Synthetic temporal fixtures and PostgreSQL readback for browser E2E."""
from datetime import datetime, timezone as utc
from pathlib import Path
import json
from django.apps import apps
from django.conf import settings
from django.db import connection, transaction, IntegrityError
from api.models import Project, Company, TemporalEntityRevision


def seed():
    projects=list(Project.objects.filter(name__startswith='Аналитический объект').order_by('id'))
    for i,project in enumerate(projects):
        project.source_created_at=datetime(2026,i+1,15,tzinfo=utc.utc)
        project.source_time_precision='exact'
        project.save(update_fields=['source_created_at','source_time_precision'])
    from django.contrib.auth.models import User
    from api.models import UserProfile, TeamMembership
    from api.authentication import create_session
    user=User.objects.create_user('79990000101',password='local-e2e-only')
    UserProfile.objects.create(user=user,phone='79990000101',full_name='Temporal analyst')
    TeamMembership.objects.create(user=user,team=projects[0].team,role='team_lead',status='active')
    _,access,refresh=create_session(user)
    (Path(settings.E2E_DIR)/'temporal-session.json').write_text(json.dumps({'access':access,'refresh':refresh,'cookie_name':settings.AUTH_REFRESH_COOKIE,'user_id':user.id}))
    company=Company.objects.create(name='Temporal timestamp fixture')
    created,updated=company.created_at,company.updated_at
    Company.objects.filter(id=company.id).update(name='Temporal timestamp changed')
    company.refresh_from_db()
    changed=company.updated_at
    Company.objects.filter(id=company.id).update(name=company.name,created_at=datetime(2000,1,1,tzinfo=utc.utc))
    company.refresh_from_db()
    before=TemporalEntityRevision.objects.filter(entity_type='company',entity_id=company.id).count()
    try:
        with transaction.atomic():
            Company.objects.filter(id=company.id).update(name='Rollback must disappear')
            raise ValueError('rollback scenario')
    except ValueError: pass
    company.refresh_from_db()
    bulk=Company.objects.bulk_create([Company(name='Temporal bulk insert')])[0]
    bulk.refresh_from_db()
    bulk.name='Temporal bulk updated'
    Company.objects.bulk_update([bulk],['name'])
    bulk.refresh_from_db()
    with connection.cursor() as c:
        c.execute("SELECT COUNT(*) FROM pg_views WHERE schemaname='analytics'"); views=c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM pg_trigger WHERE tgname='mazory_record_times' AND NOT tgisinternal"); triggers=c.fetchone()[0]
    report={'all_models_timestamped':all({'created_at','updated_at'} <= {f.name for f in m._meta.fields} for m in apps.get_app_config('api').get_models()),
        'views':views,'triggers':triggers,'created_immutable':company.created_at==created,
        'real_update':changed>updated,'noop_unchanged':company.updated_at==changed,
        'rollback_atomic':company.name=='Temporal timestamp changed' and TemporalEntityRevision.objects.filter(entity_type='company',entity_id=company.id).count()==before,
        'bulk_timestamps':bool(bulk.created_at and bulk.updated_at and bulk.updated_at>=bulk.created_at)}
    (Path(settings.E2E_DIR)/'temporal-readback.json').write_text(json.dumps(report))
    from django.db.models import Max
    project=projects[0]
    revision=TemporalEntityRevision.objects.filter(entity_type='project',entity_id=project.id).aggregate(n=Max('revision'))['n'] or 0
    for start in range(0,100000,2000):
        TemporalEntityRevision.objects.bulk_create([TemporalEntityRevision(entity_type='project',entity_id=project.id,
            project=project,team=project.team,revision=revision+i+1,event_kind='changed',time_precision='exact',
            source_key=f'benchmark:{i}',effective_at=datetime(2026,(i%9)+1,15,tzinfo=utc.utc),
            recorded_at=datetime(2026,(i%9)+1,15,tzinfo=utc.utc)) for i in range(start,start+2000)],batch_size=1000)



def benchmark():
    """Runs in the genuine Q2 worker; all SQL uses the production readonly compiler."""
    import time
    from datetime import timedelta
    from django.contrib.auth.models import User
    from api.models import AsyncOperation
    from api import access
    from api.analytics.data import OperationContext
    from api.analytics.catalog import CATALOG
    user=User.objects.get(username='79990000101')
    op=AsyncOperation.objects.create(requested_by=user,operation_type='temporal_e2e',status='running',
        expires_at=__import__('django.utils.timezone',fromlist=['now']).now()+timedelta(minutes=10),
        access_fingerprint=access.fingerprint(user),idempotency_key=f'temporal-e2e:{time.monotonic_ns()}')
    report={}
    try:
        for name,ds in CATALOG.items():
            context=OperationContext(op.id,user.id,op.access_fingerprint,op.expires_at,time.monotonic()+30,{'period':'year','currency':'KZT'})
            axis=ds.default_date or ('created_at' if 'created_at' in ds.dates else None)
            dims=[('month' if axis=='month' else f'{axis}_month')] if axis else []
            spec={'dataset':name,'dimensions':dims,'measures':list(ds.measures)[:1]}
            if axis: spec.update(date_axis=axis,date_range={'start':'2026-01-01','end_exclusive':'2027-01-01'})
            context.query(spec)
            report[name]=context.trace[-1]['elapsed_ms']
        semantic_scenario(user)
        op.status='succeeded'
    except Exception as exc:
        report['error']=str(exc); op.status='failed'
        raise
    finally:
        op.result=report; op.save()
        (Path(settings.E2E_DIR)/'temporal-benchmark.json').write_text(json.dumps(report))


def semantic_scenario(analyst):
    """Real worker writes, trigger readback, scope checks and outbox backfill."""
    from django.contrib.auth.models import User
    from django.utils import timezone
    from decimal import Decimal
    from api.models import (Team, CrmProjectSnapshot, Commitment, FinancialRecord, ProjectRevision, StageTransition)
    from api.temporal import enqueue
    team=Team.objects.create(name=f'Private temporal worker scenario {timezone.now().isoformat()}')
    project=Project.objects.create(team=team,name='Private temporal history',identity_confirmed=True,currency='USD',
        source_created_at=datetime.fromisoformat('2019-12-31T23:30:00+03:00'))
    crm=CrmProjectSnapshot.objects.create(project=project,external_stage_id='NEW',currency='USD',
        source_created_at=project.source_created_at,source_updated_at=datetime(2026,1,1,tzinfo=utc.utc))
    count=lambda kind,pk: TemporalEntityRevision.objects.filter(entity_type=kind,entity_id=pk).count()
    before=count('crmprojectsnapshot',crm.id)
    crm.synced_at=timezone.now(); crm.save(update_fields=['synced_at'])
    noop=count('crmprojectsnapshot',crm.id)==before
    crm.external_stage_id='WON'; crm.source_stage_changed_at=datetime(2026,3,2,tzinfo=utc.utc)
    crm.save(update_fields=['external_stage_id','source_stage_changed_at'])
    stage=TemporalEntityRevision.objects.filter(entity_type='crmprojectsnapshot',entity_id=crm.id).latest('revision')
    task=Commitment.objects.create(team=team,project=project,commitment_text='Synthetic timeline',is_verified=True,
        promised_at=datetime(2026,2,1,tzinfo=utc.utc),deadline_at=datetime(2026,2,5,tzinfo=utc.utc))
    task.deadline_at=datetime(2026,2,8,tzinfo=utc.utc); task.save(update_fields=['deadline_at'])
    postponed=TemporalEntityRevision.objects.filter(entity_type='commitment',entity_id=task.id).latest('revision').event_kind=='postponed'
    task.status='fulfilled'; task.fulfilled_at=datetime(2026,2,7,tzinfo=utc.utc); task.save(update_fields=['status','fulfilled_at'])
    fulfilled=TemporalEntityRevision.objects.filter(entity_type='commitment',entity_id=task.id).latest('revision').effective_at==task.fulfilled_at
    payment=FinancialRecord.objects.create(project=project,amount=Decimal('100'),currency='USD',payment_date='2026-02-01',is_verified=True)
    reversal=FinancialRecord.objects.create(project=project,amount=Decimal('-40'),currency='USD',payment_date='2026-02-02',is_verified=True,reverses=payment)
    correction=TemporalEntityRevision.objects.filter(entity_type='financialrecord',entity_id=reversal.id).latest('revision').event_kind=='correction'
    project.archived=True; project.save(update_fields=['archived'])
    archived=TemporalEntityRevision.objects.filter(entity_type='project',entity_id=project.id).latest('revision').event_kind=='archived'
    immutable=False
    try:
        with transaction.atomic(): TemporalEntityRevision.objects.filter(pk=stage.id).update(event_kind='forbidden')
    except Exception: immutable=True
    # Prior accepted revision is a synthetic legacy fixture. Replay must import it once.
    revision=ProjectRevision.objects.create(project=project,version=99,approved_at=datetime(2026,2,1,tzinfo=utc.utc),snapshot={'status':'completed'})
    StageTransition.objects.create(project=project,project_revision=revision,from_stage='lead',to_stage='completed')
    from api import access
    hidden=not access.projects_for(analyst,include_archived=True).filter(pk=project.id).exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT (source_created_at AT TIME ZONE 'Asia/Almaty')::date FROM analytics.crm_deals WHERE id=%s",[crm.id])
        boundary=cursor.fetchone()[0].isoformat()=='2020-01-01'
    report={'crm_repeat_no_event':noop,'crm_stage_source_time':stage.event_kind=='stage_changed' and stage.effective_at==crm.source_stage_changed_at,
        'commitment_postponed':postponed,'commitment_fulfilled':fulfilled,'financial_correction':correction,
        'archived_history':archived,'append_only':immutable,'foreign_scope_hidden':hidden,'source_timezone_boundary':boundary}
    # Aggregation semantics use real SQL in this worker, then browser readback.
    import time
    from datetime import timedelta
    from api.models import AsyncOperation
    from api.analytics.data import OperationContext
    from api.analytics.combine import combine
    from api.providers import ProviderUnavailable
    admin=User.objects.get(username='79990000001')
    project.contract_known=True; project.contract_amount=Decimal('100'); project.save()
    companion=Project.objects.create(team=team,name='Private average fixture',identity_confirmed=True,
        currency='USD',contract_known=True,contract_amount=Decimal('300'),archived=True)
    validation=AsyncOperation.objects.create(requested_by=admin,operation_type='temporal_e2e',status='running',
        expires_at=timezone.now()+timedelta(minutes=10),access_fingerprint=access.fingerprint(admin),
        idempotency_key=f'temporal-averages:{time.monotonic_ns()}')
    context=OperationContext(validation.id,admin.id,validation.access_fingerprint,validation.expires_at,time.monotonic()+30,{'period':'year','currency':'USD'})
    filters=[{'field':'project_id','op':'in','value':[project.id,companion.id]}]
    averaged=context.query({'dataset':'projects','dimensions':['status'],'measures':['contract_amount'],
        'aggregation':'avg','filters':filters,'date_axis':'created_at',
        'date_range':{'start':'2026-01-01','end_exclusive':'2027-01-01'}})
    report['average_no_false_sum']=not any(v['formula']=='sum' for v in context.facts.values())
    report['average_category_guard']=False
    try:
        combine(context,{'mode':'categories','dataset_ids':[averaged['dataset_id']],'category':'status',
            'groups':{r['status']:'Все стадии' for r in averaged['rows']}})
    except ProviderUnavailable as exc:
        report['average_category_guard']=str(exc)=='combine_requires_aggregation'
    calendar=context.query({'dataset':'projects','dimensions':['created_at_month'],'date_axis':'created_at',
        'measures':['contract_amount'],'aggregation':'avg','filters':filters,
        'date_range':{'start':'2026-01-01','end_exclusive':'2026-03-01'}})
    report['average_empty_unknown']=len(calendar['rows'])==2 and all(r['contract_amount'] is None for r in calendar['rows'])
    validation.status='succeeded'; validation.save(update_fields=['status'])
    op=enqueue(User.objects.get(username='79990000001'))
    report.update(backfill_operation_id=op.id,legacy_revision_id=revision.id)
    (Path(settings.E2E_DIR)/'temporal-semantic.json').write_text(json.dumps(report))


def concurrent_backfill_scenario():
    """A real Q2 chunk races a separate PostgreSQL CRM-like transaction."""
    import threading
    import time
    from datetime import timedelta
    from django.db import close_old_connections, connections
    from django.contrib.auth.models import User
    from django.utils import timezone
    from api.models import Team, AsyncOperation, OutboxEvent, UserProfile, TeamMembership
    from api.authentication import create_session
    from api import access
    from api.temporal import queue
    team=Team.objects.create(name=f'Concurrent temporal E2E {time.monotonic_ns()}')
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('mazory.temporal_backfill','on',true)")
        project=Project.objects.create(team=team,name='Legacy concurrent source fixture',archived=True,currency='USD')
    owner=User.objects.create_user(f'concurrent-{time.monotonic_ns()}')
    UserProfile.objects.create(user=owner,phone=f'fixture-{owner.id}',full_name='Concurrent history observer')
    TeamMembership.objects.create(user=owner,team=team,role='team_lead',status='active')
    _,token,_=create_session(owner)
    (Path(settings.E2E_DIR)/'temporal-concurrent-session.json').write_text(json.dumps({'access':token}))
    op=AsyncOperation.objects.create(requested_by=owner,operation_type='temporal_backfill',
        request={},result={'stage':2,'cursor':project.id-1},
        access_fingerprint=access.fingerprint(owner),expires_at=timezone.now()+timedelta(minutes=10),
        idempotency_key=f'temporal-concurrent:{time.monotonic_ns()}')
    locked=threading.Event()
    def crm_update():
        close_old_connections()
        report={'operation_id':op.id,'project_id':project.id,'overlap_observed':False,'update_committed':False}
        try:
            with transaction.atomic():
                current=Project.objects.select_for_update().get(pk=project.id)
                locked.set()
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND pg_backend_pid()=ANY(pg_blocking_pids(pid)))')
                        if cursor.fetchone()[0]:
                            report['overlap_observed']=True
                            break
                    time.sleep(0.05)
                if not report['overlap_observed']: raise RuntimeError('Backfill never overlapped the locked CRM row')
                current.source_created_at=datetime(2026,2,1,tzinfo=utc.utc)
                current.source_time_precision='exact'
                current.save(update_fields=['source_created_at','source_time_precision'])
            report['update_committed']=True
        except Exception as exc:
            report['error']=type(exc).__name__
        finally:
            connections.close_all()
            target=Path(settings.E2E_DIR)/'temporal-concurrent.json'
            temporary=target.with_suffix('.tmp')
            temporary.write_text(json.dumps(report)); temporary.replace(target)
    writer=threading.Thread(target=crm_update,daemon=True)
    writer.start()
    if not locked.wait(5): raise RuntimeError('CRM row lock unavailable')
    queue(op.id,2,project.id-1)
    # This function itself runs in Q2. Consume the genuine outbox event here so
    # unrelated browser jobs cannot delay the controlled overlap. Later chunks
    # and duplicate delivery still go through the normal publisher and worker.
    from api.tasks import run_outbox
    event=OutboxEvent.objects.get(deduplication_key=f'temporal:{op.id}:2:{project.id-1}')
    run_outbox(event.id)
    writer.join(5)
    if writer.is_alive(): raise RuntimeError('Concurrent writer did not finish')
