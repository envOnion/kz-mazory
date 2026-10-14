"""Source-backed CRM questions and an authenticated, durable WhatsApp reply inbox."""
import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from . import access
from .models import (AISettings, AuditEvent, CandidateCrmMatch, ClarificationReply,
    CrmDelivery, FactCandidate, FactClarification, FactDecision, NotificationDelivery,
    OutboxEvent, Project, TeamMembership, UserProfile)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def snapshot(candidate):
    raw = candidate.trace.raw_message
    return digest([candidate.proposed_changes, candidate.crm_match_revision,
        candidate.trace.model_version, AISettings.get_active().chat_model_name,
        raw.source_revision if raw else None, raw.processing_state if raw and raw.processing_state in ('deleted', 'superseded') else None,
        list(candidate.evidence.order_by('id').values_list('raw_message_id', 'quote'))])


def authorized(question):
    user = question.recipient
    if not user.is_active or not question.config.is_active or not question.config.team_id or not question.config.team.is_active:
        return False
    profile = UserProfile.objects.filter(user=user).first()
    if not profile or profile.phone != question.source_snapshot.get('phone') or question.config.session_name != question.source_snapshot.get('session') or question.config.group_jid != question.source_snapshot.get('group'):
        return False
    try:
        access.require_team_role(user, question.config.team_id, ['team_lead'])
    except PermissionDenied:
        return False
    return access.configs_for(user).filter(pk=question.config_id).exists()


def hold(candidate):
    review = candidate.decisions.filter(validation__has_key='review_hold').order_by('-id').first()
    if review and review.validation.get('review_hold'):
        return review
    question = candidate.clarifications.filter(state__in=['queued', 'awaiting', 'unconfirmed', 'expired', 'superseded']).order_by('-id').first()
    if question:
        return question
    raw = candidate.trace.raw_message
    if raw and raw.config_id:
        from .deduplication import normalize_deal_name
        label = normalize_deal_name(candidate.proposed_changes.get('object_name', ''))
        if label:
            for unknown in FactClarification.objects.filter(config_id=raw.config_id, state='unconfirmed', candidate__status='pending').select_related('candidate'):
                aliases = [unknown.candidate.proposed_changes.get('object_name', '')]
                saved = unknown.source_snapshot.get('object_aliases', [])
                if isinstance(saved, list):
                    aliases.extend(value for value in saved if isinstance(value, str))
                if label in {normalize_deal_name(value) for value in aliases if value}:
                    return unknown
    # A human defer survives unrelated version/model/catalog changes.
    decision = candidate.decisions.filter(actor_kind='human', outcome='deferred', reason_code='user_requested_unconfirmed').order_by('-id').first()
    answered = candidate.clarifications.filter(state__in=['answered', 'resolved']).order_by('-updated_at').first()
    return decision if decision and (not answered or decision.created_at > answered.updated_at) else None


def confirmed_identity(candidate):
    if not candidate.project_id:
        return False
    return candidate.clarifications.filter(state__in=['answered', 'resolved'],
        resolution__deal_id=candidate.project.bitrix_id,
        resolution__applied_candidate_ids__contains=[candidate.id]).exists()


