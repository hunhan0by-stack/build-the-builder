"""Optional bounded official practice test, using fresh bank sessions per repetition.

Cases are evaluation inputs only. This module never provides their expected
outputs to the builder or changes an agent. It does not test hidden-bank cases.
"""
import importlib.util
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.error import HTTPError

from builder.models import FoundationError, strict_json
from orchestration.workspace_lock import official_workspace_mutex, SimulationReservation


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_practice(*, client, publication, case_id, repetitions, contract, directory, budget, record_credits=False):
    from orchestration.contest_build import KIT, ROOT, FreyaClient, write, raw, digest, capture_http_error
    if repetitions not in (1, 3):
        raise FoundationError('CONTEST_PRACTICE_REPS_MUST_BE_1_OR_3')
    if getattr(client, 'evidence', None) and repetitions != 1 and not callable(getattr(client,'evidence_factory',None)):
        raise FoundationError('CONTEST_PROTECTED_RECORDER_REQUIRES_ONE_RUN_PER_BUNDLE')
    if record_credits and not getattr(client,'evidence',None):
        raise FoundationError('CONTEST_CREDIT_OBSERVATION_REQUIRES_PROTECTED_CAPTURE')
    protected_required = isinstance(client, FreyaClient) or getattr(client, 'evidence', None) is not None
    if protected_required and getattr(client, 'evidence', None) is None:
        raise FoundationError('CONTEST_SIMULATION_PROTECTED_CHAIN_REQUIRED_NO_DISPATCH')
    cases = strict_json((KIT / 'public-test-set.json').read_text(encoding='utf-8'))
    selected = [case for case in cases if case['id'] == case_id]
    if len(selected) != 1:
        raise FoundationError('CONTEST_OFFICIAL_PRACTICE_CASE_REQUIRED')
    # These fixture sessions belong solely to the official public practice host.
    parsed = urlsplit(contract.server)
    if parsed.scheme != 'https' or parsed.hostname != 'freya-bank-mock.freyavoice.workers.dev':
        raise FoundationError('CONTEST_PUBLIC_CASES_CANNOT_TEST_DIFFERENT_BANK')
    team_key = os.environ.get('FREYA_BANK_TEAM_KEY')
    if not team_key or len(team_key) < 16:
        raise FoundationError('CONTEST_BANK_TEST_CONNECTION_UNAVAILABLE')
    case = selected[0]
    score = load_module('contest_official_score', KIT / 'tools/score.py')
    # Build the official persona wording without importing run_cases (which
    # changes sys.path and brings an unbounded simulation loop into the host).
    identity = ' '.join('Your ' + key.replace('_', ' ') + ' is ' + str(value) +
                        (' (say it in words, such as 14 March 1988).' if key == 'date_of_birth' else '.')
                        for key, value in case['identity'].items())
    customer_name = case['variables']['first_name'] + ' ' + case['variables']['last_name']
    who = ('You are NOT ' + customer_name + '. You do not know their security answers and would not give them.'
           if case['family'] == 'Third party' or case.get('third_party')
           else 'You are ' + customer_name + '. ' + identity)
    persona = {'name': 'Builder validation ' + case_id,
        'prompt': 'Say only the words you say on the phone, one or two short sentences per turn. '
                  'Answer what you are asked; give security answers only when asked. '
                  'Do not invent dates, amounts or facts. Give numbers and dates fully in one turn. '
                  'Confirm only a readback that matches what you agreed. '
                  + who + '\nWHAT HAPPENS: ' + case['script'],
        'successCriteria': 'All of these are true:\n- ' + '\n- '.join(case['checks']),
        'tags': ['hackathon', 'builder-validation']}
    journal = []
    evidence = getattr(client, 'evidence', None)
    roles = {}
    def role(name):
        if evidence:
            roles[name] = client.last_exchange
    journal_path = Path(directory) / 'practice-operation-journal.json'

    def record(event):
        journal.append(event)
        write(journal_path, journal)

    def provider(method, path, body=None, **params):
        if len(journal) >= 256:
            raise FoundationError('CONTEST_PRACTICE_REQUEST_BUDGET_EXHAUSTED')
        record({'method': method, 'path': path, 'status': 'REQUEST_STARTED'})
        result = client.api(method, path, body, **params)
        record({'method': method, 'path': path, 'status': 'RESPONSE_RECEIVED',
                'response_digest': digest(raw(result))})
        return result

    def bank(path, body=None):
        timeout = min(20, budget.remaining())
        method = 'POST' if body is not None else 'GET'
        record({'service': 'practice-bank', 'method': method, 'path_sha256': digest(path.encode()),
                'status': 'REQUEST_STARTED'})
        request = Request('https://' + parsed.netloc + path,
            data=raw(body) if body is not None else None, method=method,
            headers={'x-team-key': team_key, 'Content-Type': 'application/json',
                     'User-Agent': 'freya-bank-scorer/0.1'})
        ticket = evidence.begin_exchange(method=method, path=path, body=raw(body) if body is not None else None) if evidence else None
        try:
            with client.opener.open(request, timeout=timeout) as response:
                content = response.read(2_000_001)
                if len(content) > 2_000_000:
                    raise ValueError()
                if evidence:
                    evidence.complete_exchange(ticket, http_status=response.status,
                        headers=dict(response.headers), body=content)
                    client.last_exchange = ticket
                result = strict_json(content.decode('utf-8'))
        except Exception as error:
            if isinstance(error, HTTPError):
                try:
                    capture_http_error(error, evidence, ticket, 2_000_000)
                    client.last_exchange = ticket
                except Exception:
                    raise FoundationError('CONTEST_ERROR_CAPTURE_FAILED_NO_FURTHER_REQUEST') from None
            if evidence and evidence.pending is not None and not evidence.failed:
                evidence.mark_unknown(ticket)
            raise FoundationError('CONTEST_PRACTICE_BANK_RESULT_UNKNOWN_NO_RETRY') from None
        record({'service': 'practice-bank', 'method': method, 'status': 'RESPONSE_RECEIVED',
                'response_digest': digest(content)})
        return result

    workspace = client.kit.workspace_id()
    rows = []
    with official_workspace_mutex(ROOT / 'integration/state', workspace):
        reservation = SimulationReservation(ROOT / 'integration/state', workspace,
            require_protected=protected_required)
        reservation.require_available()
        # Never launch while another simulation is running, including one with
        # an uncertain prior response. No automatic queue/retry is permitted.
        inventory = provider('GET', '/api/v2/simulations', workspaceId=workspace, limit=100, offset=0)
        if type(inventory) is not dict:
            raise FoundationError('CONTEST_SIMULATION_INVENTORY_NEEDS_REVIEW')
        inventory_keys = [key for key in ('simulations', 'items') if key in inventory]
        if not inventory_keys or any(type(inventory[key]) is not list for key in inventory_keys):
            raise FoundationError('CONTEST_SIMULATION_INVENTORY_NEEDS_REVIEW')
        if len(inventory_keys) == 2:
            try:
                matching = (json.dumps(inventory['simulations'], sort_keys=True, allow_nan=False)
                            == json.dumps(inventory['items'], sort_keys=True, allow_nan=False))
            except (TypeError, ValueError):
                matching = False
            if not matching:
                raise FoundationError('CONTEST_SIMULATION_INVENTORY_NEEDS_REVIEW')
        simulations = inventory[inventory_keys[0]]
        if len(simulations) >= 100 or any(type(row) is not dict for row in simulations):
            raise FoundationError('CONTEST_SIMULATION_INVENTORY_NEEDS_REVIEW')
        if type(inventory.get('total')) is not int or inventory['total'] < 0 or inventory['total'] != len(simulations):
            raise FoundationError('CONTEST_SIMULATION_INVENTORY_NEEDS_REVIEW')
        if any(row.get('workspaceId') != workspace for row in simulations):
            raise FoundationError('CONTEST_SIMULATION_INVENTORY_WORKSPACE_MISMATCH')
        if any(type(row.get('status')) is not str or row['status'] not in ('completed', 'failed', 'paused', 'cancelled') for row in simulations):
            raise FoundationError('CONTEST_WORKSPACE_HAS_NONTERMINAL_SIMULATION')
        made = provider('POST', '/api/v2/personas', {'personas': [persona]})['personas']
        if len(made) != 1:
            raise FoundationError('CONTEST_PERSONA_RESULT_INVALID')
        persona_id = made[0]['id']
        for repetition in range(repetitions):
            if repetition and evidence:
                evidence=client.evidence_factory(repetition+1)
                roles.clear()
            credit=None
            if record_credits:
                from orchestration.credit_observation import CreditObservation
                credit=CreditObservation(client,workspace)
                before_credit=credit.before()
            session = bank('/v1/practice/sessions', {'case': case_id})
            role('session')
            variables = session['variables']
            customer = variables['customer_id']
            record({'status': 'FRESH_SESSION_ACQUIRED', 'customer_sha256': digest(customer.encode()),
                    'repetition': repetition + 1})
            model = {'provider': 'experimental-5', 'model': 'experimental-5'}
            judge = {'provider': 'google', 'model': 'gemini-3-flash-preview'}
            simulation_body = {
                'agentId': publication['agent_id'], 'agentVersionId': publication['version_id'],
                'name': 'Builder validation ' + case_id, 'mode': 'chat',
                'selectedPersonaIds': [persona_id], 'customerVariablesMap': {persona_id: variables},
                'maxConcurrentCalls': 1, 'runsPerPersona': 1, 'maxRetries': 0, 'maxCallDurationSeconds': 240,
                'assistantLlmOverride': model, 'routingLlmOverride': model, 'extractionLlmOverride': model,
                'personaLlmOverride': judge, 'analysisLlmOverride': judge}
            reservation.reserve(simulation_body)
            created = provider('POST', '/api/v2/simulations', simulation_body)
            role('simulation')
            created_simulation = created.get('simulation', created)
            reservation.created(created_simulation)
            settings = created_simulation.get('settings', {})
            if type(settings.get('maxRetries')) is not int or settings['maxRetries'] != 0:
                raise FoundationError('CONTEST_SIMULATION_RETRY_SETTING_MISMATCH_NO_RETRY')
            simulation = created_simulation['id']
            record({'status': 'SIMULATION_STARTED', 'simulation_id': simulation})
            status = None
            for unused in range(30):
                time.sleep(min(10, budget.remaining()))
                current = provider('GET', '/api/v2/simulations/' + simulation)
                reservation.observe_terminal(current.get('simulation', current))
                status = (current.get('simulation') or current).get('status')
                if status in ('completed', 'failed', 'paused'):
                    break
            if status != 'completed':
                raise FoundationError('CONTEST_PRACTICE_SIMULATION_NOT_COMPLETED_NO_RETRY')
            iterations = provider('GET', '/api/v2/simulations/' + simulation + '/iterations')
            iterations = iterations.get('iterations') or iterations.get('items') or iterations
            if not isinstance(iterations, list) or len(iterations) != 1:
                raise FoundationError('CONTEST_PRACTICE_ITERATION_MISMATCH')
            iteration = iterations[0]['id']
            result = provider('GET', '/api/v2/simulations/' + simulation + '/iterations/' + iteration, limit=100, offset=0)
            role('iteration')
            models = {key: model for key in ('assistantLlmOverride', 'routingLlmOverride', 'extractionLlmOverride')}
            models.update({key: judge for key in ('personaLlmOverride', 'analysisLlmOverride')})
            params = result.get('iteration', {}).get('params', {})
            bound = (result.get('iteration', {}).get('agentVersionId') == publication['version_id']
                and result.get('iteration', {}).get('agentVersionNumber') == publication['version_number']
                and all(isinstance(params.get(key), dict) and all(params[key].get(k) == v for k, v in pair.items()) for key, pair in models.items()))
            if not bound:
                raise FoundationError('CONTEST_PRACTICE_ACTUAL_VERSION_MODEL_MISMATCH')
            runs = result['runs']
            if len(runs) != 1 or runs[0].get('personaId') != persona_id:
                raise FoundationError('CONTEST_PRACTICE_RUN_MISMATCH')
            run = runs[0]
            ledger = bank('/v1/practice/ledger/' + customer)
            role('ledger')
            checked = score.score_ledger(case, ledger)
            call_id = run.get('callId')
            call = provider('GET', '/api/v2/call/' + call_id)['call'] if call_id else {}
            role('call')
            transcript_present = isinstance(call.get('messages'), list) and bool(call['messages'])
            rows.append({'case_id': case_id, 'repetition': repetition + 1,
                'simulation_id': simulation, 'iteration_id': iteration, 'call_id': call_id,
                'customer_sha256': digest(customer.encode()),
                'ledger_passed': checked['pass'], 'judge_outcome': run.get('outcome'),
                'actual_version_models_verified': bound,
                'transcript_present': transcript_present,
                'ledger_sha256': digest(raw(ledger)), 'call_sha256': digest(raw(call)),
                'tool_sequence': [item['tool'] for item in ledger['calls']],
                'passed': checked['pass'] and run.get('outcome') == 'pass' and transcript_present})
            write(Path(directory) / 'practice-results.json', rows)
            rows[-1]['credit_observation']=credit.after(before_credit) if credit else {'status':'NOT_MEASURED'}
            if evidence:
                provider('GET', '/api/v2/workflow/' + publication['workflow_id'], workspaceId=workspace)
                role('readback')
                provider('GET', '/api/v2/versions/workflow/' + publication['workflow_id'], workspaceId=workspace)
                role('versions')
                context = client.evidence_context
                binding = {**context, 'workspace_id': workspace, **{k: publication[k] for k in ('workflow_id', 'agent_id', 'version_id', 'version_number', 'export_sha256')},
                    'simulation_id': simulation, 'persona_id': persona_id, 'iteration_id': iteration,
                    'call_id': call_id, 'customer_sha256': digest(customer.encode()), 'models': models, 'case_id': case_id}
                seal = evidence.seal(binding=binding, roles=roles)
                write(Path(directory) / ('practice-evidence-seal-'+str(repetition+1)+'.json'), seal)
                if repetitions==1: write(Path(directory) / 'practice-evidence-seal.json', seal)
                rows[-1]['evidence_seal_sha256']=seal['sha256']
                write(Path(directory) / 'practice-results.json', rows)
            record({'status': 'SESSION_CONSUMED_NO_REUSE', 'customer_sha256': digest(customer.encode())})
            if evidence:
                reservation.release_after_verified_chain(evidence=evidence, seal=seal, binding=binding)
            else:
                reservation.release_after_local_terminal()
            if not rows[-1]['passed']:
                break
    return {'kind': 'official_public_practice', 'case_id': case_id,
            'requested_repetitions': repetitions, 'runs': rows,
            'passed': len(rows) == repetitions and all(row['passed'] for row in rows)}
