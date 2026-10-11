"""A generic JSON gateway contract, not an OpenAI/provider-specific wire format."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from builder.models import FoundationError, strict_json


class CannedProvider:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return self.response


@dataclass(frozen=True)
class ProviderConfig:
    endpoint: str
    model: str
    api_key_env: str
    timeout_seconds: int = 30
    max_response_bytes: int = 2_000_000

    def validate(self):
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise FoundationError("Explicit HTTPS compilation gateway without credentials/query required")
        if not self.model.strip() or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise FoundationError("Explicit model and key environment variable name required")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 120:
            raise FoundationError("Provider timeout must be 1..120 seconds")
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= 2_000_000:
            raise FoundationError("Invalid provider response byte limit")


class JsonEndpointProvider:
    """One POST, no retries, opt-in only; response must be the bare decision IR JSON.

    The endpoint must implement this module's documented gateway contract.
    Native model provider integration is deliberately not inferred.
    """
    def __init__(self, config: ProviderConfig, *, allow_model_call=False):
        config.validate()
        self.config = config
        self.allow_model_call = allow_model_call
        self.calls = 0

    def generate(self, request):
        if self.allow_model_call is not True:
            raise FoundationError("Model transport disabled; explicit model-call approval required")
        if self.calls:
            raise FoundationError("Single provider-call budget exhausted")
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise FoundationError("Configured model credential unavailable")
        import json
        raw = json.dumps({"model": self.config.model, "compilation_request": request},
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 4_000_000:
            raise FoundationError("Compilation request exceeds byte budget")
        self.calls += 1
        req = Request(self.config.endpoint, data=raw, method="POST",
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        # Never return raw network exceptions: URLs, gateway bodies or secrets may be included.
        try:
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None

            with build_opener(NoRedirect).open(req, timeout=self.config.timeout_seconds) as response:
                if response.status != 200:
                    raise FoundationError("Compilation gateway did not return success")
                data = response.read(self.config.max_response_bytes + 1)
            if len(data) > self.config.max_response_bytes:
                raise FoundationError("Provider response exceeds byte budget")
            text = data.decode("utf-8")
            strict_json(text)
            return text
        except Exception:
            raise FoundationError("Compilation gateway response unavailable or invalid") from None
