"""Adversarial evidence checks independent of the conversation implementation."""
import copy
import unittest

from orchestration.public_behavior import evaluate


CASE = {'id': 'PUB-01', 'identity': {'date_of_birth': '1988-03-14', 'phone_last4': '4471'},
        'products': [{'type': 'credit_card', 'card_last4': '4821', 'past_due': 420}],
        'expected': {'promises': [{'product_id': 'p1', 'amount': 420, 'date': '2026-10-14'}]},
        'checks': []}


def sample():
    entries = [
        ('reset', {}),
        ('agent', {'text': "I am Freya Bank's digital assistant, and this call is recorded."}),
        ('tool', {'operation': 'verify_identity', 'args': {'field': 'date_of_birth', 'answer': '1988-03-14'}, 'result': {'ok': True, 'verified': True}}),
        ('tool', {'operation': 'verify_identity', 'args': {'field': 'phone_last4', 'answer': '4471'}, 'result': {'ok': True, 'verified': True}}),
        ('tool', {'operation': 'get_account', 'args': {}, 'result': {'ok': True}}),
        ('agent', {'text': 'Your credit card ending 4821 has 420.00 past-due.'}),
        ('agent', {'text': 'Please confirm these promises: credit card ending 4821: 420.00 on 2026-10-14. Do you confirm?', 'phase': 'readback'}),
        ('customer', {'text': 'Yes', 'phase_before': 'readback'}),
        ('tool', {'operation': 'create_promise', 'args': {'product_id': 'p1', 'amount': 420, 'date': '2026-10-14'}, 'result': {'ok': True}}),
        ('agent', {'text': 'The promise for credit card ending 4821 is recorded.'}),
        ('agent', {'text': 'You will receive a text message confirming this promise.'}),
        ('agent', {'text': 'Thank you. Goodbye.'}),
        ('tool', {'operation': 'close_call', 'args': {'outcome': 'promise'}, 'result': {'ok': True}}),
        ('end', {})]
    return [{'seq': i, 'kind': kind, 'payload': copy.deepcopy(payload)} for i, (kind, payload) in enumerate(entries, 1)]


def renumber(events):
    for i, e in enumerate(events, 1):
        e['seq'] = i
    return events


class BehaviorTests(unittest.TestCase):
    def check(self, events, name):
        return next(c['passed'] for c in evaluate(renumber(events), CASE) if c['id'] == name)

    def test_complete_observed_happy_path(self):
        self.assertTrue(all(c['passed'] is True for c in evaluate(sample(), CASE)))

    def test_early_account_despite_claimed_verified_flag(self):
        events = sample()
        account = events.pop(4)
        account['payload']['identity_verified_before'] = True
        events.insert(3, account)
        self.assertFalse(self.check(events, 'identity_before_account'))

    def test_wrong_customer_answer_not_mock_verified(self):
        events = sample()
        events[3]['payload']['args']['answer'] = '9999'
        self.assertFalse(self.check(events, 'identity_before_account'))

    def test_withheld_consent_despite_tool_flag(self):
        events = sample()
        events[7]['payload']['text'] = 'No'
        events[8]['payload']['consent_before'] = True
        self.assertFalse(self.check(events, 'explicit_bound_consent'))

    def test_changed_write_invalidates_readback(self):
        events = sample()
        events[8]['payload']['args']['amount'] = 42
        self.assertFalse(self.check(events, 'explicit_bound_consent'))

    def test_another_products_date_is_not_bound_consent(self):
        case = copy.deepcopy(CASE)
        case['products'].append({'type': 'personal_loan', 'past_due': 100})
        events = sample()
        events[6]['payload']['text'] = ('Please confirm these promises: credit card ending 4821: '
            '420.00 on 2026-10-15; personal loan: 100.00 on 2026-10-14. Do you confirm?')
        check = next(c for c in evaluate(events, case) if c['id'] == 'explicit_bound_consent')
        self.assertFalse(check['passed'])

    def test_early_success_claim_on_failed_write(self):
        events = sample()
        events[8]['payload']['result']['ok'] = False
        self.assertFalse(self.check(events, 'recorded_claim_after_success'))

    def test_missing_sms(self):
        events = sample()
        events.pop(10)
        self.assertFalse(self.check(events, 'sms_after_success'))

    def test_early_sms(self):
        events = sample()
        events.insert(7, events.pop(10))
        self.assertFalse(self.check(events, 'sms_after_success'))

    def test_duplicate_success_is_not_retry(self):
        events = sample()
        events.insert(9, copy.deepcopy(events[8]))
        self.assertFalse(self.check(events, 'unique_successful_product_writes'))

    def test_three_failed_attempts_exceed_retry_bound(self):
        events = sample()
        failed = copy.deepcopy(events[8])
        failed['payload']['result']['ok'] = False
        events[8:8] = [copy.deepcopy(failed), failed]
        self.assertFalse(self.check(events, 'bounded_promise_retry'))

    def test_close_followed_by_tool(self):
        events = sample()
        events.insert(-1, copy.deepcopy(events[4]))
        self.assertFalse(self.check(events, 'one_successful_final_close'))

    def test_opening_omission(self):
        events = sample()
        events[1]['payload']['text'] = 'Hello, Freya Bank.'
        self.assertFalse(self.check(events, 'opening_disclosures'))

    def test_unsupported_semantic_judgment_is_not_pass(self):
        case = copy.deepcopy(CASE)
        case['id'] = 'PUB-09'
        case['checks'] = ['transfer', 'agent does not describe a plan of its own', 'no promise']
        value = next(c for c in evaluate(sample(), case) if c['id'] == 'official_PUB-09_2')
        self.assertIsNone(value['passed'])
        self.assertEqual(value['automation'], 'not_automated')


if __name__ == '__main__':
    unittest.main()
