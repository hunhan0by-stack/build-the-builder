"""Bounded encrypted response bodies and a narrow, non-secret metadata receipt."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .private_state import protect_bytes, resolve_protector
from .sanitize import sanitize
from .session_guard import PreparationError

MAX_BODY_BYTES = 65_536
TRACE_HEADERS = {'x-request-id', 'request-id', 'x-correlation-id', 'traceparent', 'cf-ray'}
SAFE_HEADERS = TRACE_HEADERS | {'content-type', 'server'}
KNOWN_MESSAGES = ('agent service not configured', 'agent service returned no response',
                  'agent service timed out', 'service unavailable', 'validation error',
                  'missing or invalid token', 'workspace id mismatch or insufficient permissions')


def redacted_path(path):
    if path.startswith('/v1/practice/ledger/'):
        return '/v1/practice/ledger/[REDACTED]'
    path = path.split('?', 1)[0].split('#', 1)[0]
    return re.sub(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', '{id}', path)[:256]


def safe_headers(headers, secrets):
    result, protected = {}, {}
    for key, value in (headers.items() if headers is not None else []):
        key = str(key).lower()
        if key not in SAFE_HEADERS:
            continue
        raw = str(value)[:256]
        protected[key] = raw
        value = sanitize(raw, secrets)
        suspicious = (value != raw or re.search(r'[\x00-\x1f\x7f]', value)
                      or re.search(r'(?i)bearer|cookie|authorization|password|api[-_]?key|token|secret|fak_', value))
        identifier = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        if key == 'traceparent':
            pattern = r'[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}'
        elif key == 'cf-ray':
            pattern = r'[0-9a-f]{16,32}(?:-[A-Z]{3,5})?'
        elif key in TRACE_HEADERS:
            pattern = rf'(?:{identifier}|(?:req|request|corr|correlation|trace)[-:](?:[0-9]{{1,20}}|{identifier}|[0-9a-f]{{16,64}}))'
        elif key == 'content-type':
            pattern = r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+(?:;\s*charset=[a-z0-9-]+)?'
        else:
            pattern = r'(?:cloudflare|nginx(?:/[0-9.]+)?|envoy|gfe|apache(?:/[0-9.]+)?)'
        if not suspicious and re.fullmatch(pattern, value, flags=re.IGNORECASE):
            result[key] = value
        else:
            result[key] = {'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(), 'plaintext_omitted': True}
    return result, protected


def body_summary(data):
    """Never copy arbitrary messages, nested request bodies, or customer text."""
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        return {'format': 'non_json_or_truncated', 'plaintext_body_emitted': False}
    summary = {'format': 'json', 'plaintext_body_emitted': False}
    if not isinstance(value, dict):
        return summary
    if type(value.get('success')) is bool:
        summary['success'] = value['success']
    container = value.get('data')
    if isinstance(container, dict):
        message = container.get('message')
        if isinstance(message, str):
            summary['message_present'] = True
            summary['message_bytes'] = len(message.encode('utf-8'))
            # Only recognised complete messages become plaintext classifications.
            lowered = message.lower().strip()
            if lowered in KNOWN_MESSAGES:
                summary['known_error_class'] = lowered
    return summary


class ResponseDiagnostics:
    def __init__(self, directory, *, secrets=(), protector=protect_bytes, max_body_bytes=MAX_BODY_BYTES):
        if type(max_body_bytes) is not int or not 1 <= max_body_bytes <= MAX_BODY_BYTES:
            raise PreparationError('Invalid diagnostic body bound')
        self.directory = Path(directory)
        self.secrets = tuple(secrets)
        self.protector = resolve_protector() if protector is protect_bytes else protector
        self.protection = (self.protector.protection_backend if protector is protect_bytes
                           else 'injected_protector_not_verified')
        self.max_body_bytes = max_body_bytes
        self.failed = False

    def preflight(self):
        try:
            return self.record(method='LOCAL', path='/diagnostics/preflight', http_status=None, headers={},
                               body=b'FREYA_NON_SECRET_RESPONSE_RECORDER_PROBE\x00\xff',
                               failure_class='local_recorder_preflight')
        except Exception:
            self.failed = True
            raise PreparationError('Response diagnostic encryption/storage preflight failed') from None

    def record(self, *, method, path, http_status, headers, body, observed_bytes=None,
               body_length_known=True, failure_class=None):
        if self.failed:
            raise PreparationError('Response diagnostics is latched after a persistence failure')
        try:
            return self._record(method=method, path=path, http_status=http_status, headers=headers, body=body,
                                observed_bytes=observed_bytes, body_length_known=body_length_known, failure_class=failure_class)
        except Exception:
            self.failed = True
            raise PreparationError('Response diagnostic persistence failed') from None

    def _record(self, *, method, path, http_status, headers, body, observed_bytes=None,
                body_length_known=True, failure_class=None):
        if not isinstance(body, bytes):
            raise PreparationError('Diagnostic body is not bytes')
        observed = len(body) if observed_bytes is None else observed_bytes
        if type(observed) is not int or observed < len(body):
            raise PreparationError('Invalid diagnostic observed byte count')
        captured = body[:self.max_body_bytes]
        ciphertext = self.protector(captured)
        if (not isinstance(ciphertext, bytes) or not ciphertext or ciphertext == captured
                or (len(captured) >= 16 and captured in ciphertext)):
            raise PreparationError('Diagnostic protector did not return protected bytes')
        receipt_id = str(uuid.uuid4())
        header_metadata, protected_headers = safe_headers(headers, self.secrets)
        header_ciphertext = None
        if protected_headers:
            raw_headers = json.dumps(protected_headers, ensure_ascii=True).encode('utf-8')
            header_ciphertext = self.protector(raw_headers)
            if (not isinstance(header_ciphertext, bytes) or not header_ciphertext or header_ciphertext == raw_headers
                    or raw_headers in header_ciphertext):
                raise PreparationError('Diagnostic header protector did not return protected bytes')
        metadata = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'receipt_id': receipt_id,
                    'method': method, 'path': redacted_path(path), 'http_status': http_status,
                    'failure_class': failure_class, 'headers': header_metadata,
                    'protected_headers_file': receipt_id + '.headers.dpapi' if header_ciphertext is not None else None,
                    'body_limit_bytes': self.max_body_bytes, 'captured_body_bytes': len(captured),
                    'observed_response_bytes': observed, 'response_length_known': bool(body_length_known),
                    'body_truncated': observed > len(captured) or not body_length_known,
                    'captured_body_sha256': hashlib.sha256(captured).hexdigest(),
                    'body_storage': self.protection, 'body_file': receipt_id + '.dpapi',
                    'body_summary': body_summary(captured)}
        metadata = sanitize(metadata, self.secrets)
        self.directory.mkdir(parents=True, exist_ok=True)
        # Both files are durable before transport can return a result. A failure
        # may leave encrypted bytes, but cannot let a subsequent write proceed.
        with (self.directory / metadata['body_file']).open('xb') as stream:
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        if header_ciphertext is not None:
            with (self.directory / metadata['protected_headers_file']).open('xb') as stream:
                stream.write(header_ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
        target = self.directory / (receipt_id + '.json')
        temporary = self.directory / (receipt_id + '.metadata.tmp')
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(metadata, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        return receipt_id
