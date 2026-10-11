from pathlib import Path
from types import SimpleNamespace
import copy
import json
import tempfile
import unittest
import hashlib
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import patch

from builder.api_contract import BankContract
from builder.models import FoundationError
from builder.model_workflow import prompt_for, assemble_response, CodexWorkflowProvider, safe_process_status, checked_model_events
from orchestration.contest_build import Budget, publish, validate_workflow, verify_provider_graph, OUTPUT, KIT, run, FreyaClient
from orchestration.workspace_lock import official_workspace_mutex

API = Path(__file__).parent / 'inputs/openapi.json'


def workflow(contract):
    tools = list(contract.freya_templates()['tools'].values())
    return {'initial_node': 'collect', 'nodes': {'collect': {
        'name': 'collect', 'node_type': 'conversation', 'tools': tools,
        'transitions': [{'target_node': 'end', 'condition': {'operator': 'always'}}]},
        'end': {'name': 'end', 'node_type': 'end_call'}}}


class StaticContractTests(unittest.TestCase):
    def test_startup_notice_requires_exact_fingerprint_position_and_completed_turn(self):
        # Substitute a public test fingerprint; the real notice is bound by the
        # measured CLI diagnostic and full generation evidence, not copied here.
        message='public synthetic startup notice'.ljust(157, '.')
        notice={'type':'item.completed','item':{'type':'error','message':message}}
        thread={'type':'thread.started'}; start={'type':'turn.started'}; end={'type':'turn.completed'}
        fingerprint=(len(message.encode()),hashlib.sha256(message.encode()).hexdigest())
        encode=lambda rows:'\n'.join(json.dumps(row) for row in rows)
        with patch('builder.model_workflow._DISABLED_STARTUP_NOTICE',fingerprint):
            events=checked_model_events(encode([thread,notice,start,end]))
            self.assertEqual(events[1]['diagnostic_class'],'CODE_MODE_HOST_DISABLED_STARTUP_NOTICE')
            self.assertNotIn(message,json.dumps(events))
            altered=copy.deepcopy(notice); altered['item']['message']=message[:-1]+'!'
            nonstring=copy.deepcopy(notice); nonstring['item']['message']=None
            for rows in ([notice,thread,start,end],[thread,start,notice,end],
                    [thread,notice,notice,start,end],[thread,altered,start,end],
                    [thread,nonstring,start,end],[thread,notice,start],
                    [thread,{'type':[]},start,end],
                    [thread,{'type':'item.completed','item':{'type':'error','message':'\ud800'}},start,end],
                    [thread,notice,start,{'type':'turn.failed'}],
                    [thread,notice,start,{'type':'error','message':'private-unit-marker'}],
                    [thread,notice,end,start]):
                with self.subTest(rows=rows), self.assertRaises(FoundationError):
                    checked_model_events(encode(rows))
            for kind in ('command_execution','file_change','mcp_tool_call','web_search',
                    'collab_agent_tool_call','todo_list','UNKNOWN_TOOL'):
                tool={'type':'item.completed','item':{'type':kind,'private':'private-unit-marker'}}
                with self.subTest(kind=kind), self.assertRaisesRegex(FoundationError,'UNEXPECTED_TOOL'):
                    checked_model_events(encode([thread,notice,start,tool,end]))
            status=safe_process_status(encode([thread,notice,start,end]),'',model='unit',return_code=0)
            self.assertEqual(status['diagnostic_classes'],['CODE_MODE_HOST_DISABLED_STARTUP_NOTICE'])
            self.assertNotIn(message,json.dumps(status))

    def test_provider_success_still_requires_output_policy_coverage_and_readonly_flags(self):
        from tests_orchestration.test_model_wire import empty
        from builder.model_wire import EXTRACT, NODE
        node=empty(NODE);node.update(name='end',node_type='end_call')
        listen=empty(NODE);listen.update(name='listen',node_type='conversation')
        extraction=empty(EXTRACT);extraction.update(name='speech_event',type='string')
        listen['extraction_schema']=[extraction]
        response={'workflow_plan':{'initial_node':'listen','system_prompt':'','nodes':[listen,node]},
            'code_blocks':[],'unresolved':[], 'coverage':[{'quote':'unit source','nodes':['end'],'implementation':'unit only'}], 'limitations':[]}
        stdout='\n'.join(json.dumps({'type':kind}) for kind in ('thread.started','turn.started','turn.completed'))
        for change, expected in ((None,None),('unresolved','UNRESOLVED_MODEL_POLICY'),
                ('coverage','COVERAGE_SOURCE_MISMATCH'),('malformed','Invalid JSON'),
                ('missing','RESPONSE_UNAVAILABLE'),('nonzero','RESPONSE_UNAVAILABLE')):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                def fake_process(command,**kwargs):
                    self.assertIn('model_reasoning_effort="low"',command)
                    self.assertIn('read-only',command)
                    self.assertIn('model_provider="builder_https"', command)
                    self.assertIn('model_providers.builder_https.supports_websockets=false', command)
                    self.assertIn('model_providers.builder_https.requires_openai_auth=true', command)
                    self.assertIn('model_providers.builder_https.base_url="https://chatgpt.com/backend-api/codex"', command)
                    self.assertEqual(command[command.index('code_mode_host')-1],'--disable')
                    self.assertNotIn('FREYA_API_KEY',kwargs['env'])
                    value=copy.deepcopy(response)
                    if change=='unresolved':value['unresolved']=['missing policy']
                    if change=='coverage':value['coverage'][0]['quote']='not in source'
                    if change!='missing':
                        (Path(temporary)/'model-response.json').write_text('bad-json' if change=='malformed' else json.dumps(value))
                    return SimpleNamespace(returncode=1 if change=='nonzero' else 0,stdout=stdout,
                        stderr='model: private/unit/profile-and-key')
                with patch('builder.model_workflow.subprocess.run',side_effect=fake_process), patch.dict('os.environ',{'FREYA_API_KEY':'private-unit-marker'}):
                    provider=CodexWorkflowProvider(executable='unit-only',reasoning_effort='low')
                    if expected:
                        with self.assertRaisesRegex(FoundationError,expected):
                            provider.generate(brief='unit source',api={},references={},directory=temporary,
                                remaining_seconds=10,tool_templates={'tools':{}})
                    else:
                        _,metadata=provider.generate(brief='unit source',api={},references={},directory=temporary,
                            remaining_seconds=10,tool_templates={'tools':{}})
                        self.assertEqual(metadata['reasoning_effort_requested'],'low')
                        self.assertFalse(metadata['model_independently_observed'])
                        self.assertNotIn('private/unit/profile-and-key',json.dumps(metadata))
                self.assertNotIn('private-unit-marker',(Path(temporary)/'model-process-status.json').read_text())
        with self.assertRaisesRegex(FoundationError,'REASONING_EFFORT_INVALID'):
            CodexWorkflowProvider(executable='unit-only',reasoning_effort='invalid')
        with self.assertRaisesRegex(FoundationError,'TRANSPORT_INVALID'):
            CodexWorkflowProvider(executable='unit-only',transport='untrusted')

    def test_default_transport_is_explicit_and_errors_remain_fatal_and_visible(self):
        provider=CodexWorkflowProvider(executable='unit-only',transport='default')
        self.assertEqual(provider.transport,'default')
        events='\n'.join(json.dumps({'type':kind}) for kind in ('thread.started','turn.started','error','turn.completed'))
        with self.assertRaisesRegex(FoundationError,'RESPONSE_UNAVAILABLE'):
            checked_model_events(events)
        status=safe_process_status(events,'',model='unit',return_code=0)
        self.assertEqual(status['event_counts']['error'],1)

    def test_practice_clarification_uses_only_exact_raw_api_source_hash(self):
        fingerprint='9a8410b12e5c82b6c6a30e9ed7030574d6f919cafdb1e035cef7bd876b0badb8'
        original=copy.deepcopy(self.contract.freya_templates())
        for value in (fingerprint,None,'0'*64):
            templates=copy.deepcopy(original)
            if value is None:templates.pop('source_sha256')
            else:templates['source_sha256']=value
            data=json.loads(prompt_for('unit source',{'servers':[{'url':'https://practice.freyavoice.ai'}]}, {},templates).split('INPUT_DATA\n',1)[1])
            self.assertEqual(bool(data['bank_contract_clarifications']),value==fingerprint)
            self.assertEqual(data['bank_tool_templates']['tools'],original['tools'])
        self.assertEqual(original['source_sha256'],self.contract.source_hash)

    def test_model_process_failure_keeps_only_bounded_diagnostics(self):
        marker='private-host-profile-and-credential-marker'
        failed=SimpleNamespace(returncode=2,stdout=json.dumps({'type':'turn.failed','error':marker}),
            stderr='Error: failed to resolve CODEX_HOME. Could not find home directory '+marker)
        with tempfile.TemporaryDirectory() as temporary, patch('builder.model_workflow.subprocess.run',return_value=failed):
            with self.assertRaisesRegex(FoundationError,'RESPONSE_UNAVAILABLE'):
                CodexWorkflowProvider(executable='unit-only').generate(brief='unit source',api={},references={},
                    directory=temporary,remaining_seconds=10)
            status=json.loads((Path(temporary)/'model-process-status.json').read_text())
            self.assertEqual(status['return_code'],2)
            self.assertEqual(status['event_counts'],{'turn.failed':1})
            self.assertEqual(status['diagnostic_classes'],['HOST_HOME_DIRECTORY_UNAVAILABLE'])
            self.assertNotIn(marker,json.dumps(status))
            self.assertFalse(status['raw_logs_retained'])

    def test_reasoning_warning_is_not_model_unavailability_and_timeout_logs_stay_private(self):
        status=safe_process_status(b'{"type":"turn.started","private":"unit-only-marker"}\n[]',
            b'Model unit: reasoning summaries are not supported',model='unit',timed_out=True)
        self.assertEqual(status['event_counts'],{'turn.started':1})
        self.assertEqual(status['diagnostic_classes'],['REASONING_SUMMARY_UNSUPPORTED'])
        self.assertTrue(status['timed_out'])
        self.assertNotIn('unit-only-marker',json.dumps(status))

    def test_code_blocks_preserve_quotes_and_reject_missing_or_unused_code(self):
        response = {'workflow_json': json.dumps({'nodes': {'step': {'pre_actions': [{'function': {'parameters': {'properties': {'code': {'default': '@CODE:one'}}}}}]}}}),
            'code_blocks': [{'id':'one', 'code': 'result = {"text": "Bank\'s"}\n'}],
            'unresolved':[], 'coverage':[], 'limitations':[]}
        built = assemble_response(response)
        self.assertEqual(built['nodes']['step']['pre_actions'][0]['function']['parameters']['properties']['code']['default'], response['code_blocks'][0]['code'])
        response['code_blocks'].append({'id':'unused','code':'result = {}'})
        with self.assertRaisesRegex(FoundationError, 'UNUSED_CODE'):
            assemble_response(response)
        response['code_blocks'] = []
        with self.assertRaisesRegex(FoundationError, 'CODE_REFERENCE'):
            assemble_response(response)

    def test_provider_defaults_allowed_but_new_actions_and_changed_values_refused(self):
        expected = {'initial_node': 'start', 'nodes': {'start': {'node_type': 'conversation', 'tools': []}}}
        actual = copy.deepcopy(expected)
        actual['nodes']['start'].update({'interruptible': True, 'max_node_turns': None})
        self.assertTrue(verify_provider_graph(expected, actual))
        for changed in ({'tools': [{'function': {'handler': '__function__:api_call'}}]}, {'node_type': 'code'}, {'unexpected': ''}):
            bad = copy.deepcopy(actual)
            bad['nodes']['start'].update(changed)
            with self.assertRaisesRegex(FoundationError, 'GRAPH_CHANGED'):
                verify_provider_graph(expected, bad)

    def setUp(self):
        self.contract = BankContract.from_path(API)
        self.workflow = workflow(self.contract)

    def test_new_contract_fields_and_enums_must_reach_export(self):
        self.assertTrue(validate_workflow(self.workflow, self.contract)['static_valid'])
        changed = copy.deepcopy(self.contract)
        changed.operations['create_promise'].request['properties']['new_note_code'] = {
            'type': 'string', 'enum': ['NEW_CODE']}
        with self.assertRaisesRegex(FoundationError, 'BODY_FIELDS'):
            validate_workflow(self.workflow, changed)
        updated = workflow(changed)
        self.assertTrue(validate_workflow(updated, changed)['static_valid'])
        promise = next(tool for tool in updated['nodes']['collect']['tools'] if tool['function']['name'] == 'create_promise')
        promise['function']['parameters']['properties']['body']['properties']['new_note_code']['enum'] = ['OLD_CODE']
        with self.assertRaisesRegex(FoundationError, 'SCHEMA_MISMATCH'):
            validate_workflow(updated, changed)

    def test_old_bank_url_and_blind_retries_refused(self):
        tool = self.workflow['nodes']['collect']['tools'][0]
        tool['function']['parameters']['properties']['url']['default'] = 'https://other.invalid/bank'
        with self.assertRaisesRegex(FoundationError, 'ENDPOINT_MISMATCH'):
            validate_workflow(self.workflow, self.contract)
        self.workflow = workflow(self.contract)
        self.workflow['nodes']['collect']['tools'][0]['config']['maxRetries'] = 1
        with self.assertRaisesRegex(FoundationError, 'BLIND_RETRY'):
            validate_workflow(self.workflow, self.contract)

    def test_node_targets_and_stripped_tools_refused(self):
        self.workflow['nodes']['collect']['transitions'][0]['target_node'] = 'missing'
        with self.assertRaisesRegex(FoundationError, 'TRANSITION_TARGET'):
            validate_workflow(self.workflow, self.contract)
        self.workflow['nodes']['collect']['transitions'][0]['target_node'] = 'end'
        self.workflow['nodes']['collect']['node_type'] = 'code'
        with self.assertRaisesRegex(FoundationError, 'STRIPPED_TOOLS'):
            validate_workflow(self.workflow, self.contract)

    def test_code_is_checked_without_execution(self):
        self.workflow['nodes']['compute'] = {'node_type': 'code', 'pre_actions': [{
            'function': {'handler': '__function__:code_exec', 'parameters': {'properties': {
                'code': {'default': 'import pathlib\nopen("somewhere", "w")'}}}}}]}
        with self.assertRaisesRegex(FoundationError, 'CODE_IMPORT_REFUSED'):
            validate_workflow(self.workflow, self.contract)

    def test_code_wire_limit_checked_before_publication(self):
        self.workflow['nodes']['compute']={'node_type':'code','pre_actions':[{
            'function':{'name':'code_exec','handler':'__function__:code_exec','parameters':{'properties':{
            'code':{'default':'result = '+repr('x'*100000)}}}}}]}
        with self.assertRaisesRegex(FoundationError,'CODE_REQUEST_TOO_LARGE'):
            validate_workflow(self.workflow,self.contract)

    def test_prompt_uses_current_inputs_no_practice_documents(self):
        prompt = prompt_for('New policy clause with 17 products.', {'server': 'new'}, {'workflow': 'wire'})
        self.assertIn('17 products', prompt)
        self.assertNotIn('brief-a.md', prompt)
        self.assertIn('untrusted data', prompt)


