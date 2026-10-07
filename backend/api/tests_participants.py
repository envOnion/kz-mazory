import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.db import close_old_connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Participant, ParticipantIdentity, RawMessage, Team, TeamMembership, UserProfile, OutboxEvent, WhatsAppConfig
from .participants import Sender, directory_participants, enqueue_participants, message_sender, phone_from_jid, register_sender
from .tasks import sync_whatsapp_participants
from .tests_whatsapp_exports import ExportFixtures


class SenderTests(SimpleTestCase):
    def test_noweb_nested_participant_is_opaque_and_alternate_phone_is_explicit(self):
        self.assertEqual(message_sender({'_data': {'participant': '219999999999999@lid'}}), Sender(jid='219999999999999@lid'))
        self.assertEqual(message_sender({'_data': {'key': {'participant': '219999999999999@lid', 'participantAlt': '79990000004@s.whatsapp.net'}, 'pushName': 'Оператор'}}), Sender('219999999999999@lid', '79990000004', 'Оператор'))

    def test_phone_types_device_jids_and_outgoing_messages(self):
        self.assertEqual(phone_from_jid('79990000004:12@s.whatsapp.net'), '79990000004')
        for value in ('219999999999999@lid', '120363411153305864@g.us', '79990000004', None):
            self.assertEqual(phone_from_jid(value), '')
        self.assertEqual(message_sender({'fromMe': True, 'from': 'chat@g.us'}, '79990000001@c.us').phone, '79990000001')


class ParticipantsTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name='People')
        self.config = WhatsAppConfig.objects.create(team=self.team, group_jid='people@g.us')
        self.payload = {'config_id': self.config.id, 'team_id': self.team.id, 'session_name': 'default', 'chat_id': self.config.group_jid}

    def raw(self, key, source='waha', name='', body='Отчёт об объекте Альфа', stamp=None, payload=None):
        return RawMessage.objects.create(config=self.config, team=self.team, chat_id=self.config.group_jid, message_id=key, source=source, sender_name=name, sender_phone='219999999999999' if source == 'waha' else '', content=body, timestamp=stamp or timezone.now(), raw_payload={'payload': payload or {'_data': {'participant': '219999999999999@lid'}}})

    def provider(self, method, path, **kwargs):
        self.assertEqual(method, 'GET')
        if '/participants/v2' in path:
            return [{'id': '219999999999999@lid', 'pn': '79990000004@c.us', 'role': 'superadmin'}, {'id': '123456@lid', 'pn': '79990000005@c.us', 'role': 'participant'}]
        if '/sessions/' in path:
            return {'me': {'id': '79990000001@c.us'}}
        if '/contacts?' in path:
            jid = '219999999999999@lid' if '219999999999999' in path else '123456@lid'
            return {'id': jid, 'name': '+7∙∙∙∙65', 'pushname': 'Оператор' if jid == '219999999999999@lid' else 'Вячеслав'}
        self.fail(path)

    def test_sync_restores_lost_authors_and_silent_members_without_access_or_duplicates(self):
        raw = self.raw('history')
        original = raw.raw_payload
        event = enqueue_participants(self.config)
        self.assertEqual(enqueue_participants(self.config).id, event.id)
        with patch('api.tasks.waha_request', side_effect=self.provider):
            sync_whatsapp_participants(self.payload)
            before = list(Participant.objects.values_list('id', flat=True))
            sync_whatsapp_participants(self.payload)
        self.assertEqual(list(Participant.objects.values_list('id', flat=True)), before)
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(Participant.objects.count(), 2)
        self.assertFalse(TeamMembership.objects.exists())
        for user in User.objects.all():
            self.assertFalse(user.has_usable_password())
            self.assertFalse(user.is_staff or user.is_superuser)
        raw.refresh_from_db()
        self.assertEqual(raw.sender_phone, '79990000004')
        self.assertEqual(raw.sender_name, 'Оператор')
        self.assertEqual(raw.raw_payload, original)
        self.assertEqual(RawMessage.objects.count(), 1)

    def test_repeated_unique_messages_link_txt_aliases_without_rewriting_originals(self):
        start = timezone.now().replace(second=0, microsecond=0)
        for i in range(2):
            stamp = start + timedelta(minutes=i)
            body = f'Подробный отчёт объекта Альфа номер {i}'
            self.raw(f'waha-{i}', body=body, stamp=stamp)
            self.raw(f'txt-{i}', source='whatsapp_export', name='Имя в экспорте', body=body, stamp=stamp)
        with patch('api.tasks.waha_request', side_effect=self.provider):
            sync_whatsapp_participants(self.payload)
            sync_whatsapp_participants(self.payload)
        self.assertEqual(Participant.objects.count(), 2)
        operator = Participant.objects.get(user_profile__phone='79990000004')
        self.assertIn('имя в экспорте', list(operator.identities.values_list('value', flat=True)))
        self.assertEqual(RawMessage.objects.filter(source='whatsapp_export', sender_name='Имя в экспорте').count(), 2)

    def test_history_from_previous_config_scope_is_excluded_and_resolved_lid_uses_phone_label(self):
        self.raw('current')
        previous = self.raw('previous', payload={'participant': '999999@lid'})
        previous.team = Team.objects.create(name='Previous team')
        previous.chat_id = 'previous@g.us'
        previous.session_name = 'previous'
        previous.save()
        def provider(method, path, **kwargs):
            if '/contacts?' in path:
                return {}
            return self.provider(method, path, **kwargs)
        with patch('api.tasks.waha_request', side_effect=provider):
            sync_whatsapp_participants(self.payload)
        self.assertFalse(ParticipantIdentity.objects.filter(value='999999@lid').exists())
        person = Participant.objects.get(user_profile__phone='79990000004')
        self.assertEqual(person.display_name, '79990000004')
        self.assertEqual(User.objects.count(), 2)
        previous.refresh_from_db()
        self.assertEqual(previous.sender_phone, '219999999999999')

    def test_single_match_and_same_name_from_different_authors_do_not_merge(self):
        stamp = timezone.now().replace(second=0, microsecond=0)
        for i in range(3):
            self.raw(f'w-{i}', stamp=stamp + timedelta(minutes=i), body=f'Подробный текст для проверки номера {i}', payload={'participant': '219999999999999@lid' if i < 2 else '123456@lid'})
            self.raw(f't-{i}', source='whatsapp_export', name='Одно имя', stamp=stamp + timedelta(minutes=i), body=f'Подробный текст для проверки номера {i}')
        with patch('api.tasks.waha_request', side_effect=self.provider):
            sync_whatsapp_participants(self.payload)
        self.assertEqual(Participant.objects.count(), 3)
        self.assertIsNone(Participant.objects.get(display_name='Одно имя').user_profile_id)

    def test_missing_phone_is_visible_and_lid_never_becomes_user(self):
        self.raw('unknown')
        def provider(method, path, **kwargs):
            if '/participants/' in path: return [{'id': '219999999999999@lid', 'pn': None}]
            if '/sessions/' in path: return {'me': None}
            if '/lids/' in path: return {'lid': '219999999999999@lid', 'pn': None}
            return {'id': '219999999999999@lid', 'pushname': 'Имя без телефона'}
        with patch('api.tasks.waha_request', side_effect=provider):
            sync_whatsapp_participants(self.payload)
        self.assertEqual(User.objects.count(), 0)
        root = User.objects.create_superuser('root')
        rows = directory_participants(root)
        self.assertEqual(rows[0]['resolution_state'], 'phone_unknown')
        self.assertEqual(rows[0]['phone'], '')
        raw = RawMessage.objects.get()
        self.assertEqual(raw.sender_phone, '')

    def test_canonical_txt_original_keeps_waha_author_evidence_in_queue(self):
        from .whatsapp_identity import ingest_waha
        raw = self.raw('canonical', source='whatsapp_export', name='Оператор')
        original_payload = {'id': 'aliased-waha', 'participant': '219999999999999@lid', 'notifyName': 'Оператор'}
        original, _ = ingest_waha(self.config, message_id=original_payload['id'], content=raw.content, timestamp=raw.timestamp, sender_name='Оператор')
        self.assertEqual(original.id, raw.id)
        event = enqueue_participants(self.config, message_evidence=[{'raw_id': raw.id, 'payload': original_payload}])
        with patch('api.tasks.waha_request', side_effect=self.provider):
            sync_whatsapp_participants(event.payload)
        self.assertEqual(Participant.objects.count(), 2)
        self.assertEqual(RawMessage.objects.count(), 1)
        self.assertTrue(ParticipantIdentity.objects.filter(value='оператор', participant__user_profile__phone='79990000004').exists())

    def test_existing_profile_roles_and_inactive_account_are_preserved(self):
        user = User.objects.create_user('79990000004', password='keep-me', is_active=False)
        profile = UserProfile.objects.create(user=user, phone=user.username, full_name='Ручное имя', email='keep@example.test')
        membership = TeamMembership.objects.create(user=user, team=self.team, role='finance', status='revoked')
        register_sender(self.config, Sender('219999999999999@lid', user.username, 'Другое имя'))
        user.refresh_from_db(); profile.refresh_from_db(); membership.refresh_from_db()
        self.assertTrue(user.check_password('keep-me'))
        self.assertFalse(user.is_active)
        self.assertEqual(profile.full_name, 'Ручное имя')
        self.assertEqual(profile.email, 'keep@example.test')
        self.assertEqual(membership.status, 'revoked')

    def test_profile_conflict_does_not_silently_rebind(self):
        user = User.objects.create_user('79990000004')
        UserProfile.objects.create(user=user, phone='79990000005')
        person = register_sender(self.config, Sender('219999999999999@lid', '79990000004', 'Конфликт'))
        self.assertIsNone(person.user_profile_id)
        self.assertTrue(person.identities.filter(resolution_state='conflict').exists())

    def test_changed_lid_phone_mapping_does_not_create_or_rebind_an_account(self):
        person = register_sender(self.config, Sender('219999999999999@lid', '79990000004', 'Оператор'))
        profile_id = person.user_profile_id
        register_sender(self.config, Sender('219999999999999@lid', '79990000005', 'Оператор'))
        person.refresh_from_db()
        self.assertEqual(person.user_profile_id, profile_id)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(person.identities.filter(resolution_state='conflict').exists())

    def test_waha_failure_keeps_discovered_people_without_creating_accounts(self):
        self.raw('saved')
        self.raw('text', source='whatsapp_export', name='Имя из файла')
        with patch('api.tasks.waha_request', side_effect=requests.Timeout):
            with self.assertRaises(requests.Timeout):
                sync_whatsapp_participants(self.payload)
        self.assertEqual(Participant.objects.count(), 2)
        self.assertFalse(User.objects.exists())

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_directory_and_sync_authorization_scope(self):
        lead = User.objects.create_user('lead')
        TeamMembership.objects.create(user=lead, team=self.team, role='team_lead', status='active')
        register_sender(self.config, Sender(name='Наш участник'))
        other = Team.objects.create(name='Other')
        Participant.objects.create(team=other, display_name='Чужой')
        client = APIClient(); client.force_authenticate(lead)
        response = client.get('/api/directory/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['display_name'] for row in response.data['participants']], ['Наш участник'])
        with patch('api.tasks.waha_request', side_effect=AssertionError('HTTP must not contact WAHA')):
            self.assertEqual(client.post('/api/directory/participants-sync/', {'config_id': self.config.id}).status_code, 202)
        TeamMembership.objects.filter(user=lead).update(role='manager')
        self.assertEqual(client.get('/api/directory/').data['participants'], [])
        self.assertEqual(client.post('/api/directory/participants-sync/', {'config_id': self.config.id}).status_code, 404)

    @override_settings(ALLOWED_HOSTS=['testserver'], WAHA_WEBHOOK_SECRET='test-signing-key')
    def test_webhook_preserves_nested_identity_and_only_enqueues_provider_work(self):
        body = json.dumps({'event': 'message', 'session': 'default', 'payload': {'id': 'hook', 'body': 'Отчёт', 'from': self.config.group_jid, '_data': {'participant': '219999999999999@lid', 'pushName': 'Оператор'}}}).encode()
        signature = hmac.new(b'test-signing-key', body, hashlib.sha512).hexdigest()
        with patch('api.tasks.waha_request', side_effect=AssertionError('HTTP must not contact WAHA')):
            response = APIClient().post('/api/whatsapp/webhook/', body, content_type='application/json', HTTP_X_WEBHOOK_HMAC=signature)
        self.assertEqual(response.status_code, 202)
        raw = RawMessage.objects.get(message_id='hook')
        self.assertEqual(raw.sender_phone, '')
        self.assertEqual(raw.raw_payload['payload']['_data']['participant'], '219999999999999@lid')
        self.assertTrue(OutboxEvent.objects.filter(event_type='whatsapp_participants').exists())


class ExportPeopleTests(ExportFixtures):
    def test_media_only_author_is_discovered_without_waha(self):
        run = self.upload('01.09.2026, 12:00 - Только медиа: <Без медиафайлов>\n')
        with patch('api.tasks.waha_request', side_effect=AssertionError('local import')):
            self.import_all(run)
        self.assertTrue(Participant.objects.filter(display_name='Только медиа').exists())
        self.assertFalse(User.objects.exclude(pk=self.user.id).exists())
        self.assertFalse(RawMessage.objects.exists())


@skipUnlessDBFeature('has_select_for_update')
class ParticipantConcurrencyTests(TransactionTestCase):
    def test_same_phone_imported_in_two_teams_has_one_account_and_separate_people(self):
        configs = [WhatsAppConfig.objects.create(team=Team.objects.create(name=f'Team {i}'), group_jid=f'{i}@g.us') for i in range(2)]
        def discover(config):
            close_old_connections()
            try:
                return register_sender(config, Sender('219999999999999@lid', '79990000004', 'Оператор')).id
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(discover, configs))
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(UserProfile.objects.count(), 1)
        self.assertEqual(ParticipantIdentity.objects.filter(value='79990000004').count(), 2)
