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
            if control['action'] == 'assignee':
                from api.models import UserProfile
                UserProfile.objects.filter(phone='79990000002').update(bitrix_user_id=None)
            if control['action'] == 'bounded':
                from api.processing_attempts import reserve_attempt
                from django.db import transaction
                with transaction.atomic():
                    raw = RawMessage.objects.select_for_update().get(message_id='continuous-east')
                    trace = reserve_attempt(raw, 'continuous-bounded:'+control['nonce'])
                    trace.context_metadata['full_history_policy'] = None
                    trace.context_metadata['analysis_limits']['analysis_input_token_limit'] = 4096
                    trace.save(update_fields=['context_metadata'])
                    OutboxEvent.objects.create(event_type='extract_message', deduplication_key='continuous-bounded:'+control['nonce'], payload={'raw_id':raw.id,'trace_id':trace.id,'batch_ids':[raw.id],'reanalyze':True,'analysis_policy':'history-packets-v1'})
                    state = json.loads((settings.E2E_DIR/'provider-state.json').read_text())
                    state.update(bounded_schema_error_once=True, bounded_target_id=raw.id)
                    temporary = settings.E2E_DIR/'continuous-provider.tmp'
                    temporary.write_text(json.dumps(state))
                    temporary.chmod(0o666)
                    temporary.replace(settings.E2E_DIR/'provider-state.json')
            applied.write_text(json.dumps(control))
    proof = {'messages': list(RawMessage.objects.filter(chat_id__startswith='continuous-').values('id','message_id','processed','processing_state')),
             'events': list(OutboxEvent.objects.values('id','event_type','state','error_code','payload')),
             'deliveries': list(CrmDelivery.objects.values('id','state','patch','outbox_event_id')),
             'routing': list(ThreadRoutingAttempt.objects.values('raw_message_id','outcome','selected_thread_id'))}
    from api.models import CrmCatalogSync, BitrixSettings
    proof['catalog'] = list(CrmCatalogSync.objects.values('generation','state'))
    proof['hourly_at'] = BitrixSettings.objects.first().last_hourly_sync_at
    from api.models import MessageProcessingTrace
    proof['bounded_traces'] = [{'id':t.id,'status':t.status,'error_code':t.error_code,
                               'full_history_policy':t.context_metadata.get('full_history_policy','missing')}
                              for t in MessageProcessingTrace.objects.filter(operation_key__startswith='continuous-bounded:')]
    temporary = settings.E2E_DIR/'continuous-proof.tmp'
    temporary.write_text(json.dumps(proof,default=str))
    temporary.replace(settings.E2E_DIR/'continuous-proof.json')
