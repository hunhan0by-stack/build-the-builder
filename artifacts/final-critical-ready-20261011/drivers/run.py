"""One final-case publication and one serial simulation; no blind retries.

Case-specific evaluation inputs are external JSON. This driver never edits an
agent, generates policy, or invokes the model. It retains complete protected wire.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from builder.case_contract import CaseContract
from builder.freya_lifecycle import verify_simulation_call_binding
from builder.models import FoundationError
from integration.contest_evidence import ContestEvidence
from integration.pilot import iteration_matches
from orchestration.case_build import verify_case_snapshot
from orchestration.contest_build import (Budget,FreyaClient,digest,raw,write,
    publish,validate_workflow,verify_provider_graph,verify_source_snapshot)
from orchestration.workspace_lock import official_workspace_mutex,SimulationReservation

PHONE={'provider':'experimental-5','model':'experimental-5'}
JUDGE={'provider':'google','model':'gemini-3-flash-preview'}
MODELS={**{key:PHONE for key in ('assistantLlmOverride','routingLlmOverride','extractionLlmOverride')},
        **{key:JUDGE for key in ('personaLlmOverride','analysisLlmOverride')}}
TERMINAL=('completed','failed','paused','cancelled')

def require(value,code):
    if not value: raise FoundationError(code)

def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def idle(value,workspace):
    keys=[key for key in ('simulations','items') if key in value]
    require(keys and all(type(value[k]) is list for k in keys),'FINAL_IDLE_INVENTORY_MISSING')
    rows=value[keys[0]]
    require(len(keys)==1 or raw(value[keys[0]])==raw(value[keys[1]]),'FINAL_IDLE_INVENTORY_AMBIGUOUS')
    require(type(value.get('total')) is int and value['total']==len(rows) and len(rows)<100,
        'FINAL_IDLE_INVENTORY_INCOMPLETE')
    require(all(type(r) is dict and r.get('workspaceId')==workspace and r.get('status') in TERMINAL
                for r in rows),'FINAL_WORKSPACE_HAS_ACTIVE_OR_UNKNOWN_SIMULATION')

def loaded_generation(folder,case_folder,accept_partial=False):
    trace=read(folder/'end-to-end-trace.json')
    partial=trace.get('status')=='LOCAL_PARTIAL_CASE_WORKFLOW_EXPORTED'
    require(trace.get('status')=='LOCAL_CASE_WORKFLOW_EXPORTED' or
        (partial and accept_partial and trace.get('allow_partial') is True
         and trace.get('unresolved_source_or_implementation')),'FINAL_GENERATION_NOT_ADMITTED')
    verify_source_snapshot(trace)
    projection=read(folder/'case-projection.json')
    verify_case_snapshot(case_folder.resolve(),projection)
    graph=read(folder/'agent.workflow.json')
    require(digest((folder/'agent.workflow.json').read_bytes())==trace['workflow_sha256'],
        'FINAL_GENERATED_WORKFLOW_CHANGED')
    contract=CaseContract(read(folder/'source-derived-contract.json')['operations'],
        read(folder/'model-source-projection.json'),trace['mock_url'])
    checks=validate_workflow(graph,contract)
    return trace,graph,checks

def terminal_binding(simulation,iteration,run,call,publication,workspace,persona):
    require(simulation.get('status') in TERMINAL,'FINAL_SIMULATION_NOT_TERMINAL')
    require(iteration.get('simulationId')==simulation['id'] and iteration.get('status') in TERMINAL,
        'FINAL_ITERATION_NOT_TERMINAL_OR_FOREIGN')
    require(iteration_matches(iteration,publication['version_id'],publication['version_number'],MODELS),
        'FINAL_ITERATION_VERSION_MISMATCH')
    binding=verify_simulation_call_binding(run,call,workspace_id=workspace,
        workflow_id=publication['workflow_id'],simulation_id=simulation['id'],persona_id=persona)
    require(binding['lifecycle_ended'],'FINAL_CALL_NOT_ENDED')
    require(call.get('entityVersionId')==publication['version_id']
        and call.get('entityVersionNumber')==publication['version_number']
        and isinstance(call.get('messages'),list) and call['messages'],
        'FINAL_CALL_VERSION_OR_TRANSCRIPT_MISSING')
    return binding

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--generation',required=True,type=Path)
    parser.add_argument('--case-folder',required=True,type=Path)
    parser.add_argument('--scenario',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--accept-partial-for-test',action='store_true')
    args=parser.parse_args()
    generation,graph,checks=loaded_generation(args.generation,args.case_folder,args.accept_partial_for_test)
    scenario=read(args.scenario)
    require(set(scenario)=={'source','variables','persona'},'FINAL_SCENARIO_FIELDS_INVALID')
    require(type(scenario['variables']) is dict and scenario['variables'],'FINAL_SCENARIO_VARIABLES_REQUIRED')
    require(set(scenario['persona'])=={'name','prompt','successCriteria','tags'},'FINAL_PERSONA_FIELDS_INVALID')
    if not args.execute:
        print(json.dumps({'status':'LOCAL_PREFLIGHT_ONLY','checks':checks,'network_calls':0}))
        return 0
    output=args.out.resolve()
    require(output.is_relative_to(ROOT/'artifacts/final-builder') and not output.exists(),'FINAL_FRESH_OUTPUT_REQUIRED')
    output.mkdir()
    run_id=str(uuid.uuid4())
    budget=Budget(900)
    status={'kind':'final-native-one-test-1','run_id':run_id,'generation_run_id':generation['run_id'],
        'started_at':datetime.now(timezone.utc).isoformat(),
        'generation_trace_sha256':digest((args.generation/'end-to-end-trace.json').read_bytes()),
        'scenario_sha256':digest(args.scenario.read_bytes()),'generated_workflow_sha256':generation['workflow_sha256'],
        'status':'STARTED','models':MODELS,'simulation_posts':0,'live_success_verified':False,
        'official_hidden_tests_verified':False,'no_workflow_manual_edits':True,'no_retry':True}
    status['generation_partial']=generation.get('partial',False)
    status['unresolved_source_or_implementation']=generation.get('unresolved_source_or_implementation',[])
    write(output/'FINAL.json',status)
    actions=[]
    client=FreyaClient(budget,actions)
    client.audit_path=output/'provider-actions.json'
    evidence=ContestEvidence(ROOT/'integration/runs'/('final-native-'+run_id),secrets=(client.key,))
    client.evidence=evidence
    script_pin=digest(Path(__file__).read_bytes())
    status.update(protected_directory=str(evidence.directory.relative_to(ROOT)),
        driver_sha256=script_pin,wire_roles={})
    write(output/'FINAL.json',status)
    def intact():
        require(digest(Path(__file__).read_bytes())==script_pin,'FINAL_RUNNER_CHANGED')
        verify_source_snapshot(generation)
        verify_case_snapshot(args.case_folder.resolve(),read(args.generation/'case-projection.json'))
        require(digest((args.generation/'agent.workflow.json').read_bytes())==generation['workflow_sha256'],
            'FINAL_WORKFLOW_CHANGED')
        require(digest(args.scenario.read_bytes())==status['scenario_sha256'],'FINAL_SCENARIO_CHANGED')
        budget.remaining()
    try:
        workspace=client.kit.workspace_id()
        status['workspace_id']=workspace
        reservation=SimulationReservation(ROOT/'integration/state',workspace)
        with official_workspace_mutex(ROOT/'integration/state',workspace):
            reservation.require_available()
            idle(client.api('GET','/api/v2/simulations',workspaceId=workspace,limit=100,offset=0),workspace)
        intact()
        publication=publish(graph,client=client,state_path=ROOT/'integration/state/final-case-slots.json',
            slot='statement-final',directory=output,budget=budget)
        status['publication']=publication
        write(output/'FINAL.json',status)
        intact()
        personas=client.kit.create_personas([scenario['persona']])['personas']
        require(len(personas)==1,'FINAL_PERSONA_CREATE_COUNT_MISMATCH')
        persona=personas[0]['id']
        body={'agentId':publication['agent_id'],'agentVersionId':publication['version_id'],
            'name':'Final case controlled test '+run_id,'mode':'chat','selectedPersonaIds':[persona],
            'customerVariablesMap':{persona:scenario['variables']},'maxConcurrentCalls':1,
            'runsPerPersona':1,'maxRetries':0,'maxCallDurationSeconds':240,**MODELS}
        with official_workspace_mutex(ROOT/'integration/state',workspace):
            reservation.require_available()
            idle(client.api('GET','/api/v2/simulations',workspaceId=workspace,limit=100,offset=0),workspace)
            intact()
            reservation.reserve(body)
            pending_bytes=reservation.path.read_bytes()
            status['simulation_posts']=1
            write(output/'FINAL.json',status)
            result=client.api('POST','/api/v2/simulations',body)
            create_ticket=client.last_exchange
            status['wire_roles']['simulation_create']=create_ticket
            simulation=result.get('simulation',result)
            reservation.created(simulation)
            require(simulation.get('settings',{}).get('maxRetries')==0,
                'FINAL_SIMULATION_RETRY_SETTING_MISMATCH')
            simulation_id=simulation['id']
            status['simulation_id']=simulation_id
            write(output/'FINAL.json',status)
        while simulation.get('status') not in TERMINAL:
            time.sleep(min(10,budget.remaining()))
            intact()
            result=client.api('GET','/api/v2/simulations/'+simulation_id)
            simulation=result.get('simulation',result)
        terminal_ticket=client.last_exchange
        status['wire_roles']['simulation_terminal']=terminal_ticket
        require(simulation.get('id')==simulation_id,'FINAL_SIMULATION_ID_CHANGED')
        items=client.api('GET','/api/v2/simulations/'+simulation_id+'/iterations')['iterations']
        require(len(items)==1,'FINAL_SINGLE_ITERATION_REQUIRED')
        detail=client.kit.get_iteration(simulation_id,items[0]['id'])
        iteration_ticket=client.last_exchange
        status['wire_roles']['iteration']=iteration_ticket
        require(detail.get('iteration',{}).get('id')==items[0]['id']
            and detail['iteration'].get('workspaceId')==workspace,
            'FINAL_ITERATION_DETAIL_SCOPE_MISMATCH')
        require(detail.get('runsTotal')==1 and len(detail.get('runs',[]))==1,'FINAL_SINGLE_RUN_REQUIRED')
        run=detail['runs'][0]
        call=client.kit.get_call(run['callId'])
        call_ticket=client.last_exchange
        status['wire_roles']['call']=call_ticket
        status.update(call_id=run['callId'],persona_id=persona,iteration_id=detail['iteration']['id'])
        write(output/'FINAL.json',status)
        binding=terminal_binding(simulation,detail['iteration'],run,call,publication,workspace,persona)
        current=client.api('GET','/api/v2/workflow/'+publication['workflow_id'],workspaceId=workspace)['workflow']
        require(current['id']==publication['workflow_id'] and current['agentId']==publication['agent_id'],
            'FINAL_WORKFLOW_READBACK_SCOPE_MISMATCH')
        verify_provider_graph(read(output/'provider-export.json')['workflow'],current['workflow'])
        versions=client.api('GET','/api/v2/versions/workflow/'+publication['workflow_id'],workspaceId=workspace)['versions']
        require(any(v.get('id')==publication['version_id'] and v.get('isPublished') is True
                    and v.get('versionNumber')==publication['version_number'] for v in versions),
            'FINAL_PUBLISHED_VERSION_CHANGED')
        intact()
        require(not evidence.pending and not evidence.failed and not evidence.recorder.failed,'FINAL_WIRE_INCOMPLETE')
        # Verify the actual dispatched body, and retain all complete exchanges.
        request=evidence.cache[create_ticket][0]
        require(json.loads(base64.b64decode(request['body_base64']))==body,'FINAL_DISPATCH_BODY_CHANGED')
        status.update(status='COMPLETED_OBSERVATION',call_id=call['id'],persona_id=persona,
            iteration_id=detail['iteration']['id'],binding=binding,judge_outcome=run.get('outcome'),
            call_ended_reason=call.get('endedReason'),duration=call.get('duration'),
            protected_directory=str(evidence.directory.relative_to(ROOT)),
            wire_roles={'simulation_create':create_ticket,'simulation_terminal':terminal_ticket,
                'iteration':iteration_ticket,'call':call_ticket},protected_exchanges=len(evidence.exchanges))
        # No fabricated bank ledger; native call/tool interpretation is separate.
        status['bank_write_verified']=False
        with official_workspace_mutex(ROOT/'integration/state',workspace):
            idle(client.api('GET','/api/v2/simulations',workspaceId=workspace,limit=100,offset=0),workspace)
            reservation.observe_terminal(simulation)
            reservation._owned_terminal()
            require(reservation.path.read_bytes()==pending_bytes,'FINAL_PENDING_OWNER_CHANGED')
            require(not evidence.pending and not evidence.failed,'FINAL_LAST_WIRE_INCOMPLETE')
            manifest={p.relative_to(evidence.directory).as_posix():digest(p.read_bytes())
                      for p in evidence.directory.rglob('*') if p.is_file()}
            write(output/'protected-wire-manifest.json',manifest)
            os.rename(reservation.path,output/'consumed-terminal-reservation.json')
            status['own_terminal_reservation_archived']=True
        write(output/'FINAL.json',status)
        print(json.dumps({key:status[key] for key in ('status','judge_outcome','call_ended_reason','simulation_id')}))
        return 0
    except Exception as error:
        status.update(status='BLOCKED_NO_RETRY',diagnostic=str(error) if isinstance(error,FoundationError)
            else 'FINAL_DRIVER_'+type(error).__name__,elapsed_seconds=round(time.monotonic()-budget.started,3))
        write(output/'FINAL.json',status)
        print(json.dumps({'status':status['status'],'diagnostic':status['diagnostic']}))
        return 1
    finally:
        status.update(ended_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(time.monotonic()-budget.started,3))
        write(output/'FINAL.json',status)

if __name__=='__main__': raise SystemExit(main())
