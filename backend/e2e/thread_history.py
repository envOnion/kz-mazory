"""Immutable source fixtures for real admin/API/outbox/Q2 history scenarios."""

import hashlib
import json
from datetime import timedelta
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone
from api.authentication import create_session
from api.models import Team, TeamMembership, WhatsAppConfig, RawMessage, WhatsAppHistoryJob, WhatsAppHistoryRun
from api.processing_attempts import reserve_attempt


def seed():
    team, _ = Team.objects.get_or_create(name='Полный контекст E2E')
    user = User.objects.get(username='79990000001')
    TeamMembership.objects.get_or_create(user=user, team=team, defaults={'role':'team_lead', 'status':'active'})
    fixtures = {}
    for kind in ('whole', 'chunks', 'cancel', 'restart', 'stop', 'pages', 'repair', 'output'):
        config, _ = WhatsAppConfig.objects.get_or_create(team=team, name=f'История {kind}',
            defaults={'session_name':'full-history', 'group_jid':f'{kind}@g.us', 'snapshot':{'timezone':'UTC+03:00'}})
        texts = ['Полный разбор: сделаешь завтра?', 'Полный разбор: подготовь смету Альфа',
                 'Полный разбор: да, подготовлю смету Альфа завтра']
        if kind == 'pages':
            for index in range(10):
                texts += [f'Параллельный разбор {index}: подготовь документ {index}. ' + 'Технические требования к документу. ' * 12,
                          f'Параллельный разбор {index}: да, подготовлю документ {index} завтра. ' + 'Согласованы требования к документу. ' * 12]
        texts += [(f'Информационное обсуждение {i}: ' + 'Обсудили цвет стен и размеры помещения. ' * 70) for i in range(32)]
        texts += ['Смету Альфа перенесу на послезавтра']
        texts += [(f'Дополнительное обсуждение {i}: ' + 'Записали сведения о помещении. ' * 70) for i in range(16)]
        texts += ['Смету Альфа отменяем, готовить не нужно' if kind == 'cancel' else 'Смета Альфа готова и отправлена']
        if kind == 'pages':
            texts += [f'Параллельный разбор {index}: документ {index} готов и отправлен' for index in range(10)]
        originals = []
        for index, text in enumerate(texts):
            original, _ = RawMessage.objects.get_or_create(source='waha', config=config, message_id=f'full-{kind}-{index}', defaults={
                'team':team, 'session_name':config.session_name, 'chat_id':config.group_jid,
                'source_revision':hashlib.sha256(text.encode()).hexdigest(), 'content':text,
                'timestamp':timezone.now()-timedelta(days=10)+timedelta(minutes=index), 'sent_at_known':True,
                'sender_name':'Боб', 'sender_phone':'79990000002', 'processed':True, 'processing_state':'processed'})
            originals.append(original)
        root = originals[2]
        trace = reserve_attempt(root, f'fixture-{kind}')
        fixtures[kind] = {'raw_id':root.id, 'trace_id':trace.id, 'config_id':config.id, 'total':len(originals)}
        if kind == 'stop':
            from api.history_jobs import source
            job, _ = WhatsAppHistoryJob.objects.get_or_create(config=config, defaults={'enabled':True})
            run, _ = WhatsAppHistoryRun.objects.get_or_create(job=job, state='completed', defaults={'requested_by':user,
                'source_snapshot':source(config), 'settings_snapshot':{'only_new':False}, 'fetched_count':1})
            run.messages.add(root)
            fixtures[kind]['run_id'] = run.id
    _, access, refresh = create_session(user)
    (settings.E2E_DIR/'thread-session.json').write_text(json.dumps({
        'fixtures':fixtures, 'access':access, 'refresh':refresh, 'cookie_name':settings.AUTH_REFRESH_COOKIE}))