class CaptureSpy:
    """TEST ONLY protocol spy; does not claim encryption."""
    def __init__(self):
        self.pending = None
        self.failed = False
        self.cache = []
    def begin_exchange(self, **kwargs):
        if self.failed or self.pending:
            raise ValueError('latched')
        self.pending = 'ticket'
        return self.pending
    def complete_exchange(self, ticket, **kwargs):
        if not kwargs.get('complete', True):
            self.failed = True
            raise ValueError('incomplete')
        self.cache.append(kwargs)
        self.pending = None
    def mark_unknown(self, ticket):
        self.failed = True


class ErrorCaptureTests(unittest.TestCase):
    def client(self, sink, opener):
        with patch.dict('os.environ', {'FREYA_API_KEY':'synthetic-unit-only'}):
            client = FreyaClient(Budget(60), [])
        client.evidence, client.opener = sink, SimpleNamespace(open=opener)
        return client

    def test_provider_error_full_body_headers_captured_before_failure(self):
        payload = json.dumps({'success':False, 'data':{'message':'temporary refusal'}}).encode()
        sink = CaptureSpy()
        def open_error(*args, **kwargs):
            raise HTTPError('https://synthetic.invalid',503,'temporary',{'x-test':'retained'},BytesIO(payload))
        client = self.client(sink, open_error)
        with self.assertRaisesRegex(FoundationError,'NO_RETRY'):
            client.api('POST','/api/v2/simulations',{})
        self.assertEqual(sink.cache[0]['body'], payload)
        self.assertEqual(sink.cache[0]['headers'], {'x-test':'retained'})
        self.assertEqual(sink.cache[0]['http_status'],503)
        self.assertIsNone(sink.pending)

    def test_timeout_latches_capture_and_prevents_second_dispatch(self):
        requests=[]
        def timeout(*args,**kwargs):
            requests.append(1)
            raise TimeoutError()
        sink=CaptureSpy()
        client=self.client(sink,timeout)
        with self.assertRaises(FoundationError): client.api('POST','/api/v2/simulations',{})
        with self.assertRaises(ValueError): client.api('POST','/api/v2/simulations',{})
        self.assertEqual(len(requests),1)
        self.assertTrue(sink.failed)
        self.assertIsNotNone(sink.pending)

    def test_bank_error_captures_complete_response_once(self):
        from orchestration.contest_practice import run_practice
        sink=CaptureSpy()
        payload=b'{"ok":false,"error":"synthetic refusal"}'
        requests=[]
        def bank_error(*args,**kwargs):
            requests.append(1)
            raise HTTPError('https://synthetic.invalid',400,'refused',{'x-test':'bank'},BytesIO(payload))
        client=SimpleNamespace(evidence=sink, kit=SimpleNamespace(workspace_id=lambda:'capture-local'),
            api=lambda method,path,body=None,**kw: {'simulations':[],'total':0} if path=='/api/v2/simulations' else {'personas':[{'id':'persona-local'}]},
            opener=SimpleNamespace(open=bank_error), last_exchange=None)
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{'FREYA_BANK_TEAM_KEY':'synthetic-unit-team'}):
            with self.assertRaisesRegex(FoundationError,'NO_RETRY'):
                run_practice(client=client, publication={}, case_id='PUB-01',repetitions=1,
                    contract=BankContract.from_path(KIT/'tools/openapi.json'),directory=directory,budget=Budget(60))
        self.assertEqual(requests,[1])
        self.assertEqual(sink.cache[0]['body'],payload)
        self.assertEqual(sink.cache[0]['http_status'],400)
        self.assertIsNone(sink.pending)

    def test_single_bundle_three_repetitions_rejected_before_any_dispatch(self):
        from orchestration.contest_practice import run_practice
        with self.assertRaisesRegex(FoundationError,'ONE_RUN_PER_BUNDLE'):
            run_practice(client=SimpleNamespace(evidence=CaptureSpy()),publication={},case_id='PUB-01',
                repetitions=3,contract=None,directory=None,budget=Budget(60))


