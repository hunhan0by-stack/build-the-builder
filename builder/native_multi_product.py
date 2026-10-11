"""Source-bound Brief B graph with serialized in-call writes; no live authority.

The supported structured-answer grammar covers the public B promise/refusal
cases. It is not arbitrary voice intent, restart persistence or provider dedup.
One shared immutable readback plan and one confirmation implement B rule 7.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import textwrap
import time

from .ingest import read_source
from .models import FoundationError
from .native_freya import (_RUNTIME, _bank_node, _capsule, _code_node, _edge,
                          build_native_workflow, measure_code_exec_budget)
from .native_kit_emitter import _cfg, _READ_POST, _compress_generated_python
from .native_official_closure import compile_official_closure
from .native_write_guard import make_write_hooks, reserve_single_call, native_scope_code, bank_session_boot, validate_runtime_mode
from .native_kit_verification import OfficialNativeHarness, local_http


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


_B_TURN_SHAPES = (
    'person_confirm {kind,explicit_yes}; identity_answer {kind,field,answer}; '
    'pay_all {kind,explicit_yes}; bundle_proposal {kind,date,amounts}; '
    'proposal {kind,product_id,amount,date}; refusal {kind,product_id}; '
    'confirm {kind,readback_token,explicit_yes}'
)
_B_EXTRACTION_DESCRIPTION = (
    'Latest answer as a JSON string. '
    'Supported shapes: ' + _B_TURN_SHAPES + '. '
    'Copy identity verbatim; never pad, invent digits or combine previous turns. '
    'Normalize only payment dates. Follow the custom prompt; never infer consent. '
    'Platform voice extraction remains unmeasured.'
)
_B_NO_TODAY_JSON = '{"explicit_yes":false,"kind":"pay_all"}'
_B_EXTRACTION_PROMPT = '''{{extractor.type_preamble}}
Extract the latest answer: {{extractor.speech}}
Context: {{extractor.conversation.last-10}}. Return variables: {{extractor.variables_list}}.
Call time {{now}}. Actual account products {{_native_products_json}}.
Phase {{_native_phase}}, identity field {{_native_identity_field}}, current product {{_native_proposal_product_id}}, offer {{_native_suggested_amount}} on {{_native_suggested_date}}, current readback token {{_native_readback_token}}.
Return _native_turn_json as exactly one JSON-string object. Supported shapes: ''' + _B_TURN_SHAPES + '''.
Use person_confirm only for a clear right-person answer; identity_answer only for the asked field. For date_of_birth, answer must copy the latest extractor.speech verbatim, even incomplete or unclear: never convert it to ISO, add a missing year, reuse old answers or infer from context. Always return identity_answer. Numeric birth dates are day, month, year. For phone_last4, copy the latest extractor.speech verbatim, even incomplete or unclear; never pad, invent digits, combine previous turns or copy old digits. Always return identity_answer; code accepts only one complete four-digit phone answer. Never infer security answers.
In all_today, pay_all is clear yes/no to paying all today without an alternative. "I cannot pay today", "I can't pay today" and "not today" emit {"kind":"pay_all","explicit_yes":false}, not unsupported. Never emit pay_all true for an answer that names an alternative date, even if it starts with yes. An explicit promise for all actual products on one named date is bundle_proposal; amounts contains every actual product_id with its full value from full unless explicit different amounts. This is a proposal, never final consent.
In product, proposal concerns only the current product. Clear acceptance of the current amount/date uses that exact offer. Flat refusal with no date or amount is refusal for the current product. In readback, only a clear yes emits confirm with the current token; an explicit changed common date/amounts emits a new bundle_proposal, never confirm.
Normalize payment dates to YYYY-MM-DD using Call time, not wall-clock date. Rule 22: this weekday is in the current Monday-Sunday calendar week; next weekday is in the following calendar week. Convert spoken amounts to numbers. Never adjust for weekends, holidays or deadlines; code applies bank rules. Vague periods such as next week, end of month, when salary comes; ambiguity; unsupported intents, callback, transfer, hardship, dispute or already-paid return {"kind":"unsupported"}. Never invent IDs, dates, amounts or consent. Do not speak or decide bank policy.'''
_B_NO_TODAY_PROMPT = '''{{extractor.type_preamble}}
Latest answer {{extractor.speech}}. Context {{extractor.conversation.last-10}}. Requested variables {{extractor.variables_list}}. Phase {{_native_phase}}.
_native_no_today_json: only in all_today, standalone "I cannot pay today", "I can't pay today" or "not today" with no alternative date, amount, proposal or agreement emits exactly ''' + _B_NO_TODAY_JSON + '''. Otherwise empty. Never reuse history or infer consent.'''


_MULTI = r'''
def spoken_year(words):
    U = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
    T = 'twenty thirty forty fifty sixty seventy eighty ninety'.split()
    if len(words) < 2:
        return None
    h, w = words[0], words[1:]
    if h == 'two' and w[0] == 'thousand':
        b = 2000; w = w[1:]
    elif (h in U[10:] or h == 'twenty') and (w[0] == 'hundred' or w[0] in T):
        b = (U.index(h) if h in U else 20) * 100
        if w[0] == 'hundred':
            w = w[1:]
    else:
        return None
    if w[:1] == ['and']:
        if len(w) == 1:
            return None
        w = w[1:]
    if not w:
        return b
    if len(w) == 1 and w[0] in U:
        return b + U.index(w[0])
    if len(w) in (1, 2) and w[0] in T and (len(w) == 1 or w[1] in U[1:10]):
        return b + (T.index(w[0]) + 2) * 10 + (U.index(w[1]) if len(w) == 2 else 0)
    return None

def phone_answer(a):
    if not isinstance(a, str) or not 1 <= len(a) <= 512:
        return None
    s = a.lower().strip(' .,')
    if re.fullmatch('[0-9 ,.-]+', s):
        d = re.sub('[ ,.-]', '', s)
        return d if re.fullmatch('[0-9]{4}', d) else None
    w = s.replace('-', ' ').replace(',', ' ').replace('.', ' ').split()
    N = ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine')
    d = [str(N.index(x)) if x in N else '0' if x == 'oh' else x for x in w]
    return ''.join(d) if len(d) == 4 and all(re.fullmatch('[0-9]', x) for x in d) else None

def birth_answer(a, p=None):
    M = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december')
    O = ('first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth', 'ninth', 'tenth', 'eleventh', 'twelfth', 'thirteenth', 'fourteenth', 'fifteenth', 'sixteenth', 'seventeenth', 'eighteenth', 'nineteenth', 'twentieth', 'twenty first', 'twenty second', 'twenty third', 'twenty fourth', 'twenty fifth', 'twenty sixth', 'twenty seventh', 'twenty eighth', 'twenty ninth', 'thirtieth', 'thirty first')
    C = 'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
    C += ['twenty ' + v for v in C[:9]] + ['thirty', 'thirty one']
    if not isinstance(a, str) or not 1 <= len(a) <= 512:
        raise ValueError('Explicit bounded heard birth answer required')
    s = re.sub('\\s+', ' ', a.lower().replace('-', ' ').replace(',', ' ')).strip(' .,')
    w = s.split()
    for i in range(len(w)):
        y = spoken_year(w[i:])
        if y is not None:
            s = ' '.join(w[:i] + [str(y)]); break
    i = re.fullmatch('(\\d{4})-(\\d{2})-(\\d{2})', a.strip())
    n = re.fullmatch('(\\d{1,2})[/ .](\\d{1,2})[/ .](\\d{4})', s)
    if i:
        return (datetime.date(*map(int, i.groups())).isoformat(), {})
    if n:
        d, m, y = map(int, n.groups())
        return (datetime.date(y, m, d).isoformat(), {})
    if re.fullmatch('\\d{4}', s):
        y = int(s)
        if not 1 <= y <= 9999:
            raise ValueError('Invalid explicit birth year')
        if isinstance(p, dict) and set(p) == {'day', 'month'}:
            return (datetime.date(y, p['month'], p['day']).isoformat(), {})
        return (None, {'year': y})
    n = re.fullmatch('(?:the )?(?:(.+?)(?: of)? (' + '|'.join(M) + ')|(' + '|'.join(M) + ') (.+?))(?: (\\d{4}))?', s)
    if n:
        x, v, e, f, z = n.groups()
        x = x or f; v = v or e
        if x in O:
            d = O.index(x) + 1
        elif x in C:
            d = C.index(x) + 1
        else:
            q = re.fullmatch('(\\d{1,2})(?:st|nd|rd|th)?', x)
            if not q:
                return (None, {})
            d = int(q[1])
        m = M.index(v) + 1
        datetime.date(2000, m, d)
        if z:
            return (datetime.date(int(z), m, d).isoformat(), {})
        if isinstance(p, dict) and set(p) == {'year'}:
            return (datetime.date(p['year'], m, d).isoformat(), {})
        return (None, {'day': d, 'month': m})
    # An unrelated/unknown utterance cannot silently retain old components.
    return (None, {})

def state_binding():
    if S.get('binding') != CFG['binding'] or S.get('customer_id') != vars.get('customer_id') or S.get('now') != vars.get('now') or S.get('call_id') != vars.get('call.call_id'):
        raise ValueError('Native source/call/customer/date binding mismatch')
    C = json.loads(vars.get('_native_write_context', ''))
    if C.get('authority') != 'host_exclusive_customer_reservation' or C.get('customer_id') != S['customer_id'] or C.get('restart_policy') != 'never_automatically_resume_or_reinitialize' or C.get('provider_idempotency') is not False or C.get('reservation_id') != S.get('reservation_id'):
        raise ValueError('Missing irreversible host-exclusive customer admission')

def say(kind, text):
    if not isinstance(text, str) or not text or len(text) > 6000:
        raise ValueError('Invalid derived source wording')
    S['event_counter'] += 1
    if S['event_counter'] > 64:
        raise ValueError('Speech budget exhausted')
    S['events'].append(dict(event_id='native-b-speech-' + str(S['event_counter']), kind=kind, text=text, call_id=S['call_id'], source_binding=CFG['binding']))

def label(p):
    if p['type'] == 'credit_card':
        if not isinstance(p.get('card_last4'), str) or not re.fullmatch(r'[0-9]{4}', p['card_last4']):
            raise ValueError('Exact card suffix required')
        return 'credit card ending ' + p['card_last4']
    return p['type'].replace('_', ' ')

def money(value):
    value = cents(value)
    return str(value // 100) + '.' + str(value % 100).zfill(2)

def phrase(kind, context):
    text = CFG['wording'][kind]['template']
    for name in re.findall(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', text):
        value = context.get(name)
        if type(value) not in (str, int, float) or not str(value) or len(str(value)) > 512 or any(not 32 <= ord(c) <= 126 or c in '{}<>' for c in str(value)):
            raise ValueError('Unsafe/missing source wording context')
        text = text.replace('{' + name + '}', str(value))
    say(kind, text)

def question(hint=False):
    field = CFG['facts']['identity_order'][len(S['verified'])]
    source = CFG['flow']['identity_hint_templates' if hint else 'identity_templates'][field]
    say('identity_question', source['template'])

def read_request(operation, payload):
    if S.get('pending') or S['phase'] == 'blocked' or operation not in ('verify_identity', 'get_account'):
        raise ValueError('Unexpected read request')
    if operation == 'get_account' and S['verified'] != CFG['facts']['identity_order']:
        raise ValueError('Identity before account')
    checked(payload, CFG['operations'][operation]['request'], True)
    S['request_counter'] += 1
    S['pending'] = dict(request_id='native-b-bank-' + str(S['request_counter']), operation=operation, payload=payload, dispatched=False)
    for key, value in payload.items():
        vars['_native_arg_' + key] = value
    vars['_native_http_json'] = ''
    vars['_native_route'] = 'bank_' + operation

def product_bounds(row):
    p = dict(row)
    p['floor'] = p[CFG['facts']['floor_fields'][p['type']]]
    if not isinstance(p.get('product_id'), str) or not p['product_id'] or type(p['days_late']) is not int or not 0 < cents(p['floor']) <= cents(p['past_due']):
        raise ValueError('Invalid product bounds')
    band = [b for b in CFG['facts']['bands'] if b['min_days_late'] <= p['days_late'] <= b['max_days_late']]
    if len(band) != 1:
        raise ValueError('Ambiguous product-local band')
    d = day(S['now'][:10]); remaining = band[0]['deadline_business_days']
    for unused in range(4000):
        if not remaining:
            break
        d += datetime.timedelta(days=1)
        if business(d):
            remaining -= 1
    if remaining:
        raise ValueError('Calendar exhausted')
    p['deadline'] = d.isoformat(); p['persuasion_limit'] = band[0]['persuasion_attempts']; p['attempt'] = 0
    p['status'] = 'open'; p['reason'] = ''; p['draft'] = dict()
    return p

def admissible(p, amount, requested):
    if not cents(p['floor']) <= cents(amount) <= cents(p['past_due']):
        raise ValueError('Payment below floor or above full')
    d = min(day(requested), day(p['deadline']))
    for unused in range(370):
        if business(d):
            break
        d += datetime.timedelta(days=1)
    if not day(S['now'][:10]) <= d <= day(p['deadline']):
        raise ValueError('No permitted product date')
    for rule in CFG['constraints']:
        if 'create_promise' not in rule['operations']:
            continue
        if rule['target'] == 'payment.amount' and cents(amount) > cents(rule['value']):
            raise ValueError('Compiled amount ceiling exceeded')
        if rule['target'] == 'payment.date_on_or_before' and d > day(rule['value']) or rule['target'] == 'payment.date_on_or_after' and d < day(rule['value']):
            raise ValueError('Compiled date boundary exceeded')
    payload = dict(customer_id=S['customer_id'], product_id=p['product_id'], amount=amount, date=d.isoformat())
    checked(payload, CFG['operations']['create_promise']['request'], True)
    return payload

def offer(p):
    p['attempt'] += 1
    if p['attempt'] > p['persuasion_limit']:
        raise ValueError('Product persuasion exhausted')
    if p['attempt'] == 1:
        d = day(S['now'][:10]); friday = d + datetime.timedelta(days=6 - d.weekday())
        while not business(friday):
            friday -= datetime.timedelta(days=1)
        if friday <= d:
            friday = d + datetime.timedelta(days=13 - d.weekday())
            while not business(friday):
                friday -= datetime.timedelta(days=1)
        amount = p['past_due']; date = min(friday, day(p['deadline'])).isoformat()
    else:
        amount = p['floor']; date = p['deadline']
    p['offered_amount'] = amount; p['offered_date'] = date
    say('product_offer', 'Can you pay ' + money(amount) + ' for your ' + label(p) + ' on ' + date + '?' + (' The rest stays overdue.' if amount != p['past_due'] else ''))

def readback():
    plans = [dict(p['draft']) for p in S['products'] if p['status'] == 'agreed']
    S['plan_json'] = json.dumps(plans, sort_keys=True, separators=(',', ':'), allow_nan=False)
    S['consent'] = ''; S['readback_token'] = ''
    if not plans:
        S['phase'] = 'confirmed'; S['write_index'] = 0; select_write()
        return
    entries = [label(p) + ': ' + money(p['draft']['amount']) + ' on ' + p['draft']['date'] + (' (the latest date for this product)' if p.get('adjusted') else '') for p in S['products'] if p['status'] == 'agreed']
    S['readback_token'] = 'native-b-readback-' + str(S['event_counter'] + 1)
    S['phase'] = 'readback'
    say('readback', 'Please confirm these promises: ' + '; '.join(entries) + '. Do you confirm?')

def advance():
    remaining = [p for p in S['products'] if p['status'] == 'open']
    if remaining:
        S['current_product'] = remaining[0]['product_id']; S['phase'] = 'product'
        offer(remaining[0])
    else:
        readback()

def current():
    return next(p for p in S['products'] if p['product_id'] == S['current_product'])

def immutable_plan():
    if S['verified'] != CFG['facts']['identity_order'] or S.get('plan_json') != json.dumps([dict(p['draft']) for p in S['products'] if p['status'] in ('agreed', 'recorded', 'service_error')], sort_keys=True, separators=(',', ':'), allow_nan=False):
        raise ValueError('Confirmed plan changed')
    plan = json.loads(S['plan_json'])
    if plan and (not S.get('readback_token') or S.get('consent') != S['readback_token']):
        raise ValueError('Current bundle confirmation missing')
    return plan

def select_write():
    immutable_plan()
    journal = json.loads(vars.get('_native_write_journal', ''))
    if journal.get('reservation_id') != S['reservation_id'] or journal.get('call_id') != S['call_id']:
        raise ValueError('Journal scope mismatch')
    todo = [p for p in S['products'] if p['status'] == 'agreed']
    if todo:
        p = todo[0]; operation = 'create_promise'; payload = p['draft']
    else:
        todo = [p for p in S['products'] if p['status'] in ('refused', 'paid', 'dispute', 'hardship', 'service_error') and not p.get('note_done')]
        if not todo:
            S['phase'] = 'resolved'; vars['_native_route'] = 'native_terminal_arm'; return
        p = todo[0]; operation = 'add_note'; payload = dict(customer_id=S['customer_id'], product_id=p['product_id'], reason=p['reason'])
    key = operation + ':' + p['product_id']
    if key in journal['entries']:
        entry = journal['entries'][key]
        if entry.get('phase') != 'retry_allowed' or operation != 'create_promise' or entry.get('payload_json') != json.dumps(payload, sort_keys=True, separators=(',', ':')):
            raise ValueError('Consumed, dispatched or uncertain product cannot be replayed')
    else:
        entry = dict(version=1, reservation_id=S['reservation_id'], phase='ready', attempts=0, call_id=None, payload_json=None, proven_not_recorded=False)
        journal['entries'][key] = entry
    S['active_key'] = key; S['active_product'] = p['product_id']; S['phase'] = 'write_pending'
    vars['_native_write_journal'] = json.dumps(journal, sort_keys=True, separators=(',', ':'), allow_nan=False)
    vars['_native_write_state'] = json.dumps(entry)
    if operation == 'create_promise':
        auth = dict(source_binding=CFG['binding'], customer_id=S['customer_id'], call_id=S['call_id'], payload=payload, identity_verified=True, explicit_consent=True, consent_token=S['consent'], readback_token=S['readback_token'], account_snapshot=p, account_floor=p['floor'], account_past_due=p['past_due'], earliest_date=S['now'][:10], deadline=p['deadline'])
        vars['_native_write_authorization'] = json.dumps(auth, allow_nan=False)
    for key, value in payload.items():
        vars['_native_arg_' + key] = value
    vars['_native_route'] = 'bank_' + operation

def result_write():
    immutable_plan()
    journal = json.loads(vars['_native_write_journal']); entry = json.loads(vars['_native_write_state'])
    key = S['active_key']; p = next(p for p in S['products'] if p['product_id'] == S['active_product'])
    old = journal['entries'][key]
    if S['phase'] != 'write_pending' or old['phase'] != 'dispatched' or entry['call_id'] != S['call_id'] or old['payload_json'] != entry['payload_json'] or old['attempts'] != entry['attempts']:
        raise ValueError('Result without exact dispatched journal entry')
    if key.startswith('create_promise:'):
        auth = json.loads(vars['_native_write_authorization'])
        if auth.get('account_snapshot') != p or auth.get('payload') != p['draft'] or auth.get('readback_token') != S.get('readback_token') or auth.get('source_binding') != S['binding']:
            raise ValueError('Current product or confirmation changed during dispatch')
    journal['entries'][key] = entry
    vars['_native_write_journal'] = json.dumps(journal, sort_keys=True, separators=(',', ':'), allow_nan=False)
    outcome = vars.get('_native_write_outcome')
    if outcome == 'retry_allowed' and key.startswith('create_promise:'):
        select_write(); return
    if outcome == 'confirmed':
        if key.startswith('create_promise:'):
            p['status'] = 'recorded'
            phrase('promise_success', dict(product_label=label(p)))
        else:
            p['note_done'] = True
        select_write()
    elif outcome == 'failed' and key.startswith('create_promise:'):
        p['status'] = 'service_error'; p['reason'] = 'service_error'
        say('promise_failure', 'I could not record the promise for your ' + label(p) + '.')
        select_write()
    else:
        raise ValueError('Bank result unknown; no success, next write, retry or restart authorized')

def step(action):
    if action == 'boot':
        if vars.get('_native_state_json') or vars.get('_native_write_journal'):
            raise ValueError('Native restart unavailable; never reset consumed writes')
        cid = vars.get('call.call_id')
        if not isinstance(cid, str) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', cid) or cid == '00000000-0000-0000-0000-000000000000':
            raise ValueError('Real native call.call_id required')
        datetime.datetime.fromisoformat(vars['now']); C = json.loads(vars.get('_native_write_context', ''))
        S.update(binding=CFG['binding'], customer_id=vars['customer_id'], now=vars['now'], call_id=cid, reservation_id=C.get('reservation_id'), session_id=vars['customer_id'], phase='person_confirm', verified=[], wrong=0, event_counter=0, request_counter=0, events=[], products=[], pending=dict(), consent='', readback_token='', plan_json='[]')
        state_binding()
        vars['_native_write_journal'] = json.dumps(dict(reservation_id=S['reservation_id'], call_id=cid, entries=dict()))
        vars['_native_close_armed'] = ''; vars['_native_close_url'] = ''; vars['_native_close_customer'] = ''
        phrase('opening', vars)
    elif action == 'turn':
        raw = vars.get('_native_turn_json', '')
        negative = vars.get('_native_no_today_json', '')
        if raw == '' and negative:
            if negative != '{"explicit_yes":false,"kind":"pay_all"}' or S.get('phase') != 'all_today' or S.get('verified') != CFG['facts']['identity_order'] or not S.get('products') or any(p.get('status') != 'open' or p.get('draft') for p in S['products']) or S.get('plan_json') != '[]' or S.get('consent') or S.get('readback_token') or S.get('pending'):
                raise ValueError('Negative fallback cannot authorize a plan or write')
            journal = json.loads(vars.get('_native_write_journal', ''), object_pairs_hook=read_pairs)
            if journal.get('entries') != {}:
                raise ValueError('Negative fallback requires an untouched write journal')
            raw = negative
        event = json.loads(raw, object_pairs_hook=read_pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonfinite turn')))
        if not isinstance(event, dict) or S.get('pending'):
            raise ValueError('Unsupported or pending turn')
        kind = event.get('kind')
        if kind == 'person_confirm' and S['phase'] == 'person_confirm' and set(event) == set(['kind', 'explicit_yes']) and event['explicit_yes'] is True:
            S['phase'] = 'identity'; question()
        elif kind == 'identity_answer' and S['phase'] == 'identity' and set(event) == set(['kind', 'field', 'answer']):
            field = CFG['facts']['identity_order'][len(S['verified'])]
            partial = S.get('identity_partial', dict())
            if partial and (partial.get('field') != field or partial.get('call_id') != S['call_id']):
                S.pop('identity_partial', None); partial = dict()
            if event['field'] != field:
                raise ValueError('Identity factor out of order')
            answer = event['answer']
            if field == 'date_of_birth':
                try:
                    answer, components = birth_answer(answer, partial.get('components'))
                except ValueError:
                    answer, components = None, dict()
                if answer is None:
                    S['identity_partial'] = dict(field=field, call_id=S['call_id'], components=components)
                    question(); return
                S.pop('identity_partial', None)
            else:
                S.pop('identity_partial', None)
                answer = phone_answer(answer)
                if answer is None:
                    question(); return
            read_request('verify_identity', dict(customer_id=S['customer_id'], field=field, answer=answer))
        elif kind == 'unsupported' and S['phase'] == 'identity' and set(event) == {'kind'} and CFG['facts']['identity_order'][len(S['verified'])] in ('date_of_birth', 'phone_last4'):
            S.pop('identity_partial', None); question(); return
        elif kind == 'unsupported' and S['phase'] == 'all_today' and set(event) == {'kind'}:
            say('proposal_request', CFG['wording']['proposal_request']['template']); return
        elif kind == 'pay_all' and S['phase'] == 'all_today' and set(event) == set(['kind', 'explicit_yes']) and type(event['explicit_yes']) is bool:
            if event['explicit_yes']:
                for p in S['products']:
                    p['draft'] = admissible(p, p['past_due'], S['now'][:10]); p['status'] = 'agreed'
                readback()
            else:
                advance()
        elif kind == 'bundle_proposal' and S['phase'] in ('all_today', 'product', 'readback') and set(event) == set(['kind', 'date', 'amounts']):
            if not isinstance(event['amounts'], dict) or set(event['amounts']) != set(p['product_id'] for p in S['products']):
                raise ValueError('Exact unique complete bundle required')
            for p in S['products']:
                p['draft'] = admissible(p, event['amounts'][p['product_id']], event['date']); p['status'] = 'agreed'; p['reason'] = ''; p['adjusted'] = p['draft']['date'] != event['date']
            readback()
        elif kind == 'proposal' and S['phase'] == 'product' and set(event) == set(['kind', 'product_id', 'amount', 'date']):
            p = current()
            if event['product_id'] != p['product_id']:
                raise ValueError('Product order mismatch')
            p['draft'] = admissible(p, event['amount'], event['date']); p['status'] = 'agreed'; p['adjusted'] = p['draft']['date'] != event['date']; advance()
        elif kind == 'refusal' and S['phase'] == 'product' and set(event) == set(['kind', 'product_id']):
            p = current()
            if event['product_id'] != p['product_id']:
                raise ValueError('Refused wrong product')
            if p['attempt'] < p['persuasion_limit']:
                offer(p)
            else:
                p['status'] = 'refused'; p['reason'] = 'refused'; advance()
        elif kind == 'confirm' and S['phase'] == 'readback' and set(event) == set(['kind', 'readback_token', 'explicit_yes']):
            if event['explicit_yes'] is not True or event['readback_token'] != S['readback_token']:
                raise ValueError('Explicit current bundle confirmation required')
            S['consent'] = S['readback_token']; S['phase'] = 'confirmed'; select_write()
        else:
            raise ValueError('Unsupported structured answer/phase')
    elif action == 'observe':
        pending = S['pending']; raw = vars.get('_native_http_json') or vars.get('bank_' + pending.get('operation', '') + '_response.data._native_http_json', '')
        vars['_native_http_json'] = ''; observed = json.loads(raw, object_pairs_hook=read_pairs)
        if not pending or pending['dispatched'] is not True or observed.get('transport_ok') is not True:
            raise ValueError('Read result not definitively dispatched')
        op = pending['operation']; body = observed['body']; checked(body, CFG['operations'][op]['response'], True)
        if body.get('ok') is not True:
            raise ValueError('Read result not ok')
        S['pending'] = dict()
        if op == 'verify_identity':
            if body.get('verified') is True:
                S['verified'].append(pending['payload']['field'])
            elif not body.get('hint'):
                S['wrong'] += 1
            if S['wrong'] >= CFG['facts']['identity_max_wrong']:
                raise ValueError('Identity wrong-answer budget exhausted')
            if S['verified'] == CFG['facts']['identity_order']:
                S['phase'] = 'account'; read_request('get_account', dict(customer_id=S['customer_id']))
            else:
                S['phase'] = 'identity'; question(bool(body.get('hint')))
        else:
            rows = body.get('products')
            if not isinstance(rows, list) or not 1 <= len(rows) <= CFG['facts']['product_limit'] or len(set(p.get('product_id') for p in rows)) != len(rows):
                raise ValueError('One to three distinct products required')
            S['products'] = [product_bounds(p) for p in rows]
            for p in S['products']:
                say('account_summary', 'Your ' + label(p) + ' has a past-due amount of ' + money(p['past_due']) + ' and is ' + str(p['days_late']) + ' days late.')
            S['phase'] = 'all_today'; phrase('proposal_request', dict())
    elif action == 'write_gate':
        if S['phase'] != 'confirmed':
            raise ValueError('Bundle is not confirmed')
        select_write()
    elif action == 'write_result':
        result_write()
    elif action == 'terminal_arm':
        immutable_plan(); journal = json.loads(vars['_native_write_journal'])
        if S['phase'] != 'resolved' or vars.get('_native_close_armed') or any(e.get('phase') not in ('confirmed', 'failed') for e in journal['entries'].values()) or any(p['status'] == 'agreed' or p['status'] != 'recorded' and p.get('note_done') is not True for p in S['products']):
            raise ValueError('Unresolved or duplicate terminal')
        expected_keys = set()
        for p in S['products']:
            if p['status'] in ('recorded', 'service_error'):
                key = 'create_promise:' + p['product_id']; expected_keys.add(key); entry = journal['entries'].get(key, dict())
                expected_phase = 'confirmed' if p['status'] == 'recorded' else 'failed'
                if entry.get('phase') != expected_phase or entry.get('call_id') != S['call_id'] or entry.get('payload_json') != json.dumps(p['draft'], sort_keys=True, separators=(',', ':')) or expected_phase == 'failed' and (entry.get('attempts') != 2 or entry.get('proven_not_recorded') is not True):
                    raise ValueError('Missing exact product write evidence')
            if p['status'] != 'recorded':
                key = 'add_note:' + p['product_id']; expected_keys.add(key); entry = journal['entries'].get(key, dict())
                payload = dict(customer_id=S['customer_id'], product_id=p['product_id'], reason=p['reason'])
                if entry.get('phase') != 'confirmed' or entry.get('call_id') != S['call_id'] or entry.get('payload_json') != json.dumps(payload, sort_keys=True, separators=(',', ':')):
                    raise ValueError('Missing exact product note evidence')
        if set(journal['entries']) != expected_keys:
            raise ValueError('Unexpected product transaction entry')
        recorded = any(p['status'] == 'recorded' for p in S['products'])
        first = next((p['reason'] for p in S['products'] if p['reason'] and p['reason'] != 'paid'), None)
        outcome = 'promise' if recorded else 'no_promise' if first else 'paid'
        vars['_native_close_armed'] = json.dumps(dict(call_id=S['call_id'], reservation_id=S['reservation_id'], binding=CFG['binding']))
        vars['_native_close_url'] = CFG['operations']['close_call']['url']; vars['_native_close_customer'] = S['customer_id']
        vars['_native_route'] = 'official_' + outcome + ('_' + first if outcome == 'no_promise' else '')
    else:
        raise ValueError('Unknown native action')
'''


_TAIL = r'''
vars['_native_route'] = 'native_blocked'
vars['_native_speech'] = ''; vars['_native_has_speech'] = 'no'
S = dict()
try:
    if len(json.dumps(vars, allow_nan=False).encode()) > 24576:
        raise ValueError('Native variables budget exceeded')
    if ACTION != 'boot':
        S.update(json.loads(vars.get('_native_state_json', ''))); state_binding()
    cursor = len(S.get('events', []))
    step(ACTION)
    if vars['_native_route'] == 'native_blocked':
        vars['_native_route'] = 'native_listen'
    fresh = S['events'][cursor:]
    vars['_native_speech'] = ' '.join(e['text'] for e in fresh)
    vars['_native_has_speech'] = 'yes' if fresh else 'no'
except Exception as error:
    S.pop('identity_partial', None)
    S['phase'] = 'blocked'; S['diagnostic'] = str(error)
    vars['_native_route'] = 'native_blocked'
finally:
    vars['_native_turn_json'] = ''
    vars['_native_no_today_json'] = ''
    vars['_native_state_json'] = json.dumps(S, sort_keys=True, separators=(',', ':'), allow_nan=False)
    vars['_native_phase'] = S.get('phase', 'blocked'); vars['_native_readback_token'] = S.get('readback_token', '')
    _p = next((p for p in S.get('products', []) if p['product_id'] == S.get('current_product')), dict())
    vars['_native_proposal_product_id'] = _p.get('product_id', ''); vars['_native_suggested_amount'] = _p.get('offered_amount', ''); vars['_native_suggested_date'] = _p.get('offered_date', '')
    _v = S.get('verified', [])
    vars['_native_identity_field'] = CFG['facts']['identity_order'][len(_v)] if len(_v) < len(CFG['facts']['identity_order']) else ''
    vars['_native_products_json'] = json.dumps([dict(product_id=p['product_id'], label=p['type'], full=p['past_due'], floor=p['floor'], deadline=p['deadline']) for p in S.get('products', [])], separators=(',', ':'))
'''


def _driver(cfg, action, result_guard='', native_runtime_mode='host_exclusive'):
    base = ast.parse(_RUNTIME)
    primitives = '\n'.join(ast.unparse(n) for n in base.body if isinstance(n, ast.FunctionDef) and n.name in ('checked', 'day', 'cents', 'business'))
    pairs = '''def read_pairs(items):
    value = dict()
    for key, item in items:
        if key in value:
            raise ValueError('Duplicate JSON key')
        value[key] = item
    return value
'''
    multi = native_scope_code(_MULTI, native_runtime_mode)
    if native_runtime_mode == 'official_bank_session':
        multi = multi.replace("    if action == 'boot':\n", "    if action == 'boot':\n" + textwrap.indent(bank_session_boot(cfg['binding']), '        ') + '\n', 1)
    tree = ast.parse('import json, re, datetime\n' + primitives + '\n' + pairs + multi + _capsule(cfg) + 'ACTION = ' + repr(action) + '\n' + result_guard + native_scope_code(_TAIL, native_runtime_mode))
    step = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'step')
    branch = step.body[0]
    while isinstance(branch, ast.If) and ast.unparse(branch.test) != 'action == ' + repr(action):
        branch = branch.orelse[0] if len(branch.orelse) == 1 else None
    if not isinstance(branch, ast.If):
        raise FoundationError('MULTI_STATIC_ACTION_BRANCH_REQUIRED')
    step.body = copy.deepcopy(branch.body)
    # Static ACTION specialization retains exactly the selected AST branch and
    # its transitive helper closure. It never changes business predicates.
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    roots = [n for n in tree.body if not isinstance(n, ast.FunctionDef)]
    required = set()
    pending = [n.id for root in roots for n in ast.walk(root) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)]
    while pending:
        name = pending.pop()
        if name in required or name not in functions:
            continue
        required.add(name)
        pending.extend(n.id for n in ast.walk(functions[name]) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load))
    needed_facts = {n.slice.value for node in tree.body for n in ast.walk(node)
                    if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                    and isinstance(n.value, ast.Subscript) and ast.unparse(n.value) == "CFG['facts']"
                    and (not isinstance(node, ast.FunctionDef) or node.name in required)}
    runtime = dict(binding=cfg['binding'], facts={key: cfg['facts'][key] for key in needed_facts},
        constraints=cfg['constraints'], wording={}, flow={}, operations={})
    for kind in (['opening'] if action == 'boot' else ['proposal_request'] if action in ('turn', 'observe') else ['promise_success'] if action == 'write_result' else []):
        runtime['wording'][kind] = dict(template=cfg['wording'][kind]['template'])
    if 'question' in required:
        runtime['flow'] = {key: {field: dict(template=row['template']) for field, row in cfg['flow'][key].items()}
                           for key in ('identity_templates', 'identity_hint_templates')}
    # Static turn code queues only verify_identity and validates promise
    # proposals. Observe validates the two read responses and queues account.
    # Keep their reachable schemas without copying unused schemas into both.
    operation_names = {'turn': ('verify_identity', 'create_promise'),
                       'observe': ('verify_identity', 'get_account'),
                       'terminal_arm': ('close_call',)}.get(action, ())
    runtime['operations'] = {key: cfg['operations'][key] for key in operation_names}
    tree.body = [n for n in tree.body if (not isinstance(n, ast.FunctionDef) or n.name in required)
                 and not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CFG' for t in n.targets))]
    # ast.unparse canonicalizes the string escapes protecting Freya template
    # markers. Reinsert the unchanged JSON capsule after AST specialization.
    code = _compress_generated_python('import json\n' + _capsule(runtime) + ast.unparse(ast.fix_missing_locations(tree)) + '\n')
    if '{{' in code or '}}' in code:
        raise FoundationError('MULTI_GENERATED_TEMPLATE_MARKER_REFUSED')
    return code


_JOURNAL_PRE = '''import json
_native_pre_valid = False
try:
    _S = json.loads(vars['_native_state_json']); _J = json.loads(vars['_native_write_journal'])
    _key = _S['active_key']; _entry = _J['entries'][_key]
    _plans = json.loads(_S['plan_json'])
    _auth = json.loads(vars['_native_write_authorization'])
    _native_pre_valid = _S.get('binding') == BINDING and _S.get('phase') == 'write_pending' and _S.get('call_id') == vars.get('call.call_id') and _S.get('customer_id') == vars.get('customer_id') and _S.get('now') == vars.get('now') and _S.get('verified') == IDENTITY_ORDER and _S.get('consent') == _S.get('readback_token') == _auth.get('readback_token') and bool(_S.get('consent')) and _auth.get('payload') in _plans and _key == 'create_promise:' + config['body']['product_id'] and _auth.get('payload') == config['body'] and _J.get('call_id') == _S['call_id'] and _J.get('reservation_id') == _S['reservation_id'] and _S.get('plan_json') == json.dumps([dict(p['draft']) for p in _S['products'] if p['status'] in ('agreed', 'recorded', 'service_error')], sort_keys=True, separators=(',', ':'))
    vars['_native_write_state'] = json.dumps(_entry)
except Exception:
    pass
if _native_pre_valid:
'''

_JOURNAL_SUFFIX = '''
    if _allowed:
        _J['entries'][_key] = json.loads(vars['_native_write_state'])
        vars['_native_write_journal'] = json.dumps(_J, sort_keys=True, separators=(',', ':'))
else:
    vars['_native_write_route'] = 'blocked'; vars['_native_write_outcome'] = 'unknown'; vars['_native_transport_outcome'] = 'unknown'
    config['url'] = ''; config['body'] = dict(); config['headers'] = dict()
'''

_NOTE_PRE = '''import json
vars['_native_write_route'] = 'blocked'; vars['_native_write_outcome'] = 'unknown'; vars['_native_transport_outcome'] = 'unknown'
try:
    S = json.loads(vars['_native_state_json']); J = json.loads(vars['_native_write_journal']); C = json.loads(vars['_native_write_context'])
    key = S['active_key']; entry = J['entries'][key]; p = next(p for p in S['products'] if p['product_id'] == S['active_product'])
    payload = dict(customer_id=S['customer_id'], product_id=p['product_id'], reason=p['reason'])
    if S.get('binding') != BINDING or S.get('phase') != 'write_pending' or S.get('call_id') != vars.get('call.call_id') or S.get('customer_id') != vars.get('customer_id') or S.get('now') != vars.get('now') or S.get('verified') != IDENTITY_ORDER or C.get('authority') != 'host_exclusive_customer_reservation' or C.get('restart_policy') != 'never_automatically_resume_or_reinitialize' or C.get('provider_idempotency') is not False or C.get('reservation_id') != S['reservation_id'] or C.get('customer_id') != S['customer_id'] or J.get('call_id') != S['call_id'] or J.get('reservation_id') != S['reservation_id']:
        raise ValueError('Note scope mismatch')
    if key != 'add_note:' + p['product_id'] or p['status'] not in ('refused', 'paid', 'dispute', 'hardship', 'service_error') or p.get('note_done') or p['reason'] not in REASONS or config.get('body') != payload or config.get('url') != URL or config.get('method') != 'POST' or entry.get('phase') != 'ready' or entry.get('attempts') != 0:
        raise ValueError('Unexpected, changed or duplicate note')
    if p['reason'] == 'refused' and p['attempt'] != p['persuasion_limit']:
        raise ValueError('Refusal persuasion budget not exhausted')
    if p['reason'] == 'service_error':
        previous = J['entries'].get('create_promise:' + p['product_id'], dict())
        if previous.get('phase') != 'failed' or previous.get('attempts') != 2 or previous.get('proven_not_recorded') is not True:
            raise ValueError('Service error lacks two definitive failures')
    entry.update(call_id=S['call_id'], phase='dispatched', attempts=1, payload_json=json.dumps(payload, sort_keys=True, separators=(',', ':')), proven_not_recorded=False)
    J['entries'][key] = entry
    vars['_native_write_state'] = json.dumps(entry); vars['_native_write_journal'] = json.dumps(J, sort_keys=True, separators=(',', ':')); vars['_native_write_route'] = 'dispatched'
except Exception:
    config['url'] = ''; config['body'] = dict(); config['headers'] = dict()
'''


def build_multi_product_native_workflow(brief_paths, tool_api_path, kit_path, *,
                                       initial_variables=None, compilation=None, source_snapshots=None, native_runtime_mode='host_exclusive'):
    """Emit a fresh-call B graph. Runtime fixtures never admit live publication."""
    validate_runtime_mode(native_runtime_mode)
    from orchestration.product_build import source_bundle, compile_bundle
    docs, api, contract = source_bundle(brief_paths, tool_api_path)
    bound = compilation or compile_bundle(brief_paths, tool_api_path, docs, contract)
    prefix = build_native_workflow(bound['compiled'], bound['documents'], bound['wording_bundle'],
        wording_authorization=bound.get('wording_authorization'), source_snapshots=source_snapshots, native_runtime_mode=native_runtime_mode)
    cfg = _cfg(prefix)
    if cfg['facts']['product_limit'] != 3 or cfg['facts']['notes_enabled'] is not True or cfg['facts']['promise_max_attempts'] != 2 or not cfg['flow'].get('right_party_confirmation'):
        raise FoundationError('SOURCE_BOUND_BRIEF_B_THREE_PRODUCT_CONTRACT_REQUIRED')
    kit = Path(kit_path)
    b = read_source(kit / 'briefs/brief-b.md')
    references = []
    for quote in ('7. Read all promises back together and ask for one confirmation. Then call `create_promise` once per product.',
                  '6. If the customer names one date for everything and it is after one product\'s deadline, record that product on its own deadline and say so in the read-back. The other products keep the date the customer named.',
                  '8. The customer may promise some products and refuse others. For each product without a promise call `add_note` with that `product_id` and a reason: `refused`, `hardship`, `dispute`, `paid` or `service_error`. This also applies when no product gets a promise.'):
        start = b.text.find(quote)
        if start < 0:
            raise FoundationError('EXACT_BRIEF_B_RULE_REQUIRED')
        references.append(dict(asdict(b.evidence(start, start + len(quote))), path=b.path, sha256=b.sha256, quote=quote))
    if not any(d.sha256 == b.sha256 for d in docs):
        raise FoundationError('BRIEF_B_MUST_BE_ACTUAL_COMPILATION_INPUT')
    op = contract.operations['add_note']
    cfg['operations']['add_note'] = dict(url=op.url, method=op.method, request=copy.deepcopy(op.request), response=copy.deepcopy(op.response))
    # Locations remain in the review report; runtime authority follows the
    # same source bytes/spans when a judge relocates the archived inputs.
    runtime_references = [{key: value for key, value in ref.items() if key != 'path'} for ref in references]
    cfg['binding'] = hashlib.sha256(_json(dict(prefix_binding=cfg['binding'], multi_rules=runtime_references)).encode()).hexdigest()
    official = {key: read_source(kit / path) for key, path in dict(brief='briefs/brief-a.md', workflow_format='docs/workflow-format.md', tools_and_code='docs/tools-and-code.md', base_agent='examples/base-agent.json').items()}
    write = make_write_hooks(contract, source_binding=cfg['binding'], business_weekdays=cfg['facts']['business_weekdays'], holidays=cfg['facts']['holidays'], native_runtime_mode=native_runtime_mode)
    nodes = {}
    routes = ['native_listen', 'bank_verify_identity', 'bank_get_account', 'bank_create_promise', 'bank_add_note', 'native_terminal_arm', 'native_blocked']
    routes += ['official_promise', 'official_paid'] + ['official_no_promise_' + reason for reason in ('refused', 'hardship', 'dispute', 'service_error')]
    for name, action in [('native_boot', 'boot'), ('native_turn', 'turn'), ('native_observe', 'observe'), ('native_write_gate', 'write_gate'), ('native_write_result', 'write_result'), ('native_terminal_arm', 'terminal_arm')]:
        guard = write['result_guard_code'] if action == 'write_result' else ''
        possible = {'boot': ['native_listen'], 'turn': ['native_listen', 'bank_verify_identity', 'bank_create_promise', 'bank_add_note', 'native_terminal_arm'],
                    'observe': ['native_listen', 'bank_get_account'], 'write_gate': ['bank_create_promise', 'bank_add_note', 'native_terminal_arm'],
                    'write_result': ['bank_create_promise', 'bank_add_note', 'native_terminal_arm'],
                    'terminal_arm': [r for r in routes if r.startswith('official_')]}[action]
        transitions = [_edge('native_blocked', '_native_route', 'native_blocked'), _edge('native_speak', '_native_has_speech', 'yes')] + [_edge(r, '_native_route', r) for r in possible] + [_edge('native_blocked')]
        nodes[name] = _code_node(name, _driver(cfg, action, guard, native_runtime_mode), transitions)
    nodes['native_speak'] = copy.deepcopy(prefix['workflow']['nodes']['native_speak'])
    nodes['native_dispatch'] = _code_node('native_dispatch', 'pass\n', [_edge(r, '_native_route', r) for r in routes] + [_edge('native_blocked')])
    nodes['native_listen'] = copy.deepcopy(prefix['workflow']['nodes']['native_listen'])
    # The shared node handles identity, products and bundle consent. The
    # documented user-turn cap must exceed the prefix's six-turn budget.
    # This technical limit does not change identity or persuasion limits.
    nodes['native_listen']['max_node_turns'] = 20
    extraction = nodes['native_listen']['extraction_schema'][0]
    extraction['validation_pattern'] = '^[\\s\\S]{0,2048}$'
    # A custom template replaces the built-in extractor. Keep its variable
    # description and grammar aligned, and provide the synthetic call's date
    # anchor explicitly. Prompt inspection is not actual voice-model proof.
    extraction['description'] = _B_EXTRACTION_DESCRIPTION
    extraction['extraction_prompt'] = _B_EXTRACTION_PROMPT
    nodes['native_listen']['extraction_schema'].append(dict(
        name='_native_no_today_json', type='string',
        description='Standalone no-today only; never consent or a proposal.',
        enum_values=['', _B_NO_TODAY_JSON], min_confidence=0.5,
        validation_pattern='^[\\s\\S]{0,2048}$', max_retries=1,
        extraction_prompt=_B_NO_TODAY_PROMPT))
    # The ordinary field keeps its confidence threshold. This narrower signal
    # can only enter the first product offer while the plan/journal are empty.
    nodes['native_listen']['transitions'] = [dict(
        target_node='native_turn', description='Current structured input or guarded negative-only answer is present',
        condition=dict(logic='or', conditions=[
            dict(variable='_native_turn_json', operator='not_equals', operand=''),
            dict(logic='and', conditions=[
                dict(variable='_native_turn_json', operator='equals', operand=''),
                dict(variable='_native_phase', operator='equals', operand='all_today'),
                dict(variable='_native_no_today_json', operator='equals', operand=_B_NO_TODAY_JSON)])]))]
    for operation in ('verify_identity', 'get_account'):
        node = _bank_node(contract.operations[operation], cfg, native_runtime_mode); tool = node['pre_actions'][0]
        tool['pre_process_code'] = "vars['_native_http_json'] = ''\ntry:\n" + textwrap.indent(tool['pre_process_code'], '    ') + "\nexcept Exception:\n    config['url'] = ''\n    config['body'] = dict()\n    config['headers'] = dict()\n"
        tool['post_process_code'] = _READ_POST
        node['response_mapping'] = [dict(path='data._native_http_json', variable='_native_http_json')]
        nodes['bank_' + operation] = node
    tool = copy.deepcopy(write['tool']); mapping = tool.pop('response_mapping')
    tool['pre_process_code'] = native_scope_code(_JOURNAL_PRE, native_runtime_mode).replace('BINDING', repr(cfg['binding'])).replace('IDENTITY_ORDER', repr(cfg['facts']['identity_order'])) + textwrap.indent(tool['pre_process_code'], '    ') + _JOURNAL_SUFFIX
    for operation in ('create_promise', 'add_note'):
        if operation == 'add_note':
            tool = copy.deepcopy(tool)
            props = tool['function']['parameters']['properties']
            tool['function']['name'] = 'add_note'; tool['function']['description'] = 'Record only a source-derived, resolved product reason once.'
            props['url']['default'] = op.url
            props['body'] = dict(type='object', required=op.request['required'], properties={name: dict(op.request['properties'][name], default='{{_native_arg_' + name + '}}') for name in op.request['required']})
            tool['pre_process_code'] = native_scope_code(_NOTE_PRE, native_runtime_mode).replace('BINDING', repr(cfg['binding'])).replace('IDENTITY_ORDER', repr(cfg['facts']['identity_order'])).replace('REASONS', repr(op.request['properties']['reason']['enum'])).replace('URL', repr(op.url))
        nodes['bank_' + operation] = dict(name='bank_' + operation, node_type='api_request', on_enter=dict(type='silent'), passthrough=True, pre_actions=[tool], response_mapping=mapping, passthrough_default_transition='native_write_result', transitions=[_edge('native_write_result')])
    for outcome, reasons in [('promise', [None]), ('paid', [None]), ('no_promise', ['refused', 'hardship', 'dispute', 'service_error'])]:
        for reason in reasons:
            closure = compile_official_closure(official, contract, outcome=outcome, reason=reason, customer_id_variable='customer_id', farewell=cfg['wording']['goodbye']['template'])
            name = 'official_' + outcome + ('_' + reason if reason else '')
            end = dict(type='function', config=dict(maxRetries=0, timeoutMs=5000), function=copy.deepcopy(closure['terminal_node']['terminal']['function']))
            close = copy.deepcopy(closure['lifecycle_tool_template'])
            props = close['function']['parameters']['properties']; props['url']['default'] = '{{_native_close_url}}'; props['body']['properties']['customer_id']['default'] = '{{_native_close_customer}}'
            nodes[name] = dict(name=name, node_type='conversation', on_enter=dict(type='llm'), task_message='Invoke official_farewell_end silently. The handler supplies the farewell. Call no other tool.', disable_inactivity_monitor=True, tools=[end, close], transitions=[])
    nodes['native_blocked'] = copy.deepcopy(nodes['official_promise']); nodes['native_blocked']['name'] = 'native_blocked'; nodes['native_blocked']['tools'] = nodes['native_blocked']['tools'][:1]
    graph = dict(initial_node='native_boot', nodes=nodes, system_prompt='Execute only source-bound deterministic banking rules. Do not invent successful results, consent, dates or restart continuity.')
    for node in nodes.values():
        for edge in node.get('transitions', []):
            # Every emitted edge is deterministic. Its condition owns routing;
            # empty model descriptions save the publication body's small cap.
            edge['description'] = ''
        for action in node.get('pre_actions', []) + node.get('tools', []):
            action['function'].setdefault('description', '')
            for field in ('pre_process_code', 'post_process_code'):
                if field in action:
                    action[field] = _compress_generated_python(action[field]); compile(action[field], '<native-b-hook>', 'exec')
    budget = measure_code_exec_budget(graph, initial_variables or {}, reserve_variable_bytes=24576)
    body = dict(name='Native-B-ThreeProducts 00000000-0000-0000-0000-000000000000', workflow=graph)
    request_bytes = len(json.dumps(body, allow_nan=False).encode())
    if not budget['passed'] or request_bytes >= 131072:
        raise FoundationError('MULTI_NATIVE_PUBLICATION_OR_CODE_VARS_BUDGET_EXCEEDED: ' + str(request_bytes))
    result = dict(format_version='native-multi-product-1', workflow=graph, config=cfg,
        workflow_sha256=hashlib.sha256(_json(graph).encode()).hexdigest(), budget=dict(budget, full_api_request_bytes=request_bytes, full_api_request_limit_bytes=131072), input_inventory=[d.inventory() for d in docs] + [api.inventory()], source_references=references,
        readiness=dict(scope='fresh host-exclusive Brief B structured promises/refusals; one joint consent; serial writes', publish_allowed=False, bank_writes_enabled=False, actual_native_closure_verified=False, platform_schema_validated=False, automatic_restart_allowed=False, native_atomic_persistence_claim=False, provider_idempotency_claim=False, supported=['one_to_three_accounts', 'product_order', 'product_local_deadline_and_floor', 'joint_readback_confirmation', 'per_product_serial_promise_guard', 'definite_ok_false_retry_once', 'deferred_refused_and_service_error_notes', 'official_outcome_and_terminal_shape'], unsupported=['arbitrary_voice_intent', 'resumed_calls', 'callbacks', 'transfers', 'hardship_support_offer', 'below_floor_counteroffer', 'dispute_and_paid_turns', 'full_brief_branch_coverage'], official_competition_PASS=False))
    if native_runtime_mode == 'official_bank_session':
        result['native_runtime_mode'] = native_runtime_mode
        result['readiness']['scope'] = 'One uninterrupted fresh official bank session; one joint consent and per-product inline replay protection; cold-state-loss recovery unsupported'
    return result


class MultiProductNativeHarness(OfficialNativeHarness):
    """Actual emitted graph/hook interpreter with explicit local envelopes."""
    def dispatch(self, operation):
        if operation != 'add_note':
            return super().dispatch(operation)
        if self.current_node != 'bank_add_note':
            raise FoundationError('LOCAL_NOTE_REQUIRES_CURRENT_BANK_NODE')
        tool = self.candidate['workflow']['nodes'][self.current_node]['pre_actions'][0]
        props = tool['function']['parameters']['properties']
        config = dict(url=props['url']['default'], method=props['method']['default'], body={key: copy.deepcopy(self.variables.get('_native_arg_' + key)) for key in props['body']['properties']})
        exec(tool['pre_process_code'], dict(vars=self.variables, config=config))
        self.dispatch_attempts.append(dict(operation=operation, **copy.deepcopy(config)))
        if config.get('url'):
            self.requests.append(dict(operation=operation, **copy.deepcopy(config)))
        return config

    def resume(self, operation, envelope):
        if operation != 'add_note':
            return super().resume(operation, envelope)
        if self.current_node != 'bank_add_note' or not isinstance(envelope, dict):
            raise FoundationError('LOCAL_NOTE_RESULT_REQUIRES_CURRENT_BANK_NODE')
        node = self.candidate['workflow']['nodes'][self.current_node]; result = copy.deepcopy(envelope)
        exec(node['pre_actions'][0]['post_process_code'], dict(result=result))
        for mapping in node['response_mapping']:
            value = result
            for key in mapping['path'].split('.'):
                value = value[key]
            self.variables[mapping['variable']] = value
        return self.drive(self._choose(node))


def verify_multi_product_candidate(candidate, public_test_path=None, *, fixture_cases=None):
    """Replay supported public B fixtures on original and serialized graph.

    This is source-bounded local evaluation, not a provider result. The inputs
    and expected banking records are copied from the supplied unmodified kit;
    neither emission nor a repair step is allowed to replace that oracle.
    """
    from .models import strict_json
    if fixture_cases is not None:
        if public_test_path is not None or not isinstance(fixture_cases, (dict, list)):
            raise FoundationError('EXCLUSIVE_ARCHIVED_B_FIXTURE_INPUT_REQUIRED')
        rows = copy.deepcopy(list(fixture_cases.values()) if isinstance(fixture_cases, dict) else fixture_cases)
        source = None
        source_inventory = dict(kind='archived_selected_public_B_cases', sha256=hashlib.sha256(_json(rows).encode()).hexdigest(),
                                digest_kind='canonical_selected_fixture_bytes; outer delivery owns original public file pin')
    else:
        if public_test_path is None:
            raise FoundationError('EXPLICIT_PUBLIC_B_FIXTURE_INPUT_REQUIRED')
        source = read_source(public_test_path)
        rows = strict_json(source.text); source_inventory = source.inventory()
    cases = {row['id']: row for row in rows if row['id'] in ('PUB-11', 'PUB-12', 'PUB-13')}
    if set(cases) != set(['PUB-11', 'PUB-12', 'PUB-13']):
        raise FoundationError('PUBLIC_B_FIXTURES_REQUIRED')
    frozen = copy.deepcopy(cases)
    serialized = json.loads(json.dumps(candidate, allow_nan=False))
    reports = []
    began = time.perf_counter()
    for case_id in ('PUB-11', 'PUB-12', 'PUB-13'):
        case = frozen[case_id]
        with tempfile.TemporaryDirectory(prefix='native-b-replay-') as registry:
            # Stable synthetic admission is established BEFORE graph execution,
            # including the fsynced local reservation file. No recorded trace,
            # journal value or result is stripped or normalized afterwards.
            variables = dict(customer_id='local-pub01-fixture',
                             **{'call.call_id': '11111111-2222-4333-8444-555555555555'})
            variables.update(case['variables'])
            operation_id = '00000000-0000-4000-8000-0000000000' + case_id[-2:]
            if candidate.get('native_runtime_mode') == 'official_bank_session':
                variables.pop('call.call_id')
            else:
                variables.update(reserve_single_call(registry, workspace_id='local-fixture-workspace',
                                 customer_id=variables['customer_id'], operation_id=operation_id))
            traces = []
            for actual in (candidate, serialized):
                h = MultiProductNativeHarness(actual, variables)
                h.start(); h.receive(dict(kind='person_confirm', explicit_yes=True))
                for field in actual['config']['facts']['identity_order']:
                    if case_id == 'PUB-12' and field == 'phone_last4':
                        h.receive(dict(kind='identity_answer', field=field, answer='4401'))
                        if not h.dispatch('verify_identity').get('url'):
                            raise FoundationError('B_LOCAL_IDENTITY_DISPATCH_BLOCKED')
                        h.resume('verify_identity', local_http(dict(ok=True, verified=False)))
                    h.receive(dict(kind='identity_answer', field=field, answer=case['identity'][field]))
                    if not h.dispatch('verify_identity').get('url'):
                        raise FoundationError('B_LOCAL_IDENTITY_DISPATCH_BLOCKED')
                    h.resume('verify_identity', local_http(dict(ok=True, verified=True)))
                if not h.dispatch('get_account').get('url'):
                    raise FoundationError('B_LOCAL_ACCOUNT_DISPATCH_BLOCKED')
                products = [dict(p, product_id='p' + str(index + 1)) for index, p in enumerate(case['products'])]
                h.resume('get_account', local_http(dict(ok=True, products=products)))
                h.receive(dict(kind='pay_all', explicit_yes=False))
                if case_id == 'PUB-12':
                    # Drive with the independent customer script: full card
                    # tomorrow. Expected promise values are only the oracle.
                    tomorrow = (date.fromisoformat(case['variables']['now'][:10]) + timedelta(days=1)).isoformat()
                    h.receive(dict(kind='proposal', product_id='p1', amount=products[0]['past_due'], date=tomorrow))
                    h.receive(dict(kind='refusal', product_id='p2')); h.receive(dict(kind='refusal', product_id='p2'))
                    h.receive(dict(kind='refusal', product_id='p3'))
                else:
                    # Customer's one date is explicit in the official script;
                    # the loan adjustment is computed by emitted code, never
                    # supplied as a separately precalculated input.
                    today = date.fromisoformat(case['variables']['now'][:10])
                    requested = str(today.year) + '-10-30' if case_id == 'PUB-13' else (today + timedelta(days=(4 - today.weekday()) % 7)).isoformat()
                    h.receive(dict(kind='bundle_proposal', date=requested,
                                   amounts={p['product_id']: p['past_due'] for p in products}))
                h.receive(dict(kind='confirm', explicit_yes=True, readback_token=h.state['readback_token']))
                for unused in range(8):
                    if h.current_node not in ('bank_create_promise', 'bank_add_note'):
                        break
                    operation = 'create_promise' if h.current_node == 'bank_create_promise' else 'add_note'
                    if not h.dispatch(operation).get('url'):
                        raise FoundationError('B_LOCAL_WRITE_DISPATCH_BLOCKED')
                    h.resume(operation, local_http(dict(ok=True)))
                if not h.current_node.startswith('official_'):
                    raise FoundationError('B_LOCAL_GRAPH_DID_NOT_RESOLVE')
                h.complete_terminal_fixture()
                if not h.dispatch_lifecycle_fixture().get('url'):
                    raise FoundationError('B_LOCAL_TERMINAL_DISPATCH_BLOCKED')
                promises = [{name: r['body'][name] for name in ('product_id', 'amount', 'date')}
                            for r in h.requests if r['operation'] == 'create_promise']
                notes = [{name: r['body'][name] for name in ('product_id', 'reason')}
                         for r in h.requests if r['operation'] == 'add_note']
                actual_ledger = dict(promises=promises, notes=notes, callbacks=[], transfers=[],
                                     outcome=h.requests[-1]['body']['outcome'], reason=h.requests[-1]['body'].get('reason'))
                if actual_ledger != case['expected']:
                    raise FoundationError('B_LOCAL_BANK_LEDGER_DIFFERS_FROM_PUBLIC_ORACLE')
                journal = json.loads(h.variables['_native_write_journal'])
                if sum(e['kind'] == 'readback' for e in h.state['events']) != 1 or any(e['phase'] != 'confirmed' for e in journal['entries'].values()):
                    raise FoundationError('B_LOCAL_CONFIRMATION_OR_JOURNAL_INCOMPLETE')
                traces.append(dict(requests=h.requests, tts_events=h.tts_events, semantic_events=h.state['events'], traversal=h.traversal,
                                   ledger=actual_ledger, journal=journal, terminal_fixture=h.terminal_fixture, code_measurements=h.code_measurements))
            if traces[0] != traces[1]:
                raise FoundationError('B_LOCAL_SERIALIZED_EXPORT_TRACE_MISMATCH')
            reports.append(dict(case_id=case_id, passed=True, original_graph_trace=traces[0], serialized_graph_trace=traces[1], exact_trace_equal=True))
    if source is not None and hashlib.sha256(Path(public_test_path).read_bytes()).hexdigest() != source.sha256:
        raise FoundationError('PUBLIC_B_FIXTURE_CHANGED_DURING_VERIFICATION')
    return dict(verdict='LOCAL_NATIVE_B_PUB11_13_GRAPH_FIXTURE_PASS', passed=True, source=source_inventory,
                checks=dict(public_expectations_unchanged=True, three_case_bank_ledgers_exact=True, one_joint_readback_confirmation=True,
                            original_and_serialized_traces_equal=True), cases=reports, total_seconds=time.perf_counter() - began,
                network_calls=0, platform_schema_validated=False, native_playback_verified=False,
                official_competition_PASS=False, native_atomic_persistence_claim=False, provider_idempotency_claim=False)