def plan(candidate, decision):
    if decision.outcome != 'deferred' or decision.reason_code not in (
        'ambiguous_project', 'object_identity_unconfirmed', 'object_identity_incomplete'):
        return None
    if decision.actor_kind == 'human' or hold(candidate):
        return None
    cfg = AISettings.get_active()
    if candidate.trace.model_version != cfg.chat_model_name or candidate.trace.context_metadata.get('analysis_limits', {}).get('review_before_delivery'):
        return None
    from .autonomous import _evidence, _money_matches, Deferred
    from .facts import FactSchema
    try:
        raw, primary, refs, text = _evidence(candidate)
    except Deferred:
        return None
    schema = FactSchema(data=candidate.proposed_changes)
    if not schema.is_valid() or not raw.config_id:
        return None
    changes = schema.validated_data
    if candidate.fact_type == 'payment' and changes.get('amount') and not _money_matches(changes['amount'], text, monetary_unit=True):
        return None
    from .bitrix_service import crm_match_query, normalize_crm_object_name, _partial_object_score
    query = crm_match_query(candidate)
    matches = [item for item in candidate.crm_matches.filter(crm_match_revision=candidate.crm_match_revision).order_by('-score', 'id')
        if not query['object_name'] or any(query['object_name'] == normalize_crm_object_name(label) or _partial_object_score(query['object_name'], normalize_crm_object_name(label)) for label in (item.deal_title, item.object_label))]
    options = [{'deal_id': item.bitrix_deal_id, 'title': item.deal_title,
        'company': item.company_name, 'amount': str(item.opportunity) if item.opportunity is not None else None,
        'currency': item.currency} for item in matches[:8]]
    if not options:
        from .tasks import enqueue_crm_match
        if candidate.crm_match_state in ('not_requested', 'error'):
            enqueue_crm_match(candidate.id, allowed_states=(candidate.crm_match_state,))
            return None
        if candidate.crm_match_state not in ('not_found', 'ambiguous', 'matched'):
            return None
    leads = TeamMembership.objects.filter(team_id=candidate.team_id, role='team_lead', status='active', user__is_active=True).select_related('user').order_by('id')
    recipient = next((lead.user for lead in leads if UserProfile.objects.filter(user=lead.user).exclude(phone='').exists() and access.configs_for(lead.user).filter(pk=raw.config_id).exists()), None)
    if not recipient:
        return None
    from .deduplication import normalize_deal_name
    subject = digest([raw.config_id, normalize_deal_name(changes.get('object_name', '')),
        normalize_deal_name(changes.get('company_name', '')), sorted(row['deal_id'] for row in options)])
    unknown = FactClarification.objects.filter(subject_key=subject, state='unconfirmed').first()
    if unknown:
        unknown.candidates.add(candidate)
        return unknown
    key = digest([subject, primary.id, candidate.trace_id])
    question, created = FactClarification.objects.get_or_create(request_key=key, defaults={
        'subject_key': subject, 'candidate': candidate, 'decision': decision, 'config': raw.config,
        'recipient': recipient, 'options': options, 'expires_at': timezone.now()+timedelta(days=7),
        'source_snapshot': {'session': raw.config.session_name, 'group': raw.config.group_jid, 'phone': UserProfile.objects.get(user=recipient).phone}})
    if question.state not in ('queued', 'awaiting'):
        return question
    question.candidates.add(candidate)
    question.candidate_snapshots[str(candidate.id)] = snapshot(candidate)
    question.save(update_fields=['candidate_snapshots'])
    OutboxEvent.objects.get_or_create(deduplication_key=f'clarification-pump:{question.id}',
        defaults={'event_type': 'clarification_pump', 'payload': {}})
    if created:
        AuditEvent.objects.create(target_type='FactClarification', target_id=question.id,
            action='clarification_created', before_after={'candidate_id': candidate.id, 'decision_id': decision.id})
    return question


def message(question):
    from .message_time import source_time, source_zone
    c = question.candidate
    raw = c.trace.raw_message
    stamp = source_time(raw)[0]
    date = stamp.astimezone(source_zone(raw)).strftime('%d.%m.%Y') if stamp else 'дата неизвестна'
    data = c.proposed_changes
    label = data.get('object_name') or data.get('company_name') or 'объект'
    if c.fact_type == 'payment':
        amount = str(data.get('amount', 'не указана'))
        if data.get('amount'):
            amount = f"{Decimal(str(data['amount'])):,.2f}".replace(',', ' ').replace('.00', '')
        kind = {'invoice': 'выставленный счёт', 'promise': 'ожидаемая оплата', 'debt': 'задолженность'}.get(data.get('payment_kind'), 'сообщение об оплате')
        fact = f"{kind}: {amount} {data.get('currency', 'KZT')}"
        if data.get('amount_precision') == 'approximate':
            fact += ' (приблизительно)'
    else:
        fact = data.get('current_action') or data.get('commitment_text') or 'сообщение по объекту'
    content = raw.content
    at = content.casefold().find(label.casefold())
    if data.get('amount'):
        from .autonomous import MONEY, _money_matches
        amount = Decimal(str(data['amount']))
        at = next((m.start() for m in MONEY.finditer(content) if _money_matches(amount, content[m.start():m.end()+24], monetary_unit=True)), at)
    start, end = max(0, at-120), min(len(content), max(at, 0)+260)
    excerpt = content[start:end].strip()
    if start: excerpt = '…'+excerpt
    if end < len(content): excerpt += '…'
    lines = [f"В сообщении от {date}, {raw.sender_name or 'участник чата'}, по «{label}» указано: {fact}.",
        'Фрагмент оригинала: «'+excerpt+'»',
        'Почему нужен ответ: '+question.decision.explanation,
        'Связь с конкретной сделкой пока не подтверждена. В Bitrix есть варианты:' if question.options else 'Поиск Bitrix не дал подтверждённых вариантов. Нужна конкретная сделка; угадывать её по этому сообщению нельзя.']
    for index, option in enumerate(question.options, 1):
        detail = f"{index}. «{option['title']}» — №{option['deal_id']}"
        if option['company']:
            detail += f", {option['company']}"
        if option['amount'] and Decimal(option['amount']) != 0:
            detail += f", сумма в CRM {Decimal(option['amount']):,.2f} {option['currency']}".replace(',', ' ')
        else:
            detail += ', сумма в CRM не указана'
        lines.append(detail)
    lines += ['К какой сделке относится этот факт? До ответа запись этого факта удержана.',
        f'Ответьте на это сообщение номером варианта или «не знаю». Можно также написать: MZ-{question.id} 1.' if question.options else f'Если знаете ID существующей сделки, напишите: MZ-{question.id} сделка 123. Иначе ответьте «не знаю».']
    return '\n\n'.join(lines)


