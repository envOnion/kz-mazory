"""Wire shape is derived from the same serializers as business validation."""

import copy
from rest_framework import serializers
from .facts import FactSchema
from .dialogue_threads import ThemeSchema, LinkSchema


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


def field_schema(field):
    if isinstance(field, serializers.ListSerializer):
        shape = {"type": "array", "items": serializer_schema(field.child)}
    elif isinstance(field, serializers.ListField):
        shape = {"type": "array", "items": field_schema(field.child)}
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
    elif isinstance(field, serializers.DecimalField):
        # Native constrained decoding reliably restricts JSON numbers. The
        # server converts them to Decimal and enforces the same 14/2 precision.
        shape = {"type": "number"}
    else:
        shape = {"type": "string"}
    if field.allow_null:
        shape = {"anyOf": [shape, {"type": "null"}]}
    return shape


def serializer_schema(serializer):
    return {"type": "object", "additionalProperties": False,
            "properties": {name: field_schema(field) for name, field in serializer.fields.items()},
            "required": [name for name, field in serializer.fields.items() if field.required]}


def extraction_schema(input_data=None):
    fact = serializer_schema(FactSchema())
    fact["required"] = sorted(set(fact["required"]) | {"thread_key", "evidence_message_id"})
    # Serializer defaults do not express cross-field validate() requirements.
    # Generate separate business variants so "payment" cannot silently omit
    # its amount, and a promise cannot omit the action/source/actor.
    common = {"thread_key", "evidence_message_id", "fact_type", "object_name", "company_name", "party_role", "evidence", "evidence_messages", "confidence", "uncertainties", "sender_phone", "in_progress"}
    extra = {
        "project": {"contract_amount", "cost_amount", "currency", "stage", "current_action", "next_action"},
        "payment": {"amount", "currency", "payment_date", "payment_kind", "direction", "amount_precision", "reverses_id"},
        "commitment": {"commitment_text", "responsible_name", "assignment_kind", "commitment_status", "promise_message_id", "deadline_at", "deadline_precision", "deadline_basis", "deadline_message_id", "fulfillment_message_id", "fulfilled_at", "commitment_id", "base_commitment_version"},
    }
    required = {
        "project": {"object_name", "company_name"},
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
            known["properties"]["amount_precision"]["enum"] = ["exact", "approximate", "range"]
            known["required"] = sorted(set(known["required"]) | {"amount"})
            variants.append(known)
            variant["properties"]["amount_precision"]["enum"] = ["unknown"]
        if kind == "commitment":
            variant["properties"]["evidence_messages"]["minItems"] = 1
        variants.append(variant)
    theme = serializer_schema(ThemeSchema())
    # Defaults are useful to old saved responses, but a new generation must
    # explicitly explain its classification rather than omit the reason.
    theme["required"] = sorted(set(theme["required"]) | {"completion_reason"})
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
        for variant in variants:
            variant["properties"]["thread_key"]["enum"] = [f"t{i}" for i in range(16)]
        classification = serializer_schema(LinkSchema())
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
                for item in node.values():
                    bind(item)
        bind(shape)
        for item, pk in pinned:
            item["properties"]["raw_message_id"]["enum"] = [pk]
    return shape
