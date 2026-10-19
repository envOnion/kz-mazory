"""Wire shape is derived from the same serializers as business validation."""

import copy
from rest_framework import serializers
from .facts import FactSchema
from .dialogue_threads import ThemeSchema, LinkSchema

WIRE_VERSION = 'facts-v7-laya-unique-sources'
COMPACT_SOURCE_WIRE_VERSION = 'facts-v8-compact-source-slots'
NATIVE_STRING_POLICY = 'serializer-charfield-v1'
NATIVE_CONFIDENCE_POLICY = 'serializer-confidence-range-v1'


def resolution_schema(input_data):
    """A status check cannot regenerate amounts, actors or promise text."""
    import json
    drafts = json.loads(input_data["content"])["drafts"]
    promises = sorted({item["promise_message_id"] for item in drafts})
    sources = sorted({row["raw_message_id"] for row in input_data["context"]})
    evidence = {"type":"object", "additionalProperties":False,
                "required":["raw_message_id","quote"], "properties":{
                    "raw_message_id":{"type":"integer","enum":sources}, "quote":{"type":"string"}}}
    status = {"type":"object", "additionalProperties":False,
              "required":["draft_index","promise_message_id","commitment_status","evidence_messages"], "properties":{
                  "draft_index":{"type":"integer","enum":list(range(len(drafts)))},
                  "promise_message_id":{"type":"integer","enum":promises},
                  "commitment_status":{"type":"string","enum":["pending","fulfilled"]},
                  "evidence_messages":{"type":"array","items":evidence}}}
    return {"type":"object", "additionalProperties":False, "required":["facts"],
            "properties":{"facts":{"type":"array","minItems":len(drafts),"maxItems":len(drafts),"items":status}}}


