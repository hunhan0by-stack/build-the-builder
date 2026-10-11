"""Content-derived entry for explicit numbered policies and amendment material.

This finite grammar is not a general prose interpreter. Source names, ordering,
email senders and dates confer no authority. Every assignment keeps its original
Unicode-text span; a memo must name an unambiguous supported rule or target.
Unknown policy lines and conflicting amendments fail before typed lowering.

The existing typed/native APIs are used unchanged. Their incomplete conversation,
identity-format and sealed native write limits are not bypassed by this entry.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time

from compiler.core import make_request, validate_decisions
from compiler.typed_source import GUARDS, source_wording
from .api_contract import BankContract
from .compiled_policy import FACT_NAMES, REQUIRED_FACTS, lower_compilation
from .extract import explicit_policy_sections
from .ingest import collect_sources, read_source
from .models import FoundationError, strict_json
from .semantic_renderer import validate_wording_bundle


VERSION = 'bounded-brief-requirements-1'
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
ALIASES = {
    'Read accounts only after all required identity questions pass.': ('identity_verified', 'get_account'),
    'Record promises only after an explicit current readback confirmation.': ('consent_current', 'create_promise'),
    'Record promises only within the account floor and past-due amount.': ('amount_within_bounds', 'create_promise'),
    'Record promises only on business days.': ('date_is_business_day', 'create_promise'),
    'Say goodbye before closing the call.': ('goodbye_spoken', 'close_call'),
    'Close each call once, after all other actions.': ('close_once_last', 'close_call'),
}
REQUIRED_GUARDS = frozenset(GUARDS.values())
SUPPORTED_KINDS = {'.md', '.txt', '.eml'}
STRUCTURAL_HEADINGS = frozenset({
    'Collection specification', 'Policy', 'Policies', 'Requirements', 'Parameters',
    'Memo', 'Amendments', 'Wording', 'Code list', 'Outcome codes',
})


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _error(code):
    # Do not include arbitrary source text in an exception/log message.
    raise FoundationError('BRIEF_REQUIREMENTS_' + code)


def _effect(sentence):
    """Return a single finite typed effect; no defaults or external execution."""
    match = re.fullmatch(r'Parameter ([a-z_]+) is (.+)\.', sentence)
    if match:
        name, value = match.groups()
        if name not in FACT_NAMES:
            _error('UNKNOWN_PARAMETER')
        return ('set', 'policy.' + name, strict_json(value), None)
    guard = GUARDS.get(sentence) or ALIASES.get(sentence)
    if guard:
        return ('require', guard[0], True, guard[1])
    match = re.fullmatch(r'Business days are (' + '|'.join(WEEKDAYS) + r') to (' + '|'.join(WEEKDAYS) + r')\.', sentence)
    if match:
        first, last = [WEEKDAYS.index(value) for value in match.groups()]
        if last < first:
            _error('UNSUPPORTED_WRAPPED_WEEKDAY_RANGE')
        return ('set', 'policy.business_weekdays', list(range(first, last + 1)), None)
    match = re.fullmatch(r'Bank holidays are (none|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:, [0-9]{4}-[0-9]{2}-[0-9]{2})*)\.', sentence)
    if match:
        return ('set', 'policy.holidays', [] if match[1] == 'none' else match[1].split(', '), None)
    match = re.fullmatch(r'Identity questions, in order: ([a-z][a-z0-9_]*(?:, [a-z][a-z0-9_]*)*)\.', sentence)
    if match:
        return ('set', 'policy.identity_order', match[1].split(', '), None)
    for prefix, name in (('Maximum wrong identity answers', 'identity_max_wrong'),
                         ('Maximum products', 'product_limit'), ('Promise write attempts', 'promise_max_attempts')):
        match = re.fullmatch(re.escape(prefix) + r': ([0-9]+)\.', sentence)
        if match:
            return ('set', 'policy.' + name, int(match[1]), None)
    match = re.fullmatch(r'Notes are (enabled|disabled)\.', sentence)
    if match:
        return ('set', 'policy.notes_enabled', match[1] == 'enabled', None)
    for prefix, name in (('Floor fields', 'floor_fields'), ('Delinquency bands', 'bands'), ('Closing outcomes', 'outcome_table')):
        match = re.fullmatch(re.escape(prefix) + r': (.+)\.', sentence)
        if match:
            return ('set', 'policy.' + name, strict_json(match[1]), None)
    match = re.fullmatch(r'Callback hours: ([0-9]{2}:[0-9]{2}) to ([0-9]{2}:[0-9]{2}) inclusive\.', sentence)
    if match:
        return ('set', 'policy.callback_hours', {'start': match[1], 'end': match[2], 'inclusive': True}, None)
    match = re.fullmatch(r'(?:Maximum payment amount: |Payment amount ceiling is )([0-9]+(?:\.[0-9]{1,2})?)\.', sentence)
    if match:
        return ('limit', 'payment.amount', strict_json(match[1]), 'create_promise')
    match = re.fullmatch(r'(Latest payment date: |Earliest payment date: |Payment date must be on or before )([0-9]{4}-[0-9]{2}-[0-9]{2})\.', sentence)
    if match:
        target = 'payment.date_on_or_after' if match[1].startswith('Earliest') else 'payment.date_on_or_before'
        return ('require', target, match[2], 'create_promise')
    _error('UNKNOWN_REQUIRED_RULE')


@dataclass
class _Clause:
    identifier: str
    policy_id: str | None
    number: str | None
    effect: tuple
    evidence: dict
    target: tuple | None = None
    overrides: str | None = None
    priority: int = 0


def _collect(documents):
    if not documents or len(documents) > 32 or sum(len(doc.text) for doc in documents) > 1_000_000:
        _error('SOURCE_BUDGET')
    if len({doc.source_id for doc in documents}) != len(documents):
        _error('DUPLICATE_SOURCE_CONTENT')
    clauses, structural, policy_ids = [], [], {}
    for doc in documents:
        if doc.status != 'processed' or doc.kind not in SUPPORTED_KINDS:
            _error('UNSUPPORTED_SOURCE_MODALITY')
        if any('attachment' in limitation.lower() for limitation in doc.limitations):
            _error('UNREAD_EMAIL_ATTACHMENT')
        scopes, definitions = explicit_policy_sections(doc)
        for definition in definitions:
            policy_id = definition['policy_id']
            if policy_id in policy_ids:
                _error('AMBIGUOUS_POLICY_ID')
            policy_ids[policy_id] = doc.source_id
        lines, offset = [], 0
        for raw in doc.text.splitlines(keepends=True):
            policy_id = scopes[offset]
            stripped = raw.strip()
            start = offset + len(raw) - len(raw.lstrip())
            offset += len(raw)
            if not stripped:
                continue
            if len(stripped) > 16000:
                _error('RULE_BUDGET')
            ref = dict(source_id=doc.source_id, start=start, end=start + len(stripped), quote=stripped)
            if re.fullmatch(r'(From|To|Date|Subject|Message-ID):.*', stripped):
                structural.append(dict(kind='email_metadata_not_authority', evidence=ref))
                continue
            heading = re.fullmatch(r'#{1,6} (.+)', stripped)
            if heading:
                # Arbitrary headings can themselves contain policy. Accept a
                # finite structural vocabulary, not a modal-word denylist.
                if heading[1] not in STRUCTURAL_HEADINGS:
                    _error('UNKNOWN_OR_POLICY_HEADING')
                structural.append(dict(kind='heading_no_semantic_claim', evidence=ref))
                continue
            match = re.fullmatch(r'Policy ID: ([A-Za-z][A-Za-z0-9_.-]{0,79})', stripped)
            if match:
                structural.append(dict(kind='explicit_policy_identifier_not_authority', evidence=ref))
                continue
            if stripped.startswith('Wording '):
                if not re.fullmatch(r'Wording [a-z_]+: .+', stripped):
                    _error('MALFORMED_WORDING')
                structural.append(dict(kind='explicit_source_wording', evidence=ref))
                continue
            match = re.fullmatch(r'([1-9][0-9]*)\.\s+(.+)', stripped)
            number, sentence = (match[1], match[2]) if match else (None, stripped)
            lines.append((number, sentence, ref, policy_id))
        for number, sentence, ref, policy_id in lines:
            target = None
            memo = re.fullmatch(r'This memo overrides rule ([1-9][0-9]*) of policy ([A-Za-z][A-Za-z0-9_.-]{0,79}) with: (.+)', sentence)
            amendment = re.fullmatch(r'This amendment overrides Parameter ([a-z_]+) with (.+)\.', sentence)
            ceiling = re.fullmatch(r'This amendment replaces the payment ceiling with ([0-9]+(?:\.[0-9]{1,2})?)\.', sentence)
            latest = re.fullmatch(r'This amendment replaces the latest payment date with ([0-9]{4}-[0-9]{2}-[0-9]{2})\.', sentence)
            if memo:
                effect = _effect(memo[3]); target = ('numbered', memo[2], memo[1])
            elif amendment:
                effect = _effect('Parameter ' + amendment[1] + ' is ' + amendment[2] + '.')
                target = ('effect', effect[1], effect[3])
            elif ceiling:
                effect = _effect('Maximum payment amount: ' + ceiling[1] + '.')
                target = ('effect', effect[1], effect[3])
            elif latest:
                effect = _effect('Latest payment date: ' + latest[1] + '.')
                target = ('effect', effect[1], effect[3])
            else:
                effect = _effect(sentence)
            identifier = 'derived-' + doc.source_id[4:] + '-' + str(ref['start'])
            clauses.append(_Clause(identifier, policy_id, number, effect, ref, target))
            if len(clauses) > 256:
                _error('RULE_COUNT_BUDGET')
    if not clauses:
        _error('NO_SUPPORTED_RULES')
    return sorted(clauses, key=lambda clause: clause.identifier), structural


def derive_brief_requirements(documents, contract):
    """Return original-source IR and coverage for the documented finite grammar.

    Source documents may be supplied from an archive. No path is read here.
    This function neither substitutes practice sources nor admits live writes.
    """
    clauses, structural = _collect(documents)
    by_id = {clause.identifier: clause for clause in clauses}
    for clause in clauses:
        if clause.target is None:
            continue
        if clause.target[0] == 'numbered':
            matches = [other for other in clauses if other.policy_id == clause.target[1] and other.number == clause.target[2]]
        else:
            matches = [other for other in clauses if other.target is None and (other.effect[1], other.effect[3]) == clause.target[1:]]
        if len(matches) != 1 or matches[0] is clause:
            _error('UNRESOLVED_OR_AMBIGUOUS_OVERRIDE')
        previous = matches[0]
        if (previous.effect[0], previous.effect[1], previous.effect[3]) != (clause.effect[0], clause.effect[1], clause.effect[3]):
            _error('OVERRIDE_CHANGES_RULE_KIND_OR_TARGET')
        clause.overrides = previous.identifier

    def ancestors(clause, visiting=()):
        if clause.identifier in visiting:
            _error('CYCLIC_OVERRIDE')
        if clause.overrides is None:
            return ()
        return (clause.overrides,) + ancestors(by_id[clause.overrides], visiting + (clause.identifier,))
    ancestry = {clause.identifier: ancestors(clause) for clause in clauses}
    for clause in clauses:
        clause.priority = len(ancestry[clause.identifier])
    groups = {}
    for clause in clauses:
        groups.setdefault((clause.effect[1], clause.effect[3]), []).append(clause)
    active = []
    for group in groups.values():
        winners = [clause for clause in group if not any(clause.identifier in ancestry[other.identifier] for other in group)]
        if len(winners) != 1:
            _error('CONFLICTING_RULES_WITHOUT_EXPLICIT_OVERRIDE')
        active.extend(winners)
    facts = [dict(name=clause.effect[1][7:], value=copy.deepcopy(clause.effect[2]), category='other',
                  evidence=[copy.deepcopy(clause.evidence)]) for clause in active if clause.effect[1].startswith('policy.')]
    fact_names = {fact['name'] for fact in facts}
    if REQUIRED_FACTS - fact_names:
        _error('MISSING_REQUIRED_PARAMETERS')
    guards = {(clause.effect[1], clause.effect[3]) for clause in active
              if clause.effect[0] == 'require' and clause.effect[2] is True}
    if not REQUIRED_GUARDS <= guards:
        _error('MISSING_SOURCE_SAFETY_GUARDS')
    rules = [dict(id=clause.identifier, priority=clause.priority,
        conditions=[dict(fact='operation', operator='eq', value=clause.effect[3])] if clause.effect[3] else [],
        effects=[dict(kind=clause.effect[0], target=clause.effect[1], value=copy.deepcopy(clause.effect[2]))],
        exceptions=[], overrides=[clause.overrides] if clause.overrides else [], evidence=[copy.deepcopy(clause.evidence)])
        for clause in clauses]
    identity = next(fact for fact in facts if fact['name'] == 'identity_order')
    decisions = dict(schema_version='decision-ir-1', rules=rules, facts=facts,
        api_bindings=[dict(role='identity_check', operation='verify_identity', direction='request', field_path='field',
                           allowed_values=identity['value'], evidence=copy.deepcopy(identity['evidence']))], unresolved=[])
    validate_decisions(decisions, documents, contract)
    request = make_request(documents, contract)
    envelope = dict(kind='source_checked_decision_ir', schema_version='compiler-envelope-1',
        decisions=decisions, sources=[doc.inventory() for doc in documents], contract_sha256=contract.source_hash,
        request_sha256=hashlib.sha256(_json(request).encode()).hexdigest(),
        validation=dict(schema_valid=True, evidence_valid=True, api_bindings_valid=True, unresolved_count=0,
                        semantic_truth_verified=False, builder_integration_ready=False, publish_allowed=False))
    return dict(kind=VERSION, compilation=envelope, publish_allowed=False,
        derivation=dict(assignment_rules=len(clauses), explicit_overrides=sum(clause.overrides is not None for clause in clauses),
            active_rule_ids=sorted(clause.identifier for clause in active),
            original_sources=[doc.inventory() for doc in documents], structural_blocks=structural,
            coordinate_system='extracted Unicode text; zero-based half-open',
            source_names_or_order_used_as_authority=False, reviewed_hash_allowlist_used=False,
            full_source_semantic_coverage=False, model_calls=0, network_calls=0,
            grammar_scope='explicit bounded numbered policies and referenced memo/parameter amendments'))


def compile_requirement_documents(documents, contract, *, evaluation_source_hashes=()):
    """Compile caller-verified source documents without reading original paths.

    The caller authenticates archived raw bytes and constructs the actual bank
    contract before this entry. This function preserves the existing lowerer's
    source and API validation and does not grant official/live write authority.
    """
    began = time.perf_counter()
    documents = list(documents)
    excluded = set(evaluation_source_hashes)
    if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in excluded):
        _error('INVALID_EVALUATION_SOURCE_HASH')
    if any(doc.sha256 in excluded for doc in documents):
        _error('EVALUATION_DATA_NOT_POLICY')
    result = derive_brief_requirements(documents, contract)
    compiled = lower_compilation(result['compilation'], documents, contract)
    wording = validate_wording_bundle(source_wording(documents), documents)
    elapsed = time.perf_counter() - began
    if elapsed > 1800:
        _error('BRIEF_DEADLINE_EXCEEDED')
    return dict(compiled=compiled, documents=documents, wording_bundle=wording,
        compilation=result['compilation'], derivation=result['derivation'],
        scope=VERSION, publish_allowed=False, elapsed_seconds=elapsed,
        unsupported=['arbitrary_prose_interpretation', 'unrecognized_required_rules', 'unread_email_attachments',
            'CSV_or_code_list_without_supported_explicit_rules', 'new_identity_normalization_formats',
            'full_final_conversation_or_native_banking_authority'])


def compile_brief_requirements(policy_paths, tool_api_path, *, evaluation_paths=()):
    """Read new sources and return the builders' compiled/documents/wording shape."""
    began = time.perf_counter()
    documents = collect_sources([Path(path) for path in policy_paths])
    excluded = {read_source(Path(path)).sha256 for path in evaluation_paths}
    api = read_source(Path(tool_api_path))
    contract = BankContract(strict_json(api.text), api.source_id, api.sha256)
    result = compile_requirement_documents(documents, contract, evaluation_source_hashes=excluded)
    result['elapsed_seconds'] = time.perf_counter() - began
    if result['elapsed_seconds'] > 1800:
        _error('BRIEF_DEADLINE_EXCEEDED')
    return result


def emit_native_requirements(bundle, *, initial_variables=None):
    """Connect new original sources to the existing unpublishable generic graph."""
    from .native_freya import build_native_workflow
    if bundle.get('scope') != VERSION or bundle.get('publish_allowed') is not False:
        _error('DERIVED_BUNDLE_REQUIRED')
    return build_native_workflow(bundle['compiled'], bundle['documents'], bundle['wording_bundle'],
                                 initial_variables=initial_variables)
