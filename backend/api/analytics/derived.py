"""Decimal comparisons over registered full-result facts, never model numbers."""
import uuid
from decimal import Decimal
from .data import fail, validate
from .answers import formatted

DERIVED_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['operation', 'left', 'right', 'label'],
    'properties': {
        'operation': {'enum': ['difference', 'ratio_percent', 'change_percent']},
        'left': {'type': 'string', 'maxLength': 160}, 'right': {'type': 'string', 'maxLength': 160},
        'label': {'type': 'string', 'minLength': 1, 'maxLength': 80},
    },
}


def derive(context, arguments):
    context.check()
    validate(DERIVED_SCHEMA, arguments)
    left = context.facts.get(arguments['left'])
    right = context.facts.get(arguments['right'])
    if not left or not right or any(f['dataset_id'] not in context.registry for f in [left, right]):
        fail('unsupported_query')
    if left.get('type') != right.get('type') or left.get('unit') != right.get('unit'):
        fail('combine_incompatible_units')
    if left.get('type') not in ['money', 'count'] or left['formula'] != 'sum' or right['formula'] != 'sum':
        fail('unsupported_query')
    operation = arguments['operation']
    a, b = Decimal(str(left['value'])), Decimal(str(right['value']))
    if operation != 'difference' and b == 0:
        fail('zero_denominator')
    value = a - b if operation == 'difference' else a / b * 100 if operation == 'ratio_percent' else (a - b) / abs(b) * 100
    kind = left['type'] if operation == 'difference' else 'percent'
    unit = left.get('unit') if operation == 'difference' else '%'
    value = int(value) if kind == 'count' else str(value.quantize(Decimal('.01')))
    key = f'{left["dataset_id"]}:derived:{uuid.uuid4().hex}'
    fact = {'dataset_id': left['dataset_id'], 'measure': left['measure'], 'formula': operation,
            'operands': [arguments['left'], arguments['right']], 'value': value, 'display': formatted(value, kind, unit),
            'label': arguments['label'], 'type': kind, 'unit': unit,
            'scope': {'left': left['scope'], 'right': right['scope']}}
    context.facts[key] = fact
    context.check()
    return {'fact_key': key, 'fact': fact}
