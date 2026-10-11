"""Deterministic compiler for an explicit, bounded textual policy grammar.

This is not an arbitrary natural-language or model interpretation claim. Every
recognized rule retains its exact source span; unrecognized policy lines fail.
"""
from __future__ import annotations

import json
import re

from builder.models import FoundationError, strict_json


GUARDS = {
    'Before account read require identity_verified.': ('identity_verified', 'get_account'),
    'Before promise write require consent_current.': ('consent_current', 'create_promise'),
    'Before promise write require date_is_business_day.': ('date_is_business_day', 'create_promise'),
    'Before promise write require amount_within_bounds.': ('amount_within_bounds', 'create_promise'),
    'Before close require goodbye_spoken.': ('goodbye_spoken', 'close_call'),
    'Before close require close_once_last.': ('close_once_last', 'close_call'),
}


class TypedSourceProvider:
    """Read supported explicit rules, including evidenced amendment overrides."""
    calls = 0
    model_calls = 0
    network_calls = 0

    def generate(self, request):
        rules, facts, bindings = [], {}, []
        active = {}
        for source in request['sources']:
            offset = 0
            for raw in source['text'].splitlines(keepends=True):
                line = raw.strip()
                start = offset + len(raw) - len(raw.lstrip())
                offset += len(raw)
                if not line or re.match(r'^(From|To|Date|Subject|Message-ID):', line):
                    continue
                if line.startswith('Wording '):
                    if not re.fullmatch(r'Wording [a-z_]+: .+', line):
                        raise FoundationError('Malformed source wording definition')
                    continue  # Renderer independently validates these exact quotes.
                ref = {'source_id': source['source_id'], 'start': start,
                       'end': start + len(line), 'quote': line}
                amendment = line.startswith('This amendment ')
                match = re.fullmatch(r'Parameter ([a-z_]+) is (.+)\.', line)
                if amendment:
                    match = re.fullmatch(r'This amendment overrides Parameter ([a-z_]+) with (.+)\.', line)
                operation = None
                if match:
                    name, value = match.group(1), strict_json(match.group(2))
                    kind, target = 'set', 'policy.' + name
                    facts[name] = {'name': name, 'value': value, 'category': 'other', 'evidence': [ref]}
                elif line in GUARDS:
                    target, operation = GUARDS[line]
                    kind, value = 'require', True
                else:
                    amount = re.fullmatch(r'(?:Payment amount ceiling is |This amendment replaces the payment ceiling with )([0-9]+(?:\.[0-9]+)?)\.', line)
                    date = re.fullmatch(r'(?:Payment date must be on or before |This amendment replaces the latest payment date with )(\d{4}-\d{2}-\d{2})\.', line)
                    if amount:
                        kind, target, value, operation = 'limit', 'payment.amount', strict_json(amount.group(1)), 'create_promise'
                    elif date:
                        kind, target, value, operation = 'require', 'payment.date_on_or_before', date.group(1), 'create_promise'
                    else:
                        raise FoundationError('Unsupported policy sentence in bounded typed-source compiler')
                key = (target, operation)
                previous = active.get(key)
                if amendment and not previous:
                    raise FoundationError('Amendment must explicitly override an existing supported rule')
                if previous and not amendment:
                    raise FoundationError('Duplicate base assignment is ambiguous')
                identifier = 'rule-' + str(len(rules) + 1)
                priority = previous['priority'] + 1 if previous else 0
                rule = {'id': identifier, 'priority': priority,
                        'conditions': [{'fact': 'operation', 'operator': 'eq', 'value': operation}] if operation else [],
                        'effects': [{'kind': kind, 'target': target, 'value': value}],
                        'exceptions': [], 'overrides': [previous['id']] if amendment else [], 'evidence': [ref]}
                rules.append(rule)
                active[key] = rule
        identity = facts.get('identity_order')
        if not identity:
            raise FoundationError('Source identity order missing')
        bindings.append({'role': 'identity_check', 'operation': 'verify_identity', 'direction': 'request',
                         'field_path': 'field', 'allowed_values': identity['value'], 'evidence': identity['evidence']})
        return json.dumps({'schema_version': 'decision-ir-1', 'rules': rules,
                           'facts': list(facts.values()), 'api_bindings': bindings, 'unresolved': []}, allow_nan=False)


def source_wording(documents):
    """Extract explicit wording templates; no inferred or fallback wording."""
    bundle = {}
    for doc in documents:
        offset = 0
        for raw in doc.text.splitlines(keepends=True):
            line = raw.strip()
            match = re.fullmatch(r'Wording ([a-z_]+): (.+)', line)
            if line.startswith('Wording ') and not match:
                raise FoundationError('Malformed source wording definition')
            if match:
                kind, template = match.groups()
                if kind in bundle:
                    raise FoundationError('Ambiguous duplicate wording definition')
                start = offset + raw.index(template)
                bundle[kind] = {'template': template, 'evidence': {'source_id': doc.source_id,
                                'start': start, 'end': start + len(template), 'quote': template}}
            offset += len(raw)
    return bundle
