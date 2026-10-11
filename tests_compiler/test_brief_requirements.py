"""New source-form fixtures; bounded derivation is not hidden/live success."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from builder.api_contract import BankContract
from builder.brief_requirements import (compile_brief_requirements, compile_requirement_documents,
                                       derive_brief_requirements, emit_native_requirements)
from builder.ingest import collect_sources
from builder.models import FoundationError
from builder.native_freya import NativeHarness


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'data/freya-hackathon/kit-v3/build-the-builder/tools/openapi.json'
OUTCOMES = [
    {'outcome': 'promise', 'reasons': [], 'when': 'confirmed promise'},
    {'outcome': 'paid', 'reasons': [], 'when': 'all products paid'},
    {'outcome': 'transferred', 'reasons': ['human'], 'when': 'human request'},
    {'outcome': 'no_promise', 'reasons': ['refused', 'service_error'], 'when': 'no recorded promise'},
    {'outcome': 'not_verified', 'reasons': ['failed', 'declined'], 'when': 'identity incomplete'},
    {'outcome': 'not_reached', 'reasons': ['wrong_number'], 'when': 'wrong recipient'},
]
BANDS = [{'name': 'source-window', 'min_days_late': 1, 'max_days_late': 70,
          'deadline_business_days': 9, 'persuasion_attempts': 1}]
FLOORS = {'credit_card': 'minimum_payment', 'personal_loan': 'instalment_amount', 'car_loan': 'instalment_amount'}
WORDING = {
    'opening': 'This is a digital assistant for {bank_name}; this call is recorded.',
    'identity_question': 'Please provide your {identity_field}.',
    'account_summary': 'Your {product_label} has {past_due} overdue.',
    'proposal_request': 'Can you pay {amount} on {date}?',
    'readback': 'Please confirm {amount} on {date} for your {product_label}.',
    'promise_success': 'The promise was recorded successfully.',
    'goodbye': 'Thank you for your time. Goodbye.',
}
RULES = [
    'Business days are Monday to Thursday.',
    'Bank holidays are 2026-10-19, 2026-12-30.',
    'Identity questions, in order: phone_last4, date_of_birth.',
    'Maximum wrong identity answers: 4.',
    'Maximum products: 1.',
    'Notes are disabled.',
    'Promise write attempts: 2.',
    'Floor fields: ' + json.dumps(FLOORS, sort_keys=True) + '.',
    'Delinquency bands: ' + json.dumps(BANDS, sort_keys=True) + '.',
    'Read accounts only after all required identity questions pass.',
    'Record promises only after an explicit current readback confirmation.',
    'Record promises only within the account floor and past-due amount.',
    'Record promises only on business days.',
    'Maximum payment amount: 315.75.',
    'Latest payment date: 2026-10-28.',
    'Say goodbye before closing the call.',
    'Close each call once, after all other actions.',
]
# Independent form-specific expected values are fixed before production runs.
EXPECTED = {
    'clean': {'ceiling': 315.75, 'holidays': ['2026-10-19', '2026-12-30'],
              'identity_order': ['phone_last4', 'date_of_birth'], 'latest': '2026-10-28'},
    'raw': {'ceiling': 287.50, 'holidays': ['2026-10-22'],
            'identity_order': ['date_of_birth', 'phone_last4'], 'latest': '2026-10-21'},
}


def fixtures(directory):
    directory = Path(directory)
    clean = directory / 'never-used-clean-spec.md'
    lines = ['# Collection specification', 'Policy ID: clean-controls-47']
    lines += [str(i + 1) + '. ' + text for i, text in enumerate(RULES)]
    lines += ['18. Closing outcomes: ' + json.dumps(OUTCOMES, sort_keys=True) + '.']
    lines += ['Wording ' + name + ': ' + template for name, template in WORDING.items()]
    clean.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    policy = directory / 'bank-policy-extract.txt'
    policy.write_text('Policy ID: payment-policy-63\n' + '\n'.join(
        str(i + 1) + '. ' + text for i, text in enumerate(RULES)) + '\n', encoding='utf-8')
    codes = directory / 'bank-code-list.txt'
    codes.write_text('Policy ID: outcome-codes-51\n1. Closing outcomes: ' + json.dumps(OUTCOMES, sort_keys=True) + '.\n', encoding='utf-8')
    mail = directory / 'bank-mail.eml'
    mail.write_text('From: source@example.invalid\nSubject: Wording for synthetic entry proof\n'
        'Content-Type: text/plain; charset=utf-8\n\n' + '\n'.join(
            'Wording ' + name + ': ' + template for name, template in WORDING.items()) + '\n', encoding='utf-8')
    memo = directory / 'changing-memo.eml'
    memo.write_text('From: source@example.invalid\nSubject: Explicit synthetic control changes\n'
        'Content-Type: text/plain; charset=utf-8\n\nPolicy ID: changed-controls-86\n'
        '1. This memo overrides rule 14 of policy payment-policy-63 with: Maximum payment amount: 287.50.\n'
        '2. This memo overrides rule 2 of policy payment-policy-63 with: Bank holidays are 2026-10-22.\n'
        '3. This memo overrides rule 3 of policy payment-policy-63 with: Identity questions, in order: date_of_birth, phone_last4.\n'
        '4. This memo overrides rule 15 of policy payment-policy-63 with: Latest payment date: 2026-10-21.\n', encoding='utf-8')
    return {'clean': [clean], 'raw': [mail, policy, memo, codes]}


def native_trace(bundle, amount):
    candidate = emit_native_requirements(bundle)
    variables = dict(customer_id='source-entry-local-synthetic', now='2026-10-14T11:00',
        bank_name='Synthetic Source Bank', **{'call.call_id': '00000000-0000-4000-8000-000000000047'})
    h = NativeHarness(candidate, variables)
    h.start()
    def response(body):
        return {'status': 'success', 'status_code': 200, 'data': body}
    for field in bundle['compiled'].policy.facts['identity_order'].value:
        h.receive(dict(kind='identity_answer', field=field,
                       answer='1989-07-24' if field == 'date_of_birth' else '3672'))
        h.dispatch('verify_identity'); h.resume('verify_identity', response(dict(ok=True, verified=True)))
    h.dispatch('get_account')
    h.resume('get_account', response(dict(ok=True, products=[dict(product_id='source-entry-product',
        type='credit_card', past_due=620, days_late=6, minimum_payment=83.5, card_last4='3672')])))
    h.receive(dict(kind='proposal', product_id='source-entry-product', amount=amount, date='2026-10-20'))
    return candidate, h


class BriefRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='brief-entry-new-forms-')
        self.addCleanup(self.temp.cleanup)
        self.forms = fixtures(self.temp.name)

    def compile(self, form='clean', paths=None):
        return compile_brief_requirements(paths or self.forms[form], API)

    def assert_expected(self, bundle, form):
        facts = bundle['compiled'].policy.facts
        expected = EXPECTED[form]
        self.assertEqual(facts['business_weekdays'].value, [0, 1, 2, 3])
        self.assertEqual(facts['holidays'].value, expected['holidays'])
        self.assertEqual(facts['identity_order'].value, expected['identity_order'])
        self.assertEqual(facts['identity_max_wrong'].value, 4)
        self.assertEqual(facts['bands'].value, BANDS)
        constraints = bundle['compiled'].constraints
        self.assertEqual([item['value'] for item in constraints if item['target'] == 'payment.amount'], [expected['ceiling']])
        self.assertEqual([item['value'] for item in constraints if item['target'] == 'payment.date_on_or_before'], [expected['latest']])

    def test_never_used_clean_spec_lowers_and_emits_actual_native_graph(self):
        bundle = self.compile(); self.assert_expected(bundle, 'clean')
        candidate, h = native_trace(bundle, EXPECTED['clean']['ceiling'])
        self.assertEqual(h.state['phase'], 'readback')
        self.assertEqual([request['body']['field'] for request in h.requests if request['operation'] == 'verify_identity'], EXPECTED['clean']['identity_order'])
        self.assertIn('315.75', h.state['events'][-1]['text'])
        self.assertFalse(candidate['readiness']['bank_writes_enabled'])
        self.assertFalse(candidate['readiness']['publish_allowed'])

    def test_never_used_raw_mail_policy_memo_code_list_changes_actual_native_behavior(self):
        bundle = self.compile('raw'); self.assert_expected(bundle, 'raw')
        self.assertEqual(bundle['derivation']['explicit_overrides'], 4)
        candidate, h = native_trace(bundle, EXPECTED['raw']['ceiling'])
        self.assertEqual(h.state['phase'], 'readback')
        self.assertIn('287.5', h.state['events'][-1]['text'])
        rejected, blocked = native_trace(bundle, EXPECTED['clean']['ceiling'])
        self.assertEqual(blocked.current_node, 'native_blocked')
        self.assertFalse(any(request['operation'] == 'create_promise' for request in blocked.requests))

    def test_reordered_inputs_do_not_change_override_authority_or_native_workflow(self):
        normal = self.compile('raw'); reversed_bundle = self.compile(paths=list(reversed(self.forms['raw'])))
        self.assertEqual(normal['compilation']['decisions'], reversed_bundle['compilation']['decisions'])
        self.assertEqual(emit_native_requirements(normal)['workflow'], emit_native_requirements(reversed_bundle)['workflow'])
        self.assertFalse(normal['derivation']['source_names_or_order_used_as_authority'])

    def test_every_assignment_and_override_keeps_original_source_quote_offsets_and_hash(self):
        bundle = self.compile('raw'); sources = {doc.source_id: doc for doc in bundle['documents']}
        for rule in bundle['compilation']['decisions']['rules']:
            for ref in rule['evidence']:
                self.assertEqual(sources[ref['source_id']].text[ref['start']:ref['end']], ref['quote'])
        self.assertTrue(all(len(doc.sha256) == 64 for doc in bundle['documents']))
        overrides = [rule for rule in bundle['compilation']['decisions']['rules'] if rule['overrides']]
        all_rules = {rule['id']: rule for rule in bundle['compilation']['decisions']['rules']}
        self.assertTrue(all(rule['priority'] > all_rules[rule['overrides'][0]]['priority'] for rule in overrides))
        self.assertFalse(bundle['derivation']['reviewed_hash_allowlist_used'])

    def test_unknown_required_numbered_prose_is_rejected_not_omitted(self):
        path = self.forms['clean'][0]
        path.write_text(path.read_text() + '\n19. Customers may skip security for small payments.\n')
        with self.assertRaisesRegex(FoundationError, 'UNKNOWN_REQUIRED_RULE'):
            self.compile()

    def test_policy_bearing_and_unknown_headings_are_rejected_not_omitted(self):
        path = self.forms['clean'][0]
        original = path.read_text()
        for heading in ('Skip identity checks', 'Permit payment without consent', 'Special controls'):
            with self.subTest(heading=heading):
                path.write_text(original + '\n# ' + heading + '\n')
                with self.assertRaisesRegex(FoundationError, 'UNKNOWN_OR_POLICY_HEADING'):
                    self.compile()

    def test_ambiguous_conflicting_base_rule_is_rejected(self):
        path = self.forms['clean'][0]
        path.write_text(path.read_text() + '\n19. Maximum payment amount: 399.25.\n')
        with self.assertRaisesRegex(FoundationError, 'CONFLICTING_RULES'):
            self.compile()

    def test_conflicting_memos_cannot_win_from_date_sender_or_file_order(self):
        sibling = Path(self.temp.name) / 'latest-message.txt'
        sibling.write_text('Policy ID: competing-change-89\n1. This memo overrides rule 14 of policy payment-policy-63 with: Maximum payment amount: 290.25.\n')
        for paths in (self.forms['raw'] + [sibling], [sibling] + self.forms['raw']):
            with self.subTest(order=paths[0].name), self.assertRaisesRegex(FoundationError, 'CONFLICTING_RULES'):
                self.compile(paths=paths)

    def test_memo_wrong_target_or_missing_named_rule_is_rejected(self):
        path = self.forms['raw'][2]; before = path.read_text()
        for replacement in ('rule 99 of policy payment-policy-63', 'rule 14 of policy absent-policy-92'):
            path.write_text(before.replace('rule 14 of policy payment-policy-63', replacement))
            with self.subTest(target=replacement), self.assertRaisesRegex(FoundationError, 'UNRESOLVED_OR_AMBIGUOUS_OVERRIDE'):
                self.compile('raw')
        path.write_text(before.replace('Maximum payment amount: 287.50.', 'Maximum products: 1.'))
        with self.assertRaisesRegex(FoundationError, 'OVERRIDE_CHANGES_RULE_KIND_OR_TARGET'):
            self.compile('raw')

    def test_missing_source_guard_cannot_be_filled_by_hidden_defaults(self):
        path = self.forms['clean'][0]
        path.write_text(path.read_text().replace('11. ' + RULES[10] + '\n', ''))
        with self.assertRaisesRegex(FoundationError, 'MISSING_SOURCE_SAFETY_GUARDS'):
            self.compile()

    def test_changed_identity_format_remains_explicitly_unsupported(self):
        path = self.forms['clean'][0]
        path.write_text(path.read_text().replace('phone_last4, date_of_birth', 'postcode, date_of_birth'))
        with self.assertRaises(FoundationError):
            self.compile()

    def test_explicit_memo_cycle_is_rejected_before_lowering(self):
        cycle = Path(self.temp.name) / 'cyclic-control.txt'
        cycle.write_text('Policy ID: loop-controls-93\n'
            '1. This memo overrides rule 2 of policy loop-controls-93 with: Maximum payment amount: 287.50.\n'
            '2. This memo overrides rule 1 of policy loop-controls-93 with: Maximum payment amount: 290.25.\n')
        with self.assertRaisesRegex(FoundationError, 'CYCLIC_OVERRIDE'):
            self.compile(paths=self.forms['raw'] + [cycle])

    def test_rule_count_limit_bounds_override_graph_work(self):
        path = self.forms['clean'][0]
        path.write_text(path.read_text() + '\n'.join(
            str(number) + '. Maximum payment amount: 315.75.' for number in range(19, 260)))
        with self.assertRaisesRegex(FoundationError, 'RULE_COUNT_BUDGET'):
            self.compile()

    def test_missing_literal_source_wording_does_not_invent_a_native_template(self):
        path = self.forms['clean'][0]
        path.write_text(path.read_text().replace('Wording goodbye: ' + WORDING['goodbye'] + '\n', ''))
        with self.assertRaisesRegex(FoundationError, 'Missing'):
            self.compile()

    def test_eval_input_exclusion_and_unsupported_prose_api_remain_separate(self):
        with self.assertRaisesRegex(FoundationError, 'EVALUATION_DATA_NOT_POLICY'):
            compile_brief_requirements(self.forms['clean'], API, evaluation_paths=self.forms['clean'])
        with self.assertRaisesRegex(FoundationError, 'UNSUPPORTED_SOURCE_MODALITY'):
            compile_brief_requirements([API], API)

    def test_source_archive_documents_need_no_original_paths_or_canned_interpreter(self):
        docs = collect_sources(self.forms['raw']); contract = BankContract.from_path(API)
        for doc in docs:
            doc.path = 'archived:' + doc.source_id + doc.kind
        with patch('socket.socket.connect', side_effect=AssertionError('No network')), \
             patch('os.environ.get', side_effect=AssertionError('No model key')), \
             patch('compiler.typed_source.TypedSourceProvider.generate', side_effect=AssertionError('No older grammar fallback')):
            result = derive_brief_requirements(docs, contract)
        self.assertEqual(result['derivation']['network_calls'], 0)
        self.assertFalse(result['publish_allowed'])

    def test_archive_safe_bundle_entry_preserves_lowering_and_evaluation_exclusion(self):
        expected = self.compile('raw')
        docs = collect_sources(self.forms['raw']); contract = BankContract.from_path(API)
        for doc in docs:
            doc.path = 'archived:' + doc.source_id + doc.kind
        with patch('builder.brief_requirements.read_source', side_effect=AssertionError('No original path')):
            bundle = compile_requirement_documents(docs, contract)
            self.assertEqual(bundle['compiled'].serializable(), expected['compiled'].serializable())
            self.assertEqual(bundle['compilation']['decisions'], expected['compilation']['decisions'])
            self.assertFalse(emit_native_requirements(bundle)['readiness']['bank_writes_enabled'])
            with self.assertRaisesRegex(FoundationError, 'EVALUATION_DATA_NOT_POLICY'):
                compile_requirement_documents(docs, contract, evaluation_source_hashes=[docs[0].sha256])
            with self.assertRaisesRegex(FoundationError, 'INVALID_EVALUATION_SOURCE_HASH'):
                compile_requirement_documents(docs, contract, evaluation_source_hashes=['not-a-digest'])

    def test_native_completion_stays_sealed_after_confirm_no_live_claim(self):
        bundle = self.compile(); candidate, h = native_trace(bundle, EXPECTED['clean']['ceiling'])
        h.receive(dict(kind='confirm', readback_token=h.state['readback_token'], explicit_yes=True))
        self.assertEqual(h.current_node, 'native_blocked')
        self.assertEqual(h.state['diagnostic'], 'Native write/completion admission unavailable')
        self.assertFalse(candidate['readiness']['bank_writes_enabled'])
        self.assertFalse(bundle['compiled'].coverage['full_source_semantic_coverage'])


if __name__ == '__main__':
    unittest.main()
