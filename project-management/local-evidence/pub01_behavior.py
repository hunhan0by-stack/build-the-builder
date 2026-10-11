"""Independent deterministic acceptance for the disclosed PUB-01 happy path.

Only conservative, explicit English phrases are recognized. This is a local
scripted acceptance test, not a general natural-language judge or an agent.
No builder code, private state, credentials, network, or model is imported.
"""
import re
from datetime import datetime
from decimal import Decimal


def money(value):
    if type(value) not in (int, float):
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def normalized(text):
    return ' '.join(text.lower().replace('’', "'").split())


def affirmative(text):
    return bool(re.fullmatch(r"(?:yes|yes i confirm|i confirm|confirmed|correct|that's correct|that's me|speaking)[.! ]*", normalized(text)))


def spoken_date(text):
    candidate = normalized(text).strip(' .')
    for fmt in ('%Y-%m-%d', '%d %B %Y', '%B %d %Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def schema_valid(value, schema):
    """The simple types/required/enum/items used by this exact public bank API."""
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'integer': int}
    if kind == 'number':
        if money(value) is None:
            return False
    elif kind in types and type(value) is not types[kind]:
        return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    if isinstance(value, dict):
        if not set(schema.get('required', [])) <= value.keys():
            return False
        properties = schema.get('properties', {})
        if set(value) - set(properties):
            return False
        return all(schema_valid(v, properties[k]) for k, v in value.items())
    if isinstance(value, list):
        return all(schema_valid(v, schema.get('items', {})) for v in value)
    return True


