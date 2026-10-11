"""Source-to-platform contest runner with a per-brief deadline and honest trace.

The optional model path is experimental until its generated workflow passes live
cases. No simulation is implicitly started; local fixture checks confer no live PASS.
"""
from __future__ import annotations

import argparse
import ast
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
import re
from pathlib import Path
import time
import uuid
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from builder.api_contract import BankContract
from builder.ingest import read_source
from builder.models import FoundationError, strict_json
from orchestration.prepare_delivery import scan_public
from orchestration.product_build import ROOT, KIT, OUTPUT as PRODUCT_OUTPUT, build_product, verify_product_build

OUTPUT = ROOT / 'artifacts/final-builder'


def raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def verify_source_snapshot(trace):
    """Refuse mixed-source generation, worker execution and publication."""
    for paths in ('runtime_sha256', 'platform_reference_sha256'):
        for name, expected in trace.get(paths, {}).items():
            path = (ROOT if paths == 'runtime_sha256' else KIT) / name
            if not path.is_file() or digest(path.read_bytes()) != expected:
                raise FoundationError('CONTEST_RUNTIME_SOURCE_CHANGED')


def write(path, value):
    encoded = raw(value)
    public_scan(encoded)
    Path(path).write_bytes(encoded + b'\n')


def public_scan(encoded):
    # Final delivery explicitly requires provider IDs. The older offline bundle
    # scanner treats these as private pilot metadata; keep credential checks
    # while permitting just those non-secret final identifiers.
    value = strict_json(encoded.decode('utf-8'))
    def identifiers(row):
        if isinstance(row, dict):
            return {key: ('unavailable' if key.replace('_', '').lower() in
                ('agentid', 'publishedagentid', 'simulationid') and isinstance(child, str)
                else identifiers(child)) for key, child in row.items()}
        if isinstance(row, list):
            return [identifiers(child) for child in row]
        return row
    scan_public(raw(identifiers(value)), '.json')


class Budget:
    def __init__(self, seconds, clock=time.monotonic):
        if type(seconds) is not int or not 1 <= seconds <= 1800:
            raise FoundationError('CONTEST_BUDGET_MUST_BE_1_TO_1800')
        self.clock = clock
        self.started = clock()
        self.end = self.started + seconds

    def remaining(self):
        value = self.end - self.clock()
        if value < 1:
            raise FoundationError('CONTEST_DEADLINE_EXCEEDED')
        return value


def verify_provider_graph(expected, actual):
    """Retain every source value; permit only observed inert server defaults."""
    defaults = {'assertions': [], 'disable_inactivity_monitor': False,
        'extraction_schema': [], 'interruptible': True, 'max_node_turns': None,
        'max_turn_transition': None, 'on_enter_first_entry_only': False,
        'passthrough': False, 'passthrough_default_transition': None,
        'pre_actions': [], 'post_actions': [], 'response_mapping': [],
        'return_to_previous_on_interrupt': False, 'role_message': '',
        'suppress_tool_results': False, 'task_message': '', 'tools': [],
        'truncation_strategy': 'keep_all', 'truncation_window_size': 10}
    def inert_function_description(left, right, key, path):
        if key != 'description' or type(right[key]) is not str or right[key] != '':
            return False
        # Only newly added metadata on these exact observed paths is inert.
        # Existing values, handlers and parameters are still compared below.
        if (len(path) == 5 and path[0] in ('nodes', 'global_nodes')
                and path[2] == 'pre_actions' and path[3].isdigit()
                and path[4] == 'function'):
            return True
        if (len(path) == 4 and path[0] in ('nodes', 'global_nodes')
                and path[2:] == ('terminal', 'function')):
            node = expected.get(path[0], {}).get(path[1], {})
            return (node.get('node_type') == 'end_call'
                and left.get('name') == 'end_call'
                and left.get('handler') == '__function__:end_call'
                and left.get('parameters') == {'type': 'object', 'properties': {
                    'message_mode': {'type': 'string', 'default': 'none'}}})
        return False
    def compare(left, right, path):
        if type(left) is not type(right):
            raise FoundationError('CONTEST_PROVIDER_GRAPH_CHANGED_REVIEW_REQUIRED')
        if isinstance(left, dict):
            for key, value in left.items():
                if key not in right:
                    raise FoundationError('CONTEST_PROVIDER_GRAPH_CHANGED_REVIEW_REQUIRED')
                compare(value, right[key], path + (key,))
            for key in right.keys() - left.keys():
                node = len(path) == 2 and path[0] in ('nodes', 'global_nodes')
                metadata = key == 'description' and right[key] == '' and 'response_mapping' in path
                condition = key == 'mode' and right[key] == 'exact' and 'condition' in path and 'transitions' in path
                if not (node and key in defaults and type(right[key]) is type(defaults[key])
                        and right[key] == defaults[key] or metadata or condition
                        or inert_function_description(left, right, key, path)):
                    raise FoundationError('CONTEST_PROVIDER_GRAPH_CHANGED_REVIEW_REQUIRED:' + '/'.join(path + (key,)))
        elif isinstance(left, list):
            if len(left) != len(right):
                raise FoundationError('CONTEST_PROVIDER_GRAPH_CHANGED_REVIEW_REQUIRED')
            for index, (a, b) in enumerate(zip(left, right)):
                compare(a, b, path + (str(index),))
        elif left != right:
            raise FoundationError('CONTEST_PROVIDER_GRAPH_CHANGED_REVIEW_REQUIRED')
    for key in ('initial_node', 'system_prompt', 'nodes', 'global_nodes', 'tools'):
        default = {} if key in ('nodes', 'global_nodes') else [] if key == 'tools' else ''
        compare(expected.get(key, default), actual.get(key, default), (key,))
    return True


