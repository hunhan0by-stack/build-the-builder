"""Pure, offline recovery decisions for native conversation adapters.

This reducer never verifies identity, confirms a payment, calls a tool, or alters
bank records. A caller must run proposals through its existing source-bound
amount/date/product guards and render a NEW readback before accepting consent.
The returned state is a defensive copy. The native adapter owns phase routing.
"""
from __future__ import annotations

import datetime
import json
import re

from .models import FoundationError


RECOVERY_KINDS = frozenset({'question', 'clarify', 'correction', 'cancel_proposal'})
CONVERSATION_PHASES = frozenset({'person_confirm', 'identity', 'proposal', 'product', 'all_today', 'readback', 'confirmed'})
QUESTION_TOPICS = frozenset({'purpose', 'identity_reason', 'ai', 'scam', 'account', 'policy', 'unknown'})
EXTRACTION_GUIDANCE = '''Recovery intents are proposals to the deterministic controller, never bank authority.
question: {kind,topic}; topic is purpose, identity_reason, ai, scam, account, policy or unknown. A question is not a security answer or consent.
clarify: {kind,field}; field is identity, amount, date, product or confirmation. Use for ambiguity or incomplete payment information; do not guess a date or amount.
correction: {kind,product_id,changes}; changes contains only the explicitly changed amount and/or date. Use the final corrected values from this utterance. A yes followed by but, a changed amount, date or product is NEVER confirm.
cancel_proposal: {kind,product_id}; an explicit withdrawal before recording, not a claim that a recorded operation was cancelled.
Never copy a question, an unrelated story's date, silence, or an old confirmation into a current payment instruction. Ambiguous product references must be clarified, not guessed.
The existing source-bound workflow validates every replacement proposal and obtains a new readback confirmation. These intent shapes do not authorize a write.'''


def _error(code):
    raise FoundationError('DIALOGUE_RECOVERY_' + code)


def parse_turn(raw):
    if not isinstance(raw, str) or not 1 <= len(raw) <= 2048:
        _error('TURN_BUDGET')
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                _error('DUPLICATE_TURN_KEY')
            value[key] = item
        return value
    def constant(unused):
        _error('NONFINITE_TURN')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError):
        _error('INVALID_TURN_JSON')
    if not isinstance(value, dict) or not isinstance(value.get('kind'), str):
        _error('TURN_OBJECT_REQUIRED')
    return value


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _clone_state(state, call_id, binding):
    if not isinstance(state, dict) or not isinstance(call_id, str) or not call_id or not isinstance(binding, str) or not binding:
        _error('EXPLICIT_CALL_AND_SOURCE_REQUIRED')
    if state.get('call_id') != call_id or state.get('binding') != binding:
        _error('CALL_OR_SOURCE_MISMATCH')
    try:
        wire = json.dumps(state, allow_nan=False)
    except (ValueError, TypeError):
        _error('INVALID_STATE')
    if len(wire.encode()) > 24576:
        _error('STATE_BUDGET')
    return _copy(state)


def _invalidate(state):
    token = state.get('readback_token')
    if token:
        state['dialogue_invalidated_token'] = token
    state['consent'] = ''
    state['readback_token'] = ''
    state['dialogue_requires_readback'] = True


def _decision(state, action, *, reason='', answer=None, replacement=None, product_id=None):
    return dict(handled=action != 'delegate', action=action, state=state, reason=reason,
                answer=answer, replacement=replacement, product_id=product_id,
                requires_readback=bool(state.get('dialogue_requires_readback')),
                bank_operations=[], grants_identity=False, grants_consent=False)


def _schema(event, fields):
    if set(event) != set(fields):
        _error('UNEXPECTED_INTENT_FIELDS')


def _budget(state, limit):
    if type(limit) is not int or not 1 <= limit <= 32:
        _error('INVALID_CLARIFICATION_LIMIT')
    count = state.get('dialogue_recovery_count', 0)
    if type(count) is not int or count < 0:
        _error('INVALID_RECOVERY_COUNTER')
    state['dialogue_recovery_count'] = count + 1
    # This is a technical turn bound; identity/persuasion counts are untouched.
    return count < limit


