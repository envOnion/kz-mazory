"""Fresh native wire projection; canonical requests and results remain literal."""

import copy

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

POLICY = 'quote-line-packets-v1'


class QuoteCodecError(ValueError):
    """The immutable request cannot safely describe or decode this wire value."""


_INSTRUCTION = (
    ' Native quote wire policy quote-line-packets-v1: content fields are lossless line packets. '
    'Read every line.text together with line_ending in order; their exact concatenation is the '
    'original source. whole_quote_id denotes that full concatenation. A line quote_id denotes '
    'only that adjacent line.text without its line_ending. Existing prior quote/evidence strings '
    'have adjacent quote_id/evidence_quote_id aliases. In response facts, enum-bound evidence and '
    'evidence_messages[].quote must be those integer IDs instead of literal strings. The server '
    'restores exact ordinary quote strings before unchanged evidence checks. Keep source IDs, '
    'roles and all other fields unchanged. No missing ID authorizes a new literal.'
)
_SCOPE_KEYS = {'$ref', '$dynamicRef', '$id', '$anchor', '$dynamicAnchor', '$recursiveRef'}
_ENDINGS = '\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029'


def _children(node, transform):
    """Only schema locations are traversed; literal values stay opaque."""
    if not isinstance(node, dict):
        return node
    result = dict(node)
    for name in ('properties', 'patternProperties', 'dependentSchemas', '$defs', 'definitions'):
        if isinstance(node.get(name), dict):
            result[name] = {key: transform(value) for key, value in node[name].items()}
    for name in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
        if isinstance(node.get(name), list):
            result[name] = [transform(value) for value in node[name]]
    for name in ('items', 'additionalItems', 'additionalProperties', 'unevaluatedItems',
                 'unevaluatedProperties', 'contains', 'propertyNames', 'contentSchema',
                 'not', 'if', 'then', 'else'):
        if isinstance(node.get(name), (dict, bool)):
            result[name] = transform(node[name])
        elif name == 'items' and isinstance(node.get(name), list):
            result[name] = [transform(value) for value in node[name]]
    return result


def _catalog(schema):
    fields = []

    def inspect(node):
        if not isinstance(node, dict):
            return node
        if _SCOPE_KEYS.intersection(node):
            raise QuoteCodecError('quote_codec_scoped_schema')
        for name, field in node.get('properties', {}).items():
            if name not in ('evidence', 'quote') or not isinstance(field, dict):
                continue
            values = field.get('enum')
            if isinstance(values, list) and values and all(isinstance(value, str) for value in values):
                validator = Draft202012Validator(field)
                accepted = frozenset(value for value in values if validator.is_valid(value))
                fields.append((field, accepted))
        return _children(node, inspect)

    inspect(schema)
    quotes = sorted(set().union(*(values for _, values in fields)))
    return fields, quotes


def _sources(inputs):
    if not isinstance(inputs.get('content'), str):
        raise QuoteCodecError('quote_codec_noncanonical_content')
    yield inputs, inputs.get('target_message_id'), '/content'
    for name in ('context', 'target_messages'):
        rows = inputs.get(name, [])
        if not isinstance(rows, list):
            raise QuoteCodecError('quote_codec_invalid_sources')
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get('content'), str):
                raise QuoteCodecError('quote_codec_noncanonical_content')
            yield row, row.get('raw_message_id'), f'/{name}/{index}/content'


def _resolve_refs(value, sources, ids):
    if isinstance(value, dict):
        if 'quote_ref' in value:
            pointer = value['quote_ref']
            source = sources.get(pointer) if isinstance(pointer, str) else None
            if (source is None or 'quote' in value
                    or type(value.get('raw_message_id')) is not int
                    or value['raw_message_id'] != source[1]
                    or source[0].get('partial')
                    or source[0]['content'] not in ids):
                raise QuoteCodecError('quote_codec_invalid_quote_ref')
            value['quote'] = source[0]['content']
            del value['quote_ref']
        for child in list(value.values()):
            _resolve_refs(child, sources, ids)
    elif isinstance(value, list):
        for child in value:
            _resolve_refs(child, sources, ids)


