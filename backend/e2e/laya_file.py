"""Private inputs are copied into the isolated volume; no facts or themes are seeded."""
import hashlib
import json
import time
import os
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from django_q.tasks import async_task
from api.models import AISettings, WhatsAppConfig, BitrixSettings, RawMessage, OutboxEvent, ThreadRoutingAttempt, MessageProcessingTrace, Team, TeamMembership


def seed(user, team):
    from api.whatsapp_export_parser import parse_export
    oracle = json.loads((settings.E2E_DIR / 'laya-oracle.json').read_text())
    source = (settings.E2E_DIR / 'laya-source.txt').read_bytes()
    if hashlib.sha256(source).hexdigest() != oracle['source_sha256']:
        raise RuntimeError('Source changed')
    records, _ = parse_export(source, oracle['timezone'])
    (settings.E2E_DIR / 'laya-parser.json').write_text(json.dumps({'messages': len(records), 'sha256': hashlib.sha256(source).hexdigest()}))
    configs = {}
    inputs = []
    for index, case in enumerate(oracle['cases']):
        qwen=os.getenv('MAZORY_E2E_MODEL') == 'qwen3.8-27b'
        chat_id=f'qwen-window-{index+1}@g.us' if qwen else f"laya-{case['id']}@g.us"
        title=f'Проверка Qwen {index+1}' if qwen else case['title']
        configs[case['id']] = WhatsAppConfig.objects.get_or_create(team=team, session_name='default',
            group_jid=chat_id, defaults={'name':title, 'thread_routing_enabled':True, 'snapshot':{'timezone': 'Asia/Almaty'}})[0].id
        for item in case['messages']:
            parsed = records[item['ordinal'] - 1]
            if parsed.content != item['content'] or parsed.sender != item['sender'] or parsed.line_start != item['line_start']:
                raise RuntimeError('Independent parser mismatch')
        inputs.append({'id': case['id'], 'title': title, 'chat_id':chat_id, 'messages': [{**item,
            'timestamp': int(records[item['ordinal'] - 1].sent_at.timestamp())} for item in case['messages']]})
    synthetic_team, _ = Team.objects.get_or_create(name='Синтетические проверки LAYA')
    TeamMembership.objects.get_or_create(team=synthetic_team,user=user,role='team_lead',defaults={'status':'active'})
    for name in ('routing','baseline','economy'):
        configs[name] = WhatsAppConfig.objects.get_or_create(team=synthetic_team,session_name='default',group_jid=f'laya-synthetic-{name}@g.us',
            defaults={'name':f'LAYA synthetic {name}','thread_routing_enabled':name!='baseline','snapshot':{'timezone':'Asia/Almaty'}})[0].id
    (settings.E2E_DIR / 'laya-inputs.json').write_text(json.dumps(inputs, ensure_ascii=False))
    cfg = AISettings.get_active()
    cfg.thread_routing_mode = 'laya'
    cfg.autonomous_enabled = True
    cfg.autonomous_crm_enabled = False
    cfg.autonomous_daily_token_limit = 0
    cfg.autonomous_context_messages = 8
    cfg.analysis_target_message_limit = 8
    cfg.context_window_tokens = 65536
    cfg.max_completion_tokens = cfg.analysis_output_token_limit = 8192
    if os.getenv('MAZORY_E2E_MODEL') == 'qwen3.8-27b':
        cfg.context_window_tokens = 49152
        cfg.max_completion_tokens = cfg.analysis_output_token_limit = 32768
        cfg.analysis_target_message_limit = 20
        cfg.autonomous_context_messages = 30
    cfg.thread_extract_idle_seconds = 30
    cfg.save()
    BitrixSettings.objects.update(auto_import_deals=False, crm_matching_enabled=True)
    session = json.loads((settings.E2E_DIR / 'session.json').read_text())
    session.update(configs=configs, user_id=user.id)
    (settings.E2E_DIR / 'laya-session.json').write_text(json.dumps(session))


