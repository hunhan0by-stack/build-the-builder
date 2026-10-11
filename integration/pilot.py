"""One A/PUB-01 pilot. Default CLI is OFFLINE; --live is an explicit opt-in.

No network/credentials are touched by the default CLI or injected E2E tests.
The caller remains responsible for a current human authorization to use --live.
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
from pathlib import Path
import re
import time
import uuid

from .preflight import check_practice_entry, file_hash, read_strict
from .private_state import persist_secret, protect_secret
from .sanitize import sanitize
from .session_guard import Budget, PreparationError, SessionGuard, one_case_payload

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / 'data/freya-hackathon/kit-v3/build-the-builder'
PACKAGE = ROOT / 'artifacts/export-preview/practice-a-pilot-v1'
BEHAVIOR = ROOT / 'artifacts/public39-runs/core39-v3'


def fixed_models():
    # Inspect the kit's defaults without importing its credential-bearing module.
    tree = ast.parse((KIT / 'freya.py').read_text(encoding='utf-8'))
    assignments = {node.targets[0].id: node.value for node in tree.body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
    agent = {}
    for key, value in zip(assignments['AGENT_LLM'].keys, assignments['AGENT_LLM'].values):
        agent[ast.literal_eval(key)] = ast.literal_eval(value.args[1])
    judge = ast.literal_eval(assignments['SIM_LLM'])
    if agent != {'provider': 'experimental-5', 'model': 'experimental-5'} or judge != {'provider': 'google', 'model': 'gemini-3-flash-preview'}:
        raise PreparationError('Kit fixed model defaults changed; review the scoped pilot adapter')
    return agent, judge


def local_admission():
    from orchestration.audit_public39 import audit
    admission = check_practice_entry(PACKAGE)
    fixed_models()
    behavior = read_strict(BEHAVIOR / 'summary.json')
    integrity = audit(BEHAVIOR)
    measured = {Path(row['path']).name: row['sha256'] for row in read_strict(BEHAVIOR / 'source-snapshot.json')
                if row['path'].startswith('builder/')}
    provenance = read_strict(PACKAGE / 'export-report.json')['provenance']
    pub01 = [row for row in behavior['runs'] if row['case_id'] == 'PUB-01']
    if (not integrity['integrity_and_recalculation_passed'] or behavior.get('run_count') != 39
            or behavior.get('ledger_matches') != 39 or behavior.get('local_scope_passes') != 39
            or behavior.get('source_stable_during_runs') is not True or len(pub01) != 3
            or any(row.get('local_scope_passed') is not True for row in pub01)
            or any(measured.get(name) != sha for name, sha in provenance['core_source_sha256'].items())):
        raise PreparationError('Current measured 39-run behavior/core evidence is not admitted')
    opening = []
    for event in read_strict(BEHAVIOR / 'runs/PUB-01/repeat-1/events.json'):
        if event['kind'] == 'customer':
            break
        if event['kind'] == 'agent':
            opening.append(event['payload']['text'])
    if len(opening) != 2:
        raise PreparationError('Measured source-derived initial disclosure/question is incomplete')
    case = next(row for row in read_strict(KIT / 'public-test-set.json') if row['id'] == 'PUB-01')
    return {'admission': admission, 'behavior_summary_sha256': file_hash(BEHAVIOR / 'summary.json'),
            'behavior_integrity': integrity, 'opening': opening,
            'case': case, 'workflow': read_strict(PACKAGE / 'candidate.workflow.json')}


def atomic_json(path, value):
    path = Path(path)
    # Keep the same directory for atomic replacement without extending an
    # already long receipt filename beyond Windows' path limit.
    tmp = path.with_name('.' + str(uuid.uuid4()) + '.tmp')
    with tmp.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise PreparationError('Remote result has no valid durable resource identifier') from None


def retained_fields(expected, actual, path='workflow'):
    """Allow added server defaults, but no missing/changed candidate semantics."""
    bad = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [path]
        for key, item in expected.items():
            if key not in actual:
                bad.append(path + '.' + key)
            else:
                bad.extend(retained_fields(item, actual[key], path + '.' + key))
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return [path]
        for i, (left, right) in enumerate(zip(expected, actual)):
            bad.extend(retained_fields(left, right, f'{path}[{i}]'))
    elif type(expected) is not type(actual) or expected != actual:
        bad.append(path)
    return bad


def persona(case):
    # Same caller facts/output discipline as the kit's persona_for, with its four
    # official public case checks. This is not a new full-brief honesty judge.
    identity = case['identity']
    facts = (f"You are {case['variables']['first_name']} {case['variables']['last_name']}. "
             f"Your date of birth is {identity['date_of_birth']} (say it in words, like '14 March 1988'). "
             f"The last four digits of your phone number on file are {identity['phone_last4']}. "
             "Give these only when asked, and give the wrong ones where the text below says so.")
    prompt = ("OUTPUT RULE: Write only the words you say on the phone. No stage directions, no brackets. "
              "One or two short sentences per turn, then wait.\n\nWHO YOU ARE: " + facts +
              '\n\nWHAT HAPPENS IN THIS CALL: ' + case['script'] +
              '\n\nRULES:\n- Answer what you are asked. Do not volunteer information nobody asked for.\n'
              '- When you give a number or a date, say all of it in one turn. Never stop halfway through.\n'
              '- Do not invent dates, amounts or facts that are not written above. If asked something that is not covered, give a short neutral answer.\n'
              '- If the caller reads something back and it matches what you agreed, say "Yes, I confirm."\n'
              '- When the caller says goodbye, say "Thank you, goodbye." and say nothing more.')
    return {'name': 'PUB-01 ' + case['title'], 'prompt': prompt,
            'successCriteria': 'All of these are true:\n- ' + '\n- '.join(case['checks']),
            'tags': ['hackathon', 'brief-A']}


def ledger_score(case, ledger):
    # Load just the official scoring module; do not call its network helper.
    spec = importlib.util.spec_from_file_location('pilot_kit_score', KIT / 'tools/score.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score_ledger(case, ledger)


def no_lifecycle_actions(workflow):
    # chat/end invokes lifecycle tools. Reject them on both the input and actual
    # readback, including server-added defaults, before the startup chat is sent.
    root_keys = {'tools', 'global_nodes', 'hooks', 'on_call_start', 'on_call_end', 'on_call_end_hooks',
                 'onCallEnd', 'onCallStart', 'end_call_tools', 'lifecycle_tools'}
    if any(workflow.get(key) for key in root_keys):
        raise PreparationError('lifecycle_actions_block_non_bank_bootstrap')
    def inspect(value):
        if isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                lowered = key.lower().replace('_', '').replace('-', '')
                if lowered in {'alwaysrunsat', 'oncallend', 'oncallendhooks', 'endcalltools', 'lifecycletools'} and item:
                    raise PreparationError('lifecycle_actions_block_non_bank_bootstrap')
                inspect(item)
    inspect(workflow)


def no_added_actions(expected, actual):
    if set(actual.get('nodes', {})) != set(expected['nodes']):
        raise PreparationError('readback_added_nodes_or_actions')
    for name, node in actual['nodes'].items():
        source = expected['nodes'][name]
        for key in ('tools', 'pre_actions', 'post_actions', 'extraction_schema', 'transitions'):
            if node.get(key) and key not in source:
                raise PreparationError('readback_added_nodes_or_actions')


def transcript_valid(call, call_id, *, require_user=False):
    if not isinstance(call, dict) or call.get('id') != call_id:
        return False
    messages = call.get('messages')
    return (isinstance(messages, list) and bool(messages)
            and all(isinstance(row, dict) and row.get('role') in {'user', 'assistant', 'system', 'tool'}
                    and isinstance(row.get('message'), str) and row['message'].strip() for row in messages)
            and any(row['role'] == 'assistant' for row in messages)
            and (not require_user or any(row['role'] == 'user' for row in messages)))


def iteration_matches(iteration, version_id, version_number, expected_models):
    if not isinstance(iteration, dict):
        return False
    params = iteration.get('params')
    return (iteration.get('agentVersionId') == version_id
            and iteration.get('agentVersionNumber') is not None
            and iteration.get('agentVersionNumber') == version_number
            and isinstance(params, dict)
            and all(isinstance(params.get(key), dict)
                    and all(params[key].get(field) == value for field, value in expected.items())
                    for key, expected in expected_models.items()))


class PilotController:
    def __init__(self, output, workspace_id, transport, admission, *, state_directory,
                 mode='offline_mock', budget=None, clock=time.monotonic, sleeper=time.sleep,
                 protector=protect_secret, secrets=(), progress=None, local_preflight=None):
        if mode not in {'offline_mock', 'live'}:
            raise PreparationError('Unknown controller mode')
        self.output = Path(output)
        if self.output.exists():
            raise PreparationError('Run output exists; reconcile it instead of repeating operations')
        self.output.mkdir(parents=True)
        self.workspace_id = identifier(workspace_id)
        self.admission = admission
        self.clock, self.sleeper, self.protector = clock, sleeper, protector
        self.secrets = list(secrets)
        self.progress = progress or (lambda stage: None)
        self.local_preflight = local_preflight
        self.state_directory = Path(state_directory)
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.cache = self.state_directory / (hashlib.sha256(workspace_id.encode()).hexdigest() + '.workflow-A-PUB01.json')
        self.state = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'mode': mode,
                      'status': 'reserved', 'scope': 'A/PUB-01 one pilot', 'refs': {}, 'stage': 'local_admission',
                      'official_result': False, 'remaining_cost_uncertainty': 'No per-simulation price or monetary ceiling is documented'}
        self.guard = SessionGuard(self.state_directory, self.workspace_id, transport,
                                  budget=budget or Budget(max_mutations=12), clock=clock)
        self.state['run_id'] = self.guard.run_id
        self.chat_open = False
        self.checkpoint()

    def checkpoint(self, stage=None):
        if stage:
            self.state['stage'] = stage
        atomic_json(self.output / 'state.json', sanitize(self.state, self.secrets))
        if stage:
            self.progress(stage)

    def save(self, name, data):
        atomic_json(self.output / name, sanitize(data, self.secrets))

    def ref_callback(self, key, extractor):
        def store(result):
            value = identifier(extractor(result))
            self.state['refs'][key] = value
            if key == 'workflow_id':
                agent = identifier(result['workflow']['agentId'])
                self.state['refs']['agent_id'] = agent
                atomic_json(self.cache, {'workspace_id': self.workspace_id, 'workflow_id': value,
                                        'agent_id': agent, 'purpose': 'controlled-A-PUB01'})
            if key == 'bootstrap_call_id':
                self.chat_open = True
            self.checkpoint()
        return store

    def remote_inventory(self):
        inventory = self.guard.call('GET', '/api/v2/simulations',
                                    params={'workspaceId': self.workspace_id, 'limit': 1000, 'offset': 0})
        self.save('remote-inventory.json', inventory)
        if self.guard.remote_idle_at is None:
            raise PreparationError('remote_inventory_active_unknown_or_incomplete')

    def workflow_result(self, result):
        item = result.get('workflow') if isinstance(result, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get('workflow'), dict):
            raise PreparationError('workflow_result_wrapper_missing')
        if identifier(item.get('workspaceId')) != self.workspace_id:
            raise PreparationError('workflow_result_workspace_mismatch')
        workflow_id, agent_id = identifier(item.get('id')), identifier(item.get('agentId'))
        if (self.state['refs'].get('workflow_id', workflow_id) != workflow_id
                or self.state['refs'].get('agent_id', agent_id) != agent_id):
            raise PreparationError('workflow_result_resource_mismatch')
        return item

    def credits(self):
        value = self.guard.call('GET', f'/api/v2/workspaces/{self.workspace_id}/credits')
        if (not isinstance(value, dict) or any(type(value.get(k)) not in (int, float)
                or not math.isfinite(value[k]) or value[k] < 0 for k in ('creditsLeft', 'creditUsdRate'))):
            raise PreparationError('quota_fields_missing_or_invalid')
        return {key: value[key] for key in ('creditsLeft', 'creditUsdRate')}

    def end_bootstrap(self):
        call_id = self.state['refs']['bootstrap_call_id']
        def valid(result):
            if identifier(result.get('callId')) != call_id or result.get('ended') is not True:
                raise PreparationError('bootstrap_end_not_confirmed')
        self.guard.call('POST', '/api/v2/chat/end', body={'callId': call_id}, validate_result=valid)
        self.chat_open = False
        self.state['bootstrap_closed'] = True
        self.checkpoint()

    def bootstrap_valid(self, chat, call):
        if (not transcript_valid(call, self.state['refs']['bootstrap_call_id'])
                or chat.get('warning') or chat.get('endedByAgent')):
            return False
        text = chat.get('response', '')
        normalized = ' '.join(text.lower().split()) if isinstance(text, str) else ''
        if any(' '.join(expected.lower().split()) not in normalized for expected in self.admission['opening']):
            return False
        visited = set()
        for message in call.get('messages', []):
            if message.get('role') == 'tool' or message.get('tool_calls') or message.get('function_call'):
                return False
            snapshot = message.get('workflowSnapshot', {}) or {}
            visited.update(node for node in snapshot.get('enteredNodes', []) if isinstance(node, str))
            table = snapshot.get('variableTable', {}) or {}
            fault = table.get('_bridge_fault', {})
            if isinstance(fault, dict) and fault.get('value'):
                return False
            if message.get('role') != 'system':
                continue
            event = message.get('message')
            if isinstance(event, str):
                try:
                    event = json.loads(event)
                except ValueError:
                    return False
            if not isinstance(event, dict):
                return False
            if event.get('event') == 'error':
                return False
            if event.get('event') == 'function_call_start':
                fn = event.get('function', event.get('name'))
                if isinstance(fn, dict):
                    name, handler = fn.get('name'), fn.get('handler', '__function__:code_exec')
                    allowed = name == 'code_exec' and handler == '__function__:code_exec'
                else:
                    allowed = fn in {'code_exec', '__function__:code_exec'} if isinstance(fn, str) else False
                # Inspect the function descriptor, not bundled code text which may
                # legitimately mention api_call without executing a network action.
                args = event.get('args', {})
                if not allowed or not isinstance(args, dict) or args.get('url'):
                    return False
            if event.get('event') == 'node_enter':
                node = event.get('node')
                if node == 'bridge_fault':
                    return False
                if isinstance(node, str):
                    visited.add(node)
        return {'bridge_init', 'bridge_drive', 'bridge_listen'}.issubset(visited)

    def run(self):
        try:
            self.save('admission.json', {k: v for k, v in self.admission.items() if k not in {'case', 'workflow'}})
            if self.local_preflight is not None:
                self.checkpoint('response_diagnostics_preflight')
                self.local_preflight()
            self.checkpoint('remote_inventory_quota')
            self.remote_inventory()
            self.state['credits_before'] = self.credits()
            if self.state['credits_before']['creditsLeft'] <= 0:
                raise PreparationError('no_positive_credit_balance')
            workflow = self.admission['workflow']
            no_lifecycle_actions(workflow)
            self.checkpoint('dry_run_save')
            self.guard.call('POST', '/api/v2/workflow', body={'name': 'build-builder-A-PUB01', 'workflow': workflow}, params={'dryRun': 'true'})
            if self.cache.exists():
                cache = read_strict(self.cache)
                if cache.get('workspace_id') != self.workspace_id or cache.get('purpose') != 'controlled-A-PUB01':
                    raise PreparationError('workflow_cache_scope_mismatch')
                self.state['refs']['workflow_id'] = identifier(cache['workflow_id'])
                self.state['refs']['agent_id'] = identifier(cache['agent_id'])
                self.checkpoint()
                owner = self.workflow_result(self.guard.call('GET', '/api/v2/workflow/' + cache['workflow_id'], params={'workspaceId': self.workspace_id}))
                if not owner.get('name', '').startswith('build-builder-A-PUB01'):
                    raise PreparationError('workflow_cache_remote_owner_mismatch')
                if retained_fields(workflow, owner['workflow']):
                    self.guard.call('PUT', '/api/v2/workflow/' + cache['workflow_id'], body={'workflow': workflow})
            else:
                self.guard.call('POST', '/api/v2/workflow',
                                body={'name': 'build-builder-A-PUB01 ' + self.guard.run_id, 'workflow': workflow},
                                validate_result=self.workflow_result,
                                persist_result=self.ref_callback('workflow_id', lambda r: r['workflow']['id']))
            workflow_id = self.state['refs']['workflow_id']
            self.checkpoint('model_publish_readback')
            remote = self.workflow_result(self.guard.call('GET', '/api/v2/workflow/' + workflow_id, params={'workspaceId': self.workspace_id}))['workflow']
            model, judge_model = fixed_models()
            self.state['fixed_models'] = {'agent': model, 'judge': judge_model, 'kit_source_sha256': file_hash(KIT / 'freya.py')}
            config = remote.get('agent_config') or {}
            llm = config.get('llmConfig') or {}
            if any(llm.get(k) != v for k, v in model.items()):
                config['llmConfig'] = {**llm, **model}
                remote['agent_config'] = config
                self.guard.call('PUT', '/api/v2/workflow/' + workflow_id, body={'workflow': remote})
            versions = self.guard.call('GET', '/api/v2/versions/workflow/' + workflow_id,
                                       params={'workspaceId': self.workspace_id})['versions']
            if not versions:
                raise PreparationError('published_version_missing')
            version = max(versions, key=lambda v: v['versionNumber'])
            if type(version.get('versionNumber')) not in (int, float) or version['versionNumber'] <= 0:
                raise PreparationError('version_number_missing_or_invalid')
            self.state['refs']['version_id'] = identifier(version['id'])
            self.state['published_version_number'] = version['versionNumber']
            self.checkpoint()
            if not version.get('isPublished'):
                self.guard.call('POST', f'/api/v2/versions/workflow/{workflow_id}/publish', body={'versionId': version['id']})
            versions_after = self.guard.call('GET', '/api/v2/versions/workflow/' + workflow_id,
                                             params={'workspaceId': self.workspace_id})['versions']
            if (not versions_after or max(versions_after, key=lambda v: v['versionNumber'])['id'] != version['id']
                    or not any(v['id'] == version['id'] and v.get('isPublished') is True for v in versions_after)):
                raise PreparationError('published_version_not_confirmed')
            saved = self.workflow_result(self.guard.call('GET', '/api/v2/workflow/' + workflow_id, params={'workspaceId': self.workspace_id}))
            no_lifecycle_actions(saved['workflow'])
            no_added_actions(workflow, saved['workflow'])
            mismatches = retained_fields(workflow, saved['workflow'])
            self.save('readback-comparison.json', {'semantic_fields_retained': not mismatches, 'changed_paths': mismatches})
            self.save('published-export.sanitized.json', saved)
            if mismatches or any(saved['workflow'].get('agent_config', {}).get('llmConfig', {}).get(k) != v for k, v in model.items()):
                raise PreparationError('saved_graph_or_fixed_model_changed')
            self.state['readback_semantics_passed'] = True
            self.checkpoint('single_bootstrap_chat')
            bootstrap_vars = {**self.admission['case']['variables'], 'customer_id': 'bootstrap-no-bank-session-' + self.guard.run_id}
            chat = self.guard.call('POST', '/api/v2/chat/message',
                                   body={'workflowId': workflow_id, 'versionId': version['id'], 'message': 'Hello?', 'customerVariables': bootstrap_vars},
                                   persist_result=self.ref_callback('bootstrap_call_id', lambda r: r['callId']))
            boot_call = self.guard.call('GET', '/api/v2/call/' + self.state['refs']['bootstrap_call_id'])['call']
            boot_ok = self.bootstrap_valid(chat, boot_call)
            self.save('bootstrap.json', {'passed': boot_ok, 'response': chat, 'call': boot_call,
                                         'variables_are_non_bank_stub': True})
            self.end_bootstrap()
            after_end = self.guard.call('GET', '/api/v2/call/' + self.state['refs']['bootstrap_call_id'])['call']
            after_ok = self.bootstrap_valid(chat, after_end)
            self.save('bootstrap-after-end.json', {'no_bank_actions_or_errors': after_ok, 'call': after_end})
            boot_ok = boot_ok and after_ok
            if not boot_ok:
                raise PreparationError('bootstrap_opening_or_runtime_failed_simulation_deferred')
            self.state['bootstrap_passed'] = True
            self.checkpoint('one_pub01_session_persona')
            def store_customer(result):
                customer_id = result.get('variables', {}).get('customer_id')
                if not isinstance(customer_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', customer_id):
                    raise PreparationError('bank_session_identifier_missing')
                self.secrets.append(customer_id)
                self.customer_id = customer_id
                persist_secret(self.output / 'bank-session.dpapi', customer_id, protector=self.protector)
                self.state['bank_customer_id_sha256'] = hashlib.sha256(customer_id.encode()).hexdigest()
                self.state['bank_secret_file'] = 'bank-session.dpapi'
                self.checkpoint()
            session = self.guard.call('POST', '/v1/practice/sessions', body={'case': 'PUB-01'}, persist_result=store_customer)
            variables = session.get('variables')
            if (not isinstance(variables, dict) or any(not isinstance(value, str) for value in variables.values())
                    or any(variables.get(k) != value for k, value in self.admission['case']['variables'].items())):
                raise PreparationError('bank_session_variables_not_exact_case_strings')
            made = self.guard.call('POST', '/api/v2/personas', body={'personas': [persona(self.admission['case'])]},
                                   persist_result=self.ref_callback('persona_id', lambda r: r['personas'][0]['id']))
            self.checkpoint('single_official_simulation')
            self.remote_inventory()
            request = one_case_payload(self.state['refs']['agent_id'], self.state['refs']['persona_id'], session['variables'],
                                       name='A-PUB01 ' + self.guard.run_id, agent_model=model,
                                       judge_model=judge_model)
            request['agentVersionId'] = version['id']
            self.guard.call('POST', '/api/v2/simulations', body=request,
                            persist_result=self.ref_callback('simulation_id', lambda r: r.get('simulation', r)['id']))
            simulation_id = self.state['refs']['simulation_id']
            status = None
            while status not in {'completed', 'failed'}:
                if self.clock() >= self.guard.deadline:
                    raise PreparationError('pilot_deadline_remote_state_unknown')
                value = self.guard.call('GET', '/api/v2/simulations/' + simulation_id)
                status = value.get('simulation', value).get('status')
                self.state['remote_status'] = status
                self.checkpoint()
                if status == 'paused':
                    raise PreparationError('simulation_paused_slot_retained')
                if status not in {'completed', 'failed'}:
                    self.sleeper(min(10, max(0, self.guard.deadline - self.clock())))
            self.checkpoint('iteration_transcript_ledger_judge')
            iterations = self.guard.call('GET', f'/api/v2/simulations/{simulation_id}/iterations')['iterations']
            if len(iterations) != 1:
                raise PreparationError('expected_exactly_one_iteration')
            expected_models = {key: request[key] for key in ('assistantLlmOverride', 'routingLlmOverride', 'extractionLlmOverride',
                                                           'personaLlmOverride', 'analysisLlmOverride')}
            if not iteration_matches(iterations[0], version['id'], self.state['published_version_number'], expected_models):
                raise PreparationError('iteration_list_published_version_or_models_mismatch')
            iteration_id = identifier(iterations[0]['id'])
            self.state['refs']['iteration_id'] = iteration_id
            self.checkpoint()
            runs = self.guard.call('GET', f'/api/v2/simulations/{simulation_id}/iterations/{iteration_id}', params={'limit': 100, 'offset': 0})
            iteration = runs.get('iteration', {})
            if (runs.get('runsTotal') != 1 or len(runs.get('runs', [])) != 1
                    or iteration.get('id') != iteration_id
                    or not iteration_matches(iteration, version['id'], self.state['published_version_number'], expected_models)):
                raise PreparationError('one_run_or_published_version_evidence_mismatch')
            judged = runs['runs'][0]
            if judged.get('personaId') != self.state['refs']['persona_id']:
                raise PreparationError('simulation_persona_mismatch')
            call_id = identifier(judged.get('callId'))
            self.state['refs']['simulation_call_id'] = call_id
            self.checkpoint()
            call = self.guard.call('GET', '/api/v2/call/' + call_id)['call']
            if not transcript_valid(call, call_id, require_user=True):
                raise PreparationError('call_transcript_missing_malformed_or_wrong_id')
            ledger = self.guard.call('GET', '/v1/practice/ledger/' + self.customer_id)
            self.save('iteration.json', runs)
            self.save('call.sanitized.json', call)
            self.save('ledger.sanitized.json', ledger)
            score = ledger_score(self.admission['case'], ledger)
            judge_pass = judged.get('outcome') == 'pass'
            outcome = {'kit_ledger_score': score, 'official_public_case_checks': {'outcome': judged.get('outcome'), 'reason': judged.get('reason'),
                       'checks': self.admission['case']['checks']}, 'policy_honesty_scope': 'Only the configured official public case checks; no separate full-brief honesty judge',
                       'full_policy_and_honesty_independently_validated': False,
                       'scenario_helper_pass': status == 'completed' and score['pass'] and judge_pass,
                       'official_result': self.state['mode'] == 'live'}
            self.save('result.json', outcome)
            self.state.update(status='finished', scenario_helper_pass=outcome['scenario_helper_pass'],
                              official_result=self.state['mode'] == 'live')
            self.state['credits_after'] = self.credits()
            self.state['observed_credit_delta'] = self.state['credits_before']['creditsLeft'] - self.state['credits_after']['creditsLeft']
            self.state['delta_is_exact_test_price'] = False
            self.guard.close()
        except Exception as exc:
            self.state['status'] = 'blocked_or_incomplete'
            # Use only our own safe error messages; foreign exception text is never emitted.
            self.state['blocker'] = str(exc) if isinstance(exc, PreparationError) else 'unexpected_result_or_persistence_failure'
            if self.chat_open and not self.guard.pending and not self.guard.simulations:
                try:
                    self.end_bootstrap()
                except Exception:
                    self.state['bootstrap_cleanup'] = 'unknown_slot_retained'
            if not self.chat_open and not self.guard.pending and (not self.guard.simulations or self.guard.simulation_terminal):
                try:
                    self.guard.close()
                except Exception:
                    pass
        finally:
            self.state.update(ended_at_utc=datetime.now(timezone.utc).isoformat(),
                              requests=self.guard.requests, mutations=self.guard.mutations,
                              simulation_attempts_reserved=self.guard.simulations,
                              slot_retained=self.guard.lock_path.exists(), unknown_mutations=len(self.guard.pending))
            self.checkpoint()
        return sanitize(self.state, self.secrets)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly authorized LIVE pilot; never use while API calls are paused')
    parser.add_argument('--out', type=Path, default=ROOT / 'integration/runs/first-A-PUB01')
    args = parser.parse_args(argv)
    admitted = local_admission()
    if not args.live:
        report = {'mode': 'offline_only', 'local_admission_passed': True, 'network_calls': 0, 'credentials_read': False,
                  'live_execution_started': False, 'scope': admitted['admission']['scope'],
                  'next_live_steps': ['slot/quota', 'save/publish/readback', 'startup chat', 'one PUB-01 simulation']}
        print(json.dumps(report))
        return 0
    if not args.out.resolve().is_relative_to(ROOT / 'integration/runs'):
        parser.error('Live output must stay inside integration/runs')
    # Credentials are touched ONLY beyond the explicit --live branch.
    from .transport import FreyaTransport
    from .bank_transport import BankPracticeTransport
    from .response_diagnostics import ResponseDiagnostics
    api_key, team_key = os.environ.get('FREYA_API_KEY', ''), os.environ.get('FREYA_BANK_TEAM_KEY', '')
    diagnostics = ResponseDiagnostics(args.out / 'http-diagnostics', secrets=(api_key, team_key))
    freya = FreyaTransport(api_key, allow_writes=True, diagnostics=diagnostics, require_diagnostics=True)
    bank = BankPracticeTransport(team_key)
    connection = read_strict(ROOT / 'project-management/freya-connection.json')
    def combined(method, path, **kwargs):
        return (bank if path.startswith('/v1/practice/') else freya)(method, path, **kwargs)
    controller = PilotController(args.out, connection['workspace_id'], combined, admitted,
                                 state_directory=ROOT / 'integration/state', mode='live', secrets=(api_key, team_key),
                                 local_preflight=diagnostics.preflight,
                                 progress=lambda stage: print(json.dumps({'stage': stage}), flush=True))
    state = controller.run()
    print(json.dumps({k: state[k] for k in ('status', 'stage', 'simulation_attempts_reserved', 'slot_retained', 'official_result')}))
    return 0 if state.get('status') == 'finished' and state.get('scenario_helper_pass') else 2


if __name__ == '__main__':
    raise SystemExit(main())
