"""Versioned, source-bound JSON transport; durable paging stays in the pipeline."""

import copy
import json

MODEL = 'gpt-sol-5-6'
POLICY = 'gpt-sol-json-v1'

INSTRUCTIONS = """Верни только окончательный JSON по output_schema. Без Markdown, вступления, имени модели и пояснений вне JSON.
Если есть threads, это массив тем с key t0, t1 и т.д.; messages — массив ссылок raw_message_id/thought_state/relation/rationale. Классифицируй все batch_message_ids (иначе target_message_id); context — источники, не новые цели. Для понятного сообщения допустимы ready, final и facts=[]. ready требует completion_reason и final. Не создавай факты из «ок/принято», вопросов, KPI или общих поручений без конкретного действия. Извлеки все конкретные события целевого сообщения, в том числе разные объекты отчёта.
Неизвестные деньги не равны нулю. КП не означает договор; плановая прибыль не означает оплату. Аванс без абсолютной суммы — payment с amount_precision=unknown без amount. Процент не является суммой в тенге. Не вычисляй сумму договора по проценту. Планы, просьбы, обещания, исполнения и отмены различай по источнику, автору и хронологии. Не выдумывай контрагента из имени автора.
evidence/quote — непустые краткие буквальные непрерывные цитаты указанного оригинала, прямо подтверждающие факт. Все источники факта должны быть в messages его темы. commitment требует цитаты promise; fulfilled — отдельного fulfillment; cancelled — отдельной более поздней cancellation. Сроки и объект подтверждай источниками. Предыдущее состояние — указатель, не новое доказательство; сохраняй ранее проверенные факты при сверке очередной порции. Выполни analysis_instructions и instruction текущей фазы, не объявляй весь разбор завершённым до покрытия источников сервером."""


def enabled(cfg):
    return cfg.chat_model_name == MODEL and getattr(cfg, 'chat_api_format', 'openai_compatible') == 'openai_compatible'


def request(payload, cfg):
    """Return the same wire for boundary counting and the HTTP adapter."""
    from .providers import ProviderUnavailable
    from .extraction_schema import compatible_extraction_schema, NATIVE_STRING_POLICY
    from .commercial_amounts import SALE_AMOUNT_RULES
    from .models import AISettings
    wire = copy.deepcopy(payload)
    users = [row for row in wire.get('messages', []) if row.get('role') == 'user' and isinstance(row.get('content'), str)]
    if len(users) != 1:
        raise ProviderUnavailable('provider_invalid_request')
    try:
        inputs = json.loads(users[0]['content'])
    except (TypeError, ValueError):
        raise ProviderUnavailable('provider_invalid_request') from None
    if not isinstance(inputs, dict):
        raise ProviderUnavailable('provider_invalid_request')
    if inputs.get('analysis_transport') == POLICY and isinstance(inputs.get('output_schema'), dict):
        return wire, inputs['output_schema']
    explicit = wire.get('response_format', {}).get('json_schema', {}).get('schema') or wire.get('_ollama_format')
    if explicit is not None:
        # Promise identity and status operations own their exact schema.
        schema = explicit
    else:
        inputs['native_string_policy'] = NATIVE_STRING_POLICY
        schema = compatible_extraction_schema(inputs, fixed_slots=False)
    configured = cfg.system_prompt_worker.strip()
    default = AISettings._meta.get_field('system_prompt_worker').get_default().strip()
    requirements = INSTRUCTIONS + SALE_AMOUNT_RULES
    from .ai_service import WORKER_PROMPT, PACKET_PROMPT
    original_instructions = '\n'.join(row.get('content', '') for row in wire.get('messages', []) if row.get('role') == 'system')
    if original_instructions and not any(base in original_instructions for base in (WORKER_PROMPT, PACKET_PROMPT)):
        # Typed identity/resolution/focused passes carry their own semantics.
        requirements = original_instructions + '\n' + requirements
    if configured and configured != default:
        requirements = configured + '\nОбязательный контракт Mazory:\n' + requirements
    inputs.update(analysis_transport=POLICY, output_requirements=requirements, output_schema=schema)
    users[0]['content'] = json.dumps(inputs, ensure_ascii=False, separators=(',', ':'))
    wire['messages'] = [{'role':'system', 'content':'Извлекай подтверждённые факты. Соблюдай output_requirements и output_schema из пользовательского сообщения.'}, users[0]]
    wire['model'] = cfg.chat_model_name
    for field in ('response_format', '_ollama_format', 'provider', 'plugins', 'models', 'reasoning', 'reasoning_effort', 'chat_template_kwargs'):
        wire.pop(field, None)
    return wire, schema
