"""Isolated scenario controls and proof from the actual application database."""
import json
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from api.models import (AISettings, FactCandidate, FactClarification, ClarificationReply,
    WhatsAppConfig, UserProfile, RawMessage, FinancialRecord, CrmDelivery, OutboxEvent)


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
                state.setdefault('deals', {}).update({r['ID']: r for r in rows})
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
            if action == 'expire':
                FactClarification.objects.filter(pk=control['question_id']).update(expires_at=timezone.now()-timedelta(seconds=1))
            if action == 'finish':
                p = settings.E2E_DIR/'provider-state.json'; state = json.loads(p.read_text())
                state.pop('clarification_catalog', None)
                temporary = p.with_suffix('.clarification.tmp'); temporary.write_text(json.dumps(state)); temporary.chmod(0o666); temporary.replace(p)
            applied.write_text(json.dumps({'nonce': control['nonce']}))
    proof = {'questions': list(FactClarification.objects.values('id','state','options','notification_id','candidate_id','resolution','candidate__trace__raw_message__message_id')),
        'replies': list(ClarificationReply.objects.values('id','question_id','provider_message_id','state','error_code')),
        'raw_ids': list(RawMessage.objects.filter(config__group_jid='clarification-e2e@g.us').values_list('message_id',flat=True)),
        'financial': list(FinancialRecord.objects.values('candidate_id','project__bitrix_id','amount','amount_precision')),
        'deliveries': list(CrmDelivery.objects.values('state','fact_event__decision__candidate_id','external_object_link__external_id'))}
    temporary = settings.E2E_DIR/'clarification-proof.tmp'
    temporary.write_text(json.dumps(proof, default=str)); temporary.replace(settings.E2E_DIR/'clarification-proof.json')