class BudgetAndMutationTests(unittest.TestCase):
    def test_main_attaches_and_pins_recorder_before_publication(self):
        client=SimpleNamespace(key='synthetic-unit-only')
        sinks=[]
        class Sink:
            def __init__(self,*args,**kwargs): self.artifacts={};sinks.append(self)
            def pin_artifact(self,role,body): self.artifacts[role]=body
        def publication(wf,**kwargs):
            self.assertIs(client.evidence,sinks[0])
            self.assertEqual(set(sinks[0].artifacts),{'brief','tool_api','source_manifest'})
            Path(kwargs['directory'],'provider-export.json').write_text(json.dumps({'workflow':wf}))
            return {'workflow_id':'unit-workflow','agent_id':'unit-agent'}
        def practice(**kwargs):
            self.assertEqual(set(sinks[0].artifacts),{'brief','tool_api','source_manifest','provider_export'})
            return {'passed':True,'kind':'synthetic-wiring-test-only'}
        OUTPUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=OUTPUT) as directory, patch('orchestration.contest_build.publish',side_effect=publication), patch('orchestration.contest_practice.run_practice',side_effect=practice):
            result=run(brief_path=Path(__file__).parent/'inputs/single-brief.md',tool_api_path=API,
                directory=Path(directory)/'run',provider='bounded',publish_live=True,practice_case='PUB-01',
                client_factory=lambda *args:client,evidence_factory=Sink,deadline_seconds=60)
        self.assertEqual(result['status'],'PRACTICE_CASE_PASSED')
        self.assertEqual(result['protected_evidence']['status'],'SEALED')
        self.assertFalse(result['final_submission_ready'])

    def test_failed_recorder_prevents_publication_dispatch(self):
        def unavailable(*args,**kwargs): raise OSError('synthetic-local-storage-refusal')
        OUTPUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=OUTPUT) as directory, patch('orchestration.contest_build.publish') as publication:
            result=run(brief_path=Path(__file__).parent/'inputs/single-brief.md',tool_api_path=API,
                directory=Path(directory)/'run',provider='bounded',publish_live=True,
                client_factory=lambda *args:SimpleNamespace(key='synthetic-unit-only'),evidence_factory=unavailable,deadline_seconds=60)
        self.assertEqual(result['diagnostic'],'CONTEST_PROTECTED_EVIDENCE_HOST_UNAVAILABLE')
        publication.assert_not_called()

    def test_automatic_repair_receives_both_syntax_and_unresolved_diagnostics(self):
        calls = []
        contract = BankContract.from_path(API)
        class Model:
            def generate(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    response = {'workflow_json':'{broken', 'code_blocks':[], 'unresolved':['missing branch'], 'coverage':[], 'limitations':[]}
                    (kwargs['directory']/'model-response.json').write_text(json.dumps(response))
                    raise FoundationError('BUILDER_UNRESOLVED_MODEL_POLICY')
                return workflow(contract), {'model':'synthetic-unit-only', 'semantic_correctness_verified':False}
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=OUTPUT) as directory:
            result = run(brief_path=Path(__file__).parent/'inputs/single-brief.md', tool_api_path=API,
                directory=Path(directory)/'run', provider='codex', model_provider=Model(), deadline_seconds=60)
        self.assertEqual(result['status'], 'LOCAL_WORKFLOW_GENERATED')
        self.assertEqual(result['compilation']['calls'],2)
        codes = {issue['code'] for issue in calls[1]['feedback']['diagnostics']['issues']}
        self.assertTrue({'JSON_SYNTAX_INVALID','UNRESOLVED_POLICY'} <= codes)
        self.assertEqual(calls[0]['tool_templates'], contract.freya_templates())
        self.assertFalse(result['live_success_verified'])
        self.assertFalse(result['final_submission_ready'])

    def test_workspace_lock_excludes_second_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            with official_workspace_mutex(temporary, 'test-workspace'):
                with self.assertRaisesRegex(FoundationError, 'WORKSPACE_BUSY'):
                    with official_workspace_mutex(temporary, 'test-workspace'):
                        self.fail('second owner acquired active workspace')
    def test_total_budget_deadline_counts_all_work(self):
        times = iter([10.0, 12.0, 1809.5])
        budget = Budget(1800, clock=lambda: next(times))
        self.assertEqual(budget.remaining(), 1798)
        with self.assertRaisesRegex(FoundationError, 'DEADLINE'):
            budget.remaining()

    def test_uncertain_create_keeps_slot_blocked_and_is_not_retried(self):
        calls = []
        def create(*args):
            calls.append('create')
            raise FoundationError('CONTEST_PROVIDER_REQUEST_FAILED_NO_RETRY')
        client = SimpleNamespace(kit=SimpleNamespace(workspace_id=lambda: 'local-test-contest',
            AGENT_LLM={'provider': 'experimental-5', 'model': 'experimental-5'},
            validate_workflow=lambda *args: None, create_workflow=create))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'slots.json'
            for unused in range(2):
                with self.assertRaises(FoundationError):
                    publish({}, client=client, state_path=state, slot='A',
                            directory=temporary, budget=Budget(60))
            self.assertEqual(calls, ['create'])
            self.assertEqual(json.loads(state.read_text())['A']['status'], 'MUTATION_PENDING')

    def test_slot_update_preserves_workflow_identity(self):
        source = workflow(BankContract.from_path(API))
        stored = copy.deepcopy(source)
        stored['agent_config'] = {'llmConfig': {'provider': 'experimental-5', 'model': 'experimental-5'}}
        def export(identifier, path):
            Path(path).write_text(json.dumps({'workflow_id': identifier, 'agent_id': 'agent-local', 'workflow': stored}))
        kit = SimpleNamespace(workspace_id=lambda: 'local-test-contest',
            AGENT_LLM=stored['agent_config']['llmConfig'], validate_workflow=lambda *args: None,
            create_workflow=lambda *args: self.fail('a saved slot must not create another workflow'),
            update_workflow=lambda *args: 3, export_workflow=export)
        client = SimpleNamespace(kit=kit, api=lambda *args, **kw: {'versions': [
            {'versionNumber': 3, 'id': 'version-local', 'isPublished': True}]})
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'slots.json'
            state.write_text(json.dumps({'A': {'status': 'EXPORTED', 'workflow_id': 'wf-local', 'agent_id': 'agent-local'}}))
            receipt = publish(source, client=client, state_path=state, slot='A', directory=temporary, budget=Budget(60))
            self.assertEqual(receipt['workflow_id'], 'wf-local')
            self.assertEqual(receipt['version_number'], 3)

    def test_export_and_published_version_are_both_required(self):
        original = workflow(BankContract.from_path(API))
        stored = copy.deepcopy(original)
        stored['agent_config'] = {'llmConfig': {'provider': 'experimental-5', 'model': 'experimental-5'}}
        calls = []
        def export(identifier, path):
            calls.append('export')
            Path(path).write_text(json.dumps({'workflow_id': identifier, 'agent_id': 'agent-local', 'workflow': stored}))
        kit = SimpleNamespace(workspace_id=lambda: 'local-test-contest',
            AGENT_LLM=stored['agent_config']['llmConfig'], validate_workflow=lambda *args: None,
            create_workflow=lambda *args: {'workflow_id': 'wf-local', 'agent_id': 'agent-local'},
            update_workflow=lambda *args: 2, export_workflow=export)
        client = SimpleNamespace(kit=kit, api=lambda *args, **kw: {'versions': [
            {'versionNumber': 2, 'id': 'version-local', 'isPublished': False}]})
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'slots.json'
            with self.assertRaisesRegex(FoundationError, 'PUBLISHED_VERSION'):
                publish(original, client=client, state_path=state, slot='A', directory=temporary, budget=Budget(60))
            self.assertEqual(calls, ['export'])
            self.assertEqual(json.loads(state.read_text())['A']['status'], 'MUTATION_PENDING')


if __name__ == '__main__':
    unittest.main()