def validate_workflow(workflow, contract):
    """Static wire/endpoint checks; deliberately no claim of policy correctness."""
    if not isinstance(workflow, dict) or not isinstance(workflow.get('nodes'), dict):
        raise FoundationError('CONTEST_WORKFLOW_INVALID')
    nodes = workflow['nodes']
    globals_ = workflow.get('global_nodes', {})
    all_nodes = {**nodes, **globals_}
    if workflow.get('initial_node') not in nodes or not nodes or len(all_nodes) > 180:
        raise FoundationError('CONTEST_GRAPH_INITIAL_NODE_INVALID')
    endpoints = {op.url: op for op in contract.operations.values()}
    used = set()

    def check_code(code):
        if not isinstance(code, str):
            raise FoundationError('CONTEST_DYNAMIC_CODE_REFUSED')
        if '{{' in code or '}}' in code:
            raise FoundationError('CONTEST_CODE_TEMPLATE_DELIMITERS_REFUSED')
        try:
            tree = ast.parse(code)
        except SyntaxError:
            raise FoundationError('CONTEST_CODE_SYNTAX_INVALID') from None
        for element in ast.walk(tree):
            if isinstance(element, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in element.names] if isinstance(element, ast.Import) else [element.module]
                if any(name not in ('json', 're', 'datetime') for name in names):
                    raise FoundationError('CONTEST_CODE_IMPORT_REFUSED')
            if isinstance(element, ast.Name) and element.id in ('eval', 'exec', 'open', '__import__', 'compile'):
                raise FoundationError('CONTEST_CODE_HOST_ACCESS_REFUSED')

    def walk(value):
        if value is None:
            raise FoundationError('CONTEST_WORKFLOW_NULL_REFUSED')
        if isinstance(value, list):
            for child in value:
                walk(child)
        if not isinstance(value, dict):
            return
        for hook in ('pre_process_code', 'post_process_code'):
            if value.get(hook):
                check_code(value[hook])
        function = value.get('function', {})
        handler = function.get('handler')
        if handler == '__function__:api_call':
            fields = function.get('parameters', {}).get('properties', {})
            endpoint = fields.get('url', {}).get('default')
            operation = endpoints.get(endpoint)
            if operation is None or fields.get('method', {}).get('default') != operation.method:
                raise FoundationError('CONTEST_TOOL_ENDPOINT_MISMATCH')
            if value.get('config', {}).get('maxRetries', 0) != 0:
                raise FoundationError('CONTEST_TOOL_BLIND_RETRY_REFUSED')
            body = fields.get('body', {})
            if set(body.get('properties', {})) != set(operation.request.get('properties', {})):
                raise FoundationError('CONTEST_TOOL_BODY_FIELDS_MISMATCH')
            if set(body.get('required', [])) != set(operation.request.get('required', [])):
                raise FoundationError('CONTEST_TOOL_REQUIRED_FIELDS_MISMATCH')
            def compare_schema(actual, expected):
                for key in ('type', 'enum', 'format', 'minimum', 'maximum', 'items', 'additionalProperties'):
                    if key in expected and actual.get(key) != expected[key]:
                        raise FoundationError('CONTEST_TOOL_SCHEMA_MISMATCH')
                if 'properties' in expected:
                    if set(actual.get('properties', {})) != set(expected['properties']):
                        raise FoundationError('CONTEST_TOOL_SCHEMA_MISMATCH')
                    for key, child in expected['properties'].items():
                        compare_schema(actual['properties'][key], child)
            for key, schema in operation.request.get('properties', {}).items():
                compare_schema(body['properties'][key], schema)
            used.add(operation.name)
        elif handler == '__function__:code_exec':
            code = function.get('parameters', {}).get('properties', {}).get('code', {}).get('default')
            check_code(code)
        elif handler and handler != '__function__:end_call':
            raise FoundationError('CONTEST_HANDLER_UNSUPPORTED')
        for child in value.values():
            walk(child)

    walk(workflow)
    for node in all_nodes.values():
        if node.get('node_type') == 'conversation' and node.get('pre_actions'):
            raise FoundationError('CONTEST_STRIPPED_PRE_ACTIONS')
        if node.get('node_type') != 'conversation' and node.get('tools'):
            raise FoundationError('CONTEST_STRIPPED_TOOLS')
        for transition in node.get('transitions', []):
            if transition.get('target_node') not in all_nodes and transition.get('target_node') != '__previous__':
                raise FoundationError('CONTEST_TRANSITION_TARGET_INVALID')
        for key in ('passthrough_default_transition', 'max_turn_transition'):
            if node.get(key) and node[key] not in all_nodes:
                raise FoundationError('CONTEST_TRANSITION_TARGET_INVALID')
    if not {'verify_identity', 'get_account', 'create_promise', 'close_call'} <= used:
        raise FoundationError('CONTEST_REQUIRED_BANK_TOOLS_MISSING')
    from builder.freya_export import measure_code_exec_budget
    code_budget=measure_code_exec_budget(workflow)
    if not code_budget['passed']:
        raise FoundationError('CONTEST_CODE_REQUEST_TOO_LARGE')
    body = raw({'name': 'Final-builder-slot-' + '0' * 32, 'workflow': workflow})
    scan_public(body, '.json')
    if len(body) >= 131072:
        raise FoundationError('CONTEST_PUBLICATION_BODY_TOO_LARGE')
    return {'static_valid': True, 'bank_operations': sorted(used),
            'publication_body_bytes': len(body), 'code_request_budget':code_budget,
            'semantic_correctness_verified': False}


