"""Sanitize synthetic call evidence, including JSON-string engine events."""
import json
import re


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            normalized = str(key).lower().replace('-', '').replace('_', '')
            if normalized in {'authorization', 'xteamkey', 'apikey', 'password', 'accesskey', 'accesstoken', 'refreshtoken'}:
                out[key] = '[REDACTED]'
            else:
                out[key] = sanitize(item, secrets)
        return out
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if isinstance(secret, str) and secret:
                result = result.replace(secret, '[REDACTED]')
        result = re.sub(r'fak_[A-Za-z0-9_-]+', '[REDACTED]', result)
        if result.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(result)
                return json.dumps(sanitize(parsed, secrets), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return result
    return value
