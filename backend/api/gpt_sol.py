"""Versioned, source-bound JSON transport; durable paging stays in the pipeline."""

import copy
import json

MODEL = 'gpt-sol-5-6'
POLICY = 'gpt-sol-json-v3'
LEGACY_POLICY = 'gpt-sol-json-v1'
LITERAL_POLICY = 'gpt-sol-json-v2'
REPAIRABLE_OUTPUT = {'provider_output_truncated', 'invalid_extraction_schema', 'extraction_json_parse',
                    'extraction_top_level_type', 'extraction_facts_missing', 'extraction_facts_type'}

INSTRUCTIONS = """Верни только окончательный JSON по output_schema. Без Markdown, вступления, имени модели и пояснений вне JSON.
Если есть threads, это массив тем с key t0, t1 и т.д.; messages — массив ссылок raw_message_id/thought_state/relation/rationale. Классифицируй все batch_message_ids (иначе target_message_id); context — источники, не новые цели. Для понятного сообщения допустимы ready, final и facts=[]. ready требует completion_reason и final. Не создавай факты из «ок/принято», вопросов, KPI или общих поручений без конкретного действия. Извлеки все конкретные события целевого сообщения, в том числе разные объекты отчёта.
Неизвестные деньги не равны нулю. КП не означает договор; плановая прибыль не означает оплату. Аванс без абсолютной суммы — payment с amount_precision=unknown без amount. Процент не является суммой в тенге. Не вычисляй сумму договора по проценту. Планы, просьбы, обещания, исполнения и отмены различай по источнику, автору и хронологии. Не выдумывай контрагента из имени автора.
Сроки «завтра/в пятницу» разрешай от sent_at и timezone источника, не от current_time. Не выдумывай время: день означает deadline_precision=date. responsible_name — доказанный исполнитель, не автоматически автор поручения. income/expense — направление денег относительно компании; поступление=increment, выставленный счёт=invoice, долг=debt, остаток=balance, будущее ожидание=promise. Различай повтор события и новый платёж.
evidence/quote — непустые краткие буквальные непрерывные цитаты указанного оригинала, прямо подтверждающие факт. Все источники факта должны быть в messages его темы. commitment требует цитаты promise; fulfilled — отдельного fulfillment; cancelled — отдельной более поздней cancellation. Сроки и объект подтверждай источниками. Предыдущее состояние — указатель, не новое доказательство; сохраняй ранее проверенные факты при сверке очередной порции. Выполни analysis_instructions и instruction текущей фазы, не объявляй весь разбор завершённым до покрытия источников сервером."""

QUOTE_ID_INSTRUCTIONS = """Обязательный формат доказательств этого запроса: evidence и evidence_messages[].quote — ЦЕЛЫЕ ЧИСЛА quote_id, whole_quote_id или evidence_quote_id из входа, а не текст цитаты. Это правило заменяет указания вернуть строковые evidence/quote. Выбирай ID точной строки либо полного пакета строк, подтверждающего поле. Сервер восстановит неизменённую цитату. Не составляй новый текст, не склеивай строки и не меняй ID источника или role. content.lines содержит полный исходный текст: читай text вместе с line_ending по порядку, включая все объекты отчёта. Отдельная строка имеет quote_id, полный текст — whole_quote_id; прежние проверенные цитаты имеют quote_id/evidence_quote_id. Сохрани все подтверждённые факты целей, а не только их пересказ в summary; facts=[] допустим только при отсутствии доказанных бизнес-событий."""


def enabled(cfg):
    return cfg.chat_model_name == MODEL and getattr(cfg, 'chat_api_format', 'openai_compatible') == 'openai_compatible'


def normalize_result(result):
    """Apply only the serializer's unknown optional values, never infer a fact."""
    from rest_framework import serializers
    from .facts import FactSchema, normalize_fact_fields
    from .extraction_schema import extraction_schema
    fields = FactSchema().fields
    variants = extraction_schema(fixed_slots=False)['properties']['facts']['items']['oneOf']
    allowed = {variant['properties']['fact_type']['enum'][0]: set(variant['properties'])
               for variant in variants}
    value = copy.deepcopy(result)
    normalized = set()
    for fact in value['facts']:
        if not isinstance(fact, dict):
            continue
        for name, item in list(fact.items()):
            field = fields.get(name)
            if (item is None and field is not None and fact.get('fact_type') in allowed
                    and name not in allowed[fact['fact_type']]):
                del fact[name]
                normalized.add(name)
                continue
            if item is None and field is not None and not field.required and not field.allow_null:
                normalized.add(name)
                if isinstance(field, serializers.CharField) and field.allow_blank:
                    fact[name] = ''
    value['facts'] = normalize_fact_fields(value['facts'])
    return value, sorted(normalized)


