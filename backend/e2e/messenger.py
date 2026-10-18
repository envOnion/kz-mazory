"""Idempotent fixture users for real messenger browser/API/DB/Q2 acceptance."""
import json
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.utils import timezone

from api.authentication import create_session
from api.models import (AdminMFA, ChatAccess, ClientProjectAccess, CommunicationRecord,
    Notification, Project, RawMessage, SourceWorkItem, Team, TeamMembership,
    UserProfile, WhatsAppConfig)
from api.mfa import encryption


def viewer_session():
    viewer, _ = User.objects.get_or_create(username='crm-status-viewer', defaults={'is_staff': True})
    viewer.user_permissions.add(*Permission.objects.filter(codename__in=['view_bitrixsettings','change_whatsappconfig']))
    session, token, refresh = create_session(viewer)
    return {'access':token, 'refresh':refresh, 'cookie_name':settings.AUTH_REFRESH_COOKIE,'user_id':viewer.pk}


def otp_fixture(config_id):
    """A fresh local recipient per run; never reset or weaken rolling OTP limits."""
    phone=f'7998{secrets.randbelow(10000000):07d}'
    user=User.objects.create(username=phone)
    UserProfile.objects.create(user=user,phone=phone,full_name='Вход Messenger E2E')
    TeamMembership.objects.create(user=user,team=WhatsAppConfig.objects.get(pk=config_id).team,role='manager',status='active')
    return {'phone':phone,'user_id':user.pk}


def seed():
    directory = Path(settings.E2E_DIR)
    target = directory / 'messenger-session.json'
    if target.exists():
        data = json.loads(target.read_text())
        for role in ('team_lead','manager','finance','client','integrator','mfa','admin'):
            user = User.objects.get(pk=data[role]['user_id'])
            session, token, refresh = create_session(user)
            data[role].update(access=token, refresh=refresh)
        data['viewer'] = viewer_session()
        data['otp'] = otp_fixture(data['config_id'])
        data.update(private_reply_fixture(data))
        target.write_text(json.dumps(data))
        return
    team, _ = Team.objects.get_or_create(name='Messenger E2E: команда A')
    other, _ = Team.objects.get_or_create(name='Messenger E2E: команда B')
    cfg, _ = WhatsAppConfig.objects.get_or_create(name='Messenger E2E: чат A', defaults={
        'team': team, 'session_name': 'messenger-e2e', 'group_jid': 'messenger-a@g.us'})
    foreign, _ = WhatsAppConfig.objects.get_or_create(name='Messenger E2E: закрытый чат B', defaults={
        'team': other, 'session_name': 'messenger-e2e-b', 'group_jid': 'messenger-b@g.us'})
    sessions = {}
    users = {}
    for offset, role in enumerate(('team_lead','manager','finance','client','integrator','mfa','admin')):
        phone = f'7999000090{offset}'
        user, _ = User.objects.get_or_create(username=phone)
        UserProfile.objects.get_or_create(user=user, defaults={'phone':phone, 'full_name':f'Messenger E2E {role}'})
        if role not in ('client','integrator','mfa','admin'):
            TeamMembership.objects.get_or_create(user=user, team=team, role=role, defaults={'status':'active'})
        if role in ('integrator','mfa'):
            user.is_staff = True; user.save(update_fields=['is_staff'])
            user.user_permissions.add(Permission.objects.get(codename='change_whatsappconfig'))
            if role == 'mfa':
                user.user_permissions.add(Permission.objects.get(codename='change_aisettings'))
                AdminMFA.objects.get_or_create(user=user, defaults={
                    'encrypted_secret':encryption().encrypt(b'JBSWY3DPEHPK3PXP').decode(),
                    'confirmed_at':timezone.now(), 'last_counter':-1})
        if role == 'admin':
            user.is_staff = user.is_superuser = True; user.save(update_fields=['is_staff','is_superuser'])
        if role == 'finance':
            ChatAccess.objects.get_or_create(user=user, config=cfg)
        if role == 'client':
            project, _ = Project.objects.get_or_create(team=team, name='Messenger E2E: клиентский объект')
            ClientProjectAccess.objects.get_or_create(user=user, project=project, defaults={'status':'active'})
        users[role] = user
        session, token, refresh = create_session(user)
        sessions[role] = {'access':token, 'refresh':refresh, 'cookie_name':settings.AUTH_REFRESH_COOKIE,
            'user_id':user.pk}
    project, _ = Project.objects.get_or_create(team=team, name='Messenger E2E: клиентский объект')
    if not project.is_verified:
        project.is_verified=True; project.save(update_fields=['is_verified'])
    ClientProjectAccess.objects.get_or_create(user=users['client'], project=project, defaults={'status':'active'})
    ids = {}
    for key, config in (('allowed',cfg),('foreign',foreign)):
        raw, _ = RawMessage.objects.get_or_create(source='waha', session_name=config.session_name,
            message_id=f'messenger-e2e-{key}', source_revision='0', defaults={
                'team':config.team,'config':config,'chat_id':config.group_jid,'timestamp':timezone.now(),
                'content':f'Messenger E2E {key}: проверка области доступа', 'sender_name':'Локальный участник'})
        record = CommunicationRecord.objects.get(raw_message=raw)
        SourceWorkItem.objects.get_or_create(raw_message=raw, processing_version='messenger-e2e', defaults={'state':'done'})
        ids[key] = record.pk
    private, _ = Notification.objects.get_or_create(recipient=users['manager'],
        deduplication_key='messenger-e2e-private', defaults={'title':'Личное уточнение', 'message':'Закрытый ответ менеджеру'})
    ids['private'] = CommunicationRecord.objects.get(notification=private).pk
    sessions['viewer'] = viewer_session()
    sessions.update(records=ids, config_id=cfg.pk, other_config_id=foreign.pk)
    sessions['otp'] = otp_fixture(cfg.pk)
    sessions.update(private_reply_fixture(sessions))
    target.write_text(json.dumps(sessions))
    target.chmod(0o644)


