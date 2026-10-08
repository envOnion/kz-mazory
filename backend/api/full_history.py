"""Durable full-source reads; every generation carries explicit verified state."""

import copy
import hashlib
from django.db import transaction
from django.utils import timezone
from .context_tokens import canonical_json, context_runtime, extraction_payload, payload_hash
from .message_context import history_queryset, source_scope
from .message_time import source_metadata, source_zone
from .models import MessageProcessingTrace, OutboxEvent, RawMessage, HistoryAnalysisItem
from .plain_text import plain_text
from .providers import ProviderUnavailable

POLICY = 'thread-context-v2'


def compact_result(result, targets):
    """Compress repeated links, keeping summaries and every fact's literal sources.

    Complete responses and original links remain immutable in stage traces. This
    is only the next request's continuity index, never replacement evidence.
    """
    if not result:
        return None
    value = copy.deepcopy(result)
    for theme in value.get('threads', []):
        required = set(targets)
        for fact in value.get('facts', []):
            if fact['thread_key'] == theme['key']:
                required.update(ref['raw_message_id'] for ref in fact.get('evidence_messages', []))
                required.add(fact.get('evidence_message_id'))
        links = theme['messages']
        required.update(link['raw_message_id'] for link in links[-8:])
        required.update(link['raw_message_id'] for link in links
                        if link.get('relation') in ('answers', 'clarifies', 'cancels', 'fulfills'))
        theme['messages'] = [link for link in links if link['raw_message_id'] in required]
    return value


def state_parts(result, targets, counter, limit):
    """Hierarchical reconciliation pages; each new original visits every page."""
    value = compact_result(result, targets)
    if not value or counter.count_text(canonical_json(value)) <= limit:
        return [value]
    groups = []
    keyed = {theme['key']:theme for theme in value['threads']}
    for theme in value['threads']:
        facts = [fact for fact in value['facts'] if fact['thread_key'] == theme['key']]
        # Split a large theme by fact identities, repeating its continuity
        # summary and sources. No fact/quote is discarded to obtain a fit.
        for fact_group in ([facts] if not facts else [[fact] for fact in facts]):
            linked = {ref['raw_message_id'] for fact in fact_group for ref in fact.get('evidence_messages', [])} | set(targets)
            selected = [link for link in theme['messages'] if link['raw_message_id'] in linked]
            selected = selected or theme['messages'][-1:]
            item = {**theme, 'messages': selected}
            relatives, parent = [item], theme.get('parent_key')
            while parent and parent in keyed:
                ancestor = keyed[parent]
                relatives.append({**ancestor, 'messages':ancestor['messages'][-1:]})
                parent = ancestor.get('parent_key')
            groups.append({'threads': relatives, 'facts': fact_group})
    pages = []
    for group in groups:
        if pages and counter.count_text(canonical_json(merge_results([pages[-1], group]))) <= limit:
            pages[-1] = merge_results([pages[-1], group])
        else:
            pages.append(group)
    return pages


def merge_results(values):
    """Join independently validated pages by theme and original promise identity.

    A page returns the current status, rather than an append-only copy of the
    earlier promise. The final server validation still checks all original
    evidence, chronology, revisions and deduplication before publication.
    """
    result = {'threads': [], 'facts': []}
    themes, facts = {}, {}
    for value in values:
        if not value:
            continue
        keys = {}
        for theme in value['threads']:
            identity = (theme.get('thread_id'), theme['topic'].casefold())
            key = 'theme_' + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:24]
            keys[theme['key']] = key
            if key not in themes:
                themes[key] = {**copy.deepcopy(theme), 'key': key, 'parent_key': None}
            else:
                saved = themes[key]
                links = {link['raw_message_id']: link for link in saved['messages']}
                links.update({link['raw_message_id']: link for link in theme['messages']})
                saved.update({k: v for k, v in theme.items() if k not in ('key', 'messages', 'parent_key')})
                saved['messages'] = list(links.values())
        for theme in value['threads']:
            if theme.get('parent_key') in keys:
                themes[keys[theme['key']]]['parent_key'] = keys[theme['parent_key']]
        for fact in value['facts']:
            identity = (fact['fact_type'], fact.get('promise_message_id') or fact.get('evidence_message_id'),
                        fact.get('commitment_text', '') if fact['fact_type'] == 'commitment' else fact.get('object_name', ''))
            facts[identity] = {**copy.deepcopy(fact), 'thread_key': keys[fact['thread_key']]}
    result['threads'], result['facts'] = list(themes.values()), list(facts.values())
    return result