def pump():
    from .notifications import create_notification
    for question in FactClarification.objects.filter(state='answered'):
        ids = list(question.candidates.values_list('id', flat=True))
        deliveries = CrmDelivery.objects.filter(fact_event__decision__candidate_id__in=ids)
        if ids and not question.candidates.exclude(status='approved').exists() and deliveries.exists() and not deliveries.exclude(state__in=['delivered', 'superseded']).exists():
            question.state = 'resolved'; question.resolved_at = timezone.now()
            question.save(update_fields=['state', 'resolved_at'])
    with transaction.atomic():
        now = timezone.now()
        FactClarification.objects.filter(state='awaiting', expires_at__lte=now).update(state='expired')
        for recipient_id in FactClarification.objects.filter(state='queued').order_by().values_list('recipient_id', flat=True).distinct():
            # Recipient lock serializes independent pump workers.
            UserProfile.objects.select_for_update().filter(user_id=recipient_id).first()
            if FactClarification.objects.filter(recipient_id=recipient_id, state='awaiting').exists():
                continue
            q = FactClarification.objects.select_for_update(of=('self',)).select_related('recipient', 'config__team', 'candidate__trace__raw_message').filter(recipient_id=recipient_id, state='queued').order_by('-created_at', '-id').first()
            if not q:
                continue
            if not authorized(q) or not valid(q):
                q.state = 'superseded'; q.save(update_fields=['state']); continue
            q.notification = create_notification(q.recipient, 'Нужно уточнить сделку Bitrix', message(q),
                'crm_clarification', f'clarification:{q.id}', whatsapp=True)
            q.state = 'awaiting'
            q.expires_at = now+timedelta(days=7)
            q.save(update_fields=['notification', 'state', 'expires_at'])


def valid(question):
    cfg = AISettings.get_active()
    if not cfg.autonomous_enabled:
        return False
    for c in question.candidates.select_related('trace__raw_message', 'project'):
        if c.status != 'pending' or question.candidate_snapshots.get(str(c.id)) != snapshot(c):
            return False
        if c.trace.context_metadata.get('analysis_limits', {}).get('review_before_delivery'):
            return False
    return question.candidates.exists()


