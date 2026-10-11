"""v2 read-only PUB-01 evidence and independently recomputed behavior verifier."""
import argparse
from datetime import datetime
import json
from pathlib import Path

import validate_evidence as shape
from pub01_behavior import evaluate

CONTRACT_PATH = Path(__file__).with_name('contract-v2.json')
KIT = Path(__file__).resolve().parents[2] / 'data/freya-hackathon/kit-v3/build-the-builder'


class Rejection(ValueError):
    def __init__(self, code):
        self.code = code


def check(condition, code):
    if not condition:
        raise Rejection(code)


def exact(value, keys, code):
    check(isinstance(value, dict) and set(value) == set(keys), code)


def validate(bundle, manifest_sha256):
    result = {'schema_version': '2.0', 'integrity': 'invalid', 'acceptance': 'NOT_EVALUATED',
              'errors': [], 'official_freya_result': False, 'real_agent_reliability_measured': False,
              'evaluation_scope': 'deterministic explicit-English PUB-01 scripted acceptance'}
    try:
        check(isinstance(manifest_sha256, str) and shape.HASH.fullmatch(manifest_sha256), 'anchor.required')
        root = Path(bundle).resolve(strict=True)
        check(root.is_dir() and '.freya-local' not in root.parts, 'bundle.path')
        contract_raw = CONTRACT_PATH.read_bytes()
        contract = shape.strict_json(contract_raw)
        shape_contract = shape.strict_json(shape.CONTRACT_PATH.read_bytes())
        shape_contract['event_kinds'] = contract['event_kinds']
        paths = set(contract['batch_files']) | set(contract['source_files']) | {
            f'runs/{i}/{name}' for i in contract['repetitions'] for name in contract['run_files']}

        def read(path):
            check(path in paths | {'evidence-manifest.json'}, 'manifest.path')
            target = root / path
            check(target.resolve(strict=True).is_relative_to(root) and target.is_file(), 'bundle.escape')
            return target.read_bytes()

        manifest_raw = read('evidence-manifest.json')
        check(shape.digest(manifest_raw) == manifest_sha256, 'anchor.mismatch')
        manifest = shape.strict_json(manifest_raw)
        exact(manifest, ('schema_version', 'files'), 'manifest.fields')
        check(manifest['schema_version'] == '2.0' and isinstance(manifest['files'], list), 'manifest.version')
        blobs = {}
        for entry in manifest['files']:
            exact(entry, ('path', 'bytes', 'sha256'), 'manifest.entry_fields')
            name = entry['path']
            check(isinstance(name, str) and name in paths and name not in blobs, 'manifest.paths')
            check(type(entry['bytes']) is int and entry['bytes'] >= 0 and isinstance(entry['sha256'], str)
                  and shape.HASH.fullmatch(entry['sha256']), 'manifest.fingerprint')
            raw = read(name)
            check(len(raw) == entry['bytes'] and shape.digest(raw) == entry['sha256'], 'evidence.hash')
            blobs[name] = raw
        check(set(blobs) == paths, 'manifest.incomplete')
        for name, canonical in contract['source_files'].items():
            check(blobs[name] == (KIT / canonical).read_bytes(), 'source.drift')

        def doc(path):
            return shape.strict_json(blobs[path])

        def lines(path):
            records = blobs[path].splitlines()
            check(bool(records) and all(record.strip() for record in records), 'events.empty')
            return [shape.strict_json(record) for record in records]

        cases = doc('sources/public-test-set.json')
        case = next(c for c in cases if c.get('id') == contract['case_id'])
        api = doc('sources/bank-openapi.json')
        report = doc('report.json')
        inputs = doc('input-manifest.json')
        exact(report, contract['report_fields'], 'report.fields')
        exact(inputs, contract['input_fields'], 'input.fields')
        check(report['schema_version'] == inputs['schema_version'] == '2.0', 'report.version')
        check(inputs['contract_sha256'] == shape.digest(contract_raw), 'input.contract_hash')
        check(report['case_id'] == inputs['case_id'] == contract['case_id']
              and report['kind'] == contract['batch_kind'], 'report.kind')
        check(shape.text(report['batch_id']) and report['batch_id'] == inputs['batch_id'], 'metadata.batch')
        check(type(report['network_calls']) is int and report['network_calls'] == 0
              and type(report['model_calls']) is int and report['model_calls'] == 0, 'offline.boundary')
        agent_hash = shape.digest(blobs['agent.generated.json'])
        check(isinstance(doc('agent.generated.json'), dict)
              and inputs['agent_sha256'] == report['agent_sha256'] == agent_hash, 'metadata.agent')
        check(all(shape.text(inputs[k]) for k in ('plan_version', 'simulator_version', 'evaluator_version')), 'metadata.versions')
        check(isinstance(inputs['customers'], list) and len(inputs['customers']) == 3
              and all(shape.text(c) for c in inputs['customers']) and len(set(inputs['customers'])) == 3, 'metadata.customers')
        check(isinstance(report['runs'], list) and len(report['runs']) == 3, 'report.repetitions')
        run_results = []
        behavior_errors = []
        for repetition, run in zip(contract['repetitions'], report['runs']):
            exact(run, contract['run_fields'], 'report.run_fields')
            check(type(run['repetition']) is int and run['repetition'] == repetition, 'report.run_order')
            check(run['customer_id'] == inputs['customers'][repetition - 1]
                  and run['agent_sha256'] == agent_hash, 'metadata.run_identity')
            prefix = f'runs/{repetition}/'
            timeline = lines(prefix + 'timeline.jsonl')
            try:
                shape.sequence(timeline, repetition, shape_contract)
            except ValueError:
                raise Rejection('events.structure') from None
            check(lines(prefix + 'transcript.jsonl') == [e for e in timeline if e['kind'] == 'message'], 'events.transcript_projection')
            check(lines(prefix + 'tool-events.jsonl') == [e for e in timeline if e['kind'] == 'tool'], 'events.tool_projection')
            initial = doc(prefix + 'initial-state.json')
            exact(initial, shape_contract['state_fields'], 'state.initial_fields')
            check(timeline[0]['payload'].get('state') == initial, 'state.reset_projection')
            ledger = doc(prefix + 'final-ledger.json')
            exact(ledger, shape_contract['ledger_fields'], 'state.ledger_fields')
            check(timeline[-1]['payload'].get('ledger') == ledger, 'state.ledger_projection')
            computed = evaluate(timeline, case, api, run['customer_id'])
            check({c['check_id'] for c in computed} == set(contract['required_checks']), 'evaluator.coverage')
            recorded = doc(prefix + 'checks.json')
            check(isinstance(recorded, list), 'checks.type')
            check(len(recorded) == len(computed) and all(isinstance(c, dict) for c in recorded), 'checks.coverage')
            ids = [c.get('check_id') for c in recorded]
            check(all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids)
                  and set(ids) == set(contract['required_checks']), 'checks.coverage')
            for item in recorded:
                exact(item, contract['check_fields'], 'checks.fields')
            # Every expected/actual/verdict/reference is recomputed from the event stream.
            by_id = {c['check_id']: c for c in recorded}
            contradictions = [c['check_id'] for c in computed if by_id[c['check_id']] != c]
            failed = [c['check_id'] for c in computed if c['status'] != 'PASS']
            behavior_errors.extend({'code': 'behavior.' + name, 'repetition': repetition} for name in failed)
            if contradictions:
                behavior_errors.extend({'code': 'checks.false_claim.' + name, 'repetition': repetition} for name in contradictions)
            status = 'PASS' if not failed else 'FAIL'
            if run['status'] != status:
                behavior_errors.append({'code': 'report.false_run_verdict', 'repetition': repetition})
            run_results.append({'repetition': repetition, 'status': status, 'checks': computed})

        trace = lines('run-trace.jsonl')
        check(len(trace) == len(shape_contract['trace_lifecycle']), 'trace.lifecycle')
        prior_elapsed, prior_utc = -1, None
        for seq, (event, expected_event) in enumerate(zip(trace, shape_contract['trace_lifecycle']), 1):
            exact(event, shape_contract['trace_fields'], 'trace.fields')
            check(type(event['seq']) is int and event['seq'] == seq and event['event'] == expected_event, 'trace.sequence')
            check(shape.number(event['elapsed_ms']) and event['elapsed_ms'] >= prior_elapsed, 'trace.monotonic')
            prior_elapsed = event['elapsed_ms']
            check(shape.text(event['utc']), 'trace.utc')
            try:
                instant = datetime.fromisoformat(event['utc'].replace('Z', '+00:00'))
            except ValueError:
                raise Rejection('trace.utc') from None
            check(instant.tzinfo is not None and instant.utcoffset().total_seconds() == 0
                  and (prior_utc is None or instant >= prior_utc), 'trace.utc')
            prior_utc = instant
            run_id = None if seq in (1, 8) else seq // 2
            check(event['repetition'] == run_id and (run_id is None or type(event['repetition']) is int), 'trace.run_reference')
        acceptance = 'PASS' if all(r['status'] == 'PASS' for r in run_results) else 'FAIL'
        if report['status'] != acceptance:
            behavior_errors.append({'code': 'report.false_batch_verdict'})
        result.update(integrity='valid', acceptance=acceptance, runs=run_results, verified_files=len(blobs))
        result['errors'] = behavior_errors
        if behavior_errors:
            # A honestly recorded failed scenario has valid integrity; it still fails acceptance.
            result['claims_consistent'] = not any(e['code'].startswith(('checks.false_claim', 'report.false')) for e in behavior_errors)
        else:
            result['claims_consistent'] = True
    except Rejection as exc:
        result['errors'].append({'code': exc.code})
    except (ValueError, TypeError, KeyError, IndexError, OSError, UnicodeError, OverflowError, RuntimeError, StopIteration):
        result['errors'].append({'code': 'evidence.malformed'})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    result = validate(args.bundle, args.manifest_sha256)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['integrity'] == 'valid' and result['acceptance'] == 'PASS' and result['claims_consistent'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
