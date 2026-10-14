"""Isolated historical fixtures and database proof for WhatsApp price answers."""
import json
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django_q.tasks import async_task

from api.models import (AISettings, BitrixDealChangeLog, ClarificationReply,
    CrmDelivery, FactCandidate, FactClarification, FactDecision, FactEvent,
    FactEvidence, FieldAssertion, FinancialRecord, MessageProcessingTrace,
    NotificationDelivery, OutboxEvent, Project, RawMessage, UserProfile,
    WhatsAppConfig)

GROUP = 'sale-clarifications-e2e@g.us'


def provider_update(change):
    path = settings.E2E_DIR / 'provider-state.json'
    value = json.loads(path.read_text())
    change(value)
    temporary = path.with_suffix('.sale-clarifications.tmp')
    temporary.write_text(json.dumps(value)); temporary.chmod(0o666); temporary.replace(path)


def seed_project(name, external, offers=False):
    """An existing zero-price deal and its original approved history, not a model response."""
    config = WhatsAppConfig.objects.get(group_jid=GROUP)
    project, created = Project.objects.get_or_create(bitrix_id=external, defaults={
        'team_id': config.team_id, 'name': name, 'source': 'chat', 'identity_confirmed': True,
        'currency': 'KZT', 'version': 1, 'whatsapp_fields': ['current_action']})
    if not created:
        return project, None
    content = (f'{name}: подготовлены два варианта: компактная котельная — 18 млн тенге; '
        'расширенная котельная — 20 млн тенге. Вариант заказчик ещё не выбрал.' if offers
        else f'{name}: получено техническое задание, стоимость предложения пока не названа.')
    raw = RawMessage.objects.create(project=project, config=config, team_id=config.team_id,
        source='waha', session_name='default', chat_id=GROUP, message_id=f'sale-question-history-{external}',
        sender_phone='79990000002', sender_name='Боб', timestamp=timezone.now()-timedelta(days=2),
        content=content, processed=True, processing_state='processed')
    trace = MessageProcessingTrace.objects.create(raw_message=raw, project=project,
        whatsapp_message_id=raw.message_id, whatsapp_content=content,
        whatsapp_timestamp=raw.timestamp, model_version=AISettings.get_active().chat_model_name,
        status='success', context_metadata={'fixture': 'existing_approved_history'})
    candidate = FactCandidate.objects.create(trace=trace, project=project, team_id=project.team_id,
        fact_type='project', status='approved', confidence=.98,
        source_key=f'e2e-historical-price:{external}', crm_match_state='matched',
        proposed_changes={'fact_type': 'project', 'object_name': name, 'current_action': content,
            'currency': 'KZT', 'evidence_message_id': raw.id, 'evidence': content})
    FactEvidence.objects.create(candidate=candidate, raw_message=raw, quote=content)
    decision = FactDecision.objects.create(candidate=candidate, outcome='accepted',
        policy_version=AISettings.get_active().autonomous_policy_version,
        input_fingerprint=f'e2e-history-{external}', reason_code='e2e_historical_fixture',
        explanation='Previously accepted source, price not yet known.')
    FactEvent.objects.create(team_id=project.team_id, project=project, decision=decision,
        event_key=f'e2e-history:{external}', event_type='project', occurred_at=raw.timestamp)
    provider_update(lambda value: value.setdefault('deals', {}).update({external: {
        'ID': external, 'TITLE': name, 'OPPORTUNITY': '0.00', 'CURRENCY_ID': 'KZT',
        'STAGE_ID': 'NEW', 'ORIGINATOR_ID': 'MAZORY',
        'ORIGIN_ID': f'mazory:{project.team_id}:project:{project.id}'}}))
    review = None
    if offers:
        from api.message_time import source_time
        review = {'review_key': f'e2e-offers-{external}', 'issue_kind': 'alternative_offers',
            'reason': 'В исходном сообщении два варианта; неизвестно, какой выбран.',
            'evidence': [{'source_id': raw.id, 'quote': content, 'source_date': source_time(raw)[0].isoformat()}],
            'offers': [{'label': label, 'amount': amount, 'currency': 'KZT', 'source_id': raw.id, 'quote': quote}
                for label, amount, quote in [('Компактная котельная', '18000000.00', 'компактная котельная — 18 млн тенге'),
                    ('Расширенная котельная', '20000000.00', 'расширенная котельная — 20 млн тенге')]]}
    return project, review


