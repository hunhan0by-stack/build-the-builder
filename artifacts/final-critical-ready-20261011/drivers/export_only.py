"""Publish and officially export one admitted recorded-response continuation."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from run import loaded_generation
from integration.contest_evidence import ContestEvidence
from orchestration.contest_build import Budget, FreyaClient, publish, write, digest, verify_source_snapshot
from builder.models import FoundationError

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--generation',required=True,type=Path)
    parser.add_argument('--case-folder',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--source-commit',required=True)
    args=parser.parse_args()
    generation,graph,checks=loaded_generation(args.generation,args.case_folder,True)
    output=args.out.resolve()
    if not output.is_relative_to(ROOT/'artifacts/final-builder') or output.exists():
        raise FoundationError('FINAL_EXPORT_FRESH_OUTPUT_REQUIRED')
    output.mkdir()
    receipt={'kind':'final-official-export-only','status':'STARTED','started_at':datetime.now(timezone.utc).isoformat(),
        'generation_run_id':generation['run_id'],'generation_trace_sha256':digest((args.generation/'end-to-end-trace.json').read_bytes()),
        'generated_workflow_sha256':generation['workflow_sha256'],'source_commit':args.source_commit,
        'driver_sha256':digest(Path(__file__).read_bytes()),'static_checks':checks,
        'new_model_calls':0,'simulation_calls':0,'bank_calls':0,'email_sent':False,
        'live_behavior_verified':False,'physical_ivr_transfer_verified':False,
        'generation_partial':generation.get('partial',False),
        'unresolved_source_or_implementation':generation.get('unresolved_source_or_implementation',[])}
    write(output/'EXPORT-RECEIPT.json',receipt)
    budget=Budget(300)
    actions=[]
    try:
        client=FreyaClient(budget,actions)
        client.audit_path=output/'provider-actions.json'
        evidence=ContestEvidence(ROOT/'integration/runs'/('final-export-'+generation['run_id']),secrets=(client.key,))
        client.evidence=evidence
        receipt['protected_directory']=str(evidence.directory.relative_to(ROOT))
        write(output/'EXPORT-RECEIPT.json',receipt)
        publication=publish(graph,client=client,state_path=ROOT/'integration/state/final-case-slots.json',
            slot='statement-final',directory=output,budget=budget)
        verify_source_snapshot(generation)
        if digest((args.generation/'agent.workflow.json').read_bytes())!=generation['workflow_sha256']:
            raise FoundationError('FINAL_EXPORT_GENERATION_CHANGED')
        if evidence.pending or evidence.failed or evidence.recorder.failed:
            raise FoundationError('FINAL_EXPORT_PROTECTED_WIRE_INCOMPLETE')
        manifest={p.relative_to(evidence.directory).as_posix():digest(p.read_bytes())
                  for p in evidence.directory.rglob('*') if p.is_file()}
        write(output/'protected-wire-manifest.json',manifest)
        receipt.update(status='OFFICIAL_EXPORT_VERIFIED',publication=publication,
            protected_exchanges=len(evidence.exchanges))
        return_code=0
    except Exception as error:
        receipt.update(status='BLOCKED_NO_RETRY',diagnostic=str(error) if isinstance(error,FoundationError)
            else 'FINAL_EXPORT_'+type(error).__name__)
        return_code=1
    receipt['ended_at']=datetime.now(timezone.utc).isoformat()
    write(output/'EXPORT-RECEIPT.json',receipt)
    print(json.dumps({key:receipt.get(key) for key in ('status','diagnostic','publication')}))
    return return_code

if __name__=='__main__':raise SystemExit(main())