def _project(inputs, schema):
    fields, quotes = _catalog(schema)
    if not fields:
        return inputs, schema, []
    if not quotes:
        raise QuoteCodecError('quote_codec_empty_literal_domain')
    ids = {quote: index for index, quote in enumerate(quotes)}
    accepted = {id(field): values for field, values in fields}

    def encode(node):
        if id(node) in accepted:
            # Each finite domain is filtered with ALL original field constraints,
            # including nullable/length/pattern branches, before integer encoding.
            return {'type': 'integer', 'enum': sorted(ids[value] for value in accepted[id(node)])}
        return _children(node, encode)

    wire_schema = encode(schema)
    Draft202012Validator.check_schema(wire_schema)
    source_rows = list(_sources(inputs))
    sources = {path: (row, source_id) for row, source_id, path in source_rows}
    wire = copy.deepcopy(inputs)
    _resolve_refs(wire.get('analysis_state', {}), sources, ids)
    exposed = set()

    def annotate(value):
        if isinstance(value, dict):
            for name, child in list(value.items()):
                if name in ('quote', 'evidence') and isinstance(child, str) and child in ids:
                    alias = 'quote_id' if name == 'quote' else 'evidence_quote_id'
                    if alias in value:
                        raise QuoteCodecError('quote_codec_alias_collision')
                    value[alias] = ids[child]
                    exposed.add(ids[child])
                else:
                    annotate(child)
        elif isinstance(value, list):
            for child in value:
                annotate(child)

    annotate(wire.get('analysis_state', {}))
    for row, _, _ in _sources(wire):
        text = row['content']
        lines = []
        for part in text.splitlines(keepends=True):
            ending = '\r\n' if part.endswith('\r\n') else part[-1:] if part[-1:] in _ENDINGS else ''
            literal = part[:-len(ending)] if ending else part
            line = {'text': literal, 'line_ending': ending}
            if literal in ids:
                line['quote_id'] = ids[literal]
                exposed.add(ids[literal])
            lines.append(line)
        if ''.join(line['text'] + line['line_ending'] for line in lines) != text:
            raise QuoteCodecError('quote_codec_source_roundtrip')
        packet = {'lines': lines}
        if text in ids:
            packet['whole_quote_id'] = ids[text]
            exposed.add(ids[text])
        row['content'] = packet
    if exposed != set(ids.values()):
        raise QuoteCodecError('quote_codec_unexposed_literal')
    if 'quote_catalog_count' in wire:
        raise QuoteCodecError('quote_codec_catalog_collision')
    instruction = wire.get('analysis_instructions', '')
    if not isinstance(instruction, str):
        raise QuoteCodecError('quote_codec_invalid_instructions')
    wire['quote_catalog_count'] = len(quotes)
    wire['analysis_instructions'] = instruction + _INSTRUCTION
    return wire, wire_schema, quotes


def project(inputs, schema):
    """Project marked canonical inputs/schema without changing either argument."""
    if not isinstance(inputs, dict) or inputs.get('native_quote_policy') != POLICY:
        return inputs, schema
    try:
        wire, wire_schema, _ = _project(inputs, schema)
        return wire, wire_schema
    except QuoteCodecError:
        raise
    except (KeyError, AttributeError, TypeError, ValueError, SchemaError, ValidationError) as exc:
        raise QuoteCodecError('quote_codec_invalid_projection') from exc


def restore(result, canonical_inputs, canonical_schema):
    """Decode only native fact literals, then verify the ordinary schema again."""
    if not isinstance(canonical_inputs, dict) or canonical_inputs.get('native_quote_policy') != POLICY:
        return result
    try:
        _, wire_schema, quotes = _project(canonical_inputs, canonical_schema)
        if not quotes:
            return result
        Draft202012Validator(wire_schema).validate(result)
        canonical = copy.deepcopy(result)
        for fact in canonical.get('facts', []):
            for row, field in [(fact, 'evidence'), *[
                    (reference, 'quote') for reference in fact.get('evidence_messages', [])]]:
                if field not in row or isinstance(row[field], str):
                    continue
                index = row[field]
                if type(index) is not int or not 0 <= index < len(quotes):
                    raise QuoteCodecError('quote_codec_invalid_quote_id')
                row[field] = quotes[index]
        Draft202012Validator(canonical_schema).validate(canonical)
        return canonical
    except QuoteCodecError:
        raise
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, SchemaError, ValidationError) as exc:
        raise QuoteCodecError('quote_codec_invalid_result') from exc
