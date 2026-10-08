"""Bounded transformations of authorized aggregates, never SQL or model values."""
import uuid
from decimal import Decimal
from .data import fail, validate
from .answers import collect_facts

COMBINE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['mode', 'dataset_ids'],
    'properties': {
        'mode': {'enum': ['series', 'join', 'categories', 'append']},
        'dataset_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 4, 'uniqueItems': True},
        'keys': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 2},
        'labels': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 80}, 'maxItems': 4},
        'join': {'enum': ['left', 'full']},
        'category': {'type': 'string'},
        'groups': {'type': 'object', 'maxProperties': 100, 'additionalProperties': {'type': 'string', 'maxLength': 80}},
        'other': {'type': 'string', 'maxLength': 80},
    },
}


def combine(context, arguments):
    context.check()
    validate(COMBINE_SCHEMA, arguments)
    ids = arguments['dataset_ids']
    if any(pk not in context.registry for pk in ids):
        fail('unsupported_query')
    sources = [context.registry[pk] for pk in ids]
    if any(ds['truncated'] for ds in sources):
        fail('combine_requires_complete_groups')
    first = sources[0]
    mode = arguments['mode']
    rows, columns = [], []
    currencies = {ds['normalized_query'].get('currency') for ds in sources}
    zones = {ds['timezone'] for ds in sources}
    if len(currencies) != 1 or len(zones) != 1:
        fail('combine_incompatible_units')
    if mode == 'categories':
        if first['normalized_query'].get('aggregation', 'default') not in ('default', 'sum'):
            fail('combine_requires_aggregation')
        if len(sources) != 1 or not arguments.get('groups') or not arguments.get('category'):
            fail('invalid_tool_arguments')
        category = arguments['category']
        columns = first['columns']
        cat_col = next((c for c in columns if c['name'] == category), None)
        if not cat_col or cat_col['type'] != 'text' or any(c['type'] in ['id', 'date', 'percent'] for c in columns):
            fail('unsupported_query')
        dims = [c['name'] for c in columns if c['type'] == 'text']
        buckets = {}
        for original in first['rows']:
            row = {**original, category: arguments['groups'].get(str(original[category]), arguments.get('other', original[category]))}
            key = tuple(row[d] for d in dims)
            if key not in buckets:
                buckets[key] = row
            else:
                for col in columns:
                    if col['type'] not in ['count', 'money']:
                        continue
                    a, b = buckets[key][col['name']], row[col['name']]
                    total = Decimal(str(a)) + Decimal(str(b)) if a is not None and b is not None else None
                    buckets[key][col['name']] = None if total is None else int(total) if col['type'] == 'count' else str(total)
        rows = list(buckets.values())
    elif mode == 'append':
        columns = first['columns']
        if any(ds['columns'] != columns or ds['normalized_query'].get('dataset') != first['normalized_query'].get('dataset') for ds in sources):
            fail('combine_incompatible_units')
        ranges = sorted((ds['normalized_query'].get('date_range') or {} for ds in sources), key=lambda r: r.get('start', ''))
        if any(not r for r in ranges) or any(a['end_exclusive'] > b['start'] for a, b in zip(ranges, ranges[1:])):
            fail('combine_overlap')
        keys = [c['name'] for c in columns if c['type'] in ['date', 'id', 'text']]
        rows = [r for ds in sources for r in ds['rows']]
        if len({tuple(r[k] for k in keys) for r in rows}) != len(rows):
            fail('combine_overlap')
        rows.sort(key=lambda r: tuple((r[k] is None, str(r[k])) for k in keys))
    else:
        keys = arguments.get('keys', [])
        if not keys or len(sources) < 2:
            fail('invalid_tool_arguments')
        key_columns = [[next((c for c in ds['columns'] if c['name'] == key), None) for key in keys] for ds in sources]
        if any(any(c is None for c in cs) for cs in key_columns):
            # Different date prefixes can share the same bucket, explicitly mapped by position.
            if len(keys) != 1 or mode != 'series':
                fail('combine_invalid_keys')
            key_columns = [[c for c in ds['columns'] if c['type'] == 'date'] for ds in sources]
        if any(len(cs) != len(keys) or any(c['type'] not in ['id', 'date'] for c in cs) for cs in key_columns):
            fail('combine_invalid_keys')
        if any([c['type'] for c in cs] != [c['type'] for c in key_columns[0]] for cs in key_columns):
            fail('combine_invalid_keys')
        if any(c['type'] == 'id' and c['name'] not in ['project_id', 'team_id', 'profile_id'] for cs in key_columns for c in cs):
            fail('combine_invalid_keys')
        if mode == 'series':
            dates = {str(ds['normalized_query'].get('date_range')) for ds in sources}
            if len(dates) != 1 or any(c['type'] != 'date' for cs in key_columns for c in cs):
                fail('combine_incompatible_periods')
            # A monthly plan cannot silently become weekly data.
            grains = {c['name'].rsplit('_', 1)[-1] for cs in key_columns for c in cs}
            if not (len(grains) == 1 or grains <= {'month'}):
                fail('plan_requires_months')
        output_keys = [c['name'] for c in key_columns[0]]
        columns = key_columns[0]
        indexes = []
        measures_by_source = []
        for ds, cs in zip(sources, key_columns):
            key_names = [c['name'] for c in cs]
            if any(row[key] is None for row in ds['rows'] for key in key_names):
                fail('combine_invalid_keys')
            if any(c['name'] not in key_names and c['type'] not in ['money', 'count'] for c in ds['columns']):
                fail('combine_requires_aggregation')
            index = {tuple(r[k] for k in key_names): r for r in ds['rows']}
            if len(index) != len(ds['rows']):
                fail('combine_cardinality')
            indexes.append(index)
            measures_by_source.append([c for c in ds['columns'] if c['type'] in ['money', 'count']])
        if mode == 'series':
            if any(len(cs) != 1 for cs in measures_by_source):
                fail('combine_requires_aggregation')
            units = {(cs[0]['type'], cs[0]['unit']) for cs in measures_by_source}
            if len(units) != 1:
                fail('combine_incompatible_units')
            measure = measures_by_source[0][0]
            columns = [*columns, {'name': 'series', 'type': 'text', 'unit': None, 'label': 'Показатель'},
                       {'name': 'value', 'type': measure['type'], 'unit': measure['unit'], 'label': 'Значение'}]
            labels = arguments.get('labels') or [cs[0].get('label', 'Показатель') for cs in measures_by_source]
            if len(labels) != len(sources) or len(set(labels)) != len(labels):
                fail('invalid_tool_arguments')
            all_keys = set().union(*(set(index) for index in indexes))
            for i, (index, cs) in enumerate(zip(indexes, measures_by_source)):
                for key in sorted(all_keys, key=str):
                    row = index.get(key)
                    value = row[cs[0]['name']] if row else None
                    rows.append({**dict(zip(output_keys, key)), 'series': labels[i], 'value': value})
        else:
            measures = [c for cs in measures_by_source for c in cs]
            if len({c['name'] for c in measures}) != len(measures):
                fail('combine_ambiguous_measures')
            columns = [*columns, *measures]
            all_keys = set(indexes[0]) if arguments.get('join', 'left') == 'left' else set().union(*(set(index) for index in indexes))
            for key in sorted(all_keys, key=str):
                row = dict(zip(output_keys, key))
                for index, cs in zip(indexes, measures_by_source):
                    row.update({c['name']: index[key][c['name']] if key in index else None for c in cs})
                rows.append(row)
    if len(rows) > 1000 or len(columns) > 10:
        fail('dataset_limit_use_filters')
    ds = {**first, 'dataset_id': str(uuid.uuid4()), 'columns': columns, 'rows': rows,
          'normalized_query': {'dataset': 'combined', 'mode': mode, 'sources': [ds['normalized_query'] for ds in sources],
                               'transformation': arguments, 'currency': first['normalized_query'].get('currency'),
                               'date_range': first['normalized_query'].get('date_range')},
          'returned_count': len(rows), 'total_groups': len(rows), 'truncated': False,
          'coverage': {'status': 'complete' if all(ds['coverage']['status'] == 'complete' for ds in sources) else 'partial',
                       'message': 'Объединены агрегированные данные; отсутствующие значения оставлены неизвестными.'},
          'definition': 'Объединение после агрегации. ' + ' '.join(dict.fromkeys(ds['definition'] for ds in sources)),
          'evidence': list({r['id']: r for ds in sources for r in ds['evidence']}.values())[:20]}
    if mode == 'append':
        ds['normalized_query']['date_range'] = {'start': ranges[0]['start'], 'end_exclusive': ranges[-1]['end_exclusive']}
    context.facts.update(collect_facts(ds))
    context.registry[ds['dataset_id']] = ds
    context.check()
    return ds