def capture_http_error(error, evidence, ticket, limit):
    """Read once and retain complete error wire before any decoding or hint."""
    body = error.read(limit + 1)
    if evidence:
        evidence.complete_exchange(ticket, http_status=error.code,
            headers=dict(error.headers or {}), body=body, complete=len(body) <= limit)
    if len(body) > limit:
        raise FoundationError('CONTEST_ERROR_RESPONSE_INCOMPLETE_NO_FURTHER_REQUEST')
    return body


class FreyaClient:
    """Deadline-bound, no retries/redirects, sanitised responses in public trace."""
    def __init__(self, budget, trace):
        self.budget, self.trace = budget, trace
        self.audit_path = None
        self.evidence = None
        self.last_exchange = None
        self.key = os.environ.get('FREYA_API_KEY')
        self.url = os.environ.get('FREYA_API_URL', 'https://staging-app.freyavoice.ai').rstrip('/')
        parsed = urlsplit(self.url)
        if not self.key or parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise FoundationError('CONTEST_FREYA_CONNECTION_UNAVAILABLE')
        spec = importlib.util.spec_from_file_location('contest_official_freya', KIT / 'freya.py')
        self.kit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.kit)
        self.kit.api = self.api
        self.kit.AGENT_LLM = {'provider': 'experimental-5', 'model': 'experimental-5'}
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = build_opener(NoRedirect)

    def api(self, method, path, body=None, **params):
        timeout = min(120, self.budget.remaining())
        record = {'method': method, 'path': path, 'params': params, 'status': 'REQUEST_STARTED',
                  'request_sha256': digest(raw(body)) if body is not None else None}
        self.trace.append(record)
        if self.audit_path:
            write(self.audit_path, self.trace)
        encoded = raw(body) if body is not None else None
        if encoded is not None and len(encoded) >= 131072:
            record['status'] = 'REQUEST_NOT_SENT_BODY_TOO_LARGE'
            if self.audit_path:
                write(self.audit_path, self.trace)
            raise FoundationError('CONTEST_ACTUAL_REQUEST_BODY_TOO_LARGE')
        request = Request(self.url + path + ('?' + urlencode(params) if params else ''),
            data=encoded, method=method, headers={'Authorization': 'Bearer ' + self.key,
                'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (build-the-builder)'})
        ticket = self.evidence.begin_exchange(method=method, path=path, body=encoded, params=params) if self.evidence else None
        try:
            with self.opener.open(request, timeout=timeout) as response:
                response_raw = response.read(4_000_001)
                if len(response_raw) > 4_000_000:
                    raise ValueError('response too large')
                if self.evidence:
                    self.evidence.complete_exchange(ticket, http_status=response.status,
                        headers=dict(response.headers), body=response_raw)
                    self.last_exchange = ticket
                value = strict_json(response_raw.decode('utf-8'))
            if isinstance(value, dict) and value.get('success') is False:
                message = value.get('data', {}).get('message', 'provider refusal')
                raise ValueError(str(message))
        except Exception as error:
            failure_body = None
            if isinstance(error, HTTPError):
                try:
                    failure_body = capture_http_error(error, self.evidence, ticket, 4_000_000)
                    self.last_exchange = ticket
                except Exception:
                    record['status'] = 'ERROR_CAPTURE_FAILED_NO_FURTHER_REQUEST'
                    if self.audit_path:
                        write(self.audit_path, self.trace)
                    raise FoundationError('CONTEST_ERROR_CAPTURE_FAILED_NO_FURTHER_REQUEST') from None
            if self.evidence and self.evidence.pending is not None and not self.evidence.failed:
                self.evidence.mark_unknown(ticket)
            dry_run = method == 'POST' and path == '/api/v2/workflow' and params.get('dryRun') == 'true'
            record['status'] = ('VALIDATION_FAILED' if dry_run else 'UNKNOWN_MUTATION_RESULT') if method != 'GET' else 'READ_FAILED'
            if isinstance(getattr(error, 'code', None), int):
                record['http_status'] = error.code
            hint = str(error)[:700] if type(error) is ValueError else None
            if isinstance(error, HTTPError):
                try:
                    failure = strict_json(failure_body.decode('utf-8'))
                    hint = failure.get('data', {}).get('message') or failure.get('message') or failure.get('error')
                    hint = str(hint)[:700] if hint else None
                    details = failure.get('data', {}).get('errors') or failure.get('errors') or failure.get('details') or failure.get('data', {}).get('result')
                    if details is not None and len(raw(details)) < 5000:
                        try:
                            scan_public(raw({'validation_details': details}), '.json')
                            record['validation_details'] = details
                        except FoundationError:
                            pass
                    record['error_response_fields'] = sorted(failure)
                    if isinstance(failure.get('data'), dict):
                        record['error_data_fields'] = sorted(failure['data'])
                except (ValueError, UnicodeError, FoundationError):
                    pass
            # Only publish a short checked message; never a raw exception or
            # HTTP response, which can contain tokens or credential headers.
            if hint:
                if not re.search(r'(Bearer\s|fak_|sk-|ghp_|github_pat_|eyJ)', hint):
                    try:
                        scan_public(raw({'hint': hint}), '.json')
                        record['provider_hint'] = hint
                    except FoundationError:
                        pass
            if self.audit_path:
                write(self.audit_path, self.trace)
            raise FoundationError('CONTEST_PROVIDER_REQUEST_FAILED_NO_RETRY') from None
        record['status'] = 'RESPONSE_RECEIVED'
        record['response_sha256'] = digest(response_raw)
        if self.audit_path:
            write(self.audit_path, self.trace)
        return value.get('data', {}).get('result', value) if isinstance(value, dict) else value


