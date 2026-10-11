"""Protected evidence: Windows-user DPAPI default, explicit optional Fernet.

The Fernet key is external deployment state. Never log it, send it to a model,
store it with evidence, or replace protection with a plaintext fallback.
"""
import base64
import binascii
import ctypes
from ctypes import wintypes
import hmac
import os
from pathlib import Path
import re

from .session_guard import PreparationError


FERNET_PREFIX = b'FREYA-EVIDENCE:fernet:v1\x00'
FRAME_NAMESPACE = b'FREYA-EVIDENCE:'


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _mode():
    value = os.environ.get('FREYA_EVIDENCE_PROTECTION', 'dpapi')
    if value not in ('dpapi', 'fernet'):
        raise PreparationError('EVIDENCE_PROTECTION_MODE_INVALID')
    return value


def _fernet_key():
    value = os.environ.get('FREYA_EVIDENCE_KEY')
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}=', value):
        raise PreparationError('EVIDENCE_FERNET_KEY_MISSING_OR_INVALID')
    try:
        key = value.encode('ascii')
        decoded = base64.b64decode(key, altchars=b'-_', validate=True)
        if len(decoded) != 32 or base64.urlsafe_b64encode(decoded) != key:
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise PreparationError('EVIDENCE_FERNET_KEY_MISSING_OR_INVALID') from None
    return key


def _fernet(key):
    try:
        from cryptography.fernet import Fernet
    except (ImportError, OSError):
        raise PreparationError('EVIDENCE_FERNET_DEPENDENCY_UNAVAILABLE') from None
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise PreparationError('EVIDENCE_FERNET_KEY_MISSING_OR_INVALID') from None


def _dpapi_protect(raw):
    if os.name != 'nt':
        raise PreparationError('Encrypted evidence persistence needs the Windows user DPAPI profile')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    if not crypt.CryptProtectData(ctypes.byref(source), 'Freya protected evidence', None, None, None, 1, ctypes.byref(target)):
        raise PreparationError('Windows encrypted evidence persistence failed; no replay is allowed')
    kernel = ctypes.windll.kernel32
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def resolve_protector():
    """Validate and bind one recorder's protection config before dispatch.

    Environment changes during this recorder's lifetime fail closed. The key
    remains only in the callable's memory, never in public receipt fields.
    """
    mode = _mode()
    key = None
    cipher = None
    if mode == 'dpapi':
        if os.name != 'nt':
            raise PreparationError('Encrypted evidence persistence needs the Windows user DPAPI profile')
    else:
        key = _fernet_key()
        cipher = _fernet(key)
        probe = b'FREYA_NON_SECRET_FERNET_PREFLIGHT\x00\xff'
        try:
            if cipher.decrypt(cipher.encrypt(probe)) != probe:
                raise ValueError
        except Exception:
            raise PreparationError('EVIDENCE_FERNET_PREFLIGHT_FAILED') from None

    def bound(raw):
        if not isinstance(raw, bytes):
            raise PreparationError('Protected evidence must be bytes')
        if _mode() != mode:
            raise PreparationError('EVIDENCE_PROTECTION_CONFIG_CHANGED')
        if mode == 'dpapi':
            return _dpapi_protect(raw)
        if not hmac.compare_digest(_fernet_key(), key):
            raise PreparationError('EVIDENCE_PROTECTION_CONFIG_CHANGED')
        try:
            return FERNET_PREFIX + cipher.encrypt(raw)
        except Exception:
            raise PreparationError('EVIDENCE_FERNET_PROTECTION_FAILED') from None

    bound.protection_backend = 'windows_user_dpapi' if mode == 'dpapi' else 'fernet_v1'
    return bound


def protect_bytes(raw):
    # Keep this public function identity: existing default argument references
    # and production decoder identity checks must continue to bind this API.
    if not isinstance(raw, bytes):
        raise PreparationError('Protected evidence must be bytes')
    return resolve_protector()(raw)


def protect_secret(text):
    return protect_bytes(text.encode('utf-8'))


def ciphertext_backend(ciphertext):
    """Format label only; this does not authenticate or decrypt the record."""
    if not isinstance(ciphertext, bytes) or not ciphertext:
        raise PreparationError('Protected evidence bytes missing')
    if ciphertext.startswith(FERNET_PREFIX):
        return 'fernet_v1'
    if ciphertext.startswith(FRAME_NAMESPACE):
        raise PreparationError('EVIDENCE_PROTECTION_FRAME_UNSUPPORTED')
    return 'windows_user_dpapi'


def _dpapi_unprotect(ciphertext):
    if os.name != 'nt':
        raise PreparationError('Encrypted evidence recovery needs Windows DPAPI bytes')
    buffer = ctypes.create_string_buffer(ciphertext)
    source = Blob(len(ciphertext), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise PreparationError('Windows encrypted evidence recovery failed')
    kernel = ctypes.windll.kernel32
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def unprotect_bytes(ciphertext):
    """Explicit local recovery only; no fallback after a framed token failure.

    Framed Fernet records select their own decoder and require the external
    key. Unframed historical bytes still use the original Windows DPAPI path,
    even when new writes explicitly select Fernet.
    """
    backend = ciphertext_backend(ciphertext)
    if backend == 'windows_user_dpapi':
        return _dpapi_unprotect(ciphertext)
    cipher = _fernet(_fernet_key())
    try:
        return cipher.decrypt(ciphertext[len(FERNET_PREFIX):])
    except Exception:
        raise PreparationError('EVIDENCE_FERNET_RECOVERY_FAILED') from None


def persist_secret(path, text, *, protector=protect_secret):
    ciphertext = protector(text)
    if not isinstance(ciphertext, bytes) or not ciphertext or text.encode() in ciphertext:
        raise PreparationError('Secret protector did not return protected data')
    destination = Path(path)
    with destination.open('xb') as stream:
        stream.write(ciphertext)
        stream.flush()
        os.fsync(stream.fileno())
