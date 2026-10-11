"""Bounded repair context. No fixture expectations, speech, or bank values."""
import hashlib
import json
import math
import re

MAX_TRACE = 24
MAX_CONTEXTS = 8
SEMANTIC_FIELDS = ('interrupt', 'decision', 'identity_answer', 'scope_answer',
    'amount', 'date', 'amount_kind', 'events', 'source_intent', 'source_values')
EVENT_FIELDS = ('intent', 'scope', 'amount', 'date', 'amount_kind',
    'source_intent', 'source_values')
KINDS = ('null', 'boolean', 'number', 'string', 'object', 'array', 'invalid')


def value_kind(value):
    if value is None:
        return 'null'
    if type(value) is bool:
        return 'boolean'
    if type(value) in (int, float):
        return 'number' if type(value) is int or math.isfinite(value) else 'invalid'
    return {str: 'string', dict: 'object', list: 'array'}.get(type(value), 'invalid')


def _shape(value, fields):
    # Never stringify keys, objects, or source-specific semantic values.
    return {key: value_kind(value[key]) for key in fields if key in value}


def summarize_input(extractions):
    result = {'speech_event': 'absent'}
    if type(extractions) is not dict:
        return {'speech_event': 'invalid_extractions'}
    if 'speech_date' in extractions:
        result['speech_date_type'] = value_kind(extractions['speech_date'])
    if 'speech_event' not in extractions:
        return result
    encoded = extractions['speech_event']
    if type(encoded) is not str:
        return dict(result, speech_event='invalid_type')
    if len(encoded) > 20_000:
        return dict(result, speech_event='size_limit')
    def pairs(items):
        found = {}
        for key, value in items:
            if key in found:
                raise ValueError()
            found[key] = value
        return found
    def invalid(unused):
        raise ValueError()
    def finite(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError()
        return value
    try:
        event = json.loads(encoded, object_pairs_hook=pairs,
            parse_constant=invalid, parse_float=finite)
    except (ValueError, TypeError, RecursionError):
        return dict(result, speech_event='invalid_json')
    if type(event) is not dict:
        return dict(result, speech_event='non_object')
    result.update(speech_event='object', fields=_shape(event, SEMANTIC_FIELDS),
        additional_key_count=sum(key not in SEMANTIC_FIELDS for key in event))
    decision = event.get('decision')
    if type(decision) is str and decision in ('yes', 'no'):
        result['explicit_decision'] = decision
    events = event.get('events')
    if type(events) is list:
        result['events_count'] = len(events)
        result['events'] = [dict(fields=_shape(row, EVENT_FIELDS),
            additional_key_count=sum(key not in EVENT_FIELDS for key in row))
            if type(row) is dict else {'item_type': value_kind(row)}
            for row in events[:8]]
    return result


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _safe_shape(value, fields):
    if type(value) is not dict:
        return {}
    return {key: value[key] for key in fields if type(value.get(key)) is str
        and value[key] in KINDS}


def safe_input(value):
    """Rebuild the worker summary from a fixed vocabulary at the repair boundary."""
    if type(value) is not dict:
        return {'speech_event': 'unavailable'}
    state = value.get('speech_event')
    states = ('absent', 'invalid_extractions', 'invalid_type', 'size_limit',
        'invalid_json', 'non_object', 'object')
    result = {'speech_event': state if type(state) is str and state in states else 'unavailable'}
    kind = value.get('speech_date_type')
    if type(kind) is str and kind in KINDS:
        result['speech_date_type'] = kind
    if result['speech_event'] != 'object':
        return result
    result['fields'] = _safe_shape(value.get('fields'), SEMANTIC_FIELDS)
    for key in ('additional_key_count', 'events_count'):
        if _integer(value.get(key), 20_000):
            result[key] = value[key]
    decision = value.get('explicit_decision')
    if (result['fields'].get('decision') == 'string' and type(decision) is str
            and decision in ('yes', 'no')):
        result['explicit_decision'] = decision
    if type(value.get('events')) is list:
        result['events'] = []
        for row in value['events'][:8]:
            if type(row) is not dict:
                continue
            item = {'fields': _safe_shape(row.get('fields'), EVENT_FIELDS)}
            kind = row.get('item_type')
            if type(kind) is str and kind in KINDS:
                item = {'item_type': kind}
            if _integer(row.get('additional_key_count'), 20_000):
                item['additional_key_count'] = row['additional_key_count']
            result['events'].append(item)
    return result


def summarize_contexts(cases, workflow):
    """Bind observed node ordinals back to this exact generated graph's metadata."""
    nodes = workflow.get('nodes') if type(workflow) is dict else None
    if type(nodes) is not dict or len(nodes) > 180:
        return []
    names = sorted(nodes)
    def node_ref(index):
        if not _integer(index, len(names) - 1):
            return None
        name = names[index]
        ref = {'node_index': index}
        if type(name) is str and re.fullmatch(r'[A-Za-z0-9_]{1,80}', name):
            ref['node_id'] = name
            ref['source_path'] = '/nodes/' + name
        return ref
    contexts = []
    for case in cases:
        if type(case) is not dict or case.get('passed') is not False:
            continue
        observed = case.get('causal_observation')
        if type(observed) is not dict:
            continue
        context = {'controller_state': 'not_observed', 'input': safe_input(observed.get('input')),
            'trace': []}
        if _integer(observed.get('step_index'), 120):
            context['step_index'] = observed['step_index']
        for key in ('question_node', 'stopped_node'):
            ref = node_ref(observed.get(key))
            if ref is not None:
                context[key] = ref
        trace = observed.get('trace')
        for item in trace[-MAX_TRACE:] if type(trace) is list else []:
            if type(item) is not dict:
                continue
            source = node_ref(item.get('node'))
            if source is None:
                continue
            if item.get('event') == 'enter':
                context['trace'].append({'event': 'enter', 'node': source})
            elif item.get('event') == 'transition' and type(item.get('matched')) is bool:
                node = nodes[names[item['node']]]
                transitions = node.get('transitions') if type(node) is dict else None
                index = item.get('transition_index')
                if type(transitions) is not list or not _integer(index, len(transitions) - 1):
                    continue
                transition = transitions[index]
                target = transition.get('target_node') if type(transition) is dict else None
                target_index = names.index(target) if type(target) is str and target in names else None
                context['trace'].append({'event': 'transition', 'node': source,
                    'transition_index': index, 'matched': item['matched'],
                    'target': node_ref(target_index)})
        if type(observed.get('trace_truncated')) is bool:
            context['trace_truncated'] = observed['trace_truncated']
        if context not in contexts:
            contexts.append(context)
        if len(contexts) >= MAX_CONTEXTS:
            break
    return contexts


def remember_behavior(history, response_bytes, observations):
    """Only call with the freshly sanitized summary from a completed behavior run."""
    entry = {'response_sha256': hashlib.sha256(response_bytes).hexdigest(),
        'status': 'not_retested', 'observations': observations}
    return (history + [entry])[-2:]


def delimiter_locations(response):
    """Locate refused tokens in the previous draft; never include source snippets."""
    blocks = response.get('code_blocks') if type(response) is dict else None
    if type(blocks) is not list:
        return []
    result = []
    for index, block in enumerate(blocks[:180]):
        if type(block) is not dict or type(block.get('code')) is not str:
            continue
        code = block['code']
        if len(code) > 150_000:
            continue
        identifier = block.get('id')
        for match in re.finditer(r'\{\{|\}\}', code):
            row = {'code_block_index': index, 'line': code.count('\n', 0, match.start()) + 1,
                'column': match.start() - code.rfind('\n', 0, match.start()),
                'token': match.group()}
            if type(identifier) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier):
                row['code_block_id'] = identifier
            result.append(row)
            if len(result) == 16:
                return result
    return result
