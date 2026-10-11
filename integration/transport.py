"""Explicit Freya transport; optional encrypted diagnostics, no retries.

Writes are disabled by default. The pilot coordinator must additionally apply
SessionGuard, admission evidence and the remote workspace concurrency check.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .session_guard import PreparationError


class TransportError(PreparationError):
    def __init__(self, code, http_status=None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FreyaTransport:
    BASE_URL = 'https://staging-app.freyavoice.ai'

    def __init__(self, api_key, *, allow_writes=False, opener=None, diagnostics=None, require_diagnostics=False):
        if not isinstance(api_key, str) or not api_key.strip():
            raise TransportError('missing_key')
        self._key = api_key
        self.allow_writes = allow_writes
        if require_diagnostics and not callable(getattr(diagnostics, 'record', None)):
            raise TransportError('response_diagnostics_required')
        if diagnostics is not None and not callable(getattr(diagnostics, 'record', None)):
            raise TransportError('invalid_response_diagnostics')
        self.diagnostics = diagnostics
        self.require_diagnostics = require_diagnostics
        self.diagnostic_failed = False
        self._opener = opener or urllib.request.build_opener(NoRedirect())

    def _record_response(self, method, path, status, headers, data, *, body_length_known=True, failure_class=None):
        if self.require_diagnostics and self.diagnostics is None:
            raise TransportError('response_diagnostics_required', status)
        if self.diagnostics is None:
            return
        try:
            self.diagnostics.record(method=method, path=path, http_status=status, headers=headers,
                                    body=data, observed_bytes=len(data), body_length_known=body_length_known,
                                    failure_class=failure_class)
        except Exception:
            self.diagnostic_failed = True
            try:
                self.diagnostics.failed = True
            except Exception:
                pass
            raise TransportError('diagnostic_persistence_failed', status) from None

    def __call__(self, method, path, *, body=None, params=None, timeout=30):
        if self.diagnostic_failed or getattr(self.diagnostics, 'failed', False):
            raise TransportError('diagnostics_latched_after_failure')
        if self.require_diagnostics and self.diagnostics is None:
            raise TransportError('response_diagnostics_required')
        if method not in {'GET', 'POST', 'PUT'} or (method != 'GET' and not self.allow_writes):
            raise TransportError('writes_disabled')
        if not re.fullmatch(r'/api/v2/[a-zA-Z0-9_/-]+', path):
            raise TransportError('invalid_relative_route')
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise TransportError('invalid_timeout')
        query = urllib.parse.urlencode(params or {})
        url = self.BASE_URL + path + ('?' + query if query else '')
        raw = json.dumps(body, allow_nan=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=raw, method=method,
                                     headers={'Authorization': 'Bearer ' + self._key,
                                              'Content-Type': 'application/json',
                                              'User-Agent': 'Mozilla/5.0 (build-the-builder-integration)'})
        try:
            with self._opener.open(req, timeout=timeout) as response:
                data = response.read(8_000_001)
                failure_class = None
                envelope = None
                if len(data) > 8_000_000:
                    failure_class = 'response_too_large'
                else:
                    try:
                        envelope = json.loads(data)
                    except Exception:
                        failure_class = 'network_or_decode_error'
                    if failure_class is None and (not isinstance(envelope, dict) or envelope.get('success') is not True
                            or not isinstance(envelope.get('data'), dict) or 'result' not in envelope['data']):
                        failure_class = 'invalid_or_unsuccessful_envelope'
                self._record_response(method, path, getattr(response, 'status', 200), getattr(response, 'headers', None),
                                      data, body_length_known=len(data) < 8_000_001,
                                      failure_class=failure_class)
                if failure_class is not None:
                    raise TransportError(failure_class)
        except urllib.error.HTTPError as exc:
            if self.diagnostics is not None:
                try:
                    limit = getattr(self.diagnostics, 'max_body_bytes', 65_536)
                    if type(limit) is not int or not 1 <= limit <= 65_536:
                        raise TransportError('invalid_diagnostic_body_bound', exc.code)
                    data = exc.read(limit + 1)
                    self._record_response(method, path, exc.code, exc.headers, data,
                                          body_length_known=len(data) <= limit, failure_class='http_error')
                except TransportError:
                    raise
                except Exception:
                    self.diagnostic_failed = True
                    try:
                        self.diagnostics.failed = True
                    except Exception:
                        pass
                    raise TransportError('diagnostic_capture_failed', exc.code) from None
                finally:
                    try:
                        exc.close()
                    except Exception:
                        pass
            # Only a safe code/status leaves this layer; no body/header content.
            raise TransportError('http_error', exc.code) from None
        except TransportError:
            raise
        except Exception:
            raise TransportError('network_or_decode_error') from None
        return envelope['data']['result']