def ingest(data):
    payload = data['payload']
    original = data['original_payload']
    body = payload['body'].strip()
    code = re.search(r'\bMZ-(\d+)\b', body, re.I)
    if code and len(code[1]) > 18:
        return 'ignored'
    quoted = original.get('replyTo') or {}
    quoted_id = next((quoted.get(key) for key in ('quotedMessageId', 'id', 'quotedStanzaID', 'stanzaId') if isinstance(quoted.get(key), str)), '')
    if not quoted_id:
        inner = original.get('_data') or {}
        quoted_id = inner.get('quotedStanzaID', '') if isinstance(inner.get('quotedStanzaID', ''), str) else ''
    questions = FactClarification.objects.select_related('recipient', 'config__team')
    stanza = re.fullmatch(r'[A-Za-z0-9-]{8,128}', quoted_id) is not None
    quote_filter = Q(notification__deliveries__provider_message_id=quoted_id)
    if stanza:
        quote_filter |= Q(notification__deliveries__provider_message_id__endswith='_'+quoted_id)
    matches = list(questions.filter(quote_filter, config__session_name=data['session']).distinct()[:2]) if quoted_id else []
    question = questions.filter(pk=int(code[1])).first() if code else matches[0] if len(matches)==1 else None
    if payload['fromMe'] and (NotificationDelivery.objects.filter(provider_message_id=payload['id']).exclude(provider_message_id='').exists()
        or question and question.notification_id and body == f'{question.notification.title}\n{question.notification.message}'):
        return 'ignored'
    if not question:
        return None
    from .participants import phone_from_jid, message_sender
    phone = question.source_snapshot.get('self_phone', '') if payload['fromMe'] else message_sender(original).phone if data['chat_id'].endswith('@g.us') else phone_from_jid(data['chat_id'])
    declared_sender = message_sender(original).phone
    if payload['fromMe'] and declared_sender and declared_sender != phone:
        raise PermissionDenied('Автор ответа не совпадает с аккаунтом сессии WhatsApp.')
    profile = UserProfile.objects.filter(user=question.recipient).first()
    if not phone or not profile or phone != profile.phone or data['session'] != question.config.session_name or not authorized(question):
        raise PermissionDenied('Ответ на уточнение доступен только его получателю.')
    if data['chat_id'] not in (question.config.group_jid, phone+'@c.us', phone+'@s.whatsapp.net'):
        raise PermissionDenied('Ответ пришёл из другого чата.')
    if code and quoted_id and (len(matches)!=1 or matches[0].id != question.id):
        raise PermissionDenied('Код вопроса и цитируемое сообщение не совпадают.')
    with transaction.atomic():
        reply, created = ClarificationReply.objects.get_or_create(session=data['session'], provider_message_id=payload['id'], defaults={
            'question': question, 'sender': question.recipient, 'body': body[:32000], 'quoted_message_id': quoted_id})
        if not created and (reply.question_id != question.id or reply.sender_id != question.recipient_id):
            raise PermissionDenied('Идентификатор ответа уже использован.')
        if created:
            OutboxEvent.objects.create(event_type='clarification_reply', deduplication_key=f'clarification-reply:{reply.id}', payload={'reply_id': reply.id})
    return 'accepted'