def dispatch_forever():
    from api.dialogue_threads import source_key, analysis_position
    applied = None
    while True:
        close_old_connections()
        control_path = settings.E2E_DIR / 'laya-control.json'
        control = json.loads(control_path.read_text()) if control_path.exists() else {}
        if control.get('nonce') != applied:
            if control.get('seed_quality') and not (settings.E2E_DIR/'laya-quality-ready.json').exists():
                async_task('e2e.laya_quality.seed')
            if control.get('mode') in ('llm','shadow','laya'):
                AISettings.objects.update(thread_routing_mode=control['mode'])
            if control.get('enable_baseline_threads'):
                WhatsAppConfig.objects.filter(group_jid='laya-synthetic-baseline@g.us').update(thread_routing_enabled=True)
            if control.get('verify_qwen'):
                task_id = async_task('api.tasks.verify_qwen_provider')
                (settings.E2E_DIR/'qwen-canary-task.json').write_text(json.dumps({'id':task_id}))
            if type(control.get('bump_thread')) is int:
                from api.models import DialogueThread
                from django.db.models import F
                DialogueThread.objects.filter(pk=control['bump_thread'],config__group_jid__startswith='laya-synthetic-').update(routing_descriptor_version=F('routing_descriptor_version')+1)
            applied = control.get('nonce')
            (settings.E2E_DIR/'laya-control-applied.json').write_text(json.dumps({'nonce':applied}))
        # The vector index is independent of classification, which considers every prepared topic.
        OutboxEvent.objects.filter(event_type='index_message', state='pending').update(state='done')
        for event in OutboxEvent.objects.filter(state='pending', next_attempt_at__lte=timezone.now()).order_by('id')[:100]:
            if (settings.E2E_DIR / 'laya-pause').exists():
                break
            if event.event_type in ('extract_message', 'route_message', 'extract_thread'):
                raw = RawMessage.objects.select_related('config__team', 'team', 'project').get(pk=event.payload['raw_id'])
                event.analysis_source_key, event.analysis_position = source_key(raw), analysis_position(raw)
                event.save(update_fields=['analysis_source_key', 'analysis_position'])
                if OutboxEvent.objects.filter(analysis_source_key=event.analysis_source_key, state__in=['enqueued','processing']).exclude(pk=event.pk).exists():
                    continue
            if OutboxEvent.objects.filter(pk=event.id, state='pending').update(state='enqueued'):
                async_task('api.tasks.run_outbox', event.id)
        report = {'messages': list(RawMessage.objects.filter(chat_id__regex=r'^(laya-|qwen-window-)').values('id','chat_id','message_id','content','processed','processing_state')),
                  'routing': list(ThreadRoutingAttempt.objects.values('raw_message_id','outcome','reason_code','scores','latency_ms','model_revision')),
                  # Full request envelopes remain in PostgreSQL and the API.
                  # Polling proof needs results, not repeated native JSON schemas.
                  'traces': list(MessageProcessingTrace.objects.filter(raw_message__chat_id__regex=r'^(laya-|qwen-window-)').values('id','raw_message_id','status','error_code','ai_extracted_facts','context_metadata__provider_profile','context_metadata__response_diagnostics','context_metadata__promise_identity','context_metadata__promise_reconciliation')),
                  'events': list(OutboxEvent.objects.values('id','event_type','state','error_code','payload'))}
        from api.models import ProviderUsage
        report['usage'] = list(ProviderUsage.objects.values('outbox_event_id','operation','input_tokens','output_tokens'))
        from api.models import Commitment
        report['commitments'] = list(Commitment.objects.filter(source_message__chat_id__startswith='laya-synthetic-').values('id','responsible_name','commitment_text','status','source_message_id','deadline_at','fulfilled_at','version'))
        canary = settings.E2E_DIR/'qwen-canary-task.json'
        if canary.exists():
            from django_q.models import Task
            task=Task.objects.filter(pk=json.loads(canary.read_text())['id']).first()
            if task:
                (settings.E2E_DIR/'qwen-canary-result.json').write_text(json.dumps({'task_id':task.id,'success':task.success,'result':task.result}, default=str))
        temporary = settings.E2E_DIR / 'laya-proof.tmp'
        temporary.write_text(json.dumps(report, default=str, ensure_ascii=False))
        temporary.replace(settings.E2E_DIR / 'laya-proof.json')
        time.sleep(0.3)
