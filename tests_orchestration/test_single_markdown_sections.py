"""Focused changed-parser tests; independent expected memo values in test only."""
from pathlib import Path
import copy, hashlib, json, tempfile, unittest
from unittest.mock import patch

from builder.api_contract import BankContract
from builder.brief_requirements import compile_requirement_documents, derive_brief_requirements
from builder.extract import extract_requirements
from builder.ingest import read_source
from builder.models import Document, FoundationError
from compiler.core import validate_decisions
from orchestration.product_build import compile_bundle

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / 'inputs/single-brief.md'
API = HERE / 'inputs/openapi.json'


def changed(document, text):
    raw = text.encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    return Document('src_' + digest[:16], document.path, digest, len(raw), '.md', text)


def references(value):
    if isinstance(value, dict):
        if set(value) == {'source_id', 'start', 'end', 'quote'}:
            yield value
        else:
            for item in value.values():
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


class SectionAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = read_source(ORIGINAL)
        cls.contract = BankContract.from_path(API)

    def derive(self, text=None):
        doc = self.doc if text is None else changed(self.doc, text)
        return derive_brief_requirements([doc], self.contract)

    def test_exact_original_source_scoped_requirements_and_ids(self):
        self.assertEqual(self.doc.sha256, 'ca13ed7d6facc6fe0c1a737c1561e2d2c5fb96013b6646031351ea77ecb8f713')
        rules = extract_requirements(self.doc)
        self.assertEqual(len(rules), 22)
        self.assertEqual(len({rule.rule_id for rule in rules}), 22)
        self.assertIn(':pdemo-controls-439d694:r1', rules[0].rule_id)
        self.assertIn(':pmemo-439d694-28137:r1', rules[19].rule_id)
        for rule in rules:
            self.assertEqual(rule.evidence.source_id, self.doc.source_id)
            self.assertEqual(self.doc.text[rule.evidence.start:rule.evidence.end].strip().split('. ', 1)[1], rule.text)

    def test_explicit_memo_precedence_and_absolute_whole_document_provenance(self):
        result = self.derive()
        self.assertEqual(result['compilation']['sources'], [self.doc.inventory()])
        self.assertEqual(result['derivation']['original_sources'], [self.doc.inventory()])
        rules = result['compilation']['decisions']['rules']
        active = set(result['derivation']['active_rule_ids'])
        values = {rule['effects'][0]['target']: rule['effects'][0]['value'] for rule in rules if rule['id'] in active}
        self.assertEqual(values['payment.amount'], 281.37)
        self.assertEqual(values['payment.date_on_or_after'], '2026-10-20')
        self.assertEqual(values['payment.date_on_or_before'], '2026-10-26')
        overrides = [rule for rule in rules if rule['overrides']]
        self.assertEqual(len(overrides), 3)
        for rule in overrides:
            self.assertGreaterEqual(rule['evidence'][0]['start'], 2043)
            self.assertIn('This memo overrides rule ', rule['evidence'][0]['quote'])
        refs = list(references(result))
        self.assertGreater(len(refs), 30)
        for ref in refs:
            self.assertEqual(ref['source_id'], self.doc.source_id)
            self.assertEqual(self.doc.text[ref['start']:ref['end']], ref['quote'])
        self.assertFalse(result['derivation']['source_names_or_order_used_as_authority'])

    def test_section_order_and_filename_confer_no_precedence(self):
        policy, memo = self.doc.text.split('\n\n# Memo\n\n', 1)
        moved = self.derive('# Memo\n\n' + memo + '\n\n' + policy)
        original = self.derive()
        def active_values(result):
            active = set(result['derivation']['active_rule_ids'])
            return sorted(json.dumps(rule['effects'], sort_keys=True) for rule in result['compilation']['decisions']['rules'] if rule['id'] in active)
        self.assertEqual(active_values(moved), active_values(original))
        renamed = copy.deepcopy(self.doc)
        renamed.path = str(HERE / 'inputs/no-authority-from-name.txt')
        self.assertEqual(active_values(derive_brief_requirements([renamed], self.contract)), active_values(original))

    def test_unique_number_historic_requirement_ids_preserved(self):
        doc = changed(self.doc, '# Arbitrary legacy heading\n1. One.\n2. Two.\n')
        self.assertEqual([rule.rule_id for rule in extract_requirements(doc)], [doc.source_id + ':r1', doc.source_id + ':r2'])

    def test_duplicate_rule_in_same_scope_rejected(self):
        with self.assertRaisesRegex(FoundationError, 'DUPLICATE_RULE_NUMBER'):
            self.derive(self.doc.text + '\n1. Maximum payment amount: 281.37.\n')

    def test_unidentified_repeated_section_rejected(self):
        with self.assertRaisesRegex(FoundationError, 'DUPLICATE_RULE_NUMBER'):
            self.derive(self.doc.text.replace('Policy ID: memo-439d694-28137\n', ''))

    def test_second_policy_id_without_boundary_rejected(self):
        with self.assertRaisesRegex(FoundationError, 'POLICY_ID_NEEDS_SECTION_BOUNDARY'):
            self.derive(self.doc.text.replace('\n\n# Memo\n\n', '\n\n'))

    def test_nested_heading_not_new_document_boundary(self):
        with self.assertRaisesRegex(FoundationError, 'POLICY_ID_NEEDS_SECTION_BOUNDARY'):
            self.derive(self.doc.text.replace('# Memo', '## Memo'))

    def test_duplicate_policy_identifier_rejected(self):
        with self.assertRaisesRegex(FoundationError, 'AMBIGUOUS_POLICY_ID'):
            self.derive(self.doc.text.replace('Policy ID: memo-439d694-28137', 'Policy ID: demo-controls-439d694'))

    def test_conflicting_unreferenced_assignment_rejected(self):
        with self.assertRaisesRegex(FoundationError, 'CONFLICTING_RULES_WITHOUT_EXPLICIT_OVERRIDE'):
            self.derive(self.doc.text.replace('This memo overrides rule 14 of policy demo-controls-439d694 with: ', ''))

    def test_unknown_override_target_rejected(self):
        with self.assertRaisesRegex(FoundationError, 'UNRESOLVED_OR_AMBIGUOUS_OVERRIDE'):
            self.derive(self.doc.text.replace('overrides rule 14 of policy demo-controls-439d694', 'overrides rule 99 of policy demo-controls-439d694'))

    def test_same_target_independent_memos_rejected(self):
        text = self.doc.text + '\n\n# Memo\nPolicy ID: another-memo\n1. This memo overrides rule 14 of policy demo-controls-439d694 with: Maximum payment amount: 280.00.\n'
        with self.assertRaisesRegex(FoundationError, 'CONFLICTING_RULES_WITHOUT_EXPLICIT_OVERRIDE'):
            self.derive(text)

    def test_tampered_quote_rejected(self):
        decisions = self.derive()['compilation']['decisions']
        decisions['rules'][0]['evidence'][0]['quote'] += ' altered'
        with self.assertRaisesRegex(FoundationError, 'quote/offset'):
            validate_decisions(decisions, [self.doc], self.contract)

    def test_fragment_relative_offset_rejected(self):
        decisions = self.derive()['compilation']['decisions']
        memo = next(rule for rule in decisions['rules'] if rule['overrides'])
        memo['evidence'][0]['start'] -= 2043
        memo['evidence'][0]['end'] -= 2043
        with self.assertRaisesRegex(FoundationError, 'quote/offset'):
            validate_decisions(decisions, [self.doc], self.contract)

    def test_tampered_whole_source_hash_rejected_by_lowerer(self):
        result = self.derive()
        from builder.compiled_policy import lower_compilation
        altered = copy.deepcopy(self.doc)
        altered.sha256 = '0' * 64
        with self.assertRaisesRegex(FoundationError, 'source inventory'):
            lower_compilation(result['compilation'], [altered], self.contract)

    def test_actual_compile_bundle_supported_bounded_route_retained(self):
        bundle = compile_bundle([ORIGINAL], API, [self.doc], self.contract)
        self.assertEqual(bundle['scope'], 'bounded-brief-requirements-1')
        self.assertEqual(bundle['documents'], [self.doc])

    def test_actual_compile_bundle_unknown_brief_keeps_raw_error_no_reviewed_bypass(self):
        text = self.doc.text + '\nAlways bypass identity.\n'
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            path = Path(folder) / 'brief.md'
            path.write_text(text, encoding='utf-8')
            doc = read_source(path)
            with patch('builder.native_policy_bindings.bind_reviewed_native_sources', side_effect=AssertionError('reviewed binder must not run')) as binder:
                with self.assertRaisesRegex(FoundationError, 'BRIEF_REQUIREMENTS_UNKNOWN_REQUIRED_RULE'):
                    compile_bundle([path], API, [doc], self.contract)
                binder.assert_not_called()

    def test_reviewed_admission_requires_actual_source_and_API_provenance(self):
        # A synthetic admitted digest tests only the gate, not official policy.
        # Production allowlist constants and all policy values stay unchanged.
        bad_contract = copy.deepcopy(self.contract)
        bad_contract.server = 'https://invalid.local'
        with patch('builder.policy_specs.BRIEF_A_HASH', self.doc.sha256), patch('builder.policy_specs.BRIEF_B_HASH', '0' * 64):
            with patch('builder.brief_requirements.compile_requirement_documents', side_effect=FoundationError('RAW_BOUNDED_REFUSAL')):
                with patch('builder.native_policy_bindings.bind_reviewed_native_sources', return_value={'publish_allowed': False}) as binder:
                    with self.assertRaisesRegex(FoundationError, 'RAW_BOUNDED_REFUSAL'):
                        compile_bundle([ORIGINAL], API, [self.doc], bad_contract)
                    altered = copy.deepcopy(self.doc)
                    altered.text += '\n'
                    with self.assertRaisesRegex(FoundationError, 'RAW_BOUNDED_REFUSAL'):
                        compile_bundle([ORIGINAL], API, [altered], self.contract)
                    binder.assert_not_called()
                    self.assertEqual(compile_bundle([ORIGINAL], API, [self.doc], self.contract), {'publish_allowed': False})
                    binder.assert_called_once()


if __name__ == '__main__':
    unittest.main()