def prepare(control):
    """Run real CRM reads/planning in Q2, including repeated historical scans."""
    from api.price_questions import plan_project
    if control['action'] in ('history_replay', 'history_first'):
        from api.history_jobs import start_job
        from api.models import WhatsAppHistoryJob
        if control['action'] == 'history_replay':
            reply = ClarificationReply.objects.select_related('raw_message', 'question__config', 'sender').get(provider_message_id=control['message_id'])
            question, payload = reply.question, reply.raw_message.raw_payload['payload']
        else:
            question = FactClarification.objects.select_related('config', 'recipient').get(pk=control['question_id'])
            payload = control['payload']
        provider_update(lambda value: value.update(sale_clarification_history={
            'chat_id': GROUP, 'messages': [payload]}))
        job, _ = WhatsAppHistoryJob.objects.get_or_create(config=question.config, defaults={
            'initial_wait_seconds': 0, 'poll_seconds': 1, 'stable_scans_required': 2,
            'page_size': 1, 'analyze_after_import': True})
        run = start_job(job.id, question.recipient)
        (settings.E2E_DIR/'sale-clarifications-history.json').write_text(json.dumps({'run_id': run.id, 'message_id': payload['id']}))
        return {'run_id': run.id}
    if control['action'] == 'inventory':
        projects = [seed_project(f'БЦ История {i:02}', str(4200+i))[0] for i in range(1, 22)]
    elif control['action'] == 'seed':
        project, review = seed_project(control['name'], control['external_id'], control.get('offers', False))
        if control.get('no_writes'):
            review = {'review_key': 'e2e-preserved-identity-hold', 'no_writes': True,
                'reason': 'Связь с объектом оставлена неподтверждённой; одна цена не подтверждает эту связь.'}
        plan_project(project.id, review=review)
        return {'project_ids': [project.id]}
    else:
        projects = Project.objects.filter(pk__in=control['project_ids'])
    ids = []
    for project in projects:
        plan_project(project.id)
        ids.append(project.id)
    return {'project_ids': ids}


