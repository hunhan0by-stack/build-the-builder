"""Executable, portable structured workflow export and behavioral comparison.

The artifact is a Python workflow bundle, not an admitted native Freya export.
The platform must supply authenticated speech receipts and durable storage.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from string import Formatter
import zipfile

from .api_contract import BankContract
from .compiled_policy import lower_compilation
from .models import Document, FoundationError, strict_json
from .ingest import read_source

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('builder/__init__.py', 'builder/models.py', 'builder/api_contract.py',
           'builder/ingest.py', 'builder/calendar.py', 'builder/plan.py',
           'builder/runtime.py', 'builder/compiled_policy.py', 'builder/platform_bridge.py',
           'builder/durable_replay.py', 'builder/semantic_renderer.py', 'builder/generic_emitter.py',
           'compiler/__init__.py', 'compiler/core.py', 'compiler/schema.py', 'compiler/providers.py')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def emit_compiled_workflow(compiled, documents, contract, wording_bundle, source_snapshots, upstream_trace):
    """Emit only the approved one-product/promise/structured-input subset."""
    from .semantic_renderer import validate_wording_bundle
    wording = validate_wording_bundle(wording_bundle, documents)
    if not source_snapshots or any(source_snapshots.get(doc.path) != doc.sha256 for doc in documents):
        raise FoundationError('Complete policy source snapshots required')
    matching_api = []
    for path, expected in source_snapshots.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise FoundationError('Source snapshot changed before emission')
        if expected == contract.source_hash:
            source = read_source(Path(path))
            if source.source_id == contract.source_id:
                matching_api.append(source)
    envelope = upstream_trace['compilation']
    api = upstream_trace['api_document']
    if not matching_api or any(canonical(strict_json(source.text)) != canonical(api) for source in matching_api):
        raise FoundationError('API document differs from pinned source snapshot')
    rebuilt_contract = BankContract(api, contract.source_id, contract.source_hash)
    if canonical(rebuilt_contract.serializable()) != canonical(contract.serializable()):
        raise FoundationError('Emitter API contract differs from supplied contract')
    rebuilt = lower_compilation(envelope, documents, rebuilt_contract)
    if canonical(rebuilt.serializable()) != canonical(compiled.serializable()):
        raise FoundationError('Emitter inputs differ from validated source compilation')
    code = {name: (ROOT / name).read_text(encoding='utf-8') for name in MODULES}
    bundle = {'format': 'bounded-structured-workflow-1', 'compilation': copy.deepcopy(envelope),
              'documents': [asdict(doc) for doc in documents], 'api_document': copy.deepcopy(api),
              'contract_source_id': contract.source_id, 'contract_sha256': contract.source_hash,
              'wording': wording, 'source_snapshots': dict(source_snapshots),
              'scope': ['single_product', 'single_promise', 'structured_customer_inputs'],
              'native_freya_export': False, 'platform_receipt_binding_verified': False,
              'publish_allowed': False}
    return {'format': bundle['format'], 'bundle': bundle, 'code': code,
            'bundle_sha256': digest(bundle),
            'code_sha256': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in code.items()},
            'publish_allowed': False}


def write_workflow(workflow, destination):
    """Fresh ZIP application contains its runtime, source IR and wording."""
    path = Path(destination)
    if path.exists():
        raise FoundationError('Existing export may not be overwritten')
    if digest(workflow['bundle']) != workflow['bundle_sha256']:
        raise FoundationError('Export bundle changed')
    for name, text in workflow['code'].items():
        if name not in MODULES or hashlib.sha256(text.encode()).hexdigest() != workflow['code_sha256'].get(name):
            raise FoundationError('Export runtime changed')
    if set(workflow['code']) != set(MODULES):
        raise FoundationError('Export runtime module missing')
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in sorted(workflow['code'].items()):
            archive.writestr(name, text.encode())
        archive.writestr('workflow.json', canonical(workflow['bundle']).encode())
        archive.writestr('__main__.py',
            'import socket\ndef deny(*args, **kwargs):\n    raise RuntimeError("Network disabled in isolated equivalence fixture")\n'
            'socket.create_connection=deny\nsocket.socket.connect=deny\n'
            'from builder.generic_emitter import export_main\nexport_main()\n')
    return path


def load_compiled(bundle):
    documents = [Document(**value) for value in bundle['documents']]
    contract = BankContract(bundle['api_document'], bundle['contract_source_id'], bundle['contract_sha256'])
    return lower_compilation(bundle['compilation'], documents, contract), documents


def execute_script(compiled, documents, wording, variables, actions, journal_path, *, scope='generic-equivalence'):
    """Local fixture adapter; never pretends its completion receipt is Freya's."""
    from .durable_replay import DurableJournal
    from .platform_bridge import PlatformBridge
    from .semantic_renderer import SourceRenderer
    journal = DurableJournal(journal_path, scope, digest(compiled.serializable()))
    renderer = SourceRenderer(documents, wording)

    def verified_fixture(receipt):
        return isinstance(receipt, dict) and receipt.get('authority') == 'local_fixture'

    bridge = PlatformBridge(compiled, variables, journal, receipt_verifier=verified_fixture, receipt_authority='local_fixture')
    spoken, requests, checkpoints, rejected = [], [], [], []
    rendered = set()

    def collect():
        snap = bridge.snapshot()
        for event in snap['events']:
            if event['event_id'] in rendered or event['kind'] not in wording:
                continue
            used = {field for _, field, _, _ in Formatter().parse(wording[event['kind']]['template']) if field is not None}
            context = {key: value for key, value in event['context'].items() if key in used}
            item = renderer.render(event['event_id'], event['kind'], context,
                                   bank_outcome=event.get('bank_outcome'))
            bridge.bind_speech(event['event_id'], item['text'])
            spoken.append(item)
            rendered.add(event['event_id'])
        return bridge.snapshot()

    for action in actions:
        try:
            kind = action['op']
            if kind == 'start':
                bridge.start()
            elif kind == 'input':
                value = copy.deepcopy(action['value'])
                if value.get('kind') == 'confirm' and value.get('token') == '$current':
                    value.pop('token')
                    value['readback_token'] = bridge.snapshot()['readback_token']
                if value.get('kind') == 'proposal' and 'product_id' not in value:
                    value['product_id'] = bridge.snapshot()['selected']
                bridge.receive(value)
            elif kind == 'advance':
                bridge.advance()
            elif kind == 'bank':
                pending = bridge.snapshot()['pending_bank_requests']
                if len(pending) != 1:
                    raise FoundationError('Exactly one bank request must be pending')
                request = pending[0]
                bridge.mark_dispatched(request['request_id'])
                requests.append({'request_id': request['request_id'], 'operation': request['operation'], 'payload': request['payload']})
                bridge.resume_bank(request['request_id'], action['result'])
            elif kind == 'close':
                bridge.request_close('promise')
            elif kind == 'ack':
                event = next(item for item in reversed(bridge.snapshot()['events']) if item['kind'] == 'goodbye')
                text = next(item['text'] for item in spoken if item['event_id'] == event['event_id'])
                receipt = {'event_id': event['event_id'], 'status': 'completed', 'authority': 'local_fixture',
                           'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
                bridge.acknowledge_speech(event['event_id'], receipt)
            elif kind == 'restart':
                bridge = PlatformBridge(compiled, variables,
                    DurableJournal(journal_path, scope, digest(compiled.serializable())), receipt_verifier=verified_fixture,
                    receipt_authority='local_fixture')
            else:
                raise FoundationError('Unsupported script action')
        except FoundationError as exc:
            rejected.append({'action_index': len(checkpoints), 'error': str(exc)})
            if action.get('expect_rejected') is not True:
                raise
        else:
            if action.get('expect_rejected') is True:
                raise FoundationError('Expected negative action was accepted')
        checkpoints.append(collect())
    return {'spoken_events': spoken, 'bank_requests': requests, 'checkpoints': checkpoints,
            'rejected': rejected, 'final': bridge.snapshot(), 'receipt_authority': 'local_fixture',
            'real_platform_speech_proven': False, 'network_calls': 0, 'publish_allowed': False}


def export_main():
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('--script', required=True, type=Path)
    parser.add_argument('--journal', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Evidence may not be overwritten')
    with zipfile.ZipFile(sys.argv[0]) as archive:
        bundle = strict_json(archive.read('workflow.json').decode())
    script = strict_json(args.script.read_text(encoding='utf-8'))
    compiled, documents = load_compiled(bundle)
    result = execute_script(compiled, documents, bundle['wording'], script['variables'], script['actions'], args.journal)
    args.out.write_text(canonical(result) + '\n', encoding='utf-8')

