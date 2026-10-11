"""One synthetic practice session and its ledger; no direct bank tool calls."""
import json
import re
import urllib.error
import urllib.request

from .transport import NoRedirect, TransportError


class BankPracticeTransport:
    BASE_URL = 'https://freya-bank-mock.freyavoice.workers.dev'

    def __init__(self, team_key, *, opener=None, diagnostics=None, require_diagnostics=False):
        if not isinstance(team_key, str) or len(team_key) < 16:
            raise TransportError('missing_team_key')
        self._key = team_key
        if require_diagnostics and not callable(getattr(diagnostics, 'record', None)):
            raise TransportError('bank_response_diagnostics_required')
        self.diagnostics = diagnostics
        self.require_diagnostics = require_diagnostics
        self.diagnostic_failed = False
        self._opener = opener or urllib.request.build_opener(NoRedirect())

    def record_response(self, method, path, status, headers, raw, *, failure_class=None, length_known=True):
        if self.diagnostics is None:
            return
        try:
            self.diagnostics.record(method=method, path=path, http_status=status, headers=headers, body=raw,
                                    failure_class=failure_class, body_length_known=length_known)
        except Exception:
            self.diagnostic_failed = True
            self.diagnostics.failed = True
            raise TransportError('bank_diagnostic_persistence_failed', status) from None

    def __call__(self, method, path, *, body=None, params=None, timeout=30):
        if self.diagnostic_failed or getattr(self.diagnostics, 'failed', False):
            raise TransportError('bank_diagnostics_latched_after_failure')
        if self.require_diagnostics and self.diagnostics is None:
            raise TransportError('bank_response_diagnostics_required')
        if not ((method == 'POST' and path == '/v1/practice/sessions' and body == {'case': 'PUB-01'})
                or (method == 'GET' and re.fullmatch(r'/v1/practice/ledger/[A-Za-z0-9_-]+', path))):
            raise TransportError('bank_route_outside_pilot')
        req = urllib.request.Request(self.BASE_URL + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={'x-team-key': self._key,
                                                            'Content-Type': 'application/json',
                                                            'User-Agent': 'freya-bank-scorer/0.1'})
        try:
            with self._opener.open(req, timeout=min(timeout, 30)) as response:
                data = response.read(2_000_001)
                failure = 'bank_response_too_large' if len(data) > 2_000_000 else None
                result = None
                if failure is None:
                    try:
                        result = json.loads(data)
                        if not isinstance(result, dict):
                            failure = 'invalid_bank_result'
                    except Exception:
                        failure = 'bank_network_or_decode_error'
                self.record_response(method, path, getattr(response, 'status', 200), getattr(response, 'headers', None),
                                     data, failure_class=failure, length_known=len(data) < 2_000_001)
                if failure:
                    raise TransportError(failure)
        except urllib.error.HTTPError as exc:
            if self.diagnostics is not None:
                try:
                    limit = self.diagnostics.max_body_bytes
                    raw = exc.read(limit + 1)
                    self.record_response(method, path, exc.code, exc.headers, raw,
                                         failure_class='bank_http_error', length_known=len(raw) <= limit)
                except TransportError:
                    raise
                except Exception:
                    self.diagnostic_failed = True
                    self.diagnostics.failed = True
                    raise TransportError('bank_diagnostic_capture_failed', exc.code) from None
                finally:
                    exc.close()
            raise TransportError('bank_http_error', exc.code) from None
        except TransportError:
            raise
        except Exception:
            raise TransportError('bank_network_or_decode_error') from None
        if not isinstance(result, dict):
            raise TransportError('invalid_bank_result')
        return result