def update():
    path = settings.E2E_DIR / 'sale-clarifications-control.json'
    applied = settings.E2E_DIR / 'sale-clarifications-applied.json'
    if path.exists():
        control = json.loads(path.read_text())
        previous = json.loads(applied.read_text()) if applied.exists() else {}
        if previous.get('nonce') != control['nonce']:
            action = control['action']
            result = {'nonce': control['nonce']}
            if action == 'start':
                from django.contrib.auth.models import User
                from api.authentication import create_session
                config = WhatsAppConfig.objects.first()
                WhatsAppConfig.objects.get_or_create(group_jid=GROUP, defaults={'team_id': config.team_id,
                    'name': 'Sale clarification E2E', 'snapshot': {'timezone': 'Asia/Almaty'}})
                cfg = AISettings.get_active()
                cfg.autonomous_enabled = cfg.autonomous_crm_enabled = True
                cfg.thread_routing_mode = 'llm'; cfg.context_window_tokens = 131072
                cfg.analysis_input_token_limit = 16384; cfg.save()
                user = User.objects.get(username='79990000001')
                UserProfile.objects.filter(user=user).update(notification_preferences={
                    'quiet_start': '00:00', 'quiet_end': '00:00', 'whatsapp': True})
                # Independent fixture scenarios share one recipient's serial inbox.
                FactClarification.objects.filter(kind='sale_amount', state__in=['queued', 'awaiting']).exclude(
                    config__group_jid=GROUP).update(state='unconfirmed')
                _, access, refresh = create_session(user)
                (settings.E2E_DIR/'sale-clarifications-session.json').write_text(json.dumps({
                    'access': access, 'refresh': refresh, 'cookie_name': settings.AUTH_REFRESH_COOKIE}))
                def start(value):
                    value.pop('thread_window', None); value.pop('autonomous', None)
                    value.pop('crm_discard_write', None)
                    value['sale_clarifications'] = True
                provider_update(start)
            if action in ('seed', 'inventory', 'replan', 'history_replay', 'history_first'):
                result['task_id'] = async_task('e2e.sale_clarifications.prepare', control)
            if action == 'pause_reply':
                (settings.E2E_DIR/'sale-clarification-paused').touch()
            if action == 'resume_reply':
                (settings.E2E_DIR/'sale-clarification-paused').unlink(missing_ok=True)
            if action == 'pause_delivery':
                (settings.E2E_DIR/'sale-clarification-delivery-paused').touch()
            if action == 'resume_delivery':
                (settings.E2E_DIR/'sale-clarification-delivery-paused').unlink(missing_ok=True)
            if action == 'discard_write':
                provider_update(lambda value: value.update(crm_discard_write=True))
            if action == 'retry':
                provider_update(lambda value: value.pop('crm_discard_write', None))
                OutboxEvent.objects.filter(state='pending', error_code='crm_readback_mismatch').update(next_attempt_at=timezone.now())
            if action == 'crm_changed':
                project = Project.objects.get(pk=control['project_id'])
                provider_update(lambda value: value['deals'][project.bitrix_id].update(OPPORTUNITY='61000000.00'))
            if action == 'local_changed':
                project = Project.objects.get(pk=control['project_id'])
                project.sale_amount = Decimal('62000000.00'); project.version += 1
                project.whatsapp_fields = list(set(project.whatsapp_fields) | {'sale_amount'})
                project.save(update_fields=['sale_amount', 'version', 'whatsapp_fields'])
            if action == 'finish':
                FactClarification.objects.filter(config__group_jid=GROUP, state__in=['queued', 'awaiting']).update(state='unconfirmed')
                def finish(value):
                    value.pop('sale_clarifications', None); value.pop('sale_clarification_history', None)
                provider_update(finish)
            applied.write_text(json.dumps(result))
    projects = Project.objects.filter(candidates__trace__raw_message__config__group_jid=GROUP).distinct()
    questions = FactClarification.objects.filter(kind='sale_amount', config__group_jid=GROUP)
    from django_q.models import Task
    from api.models import SourceWorkItem, ThreadMessage, WhatsAppHistoryRun
    history_path = settings.E2E_DIR/'sale-clarifications-history.json'
    history = json.loads(history_path.read_text()) if history_path.exists() else None
    reply_ids = ClarificationReply.objects.filter(question__in=questions).values('provider_message_id')
    reply_raws = RawMessage.objects.filter(message_id__in=reply_ids)
    control_result = json.loads(applied.read_text()) if applied.exists() else {}
    task = Task.objects.filter(pk=control_result.get('task_id')).values('success', 'result').first()
    proof = {'control': control_result, 'task': task,
        'projects': list(projects.values('id', 'name', 'bitrix_id', 'version', 'sale_amount', 'contract_known', 'contract_amount', 'paid_amount', 'cost_amount', 'current_action')),
        'questions': list(questions.values('id', 'kind', 'project_id', 'state', 'notification_id', 'candidate_id', 'price_context', 'resolution', 'options')),
        'replies': list(ClarificationReply.objects.filter(question__in=questions).values('id', 'question_id', 'provider_message_id', 'state', 'error_code', 'raw_message_id')),
        'sources': list(RawMessage.objects.filter(pk__in=ClarificationReply.objects.filter(question__in=questions).values('raw_message_id')).values('id', 'message_id', 'content', 'source', 'processing_state')),
        'deliveries': list(CrmDelivery.objects.filter(fact_event__project__in=projects).values('id', 'state', 'error_code', 'patch', 'target_version', 'fact_event__project_id', 'fact_event__decision__candidate_id')),
        'assertions': list(FieldAssertion.objects.filter(project__in=projects, field_name='sale_amount').values('project_id', 'value', 'fact_event__decision__candidate_id')),
        'answer_candidates': list(FactCandidate.objects.filter(project__in=projects, source_key__startswith='sale-clarification-reply:').values(
            'id', 'project_id', 'status', 'trace__model_version', 'trace__raw_message_id', 'decisions__actor_kind',
            'decisions__reason_code', 'evidence__raw_message_id', 'evidence__quote')),
        'financial': list(FinancialRecord.objects.filter(project__in=projects).values('id', 'project_id', 'amount')),
        'reply_raws': list(reply_raws.values('id', 'message_id', 'processing_state')),
        'reply_traces': list(MessageProcessingTrace.objects.filter(raw_message__in=reply_raws).values('id', 'raw_message_id', 'model_version')),
        'reply_work_items': list(SourceWorkItem.objects.filter(raw_message__in=reply_raws).values_list('raw_message_id', flat=True)),
        'reply_thread_links': list(ThreadMessage.objects.filter(raw_message__in=reply_raws).values_list('raw_message_id', flat=True)),
        'reply_extract_events': list(OutboxEvent.objects.filter(event_type='extract_message', payload__raw_id__in=list(reply_raws.values_list('id', flat=True))).values('id', 'payload__raw_id')),
        'history': WhatsAppHistoryRun.objects.filter(pk=history['run_id']).values('id', 'state', 'scheduled_count', 'existing_count', 'imported_count', 'error_code').first() if history else None,
        'history_sources': list(RawMessage.objects.filter(message_id=history['message_id']).values('id', 'message_id', 'processing_state')) if history else [],
        'notifications': list(NotificationDelivery.objects.filter(notification_id__in=questions.values('notification_id')).values('id', 'notification_id', 'state', 'provider_message_id')),
        'journal': list(BitrixDealChangeLog.objects.filter(external_id__in=projects.values('bitrix_id')).values('id', 'method', 'status', 'external_id', 'payload', 'delivery_id'))}
    temporary = settings.E2E_DIR/'sale-clarifications-proof.tmp'
    temporary.write_text(json.dumps(proof, default=str)); temporary.replace(settings.E2E_DIR/'sale-clarifications-proof.json')