def normalize_grounded_fields(result, diagnostics):
    """Keep supported events, but never promote an inferred optional value."""
    import re
    from .commercial_amounts import contract_price_evidence
    from .deduplication import normalize_deal_name
    for index, fact in enumerate(result['facts']):
        quotes = [ref['quote'] for ref in fact.get('evidence_messages', [])]
        quotes.append(fact['evidence'])
        removed = []
        if (fact['fact_type'] == 'project' and fact.get('contract_amount') is not None
                and not contract_price_evidence(fact['contract_amount'], quotes)):
            del fact['contract_amount']
            removed.append(('contract_amount', 'contract_price_not_in_evidence',
                            'Сумма договора не названа в доказательствах; цена продажи не является суммой договора.'))
        company = normalize_deal_name(fact.get('company_name', ''))
        if company and not re.search(r'(?<!\w)' + re.escape(company) + r'(?!\w)',
                                     normalize_deal_name('\n'.join(quotes), max_length=None)):
            fact['company_name'], fact['party_role'] = '', 'unknown'
            removed.append(('company_name', 'company_not_in_evidence',
                            'Наименование контрагента не подтверждено цитатами.'))
        for field, reason, explanation in removed:
            entry = {'fact_index': index, 'field': field, 'reason': reason}
            entries = diagnostics.setdefault('unsupported_optional_fields', [])
            if entry not in entries:
                entries.append(entry)
            uncertainties = fact.setdefault('uncertainties', [])
            if explanation not in uncertainties and len(uncertainties) < 20:
                uncertainties.append(explanation)


def _explicit_schema(payload):
    response_format = payload.get('response_format')
    container = response_format.get('json_schema') if isinstance(response_format, dict) else None
    schema = container.get('schema') if isinstance(container, dict) else None
    return schema or payload.get('_ollama_format')


def _quote_plan(inputs):
    """Build a fresh uninterned literal contract before lossless ID projection."""
    from .providers import ProviderUnavailable
    from .extraction_schema import extraction_schema, NATIVE_STRING_POLICY
    from .quote_input_codec import POLICY as QUOTE_POLICY, QuoteCodecError, project
    from jsonschema.exceptions import SchemaError, ValidationError

    canonical = copy.deepcopy(inputs)
    canonical['native_string_policy'] = NATIVE_STRING_POLICY
    canonical['native_quote_policy'] = QUOTE_POLICY
    try:
        schema = extraction_schema(canonical, literal_quote_enums=True,
                                   fixed_slots=False, focused_quote_enums=True)
        projected, wire_schema = project(canonical, schema)
    except (QuoteCodecError, KeyError, TypeError, ValueError, AttributeError,
            SchemaError, ValidationError):
        raise ProviderUnavailable('context_snapshot_mismatch', diagnostics={
            'quote_codec_error': 'invalid_canonical_projection'}) from None
    return canonical, schema, projected, wire_schema


def restore_result(result, canonical_payload, cfg):
    """Restore v3 integer evidence after wire validation, before business checks.

    canonical_payload must be the untouched payload captured immediately before
    request(); v1/v2 and explicit operation schemas keep their literal results.
    Regenerating the plan binds IDs to that request, without saved mutable state.
    """
    if not enabled(cfg) or _explicit_schema(canonical_payload) is not None:
        return result
    from .providers import ProviderUnavailable
    from .quote_input_codec import QuoteCodecError, restore
    from jsonschema.exceptions import SchemaError, ValidationError

    try:
        users = [row for row in canonical_payload.get('messages', [])
                 if isinstance(row, dict) and row.get('role') == 'user'
                 and isinstance(row.get('content'), str)]
        if len(users) != 1:
            return result
        inputs = json.loads(users[0]['content'])
    except (TypeError, ValueError):
        return result
    if not isinstance(inputs, dict) or inputs.get('analysis_transport', POLICY) != POLICY:
        return result
    if type(inputs.get('target_message_id')) is not int:
        return result
    canonical, schema, _, _ = _quote_plan(inputs)
    try:
        return restore(result, canonical, schema)
    except (QuoteCodecError, KeyError, TypeError, ValueError, AttributeError,
            SchemaError, ValidationError):
        raise ProviderUnavailable('invalid_extraction_schema', diagnostics={
            'quote_codec_error': 'invalid_quote_id_result'}) from None


