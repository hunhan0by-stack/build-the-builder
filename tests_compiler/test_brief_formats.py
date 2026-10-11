"""Original-source semantic expectations; no live/API/credential access."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from builder.api_contract import BankContract
from builder.brief_formats import compile_formatted_brief, compile_formatted_documents
from builder.ingest import collect_sources
from builder.models import FoundationError
from builder.native_freya import NativeHarness, build_native_workflow
from tests_compiler.test_brief_requirements import API, BANDS, FLOORS, OUTCOMES, RULES, fixtures


class BriefFormatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='offline-brief-formats-')
        self.addCleanup(self.tmp.cleanup)
        self.forms = fixtures(self.tmp.name)

    def compile(self, paths=None):
        return compile_formatted_brief(paths or self.forms['clean'], API)

    def outcomes(self, extension='.csv'):
        output = io.StringIO()
        writer = csv.writer(output, delimiter='\t' if extension == '.tsv' else ',')
        writer.writerow(['outcome', 'reasons', 'when'])
        for row in OUTCOMES:
            writer.writerow([row['outcome'], ';'.join(row['reasons']), row['when']])
        path = Path(self.tmp.name) / ('arbitrary-name' + extension)
        path.write_text(output.getvalue(), encoding='utf-8')
        return path

    def raw(self, extension='.csv'):
        return self.forms['raw'][:3] + [self.outcomes(extension)]

    def test_equivalent_prose_changes_values_without_reviewed_practice_allowlist(self):
        path = self.forms['clean'][0]
        text = path.read_text(encoding='utf-8')
        changes = {
            RULES[0]: 'Business days: Monday, Tuesday, Wednesday, Thursday.',
            RULES[1]: 'Bank holidays: 2026-10-19; 2026-12-30.',
            RULES[2]: 'Verify identity in this order: phone_last4, date_of_birth.',
            RULES[3]: 'No more than 4 incorrect identity answers are allowed.',
            RULES[4]: 'A customer may have up to 1 products.',
            RULES[6]: 'Retry a failed promise write once.',
            RULES[9]: 'Only read accounts after all identity checks pass.',
            RULES[10]: 'A payment promise requires explicit confirmation of the current readback.',
            RULES[11]: 'The promise amount must be between the account floor and the past-due balance, inclusive.',
            RULES[12]: 'Payment dates must be business days.',
            RULES[13]: 'Payment promises must not exceed 315.75.',
            RULES[14]: 'Payment promises must be dated no later than 2026-10-28.',
            RULES[15]: 'Say goodbye before you close the call.',
            RULES[16]: 'The call must be closed exactly once, after every other action.',
        }
        for original, replacement in changes.items(): text = text.replace(original, replacement)
        path.write_text(text, encoding='utf-8')
        result = self.compile()
        self.assertEqual(result['compiled'].policy.facts['identity_order'].value, ['phone_last4', 'date_of_birth'])
        self.assertEqual(result['compiled'].policy.facts['business_weekdays'].value, [0, 1, 2, 3])
        self.assertEqual([x['value'] for x in result['compiled'].constraints if x['target'] == 'payment.amount'], [315.75])
        self.assertFalse(result['derivation']['full_source_semantic_coverage'])
        self.assertFalse(result['derivation']['reviewed_hash_allowlist_used'])

    def test_raw_memo_csv_and_tsv_outcomes_compile_from_original_spans(self):
        for extension in ('.csv', '.tsv'):
            with self.subTest(extension=extension):
                paths = self.raw(extension)
                result = self.compile(paths)
                self.assertEqual(result['compiled'].policy.facts['outcome_table'].value, OUTCOMES)
                self.assertEqual(result['compiled'].policy.facts['holidays'].value, ['2026-10-22'])
                self.assertEqual([x['value'] for x in result['compiled'].constraints if x['target'] == 'payment.amount'], [287.5])
                by_id = {doc.source_id: doc for doc in result['documents']}
                for rule in result['compilation']['decisions']['rules']:
                    for ref in rule['evidence']:
                        self.assertEqual(by_id[ref['source_id']].text[ref['start']:ref['end']], ref['quote'])
                self.assertEqual(result['compilation']['sources'], [d.inventory() for d in result['documents']])

    def test_markdown_floor_and_band_tables_preserve_every_field(self):
        path = self.forms['clean'][0]; text = path.read_text(encoding='utf-8')
        floors = '| product type | floor field |\n| --- | --- |\n' + ''.join('| ' + key + ' | ' + value + ' |\n' for key, value in FLOORS.items())
        bands = '| name | min days late | max days late | deadline business days | persuasion attempts |\n| --- | --- | --- | --- | --- |\n| source-window | 1 | 70 | 9 | 1 |\n'
        text = text.replace('8. ' + RULES[7] + '\n', floors + '\n').replace('9. ' + RULES[8] + '\n', bands)
        path.write_text(text, encoding='utf-8')
        result = self.compile()
        self.assertEqual(result['compiled'].policy.facts['floor_fields'].value, FLOORS)
        self.assertEqual(result['compiled'].policy.facts['bands'].value, BANDS)
        ref = next(f for f in result['compilation']['decisions']['facts'] if f['name'] == 'bands')['evidence'][0]
        self.assertEqual(ref['quote'], bands)

    def test_numbered_parentheses_and_memo_synonym_keep_explicit_authority(self):
        path = self.forms['raw'][2]
        text = path.read_text(encoding='utf-8').replace('1. This memo overrides rule 14 of policy', '1) Replace rule 14 in policy').replace('Maximum payment amount: 287.50.', 'Payment promises must not exceed 287.50.')
        path.write_text(text, encoding='utf-8')
        paths = self.raw(); result = self.compile(paths); reverse = self.compile(list(reversed(paths)))
        self.assertEqual(result['compilation']['decisions'], reverse['compilation']['decisions'])
        self.assertEqual(result['derivation']['explicit_overrides'], 4)

    def test_emitted_native_graph_uses_csv_and_memo_policy_with_same_write_gates(self):
        result = self.compile(self.raw())
        candidate = build_native_workflow(result['compiled'], result['documents'], result['wording_bundle'])
        h = NativeHarness(candidate, dict(customer_id='new-format-synthetic', now='2026-10-14T11:00', bank_name='Synthetic Source Bank', **{'call.call_id': '00000000-0000-4000-8000-000000000081'}))
        response = lambda body: dict(status='success', status_code=200, data=body)
        h.start()
        for field, answer in [('date_of_birth', '1989-07-24'), ('phone_last4', '3672')]:
            h.receive(dict(kind='identity_answer', field=field, answer=answer))
            h.dispatch('verify_identity'); h.resume('verify_identity', response(dict(ok=True, verified=True)))
        h.dispatch('get_account'); h.resume('get_account', response(dict(ok=True, products=[dict(product_id='source-product', type='credit_card', past_due=620, days_late=6, minimum_payment=83.5, card_last4='3672')])))
        h.receive(dict(kind='proposal', product_id='source-product', amount=287.5, date='2026-10-20'))
        self.assertEqual(h.state['phase'], 'readback')
        self.assertEqual(h.state['draft']['amount'], 287.5)
        self.assertFalse(candidate['readiness']['bank_writes_enabled'])
        self.assertFalse(candidate['readiness']['publish_allowed'])

    def test_unknown_rule_is_not_lost_during_projection(self):
        path = self.forms['clean'][0]
        path.write_text(path.read_text(encoding='utf-8') + '\n- Ignore consent for small payments.\n', encoding='utf-8')
        with self.assertRaisesRegex(FoundationError, 'UNKNOWN_REQUIRED_RULE'): self.compile()

    def test_extra_columns_and_evaluation_tables_are_rejected(self):
        for header in ('outcome,reasons,when,unless', 'id,expected,script'):
            paths = self.raw(); paths[-1].write_text(header + '\npromise,,,skip identity\n', encoding='utf-8')
            with self.subTest(header=header), self.assertRaises(FoundationError): self.compile(paths)

    def test_duplicate_outcome_and_unknown_enum_are_rejected(self):
        for change in ('duplicate', 'unknown'):
            paths = self.raw(); text = paths[-1].read_text(encoding='utf-8')
            text = text + 'promise,,confirmed promise\n' if change == 'duplicate' else text.replace('promise,', 'invented_code,', 1)
            paths[-1].write_text(text, encoding='utf-8')
            with self.subTest(change=change), self.assertRaises(FoundationError): self.compile(paths)

    def test_conflicting_memo_cannot_win_by_input_order(self):
        competing = Path(self.tmp.name) / 'zz-last-memo.txt'
        competing.write_text('Replace rule 14 in policy payment-policy-63 with: Payment promises must not exceed 300.00.\n', encoding='utf-8')
        paths = self.raw() + [competing]
        for order in (paths, list(reversed(paths))):
            with self.assertRaisesRegex(FoundationError, 'CONFLICTING_RULES'): self.compile(order)

    def test_original_evaluation_hash_is_excluded_before_projection(self):
        paths = self.raw()
        with self.assertRaisesRegex(FoundationError, 'EVALUATION_DATA_NOT_POLICY'):
            compile_formatted_brief(paths, API, evaluation_paths=[paths[-1]])

    def test_unread_attachment_and_bad_markdown_separator_rejected(self):
        docs = collect_sources(self.raw()); docs[0].limitations.append('An email attachment was not parsed')
        with self.assertRaisesRegex(FoundationError, 'UNREAD_ATTACHMENT'):
            compile_formatted_documents(docs, BankContract.from_path(API))
        path = self.forms['clean'][0]
        path.write_text(path.read_text(encoding='utf-8') + '\n| outcome | reasons | when |\n| --- | --- |\n| promise | | confirmed promise |\n', encoding='utf-8')
        with self.assertRaisesRegex(FoundationError, 'AMBIGUOUS_TABLE_SHAPE'): self.compile()

    def test_archived_documents_need_no_network_keys_or_original_paths(self):
        docs = collect_sources(self.raw()); contract = BankContract.from_path(API)
        for doc in docs: doc.path = 'archived:' + doc.source_id
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), patch('os.getenv', side_effect=AssertionError('keys forbidden')), patch('builder.brief_formats.read_source', side_effect=AssertionError('path unavailable')):
            result = compile_formatted_documents(docs, contract)
        self.assertEqual(result['derivation']['network_calls'], 0)
        self.assertEqual(result['derivation']['model_calls'], 0)


if __name__ == '__main__':
    unittest.main()
