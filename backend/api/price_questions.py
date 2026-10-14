"""Durable requests for missing selling prices, planned only by Q2 workers."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import CharField, Exists, OuterRef, Q, Value
from django.db.models.functions import Cast, Concat
from django.utils import timezone

from . import access
from .models import (AISettings, AuditEvent, BitrixSettings, FactCandidate,
    FactClarification, FactDecision, FactEvidence, OutboxEvent, Project,
    RawMessage, TeamMembership, UserProfile, WhatsAppConfig)
from .providers import ProviderUnavailable


def enqueue(project_id):
    """No external I/O here; callers may be completing a CRM delivery."""
    cfg = AISettings.get_active()
    if not cfg.autonomous_enabled or not cfg.autonomous_crm_enabled:
        return None
    project = Project.objects.filter(pk=project_id, archived=False).first()
    if not project or not project.bitrix_id or project.sale_amount or project.contract_known and project.contract_amount:
        return None
    event, _ = OutboxEvent.objects.get_or_create(
        deduplication_key=f'price-question:{project.id}:{project.version}',
        defaults={'event_type': 'price_question', 'payload': {'project_id': project.id}})
    return event.id


def reconcile(limit=20):
    """Find unsurveyed Mazory projects without repeatedly reading every CRM row."""
    candidates = Project.objects.filter(archived=False, source='chat', team__is_active=True).exclude(Q(bitrix_id='') | Q(bitrix_id__isnull=True))
    candidates = candidates.filter(Q(sale_amount__isnull=True) | Q(sale_amount=0)).exclude(contract_known=True, contract_amount__gt=0)
    # A checked-but-ineligible project must not occupy the first batch forever.
    candidates = candidates.annotate(_price_key=Concat(Value('price-question:'),
        Cast('id', CharField()), Value(':'), Cast('version', CharField()))).annotate(
            _price_planned=Exists(OutboxEvent.objects.filter(deduplication_key=OuterRef('_price_key')))).filter(_price_planned=False)
    covered = FactClarification.objects.filter(kind='sale_amount', project__isnull=False).exclude(state='superseded').values_list('project_id', flat=True)
    for project_id in candidates.exclude(pk__in=covered).order_by('id').values_list('id', flat=True)[:limit]:
        enqueue(project_id)


def _human_hold(project, config):
    from .deduplication import normalize_deal_name
    labels = {project.normalized_name, normalize_deal_name(project.name)}
    labels.update(project.aliases.values_list('normalized_name', flat=True))
    for q in FactClarification.objects.filter(kind='identity', config=config, state='unconfirmed', candidate__status='pending', candidate__fact_type='project').select_related('candidate'):
        names = [q.candidate.proposed_changes.get('object_name', ''), *q.source_snapshot.get('object_aliases', [])]
        if q.project_id == project.id or q.candidate.project_id == project.id or any(normalize_deal_name(name) in labels for name in names if isinstance(name, str) and name):
            return f'Сохранён ответ «не знаю / оставить неподтверждённым» из вопроса MZ-{q.id}.'
    originals = project.candidates.filter(fact_type='project').exclude(source_key__startswith='price-question:').exclude(source_key__startswith='sale-clarification-reply:')
    for candidate in originals:
        held = candidate.decisions.filter(actor_kind='human', outcome='deferred', reason_code='user_requested_unconfirmed').order_by('-id').first()
        answered = candidate.clarifications.filter(kind='identity', state__in=('answered', 'resolved')).order_by('-updated_at').first()
        if held and (not answered or held.created_at > answered.updated_at):
            return held.explanation
    return ''


def _evidence(project, review):
    """Check literal references, never manufacture text or infer a price."""
    from .message_time import source_time
    entries = review.get('evidence', [])
    if not entries:
        candidate = project.candidates.filter(status='approved', trace__raw_message__config__is_active=True).exclude(source_key__startswith='price-').select_related('trace__raw_message__config').order_by('-id').first()
        if not candidate:
            return None, []
        raw = candidate.trace.raw_message
        quote = raw.content
        labels = [project.name, candidate.proposed_changes.get('object_name', ''), *project.aliases.values_list('normalized_name', flat=True)]
        positions = [quote.casefold().find(label.casefold()) for label in labels if label and label.casefold() in quote.casefold()]
        display_quote = ''
        if positions:
            at = min(positions)
            start = quote.rfind('\n', 0, at)+1
            quote = quote[start:start+650]
            display_quote = quote
        elif len(quote) <= 650:
            display_quote = quote
        # The complete report remains evidence if its object alias cannot be
        # located literally. Never show another object's opening paragraph.
        entries = [{'source_id': raw.id, 'quote': quote, 'display_quote': display_quote, 'source_date': source_time(raw)[0].isoformat() if source_time(raw)[0] else None}]
    checked = []
    config = None
    for entry in entries:
        if type(entry.get('source_id')) is not int or not isinstance(entry.get('quote'), str) or not entry['quote'].strip():
            raise ValueError('price_question_invalid_evidence')
        raw = RawMessage.objects.select_related('config__team').get(pk=entry['source_id'])
        if not raw.config_id or not raw.config.is_active or raw.config.team_id != project.team_id or raw.processing_state in ('deleted', 'superseded') or entry['quote'] not in raw.content:
            raise ValueError('price_question_evidence_unavailable')
        if config and config.id != raw.config_id:
            raise ValueError('price_question_source_scope_mismatch')
        config = raw.config
        stamp = source_time(raw)[0]
        if entry.get('source_date'):
            from django.utils.dateparse import parse_datetime
            if parse_datetime(entry['source_date']) != stamp:
                raise ValueError('price_question_evidence_date_mismatch')
        display_quote = entry.get('display_quote', entry['quote'])
        if display_quote and display_quote not in entry['quote']:
            raise ValueError('price_question_display_quote_not_literal')
        checked.append({'source_id': raw.id, 'quote': entry['quote'], 'display_quote': display_quote, 'source_date': stamp.isoformat() if stamp else None})
    return config, checked


def plan_project(project_id, review=None):
    """Read CRM and persist one question. Returns its id, never sends directly."""
    from .bitrix_service import BitrixService
    from .clarifications import digest, snapshot
    from .commercial_amounts import commercial_amount
    cfg = AISettings.get_active()
    if not cfg.autonomous_enabled or not cfg.autonomous_crm_enabled or not BitrixSettings.get_active().is_active:
        return None
    review = review or {}
    project = Project.objects.select_related('team').get(pk=project_id)
    if project.archived or not project.team_id or not project.team.is_active or not project.bitrix_id or commercial_amount(project) is not None:
        return None
    existing = FactClarification.objects.filter(kind='sale_amount', project=project).order_by('-id').first()
    if existing and (existing.state in ('queued', 'awaiting', 'answered', 'unconfirmed', 'resolved', 'expired') or existing.price_context.get('project_version') == project.version):
        return existing.id
    config, evidence = _evidence(project, review)
    if not config:
        return None
    recipient = next((m.user for m in TeamMembership.objects.filter(team=project.team, role='team_lead', status='active', user__is_active=True).select_related('user').order_by('id')
        if UserProfile.objects.filter(user=m.user).exclude(phone='').exists() and access.configs_for(m.user).filter(pk=config.id).exists()), None)
    if not recipient:
        return None
    current = BitrixService.call('crm.deal.get', {'id': project.bitrix_id}).get('result')
    if not isinstance(current, dict) or str(current.get('ID')) != project.bitrix_id:
        raise ProviderUnavailable('crm_readback_failed')
    if current.get('ORIGINATOR_ID') != 'MAZORY' or str(current.get('ORIGIN_ID')) not in (str(project.id), f'mazory:{project.team_id}:project:{project.id}'):
        return None
    try:
        price = Decimal(str(current.get('OPPORTUNITY')))
    except (InvalidOperation, TypeError):
        raise ProviderUnavailable('crm_invalid_price')
    if not price.is_finite() or price != 0 or current.get('TITLE') != project.name or current.get('CURRENCY_ID') != project.currency:
        return None
    offers = review.get('offers', [])
    for offer in offers:
        source = RawMessage.objects.get(pk=offer['source_id'])
        from .autonomous import _money_matches
        value = Decimal(str(offer['amount']))
        if source.config_id != config.id or source.processing_state in ('deleted', 'superseded') or offer['quote'] not in source.content or not value.is_finite() or value <= 0 or offer['currency'] != project.currency or not _money_matches(value, offer['quote'], monetary_unit=True):
            raise ValueError('price_question_offer_not_grounded')
    with transaction.atomic():
        WhatsAppConfig.objects.select_for_update().get(pk=config.id)
        locked = Project.objects.select_for_update().get(pk=project.id)
        if locked.version != project.version or locked.bitrix_id != project.bitrix_id or commercial_amount(locked) is not None:
            return None
        existing = FactClarification.objects.filter(kind='sale_amount', project=project).order_by('-id').first()
        if existing and (existing.state in ('queued', 'awaiting', 'answered', 'unconfirmed', 'resolved', 'expired') or existing.price_context.get('project_version') == project.version):
            return existing.id
        raw = RawMessage.objects.get(pk=evidence[0]['source_id'])
        trace = raw.traces.order_by('-id').first()
        if not trace:
            return None
        held = review.get('reason', '') if review.get('no_writes') else _human_hold(project, config)
        reason = review.get('reason') or 'В созданной из переписки сделке Bitrix сумма равна нулю. В обработанных сообщениях нет однозначной цены нашей продажи или нашего КП.'
        context = {'baseline': {key: current.get(key) for key in ('ID', 'TITLE', 'OPPORTUNITY', 'CURRENCY_ID', 'ORIGINATOR_ID', 'ORIGIN_ID')},
            'project_version': project.version, 'reason': reason, 'evidence': evidence, 'offers': offers,
            'review_key': review.get('review_key', ''), 'issue_kind': review.get('issue_kind', 'missing_price'),
            'question': review.get('question', ''), 'current_scope': review.get('current_scope', ''), 'no_writes': bool(held)}
        key = digest(['sale-price', project.id, project.version, evidence, offers])
        candidate = FactCandidate.objects.create(trace=trace, project=project, team=project.team, manager=project.manager,
            fact_type='project', proposed_changes={'fact_type': 'project', 'object_name': project.name, 'currency': project.currency,
                'evidence_message_id': raw.id, 'evidence': evidence[0]['quote']},
            base_project_version=project.version, source_key='price-question:'+key, status='pending', crm_match_state='matched')
        FactEvidence.objects.bulk_create([FactEvidence(candidate=candidate, raw_message_id=item['source_id'], quote=item['quote'], field_name='price_question_context') for item in evidence])
        decision = FactDecision.objects.create(candidate=candidate, outcome='deferred', actor_kind='system',
            policy_version=cfg.autonomous_policy_version, input_fingerprint=key, reason_code='missing_sale_amount',
            explanation=held or reason, validation={'price_question': True, 'review_key': context['review_key'], 'no_llm_extraction': True})
        question = FactClarification.objects.create(kind='sale_amount', project=project, price_context=context,
            candidate=candidate, decision=decision, config=config, recipient=recipient, request_key=key,
            subject_key=digest(['sale-price', config.id, project.id]), state='unconfirmed' if held else 'queued',
            source_snapshot={'session': config.session_name, 'group': config.group_jid, 'phone': UserProfile.objects.get(user=recipient).phone},
            candidate_snapshots={str(candidate.id): snapshot(candidate)}, expires_at=timezone.now()+timedelta(days=7),
            resolution={'external_writes_allowed': False, 'preserved_hold': held} if held else {})
        question.candidates.add(candidate)
        AuditEvent.objects.create(target_type='FactClarification', target_id=question.id, action='sale_price_question_created',
            before_after={'project_id': project.id, 'external_id': project.bitrix_id, 'review_key': context['review_key'], 'state': question.state})
        if not held:
            OutboxEvent.objects.get_or_create(deduplication_key=f'clarification-pump:{question.id}', defaults={'event_type': 'clarification_pump', 'payload': {}})
        return question.id