def budget(cfg):
    native = cfg.context_window_tokens - cfg.max_completion_tokens - cfg.context_safety_tokens
    return min(native, cfg.analysis_input_token_limit) if cfg.analysis_input_token_limit else native


def row(message):
    source = source_metadata(message)
    return {'raw_message_id': message.id, 'message_id': message.message_id,
            'source_revision': message.source_revision, 'content': plain_text(message.content),
            'sender_name': source['sender'], 'timestamp': source['sent_at'],
            'received_at': message.received_at.isoformat(), 'source_metadata': source,
            'order_time': (message.timestamp if message.sent_at_known else message.received_at).isoformat(),
            'partial': False}


def snapshot(raw, maximum):
    qs, _ = history_queryset(raw, maximum, include_following=True)
    messages = list(qs.select_related('config').order_by('order_time', 'id')) + [raw]
    messages.sort(key=lambda m: (m.timestamp if m.sent_at_known else m.received_at, m.id))
    return [row(message) for message in messages]


def source_hash(rows):
    return payload_hash([[r['raw_message_id'], r['source_revision'], r['source_metadata'], r['received_at'],
                          hashlib.sha256(r['content'].encode()).hexdigest()] for r in rows])


def prepare(raw, cfg, known_projects, maximum, batch_ids, state):
    """Select a chronological chunk using complete provider token counts."""
    counter, endpoint = context_runtime(cfg)
    rows = snapshot(raw, maximum)
    digest = source_hash(rows)
    if state.get('source_hash') and state['source_hash'] != digest:
        raise ProviderUnavailable('context_snapshot_mismatch')
    targets = set(batch_ids or [raw.id])
    available = budget(cfg)
    chunk_limit = min(available, getattr(cfg, 'analysis_chunk_input_limit', None) or available)
    by_id = {r['raw_message_id']: r for r in rows}
    if not targets <= by_id.keys():
        raise ProviderUnavailable('context_source_unavailable')
    source = source_metadata(raw)
    page = copy.deepcopy(state.get('page', {}))
    parts = page.get('parts') or sorted(state_parts(state.get('result'), targets, counter, min(chunk_limit // 3, cfg.max_completion_tokens // 2)),
                                      key=lambda value: counter.count_text(canonical_json(value)), reverse=True)
    part_index = page.get('index', 0)
    prior = parts[part_index]
    ranges = copy.deepcopy(state.get('ranges', {}))
    previous_source_ids = [int(pk) for pk in ranges]
    validated_read = sum(not item['content'] or ranges.get(str(item['raw_message_id']), 0) >= len(item['content']) for item in rows)
    cursor = state.get('cursor', 0)
    offset = state.get('offset', 0)
    final = state.get('phase') == 'reconcile'
    # Compact catalog hints; canonical source text and literal evidence stay intact.
    texts = ' '.join(by_id[pk]['content'] for pk in targets).casefold()
    known_projects = [p for p in known_projects if p['name'].casefold() in texts][:20]
    from .dialogue_threads import context_threads
    themes = context_threads(raw)
    from .dialogue_threads import source_key
    from .models import DialogueThread
    required_ids = {theme['thread_id'] for theme in (prior or {}).get('threads', []) if theme.get('thread_id')}
    offered_ids = {theme['id'] for theme in themes}
    for theme in DialogueThread.objects.filter(source_key=source_key(raw), pk__in=required_ids-offered_ids).exclude(state='superseded'):
        themes.append({'id':theme.id, 'version':theme.version, 'topic':theme.topic, 'state':theme.state,
                       'parent_id':theme.parent_id, 'project_id':theme.project_id, 'commitments':[],
                       'message_ids':list(theme.message_links.values_list('raw_message_id', flat=True))})
    themes = [{k: v for k, v in theme.items() if k != 'summary'} for theme in themes]
    prepared_at = state.get('analysis_time') or timezone.now().astimezone(source_zone(raw)).isoformat()
    target_text = by_id[raw.id]['content']
    target_range = [0, len(target_text)]
    # A long single target is read in portions before reconciliation.
    target_offset = ranges.get(str(raw.id), 0)
    target_done = target_offset >= len(target_text)
    if final or target_done:
        if prior and counter.count_text(target_text) > chunk_limit // 3:
            target_text = '\n'.join(ref['quote'] for fact in prior.get('facts', [])
                                    for ref in fact.get('evidence_messages', []) if ref['raw_message_id'] == raw.id)
            target_range = None
    elif counter.count_text(target_text) > chunk_limit // 3:
        end = min(len(target_text), target_offset + max(256, chunk_limit))
        while end > target_offset + 1 and counter.count_text(target_text[target_offset:end]) > chunk_limit // 3:
            end = target_offset + (end - target_offset) // 2
        target_text, target_range = target_text[target_offset:end], [target_offset, end]
    if page.get('target_range'):
        target_range = page['target_range']
        target_text = by_id[raw.id]['content'][target_range[0]:target_range[1]]
    target_rows = [by_id[pk] for pk in targets if pk != raw.id]
    reference_ids = {ref['raw_message_id'] for fact in (prior or {}).get('facts', [])
                     for ref in fact.get('evidence_messages', [])}
    reference_ids.update(fact['evidence_message_id'] for fact in (prior or {}).get('facts', []) if fact.get('evidence_message_id'))
    reference_ids.update(link['raw_message_id'] for theme in (prior or {}).get('threads', [])
                         for link in theme['messages'] if theme['state'] == 'open'
                         or link.get('relation') in ('answers', 'clarifies', 'cancels', 'fulfills'))
    carry = [by_id[pk] for pk in sorted(reference_ids - targets) if pk in by_id]

    def request(items):
        active_ids = {r['raw_message_id'] for r in items}
        payload = extraction_payload(cfg, endpoint, target_text, source['sender'],
            target_rows + [r for r in carry if r['raw_message_id'] not in active_ids] + items,
            known_projects, source['sent_at'], source['timezone'], source_metadata=source,
            current_time=prepared_at, target_message_id=raw.id, known_threads=themes,
            batch_message_ids=list(batch_ids or []))
        message = payload['messages'][-1]
        import json
        value = json.loads(message['content'])
        value['analysis_state'] = {'previous_result': prior, 'read_source_ids': previous_source_ids,
                'reconciliation_page': part_index + 1, 'reconciliation_pages': len(parts),
                'phase': 'reconcile' if final else 'read',
                'instruction': 'Это промежуточный результат, не новые доказательства. Сверь его с очередными оригиналами. Обнови связи, переносы сроков, отмены и исполнения; не сохраняй противоречащий прежний статус. Верни единый актуальный результат для целей. Факты требуют точных цитат ранее прочитанных или текущих оригиналов. Не теряй существующие факты без объяснения в теме.'}
        value['analysis_instructions'] += ' Сжимай непродуктивные обсуждения в summary с ID незавершённых просьб и их смыслом. Не перечисляй каждую нейтральную реплику в выходе. Сохраняй целевые сообщения, обещания, запросы, переносы, отмены, исполнения и источники фактов. Верни актуальное состояние previous_result после сверки с оригиналами этой части. Каждый запрос содержит очередную страницу состояния; остальные страницы проверяются отдельно с теми же новыми оригиналами. evidence — буквальная цитата evidence_message_id, не пересказ. Для обязательства обязательно evidence_messages с role=promise и ID исходного обещания; для исполнения — role=fulfillment и fulfillment_message_id; для отмены — role=cancellation и более поздняя буквальная цитата отмены. Готовый и отправленный результат означает fulfilled, не cancelled. Ссылайся deadline_message_id на актуальное изменение срока, а не первоначальный срок. Используй короткие достаточные непрерывные цитаты, не копируй целиком длинные исходники.'
        if final:
            value['analysis_instructions'] += ' Все оригиналы snapshot уже прочитаны по этапам. Выполни окончательное сведение: просьба сама по себе не обещание; проверь авторство, отмены, переносы и исполнения через evidence_messages. Сводка служит указателем, а не доказательством.'
        from .context_tokens import extraction_input
        message['content'] = extraction_input(value)
        return payload

    chunk_budget = int(chunk_limit * .9) if len(parts) > 1 else chunk_limit
    fixed = counter.count_payload(request([]))
    if fixed > chunk_budget and carry:
        # Exact evidence excerpts keep identity/date/author provenance without
        # repeatedly spending a window on already read long originals.
        carry = [{**item, 'content': '\n'.join(dict.fromkeys(ref['quote'] for fact in prior.get('facts', [])
                  for ref in fact.get('evidence_messages', []) if ref['raw_message_id'] == item['raw_message_id'])) or item['content'],
                  'evidence_excerpt': True} for item in carry]
        fixed = counter.count_payload(request([]))
    if fixed > available:
        raise ProviderUnavailable('context_fixed_input_too_large', diagnostics={
            'input_tokens': fixed, 'available_input_tokens': available,
            'user_input_token_limit': cfg.analysis_input_token_limit,
        })
    # A scheduling cap reduces newly read originals, never the actual model
    # window or already verified evidence. Leave room for at least a fragment.
    chunk_budget = min(available, max(chunk_budget, fixed + min(512, available-fixed)))
    selected = []
    empty = sum(not r['content'] for r in rows)
    if target_range:
        ranges[str(raw.id)] = max(target_offset, target_range[1])
    for pk in targets - {raw.id}:
        ranges[str(pk)] = len(by_id[pk]['content'])
    if page:
        selected = page['selected']
        ranges, cursor, offset = page['ranges'], page['cursor'], page['offset']
    elif not final:
        pending = [(index, item) for index, item in enumerate(rows[cursor:], cursor)
                   if item['raw_message_id'] not in targets and item['content']
                   and ranges.get(str(item['raw_message_id']), 0) < len(item['content'])]
        candidates = [{**item, 'content': item['content'][ranges.get(str(item['raw_message_id']), 0):]}
                      for _, item in pending]
        # One full-window probe, then logarithmic boundary probes. Calling a
        # remote count endpoint once per original exhausts its request budget.
        low, high = 0, len(candidates)
        while low < high:
            middle = (low + high + 1) // 2
            if counter.count_payload(request(candidates[:middle])) <= chunk_budget:
                low = middle
            else:
                high = middle - 1
        selected = candidates[:low]
        for _, item in pending[:low]:
            ranges[str(item['raw_message_id'])] = len(item['content'])
        if low:
            cursor, offset = pending[low-1][0] + 1, 0
        elif pending:
            cursor, item = pending[0]
            pk = str(item['raw_message_id'])
            start = ranges.get(pk, 0)
            low, high, accepted = start + 1, len(item['content']), start
            while low <= high:
                middle = (low + high) // 2
                current = {**item, 'content': item['content'][start:middle], 'partial': True,
                           'included_character_range': [start, middle], 'original_characters': len(item['content'])}
                if counter.count_payload(request([current])) <= chunk_budget:
                    accepted, low = middle, middle + 1
                else:
                    high = middle - 1
            if accepted == start:
                raise ProviderUnavailable('context_fixed_input_too_large')
            selected.append({**item, 'content': item['content'][start:accepted], 'partial': True,
                             'included_character_range': [start, accepted], 'original_characters': len(item['content'])})
            ranges[pk], offset = accepted, accepted
        else:
            cursor, offset = len(rows), 0
    payload = request(selected)
    input_tokens = counter.count_payload(payload)
    if input_tokens > available:
        raise ProviderUnavailable('context_batch_too_large', diagnostics={'input_tokens': input_tokens, 'available_input_tokens': available})
    read_ids = [r['raw_message_id'] for r in rows if not r['content'] or ranges.get(str(r['raw_message_id']), 0) >= len(r['content'])]
    done = len(read_ids) == len(rows)
    request_context = target_rows + [r for r in carry if r['raw_message_id'] not in {item['raw_message_id'] for item in selected}] + selected
    metadata = {
        'schema_version': 1, 'source': 'chat_history', 'policy_version': POLICY,
        'full_history_policy': POLICY, 'model': cfg.chat_model_name, 'provider': endpoint['tag'],
        'api_format': endpoint['api_format'], 'effective_provider_url': endpoint['effective_provider_url'],
        'token_counter': counter.strategy, 'snapshot_max_id': maximum, 'input_serialization': 'target-last-v1',
        'known_threads': themes, 'batch_message_ids': list(batch_ids or []), 'analysis_time': prepared_at,
        'target_source_metadata': source, 'includes_following_messages': True, 'time_basis': 'order_time',
        'window_tokens': cfg.context_window_tokens, 'completion_reserve_tokens': cfg.max_completion_tokens,
        'safety_tokens': cfg.context_safety_tokens, 'effective_input_budget': available,
        'user_input_token_limit': cfg.analysis_input_token_limit, 'fixed_input_tokens': fixed,
        'analysis_chunk_input_limit': getattr(cfg, 'analysis_chunk_input_limit', None),
        'history_budget_tokens': available-fixed, 'input_tokens_preflight': input_tokens,
        'payload_sha256': payload_hash(payload), 'request_state': 'prepared',
        'available_messages_count': len(rows), 'included_messages_count': len(request_context),
        'fully_included_messages_count': sum(not r.get('partial') for r in request_context),
        'partial_messages_count': sum(bool(r.get('partial')) for r in selected),
        'omitted_messages_count': len(rows) - len(read_ids), 'empty_messages_count': empty,
        'history_complete_in_request': done and not state.get('stage_trace_ids'),
        'full_history': {'source_hash': digest, 'total': len(rows), 'read': len(read_ids), 'ranges': ranges,
                         'validated_read':validated_read,
                         'cursor': cursor, 'offset': offset, 'phase': 'reconcile' if final else 'read',
                         'complete': done and (final or not state.get('stage_trace_ids')) and part_index + 1 == len(parts),
                         'stage': len(state.get('stage_trace_ids', []))+1,
                         'source_ids': [r['raw_message_id'] for r in rows], 'target_range': target_range},
        'included_period': [selected[0]['order_time'], selected[-1]['order_time']] if selected else [None, None],
        'external_import_completeness': 'unknown', 'relevance_selected': False,
    }
    metadata['token_breakdown'] = {'instructions_target_catalog_state': fixed, 'history': input_tokens-fixed}
    if len(parts) > 1:
        metadata['full_history']['reconciliation_page'] = part_index + 1
        metadata['full_history']['reconciliation_pages'] = len(parts)
        metadata['page_plan'] = {'parts': parts, 'index': part_index, 'results': page.get('results', []),
                                 'selected': selected, 'ranges': ranges, 'cursor': cursor, 'offset': offset,
                                 'target_range': target_range}
    return request_context, metadata, payload


def validate_evidence(result, raw, trace):
    """Summaries cannot authorize a quote outside the already read originals."""
    coverage = trace.context_metadata.get('full_history', {}).get('ranges', {})
    if not coverage:
        return
    from .facts import FactSchema, normalize_fact_fields
    result['facts'] = normalize_fact_fields(result.get('facts'))
    FactSchema(data=result.get('facts'), many=True).is_valid(raise_exception=True)
    from .pipeline import _source_quote
    references = [ref for fact in result.get('facts', []) for ref in fact.get('evidence_messages', [])]
    anchors = []
    for fact in result.get('facts', []):
        matched = [ref for ref in fact.get('evidence_messages', [])
                   if ref['raw_message_id'] == fact.get('evidence_message_id') == fact.get('promise_message_id')
                   and ref['role'] == 'promise'] if fact['fact_type'] == 'commitment' else []
        if len(matched) == 1:
            # Some models put a narrative summary in the redundant evidence
            # field. Bind it to their own explicit promise citation; that quote
            # still has to match the original read prefix below.
            if fact.get('evidence') != matched[0]['quote']:
                trace.context_metadata.setdefault('schema_repairs', []).append('literal_promise_anchor_from_reference')
            fact['evidence'] = matched[0]['quote']
        if fact.get('evidence_message_id') and fact.get('evidence'):
            anchors.append({'raw_message_id':fact['evidence_message_id'], 'quote':fact['evidence']})
    references.extend(anchors)
    originals = {m.id: m for m in source_scope(raw).filter(pk__in=[r['raw_message_id'] for r in references])}
    for ref in references:
        message = originals.get(ref['raw_message_id'])
        end = coverage.get(str(ref['raw_message_id']), 0)
        if message is None or not end:
            raise ProviderUnavailable('fact_thread_evidence_missing')
        _source_quote(ref['quote'], plain_text(message.content)[:end])
    from .dialogue_threads import normalize_themes
    result['threads'] = normalize_themes(result.get('threads'))
    if not isinstance(result['threads'], list) or any(not isinstance(theme, dict) or 'key' not in theme
                                                    or not isinstance(theme.get('messages'), list)
                                                    or any(not isinstance(link, dict) or type(link.get('raw_message_id')) is not int
                                                           for link in theme['messages']) for theme in result['threads']):
        raise ProviderUnavailable('thread_classification_invalid')
    keyed = {theme['key']:theme for theme in result['threads']}
    relations = {'promise':'answers', 'deadline':'clarifies', 'fulfillment':'fulfills', 'cancellation':'cancels'}
    multiple = result.get('target_classifications', {})
    classified = [result.get('target_classification')] + (list(multiple.values()) if isinstance(multiple, dict) else [])
    for fact in result.get('facts', []):
        theme = keyed.get(fact.get('thread_key'))
        if theme is None:
            continue
        links = {link['raw_message_id']:link for link in theme['messages']}
        for ref in fact.get('evidence_messages', []):
            pk, relation = ref['raw_message_id'], relations.get(ref['role'], 'discusses')
            if pk not in links:
                links[pk] = {'raw_message_id':pk, 'thought_state':'intermediate', 'relation':relation,
                             'rationale':'Источник проверенной цитаты этого факта.'}
                trace.context_metadata.setdefault('schema_repairs', []).append('literal_fact_source_link')
            elif ref['role'] in relations and links[pk].get('relation') != relation:
                links[pk]['relation'] = relation
                trace.context_metadata.setdefault('schema_repairs', []).append('fact_source_role_alignment')
            for item in classified:
                if isinstance(item, dict) and item.get('raw_message_id') == pk and item.get('thread_key') == fact.get('thread_key'):
                    item['relation'] = links[pk]['relation']
        theme['messages'] = list(links.values())


def extract(payload):
    from .pipeline import extract_message
    from .processing_attempts import reserve_attempt
    from .ai_service import usage_event_id, outbox_claim
    event_id = usage_event_id.get()
    if not event_id:
        raise ProviderUnavailable('analysis_claim_expired')
    event = OutboxEvent.objects.get(pk=event_id)
    trace = MessageProcessingTrace.objects.get(pk=payload['trace_id'])
    state = copy.deepcopy(event.payload.get('full_history_state', {}))
    if trace.context_metadata.get('request_state') == 'segment_validated':
        # A worker may stop after validation but before advancing the cursor.
        value = (trace.context_metadata['segment_result'], trace.context_metadata.get('segment_usage', {}),
                 trace.context_metadata.get('segment_diagnostics', {}))
    else:
        value = extract_message(payload['raw_id'], trace_id=trace.id,
            requested_by_id=payload.get('requested_by_id'), batch_ids=payload.get('batch_ids'),
            trace_ids=payload.get('trace_ids'), full_state=state, defer_result=True,
            commitment_refresh=bool(payload.get('commitment_refresh') and not state.get('stage_trace_ids')))
        if value is None:
            return
        trace.refresh_from_db()
    coverage = trace.context_metadata['full_history']
    if source_hash(snapshot(trace.raw_message, trace.context_metadata['snapshot_max_id'])) != coverage['source_hash']:
        raise ProviderUnavailable('context_snapshot_mismatch')
    batch_ids = payload.get('batch_ids') or trace.context_metadata.get('batch_message_ids')
    stages = state.get('stage_trace_ids', []) + [trace.id]
    plan = trace.context_metadata.get('page_plan')
    if plan:
        results = plan['results'] + [value[0]]
        if plan['index'] + 1 == len(plan['parts']):
            value = (merge_results(results), value[1], value[2])
    complete = coverage['complete']
    if complete:
        trace.context_metadata['full_history']['stage_trace_ids'] = stages
        trace.save(update_fields=['context_metadata'])
        return extract_message(payload['raw_id'], trace_id=trace.id,
            requested_by_id=payload.get('requested_by_id'), batch_ids=batch_ids,
            trace_ids=payload.get('trace_ids'), full_state=state, result_override=value)
    next_state = {k: coverage[k] for k in ['source_hash', 'ranges', 'cursor', 'offset']}
    next_state.update(result=value[0], stage_trace_ids=stages,
                      analysis_time=trace.context_metadata['analysis_time'],
                      phase='reconcile' if coverage['read'] == coverage['total'] else 'read')
    if plan and plan['index'] + 1 < len(plan['parts']):
        next_state.update(result=state.get('result'), phase=state.get('phase', 'read'),
                          page={**plan, 'index': plan['index']+1, 'results': results})
    with transaction.atomic():
        current = OutboxEvent.objects.select_for_update().get(pk=event_id)
        if current.state != 'processing' or current.payload.get('claim_generation') != outbox_claim.get() or current.payload.get('history_cancelled'):
            raise ProviderUnavailable('analysis_claim_expired')
        raw = RawMessage.objects.select_for_update().get(pk=payload['raw_id'])
        following = reserve_attempt(raw, f'{current.deduplication_key}:full-stage:{len(stages)}')
        following.context_metadata.update({k: trace.context_metadata[k] for k in ['analysis_policy', 'analysis_limits', 'snapshot_max_id', 'full_history_policy', 'analysis_chunk_input_limit']})
        if trace.context_metadata.get('replace_unsent'):
            following.context_metadata['replace_unsent'] = True
        following.context_metadata['full_history'] = {**coverage, 'phase': next_state['phase'], 'complete': False}
        following.save(update_fields=['context_metadata'])
        members = [entry for entry in payload.get('trace_ids', []) if entry['raw_id'] != raw.id]
        members.append({'raw_id': raw.id, 'trace_id': following.id})
        current.payload = {**current.payload, 'full_history_state': next_state, 'trace_id': following.id,
                           'trace_ids': members, 'batch_ids':batch_ids, 'schema_repaired': False}
        current.save(update_fields=['payload'])
        HistoryAnalysisItem.objects.filter(outbox_event=current, raw_message_id=raw.id).update(trace=following)
    return {'history_continuation': True}
