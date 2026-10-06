"""Wire shape is derived from the same serializers as business validation."""

from rest_framework import serializers
from .facts import FactSchema
from .dialogue_threads import ThemeSchema


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
        shape = {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"}
    else:
        shape = {"type": "string"}
    if field.allow_null:
        shape = {"anyOf": [shape, {"type": "null"}]}
    return shape


def serializer_schema(serializer):
    return {"type": "object", "additionalProperties": False,
            "properties": {name: field_schema(field) for name, field in serializer.fields.items()},
            "required": [name for name, field in serializer.fields.items() if field.required]}


def extraction_schema():
    fact = serializer_schema(FactSchema())
    fact["required"] = sorted(set(fact["required"]) | {"thread_key", "evidence_message_id"})
    return {"type": "object", "additionalProperties": False, "required": ["threads", "facts"],
            "properties": {"threads": {"type": "array", "items": serializer_schema(ThemeSchema())},
                           "facts": {"type": "array", "items": fact}}}
