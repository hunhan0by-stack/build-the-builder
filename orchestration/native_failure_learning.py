"""SHA-bound native failure feedback; diagnosis never grants repair authority.

The unchanged failure_learning recorder remains the raw documentary layer.
Only two bounded safe-fact grammars are recognized here. Missing run/source/
export/manifest joins remain UNVERIFIED_FAILURE; no workflow is edited or run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integration import failure_learning as legacy

FORMAT = 'native-real-failure-descriptor-1'
KINDS = {'negative_confidence_rejection', 'spoken_cardinal_dob_parser_gap'}
HASH = re.compile(r'[0-9a-f]{64}')
LABEL = re.compile(r'[A-Za-z0-9_-]{1,64}')
NO_AUTHORITY = {'automatic_workflow_repair_allowed': False,
    'automatic_retry_allowed': False, 'bank_write_authority': False,
    'live_execution_authority': False, 'official_success_established': False}

class NativeFailureLearningError(ValueError):
    pass

def require(condition, code):
    if not condition:
        raise NativeFailureLearningError(code)

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def safe_read(root, ref, *, optional=False, as_json=False):
    if ref is None:
        require(optional, 'MISSING_REQUIRED_REFERENCE')
        return None, None
    require(isinstance(ref, dict) and set(ref) <= {'path','sha256','manifest_entry'}
        and set(ref) >= {'path','sha256'}, 'REFERENCE_SHAPE_INVALID')
    require(isinstance(ref['path'], str) and isinstance(ref['sha256'], str)
        and HASH.fullmatch(ref['sha256']), 'REFERENCE_PIN_INVALID')
    try:
        pin, value = legacy._read(root, ref['path'], json_required=as_json)
    except (legacy.FailureLearningError, OSError) as exc:
        raise NativeFailureLearningError('UNSAFE_OR_UNAVAILABLE_REFERENCE') from exc
    require(pin['sha256'] == ref['sha256'], 'REFERENCE_HASH_MISMATCH')
    return pin, value

def reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            require(isinstance(key,str) and not re.search(r'(?i)^(authorization|api[_-]?key|password|access[_-]?token|secret|credentials)$',key),
                    'PRIVATE_DESCRIPTOR_FIELD')
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value,str):
        require(not re.search(r'(?i)\bfak_[A-Za-z0-9_-]+|\bbearer\s+\S+',value), 'PRIVATE_DESCRIPTOR_VALUE')

def validate_descriptor(value):
    require(isinstance(value,dict) and set(value) == {'schema_version','failure_id','failure_kind',
        'case_id','run_id','evidence_level','snapshot','bindings','evidence','policy','history','hypothesis'},
        'DESCRIPTOR_SHAPE_INVALID')
    reject_secrets(value)
    require(isinstance(value['failure_kind'],str) and value['schema_version'] == FORMAT and value['failure_kind'] in KINDS
        and value['case_id'] == 'PUB-11', 'UNSUPPORTED_DESCRIPTOR_GRAMMAR')
    require(isinstance(value['failure_id'],str) and LABEL.fullmatch(value['failure_id']), 'FAILURE_ID_INVALID')
    try:
        require(str(uuid.UUID(value['run_id'])) == value['run_id'], 'RUN_ID_INVALID')
    except (ValueError, TypeError, AttributeError) as exc:
        raise NativeFailureLearningError('RUN_ID_INVALID') from exc
    require(isinstance(value['evidence_level'],str) and value['evidence_level'] in {'independent_audit','owner_reported','provisional'}, 'EVIDENCE_LEVEL_INVALID')
    snapshot = value['snapshot']
    require(isinstance(snapshot,dict) and set(snapshot) <= {'test_kind','case_id','run_id','status','mode',
        'judge_outcome','bank_calls','unknown_mutations','credits_before','credits_after'}, 'SNAPSHOT_FIELDS_INVALID')
    require(snapshot.get('run_id') == value['run_id'] and snapshot.get('case_id') == 'PUB-11'
        and snapshot.get('test_kind') == 'PUB-11', 'SNAPSHOT_RUN_MISMATCH')
    require(isinstance(snapshot.get('status'),str) and snapshot['status'] in {'fail','blocked','blocked_or_incomplete'}
        and isinstance(snapshot.get('mode'),str) and snapshot['mode'] in {'chat','live'}, 'SNAPSHOT_STATUS_INVALID')
    judge = snapshot.get('judge_outcome')
    require(judge is None or isinstance(judge,str) and judge in {'pass','fail','error','inconclusive'}, 'JUDGE_OUTCOME_INVALID')
    for name in ('bank_calls','unknown_mutations'):
        item = snapshot.get(name)
        require(item is None or type(item) is int and 0 <= item <= 1_000_000, 'SNAPSHOT_COUNT_INVALID')
    for name in ('credits_before','credits_after'):
        if name in snapshot:
            credit = snapshot[name]
            require(isinstance(credit,dict) and set(credit) <= {'creditsLeft','creditUsdRate'}
                and 'creditsLeft' in credit and all(type(v) in (int,float) and math.isfinite(v) and v >= 0
                for v in credit.values()), 'CREDIT_SAFE_FACT_INVALID')
    require(isinstance(value['bindings'],dict) and set(value['bindings']) == {'source','export','manifest'}, 'BINDINGS_SHAPE_INVALID')
    require(isinstance(value['evidence'],dict) and set(value['evidence']) == {'primary','source_join'}, 'EVIDENCE_SHAPE_INVALID')
    require(isinstance(value['policy'],dict) and set(value['policy']) == {'source','required_policy_unchanged'}
        and value['policy']['required_policy_unchanged'] is True, 'POLICY_CHANGE_FORBIDDEN')
    require(isinstance(value['history'],dict) and set(value['history']) == {'old_FAIL_preserved',
        'manual_compiler_fix','automated_generated_repair'}, 'HISTORY_SHAPE_INVALID')
    require(value['history']['old_FAIL_preserved'] is True
        and value['history']['automated_generated_repair'] == 'not_demonstrated'
        and isinstance(value['history']['manual_compiler_fix'],str)
        and value['history']['manual_compiler_fix'] in {'none_by_this_bridge','external_owner_pending_or_unverified'},
        'REPAIR_AUTHORITY_PROMOTED')
    hypothesis = value['hypothesis']
    require(isinstance(hypothesis,dict) and set(hypothesis) == {'code','authority'}
        and hypothesis['code'] == value['failure_kind']
        and isinstance(hypothesis['authority'],str)
        and hypothesis['authority'] in {'independent_safe_facts','owner_commentary_unverified'}, 'HYPOTHESIS_INVALID')

def _selected_manifest_matches(manifest, refs):
    if not isinstance(manifest,dict) or not isinstance(manifest.get('files'),dict):
        return False
    for ref in refs:
        if ref is None or not isinstance(ref.get('manifest_entry'),str):
            return False
        row = manifest['files'].get(ref['manifest_entry'])
        if not isinstance(row,dict) or row.get('sha256') != ref['sha256']:
            return False
    return True

def normalized_diagnosis(value, primary, source_join, manifest):
    reasons = []
    bindings = value['bindings']
    if value['evidence_level'] != 'independent_audit':
        reasons.append('independent_run_audit_absent')
    if any(bindings[k] is None for k in ('source','export','manifest')) or source_join is None:
        reasons.append('source_export_manifest_or_historical_join_missing')
    if not isinstance(primary,dict) or primary.get('operation_id',primary.get('run_id')) != value['run_id']:
        reasons.append('audited_run_id_not_bound')
    if not _selected_manifest_matches(manifest,[value['evidence']['primary'],value['evidence']['source_join'],bindings['source']]):
        reasons.append('selected_audit_manifest_entries_not_bound')
    if not reasons:
        historical = source_join.get('historical_source_copies_verified',[]) if isinstance(source_join,dict) else []
        source_ok = any(isinstance(row,dict) and row.get('path') == 'builder/native_multi_product.py'
            and row.get('actual_sha256') == bindings['source']['sha256'] for row in historical)
        # The retained DOB audit has a separate, explicitly versioned public
        # receipt shape. Read its actual fields; do not rewrite it as a new audit.
        dob_audit = (value['failure_kind'] == 'spoken_cardinal_dob_parser_gap'
            and primary.get('verdict') == 'INDEPENDENT_RETAINED_WIRE_AUDIT_VERIFIED_REAL_DOB_FAIL'
            and source_join.get('status') == 'SEALED_INDEPENDENT_AUDIT_PASS_OF_ACTUAL_TEST_FAIL')
        export_pin = primary.get('export_sha256')
        report_pin = source_join.get('measured_audit_sha256')
        if dob_audit:
            source = primary.get('source',{})
            original = source_join.get('historical_actual_source_sha256',{}).get('builder/native_multi_product.py',{})
            source_ok = (original.get('original_sha256') == bindings['source']['sha256']
                and source.get('actual_native_sha256') == bindings['source']['sha256']
                and source.get('actual_published_parser_AST_matches_historical_source') is True)
            export_pin = source.get('actual_export_sha256')
            report_pin = source_join.get('measured_report_sha256')
        if (not source_ok or export_pin != bindings['export']['sha256']
                or report_pin != value['evidence']['primary']['sha256']):
            reasons.append('historical_graph_source_or_export_join_not_verified')
    code, confidence = 'UNVERIFIED_FAILURE', 'unverified'
    facts = {}
    if not reasons and value['failure_kind'] == 'negative_confidence_rejection':
        observed = primary.get('actual_failure',{})
        scenario = primary.get('standalone_scenario_evidence',{})
        if not isinstance(observed,dict) or not isinstance(scenario,dict):
            reasons.append('negative_safe_facts_missing')
        else:
            threshold = observed.get('published_minConfidence')
            discarded = observed.get('low_confidence_rejections')
            require(type(threshold) in (int,float) and math.isfinite(threshold) and 0 <= threshold <= 1,
                    'CONFIDENCE_THRESHOLD_INVALID')
            require(isinstance(discarded,list) and 1 <= len(discarded) <= 64, 'CONFIDENCE_REJECTIONS_INVALID')
            confidences = []
            for row in discarded:
                require(isinstance(row,dict) and type(row.get('index')) is int and row['index'] >= 0
                    and type(row.get('confidence')) in (int,float) and math.isfinite(row['confidence'])
                    and 0 <= row['confidence'] < threshold and row.get('reason') == 'low_confidence',
                    'CONFIDENCE_FACT_INVALID')
                confidences.append(row['confidence'])
            for key in ('correct_false_JSON_rejected','no_early_draft_consent_write'):
                require(type(observed.get(key)) is bool, 'CONFIDENCE_BOOLEAN_INVALID')
            if (observed['correct_false_JSON_rejected'] is True and observed['no_early_draft_consent_write'] is True
                and scenario.get('valid') is False and 'one_standalone_negative_reply_required' in scenario.get('failed',[])):
                code, confidence = 'NEGATIVE_CONFIDENCE_REJECTION', 'high_safe_audit'
                facts = {'published_minConfidence':threshold,'rejected_confidences':confidences,
                    'official_iteration_PASS':primary.get('official_iteration_PASS') is True,
                    'official_call_criteria_PASS':primary.get('official_call_criteria_PASS') is True,
                    'official_ledger_PASS':primary.get('unchanged_official_kit_ledger_PASS') is True,
                    'standalone_negative_variation_PASS':False,'no_early_bank_write':True}
            else:
                reasons.append('specific_negative_failure_not_established')
    elif not reasons:
        observed = primary.get('spoken_cardinal_dob_safe_facts')
        if observed is None and dob_audit:
            failure, results = primary.get('failure',{}), primary.get('real_results',{})
            turns, questions = failure.get('accepted_DOB_turns'), failure.get('DOB_question_count')
            require(type(turns) is int and 1 <= turns <= 1000
                and type(questions) is int and questions == turns, 'DOB_REPETITION_COUNT_INVALID')
            require(type(failure.get('first_accepted_DOB_index')) is int
                and type(failure.get('first_accepted_DOB_confidence')) in (int,float)
                and math.isfinite(failure['first_accepted_DOB_confidence'])
                and type(failure.get('published_main_extractor_min_confidence')) in (int,float)
                and math.isfinite(failure['published_main_extractor_min_confidence'])
                and 0 <= failure['published_main_extractor_min_confidence']
                    <= failure['first_accepted_DOB_confidence'] <= 1,
                'DOB_ACCEPTED_CONFIDENCE_INVALID')
            # Exact safe grammar of the existing independent audit; user words
            # remain in its bound report, never in generated learning snapshots.
            established = (failure.get('source_parser_outputs_for_all_accepted_DOB') == [None,{}]
                and failure.get('cause') == 'The historical published birth_answer recognizes ordinal third or numeric 3 but not cardinal three for day-of-month. Spoken-year parsing succeeds, day parsing returns (None,{}), then identity_question repeats without verify_identity.'
                and failure.get('first_phone_or_account_reached') is False
                and failure.get('no_pending_bank_request_or_write_journal_entry') is True
                and results.get('actual_bank_dispatches') == 0
                and type(results.get('actual_bank_dispatches')) is int
                and results.get('official_iteration_PASS') is False)
            if established:
                observed = {'spoken_day_form':'cardinal','parser_rejected':True,
                    'identity_phase_repeated':True,'payment_phase_reached':False}
        if not isinstance(observed,dict):
            reasons.append('independently_audited_cardinal_DOB_facts_missing')
        else:
            require(set(observed) == {'spoken_day_form','parser_rejected','identity_phase_repeated','payment_phase_reached'},
                    'DOB_FACT_SHAPE_INVALID')
            require(observed['spoken_day_form'] == 'cardinal'
                and all(type(observed[k]) is bool for k in ('parser_rejected','identity_phase_repeated','payment_phase_reached')),
                'DOB_FACT_TYPE_INVALID')
            if (observed['parser_rejected'] and observed['identity_phase_repeated'] and not observed['payment_phase_reached']):
                code, confidence = 'SPOKEN_CARDINAL_DOB_PARSER_GAP', 'high_safe_audit'
                facts = observed.copy()
            else:
                reasons.append('specific_DOB_parser_failure_not_established')
    return {'code':code,'confidence':confidence,'unverified_reasons':reasons,'safe_facts':facts,
        'suggested_failure_family':value['failure_kind'],'source_policy_unchanged':True,
        'selected_manifest_entries_only':True,'complete_manifest_inventory_reverified':False,
        'audit_authenticated_by_this_bridge':False,'external_descriptor_pin_required':True, **NO_AUTHORITY}

def consume_descriptor(path, *, root, expected_descriptor_sha256, output_directory):
    started = time.monotonic()
    require(isinstance(expected_descriptor_sha256,str) and HASH.fullmatch(expected_descriptor_sha256), 'EXTERNAL_DESCRIPTOR_PIN_REQUIRED')
    try:
        descriptor_pin, value = legacy._read(root,path,json_required=True)
    except (legacy.FailureLearningError,OSError) as exc:
        raise NativeFailureLearningError('DESCRIPTOR_UNAVAILABLE_OR_UNSAFE') from exc
    require(descriptor_pin['sha256'] == expected_descriptor_sha256, 'DESCRIPTOR_EXTERNAL_PIN_MISMATCH')
    validate_descriptor(value)
    pins, contents = {}, {}
    for name, ref, optional, as_json in (
        ('primary',value['evidence']['primary'],False,True),
        ('source_join',value['evidence']['source_join'],True,True),
        ('source',value['bindings']['source'],True,False),
        ('export',value['bindings']['export'],True,True),
        ('manifest',value['bindings']['manifest'],True,True),
        ('policy',value['policy']['source'],False,False)):
        pin, content = safe_read(root,ref,optional=optional,as_json=as_json)
        pins[name], contents[name] = pin, content
    diagnosis = normalized_diagnosis(value,contents['primary'],contents['source_join'],contents['manifest'])
    output, _ = legacy._path(root,output_directory)
    require(not output.exists(), 'FRESH_OUTPUT_REQUIRED')
    output.mkdir(parents=True)
    snapshot = output/'legacy-snapshot.json'
    snapshot.write_text(json.dumps(value['snapshot'],sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    raw_record = legacy.record_failure(snapshot,root=root,
        source_paths=[p['path'] for k,p in pins.items() if p is not None and k in {'source','policy'}],
        evidence_paths=[p['path'] for k,p in pins.items() if p is not None and k in {'primary','source_join','export','manifest'}],
        output=output/'legacy-learning.json')
    receipt = {'schema_version':'native-failure-learning-receipt-1','failure_id':value['failure_id'],
        'run_id':value['run_id'],'case_id':value['case_id'],'descriptor_pin':descriptor_pin,
        'legacy_record_sha256':digest((output/'legacy-learning.json').read_bytes()),
        'legacy_diagnosis_preserved':raw_record['diagnosis'],'normalized_diagnosis':diagnosis,
        'existing_bounded_repair_eligibility':{'supported':False,'reason':'Confidence and DOB parser changes are outside static source-role renderer repair; no generated repair performed.'},
        'old_FAIL_preserved':True,'manual_compiler_fix':'none_by_this_bridge','automated_generated_repair':'not_demonstrated',
        'source_and_evidence_pins':pins,'elapsed_seconds':time.monotonic()-started,
        'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'API_calls':0,'model_calls':0,
        'source_policy_changes':0,'workflow_changes':0, **NO_AUTHORITY}
    for name,pin in pins.items():
        if pin is not None:
            require(legacy._read(root,pin['path'])[0] == pin, 'REFERENCE_CHANGED_DURING_RECORDING')
    require(legacy._read(root,path)[0] == descriptor_pin, 'DESCRIPTOR_CHANGED_DURING_RECORDING')
    (output/'receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return receipt

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--descriptor',action='append',required=True)
    parser.add_argument('--expected-descriptor-sha256',action='append',required=True)
    parser.add_argument('--out',required=True)
    args = parser.parse_args(argv)
    require(1 <= len(args.descriptor) <= 8 and len(args.descriptor) == len(args.expected_descriptor_sha256), 'DESCRIPTOR_PIN_PAIRS_REQUIRED')
    began = time.monotonic()
    out, _ = legacy._path(args.root,args.out)
    require(not out.exists(), 'FRESH_BATCH_OUTPUT_REQUIRED')
    out.mkdir(parents=True)
    rows = [consume_descriptor(path,root=args.root,expected_descriptor_sha256=pin,output_directory=out/('failure-'+str(i)))
            for i,(path,pin) in enumerate(zip(args.descriptor,args.expected_descriptor_sha256),1)]
    report = {'schema_version':'native-failure-learning-batch-1','status':'BOUNDED_FAILURE_FEEDBACK_RECORDED',
        'receipts':[{'failure_id':r['failure_id'],'run_id':r['run_id'],
                     'classification':r['normalized_diagnosis']['code'],
                     'raw_legacy_classification':r['legacy_diagnosis_preserved']['code'],
                     'automatic_repair_eligible':False} for r in rows],
        'elapsed_seconds':time.monotonic()-began,'API_calls':0,'model_calls':0, **NO_AUTHORITY}
    (out/'batch-receipt.json').write_text(json.dumps(report,sort_keys=True,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps(report,allow_nan=False))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
