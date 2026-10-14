"""Isolated scenario controls and proof from the actual application database."""
import json
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from api.models import (AISettings, FactCandidate, FactClarification, ClarificationReply,
    WhatsAppConfig, UserProfile, RawMessage, FinancialRecord, CrmDelivery, OutboxEvent, NotificationDelivery,
    Project, CandidateCrmMatch)


def update():
    path = settings.E2E_DIR/'clarification-control.json'
    applied = settings.E2E_DIR/'clarification-applied.json'
    if path.exists():
        control = json.loads(path.read_text())
        previous = json.loads(applied.read_text()) if applied.exists() else {}
        if previous.get('nonce') != control['nonce']:
            action = control['action']
            if action == 'start':
                from django.contrib.auth.models import User
                from api.authentication import create_session
                cfg = AISettings.objects.first()
                cfg.autonomous_enabled = cfg.autonomous_crm_enabled = True
                cfg.thread_routing_mode = 'llm'; cfg.context_window_tokens = 131072
                cfg.analysis_input_token_limit = 16384; cfg.save()
                source = WhatsAppConfig.objects.first()
                WhatsAppConfig.objects.get_or_create(group_jid=control['group'], defaults={'team_id': source.team_id,
                    'name': 'Clarifications E2E', 'snapshot': {'timezone': 'Asia/Almaty'}})
                user = User.objects.get(username='79990000001')
                UserProfile.objects.filter(user=user).update(notification_preferences={'quiet_start': '00:00', 'quiet_end': '00:00', 'whatsapp': True})
                _, access, refresh = create_session(user)
                (settings.E2E_DIR/'clarification-session.json').write_text(json.dumps({'access': access, 'refresh': refresh, 'cookie_name': settings.AUTH_REFRESH_COOKIE}))
                p = settings.E2E_DIR/'provider-state.json'; state = json.loads(p.read_text())
                state.pop('thread_window', None); state.pop('autonomous', None)
                rows = []
                for name, ids in [('Парус', [701,702]), ('Маяк', [711,712]), ('Риф', [721,722])]:
                    for index, ident in enumerate(ids, 1):
                        rows.append({'ID': str(ident), 'TITLE': f'ЦТП {name} {index}', 'CURRENCY_ID': 'KZT', 'OPPORTUNITY': '75000000.00', 'STAGE_ID': 'NEW', 'COMPANY_ID': '0',
                            'ASSIGNED_BY_ID': '7', 'DATE_CREATE': '2026-10-01T10:00:00+05:00', 'DATE_MODIFY': '2026-10-08T10:00:00+05:00',
                            'MOVED_TIME': '2026-10-07T10:00:00+05:00', 'BEGINDATE': '2026-10-01', 'CLOSEDATE': '2027-01-01', 'CLOSED': 'N'})
                state['clarification_catalog'] = rows
                sample = rows[0]
                state['clarification_catalog'] += [{**sample,'ID':'731','TITLE':'Поликлиника в мкр Шугла БМК 1,24 МВт','COMPANY_ID':'0'},
                    {**sample,'ID':'732','TITLE':'КШ село Достык','COMPANY_ID':'900'}]
                state.setdefault('companies',{})['900']={'ID':'900','TITLE':'Интегра'}
                state['waha_missing_send_id'] = True
                state.setdefault('deals', {}).update({r['ID']: r for r in state['clarification_catalog']})
                temporary = p.with_suffix('.clarification.tmp'); temporary.write_text(json.dumps(state)); temporary.chmod(0o666); temporary.replace(p)
            if action == 'pause_reply':
                (settings.E2E_DIR/'clarification-paused').touch()
            if action == 'resume_reply':
                (settings.E2E_DIR/'clarification-paused').unlink(missing_ok=True)
            if action == 'stale':
                question = FactClarification.objects.get(pk=control['question_id'])
                c = question.candidate
                c.proposed_changes = {**c.proposed_changes, 'current_action': 'Новая версия кандидата'}
                c.save(update_fields=['proposed_changes'])
            if action == 'review_hold':
                from api.models import FactDecision
                from api.autonomous import enqueue_decision
                question = FactClarification.objects.get(pk=control['question_id'])
                c = question.candidate
                FactDecision.objects.create(candidate=c, outcome='deferred', reason_code='source_review_hold',
                    input_fingerprint='e2e-reviewed-source-hold', policy_version=AISettings.get_active().autonomous_policy_version,
                    explanation='Проверка оригинала не подтверждает факт.', validation={'review_hold': True})
                # A later catalog revision and a worker rerun must retain the hold.
                c.crm_match_revision += 1
                c.save(update_fields=['crm_match_revision'])
                enqueue_decision(c.id, 'e2e-reviewed-source-hold')
            if action == 'expire':
                FactClarification.objects.filter(pk=control['question_id']).update(expires_at=timezone.now()-timedelta(seconds=1))
            if action == 'clinic_prelink':
                from api.tasks import enqueue_crm_match
                c = FactClarification.objects.get(pk=control['question_id']).candidate
                c.project, _ = Project.objects.get_or_create(bitrix_id='732', defaults={
                    'name': 'КШ село Достык', 'team_id': c.team_id})
                c.save(update_fields=['project'])
                enqueue_crm_match(c.id, allowed_states=('matched', 'ambiguous', 'not_found'))
            if action == 'finish':
                p = settings.E2E_DIR/'provider-state.json'; state = json.loads(p.read_text())
                state.pop('clarification_catalog', None)
                state.pop('waha_missing_send_id', None)
                temporary = p.with_suffix('.clarification.tmp'); temporary.write_text(json.dumps(state)); temporary.chmod(0o666); temporary.replace(p)
            applied.write_text(json.dumps({'nonce': control['nonce']}))
    proof = {'questions': list(FactClarification.objects.values('id','state','options','notification_id','candidate_id','resolution','candidate__trace__raw_message__message_id')),
        'replies': list(ClarificationReply.objects.values('id','question_id','provider_message_id','state','error_code')),
        'decisions': list(FactCandidate.objects.filter(decisions__reason_code='source_review_hold').values('id','status','decisions__reason_code')),
        'candidates': list(FactCandidate.objects.filter(trace__raw_message__message_id__startswith='clarification-').values('id','status','trace__raw_message__message_id','decisions__reason_code')),
        'matches': list(CandidateCrmMatch.objects.filter(candidate__trace__raw_message__message_id='clarification-clinic').values('candidate_id','crm_match_revision','bitrix_deal_id')),
        'raw_ids': list(RawMessage.objects.filter(config__group_jid='clarification-e2e@g.us').values_list('message_id',flat=True)),
        'financial': list(FinancialRecord.objects.values('candidate_id','project__bitrix_id','amount','amount_precision')),
        'notification_deliveries': list(NotificationDelivery.objects.values('id','notification_id','state','provider_message_id')),
        'notification_events': list(OutboxEvent.objects.filter(event_type='notification').values('state','payload__delivery_id')),
        'deliveries': list(CrmDelivery.objects.values('state','fact_event__decision__candidate_id','external_object_link__external_id'))}
    temporary = settings.E2E_DIR/'clarification-proof.tmp'
    temporary.write_text(json.dumps(proof, default=str)); temporary.replace(settings.E2E_DIR/'clarification-proof.json')
