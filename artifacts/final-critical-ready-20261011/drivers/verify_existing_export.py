"""Read-only completion of an already-created workflow export; no remote mutations."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from run import loaded_generation
from integration.contest_evidence import ContestEvidence
from orchestration.contest_build import Budget,FreyaClient,digest,write,verify_provider_graph,verify_source_snapshot
from orchestration.workspace_lock import official_workspace_mutex
from builder.models import FoundationError

def require(value,code):
    if not value:raise FoundationError(code)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--generation',required=True,type=Path)
    parser.add_argument('--case-folder',required=True,type=Path)
    parser.add_argument('--previous-export',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--source-commit',required=True)
    args=parser.parse_args()
    trace,graph,checks=loaded_generation(args.generation,args.case_folder,True)
    previous=json.loads((args.previous_export/'provider-export.json').read_bytes())
    previous_receipt=json.loads((args.previous_export/'EXPORT-RECEIPT.json').read_bytes())
    require(trace['workflow_sha256']==previous_receipt['generated_workflow_sha256'],'EXPORT_CONTINUATION_GRAPH_CHANGED')
    verify_provider_graph(graph,previous['workflow'])
    output=args.out.resolve()
    require(output.is_relative_to(ROOT/'artifacts/final-builder') and not output.exists(),'EXPORT_CONTINUATION_FRESH_OUTPUT_REQUIRED')
    output.mkdir()
    status={'kind':'existing-official-export-readback','status':'STARTED','started_at':datetime.now(timezone.utc).isoformat(),
        'source_commit':args.source_commit,'generation_run_id':trace['run_id'],
        'generation_trace_sha256':digest((args.generation/'end-to-end-trace.json').read_bytes()),
        'workflow_sha256':trace['workflow_sha256'],'previous_export_sha256':digest((args.previous_export/'provider-export.json').read_bytes()),
        'previous_receipt_sha256':digest((args.previous_export/'EXPORT-RECEIPT.json').read_bytes()),
        'new_model_calls':0,'new_remote_mutations':0,'simulations':0,'bank_calls':0,'live_behavior_verified':False,
        'physical_ivr_transfer_verified':False,'partial_generation':trace.get('partial',False),
        'driver_sha256':digest(Path(__file__).read_bytes())}
    write(output/'EXPORT-RECEIPT.json',status)
    try:
        actions=[];client=FreyaClient(Budget(180),actions)
        client.audit_path=output/'provider-actions.json'
        evidence=ContestEvidence(ROOT/'integration/runs'/('export-readback-'+trace['run_id']),secrets=(client.key,))
        client.evidence=evidence
        status['protected_directory']=str(evidence.directory.relative_to(ROOT))
        workspace=client.kit.workspace_id()
        path=ROOT/'integration/state/final-case-slots.json'
        with official_workspace_mutex(ROOT/'integration/state',workspace):
            state=json.loads(path.read_bytes());slot=state['statement-final']
            require(slot['status']=='MUTATION_PENDING' and all(slot[k]==previous[k] for k in ('workflow_id','agent_id')),'EXPORT_CONTINUATION_OWNER_MISMATCH')
            client.kit.export_workflow(previous['workflow_id'],str(output/'provider-export.json'))
            observed=json.loads((output/'provider-export.json').read_bytes())
            require(all(observed[k]==previous[k] for k in ('workflow_id','agent_id')),'EXPORT_CONTINUATION_ID_CHANGED')
            verify_provider_graph(graph,observed['workflow'])
            require(all(observed['workflow'].get('agent_config',{}).get('llmConfig',{}).get(k)==v
                        for k,v in client.kit.AGENT_LLM.items()),'EXPORT_CONTINUATION_MODEL_MISMATCH')
            versions=client.api('GET','/api/v2/versions/workflow/'+previous['workflow_id'],workspaceId=workspace)['versions']
            published=[row for row in versions if row.get('isPublished') is True]
            require(len(published)==1 and published[0]['versionNumber']==max(v['versionNumber'] for v in versions),'EXPORT_CONTINUATION_PUBLISHED_VERSION_MISMATCH')
            version=published[0]
            verify_source_snapshot(trace)
            require(not evidence.pending and not evidence.failed and not evidence.recorder.failed,'EXPORT_CONTINUATION_WIRE_INCOMPLETE')
            require(all(action['method']=='GET' for action in actions),'EXPORT_CONTINUATION_UNEXPECTED_MUTATION')
            publication={k:observed[k] for k in ('workflow_id','agent_id')}
            publication.update(version_id=version['id'],version_number=version['versionNumber'],
                export_sha256=digest((output/'provider-export.json').read_bytes()),status='EXPORTED')
            state['statement-final']=publication
            write(path,state)
            write(output/'protected-wire-manifest.json',{p.relative_to(evidence.directory).as_posix():digest(p.read_bytes())
                for p in evidence.directory.rglob('*') if p.is_file()})
            status.update(status='OFFICIAL_EXPORT_VERIFIED',publication=publication,
                protected_exchanges=len(evidence.exchanges),metadata_normalization='Only added empty description on unchanged named end_call function; all graph fields recursively compared.')
        code=0
    except Exception as error:
        status.update(status='BLOCKED_NO_RETRY',diagnostic=str(error) if isinstance(error,FoundationError) else 'EXPORT_CONTINUATION_'+type(error).__name__)
        code=1
    status['ended_at']=datetime.now(timezone.utc).isoformat()
    write(output/'EXPORT-RECEIPT.json',status)
    print(json.dumps({k:status.get(k) for k in ('status','diagnostic','publication')}))
    return code

if __name__=='__main__':raise SystemExit(main())