def publish(workflow, *, client, state_path, slot, directory, budget):
    from orchestration.workspace_lock import official_workspace_mutex
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    workspace = client.kit.workspace_id()
    with official_workspace_mutex(ROOT / 'integration/state', workspace):
        state = strict_json(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        old = state.get(slot)
        if old and old.get('status') != 'EXPORTED':
            raise FoundationError('CONTEST_SLOT_HAS_UNRECONCILED_MUTATION')
        wf = copy.deepcopy(workflow)
        # The platform must populate the full voice/compliance/default model
        # object. A partial agent_config fails creation validation. The kit's
        # update helper later merges the fixed event model into those defaults.
        wf.pop('agent_config', None)
        client.kit.validate_workflow('Final-builder-slot-' + slot, wf)
        budget.remaining()
        # Persist before mutation; a timeout/process crash cannot permit a blind repeat.
        state[slot] = {'status': 'MUTATION_PENDING', 'previous': old}
        write(state_path, state)
        ids = {key: old[key] for key in ('workflow_id', 'agent_id')} if old else client.kit.create_workflow('Final-builder-slot-' + slot, wf)
        state[slot].update(ids)
        write(state_path, state)
        version = client.kit.update_workflow(ids['workflow_id'], wf)
        export_path = Path(directory) / 'provider-export.json'
        client.kit.export_workflow(ids['workflow_id'], str(export_path))
        exported = strict_json(export_path.read_text(encoding='utf-8'))
        public_scan(export_path.read_bytes())
        if exported['workflow_id'] != ids['workflow_id'] or exported['agent_id'] != ids['agent_id']:
            raise FoundationError('CONTEST_PROVIDER_EXPORT_ID_MISMATCH')
        stored = exported['workflow']
        stored_llm = stored.get('agent_config', {}).get('llmConfig', {})
        if any(stored_llm.get(key) != value for key, value in client.kit.AGENT_LLM.items()):
            raise FoundationError('CONTEST_EXPORTED_PHONE_MODEL_MISMATCH')
        # Platform defaults/layout are permitted; semantic graph/tool edits are not.
        verify_provider_graph(wf, stored)
        versions = client.api('GET', '/api/v2/versions/workflow/' + ids['workflow_id'], workspaceId=workspace)['versions']
        selected = [row for row in versions if row['versionNumber'] == version and row.get('isPublished')]
        if len(selected) != 1:
            raise FoundationError('CONTEST_PUBLISHED_VERSION_NOT_CONFIRMED')
        budget.remaining()
        receipt = {**ids, 'version_number': version, 'version_id': selected[0]['id'],
                   'export_sha256': digest(export_path.read_bytes()), 'status': 'EXPORTED'}
        state[slot] = receipt
        write(state_path, state)
        return receipt


def run(*, brief_path, tool_api_path, directory, provider='bounded', publish_live=False,
        deadline_seconds=1800, slot=None, model_provider=None, client_factory=FreyaClient,
        practice_case=None, repetitions=1, evidence_factory=None, record_credits=False,
        chat_fixture=None, chat_fixture_sha256=None, chat_authorized=False, chat_evidence_factory=None,
        behavior_fixture=None):
    budget = Budget(deadline_seconds)
    directory = Path(directory).resolve()
    if directory.exists() or not directory.is_relative_to(OUTPUT.resolve()) or directory == OUTPUT.resolve():
        raise FoundationError('CONTEST_FRESH_OUTPUT_DIRECTORY_REQUIRED')
    directory.mkdir(parents=True)
    trace = {'kind': 'contest-builder-run-1', 'run_id': str(uuid.uuid4()),
             'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'STARTED',
             'provider_actions': [], 'tests': [], 'deadline_seconds': deadline_seconds,
             'live_success_verified': False, 'local_behavior_verified': False,
             'final_submission_ready': False}
    try:
        if chat_fixture is not None or chat_fixture_sha256 is not None:
            if chat_fixture is None or not isinstance(chat_fixture_sha256,str) or not re.fullmatch('[a-f0-9]{64}',chat_fixture_sha256):
                raise FoundationError('CONTEST_CHAT_FIXTURE_AND_HASH_REQUIRED')
            if not publish_live or chat_authorized is not True:
                raise FoundationError('CONTEST_CHAT_PUBLICATION_AND_NEW_AUTHORIZATION_REQUIRED')
            if practice_case or repetitions != 1 or record_credits:
                raise FoundationError('CONTEST_CHAT_AND_PRACTICE_SCOPES_MUST_BE_SEPARATE')
            from orchestration.contest_chat_evidence import fixture_value
            fixture_path=Path(chat_fixture)
            if not fixture_path.is_file() or fixture_path.stat().st_size>100_000:
                raise FoundationError('CONTEST_CHAT_FIXTURE_INVALID')
            fixture_bytes=fixture_path.read_bytes()
            if digest(fixture_bytes)!=chat_fixture_sha256:
                raise FoundationError('CONTEST_CHAT_FIXTURE_HASH_MISMATCH')
            fixture_value(fixture_bytes)
            trace['chat_fixture_sha256']=chat_fixture_sha256
        if practice_case and not publish_live:
            raise FoundationError('CONTEST_PRACTICE_REQUIRES_PUBLISHED_WORKFLOW')
        if record_credits and not practice_case:
            raise FoundationError('CONTEST_CREDIT_OBSERVATION_REQUIRES_PRACTICE')
        if record_credits and client_factory is not FreyaClient and evidence_factory is None:
            raise FoundationError('CONTEST_CREDIT_OBSERVATION_REQUIRES_PROTECTED_CAPTURE')
        brief, api = read_source(brief_path), read_source(tool_api_path)
        contract = BankContract(strict_json(api.text), api.source_id, api.sha256)
        trace['inputs'] = {'brief_sha256': brief.sha256, 'tool_api_sha256': api.sha256}
        fixture = None
        if behavior_fixture is not None:
            from orchestration.generated_behavior import BehaviorError, read_fixture
            try:
                fixture = read_fixture(behavior_fixture, brief.sha256, api.sha256)
            except BehaviorError as error:
                raise FoundationError(str(error)) from None
            fixture_bytes = Path(behavior_fixture).read_bytes()
            if digest(fixture_bytes) != fixture['_fixture_sha256']:
                raise FoundationError('CONTEST_BEHAVIOR_FIXTURE_CHANGED_AFTER_READ')
            public_scan(fixture_bytes)
            (directory / 'behavior-fixture.json').write_bytes(fixture_bytes)
            trace['inputs']['behavior_fixture_sha256'] = fixture['_fixture_sha256']
        for filename, source_path, fingerprint in (('input-brief.md',brief_path,brief.sha256),('input-api.json',tool_api_path,api.sha256)):
            content=Path(source_path).read_bytes()
            if digest(content)!=fingerprint: raise FoundationError('CONTEST_INPUT_CHANGED_AFTER_READ')
            (directory/filename).write_bytes(content)
        trace['runtime_sha256'] = {path.relative_to(ROOT).as_posix(): digest(path.read_bytes())
            for package in ('builder', 'compiler', 'orchestration')
            for path in (ROOT / package).glob('*.py')}
        from orchestration.contest_preflight import EVIDENCE_FILES
        trace['runtime_sha256'].update({'integration/'+name:digest((ROOT/'integration'/name).read_bytes()) for name in EVIDENCE_FILES})
        trace['platform_reference_sha256'] = {name: digest((KIT / name).read_bytes()) for name in
            ('freya.py', 'docs/workflow-format.md', 'docs/tools-and-code.md', 'tools/score.py')}
        if provider == 'bounded':
            product = PRODUCT_OUTPUT / ('contest-' + trace['run_id'])
            result = build_product(brief_paths=[brief_path], tool_api_path=tool_api_path,
                output_directory=product, deadline_seconds=math.floor(budget.remaining()),
                native_mode='official', native_runtime_mode='official_bank_session')
            if result['status'] != 'LOCAL_SOURCE_NATIVE_PRODUCT_VERIFIED':
                raise FoundationError('CONTEST_BOUNDED_COMPILATION_REFUSED')
            verified = verify_product_build(product, deadline_monotonic=budget.end)
            workflow = strict_json((product / 'agent.workflow.json').read_text(encoding='utf-8'))
            trace['compilation'] = {'provider': 'bounded-source-compiler', 'calls': 0,
                'product_directory': product.relative_to(ROOT).as_posix(), 'verification': verified}
        elif provider == 'codex':
            from builder.model_workflow import CodexWorkflowProvider
            refs = {name: (KIT / name).read_text(encoding='utf-8') for name in
                    ('docs/workflow-format.md', 'docs/tools-and-code.md')}
            model = model_provider or CodexWorkflowProvider()
            trace['compilation'] = {'provider': 'codex-cli', 'calls': 0, 'attempts': []}
            feedback = None
            behavior_history = []
            for attempt in range(3):
                verify_source_snapshot(trace)
                model_dir = directory / ('model-attempt-' + str(attempt + 1))
                model_dir.mkdir()
                trace['compilation']['calls'] += 1
                record = {'attempt': attempt + 1, 'status': 'REQUEST_STARTED',
                    'model_requested':getattr(model,'model',None),
                    'reasoning_effort_requested':getattr(model,'reasoning_effort',None),
                    'purpose':'Generate source-bound workflow' if not attempt else 'Repair draft from sources and validation diagnostics'}
                trace['compilation']['attempts'].append(record)
                try:
                    workflow, compilation = model.generate(brief=brief.text, api=strict_json(api.text),
                        references=refs, directory=model_dir,
                        remaining_seconds=budget.remaining(), feedback=feedback,
                        tool_templates=contract.freya_templates())
                    checks = validate_workflow(workflow, contract)
                    verify_source_snapshot(trace)
                    record['status'] = 'STATIC_VALID'
                    if fixture is not None:
                        from orchestration.generated_behavior import verify_behavior
                        behavior = verify_behavior(workflow, contract, fixture,
                            model_dir / 'behavior', budget)
                        verify_source_snapshot(trace)
                        write(model_dir / 'behavior-report.json', behavior)
                        record['behavior_check'] = {'passed': behavior['passed'],
                            'report_path': 'model-attempt-' + str(attempt + 1) + '/behavior-report.json'}
                        if not behavior['passed']:
                            raise FoundationError('CONTEST_GENERATED_BEHAVIOR_FAILED')
                        record['status'] = 'LOCAL_BEHAVIOR_PASSED'
                        trace['tests'].append(behavior)
                        trace['local_behavior_verified'] = True
                    trace['compilation'].update({key: value for key, value in compilation.items() if key != 'calls'})
                    break
                except FoundationError as error:
                    record.update(status='REFUSED', diagnostic=str(error))
                    response_path = model_dir / 'model-response.json'
                    if not response_path.is_file() or str(error) in (
                        'BUILDER_MODEL_USED_UNEXPECTED_TOOL', 'BUILDER_MODEL_DEADLINE_EXCEEDED',
                        'BUILDER_MODEL_RESPONSE_UNAVAILABLE', 'BUILDER_MODEL_PROCESS_UNAVAILABLE',
                        'BUILDER_MODEL_PROTOCOL_INVALID', 'BUILDER_MODEL_PROCESS_ERROR',
                        'BUILDER_MODEL_FRESH_OUTPUT_REQUIRED', 'CONTEST_RUNTIME_SOURCE_CHANGED'):
                        raise
                    previous_bytes=response_path.read_bytes()
                    feedback = {'diagnostic': str(error)}
                    if behavior_history:
                        # Historical failure belongs only to its original response.
                        feedback['behavior_history'] = copy.deepcopy(behavior_history)
                    try:
                        feedback['previous_generated_response']=strict_json(previous_bytes.decode('utf-8'))
                    except FoundationError:
                        feedback['previous_generated_response_json']=previous_bytes.decode('utf-8')
                    from orchestration.repair_observations import delimiter_locations, remember_behavior
                    locations = delimiter_locations(feedback.get('previous_generated_response'))
                    if locations:
                        feedback['code_delimiter_locations'] = locations
                    from builder.model_diagnostics import diagnose_model_response
                    assembled = model_dir / 'assembled-response.json'
                    diagnostic_response = (assembled.read_bytes() if assembled.exists() else
                        raw({k:v for k,v in feedback['previous_generated_response'].items() if k not in ('code_blocks','workflow_plan')})
                        if 'previous_generated_response' in feedback else previous_bytes)
                    feedback['diagnostics'] = diagnose_model_response(diagnostic_response,
                        brief.text, lambda candidate: validate_workflow(candidate, contract))
                    if str(error) == 'CONTEST_GENERATED_BEHAVIOR_FAILED':
                        # Independent expectations remain outside the repair prompt.
                        from orchestration.generated_behavior import summarize_behavior_observations
                        feedback['behavior_observations'] = summarize_behavior_observations(behavior, workflow)
                        feedback['behavior_response_sha256'] = digest(previous_bytes)
                        behavior_history = remember_behavior(behavior_history, previous_bytes,
                            feedback['behavior_observations'])
                        feedback['behavior_diagnostics'] = sorted({
                            row.get('diagnostic', 'BEHAVIOR_CASE_FAILED')
                            for row in behavior.get('cases', []) if not row.get('passed')}
                            | ({behavior['diagnostic']} if behavior.get('diagnostic') else set()))
                    write(model_dir / 'diagnostics.json', feedback['diagnostics'])
                    if attempt == 2:
                        raise
        else:
            raise FoundationError('CONTEST_PROVIDER_INVALID')
        verify_source_snapshot(trace)
        if provider == 'codex':
            trace['static_checks'] = validate_workflow(workflow, contract)
        else:
            # The bounded emitter has verified guarded dynamic close URLs and
            # outcome-specific optional fields; its independent replay is the
            # appropriate validator, rather than the model's literal template gate.
            trace['static_checks'] = {'source_export_independently_verified': verified['valid'],
                'semantic_correctness_verified': False,
                'publication_body_bytes': len(raw({'name': 'Final-builder-slot-' + '0' * 32, 'workflow': workflow}))}
        write(directory / 'agent.workflow.json', workflow)
        budget.remaining()
        trace['status'] = 'LOCAL_WORKFLOW_GENERATED'
        if fixture is not None and provider != 'codex':
            from orchestration.generated_behavior import verify_behavior
            behavior = verify_behavior(workflow, contract, fixture, directory / 'behavior', budget)
            verify_source_snapshot(trace)
            write(directory / 'behavior-report.json', behavior)
            trace['tests'].append(behavior)
            if not behavior['passed']:
                raise FoundationError('CONTEST_GENERATED_BEHAVIOR_FAILED')
            trace['local_behavior_verified'] = True
        if trace['local_behavior_verified']:
            trace['status'] = 'LOCAL_BEHAVIOR_PASSED'
        if publish_live:
            verify_source_snapshot(trace)
            chosen_slot = slot or brief.sha256[:24]
            if not chosen_slot.isascii() or not chosen_slot.replace('-', '').isalnum() or len(chosen_slot) > 40:
                raise FoundationError('CONTEST_SLOT_INVALID')
            client = client_factory(budget, trace['provider_actions'])
            client.audit_path = directory / 'provider-actions.json'
            protected = client_factory is FreyaClient or evidence_factory is not None
            if chat_fixture is not None and not protected:
                raise FoundationError('CONTEST_CHAT_REQUIRES_PROTECTED_PUBLICATION_CAPTURE')
            if protected:
                from integration.contest_evidence import ContestEvidence
                from integration.session_guard import PreparationError
                write(directory/'runtime-source-manifest.json',trace['runtime_sha256'])
                pins={'brief':(directory/'input-brief.md').read_bytes(),
                    'tool_api':(directory/'input-api.json').read_bytes(),
                    'source_manifest':(directory/'runtime-source-manifest.json').read_bytes()}
                trace['protected_evidence']={'bundles':[],'status':'CAPTURE_STARTED_TEST_JOIN_REQUIRED'}
                def fresh_evidence(repetition):
                    run_id=trace['run_id'] if repetition==1 else str(uuid.uuid4())
                    private_directory=ROOT/'integration/runs'/('contest-'+run_id)
                    try:
                        sink=(evidence_factory or ContestEvidence)(private_directory,
                            secrets=tuple(value for value in (getattr(client,'key',None),os.environ.get('FREYA_BANK_TEAM_KEY')) if value))
                        for role,content in pins.items(): sink.pin_artifact(role,content)
                    except (ValueError,OSError,PreparationError):
                        raise FoundationError('CONTEST_PROTECTED_EVIDENCE_HOST_UNAVAILABLE') from None
                    client.evidence=sink
                    client.evidence_context={'run_id':run_id,'brief_sha256':digest(pins['brief']),
                        'tool_api_sha256':digest(pins['tool_api']),'source_manifest_sha256':digest(pins['source_manifest'])}
                    trace['protected_evidence']['bundles'].append({'run_id':run_id,'repetition':repetition,
                        'directory':private_directory.relative_to(ROOT).as_posix()})
                    return sink
                client.evidence_factory=fresh_evidence
                fresh_evidence(1)
            trace['publication'] = publish(workflow, client=client,
                state_path=OUTPUT / 'state/slots.json', slot=chosen_slot, directory=directory, budget=budget)
            verify_source_snapshot(trace)
            if protected:
                pins['provider_export']=(directory/'provider-export.json').read_bytes()
                client.evidence.pin_artifact('provider_export',pins['provider_export'])
            trace['status'] = 'PROVIDER_EXPORTED_TESTS_REQUIRED'
            if practice_case:
                from orchestration.contest_practice import run_practice
                evidence = run_practice(client=client, publication=trace['publication'],
                    case_id=practice_case, repetitions=repetitions, contract=contract,
                    directory=directory, budget=budget,record_credits=record_credits)
                trace['tests'].append(evidence)
                trace['live_success_verified'] = evidence['passed']
                trace['status'] = 'PRACTICE_CASE_PASSED' if evidence['passed'] else 'PRACTICE_CASE_FAILED'
                if protected:
                    trace['protected_evidence']['status']='SEALED'
            elif chat_fixture is not None:
                from orchestration.contest_chat import run_chat
                from orchestration.contest_chat_evidence import ChatEvidence
                chat_client=client_factory(budget, [])
                evidence=run_chat(client=chat_client, publication=trace['publication'],
                    workspace_id=client.kit.workspace_id(), brief_path=directory/'input-brief.md',
                    tool_api_path=directory/'input-api.json', source_manifest_path=directory/'runtime-source-manifest.json',
                    provider_export_path=directory/'provider-export.json', fixture_path=chat_fixture,
                    official_fixture_sha256=chat_fixture_sha256, generation_run_id=trace['run_id'],
                    generation_input_sha256={name:digest(pins[name]) for name in ('brief','tool_api','source_manifest')},
                    directory=directory/'chat', budget=budget, live_authorized=chat_authorized,
                    evidence_factory=chat_evidence_factory or ChatEvidence)
                trace['tests'].append(evidence)
                trace['status']=evidence['status']
                if evidence['status']=='BLOCKED':
                    trace['diagnostic']=evidence['diagnostic']
                trace['protected_evidence']['status']='PUBLICATION_CAPTURED_SEPARATE_CHAT_JOIN'
    except Exception as error:
        trace['status'] = 'BLOCKED'
        trace['diagnostic'] = str(error) if isinstance(error, FoundationError) else 'CONTEST_LOCAL_OPERATION_FAILED'
    finally:
        # A test may outlive the last publication check. Never attach success
        # to a source epoch that changed while that test was running.
        try:
            verify_source_snapshot(trace)
        except (FoundationError, OSError):
            trace.update(status='BLOCKED', diagnostic='CONTEST_RUNTIME_SOURCE_CHANGED',
                         live_success_verified=False, local_behavior_verified=False)
        if trace['status']=='BLOCKED' and 'protected_evidence' in trace:
            trace['protected_evidence']['status']='CAPTURE_FAILED_OR_JOIN_INCOMPLETE'
        trace['ended_at'] = datetime.now(timezone.utc).isoformat()
        trace['elapsed_seconds'] = round(budget.clock() - budget.started, 6)
        if trace['elapsed_seconds'] > deadline_seconds:
            trace.update(status='BLOCKED', diagnostic='CONTEST_DEADLINE_EXCEEDED')
        write(directory / 'trace.json', trace)
        inventory = {path.relative_to(directory).as_posix(): digest(path.read_bytes())
                     for path in directory.rglob('*') if path.is_file()}
        write(directory / 'manifest.json', inventory)
        write(directory / 'FINAL.json', {'status': trace['status'],
            'manifest_sha256': digest((directory / 'manifest.json').read_bytes()),
            'final_submission_ready': False})
    return trace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('brief_path', type=Path)
    parser.add_argument('tool_api_path', type=Path)
    parser.add_argument('--provider', choices=('bounded', 'codex'), default='codex')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--slot')
    parser.add_argument('--practice-case', help='Optional official public practice ID, never passed to the model.')
    parser.add_argument('--repetitions', type=int, choices=(1, 3), default=1)
    parser.add_argument('--record-credits',action='store_true',help='Optional captured before/after workspace credit observations for an authorized practice test.')
    parser.add_argument('--behavior-fixture', type=Path,
        help='Independent source-hash-bound offline behavior cases; never sent to the model or bank.')
    parser.add_argument('--chat-fixture',type=Path,help='Explicitly requested new text-chat test using an organizer-supplied variables/messages JSON; no practice case.')
    parser.add_argument('--chat-fixture-sha256',help='Independently obtained SHA256 of that official fixture.')
    parser.add_argument('--deadline-seconds', type=int, default=1800)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    result = run(brief_path=args.brief_path, tool_api_path=args.tool_api_path,
        directory=args.out or OUTPUT / ('run-' + uuid.uuid4().hex), provider=args.provider,
        publish_live=args.publish, slot=args.slot, deadline_seconds=args.deadline_seconds,
        practice_case=args.practice_case, repetitions=args.repetitions,record_credits=args.record_credits,
        chat_fixture=args.chat_fixture,chat_fixture_sha256=args.chat_fixture_sha256,
        chat_authorized=args.chat_fixture is not None, behavior_fixture=args.behavior_fixture)
    print(json.dumps({'status': result['status'], 'elapsed_seconds': result['elapsed_seconds'],
                      'diagnostic': result.get('diagnostic')}, ensure_ascii=False))
    return 2 if result['status'] == 'BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