def field_schema(field, *, strict_strings=False):
    if isinstance(field, serializers.ListSerializer):
        shape = {"type": "array", "items": serializer_schema(field.child, strict_strings=strict_strings)}
    elif isinstance(field, serializers.ListField):
        shape = {"type": "array", "items": field_schema(field.child, strict_strings=strict_strings)}
    elif isinstance(field, serializers.ChoiceField):
        shape = {"type": "string", "enum": list(field.choices)}
    elif isinstance(field, serializers.BooleanField):
        shape = {"type": "boolean"}
    elif isinstance(field, serializers.IntegerField):
        shape = {"type": "integer"}
        if field.min_value is not None:
            shape["minimum"] = field.min_value
    elif isinstance(field, serializers.FloatField):
        shape = {"type": "number"}
    elif isinstance(field, serializers.DateTimeField):
        shape = {"type":"string", "pattern":r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$"}
    elif isinstance(field, serializers.DateField):
        shape = {"type":"string", "pattern":r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    elif isinstance(field, serializers.DecimalField):
        # Native constrained decoding reliably restricts JSON numbers. The
        # server converts them to Decimal and enforces the same 14/2 precision.
        shape = {"type": "number"}
    elif strict_strings and isinstance(field, serializers.CharField):
        shape = {"type": "string"}
        if field.max_length is not None:
            shape["maxLength"] = field.max_length
        if field.min_length is not None:
            shape["minLength"] = field.min_length
        if not field.allow_blank:
            shape["minLength"] = max(1, field.min_length or 0)
            if field.trim_whitespace:
                shape["pattern"] = r"\S"
        elif field.min_length:
            # DRF accepts an explicit blank before running length validators.
            shape = {"anyOf": [{"type": "string", "const": ""}, shape]}
    else:
        shape = {"type": "string"}
    if field.allow_null or (isinstance(field, serializers.DecimalField) and not field.required):
        # The business normalizer already treats optional null as unknown.
        # Let native decoding say unknown rather than force a guessed number.
        shape = {"anyOf": [shape, {"type": "null"}]}
    return shape


def serializer_schema(serializer, *, strict_strings=False):
    return {"type": "object", "additionalProperties": False,
            "properties": {name: field_schema(field, strict_strings=strict_strings) for name, field in serializer.fields.items()},
            "required": [name for name, field in serializer.fields.items() if field.required]}


def extraction_schema(input_data=None):
    strict_strings = isinstance(input_data, dict) and input_data.get('native_string_policy') == NATIVE_STRING_POLICY
    fact_serializer = FactSchema()
    fact = serializer_schema(fact_serializer, strict_strings=strict_strings)
    if isinstance(input_data, dict) and input_data.get('native_confidence_policy') == NATIVE_CONFIDENCE_POLICY:
        confidence = fact_serializer.fields['confidence']
        for keyword, limit in (('minimum', confidence.min_value), ('maximum', confidence.max_value)):
            if limit is not None:
                fact['properties']['confidence'][keyword] = limit
    fact["required"] = sorted(set(fact["required"]) | {"thread_key", "evidence_message_id", "object_name", "company_name", "confidence", "evidence_messages", "uncertainties"})
    # Serializer defaults do not express cross-field validate() requirements.
    # Generate separate business variants so "payment" cannot silently omit
    # its amount, and a promise cannot omit the action/source/actor.
    common = {"thread_key", "evidence_message_id", "fact_type", "object_name", "company_name", "party_role", "evidence", "evidence_messages", "confidence", "uncertainties", "sender_phone", "in_progress"}
    extra = {
        "project": {"contract_amount", "sale_amount", "cost_amount", "currency", "stage", "current_action", "next_action"},
        "payment": {"amount", "currency", "payment_date", "payment_kind", "direction", "amount_precision", "reverses_id"},
        "commitment": {"commitment_text", "responsible_name", "assignment_kind", "commitment_status", "promise_message_id", "deadline_at", "deadline_precision", "deadline_basis", "deadline_message_id", "fulfillment_message_id", "fulfilled_at", "commitment_id", "base_commitment_version"},
    }
    required = {
        "project": {"object_name", "company_name", "current_action"},
        "payment": {"amount_precision", "currency", "payment_kind", "direction", "payment_date"},
        "commitment": {"commitment_text", "responsible_name", "assignment_kind", "commitment_status", "promise_message_id", "evidence_messages", "deadline_at", "deadline_precision", "deadline_basis", "deadline_message_id", "fulfillment_message_id"},
    }
    variants = []
    for kind in extra:
        variant = copy.deepcopy(fact)
        variant["properties"] = {key:value for key,value in variant["properties"].items() if key in common | extra[kind]}
        variant["properties"]["fact_type"]["enum"] = [kind]
        variant["required"] = sorted(set(variant["required"]) | required[kind])
        if kind == "payment":
            known = copy.deepcopy(variant)
            if 'anyOf' in known['properties']['amount']:
                known['properties']['amount'] = known['properties']['amount']['anyOf'][0]
            known["properties"]["amount_precision"]["enum"] = ["exact", "approximate", "range"]
            known["required"] = sorted(set(known["required"]) | {"amount"})
            variants.append(known)
            variant["properties"]["amount_precision"]["enum"] = ["unknown"]
        if kind == "commitment":
            variant["properties"]["evidence_messages"]["minItems"] = 1
            evidence = variant['properties']['evidence_messages']['items']
            evidence['required'] = sorted(set(evidence['required']) | {'role'})
        if kind == 'commitment':
            for status_name in ('pending','fulfilled','cancelled'):
                version = copy.deepcopy(variant)
                version['properties']['commitment_status']['enum'] = [status_name]
                if status_name == 'fulfilled':
                    version['properties']['fulfillment_message_id'] = version['properties']['fulfillment_message_id']['anyOf'][0]
                    version['properties']['evidence_messages']['minItems'] = 2
                variants.append(version)
        else:
            variants.append(variant)
    if input_data is not None and input_data.get('analysis_fact_types') in (['commitment'], ['project'], ['payment']):
        variants[:] = [variant for variant in variants if variant['properties']['fact_type']['enum'] == input_data['analysis_fact_types']]
    theme = serializer_schema(ThemeSchema(), strict_strings=strict_strings)
    # Defaults are useful to old saved responses, but a new generation must
    # explicitly explain its classification rather than omit the reason.
    theme["required"] = sorted(set(theme["required"]) | {"completion_reason", "thread_id", "summary"})
    theme["properties"]["summary"]["minLength"] = 1
    theme["properties"]["messages"]["minItems"] = 1
    shape = {"type": "object", "additionalProperties": False, "required": ["threads", "facts"],
            "properties": {"threads": {"type": "array", "minItems": 1, "items": theme},
                           "facts": {"type": "array", "items": {"oneOf":variants}}}}
    if input_data is not None:
        # Object properties are emitted once by native constrained decoding.
        # A list of free-text keys allowed the same theme to be repeated.
        slot = copy.deepcopy(theme)
        slot["properties"].pop("key")
        slot["required"].remove("key")
        shape["properties"]["threads"] = {"type":"object", "additionalProperties":False,
            "required":["t0"], "properties":{f"t{i}":copy.deepcopy(slot) for i in range(16)}}
        for key, theme_slot in shape['properties']['threads']['properties'].items():
            theme_slot['properties']['parent_key'] = {'anyOf':[{'type':'string','enum':['',*[f't{i}' for i in range(16) if f't{i}'!=key]]},{'type':'null'}]}
        previous = input_data.get('analysis_state', {}).get('previous_result', {})
        previous_threads = previous.get('threads', []) if isinstance(previous, dict) else []
        previous_keys = list(previous_threads) if isinstance(previous_threads, dict) else [
            row.get('key') for row in previous_threads if isinstance(row, dict)] if isinstance(previous_threads, list) else []
        shape['properties']['threads']['required'] = sorted({'t0'} | {
            key for key in [*input_data.get('required_thread_keys', []), *previous_keys]
            if isinstance(key, str) and key in shape['properties']['threads']['properties']})
        for variant in variants:
            variant["properties"]["thread_key"]["enum"] = [f"t{i}" for i in range(16)]
        classification = serializer_schema(LinkSchema(), strict_strings=strict_strings)
        classification["properties"]["raw_message_id"] = {"type":"integer", "enum":[input_data["target_message_id"]]}
        classification["properties"]["thread_key"] = {"type":"string", "enum":["t0"]}
        classification["required"] = sorted(set(classification["required"]) | {"thread_key", "relation", "rationale"})
        targets = input_data.get("batch_message_ids") or [input_data["target_message_id"]]
        pinned = []
        if len(targets) == 1:
            shape["properties"]["target_classification"] = classification
            shape["required"].append("target_classification")
            pinned.append((classification, targets[0]))
        else:
            classifications = {"type":"object", "additionalProperties":False, "required":[], "properties":{}}
            for i, pk in enumerate(targets):
                item = copy.deepcopy(classification)
                if i:
                    item["properties"]["thread_key"]["enum"] = [f"t{j}" for j in range(16)]
                classifications["required"].append(f"c{i}")
                classifications["properties"][f"c{i}"] = item
                pinned.append((item, pk))
            shape["properties"]["target_classifications"] = classifications
            shape["required"].append("target_classifications")
        # Only complete originals actually present in this immutable request
        # can be evidence. Known-thread message IDs are retrieval hints.
        message_ids = {input_data["target_message_id"]}
        for row in input_data.get("context", []) + input_data.get("target_messages", []):
            if not row.get("partial") and type(row.get("raw_message_id")) is int:
                message_ids.add(row["raw_message_id"])
        threads = input_data.get("known_threads", [])
        thread_ids = [row["id"] for row in threads if type(row.get("id")) is int]
        commitment_ids = [item["id"] for row in threads for item in row.get("commitments", []) if type(item.get("id")) is int]
        message_ids.update(input_data.get('analysis_state', {}).get('read_source_ids', []))
        originals = [input_data.get('content','')] + [row.get('content','') for row in input_data.get('context',[]) + input_data.get('target_messages',[]) if not row.get('partial')]
        quotes = {part for original in originals if isinstance(original,str) for part in [original, *original.splitlines()] if part.strip() and len(part)<=32000}
        def previous_quotes(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in ('quote','evidence') and isinstance(item,str) and item.strip() and len(item)<=32000:
                        quotes.add(item)
                    else:
                        previous_quotes(item)
            elif isinstance(value,list):
                for item in value:
                    previous_quotes(item)
        previous_quotes(input_data.get('analysis_state',{}))
        if 'analysis_state' in input_data:
            message_ids.update(row['raw_message_id'] for row in input_data.get('context', []))
        raw_fields = {"raw_message_id", "evidence_message_id", "promise_message_id", "deadline_message_id", "fulfillment_message_id"}

        def bind(node):
            if isinstance(node, list):
                for item in node:
                    bind(item)
            elif isinstance(node, dict):
                for name, field in list(node.get("properties", {}).items()):
                    values = sorted(message_ids) if name in raw_fields else thread_ids if name == "thread_id" else commitment_ids if name == "commitment_id" else None
                    if values is not None:
                        if name == "commitment_id" and not values:
                            node["properties"].pop(name)
                            node["properties"].pop("base_commitment_version", None)
                            continue
                        if "anyOf" in field:
                            field["anyOf"][0]["enum"] = values
                            if not values:
                                field.clear(); field.update({"type":"null"})
                        else:
                            field["enum"] = values
                    if name in ('quote','evidence') and quotes:
                        field['enum'] = sorted(quotes)
                for item in node.values():
                    bind(item)
        bind(shape)
        for item, pk in pinned:
            item["properties"]["raw_message_id"]["enum"] = [pk]
        if len(message_ids) <= 200:
            # Fixed source slots cannot repeat a message with another rationale.
            # Keep the public and saved-response representation as a list.
            for key, theme_slot in shape['properties']['threads']['properties'].items():
                link = theme_slot['properties']['messages']['items']
                sources = {}
                for pk in sorted(message_ids):
                    item = copy.deepcopy(link)
                    if input_data.get('source_wire_version') == COMPACT_SOURCE_WIRE_VERSION:
                        # The slot already pins identity. Identical value schemas
                        # can share one definition instead of one per source ID.
                        item['properties'].pop('raw_message_id')
                        item['required'].remove('raw_message_id')
                    else:
                        item['properties']['raw_message_id'] = {'type':'integer','enum':[pk]}
                    sources[f'm{pk}'] = item
                theme_slot['properties']['messages'] = {'type':'object','additionalProperties':False,
                    'minProperties':1,'properties':sources,'required':[f'm{targets[0]}'] if key=='t0' else []}
        properties = shape['properties']
        classification_key = 'target_classifications' if len(targets)>1 else 'target_classification'
        shape['properties'] = {classification_key:properties[classification_key],
            'threads':properties['threads'],'facts':properties['facts']}
        if input_data.get('analysis_operation') == 'focused_fact_extraction':
            if input_data.get('analysis_payment_dates'):
                for variant in variants:
                    variant['properties']['payment_date'] = {'type':'string','enum':input_data['analysis_payment_dates']}
            if input_data.get('analysis_payment_kinds'):
                for variant in variants:
                    variant['properties']['payment_kind']['enum'] = input_data['analysis_payment_kinds']
            if input_data.get('analysis_amount_precision') == 'unknown':
                variants[:] = [variant for variant in variants if variant['properties']['amount_precision']['enum'] == ['unknown']]
                for variant in variants:
                    variant['properties']['amount'] = {'type':'null'}
            if input_data.get('analysis_fact_types') == ['commitment']:
                constrained = []
                for variant in variants:
                    for promise_id in input_data['eligible_promise_ids']:
                        pinned_variant = copy.deepcopy(variant)
                        for name in ('promise_message_id','evidence_message_id'):
                            pinned_variant['properties'][name]['enum'] = [promise_id]
                        pinned_variant['properties']['assignment_kind']['enum'] = ['promise','assignment']
                        constrained.append(pinned_variant)
                variants[:] = constrained
            for variant in variants:
                variant['properties']['thread_key']['enum'] = input_data['known_extraction_theme_keys']
                if input_data.get('analysis_fact_types') in (['project'], ['payment']):
                    variant['properties']['evidence_message_id'] = {'type':'integer','enum':[input_data['target_message_id']]}
                    target_quotes = sorted({text for text in [input_data['content'],*input_data['content'].splitlines()] if text.strip()})
                    variant['properties']['evidence'] = {'type':'string','enum':target_quotes}
            update = {'type':'object', 'additionalProperties':False, 'required':['thread_key','topic','summary','state'], 'properties':{
                'thread_key':{'type':'string','enum':input_data['known_extraction_theme_keys']}, 'topic':{'type':'string'},
                'summary':{'type':'string'}, 'state':{'type':'string','enum':['open','unknown']}}}
            shape = {'type':'object', 'additionalProperties':False, 'required':['facts','topic_updates'], 'properties':{
                'facts':shape['properties']['facts'], 'topic_updates':{'type':'array','items':update}}}
    return shape
