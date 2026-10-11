"""Synthetic original contest traces, including negative resealed mutations."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from builder.models import FoundationError
from orchestration.final_run_pairing import verify_contest_pairing


def raw(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode() + b'\n'


def digest(value):
    return hashlib.sha256(value).hexdigest()


class ContestRunPairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'package'
        self.package.mkdir()
        self.selection_path = self.root / 'independent-selection.json'
        self.commit = 'c' * 40
        self.contents, self.pairs = {}, []
        self.api = {'servers': [{'url': 'https://synthetic-bank.example.invalid'}],
                    'paths': {f'/{op}': {'post': {}} for op in
                              ('verify_identity', 'get_account', 'create_promise', 'close_call')}}
        self.source_pins = {'builder/build.py': 'a' * 64}
        for index in range(2):
            label = 'synthetic-final-' + str(index)
            identifiers = {key: str(uuid.UUID(int=100 + index * 10 + position)) for position, key in
                           enumerate(('run_id', 'workflow_id', 'agent_id', 'version_id'))}
            binding = {key: identifiers[key] for key in ('workflow_id', 'agent_id', 'version_id')}
            workflow = {'initial_node': 'fixture', 'nodes': {'fixture': {'pre_actions': [
                {'function': {'handler': '__function__:api_call', 'parameters': {'properties': {
                    'url': {'default': self.api['servers'][0]['url'] + op},
                    'method': {'default': 'POST'}}}}} for op in self.api['paths'].keys()]}}}
            exported_graph = copy.deepcopy(workflow)
            exported_graph['agent_config'] = {'llmConfig': {'provider': 'experimental-5', 'model': 'experimental-5'}}
            exported = raw({'workflow_id': binding['workflow_id'], 'agent_id': binding['agent_id'],
                            'updated_at': '2026-10-11T17:00:09Z', 'workflow': exported_graph})
            workflow_path = '/api/v2/workflow/' + binding['workflow_id']
            actions = [{'method': method, 'path': path, 'status': 'RESPONSE_RECEIVED'} for method, path in (
                ('PUT', workflow_path), ('POST', '/api/v2/versions/workflow/' + binding['workflow_id'] + '/publish'),
                ('GET', workflow_path), ('GET', '/api/v2/versions/workflow/' + binding['workflow_id']))]
            files = {'input-brief.md': b'Synthetic selection fixture; no real final input.\n',
                     'input-api.json': raw(self.api), 'agent.workflow.json': raw(workflow),
                     'provider-export.json': exported, 'runtime-source-manifest.json': raw(self.source_pins)}
            trace = {'kind': 'contest-builder-run-1', 'run_id': identifiers['run_id'],
                'status': 'PROVIDER_EXPORTED_TESTS_REQUIRED', 'started_at': '2026-10-11T17:00:00Z',
                'ended_at': '2026-10-11T17:00:10Z', 'elapsed_seconds': 10, 'deadline_seconds': 1800,
                'inputs': {'brief_sha256': digest(files['input-brief.md']), 'tool_api_sha256': digest(files['input-api.json'])},
                'runtime_sha256': self.source_pins, 'provider_actions': actions, 'tests': [],
                'compilation': {'provider': 'codex-cli', 'calls': 1,
                    'attempts': [{'model_requested': 'synthetic-builder-model', 'purpose': 'Generate from supplied sources'}],
                    'model_independently_observed': False},
                'publication': {**binding, 'version_number': 2, 'export_sha256': digest(exported), 'status': 'EXPORTED'}}
            files['trace.json'] = raw(trace)
            self.contents[label] = files
            self.pairs.append({'brief_id': label, 'run_directory': label, 'input_origin': 'official_final',
                'manual_agent_edits': False, **identifiers, 'version_number': 2,
                'committed_runtime_sha256': self.source_pins})

    def mutate(self, name, edit, index=0):
        label = self.pairs[index]['run_directory']
        value = json.loads(self.contents[label][name])
        edit(value)
        self.contents[label][name] = raw(value)

    def seal(self):
        required = ('trace.json', 'FINAL.json', 'manifest.json', 'input-brief.md', 'input-api.json',
                    'agent.workflow.json', 'provider-export.json', 'runtime-source-manifest.json')
        for pair in self.pairs:
            directory = self.package / pair['run_directory']
            directory.mkdir(exist_ok=True)
            files = self.contents[pair['run_directory']]
            manifest = raw({name: digest(content) for name, content in files.items() if name not in ('manifest.json', 'FINAL.json')})
            files['manifest.json'] = manifest
            files['FINAL.json'] = raw({'status': json.loads(files['trace.json'])['status'],
                                      'manifest_sha256': digest(manifest), 'final_submission_ready': False})
            pair['files_sha256'] = {name: digest(files[name]) for name in required}
            for name, content in files.items():
                (directory / name).write_bytes(content)
        selection = raw({'version': 'contest-final-selection-1', 'commit_sha': self.commit,
                         'input_origin': 'official_final', 'pairs': self.pairs})
        self.selection_path.write_bytes(selection)
        return digest(selection)

    def verify(self):
        anchor = self.seal()
        return verify_contest_pairing(self.package, self.selection_path,
            trusted_selection_sha256=anchor, expected_commit=self.commit)

    def test_original_contest_traces_work_without_fabricated_legacy_events(self):
        originals = [self.contents[p['run_directory']]['trace.json'] for p in self.pairs]
        with patch('socket.create_connection', side_effect=AssertionError('No network')):
            result = self.verify()
        self.assertTrue(result['valid'])
        self.assertEqual(result['events_synthesized'], 0)
        self.assertFalse(result['final_submission_ready'])
        self.assertFalse(result['published_version_readback_independently_verified'])
        self.assertFalse(result['pairs'][0]['model_independently_observed_by_this_module'])
        self.assertEqual(originals, [(self.package / p['run_directory'] / 'trace.json').read_bytes() for p in self.pairs])

    def test_practice_origin_cannot_be_promoted_to_final(self):
        self.pairs[0]['input_origin'] = 'practice'
        with self.assertRaisesRegex(FoundationError, 'OFFICIAL_SELECTION_REQUIRED'):
            self.verify()

    def test_manual_agent_edit_rejected(self):
        self.pairs[0]['manual_agent_edits'] = True
        with self.assertRaisesRegex(FoundationError, 'OFFICIAL_SELECTION_REQUIRED'):
            self.verify()

    def test_blocked_trace_cannot_be_relabelled_complete(self):
        self.mutate('trace.json', lambda value: value.update(status='BLOCKED'))
        with self.assertRaisesRegex(FoundationError, 'CONTEST_TERMINAL_EXPORT_REQUIRED'):
            self.verify()

    def test_reported_commit_does_not_override_different_source_pins(self):
        self.mutate('trace.json', lambda value: value.update(runtime_sha256={'builder/build.py': 'b' * 64}))
        with self.assertRaisesRegex(FoundationError, 'CONTEST_SOURCE_COMMIT_BINDING_MISMATCH'):
            self.verify()

    def test_two_runs_cannot_claim_one_commit_with_different_runtime_sources(self):
        changed = {'builder/build.py': 'b' * 64}
        self.pairs[1]['committed_runtime_sha256'] = changed
        self.mutate('trace.json', lambda value: value.update(runtime_sha256=changed), 1)
        self.mutate('runtime-source-manifest.json', lambda value: value.update(changed), 1)
        with self.assertRaisesRegex(FoundationError, 'CONTEST_SOURCE_COMMIT_BINDING_MISMATCH'):
            self.verify()

    def test_elapsed_cannot_hide_excess_wall_time(self):
        self.mutate('trace.json', lambda value: value.update(ended_at='2026-10-11T17:31:00Z'))
        with self.assertRaisesRegex(FoundationError, 'THIRTY_MINUTE_LIMIT'):
            self.verify()

    def test_missing_model_attempt_is_not_an_empty_valid_list(self):
        self.mutate('trace.json', lambda value: value['compilation'].update(attempts=[]))
        with self.assertRaisesRegex(FoundationError, 'MODEL_TRACE_INCOMPLETE'):
            self.verify()

    def test_export_hash_must_be_recorded_by_original_publication(self):
        self.mutate('trace.json', lambda value: value['publication'].update(export_sha256='0' * 64))
        with self.assertRaisesRegex(FoundationError, 'CONTEST_PUBLICATION_MISMATCH'):
            self.verify()

    def test_wrong_agent_model_rejected_even_with_resealed_inputs(self):
        self.mutate('provider-export.json', lambda value: value['workflow']['agent_config']['llmConfig'].update(model='different'))
        self.mutate('trace.json', lambda value: value['publication'].update(export_sha256=digest(self.contents[self.pairs[0]['run_directory']]['provider-export.json'])))
        with self.assertRaisesRegex(FoundationError, 'AGENT_MODEL_MISMATCH'):
            self.verify()

    def test_manual_graph_change_rejected_even_when_export_hash_is_resealed(self):
        self.mutate('provider-export.json', lambda value: value['workflow']['nodes']['fixture'].update(task_message='Changed payment policy'))
        self.mutate('trace.json', lambda value: value['publication'].update(export_sha256=digest(self.contents[self.pairs[0]['run_directory']]['provider-export.json'])))
        with self.assertRaisesRegex(FoundationError, 'PROVIDER_GRAPH_CHANGED'):
            self.verify()

    def test_publish_action_missing_is_not_invented(self):
        self.mutate('trace.json', lambda value: value.update(provider_actions=[item for item in value['provider_actions'] if item['method'] != 'POST']))
        with self.assertRaisesRegex(FoundationError, 'CONTEST_PUBLICATION_ACTIONS_INCOMPLETE'):
            self.verify()

    def test_prepublication_read_does_not_substitute_for_selected_export(self):
        self.mutate('trace.json', lambda value: value['provider_actions'].reverse())
        with self.assertRaisesRegex(FoundationError, 'CONTEST_PUBLICATION_ACTIONS_INCOMPLETE'):
            self.verify()

    def test_local_generation_without_export_is_not_final_evidence(self):
        self.mutate('trace.json', lambda value: value.update(status='LOCAL_WORKFLOW_GENERATED'))
        with self.assertRaisesRegex(FoundationError, 'CONTEST_TERMINAL_EXPORT_REQUIRED'):
            self.verify()

    def test_private_extra_manifest_path_refused(self):
        label = self.pairs[0]['run_directory']
        self.contents[label]['.env'] = b'Synthetic forbidden private file'
        with self.assertRaisesRegex(FoundationError, 'PATH_INVALID'):
            self.verify()

    def test_changed_trace_bytes_are_rejected_by_original_selection(self):
        anchor = self.seal()
        (self.package / self.pairs[0]['run_directory'] / 'trace.json').write_bytes(b'{}')
        with self.assertRaisesRegex(FoundationError, 'SELECTED_CONTEST_FILE_MISMATCH'):
            verify_contest_pairing(self.package, self.selection_path,
                trusted_selection_sha256=anchor, expected_commit=self.commit)


if __name__ == '__main__':
    unittest.main()