def private_reply_fixture(sessions):
    cfg=WhatsAppConfig.objects.get(pk=sessions['config_id'])
    raw,_=RawMessage.objects.get_or_create(source='waha',session_name=cfg.session_name,
        message_id='messenger-e2e-private-raw-reply',source_revision='0',defaults={
            'config':cfg,'team':cfg.team,'chat_id':'79990000901@c.us','sender_name':'Личный ответ менеджера',
            'content':'Messenger E2E private answer: только адресату','timestamp':timezone.now(),
            'processed':True,'processing_state':'clarification_reply'})
    record=CommunicationRecord.objects.get(raw_message=raw)
    CommunicationRecord.objects.filter(pk=record.pk).update(recipient_id=sessions['manager']['user_id'],purpose='reply')
    return {'private_raw_id':raw.pk,'private_reply_record_id':record.pk}


def seed_performance():
    """Append a separate 100k-message history, never reset existing fixtures."""
    from api.communication import journal_raw_bulk
    from django.db import connection
    team, _ = Team.objects.get_or_create(name='Messenger E2E: 100000 history')
    user, _ = User.objects.get_or_create(username='79990000888')
    UserProfile.objects.get_or_create(user=user, defaults={'phone':user.username,'full_name':'Проверка большой истории'})
    TeamMembership.objects.get_or_create(user=user,team=team,role='team_lead',defaults={'status':'active'})
    start = timezone.now() - timedelta(days=10)
    for batch in range(100):
        config,_=WhatsAppConfig.objects.get_or_create(name=f'Messenger E2E: нагрузочная история {batch}',defaults={
            'team':team,'session_name':'messenger-performance','group_jid':f'performance-{batch}@g.us'})
        existing = set(RawMessage.objects.filter(session_name='messenger-performance',message_id__startswith=f'perf-{batch}-').values_list('message_id',flat=True))
        rows = [RawMessage(team=team,config=config,session_name='messenger-performance',
            message_id=f'perf-{batch}-{index}', chat_id=f'performance-{batch}@g.us',
            content=f'История для проверки производительности: часть {batch}, сообщение {index}',
            sender_name='Участник истории',timestamp=start+timedelta(seconds=batch*1000+index),
            processed=True) for index in range(1000) if f'perf-{batch}-{index}' not in existing]
        RawMessage.objects.bulk_create(rows,batch_size=250)
        journal_raw_bulk(list(RawMessage.objects.filter(session_name='messenger-performance',message_id__startswith=f'perf-{batch}-').select_related('config')))
    with connection.cursor() as cursor:
        cursor.execute('ANALYZE api_rawmessage'); cursor.execute('ANALYZE api_communicationrecord'); cursor.execute('ANALYZE api_communicationevent')
    _, access_token, refresh = create_session(user)
    count = RawMessage.objects.filter(session_name='messenger-performance').count()
    (Path(settings.E2E_DIR)/'messenger-performance.json').write_text(json.dumps({
        'access':access_token,'refresh':refresh,'cookie_name':settings.AUTH_REFRESH_COOKIE,'count':count}))
    print(f'Performance history ready: {count} real messages')