def _product(state, product_id):
    products = state.get('products')
    if products is None:
        product = state.get('product')
        products = [product] if isinstance(product, dict) and product else []
    if not isinstance(products, list) or not 1 <= len(products) <= 3 or any(not isinstance(p, dict) for p in products):
        _error('AUTHORITATIVE_PRODUCTS_REQUIRED')
    identifiers = [p.get('product_id') for p in products]
    if any(not isinstance(value, str) or not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        _error('AMBIGUOUS_PRODUCT_SNAPSHOT')
    return next((p for p in products if p['product_id'] == product_id), None)


def _amount(value):
    if type(value) not in (int, float) or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', str(value)):
        _error('EXPLICIT_FINITE_AMOUNT_REQUIRED')
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        _error('EXPLICIT_DATE_REQUIRED')
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        _error('INVALID_DATE')
    return value


def _answer(topic, answers, state, identity_order):
    """Only caller-compiled literal responses; no raw customer text is rendered."""
    if not isinstance(answers, dict) or set(answers) - QUESTION_TOPICS:
        _error('INVALID_ANSWER_POLICY')
    row = answers.get(topic)
    if row is None:
        return None
    if not isinstance(row, dict) or set(row) != {'text', 'scope', 'evidence'} or row['scope'] not in ('public', 'verified'):
        _error('INVALID_ANSWER_DECLARATION')
    text, evidence = row['text'], row['evidence']
    if not isinstance(text, str) or not text.strip() or len(text) > 2000 or any(c in text for c in '{}<>'):
        _error('UNSAFE_ANSWER_TEMPLATE')
    if not isinstance(evidence, dict) or set(evidence) != {'source_id', 'start', 'end', 'quote'}:
        _error('ANSWER_EVIDENCE_REQUIRED')
    if row['scope'] == 'verified' or topic in ('account', 'policy'):
        if not identity_order or state.get('verified') != list(identity_order) or state.get('phase') not in ('proposal', 'product', 'all_today', 'readback', 'confirmed'):
            return None
    return _copy(row)


def validate_answers(answers, documents):
    """Compile literal QA declarations with exact original-source coordinates.

    A source interpreter must assign topic/scope. Exact quotation is provenance,
    not proof that an arbitrary sentence is suitable before identity checks.
    """
    if not isinstance(answers, dict) or set(answers) - QUESTION_TOPICS:
        _error('INVALID_ANSWER_POLICY')
    sources = {doc.source_id: doc for doc in documents}
    for topic, row in answers.items():
        _answer(topic, {topic: row}, {}, ())
        ref = row['evidence']; source = sources.get(ref['source_id'])
        if source is None or source.status != 'processed' or type(ref['start']) is not int or type(ref['end']) is not int or not 0 <= ref['start'] < ref['end'] <= len(source.text):
            _error('INVALID_ANSWER_SOURCE')
        if source.text[ref['start']:ref['end']] != row['text'] or ref['quote'] != row['text']:
            _error('ANSWER_QUOTE_MISMATCH')
    return _copy(answers)


def recover_turn(state, event, *, call_id, binding, answers=None, identity_order=(), max_recoveries=6):
    """Handle recovery intents before the native controller's ordinary dispatch.

    Actions: delegate, resume, clarify, replace_proposal, cancel_proposal, blocked.
    Replacement is UNVALIDATED against bank bounds; the native owner must run its
    admissibility function, update its authoritative draft and call mark_readback
    after emitting a fresh readback. No returned decision grants bank authority.
    """
    updated = _clone_state(state, call_id, binding)
    if not isinstance(event, dict) or not isinstance(event.get('kind'), str):
        _error('TURN_OBJECT_REQUIRED')
    kind = event['kind']
    held = bool(updated.get('dialogue_requires_readback'))
    if kind not in RECOVERY_KINDS and not (kind == 'confirm' and (held or event.get('explicit_yes') is False)):
        return _decision(updated, 'delegate')
    if updated.get('pending') or updated.get('phase') not in CONVERSATION_PHASES or updated.get('write_started'):
        _invalidate(updated)
        return _decision(updated, 'blocked', reason='operation_or_phase_not_recoverable')
    if not _budget(updated, max_recoveries):
        _invalidate(updated)
        return _decision(updated, 'blocked', reason='recovery_turn_budget_exhausted')
    if updated.get('phase') in ('readback', 'confirmed'):
        _invalidate(updated)
    if kind == 'question':
        _schema(event, ('kind', 'topic'))
        if not isinstance(event['topic'], str) or event['topic'] not in QUESTION_TOPICS:
            _error('UNSUPPORTED_QUESTION_TOPIC')
        answer = _answer(event['topic'], {} if answers is None else answers, updated, identity_order)
        return _decision(updated, 'resume' if answer else 'clarify', reason='source_answer' if answer else 'no_admitted_answer', answer=answer)
    if kind == 'clarify':
        _schema(event, ('kind', 'field'))
        if not isinstance(event['field'], str) or event['field'] not in ('identity', 'amount', 'date', 'product', 'confirmation'):
            _error('UNSUPPORTED_CLARIFICATION_FIELD')
        return _decision(updated, 'clarify', reason='clarify_' + event['field'])
    if kind == 'confirm':
        _schema(event, ('kind', 'readback_token', 'explicit_yes'))
        if held:
            return _decision(updated, 'clarify', reason='fresh_readback_required')
        if event['readback_token'] != state.get('readback_token') or state.get('phase') != 'readback':
            return _decision(updated, 'blocked', reason='stale_or_misplaced_confirmation')
        return _decision(updated, 'clarify', reason='confirmation_declined')
    _schema(event, ('kind', 'product_id', 'changes') if kind == 'correction' else ('kind', 'product_id'))
    _invalidate(updated)
    if updated.get('phase') not in ('proposal', 'product', 'all_today', 'readback', 'confirmed'):
        return _decision(updated, 'blocked', reason='payment_intent_before_account')
    if not isinstance(event['product_id'], str):
        _error('PRODUCT_ID_REQUIRED')
    product = _product(updated, event['product_id'])
    if product is None:
        return _decision(updated, 'clarify', reason='ambiguous_product')
    if product.get('status') in ('recorded', 'service_error') or product.get('note_done'):
        return _decision(updated, 'blocked', reason='product_operation_already_started')
    if kind == 'cancel_proposal':
        return _decision(updated, 'cancel_proposal', reason='withdraw_unrecorded_proposal', product_id=product['product_id'])
    changes = event['changes']
    if not isinstance(changes, dict) or not changes or set(changes) - {'amount', 'date'}:
        _error('EXPLICIT_CHANGED_SLOTS_REQUIRED')
    draft = product.get('draft', updated.get('draft', {}))
    if not isinstance(draft, dict):
        _error('INVALID_DRAFT')
    replacement = dict(product_id=product['product_id'])
    for key, check in (('amount', _amount), ('date', _date)):
        value = changes[key] if key in changes else draft.get(key)
        if value is None:
            return _decision(updated, 'clarify', reason='missing_' + key)
        replacement[key] = check(value)
    return _decision(updated, 'replace_proposal', reason='revalidate_then_readback', replacement=replacement, product_id=product['product_id'])


def mark_readback(state, token, *, call_id, binding):
    """After native validation AND rendering, clear only the recovery hold.

    Caller-owned token must be new. This never sets consent, changes the draft,
    advances to confirmed or supplies a bank authorization.
    """
    updated = _clone_state(state, call_id, binding)
    if updated.get('phase') != 'readback' or updated.get('pending') or updated.get('write_started'):
        _error('READBACK_PHASE_REQUIRED')
    if not isinstance(token, str) or not token or len(token) > 200 or token == updated.get('dialogue_invalidated_token'):
        _error('FRESH_READBACK_TOKEN_REQUIRED')
    updated['readback_token'] = token
    updated['consent'] = ''
    updated['dialogue_requires_readback'] = False
    return updated


def sandbox_helpers():
    """Return the same reducer functions for code_exec; no host import in output.

    Generation reads only this module's source. The emitted helper uses the
    documented json/re/datetime sandbox imports. The native owner must integrate
    the returned actions with its own guarded workflow; this is not an export.
    """
    import ast
    from pathlib import Path
    tree = ast.parse(Path(__file__).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, (ast.Assign, ast.FunctionDef))
             and not (isinstance(n, ast.FunctionDef) and n.name in ('sandbox_helpers', 'validate_answers'))
             and not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'EXTRACTION_GUIDANCE' for t in n.targets))]
    source = 'import json, re, datetime\nclass FoundationError(ValueError):\n    pass\n' + '\n'.join(ast.unparse(n) for n in nodes) + '\n'
    compile(source, '<dialogue-recovery-helpers>', 'exec')
    return source
