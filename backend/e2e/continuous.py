"""Controlled provider outage and durable extraction gap in the Docker E2E."""
import json
from django.conf import settings
from django.utils import timezone
from api.models import AISettings, WhatsAppConfig, FactCandidate, RawMessage, OutboxEvent, CrmDelivery, ThreadRoutingAttempt


def update():
    path = settings.E2E_DIR / 'continuous-control.json'
    applied = settings.E2E_DIR / 'continuous-applied.json'
    if path.exists():
        control = json.loads(path.read_text())
        previous = json.loads(applied.read_text()) if applied.exists() else {}
        if previous.get('nonce') != control['nonce']:
            cfg = AISettings.objects.first()
            if control['action'] in ('start', 'routing'):
                provider = settings.E2E_DIR/'provider-state.json'
                state = json.loads(provider.read_text())
                state.pop('thread_window', None)
                state.pop('autonomous', None)
                temporary = settings.E2E_DIR/'continuous-provider.tmp'
                temporary.write_text(json.dumps(state))
                temporary.chmod(0o666)
                temporary.replace(provider)
                source = WhatsAppConfig.objects.first()
                WhatsAppConfig.objects.get_or_create(group_jid=control['group'], defaults={'team_id': source.team_id, 'name': 'Continuous E2E', 'thread_routing_enabled': control['action']=='routing', 'snapshot': {'timezone':'UTC+03:00'}})
                cfg.autonomous_enabled = cfg.autonomous_crm_enabled = True
                cfg.thread_routing_mode = 'laya' if control['action']=='routing' else 'llm'
                # Previous full-history cases deliberately leave a 12k window.
                # This scenario needs the seeded 131k provider window to test
                # a 9k fixed prompt independently of that overflow fixture.
                cfg.context_window_tokens = 131072
                cfg.analysis_input_token_limit = 16384
                cfg.thread_extract_idle_seconds = 120
                cfg.thread_extract_max_wait_seconds = 120
                cfg.save()
                if control['action'] == 'start':
                    from api.authentication import create_session
                    from django.contrib.auth.models import User
                    _, access, refresh = create_session(User.objects.get(username='79990000001'))
                    (settings.E2E_DIR/'continuous-session.json').write_text(json.dumps({'access':access,'refresh':refresh,'cookie_name':settings.AUTH_REFRESH_COOKIE}))
            if control['action'] == 'legacy':
                candidate = FactCandidate.objects.get(pk=control['candidate_id'])
                candidate.trace.model_version = 'retired-fixture-model'
                candidate.trace.save(update_fields=['model_version'])
            if control['action'] == 'gap':
                OutboxEvent.objects.filter(event_type='extract_thread', state='pending').delete()
            if control['action'] in ('gap', 'legacy'):
                OutboxEvent.objects.create(event_type='autonomous_reconcile', deduplication_key='continuous:'+control['nonce'], payload={})
            if control['action'] == 'retry':
                OutboxEvent.objects.filter(error_code='crm_readback_mismatch', state='pending').update(next_attempt_at=timezone.now())
            if control['action'] == 'hourly':
                from api.models import CrmCatalogSync, BitrixSettings
                from api.tasks import enqueue_hourly_bitrix_sync_task
                CrmCatalogSync.objects.update(state='failed')
                BitrixSettings.objects.update(last_hourly_sync_at=None)
                enqueue_hourly_bitrix_sync_task()
                enqueue_hourly_bitrix_sync_task()
            if control['action'] == 'coalesce':
                from api.autonomous import enqueue_periodic_reconcile
                from django.db import transaction
                from datetime import timedelta
                with transaction.atomic():
                    source=WhatsAppConfig.objects.first()
                    benchmark=WhatsAppConfig.objects.create(team_id=source.team_id,group_jid='continuous-checkpoint-benchmark@g.us',name='Checkpoint benchmark',snapshot={'timezone':'UTC+03:00'})
                    RawMessage.objects.bulk_create([RawMessage(config=benchmark,team_id=source.team_id,source='waha',session_name='default',chat_id=benchmark.group_jid,message_id=f'continuous-benchmark-{i}',content=f'Обсуждение помещения {i}. '+ 'Записали размеры стен и помещения. '*30,timestamp=timezone.now()-timedelta(days=30,seconds=i),sent_at_known=True,processed=True,processing_state='no_facts') for i in range(735)])
                    ident=enqueue_periodic_reconcile()
                    OutboxEvent.objects.filter(pk=ident).update(next_attempt_at=timezone.now()+timedelta(minutes=30))
                    duplicate=enqueue_periodic_reconcile()
                (settings.E2E_DIR/'continuous-periodic.json').write_text(json.dumps({'id':ident,'duplicate_id':duplicate}))
            if control['action'] == 'release_coalesce':
                marker=json.loads((settings.E2E_DIR/'continuous-periodic.json').read_text())
                OutboxEvent.objects.filter(pk=marker['id']).update(next_attempt_at=timezone.now())
            if control['action'] == 'assignee':
                from api.models import UserProfile
                UserProfile.objects.filter(phone='79990000002').update(bitrix_user_id=None)
            if control['action'] == 'retry_unbound_task':
                from django.db import transaction
                from api.models import Commitment
                with transaction.atomic():
                    delivery=CrmDelivery.objects.select_for_update().get(pk=control['delivery_id'])
                    event=OutboxEvent.objects.select_for_update().get(pk=delivery.outbox_event_id)
                    assert event.payload['kind']=='task' and delivery.state=='delivered'
                    # Reproduce uncertain external success before the local
                    # transaction persisted its external identity.
                    Commitment.objects.filter(pk=event.payload['local_id']).update(bitrix_task_id=None)
                    delivery.external_object_link.external_id=None
                    delivery.external_object_link.save(update_fields=['external_id'])
                    delivery.state='pending';delivery.error_code=''
                    delivery.save(update_fields=['state','error_code'])
                    event.state='pending';event.attempt_count=0;event.error_code='';event.lease_until=None
                    event.next_attempt_at=timezone.now();event.payload.pop('claim_generation',None)
                    event.save()
            if control['action'] == 'review_next':
                (settings.E2E_DIR/'continuous-review.json').write_text(json.dumps({'message_id':control['message_id']}))
            if control['action'] == 'evidence_source':
                source=WhatsAppConfig.objects.first()
                WhatsAppConfig.objects.get_or_create(group_jid=control['group'],defaults={'team_id':source.team_id,'name':'Evidence boundary','snapshot':{'timezone':'UTC+03:00'}})
            if control['action'] == 'release_review':
                from api.autonomous import enqueue_decision
                raw = RawMessage.objects.get(message_id=control['message_id'])
                for candidate in FactCandidate.objects.filter(trace__raw_message=raw).select_related('trace'):
                    candidate.trace.context_metadata['analysis_limits']['review_before_delivery']=False
                    candidate.trace.save(update_fields=['context_metadata'])
                    enqueue_decision(candidate.id, 'reviewed:'+control['nonce'])
            if control['action'] == 'bounded':
                from api.processing_attempts import reserve_attempt
                from django.db import transaction
                with transaction.atomic():
                    raw = RawMessage.objects.select_for_update().get(message_id='continuous-east')
                    trace = reserve_attempt(raw, 'continuous-bounded:'+control['nonce'])
                    trace.context_metadata['full_history_policy'] = None
                    trace.context_metadata['analysis_limits']['analysis_input_token_limit'] = 16384
                    trace.save(update_fields=['context_metadata'])
                    OutboxEvent.objects.create(event_type='extract_message', deduplication_key='continuous-bounded:'+control['nonce'], payload={'raw_id':raw.id,'trace_id':trace.id,'batch_ids':[raw.id],'reanalyze':True,'analysis_policy':'history-packets-v1'})
                    state = json.loads((settings.E2E_DIR/'provider-state.json').read_text())
                    state.update(bounded_schema_error_once=True, bounded_target_id=raw.id, bounded_token_floor=9000)
                    temporary = settings.E2E_DIR/'continuous-provider.tmp'
                    temporary.write_text(json.dumps(state))
                    temporary.chmod(0o666)
                    temporary.replace(settings.E2E_DIR/'provider-state.json')
            applied.write_text(json.dumps(control))
    review_path=settings.E2E_DIR/'continuous-review.json'
    if review_path.exists():
        from django.db import transaction
        from api.processing_attempts import reserve_attempt
        target=json.loads(review_path.read_text())['message_id']
        with transaction.atomic():
            raw=RawMessage.objects.select_for_update().filter(message_id=target).first()
            event=OutboxEvent.objects.filter(event_type='extract_message',state='pending',payload__raw_id=raw.id).first() if raw else None
            if event:
                trace=reserve_attempt(raw,'continuous-review:'+target)
                trace.context_metadata['full_history_policy']=None
                trace.context_metadata['analysis_limits']['review_before_delivery']=True
                trace.save(update_fields=['context_metadata'])
                event.payload={**event.payload,'trace_id':trace.id,'batch_ids':[raw.id],'analysis_policy':'history-packets-v1'}
                event.save(update_fields=['payload'])
                review_path.unlink()
    proof = {'messages': list(RawMessage.objects.filter(chat_id__startswith='continuous-').values('id','message_id','processed','processing_state')),
             'events': list(OutboxEvent.objects.values('id','event_type','state','error_code','payload')),
        'deliveries': list(CrmDelivery.objects.values('id','state','patch','outbox_event_id','external_object_link__external_id')),
             'routing': list(ThreadRoutingAttempt.objects.values('raw_message_id','outcome','selected_thread_id'))}
    proof['control_nonce']=json.loads(applied.read_text()).get('nonce') if applied.exists() else None
    from api.models import CrmCatalogSync, BitrixSettings
    proof['catalog'] = list(CrmCatalogSync.objects.values('generation','state'))
    proof['hourly_at'] = BitrixSettings.objects.first().last_hourly_sync_at
    periodic=settings.E2E_DIR/'continuous-periodic.json'
    proof['periodic']=json.loads(periodic.read_text()) if periodic.exists() else None
    from api.models import DialogueThread
    proof['multi_threads']=list(DialogueThread.objects.filter(topic='Разные объекты одного отчёта').values('id','project_id'))
    from api.models import MessageProcessingTrace
    proof['topic_labels']=[row for t in MessageProcessingTrace.objects.filter(raw_message__message_id__in=['continuous-long-label','continuous-focused-long-label']) for row in t.context_metadata.get('topic_label_bounds', [])]
    proof['evidence_links']=[{'message_id':t.raw_message.message_id,'status':t.status,'error_code':t.error_code,'full_history_policy':t.context_metadata.get('full_history_policy','missing'),'repairs':t.context_metadata.get('schema_repairs',[]),'ranges':t.context_metadata.get('evidence_ranges',{})} for t in MessageProcessingTrace.objects.filter(raw_message__message_id__in=['continuous-context-evidence','continuous-unavailable-evidence']).select_related('raw_message')]
    proof['bounded_traces'] = [{'id':t.id,'status':t.status,'error_code':t.error_code,
                               'full_history_policy':t.context_metadata.get('full_history_policy','missing')}
                              for t in MessageProcessingTrace.objects.filter(operation_key__startswith='continuous-bounded:')]
    temporary = settings.E2E_DIR/'continuous-proof.tmp'
    temporary.write_text(json.dumps(proof,default=str))
    temporary.replace(settings.E2E_DIR/'continuous-proof.json')
