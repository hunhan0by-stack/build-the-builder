"""Read-only, standard-library verifier. No APIs, credentials, or simulator imports.

Validates the locked v1 artifact contract and a caller-provided manifest anchor.
It checks recorded evaluator verdicts, not the truth of conversation policy claims.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

CONTRACT_PATH = Path(__file__).with_name('contract-v1.json')
HASH = re.compile(r'[0-9a-f]{64}')


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def bad_constant(value):
        raise ValueError('Nonfinite JSON number')

    result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                        parse_constant=bad_constant)
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Nonfinite JSON number')
        if isinstance(value, dict):
            for child in value.values():
                finite(child)
        if isinstance(value, list):
            for child in value:
                finite(child)
    finite(result)
    return result


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fields(value, names, label):
    require(isinstance(value, dict) and set(value) == set(names),
            f'{label}: missing or unknown fields')


def text(value):
    return isinstance(value, str) and bool(value.strip())


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def sequence(events, repetition, contract):
    require(isinstance(events, list) and len(events) >= 2, 'Missing event timeline')
    previous = -1
    for index, event in enumerate(events, 1):
        fields(event, contract['event_fields'], 'event')
        require(type(event['seq']) is int and event['seq'] == index, 'Broken event sequence')
        require(type(event['repetition']) is int and event['repetition'] == repetition,
                'Event belongs to another run')
        require(number(event['elapsed_ms']) and event['elapsed_ms'] >= previous,
                'Event time moved backwards')
        previous = event['elapsed_ms']
        require(event['kind'] in contract['event_kinds'] and isinstance(event['payload'], dict),
                'Invalid event kind or payload')
        if event['kind'] == 'message':
            fields(event['payload'], ['role', 'text'], 'message')
            require(event['payload']['role'] in ('agent', 'customer') and text(event['payload']['text']),
                    'Invalid transcript message')
        if event['kind'] == 'tool':
            fields(event['payload'], ['tool', 'args', 'result', 'state_before_seq', 'state_after_seq'], 'tool')
            require(text(event['payload']['tool']) and isinstance(event['payload']['args'], dict)
                    and isinstance(event['payload']['result'], dict), 'Invalid tool event')
            before, after = event['payload']['state_before_seq'], event['payload']['state_after_seq']
            require(type(before) is int and type(after) is int and 1 <= before <= index <= after <= len(events),
                    'Invalid state references')
            require('state' in events[before - 1]['payload'] and 'state' in events[after - 1]['payload'],
                    'State reference has no state evidence')
    require(events[0]['kind'] == 'reset' and events[-1]['kind'] == 'end', 'Missing reset/end')


def validate(bundle: Path, manifest_sha256: str) -> dict:
    # Errors contain labels, never document contents or credential values.
    result = {'contract_version': '1.0', 'integrity': 'invalid', 'errors': [],
              'policy_execution_verified': False, 'official_freya_result': False}
    try:
        require(isinstance(manifest_sha256, str) and HASH.fullmatch(manifest_sha256),
                'A separately recorded manifest SHA-256 is required')
        root = bundle.resolve(strict=True)
        require(root.is_dir(), 'Bundle directory missing')
        contract_raw = CONTRACT_PATH.read_bytes()
        contract = strict_json(contract_raw)
        expected_paths = set(contract['batch_files']) | {
            f'runs/{run}/{name}' for run in contract['repetitions'] for name in contract['run_files']}

        def read(path):
            # Only fixed contract filenames are admitted. No arbitrary input-source reads.
            require(path in expected_paths | {'report.json', 'evidence-manifest.json'}, 'Unadmitted evidence path')
            target = root / path
            require(target.resolve(strict=True).is_relative_to(root), 'Evidence escapes bundle')
            require(target.is_file(), 'Evidence file missing')
            return target.read_bytes()

        manifest_raw = read('evidence-manifest.json')
        require(digest(manifest_raw) == manifest_sha256, 'Manifest differs from trusted anchor')
        manifest = strict_json(manifest_raw)
        fields(manifest, ['schema_version', 'files'], 'manifest')
        require(manifest['schema_version'] == '1.0' and isinstance(manifest['files'], list), 'Manifest version/type')
        blobs = {}
        for entry in manifest['files']:
            fields(entry, ['path', 'bytes', 'sha256'], 'manifest entry')
            path = entry['path']
            require(isinstance(path, str) and path in expected_paths and path not in blobs,
                    'Unknown or duplicate manifest path')
            require(type(entry['bytes']) is int and entry['bytes'] >= 0
                    and isinstance(entry['sha256'], str) and HASH.fullmatch(entry['sha256']), 'Invalid fingerprint')
            raw = read(path)
            require(len(raw) == entry['bytes'] and digest(raw) == entry['sha256'], 'Evidence bytes/hash changed')
            blobs[path] = raw
        require(set(blobs) == expected_paths, 'Required evidence missing from manifest')

        def document(path):
            return strict_json(blobs[path])

        def lines(path):
            records = blobs[path].splitlines()
            require(records and all(line.strip() for line in records), 'Empty or blank JSONL record')
            return [strict_json(line) for line in records]

        report = strict_json(read('report.json'))
        fields(report, contract['report_fields'], 'report')
        require(report['schema_version'] == '1.0' and report['kind'] == contract['batch_kind']
                and report['case_id'] == contract['case_id'] and text(report['batch_id']), 'Report version/kind/id')
        require(type(report['network_calls']) is int and report['network_calls'] == 0
                and type(report['model_calls']) is int and report['model_calls'] == 0, 'Offline boundary violated')
        require(report['manifest_sha256'] == manifest_sha256, 'Report manifest mismatch')
        agent_hash = digest(blobs['agent.generated.json'])
        require(isinstance(document('agent.generated.json'), dict), 'Agent must be JSON object')
        require(report['agent_sha256'] == agent_hash, 'Report agent mismatch')
        inputs = document('input-manifest.json')
        fields(inputs, contract['input_fields'], 'input manifest')
        require(inputs['schema_version'] == '1.0' and inputs['contract_sha256'] == digest(contract_raw)
                and inputs['case_id'] == contract['case_id'] and inputs['agent_sha256'] == agent_hash,
                'Input contract/case/agent mismatch')
        require(all(text(inputs[k]) for k in ('plan_version', 'simulator_version', 'evaluator_version')),
                'Missing component version')
        require(isinstance(inputs['sources'], list) and len(inputs['sources']) >= 3, 'Missing source fingerprints')
        roles = set()
        for source in inputs['sources']:
            fields(source, ['role', 'path', 'sha256'], 'source fingerprint')
            require(source['role'] in ('brief', 'api', 'fixture', 'plan') and text(source['path'])
                    and isinstance(source['sha256'], str) and HASH.fullmatch(source['sha256']), 'Invalid source metadata')
            roles.add(source['role'])
        require({'brief', 'api', 'fixture'} <= roles, 'Brief/API/fixture provenance missing')

        require(isinstance(report['runs'], list) and len(report['runs']) == 3, 'Exactly three runs required')
        customers = set()
        statuses = []
        for repetition, run in zip(contract['repetitions'], report['runs']):
            fields(run, contract['run_fields'], 'run report')
            require(type(run['repetition']) is int and run['repetition'] == repetition, 'Run order/duplicate')
            require(text(run['customer_id']) and run['customer_id'] not in customers, 'Run state isolation id')
            customers.add(run['customer_id'])
            require(run['agent_sha256'] == agent_hash and run['status'] in contract['statuses'], 'Run agent/status')
            prefix = f'runs/{repetition}/'
            timeline = lines(prefix + 'timeline.jsonl')
            sequence(timeline, repetition, contract)
            require(lines(prefix + 'transcript.jsonl') == [e for e in timeline if e['kind'] == 'message'], 'Transcript projection mismatch')
            require(lines(prefix + 'tool-events.jsonl') == [e for e in timeline if e['kind'] == 'tool'], 'Tool projection mismatch')
            initial = document(prefix + 'initial-state.json')
            fields(initial, contract['state_fields'], 'initial state')
            require(all(initial[k] is False for k in ('dob_verified', 'phone_verified', 'consent', 'account_read', 'closed'))
                    and all(type(initial[k]) is int and initial[k] == 0 for k in ('wrong_answers', 'close_count'))
                    and all(initial[k] == [] for k in ('promises', 'notes', 'callbacks', 'transfers')), 'Initial state not reset')
            require(timeline[0]['payload'].get('state') == initial, 'Reset evidence mismatch')
            ledger = document(prefix + 'final-ledger.json')
            fields(ledger, contract['ledger_fields'], 'final ledger')
            require(isinstance(ledger['outcome'], (str, type(None))) and isinstance(ledger['reason'], (str, type(None)))
                    and all(isinstance(ledger[k], list) for k in ('promises', 'notes', 'callbacks', 'transfers')), 'Ledger types')
            require(timeline[-1]['payload'].get('ledger') == ledger, 'Final ledger evidence mismatch')
            checks = document(prefix + 'checks.json')
            require(isinstance(checks, list), 'Checks must be array')
            check_ids = set()
            for check in checks:
                fields(check, contract['check_fields'], 'check')
                require(check['check_id'] in contract['required_checks'] and check['check_id'] not in check_ids, 'Unknown/duplicate check')
                check_ids.add(check['check_id'])
                require(check['status'] in contract['statuses'], 'Invalid check verdict')
                refs = check['evidence_seq']
                require(isinstance(refs, list) and refs and all(type(ref) is int and 1 <= ref <= len(timeline) for ref in refs)
                        and len(set(refs)) == len(refs), 'Missing/unresolved check evidence')
            require(check_ids == set(contract['required_checks']), 'Required check omitted')
            derived = 'PASS' if all(c['status'] == 'PASS' for c in checks) else (
                'ERROR' if any(c['status'] == 'ERROR' for c in checks) else (
                    'NOT_RUN' if any(c['status'] == 'NOT_RUN' for c in checks) else 'FAIL'))
            require(run['status'] == derived, 'Run verdict contradicts checks')
            statuses.append(derived)

        trace = lines('run-trace.jsonl')
        require(len(trace) == len(contract['trace_lifecycle']), 'Trace lifecycle incomplete')
        previous = -1
        previous_utc = None
        for seq, (event, expected) in enumerate(zip(trace, contract['trace_lifecycle']), 1):
            fields(event, contract['trace_fields'], 'trace')
            require(type(event['seq']) is int and event['seq'] == seq and event['event'] == expected, 'Trace sequence/lifecycle')
            require(number(event['elapsed_ms']) and event['elapsed_ms'] >= previous, 'Trace time moved backwards')
            previous = event['elapsed_ms']
            require(text(event['utc']), 'UTC timestamp missing')
            try:
                instant = datetime.fromisoformat(event['utc'].replace('Z', '+00:00'))
            except ValueError:
                raise ValueError('Invalid UTC timestamp') from None
            require(instant.tzinfo is not None and instant.utcoffset().total_seconds() == 0,
                    'Trace timestamp must be UTC')
            require(previous_utc is None or instant >= previous_utc, 'Trace UTC moved backwards')
            previous_utc = instant
            expected_run = None if seq in (1, 8) else (seq // 2)
            require(event['repetition'] == expected_run and (expected_run is None or type(event['repetition']) is int), 'Trace run reference')
        batch = 'PASS' if statuses == ['PASS'] * 3 else 'FAIL'
        require(report['status'] == batch, 'Batch verdict contradicts three runs')
        result.update(integrity='valid', reported_batch_status=batch, verified_files=len(blobs),
                      manifest_sha256=manifest_sha256)
    except (ValueError, TypeError, KeyError, OSError, UnicodeError, OverflowError, RuntimeError) as exc:
        # Do not echo paths or arbitrary content from malformed evidence.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else 'Malformed or unreadable evidence'
        if isinstance(exc, json.JSONDecodeError):
            message = 'Invalid JSON'
        result['errors'].append(message)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    result = validate(args.bundle, args.manifest_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['integrity'] == 'valid' else 1


if __name__ == '__main__':
    raise SystemExit(main())
