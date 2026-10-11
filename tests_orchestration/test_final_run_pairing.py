"""Synthetic protocol fixtures; none is an actual hidden brief or Freya run."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from builder.models import FoundationError
from orchestration import final_run_pairing as pairing


def raw(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode() + b'\n'


def digest(value):
    return hashlib.sha256(value).hexdigest()


class FinalRunPairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'package'
        self.package.mkdir()
        self.selection_file = self.root / 'reviewer-selection.json'
        self.commit = 'c' * 40
        self.contents = {}
        self.pairs = []
        self.selected = []
        self.model = {'provider': 'experimental-5', 'model': 'experimental-5'}
        api = {'servers': [{'url': 'https://synthetic-bank.example.invalid'}],
               'paths': {f'/{op}': {'post': {}} for op in
                         ('verify_identity', 'get_account', 'create_promise', 'close_call')}}
        for index, label in enumerate(('fixture-final-one', 'fixture-final-two')):
            prefix = label + '/'
            ids = {key: str(uuid.UUID(int=100 + index * 10 + position)) for position, key in
                   enumerate(('run_id', 'workflow_id', 'agent_id', 'version_id'))}
            binding = {key: ids[key] for key in ('workflow_id', 'agent_id', 'version_id')}
            binding['version_number'] = 2
            workflow = {'initial_node': 'fixture', 'nodes': {'fixture': {'pre_actions': [
                {'function': {'handler': '__function__:api_call', 'parameters': {'properties': {
                    'url': {'default': api['servers'][0]['url'] + '/' + op},
                    'method': {'default': 'POST'}}}}} for op in
                ('verify_identity', 'get_account', 'create_promise', 'close_call')]}},
                'agent_config': {'llmConfig': self.model}}
            self.contents[prefix + 'brief.txt'] = b'Synthetic protocol fixture, not an official final input.\n'
            self.contents[prefix + 'tool-api.json'] = raw(api)
            briefs = {prefix + 'brief.txt': digest(self.contents[prefix + 'brief.txt'])}
            api_hash = digest(self.contents[prefix + 'tool-api.json'])
            trace = {'kind': 'official_final_builder_trace', 'input_origin': 'official_final',
                'scope': 'full_final_phone_agent', 'run_id': ids['run_id'], 'commit_sha': self.commit,
                'brief_id': label, 'manual_agent_edits': False, 'brief_sha256': briefs,
                'tool_api_sha256': api_hash, 'started_at_utc': '2026-10-11T10:00:00Z',
                'ended_at_utc': '2026-10-11T10:00:10Z', 'elapsed_seconds': 10,
                'includes_agent_tests_in_elapsed': True, 'model_calls': [], 'tests': [],
                'freya_actions': [{'kind': 'export', 'publication': binding}]}
            self.contents[prefix + 'trace.json'] = raw(trace)
            self.contents[prefix + 'build-receipt.json'] = raw({
                'kind': 'official_final_builder_run', 'input_origin': 'official_final',
                'scope': 'full_final_phone_agent', 'run_id': ids['run_id'], 'commit_sha': self.commit,
                'brief_id': label, 'manual_agent_edits': False, 'trace_sha256':
                digest(self.contents[prefix + 'trace.json']), 'selected_publication': binding})
            updated = '2026-10-11T10:00:09Z'
            self.contents[prefix + 'export.workflow.json'] = raw({'workflow_id': ids['workflow_id'],
                'agent_id': ids['agent_id'], 'updated_at': updated, 'workflow': workflow})
            self.contents[prefix + 'workflow-readback.json'] = raw({'workflow': {
                'id': ids['workflow_id'], 'agentId': ids['agent_id'], 'updatedAt': updated, 'workflow': workflow}})
            self.contents[prefix + 'versions-readback.json'] = raw({'versions': [
                {'id': ids['version_id'], 'versionNumber': 2, 'isPublished': True}]})
            self.contents[prefix + 'generated-workflow.json'] = raw(workflow)
            paths = {'export': prefix + 'export.workflow.json', 'trace': prefix + 'trace.json',
                'build_receipt': prefix + 'build-receipt.json', 'workflow_readback': prefix + 'workflow-readback.json',
                'versions_readback': prefix + 'versions-readback.json',
                'generated_workflow': prefix + 'generated-workflow.json'}
            self.pairs.append({'brief_id': label, 'briefs': list(briefs),
                               'tool_api': prefix + 'tool-api.json', **paths})
            self.selected.append({'brief_id': label, 'input_origin': 'official_final', **ids,
                'version_number': 2, 'brief_sha256': briefs, 'tool_api_sha256': api_hash,
                'agent_model': self.model, **{key + '_sha256': digest(self.contents[path])
                                            for key, path in paths.items()}})

    def update(self, field, edit):
        name = self.pairs[0][field]
        value = json.loads(self.contents[name])
        edit(value)
        self.contents[name] = raw(value)
        self.selected[0][field + '_sha256'] = digest(self.contents[name])
        if field == 'trace':
            self.update('build_receipt', lambda receipt: receipt.update(trace_sha256=digest(self.contents[name])))

    def seal(self):
        self.contents['pairing.json'] = raw({'version': 'final-run-package-1',
            'commit_sha': self.commit, 'pairs': self.pairs})
        for name, value in self.contents.items():
            path = self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        manifest = raw({'version': 'final-run-package-1', 'commit_sha': self.commit,
            'files': [{'path': name, 'bytes': len(value), 'sha256': digest(value)}
                      for name, value in sorted(self.contents.items())]})
        (self.package / 'manifest.json').write_bytes(manifest)
        selection = raw({'version': 'final-run-selection-1', 'commit_sha': self.commit,
            'input_origin': 'official_final', 'manifest_sha256': digest(manifest), 'pairs': self.selected})
        self.selection_file.write_bytes(selection)
        return digest(selection)

    def verify(self):
        anchor = self.seal()
        return pairing.verify_pairing(self.package, self.selection_file,
            expected_commit=self.commit, trusted_selection_sha256=anchor)

    def test_two_selected_protocol_pairs_do_not_claim_official_success(self):
        with patch('socket.create_connection', side_effect=AssertionError('No network')):
            result = self.verify()
        self.assertTrue(result['valid'])
        self.assertEqual(len(result['pairs']), 2)
        self.assertFalse(result['official_call_evaluation_verified'])
        self.assertFalse(result['final_submission_ready'])
        self.assertFalse(result['provider_transport_authenticated_by_this_module'])

    def test_diagnostic_pass_cannot_substitute_for_final_builder_run(self):
        self.update('build_receipt', lambda value: value.update(kind='runtime_diagnostic', judge='PASS'))
        with self.assertRaisesRegex(FoundationError, 'SELECTED_BUILD_RUN_MISMATCH'):
            self.verify()

    def test_practice_selection_not_official_final_input(self):
        self.selected[0]['input_origin'] = 'practice'
        with self.assertRaisesRegex(FoundationError, 'OFFICIAL_SELECTION_REQUIRED'):
            self.verify()

    def test_wrong_brief_hash_rejected_even_when_package_resealed(self):
        self.selected[0]['brief_sha256'][self.pairs[0]['briefs'][0]] = '0' * 64
        with self.assertRaisesRegex(FoundationError, 'BRIEF_HASH_MISMATCH'):
            self.verify()

    def test_wrong_selected_commit_rejected(self):
        self.update('trace', lambda value: value.update(commit_sha='d' * 40))
        with self.assertRaisesRegex(FoundationError, 'SELECTED_BUILD_RUN_MISMATCH'):
            self.verify()

    def test_wrong_selected_run_trace_rejected(self):
        self.update('trace', lambda value: value.update(run_id=str(uuid.UUID(int=555))))
        with self.assertRaisesRegex(FoundationError, 'SELECTED_BUILD_RUN_MISMATCH'):
            self.verify()

    def test_wrong_published_version_rejected(self):
        self.update('versions_readback', lambda value: value['versions'][0].update(versionNumber=3))
        with self.assertRaisesRegex(FoundationError, 'PUBLISHED_VERSION_MISMATCH'):
            self.verify()

    def test_newer_unpublished_version_makes_current_export_ambiguous(self):
        self.update('versions_readback', lambda value: value['versions'].append(
            {'id': str(uuid.UUID(int=999)), 'versionNumber': 3, 'isPublished': False}))
        with self.assertRaisesRegex(FoundationError, 'PUBLISHED_VERSION_MISMATCH'):
            self.verify()

    def test_platform_export_wrapper_required(self):
        name = self.pairs[0]['export']
        self.contents[name] = raw(json.loads(self.contents[name])['workflow'])
        self.selected[0]['export_sha256'] = digest(self.contents[name])
        with self.assertRaisesRegex(FoundationError, 'FREYA_EXPORT_READBACK_MISMATCH'):
            self.verify()

    def test_read_only_prefix_rejected_even_with_final_labels(self):
        for field in ('export', 'workflow_readback', 'generated_workflow'):
            def remove(value, selected_field=field):
                workflow = value['workflow'] if selected_field == 'export' else (
                    value['workflow']['workflow'] if selected_field == 'workflow_readback' else value)
                workflow['nodes']['fixture']['pre_actions'] = workflow['nodes']['fixture']['pre_actions'][:2]
            self.update(field, remove)
        with self.assertRaisesRegex(FoundationError, 'DIAGNOSTIC_OR_READ_ONLY_PREFIX'):
            self.verify()

    def test_30_minute_wall_time_cannot_be_hidden_by_small_elapsed(self):
        self.update('trace', lambda value: value.update(ended_at_utc='2026-10-11T10:31:00Z'))
        with self.assertRaisesRegex(FoundationError, 'THIRTY_MINUTE_LIMIT'):
            self.verify()

    def test_wrong_trace_bytes_rejected_by_selection(self):
        anchor = self.seal()
        (self.package / self.pairs[0]['trace']).write_bytes(b'{}')
        with self.assertRaisesRegex(FoundationError, 'FILE_HASH_MISMATCH'):
            pairing.verify_pairing(self.package, self.selection_file,
                expected_commit=self.commit, trusted_selection_sha256=anchor)

    def test_missing_model_call_list_is_not_a_complete_trace(self):
        self.update('trace', lambda value: value.update(model_calls={'model': 'unstructured'}))
        with self.assertRaisesRegex(FoundationError, 'BUILDER_TRACE_INCOMPLETE'):
            self.verify()

    def test_independent_selection_must_not_be_stored_inside_package(self):
        anchor = self.seal()
        local = self.package / 'selection.json'
        local.write_bytes(self.selection_file.read_bytes())
        with self.assertRaisesRegex(FoundationError, 'INDEPENDENT_SELECTION_REQUIRED'):
            pairing.verify_pairing(self.package, local,
                expected_commit=self.commit, trusted_selection_sha256=anchor)

    def test_private_selection_path_rejected_before_read(self):
        private = self.root / '.freya-local' / 'selection.json'
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('Private read forbidden')):
            with self.assertRaisesRegex(FoundationError, 'PRIVATE_FILE_FORBIDDEN'):
                pairing.verify_pairing(self.package, private,
                    expected_commit=self.commit, trusted_selection_sha256='0' * 64)


if __name__ == '__main__':
    unittest.main()
