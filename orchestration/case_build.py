"""One-command case-folder + mock URL builder; export only, never implicit live calls."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import time
import uuid

from builder.case_contract import CaseContract, mock_base
from builder.hook_validation import validate_posthook_retention
from builder.case_model import CASE_RESPONSE, assemble_case, case_prompt
from builder.case_reader import CaseReaderError, read_case_folder
from builder.models import FoundationError
from builder.model_workflow import CodexWorkflowProvider
from builder.case_replay import RecordedCaseResponse
from .contest_build import Budget, raw, write, ROOT, KIT

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def verify_case_snapshot(folder, projection):
    expected={row['path']:row['sha256'] for row in projection['manifest']}
    observed={}
    for path in folder.rglob('*'):
        if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
            raise FoundationError('CASE_SOURCE_CHANGED')
        if path.is_file():
            if not path.resolve().is_relative_to(folder):
                raise FoundationError('CASE_SOURCE_CHANGED')
            observed[path.relative_to(folder).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    if observed!=expected:
        raise FoundationError('CASE_SOURCE_CHANGED')


def select_model_sources(projection, reference_only):
    """Explicit input roles keep mock/test datasets out of policy generation."""
    paths=[]
    for value in reference_only or []:
        path=PurePosixPath(value)
        if (not value or '\\' in value or ':' in value or path.is_absolute()
                or any(part in ('.','..') for part in value.split('/'))):
            raise FoundationError('CASE_REFERENCE_PATH_INVALID')
        prefix=path.as_posix()
        matches=[row['path'] for row in projection['manifest']
                 if row['path']==prefix or row['path'].startswith(prefix+'/')]
        if not matches:
            raise FoundationError('CASE_REFERENCE_PATH_NOT_FOUND')
        if any(PurePosixPath(name).name.upper() in ('BRIEF.MD','README.MD') for name in matches):
            raise FoundationError('CASE_PRIMARY_DOCUMENT_CANNOT_BE_REFERENCE_ONLY')
        paths.extend(matches)
    selected=copy.deepcopy(projection)
    selected.pop('text',None)
    selected['reference_only_paths']=sorted(set(paths))
    for doc in selected['documents']:
        if doc['path'] in paths:
            doc['segments']=[]
            doc['model_role']='reference_only_not_policy_input'
    selected['model_projection_sha256']=hashlib.sha256(raw(selected)).hexdigest()
    return selected


def run(case_folder, mock_url, *, output_directory=None, budget_seconds=1800,
        max_model_attempts=3, model_provider=None, reference_only=None,
        allow_partial=False, repair_from=None, assemble_from=None, assemble_attempt=1):
    if assemble_from is not None:
        if repair_from is not None or model_provider is not None:
            raise FoundationError('CASE_ASSEMBLY_MODE_CONFLICT')
        max_model_attempts=1
    if type(max_model_attempts) is not int or not 1<=max_model_attempts<=3:
        raise FoundationError('CASE_MODEL_ATTEMPTS_MUST_BE_1_TO_3')
    mock_url=mock_base(mock_url)
    folder=Path(case_folder).resolve()
    identifier=uuid.uuid4().hex
    output=Path(output_directory or ROOT/'artifacts/final-builder'/('case-'+identifier)).resolve()
    if output==folder or output.is_relative_to(folder):
        raise FoundationError('CASE_OUTPUT_MUST_BE_OUTSIDE_SOURCE')
    output.mkdir(parents=True,exist_ok=False)
    budget=Budget(budget_seconds)
    trace={'kind':'case-folder-build-1','run_id':identifier,'status':'STARTED',
        'mock_url':mock_url,'model_attempt_limit':max_model_attempts,'model_calls':0,
        'attempts':[],'publication_calls':0,'simulation_calls':0,'started_at':utc_now(),
        'semantic_policy_verified':False,'live_verified':False,
        'allow_partial':allow_partial,
        'budget_seconds':budget_seconds,'budget_is_internal_limit':True}
    def save():
        trace['elapsed_seconds']=round(time.monotonic()-budget.started,3)
        if trace['status'] in ('REFUSED','LOCAL_CASE_WORKFLOW_EXPORTED','LOCAL_PARTIAL_CASE_WORKFLOW_EXPORTED'):
            trace['ended_at']=utc_now()
        write(output/'end-to-end-trace.json',trace)
    try:
        projection=read_case_folder(folder,limits={'timeout_seconds':min(60,int(budget.remaining()))})
        write(output/'case-projection.json',projection)
        trace.update(source_manifest=projection['manifest'],manifest_sha256=projection['manifest_sha256'],
            projection_sha256=projection['projection_sha256'],requires_review=projection['requires_review'],
            extraction_diagnostics=projection['diagnostics'])
        if projection.get('complete') is not True:
            raise FoundationError('CASE_EXTRACTION_INCOMPLETE')
        verify_case_snapshot(folder,projection)
        model_projection=select_model_sources(projection,reference_only)
        write(output/'model-source-projection.json',model_projection)
        trace.update(reference_only_paths=model_projection['reference_only_paths'],
            model_projection_sha256=model_projection['model_projection_sha256'])
        references={name:(KIT/name).read_text(encoding='utf-8') for name in
            ('docs/workflow-format.md','docs/tools-and-code.md')}
        pinned={path:hashlib.sha256(path.read_bytes()).hexdigest()
            for parent in ('builder','orchestration','compiler') for path in (ROOT/parent).rglob('*.py')}
        pinned.update({KIT/name:hashlib.sha256((KIT/name).read_bytes()).hexdigest() for name in references})
        trace['runtime_sha256']={path.relative_to(ROOT).as_posix():digest for path,digest in pinned.items()}
        def unchanged():
            verify_case_snapshot(folder,projection)
            if any(not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest for path,digest in pinned.items()):
                raise FoundationError('CASE_RUNTIME_CHANGED')
            budget.remaining()
        provider=(RecordedCaseResponse(assemble_from,assemble_attempt,model_projection,mock_url,references=references)
                  if assemble_from is not None else model_provider or CodexWorkflowProvider())
        replay=bool(getattr(provider,'is_recorded_response',False))
        if replay:
            trace['assembly_origin']=provider.origin
            trace['assembly_mode']='deterministic_recorded_response_continuation'
        feedback=None
        if repair_from is not None:
            previous=Path(repair_from).resolve()
            prior_trace_bytes=(previous/'end-to-end-trace.json').read_bytes()
            prior=json.loads(prior_trace_bytes)
            if (prior.get('manifest_sha256')!=projection['manifest_sha256']
                    or prior.get('mock_url')!=mock_url or not prior.get('attempts')):
                raise FoundationError('CASE_REPAIR_SOURCE_MISMATCH')
            if prior.get('status')=='STARTED':
                raise FoundationError('CASE_REPAIR_RUN_STILL_ACTIVE')
            if prior.get('reference_only_paths')!=trace['reference_only_paths']:
                raise FoundationError('CASE_REPAIR_SOURCE_MISMATCH')
            candidates=[entry for entry in prior['attempts'] if entry.get('response_sha256')]
            if not candidates:
                raise FoundationError('CASE_REPAIR_RESPONSE_UNAVAILABLE')
            last=candidates[-1]
            number=last.get('attempt')
            if type(number) is not int or not 1<=number<=3:
                raise FoundationError('CASE_REPAIR_ATTEMPT_INVALID')
            prior_response=(previous/('model-attempt-'+str(number))/'model-response.json').read_bytes()
            prior_digest=hashlib.sha256(prior_response).hexdigest()
            if prior_digest!=last.get('response_sha256'):
                raise FoundationError('CASE_REPAIR_RESPONSE_CHANGED')
            previous_response=json.loads(prior_response)
            diagnostics=[]
            for operation in previous_response.get('operations',[]):
                try:
                    CaseContract([operation],model_projection,mock_url)
                except FoundationError as error:
                    diagnostics.append(str(error))
            for block in previous_response.get('code_blocks',[]):
                try:
                    validate_posthook_retention(block['code'])
                except FoundationError as error:
                    diagnostics.append(str(error)+':'+block['id'])
            feedback={'diagnostic':last.get('diagnostic'),
                'current_source_diagnostics':diagnostics,
                'previous_generated_response':previous_response,
                'previous_response_sha256':prior_digest,'not_retested':True,
                'authority':'Historical generated draft; current source projection governs.'}
            trace['repair_origin']={'run_id':prior['run_id'],
                'attempt':number,'prior_status':prior['status'],
                'trace_sha256':hashlib.sha256(prior_trace_bytes).hexdigest(),
                'response_sha256':prior_digest}
        for attempt in range(1,max_model_attempts+1):
            unchanged()
            directory=output/('model-attempt-'+str(attempt))
            directory.mkdir()
            record={'attempt':attempt,'status':'STARTED','started_at':utc_now(),
                'model_requested':getattr(provider,'model',None),
                'reasoning_effort_requested':getattr(provider,'reasoning_effort',None),
                'purpose':'recorded_response_reassembly' if replay else
                    ('source_to_workflow' if attempt==1 else 'repair_refused_draft')}
            trace['attempts'].append(record)
            if not replay:
                trace['model_calls']+=1
            save()
            response=None
            try:
                response,raw_response,_=provider.structured_response(
                    prompt=case_prompt(model_projection,mock_url,references,feedback,
                        allow_partial=allow_partial),
                    schema_value=CASE_RESPONSE,directory=directory,remaining_seconds=budget.remaining())
                record['response_sha256']=hashlib.sha256(raw_response).hexdigest()
                trace['unresolved_source_or_implementation']=response.get('unresolved',[])
                unchanged()
                workflow,checks=assemble_case(response,model_projection,mock_url,directory,
                    raw_response_sha256=record['response_sha256'],allow_partial=allow_partial,
                    references=references)
                unchanged()
                write(output/'source-derived-contract.json',{'operations':response['operations'],
                    'mock_url':mock_url,'manifest_sha256':projection['manifest_sha256'],
                    'citation_locations_verified':True,'schema_semantics_independently_verified':False})
                write(output/'agent.workflow.json',workflow)
                partial=bool(response['unresolved'])
                record.update(status='STATIC_VALID_WITH_OPEN_ISSUES' if partial else 'STATIC_VALID',
                    checks=checks,ended_at=utc_now())
                trace.update(status='LOCAL_PARTIAL_CASE_WORKFLOW_EXPORTED' if partial else 'LOCAL_CASE_WORKFLOW_EXPORTED',
                    partial=partial,workflow_file='agent.workflow.json',
                    workflow_sha256=hashlib.sha256((output/'agent.workflow.json').read_bytes()).hexdigest(),
                    coverage=response['coverage'],limitations=response['limitations'],
                    verification_scope='Source locations, schema/wire structure and literal API routes; generated policy and native behavior need independent/live verification.')
                save()
                return trace
            except (FoundationError,ValueError,TypeError,KeyError,RecursionError) as error:
                # No arbitrary subprocess stderr or host secrets enter the trace.
                diagnostic=str(error) if isinstance(error,FoundationError) else 'CASE_RESPONSE_INVALID_'+type(error).__name__
                record.update(status='REFUSED',diagnostic=diagnostic,ended_at=utc_now())
                feedback={'diagnostic':diagnostic,'previous_generated_response':response,
                    'previous_response_sha256':record.get('response_sha256'),'not_retested':True}
                save()
                if diagnostic in ('CASE_SOURCE_CHANGED','CASE_RUNTIME_CHANGED'):
                    raise
        trace['attempt_limit_reached']=True
        raise FoundationError('CASE_MODEL_ATTEMPT_LIMIT_REACHED')
    except (FoundationError,CaseReaderError,ValueError,OSError) as error:
        trace.update(status='REFUSED',diagnostic=str(error) if isinstance(error,(FoundationError,CaseReaderError)) else 'CASE_IO_OR_INPUT_ERROR')
        save()
        return trace


def main(argv=None):
    parser=argparse.ArgumentParser(description='Build from a case folder and your HTTPS mock URL')
    parser.add_argument('case_folder')
    parser.add_argument('mock_url')
    parser.add_argument('--output-directory','--output','--out',dest='output_directory')
    parser.add_argument('--budget-seconds','--deadline-seconds',type=int,default=1800)
    parser.add_argument('--max-model-attempts',type=int,choices=(1,2,3),default=3)
    parser.add_argument('--reference-only',action='append',default=[],
        help='Relative file/folder retained in provenance but omitted from model policy input; repeatable')
    parser.add_argument('--allow-partial',action='store_true',
        help='Export structurally valid partial drafts while retaining all unresolved issues; no semantic/live approval')
    parser.add_argument('--repair-from',type=Path,
        help='Prior build directory with a hash-bound model response from the same source manifest and mock')
    parser.add_argument('--assemble-from',type=Path,
        help='Reassemble an unchanged authentic recorded response; makes zero new model calls')
    parser.add_argument('--assemble-attempt',type=int,choices=(1,2,3),default=1)
    args=parser.parse_args(argv)
    try:
        result=run(**vars(args))
    except (FoundationError,ValueError,OSError) as error:
        print(json.dumps({'status':'REFUSED','diagnostic':str(error) if isinstance(error,FoundationError) else 'CASE_INPUT_INVALID'}))
        return 1
    print(json.dumps({key:result.get(key) for key in ('status','run_id','model_calls','diagnostic')},ensure_ascii=False))
    return 0 if result['status'] in ('LOCAL_CASE_WORKFLOW_EXPORTED','LOCAL_PARTIAL_CASE_WORKFLOW_EXPORTED') else 1


if __name__=='__main__':
    raise SystemExit(main())