def request(payload, cfg):
    """Return the same wire for boundary counting and the HTTP adapter."""
    from .providers import ProviderUnavailable
    from .extraction_schema import compatible_extraction_schema, NATIVE_STRING_POLICY
    from .commercial_amounts import SALE_AMOUNT_RULES
    from .models import AISettings
    wire = copy.deepcopy(payload)
    users = [row for row in wire.get('messages', []) if row.get('role') == 'user' and isinstance(row.get('content'), str)]
    if len(users) != 1:
        return wire, None
    try:
        inputs = json.loads(users[0]['content'])
    except (TypeError, ValueError):
        return wire, None
    if not isinstance(inputs, dict):
        return wire, None
    explicit = _explicit_schema(wire)
    policies = (POLICY, LEGACY_POLICY, LITERAL_POLICY)
    if (inputs.get('analysis_transport') in policies
            and isinstance(inputs.get('output_schema'), dict)):
        # Encoded v3 content is a line packet, not a string. Recognize its
        # immutable inline contract before the ordinary extraction shape check.
        return wire, inputs['output_schema']
    if explicit is None and (type(inputs.get('target_message_id')) is not int or not isinstance(inputs.get('content'), str)):
        # Chat answers and analytics share the counter, not this extraction wire.
        return wire, None
    policy = inputs.get('analysis_transport', POLICY)
    if policy not in policies:
        raise ProviderUnavailable('context_configuration_changed')
    if isinstance(inputs.get('output_schema'), dict):
        return wire, inputs['output_schema']
    if explicit is not None:
        # Promise identity and status operations own their exact schema.
        schema = explicit
    elif policy == POLICY:
        from .free_router import compact_schema
        _, _, inputs, projected_schema = _quote_plan(inputs)
        schema = compact_schema(projected_schema)
    else:
        inputs['native_string_policy'] = NATIVE_STRING_POLICY
        schema = compatible_extraction_schema(inputs, fixed_slots=False,
                                              focused_quote_enums=policy == LEGACY_POLICY)
    configured = cfg.system_prompt_worker.strip()
    default = AISettings._meta.get_field('system_prompt_worker').get_default().strip()
    requirements = INSTRUCTIONS + SALE_AMOUNT_RULES
    from .ai_service import WORKER_PROMPT, PACKET_PROMPT
    original_instructions = '\n'.join(row.get('content', '') for row in wire.get('messages', []) if row.get('role') == 'system')
    base = next((base for base in sorted((WORKER_PROMPT, PACKET_PROMPT), key=len, reverse=True)
                 if base in original_instructions), None)
    if base is not None:
        suffix = original_instructions.split(base, 1)[1].replace(SALE_AMOUNT_RULES, '')
        requirements += '\n' + suffix
    elif original_instructions:
        # Typed identity/resolution/focused passes carry their own semantics.
        requirements = original_instructions + '\n' + requirements
    if configured and configured != default:
        requirements = configured + '\nОбязательный контракт Mazory:\n' + requirements
    if policy == POLICY and explicit is None:
        requirements += '\n' + QUOTE_ID_INSTRUCTIONS
    inputs.update(analysis_transport=policy, output_requirements=requirements, output_schema=schema)
    users[0]['content'] = json.dumps(inputs, ensure_ascii=False, separators=(',', ':'))
    wire['messages'] = [{'role':'system', 'content':'Извлекай подтверждённые факты. Соблюдай output_requirements и output_schema из пользовательского сообщения.'}, users[0]]
    wire['model'] = cfg.chat_model_name
    for field in ('response_format', '_ollama_format', 'provider', 'plugins', 'models', 'reasoning', 'reasoning_effort', 'chat_template_kwargs'):
        wire.pop(field, None)
    return wire, schema