def resolve(payload):
    from .bitrix_service import BitrixService
    from .facts import select_crm_match
    from .autonomous import enqueue_decision
    reply = ClarificationReply.objects.select_related('question__recipient', 'question__config__team').get(pk=payload['reply_id'])
    if reply.state != 'queued':
        return
    q = reply.question
    text = re.sub(r'\bMZ-\d+\b', '', reply.body, flags=re.I).strip(' .,:;-').casefold()
    unknown = bool(re.search(r'\b(?:не знаю|не уверен[а]?|оставь(?:те)?(?: без подтверждения| неподтвержд[её]нным| не подтвержд[её]нным)|без подтверждения|ни од(?:на|ной)|не подтверждаю)\b', text))
    index = re.fullmatch(r'(?:вариант\s*)?(\d+)', text)
    external = re.fullmatch(r'(?:сделка\s*|№\s*|id\s*)(\d+)', text)
    option = q.options[int(index[1])-1] if index and 0 < int(index[1]) <= len(q.options) else next((row for row in q.options if external and row['deal_id'] == external[1] or row['title'].casefold() == text), None)
    explicit_project = None
    if not q.options and external:
        explicit_project = Project.objects.filter(team_id=q.config.team_id, bitrix_id=external[1], archived=False).first()
        if explicit_project:
            option = {'deal_id': explicit_project.bitrix_id, 'title': explicit_project.name, 'currency': explicit_project.currency}
    current = BitrixService.call('crm.deal.get', {'id': option['deal_id']}).get('result') if option else None
    with transaction.atomic():
        from .dialogue_threads import lock_candidate_source
        lock_candidate_source(q.candidate)
        q = FactClarification.objects.select_for_update(of=('self',)).select_related('recipient', 'config__team').get(pk=q.id)
        reply = ClarificationReply.objects.select_for_update().get(pk=reply.id)
        if reply.state != 'queued':
            return
        if q.state not in ('awaiting', 'unconfirmed') or q.expires_at <= timezone.now() or not authorized(q) or not valid(q):
            reply.state = 'ignored'; reply.error_code = 'clarification_stale'
            if q.state == 'awaiting': q.state = 'superseded'; q.save(update_fields=['state'])
        elif unknown:
            q.state = 'unconfirmed'; q.resolution = {'reply_id': reply.id, 'user_answer': reply.body, 'external_writes_allowed': False}
            q.save(update_fields=['state', 'resolution'])
            reply.state = 'applied'
            for c in q.candidates.select_related('trace', 'project'):
                FactDecision.objects.create(candidate=c, supersedes=c.decisions.order_by('-id').first(),
                    outcome='deferred', actor_kind='human', policy_version=AISettings.get_active().autonomous_policy_version,
                    input_fingerprint=digest(['clarification-unconfirmed', q.id, c.id]), reason_code='user_requested_unconfirmed',
                    explanation=reply.body, validation={'clarification_id': q.id, 'reply_id': reply.id, 'external_writes_allowed': False})
        elif option and isinstance(current, dict) and str(current.get('ID')) == option['deal_id'] and current.get('TITLE') == option['title']:
            applied = []
            for c in q.candidates.select_for_update(of=('self',)).select_related('project', 'trace').order_by('id'):
                match = c.crm_matches.filter(crm_match_revision=c.crm_match_revision, bitrix_deal_id=option['deal_id']).first()
                if not match and explicit_project:
                    c.crm_match_revision = max(1, c.crm_match_revision)
                    c.save(update_fields=['crm_match_revision'])
                    match = CandidateCrmMatch.objects.create(candidate=c, crm_match_revision=c.crm_match_revision,
                        project=explicit_project, bitrix_deal_id=option['deal_id'], deal_title=option['title'],
                        normalized_deal_title=explicit_project.normalized_name, currency=option['currency'],
                        score=100, match_reasons=['Явный ID сделки от получателя уточнения WhatsApp'])
                if not match:
                    raise ValueError('clarification_option_stale')
                project, _ = Project.objects.get_or_create(bitrix_id=option['deal_id'], defaults={'team_id': c.team_id,
                    'name': option['title'], 'source': 'bitrix_crm', 'identity_confirmed': True, 'currency': option['currency'] or 'KZT'})
                if project.team_id != c.team_id or project.archived:
                    raise PermissionDenied('Выбранная сделка недоступна.')
                match.project = project; match.save(update_fields=['project'])
                previous = c.project_id
                # Pending heuristic bindings are not human business approvals.
                c.project = None; c.crm_match_state = 'ambiguous'; c.save(update_fields=['project', 'crm_match_state'])
                select_crm_match(c.id, q.recipient, match.id, c.crm_match_revision, project.version,
                    f'WhatsApp MZ-{q.id}: {reply.body}')
                AuditEvent.objects.create(actor=q.recipient, target_type='FactCandidate', target_id=c.id,
                    action='clarification_identity', before_after={'previous_project_id': previous, 'project_id': project.id, 'reply_id': reply.id})
                applied.append(c.id)
            q.state = 'answered'; q.resolution = {'deal_id': option['deal_id'], 'reply_id': reply.id, 'applied_candidate_ids': applied}
            q.save(update_fields=['state', 'resolution'])
            for ident in applied: enqueue_decision(ident, f'clarification:{q.id}:{reply.id}')
            reply.state = 'applied'
        elif option:
            q.state = 'superseded'; q.save(update_fields=['state'])
            reply.state = 'ignored'; reply.error_code = 'clarification_crm_changed'
        else:
            reply.state = 'ignored'; reply.error_code = 'clarification_answer_unclear'
            from .notifications import create_notification
            create_notification(q.recipient, f'Уточнение MZ-{q.id}', 'Не удалось однозначно понять выбор. Ответьте номером варианта из вопроса или «не знаю». Запись факта удержана.',
                'crm_clarification', f'clarification-answer:{reply.id}', whatsapp=True)
        reply.save(update_fields=['state', 'error_code'])
        AuditEvent.objects.create(actor=reply.sender, target_type='ClarificationReply', target_id=reply.id,
            action='clarification_answer', before_after={'question_id': q.id, 'state': reply.state, 'reason': reply.error_code})
        OutboxEvent.objects.get_or_create(deduplication_key=f'clarification-pump:reply:{reply.id}', defaults={'event_type': 'clarification_pump', 'payload': {}})