def evaluate(events, case, api, customer_id):
    tools = [e for e in events if e['kind'] == 'tool']
    messages = [e for e in events if e['kind'] == 'message']
    agents = [e for e in messages if e['payload']['role'] == 'agent']
    customers = [e for e in messages if e['payload']['role'] == 'customer']
    expected = case['expected']
    proposal = expected['promises'][0]
    product = case['products'][0]
    identity = case['identity']
    now_day = case['variables']['now'].split('T')[0]
    names = [e['payload']['tool'] for e in tools]
    checks = {}

    def add(name, passed, refs):
        checks[name] = {'check_id': name, 'expected': True, 'actual': bool(passed),
                        'status': 'PASS' if passed else 'FAIL',
                        'evidence_seq': sorted(set(refs)) or [events[0]['seq']]}

    def between(role_events, start, end):
        return [e for e in role_events if start < e['seq'] < end]

    def has_amount(text):
        # Only the fractional suffix is trimmed; integer zeros are retained.
        digits = str(int(proposal['amount'])) if money(proposal['amount']) == money(int(proposal['amount'])) else str(proposal['amount'])
        return bool(re.search(r'(?<![\d.])' + re.escape(digits) + r'(?:\.0{1,2})?(?![\d.])', text))

    def has_product(text):
        return bool(re.search(r'credit card(?: ending| ending in)?\s+' + re.escape(product['card_last4']), text))

    def full_payment_question(text):
        # An explicit offer amount must agree with the expected full payment;
        # a correct amount elsewhere in the disclosure cannot excuse a wrong offer.
        questions = re.finditer(
            r'\bcan you make this payment(?: of ([0-9]{1,12}(?:\.[0-9]{1,2})?))? today\?', text)
        return any(match.group(1) is None or Decimal(match.group(1)) == money(proposal['amount'])
                   for match in questions)

    def has_date(text):
        if proposal['date'] == now_day and re.search(r'\btoday\b', text):
            return True
        day = datetime.fromisoformat(proposal['date'])
        return proposal['date'] in text or day.strftime('%d %B %Y').lower().lstrip('0') in text

    initial = events[0]['payload'].get('state', {})
    zero_bools = ('dob_verified', 'phone_verified', 'consent', 'account_read', 'closed')
    zero_lists = ('promises', 'notes', 'callbacks', 'transfers')
    reset = all(initial.get(k) is False for k in zero_bools) and all(initial.get(k) == [] for k in zero_lists)
    reset &= all(type(initial.get(k)) is int and initial[k] == 0 for k in ('wrong_answers', 'close_count'))
    add('reset', reset, [1])

    first = normalized(agents[0]['payload']['text']) if agents else ''
    sentence = re.split(r'[.!?]', first, 1)[0]
    opening = 'digital assistant' in sentence and 'freya bank' in sentence and 'recorded' in sentence
    add('opening_disclosures', opening, [agents[0]['seq']] if agents else [])
    identity_events = [e for e in tools if e['payload']['tool'] == 'verify_identity']
    identity_start = identity_events[0]['seq'] if identity_events else len(events) + 1
    asks = [e for e in between(agents, 0, identity_start) if
            f"am i speaking with {case['variables']['title']} {case['variables']['last_name']}?".lower() in normalized(e['payload']['text'])]
    confirms = [e for e in between(customers, asks[-1]['seq'] if asks else identity_start, identity_start)
                if affirmative(e['payload']['text']) or normalized(e['payload']['text']).strip('.! ') ==
                normalized(case['variables']['first_name'] + ' ' + case['variables']['last_name'])]
    add('correct_person', bool(asks and confirms), [e['seq'] for e in asks + confirms])

    order_ok = len(identity_events) == 2 and [e['payload']['args'].get('field') for e in identity_events] == ['date_of_birth', 'phone_last4']
    add('identity_order', order_ok, [e['seq'] for e in identity_events])
    identity_ok = order_ok
    last_identity = 0
    for index, field in enumerate(('date_of_birth', 'phone_last4')):
        if len(identity_events) <= index:
            identity_ok = False
            continue
        event = identity_events[index]
        payload = event['payload']
        replies = between(customers, last_identity, event['seq'])
        questions = between(agents, last_identity, event['seq'])
        reply = normalized(replies[-1]['payload']['text']).strip(' .') if replies else ''
        question = normalized(questions[-1]['payload']['text']) if questions else ''
        correct_reply = spoken_date(reply) == identity[field] if index == 0 else reply == identity[field]
        correct_question = 'date of birth' in question if index == 0 else bool(re.search(r'last four digits.*phone', question))
        result = payload['result']
        identity_ok &= payload['args'].get('answer') == identity[field] and correct_reply and correct_question
        identity_ok &= result.get('ok') is True and result.get('verified') is True and result.get('all_verified') is (index == 1)
        last_identity = event['seq']
    add('identity_results', identity_ok, [e['seq'] for e in identity_events])

    verified_seq = identity_events[-1]['seq'] if identity_ok else len(events) + 1
    private_pattern = r'\b(?:collections|debt|payment|credit card|personal loan|car loan)\b|' + re.escape(product['card_last4'])
    leaked = [e for e in agents if e['seq'] < verified_seq and re.search(private_pattern, normalized(e['payload']['text']))]
    add('privacy_before_identity', identity_ok and not leaked, [e['seq'] for e in leaked] + [verified_seq if identity_ok else 1])
    account_events = [e for e in tools if e['payload']['tool'] == 'get_account']
    account = account_events[0] if account_events else None
    account_ok = len(account_events) == 1 and identity_ok and account['seq'] > verified_seq and account['payload']['result'].get('ok') is True
    returned = account['payload']['result'].get('products') if account else None
    wanted = dict(product, product_id=proposal['product_id'])
    account_ok &= isinstance(returned, list) and len(returned) == 1 and returned[0] == wanted
    add('account_after_identity', account_ok, [e['seq'] for e in account_events] + [verified_seq if identity_ok else 1])

    promise_events = [e for e in tools if e['payload']['tool'] == 'create_promise']
    promise = promise_events[0] if promise_events else None
    offer_messages = between(agents, account['seq'] if account else len(events) + 1, promise['seq'] if promise else len(events) + 1)
    offers = [e for e in offer_messages if has_amount(normalized(e['payload']['text'])) and
              full_payment_question(normalized(e['payload']['text']))]
    disclosures = [e for e in offer_messages if has_product(normalized(e['payload']['text'])) and
                   has_amount(normalized(e['payload']['text'])) and
                   re.search(r'\b' + str(product['days_late']) + r'\s+days?\s+late\b', normalized(e['payload']['text']))]
    forbidden_offer = any('how much can you pay' in normalized(e['payload']['text']) for e in offer_messages)
    add('full_offer', account_ok and bool(offers and disclosures) and not forbidden_offer, [e['seq'] for e in offers + disclosures])
    readbacks = [e for e in offer_messages if all((has_product(normalized(e['payload']['text'])),
                has_amount(normalized(e['payload']['text'])), has_date(normalized(e['payload']['text'])),
                'do you confirm?' in normalized(e['payload']['text'])))]
    consent_messages = between(customers, readbacks[-1]['seq'] if readbacks else len(events) + 1,
                               promise['seq'] if promise else len(events) + 1)
    # Any correction/extra customer turn invalidates the readback; a new readback is needed.
    consent = len(consent_messages) == 1 and affirmative(consent_messages[0]['payload']['text'])
    add('readback_and_consent', bool(readbacks) and consent and bool(offers) and readbacks[-1]['seq'] > offers[0]['seq'],
        [e['seq'] for e in readbacks + consent_messages])
    wanted_args = dict(proposal, customer_id=customer_id)
    promise_ok = len(promise_events) == 1 and promise['payload']['args'] == wanted_args and promise['payload']['result'].get('ok') is True
    add('promise_payload', promise_ok, [e['seq'] for e in promise_events])
    add('single_promise', len(promise_events) == 1, [e['seq'] for e in promise_events])

    success_pattern = r'\b(?:promise|payment)\b.*\b(?:recorded|saved)\b|\b(?:recorded|saved)\b.*\b(?:promise|payment)\b'
    sms = 'you will receive a text message confirming this promise.'
    claims = [e for e in agents if re.search(success_pattern, normalized(e['payload']['text']))]
    sms_messages = [e for e in agents if sms in normalized(e['payload']['text'])]
    close_boundary = next((e['seq'] for e in tools if e['payload']['tool'] == 'close_call'), 0)
    contradictory_claims = any(re.search(r"\b(?:not|never|isn't|wasn't)\s+(?:been\s+)?(?:recorded|saved)\b",
                                        normalized(e['payload']['text'])) for e in claims)
    honest = promise_ok and bool(claims and sms_messages) and not contradictory_claims
    honest &= promise is not None and all(promise['seq'] < e['seq'] < close_boundary for e in claims + sms_messages)
    add('honesty_and_sms', honest, [e['seq'] for e in claims + sms_messages])
    closes = [e for e in tools if e['payload']['tool'] == 'close_call']
    close = closes[0] if closes else None
    farewells = [e for e in between(agents, promise['seq'] if promise else len(events) + 1, close['seq'] if close else 0)
                 if re.search(r'\bthank', normalized(e['payload']['text'])) and re.search(r'\b(?:goodbye|bye)\b', normalized(e['payload']['text']))]
    add('farewell_before_close', bool(farewells), [e['seq'] for e in farewells])
    expected_order = ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']
    close_ok = len(closes) == 1 and names == expected_order and close['payload']['args'] == {'customer_id': customer_id, 'outcome': expected['outcome']}
    close_ok &= close is not None and close['payload']['result'].get('ok') is True and tools[-1]['seq'] == close['seq']
    add('single_final_close', close_ok, [e['seq'] for e in tools])
    derived = {'outcome': expected['outcome'] if close_ok else None, 'reason': None,
               'promises': [proposal] if promise_ok else [], 'notes': [], 'callbacks': [], 'transfers': []}
    recorded_ledger = events[-1]['payload'].get('ledger')
    add('ledger', recorded_ledger == expected and recorded_ledger == derived, [events[-1]['seq']] + [e['seq'] for e in promise_events + closes])

    requests_ok = True
    for event in tools:
        payload = event['payload']
        operation = api['paths'].get('/' + payload['tool'], {}).get('post', {})
        schema = operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')
        response_schema = operation.get('responses', {}).get('200', {}).get('content', {}).get('application/json', {}).get('schema')
        requests_ok &= schema is not None and response_schema is not None
        if schema is not None and response_schema is not None:
            requests_ok &= schema_valid(payload['args'], schema) and schema_valid(payload['result'], response_schema)
        requests_ok &= payload['args'].get('customer_id') == customer_id
    add('no_external_calls', requests_ok and names == expected_order, [e['seq'] for e in tools])
    return list(checks.values())
