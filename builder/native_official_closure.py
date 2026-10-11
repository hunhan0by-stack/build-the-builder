"""Official native closure graph construction, without dispatch or callbacks.

Brief A requires a farewell before the last close_call. The kit documents a
terminal that speaks its farewell and hangs up after speech, followed by the
engine-owned end_of_call lifecycle tool. It does not require the agent to
receive an SDK semantic receipt. Construction proves this documented graph
shape, not real playback, restart persistence or banking authorization.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

from .api_contract import BankContract
from .ingest import read_source
from .models import Document, FoundationError, strict_json


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
_REASONS = {
    'promise': (), 'paid': (),
    'transferred': ('restructuring', 'human', 'support'),
    'no_promise': ('refused', 'hardship', 'dispute', 'service_error'),
    'not_verified': ('failed', 'declined'),
    'not_reached': ('callback', 'no_callback', 'wrong_number', 'deceased', 'unclear'),
}
_QUOTES = {
    'brief': (
        '46. Every call ends with exactly one `close_call`, with an outcome and, where the table lists one, a reason. It is the last tool call.',
        '47. Thank the customer and say goodbye before `close_call`.',
        '| `promise` | none | at least one promise was recorded |',
        '| `paid` | none | rule 39 |',
        '| `transferred` | `restructuring`, `human`, `support` | rules 41, 42, 32 |',
        '| `no_promise` | `refused`, `hardship`, `dispute`, `service_error` | rules 31 and 45, 32, 40, 37 |',
        '| `not_verified` | `failed`, `declined` | rules 14, 15 |',
        '| `not_reached` | `callback`, `no_callback`, `wrong_number`, `deceased`, `unclear` | rules 6, 7, 8, 9, 3 |'),
    'workflow_format': (
        'An `end_call` node hangs up. It has a `terminal` object and no outgoing transitions.',
        'Default: hang up as soon as the agent stops speaking',
        'The farewell comes from the terminal, not from `on_enter`.'),
    'tools_and_code': (
        'The engine fires it at a fixed point of the call.',
        '| `end_of_call` | After the call has ended |',
        "Put a lifecycle tool that must run on every call into the workflow's top-level `tools` array.",
        'At the end of a call the order is: call analysis, then `end_of_call` tools, then success-criteria judging.',
        '- Declare it next to `url` and `method`: `"connect_retries": {"type": "number", "default": 0}`. It is not a `config` field.'),
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _references(documents):
    if not isinstance(documents, dict) or set(documents) != {*_QUOTES, 'base_agent'}:
        raise FoundationError('OFFICIAL_CLOSURE_SOURCE_SET_REQUIRED')
    refs = []
    for key, document in documents.items():
        if not isinstance(document, Document) or document.status != 'processed':
            raise FoundationError('OFFICIAL_CLOSURE_PROCESSED_SOURCE_REQUIRED')
        if not re.fullmatch(r'[0-9a-f]{64}', document.sha256):
            raise FoundationError('OFFICIAL_CLOSURE_SOURCE_HASH_REQUIRED')
        current = read_source(Path(document.path))
        if current.inventory() != document.inventory() or current.text != document.text:
            raise FoundationError('OFFICIAL_CLOSURE_SOURCE_CHANGED')
        for quote in _QUOTES.get(key, ()):
            start = document.text.find(quote)
            if start < 0 or document.text.find(quote, start + 1) >= 0:
                raise FoundationError('OFFICIAL_CLOSURE_EXACT_CONTRACT_MISSING')
            refs.append({'role': key, 'path': document.path, 'sha256': document.sha256,
                         **asdict(document.evidence(start, start + len(quote))), 'quote': quote})
    return refs


def _source_policy_references(documents, compiled, policy_documents, farewell, wording, source_scope):
    """Original typed policy authority plus docs-only platform mechanics.

    No practice brief/outcome table is supplied here. Rebuild the same typed
    decisions from their original hashes/spans before selecting closure codes.
    This finite grammar is not semantic certification of arbitrary prose.
    """
    from .compiled_policy import CompiledPolicy, lower_compilation
    from .semantic_renderer import validate_wording_bundle
    from compiler.typed_source import source_wording, TypedSourceProvider
    if not isinstance(documents, dict) or set(documents) != {'workflow_format', 'tools_and_code', 'base_agent'}:
        raise FoundationError('SOURCE_POLICY_CLOSURE_PLATFORM_SOURCE_SET_REQUIRED')
    if not isinstance(compiled, CompiledPolicy) or not isinstance(policy_documents, (list, tuple)) or not policy_documents:
        raise FoundationError('SOURCE_POLICY_CLOSURE_ORIGINAL_LOWERING_REQUIRED')
    if source_scope not in ('bounded-brief-requirements-1', 'bounded_explicit_typed_source'):
        raise FoundationError('SOURCE_POLICY_CLOSURE_BOUNDED_GRAMMAR_REQUIRED')
    refs = []
    by_id = {}
    for role, document in [('original_policy', doc) for doc in policy_documents] + list(documents.items()):
        if not isinstance(document, Document) or document.status != 'processed':
            raise FoundationError('SOURCE_POLICY_CLOSURE_PROCESSED_SOURCE_REQUIRED')
        current = read_source(Path(document.path))
        if current.inventory() != document.inventory() or current.text != document.text:
            raise FoundationError('SOURCE_POLICY_CLOSURE_SOURCE_CHANGED')
        if role == 'original_policy':
            if document.source_id in by_id:
                raise FoundationError('SOURCE_POLICY_CLOSURE_DUPLICATE_POLICY_SOURCE')
            by_id[document.source_id] = document
        else:
            for quote in _QUOTES.get(role, ()):
                start = document.text.find(quote)
                if start < 0 or document.text.find(quote, start + 1) >= 0:
                    raise FoundationError('OFFICIAL_CLOSURE_EXACT_CONTRACT_MISSING')
                refs.append({'role': role, 'path': document.path, 'sha256': document.sha256,
                             **asdict(document.evidence(start, start + len(quote))), 'quote': quote})
    if (compiled.coverage.get('unresolved_count') or compiled.coverage.get('unsupported_decisions')
            or not compiled.semantics.get('complete_for_supported_decisions')):
        raise FoundationError('SOURCE_POLICY_CLOSURE_RESOLVED_DECISIONS_REQUIRED')
    def ref_value(ref):
        doc = by_id.get(ref.source_id)
        if doc is None or not 0 <= ref.start < ref.end <= len(doc.text):
            raise FoundationError('SOURCE_POLICY_CLOSURE_ORIGINAL_EVIDENCE_REQUIRED')
        quote = doc.text[ref.start:ref.end]
        refs.append({'role': 'original_policy', 'path': doc.path, 'sha256': doc.sha256,
                     **asdict(ref), 'quote': quote})
        return {'source_id': doc.source_id, 'start': ref.start, 'end': ref.end, 'quote': quote}
    facts = [{'name': name, 'value': copy.deepcopy(fact.value), 'category': 'other',
              'evidence': [ref_value(ref) for ref in fact.evidence]}
             for name, fact in compiled.policy.facts.items()]
    # Reparse ORIGINAL raw source text, not merely the caller's internally
    # consistent IR. A changed effect retaining a legitimate quote must fail.
    if source_scope == 'bounded-brief-requirements-1':
        from .brief_requirements import derive_brief_requirements
        envelope = derive_brief_requirements(policy_documents, compiled.contract)['compilation']
    else:
        from compiler.core import make_request
        decisions = strict_json(TypedSourceProvider().generate(make_request(policy_documents, compiled.contract)))
        envelope = {'kind': 'source_checked_decision_ir', 'schema_version': 'compiler-envelope-1',
            'contract_sha256': compiled.contract.source_hash, 'sources': [doc.inventory() for doc in policy_documents],
            'decisions': decisions}
    rebuilt = lower_compilation(envelope, policy_documents, compiled.contract)
    if (rebuilt.policy.serializable() != compiled.policy.serializable() or rebuilt.constraints != compiled.constraints
            or rebuilt.semantics['decisions'] != compiled.semantics['decisions']):
        raise FoundationError('SOURCE_POLICY_CLOSURE_LOWERING_CHANGED')
    guards = {(row['target'], operation) for row in rebuilt.constraints if row['value'] is True
              for operation in row['operations']}
    required = {('identity_verified', 'get_account'), ('consent_current', 'create_promise'),
                ('amount_within_bounds', 'create_promise'), ('date_is_business_day', 'create_promise'),
                ('goodbye_spoken', 'close_call'), ('close_once_last', 'close_call')}
    if not required <= guards:
        raise FoundationError('SOURCE_POLICY_CLOSURE_MANDATORY_ORIGINAL_GUARDS_REQUIRED')
    actual_wording = validate_wording_bundle(wording, policy_documents)
    if actual_wording != source_wording(policy_documents) or actual_wording['goodbye']['template'] != farewell:
        raise FoundationError('SOURCE_POLICY_CLOSURE_ORIGINAL_FAREWELL_REQUIRED')
    ref = actual_wording['goodbye']['evidence']
    doc = by_id[ref['source_id']]
    refs.append({'role': 'original_policy_farewell', 'path': doc.path, 'sha256': doc.sha256, **ref})
    # Include the exact guard/override clauses as well as fact assignments.
    for decision in compiled.semantics['decisions']:
        for ref in decision['evidence']:
            doc = by_id[ref['source_id']]
            refs.append({'role': 'original_policy_rule', 'path': doc.path, 'sha256': doc.sha256, **ref})
    return refs


def compile_official_closure(documents, contract, *, outcome, customer_id=None,
                             customer_id_variable=None, reason=None, farewell='Thank you. Goodbye.',
                             source_policy=None, policy_documents=None, farewell_authority=None, source_scope=None):
    """Produce documented graph components; no component is sent or published.

    The caller must select outcome from actual bank results. This function only
    validates the Brief A table and bank wire schema, never infers that a promise
    happened. A graph consumer still owns banking safety and live acceptance.
    The default farewell is derived from rule 47, not claimed as a source quote.
    """
    generic = source_policy is not None
    if generic:
        if not isinstance(contract, BankContract):
            raise FoundationError('OFFICIAL_CLOSURE_BANK_CONTRACT_REQUIRED')
        refs = _source_policy_references(documents, source_policy, policy_documents, farewell, farewell_authority, source_scope)
        if source_policy.contract.source_hash != contract.source_hash:
            raise FoundationError('SOURCE_POLICY_CLOSURE_BANK_CONTRACT_MISMATCH')
    else:
        if policy_documents is not None or farewell_authority is not None or source_scope is not None:
            raise FoundationError('SOURCE_POLICY_CLOSURE_PARTIAL_AUTHORITY_FORBIDDEN')
        refs = _references(documents)
    if not isinstance(contract, BankContract):
        raise FoundationError('OFFICIAL_CLOSURE_BANK_CONTRACT_REQUIRED')
    runtime_customer = customer_id_variable is not None
    if runtime_customer:
        if customer_id is not None or customer_id_variable != 'customer_id':
            raise FoundationError('OFFICIAL_CLOSURE_EXCLUSIVE_DOCUMENTED_CUSTOMER_BINDING_REQUIRED')
        customer_value = '{{customer_id}}'
    else:
        if (not isinstance(customer_id, str) or not 1 <= len(customer_id) <= 160
                or any(char in customer_id for char in '{}\r\n')):
            raise FoundationError('OFFICIAL_CLOSURE_RESOLVED_CUSTOMER_REQUIRED')
        customer_value = customer_id
    reasons = _REASONS
    if generic:
        reasons = {row['outcome']: tuple(row['reasons']) for row in source_policy.policy.facts['outcome_table'].value}
    if not isinstance(outcome, str) or outcome not in reasons or (reason is not None if not reasons[outcome]
                                   else reason not in reasons[outcome]):
        raise FoundationError('OFFICIAL_CLOSURE_OUTCOME_REASON_MISMATCH')
    if (not isinstance(farewell, str) or not 1 <= len(farewell) <= 512
            or any(char in farewell for char in '{}\r\n')
            or not re.search(r'\bthank(?:s| you)\b', farewell, re.I)
            or not re.search(r'\b(?:goodbye|bye|have a good day)\b', farewell, re.I)):
        raise FoundationError('OFFICIAL_CLOSURE_THANK_AND_GOODBYE_REQUIRED')
    example = strict_json(documents['base_agent'].text)
    candidates = [tool for tool in example.get('tools', [])
                  if tool.get('function', {}).get('name') == 'close_call']
    if len(candidates) != 1:
        raise FoundationError('OFFICIAL_CLOSURE_UNIQUE_EXAMPLE_REQUIRED')
    example_tool = candidates[0]
    operation = contract.operations['close_call']
    try:
        if (example_tool['config']['alwaysRunsAt'] != ['end_of_call']
                or example_tool['function']['handler'] != '__function__:api_call'
                or example_tool['function']['parameters']['properties']['url']['default'] != operation.url
                or operation.method != 'POST'):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise FoundationError('OFFICIAL_CLOSURE_EXAMPLE_CONTRACT_MISMATCH') from None
    if (runtime_customer and example_tool['function']['parameters']['properties']['body']
            ['properties']['customer_id'].get('default') != '{{customer_id}}'):
        raise FoundationError('OFFICIAL_CLOSURE_RUNTIME_CUSTOMER_BINDING_NOT_DOCUMENTED')
    payload = {'customer_id': customer_value, 'outcome': outcome}
    if reason is not None:
        payload['reason'] = reason
    contract.validate_request('close_call', payload)
    lifecycle = copy.deepcopy(example_tool)
    lifecycle.pop('pre_process_code', None)  # static promise payload already omits reason
    lifecycle['config'] = {'timeoutMs': 12000, 'maxRetries': 0, 'alwaysRunsAt': ['end_of_call']}
    properties = lifecycle['function']['parameters']['properties']
    properties['body'] = {'type': 'object', 'required': list(payload),
                          'properties': {key: {'type': 'string', 'default': value}
                                         for key, value in payload.items()}}
    properties['connect_retries'] = {'type': 'number', 'default': 0}
    terminal = {'name': 'official_goodbye', 'node_type': 'end_call',
        'on_enter': {'type': 'silent'}, 'disable_inactivity_monitor': True, 'transitions': [],
        'terminal': {'type': 'function', 'function': {'name': 'official_farewell_end',
            'handler': '__function__:end_call', 'parameters': {'type': 'object', 'properties': {
                'message_mode': {'type': 'string', 'default': 'custom'},
                'spoken_message': {'type': 'string', 'default': farewell}}}}}}
    result = {'schema_version': 'official-native-closure-1', 'terminal_node': terminal,
        'lifecycle_tool_template': lifecycle, 'bank_payload': payload, 'source_references': refs,
        'bank_payload_kind': 'runtime_template_not_wire_payload' if runtime_customer else 'resolved_wire_payload',
        'runtime_bindings_required': ['customer_id'] if runtime_customer else [],
        'runtime_customer_scope_verified': False,
        'example_source_sha256': documents['base_agent'].sha256,
        'bank_contract_sha256': contract.source_hash,
        'farewell_origin': 'original_source_quoted_wording' if generic else 'derived_from_brief_rule_47',
        'documented_order': ['terminal_farewell', 'engine_hangup_after_speech', 'call_analysis',
                             'end_of_call_close_call', 'success_criteria_judging'],
        'sdk_semantic_receipt_required_by_official_brief': False,
        'sdk_semantic_receipt_verified': False, 'real_playback_verified': False,
        'native_atomic_persistence_verified': False, 'bank_dispatch_authorized': False,
        'decision': 'IMPLEMENTABLE', 'network_calls': 0}
    if generic:
        result['source_policy_authority'] = {'scope': 'original bounded typed rules/outcome codes/wording',
            'policy_sources': [doc.inventory() for doc in policy_documents],
            'outcome_table': copy.deepcopy(source_policy.policy.facts['outcome_table'].value),
            'practice_brief_or_public_fixture_used': False, 'full_arbitrary_prose_semantics_claim': False}
    result['components_sha256'] = _digest(result)
    return result


def install_official_terminal(workflow, components, *, node_name, include_close_tool=False):
    """Replace one inert closure placeholder; optionally construct the final tool.

    Default leaves bank closure inactive. include_close_tool only constructs a
    graph and is NOT a banking-safety authorization. It must remain unpublished
    until the controller's separate native-bank and live admission checks pass.
    No other end-of-call lifecycle tool is admitted, preserving close as last.
    """
    if (not isinstance(workflow, dict) or not isinstance(workflow.get('nodes'), dict)
            or not isinstance(node_name, str) or not _NAME.fullmatch(node_name)
            or node_name not in workflow['nodes'] or type(include_close_tool) is not bool):
        raise FoundationError('OFFICIAL_CLOSURE_SUPPORTED_GRAPH_REQUIRED')
    old = workflow['nodes'][node_name]
    if (not isinstance(old, dict) or old.get('node_type') not in {'conversation', 'end_call'}
            or old.get('on_enter') != {'type': 'silent'} or old.get('transitions', [])
            or old.get('tools', []) or old.get('pre_actions', [])):
        raise FoundationError('OFFICIAL_CLOSURE_INERT_PLACEHOLDER_REQUIRED')
    if (not isinstance(components, dict) or components.get('schema_version') != 'official-native-closure-1'
            or components.get('bank_dispatch_authorized') is not False):
        raise FoundationError('OFFICIAL_CLOSURE_COMPONENTS_REQUIRED')
    if components.get('components_sha256') != _digest({key: value for key, value in components.items()
                                                       if key != 'components_sha256'}):
        raise FoundationError('OFFICIAL_CLOSURE_COMPONENTS_CHANGED')
    close = components['lifecycle_tool_template']
    close_url = close['function']['parameters']['properties']['url']['default']
    # Inspect executable tool slots only, not descriptive text or parameter JSON.
    slots = [workflow.get('tools', [])]
    for group in ('nodes', 'global_nodes'):
        values = workflow.get(group, {})
        if not isinstance(values, dict):
            raise FoundationError('OFFICIAL_CLOSURE_UNSUPPORTED_NODE_CONTAINER')
        for node in values.values():
            if not isinstance(node, dict):
                raise FoundationError('OFFICIAL_CLOSURE_UNSUPPORTED_NODE')
            slots.extend([node.get('tools', []), node.get('pre_actions', [])])
            if 'terminal' in node:
                slots.append([node['terminal']])
    for tools in slots:
        if not isinstance(tools, list):
            raise FoundationError('OFFICIAL_CLOSURE_UNSUPPORTED_TOOL_CONTAINER')
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get('function'), dict):
                raise FoundationError('OFFICIAL_CLOSURE_UNSUPPORTED_TOOL')
            fn = tool['function']
            url = fn.get('parameters', {}).get('properties', {}).get('url', {}).get('default')
            if fn.get('name') == 'close_call' or url == close_url:
                raise FoundationError('OFFICIAL_CLOSURE_DUPLICATE_CLOSE_FORBIDDEN')
            config = tool.get('config', {})
            if (not isinstance(config, dict) or not isinstance(config.get('alwaysRunsAt', []), list)):
                raise FoundationError('OFFICIAL_CLOSURE_UNSUPPORTED_LIFECYCLE_CONFIG')
            if 'end_of_call' in config.get('alwaysRunsAt', []):
                raise FoundationError('OFFICIAL_CLOSURE_OTHER_END_TOOL_FORBIDDEN')
    result = copy.deepcopy(workflow)
    result['nodes'][node_name] = copy.deepcopy(components['terminal_node'])
    result['nodes'][node_name]['name'] = node_name
    if include_close_tool:
        result.setdefault('tools', []).append(copy.deepcopy(close))
    return result
