"""Portable lock interoperable with the existing official workspace controllers."""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import uuid

from builder.models import FoundationError


class SimulationReservation:
    """Keep an unknown simulation POST consumed across fresh run directories.

    The workspace mutex must remain held by the caller. The filename is shared
    with the private diagnostic dispatcher, which also refuses any pending file.
    No customer variables, credentials or request body are stored here.
    """
    def __init__(self, directory, workspace_id, *, require_protected=True):
        self.directory = Path(directory)
        self.workspace_sha256 = hashlib.sha256(workspace_id.encode()).hexdigest()
        self.path = self.directory / (self.workspace_sha256 + '.official-live-simulation-pending.json')
        self.owner_sha256 = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        self.simulation_id = None
        self.terminal = False
        self.require_protected = require_protected is not False

    def require_available(self):
        if self.directory.is_symlink() or self.path.is_symlink() or self.path.exists():
            raise FoundationError('CONTEST_WORKSPACE_SIMULATION_PENDING_NO_REPLAY')

    def reserve(self, body):
        self.require_available()
        self.simulation_id = None
        self.terminal = False
        self.directory.mkdir(parents=True, exist_ok=True)
        request = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        value = {'kind': 'WORKSPACE_SIMULATION_NO_REPLAY_RESERVATION',
                 'status': 'RESERVED_OUTCOME_UNKNOWN_UNTIL_TERMINAL',
                 'owner_sha256': self.owner_sha256, 'workspace_sha256': self.workspace_sha256,
                 'request_sha256': hashlib.sha256(request).hexdigest()}
        try:
            with self.path.open('xb') as stream:
                stream.write(json.dumps(value, sort_keys=True).encode())
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise FoundationError('CONTEST_WORKSPACE_SIMULATION_PENDING_NO_REPLAY') from None

    def created(self, simulation):
        identifier = simulation.get('id') if type(simulation) is dict else None
        if type(identifier) is not str or not identifier or len(identifier) > 256:
            raise FoundationError('CONTEST_SIMULATION_ID_UNMEASURED_NO_REPLAY')
        self.simulation_id = identifier

    def observe_terminal(self, simulation):
        self.terminal = (bool(self.simulation_id) and type(simulation) is dict
            and simulation.get('id') == self.simulation_id
            and type(simulation.get('status')) is str
            and simulation['status'] in ('completed', 'failed', 'paused', 'cancelled'))

    def _owned_terminal(self):
        if not self.simulation_id or not self.terminal:
            raise FoundationError('CONTEST_SIMULATION_TERMINAL_UNMEASURED_NO_RELEASE')
        if self.directory.is_symlink() or self.path.is_symlink():
            raise FoundationError('CONTEST_SIMULATION_RESERVATION_OWNERSHIP_CHANGED')
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise FoundationError('CONTEST_SIMULATION_RESERVATION_OWNERSHIP_CHANGED') from None
        if (type(value) is not dict or value.get('owner_sha256') != self.owner_sha256
                or value.get('workspace_sha256') != self.workspace_sha256):
            raise FoundationError('CONTEST_SIMULATION_RESERVATION_OWNERSHIP_CHANGED')
        return value

    def release_after_verified_chain(self, *, evidence, seal, binding):
        """Release only this recorder's sealed, complete current simulation."""
        from integration.contest_evidence import ContestEvidence, ROLE_KEYS, _join
        reservation = self._owned_terminal()
        try:
            if (not isinstance(evidence, ContestEvidence) or evidence.sealed is not True
                    or evidence.pending is not None or evidence.failed
                    or evidence.recorder.failed or type(seal) is not dict
                    or type(binding) is not dict or evidence.directory.is_symlink()):
                raise ValueError()
            final = evidence.directory / 'FINAL.json'
            if final.is_symlink() or Path(seal['path']).absolute() != final.absolute():
                raise ValueError()
            content = final.read_bytes()
            if hashlib.sha256(content).hexdigest() != seal['sha256']:
                raise ValueError()
            value = json.loads(content)
            if (value.get('format') != 'contest-protected-evidence-v1'
                    or value.get('binding') != binding
                    or binding.get('simulation_id') != self.simulation_id
                    or type(binding.get('workspace_id')) is not str
                    or hashlib.sha256(binding['workspace_id'].encode()).hexdigest() != self.workspace_sha256
                    or value.get('result') != seal.get('result')
                    or type(value.get('result')) is not dict
                    or value['result'].get('wire_join_verified') is not True
                    or type(value.get('roles')) is not dict
                    or set(value['roles']) != set(ROLE_KEYS)
                    or any(pair not in evidence.exchanges.values() for pair in value['roles'].values())
                    or len({pair['exchange_id'] for pair in value['roles'].values()}) != len(ROLE_KEYS)
                    or value.get('artifacts') != evidence.artifacts):
                raise ValueError()
            owned = {role: evidence.cache[pair['exchange_id']]
                     for role, pair in value['roles'].items()}
            artifacts = {role: evidence.cache['artifact:' + role] for role in evidence.artifacts}
            if _join(binding, owned, artifacts) != value['result']:
                raise ValueError()
            request = owned['simulation'][0]
            body = json.loads(base64.b64decode(request['body_base64'], validate=True))
            encoded = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            if hashlib.sha256(encoded).hexdigest() != reservation.get('request_sha256'):
                raise ValueError()
            # Verify the protected physical chain without recovering private bytes.
            manifest = value.get('manifest')
            if type(manifest) is not list or not manifest:
                raise ValueError()
            recorded = []
            for item in manifest:
                if type(item) is not dict or type(item.get('path')) is not str:
                    raise ValueError()
                relative = Path(item['path'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError()
                target = evidence.directory / relative
                if any(parent.is_symlink() for parent in (target, *target.parents)
                       if parent == evidence.directory or evidence.directory in parent.parents):
                    raise ValueError()
                data = target.read_bytes()
                if type(item.get('bytes')) is not int or len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item.get('sha256'):
                    raise ValueError()
                recorded.append(relative.as_posix())
            actual = [file.relative_to(evidence.directory).as_posix()
                      for file in evidence.directory.rglob('*') if file.is_file() and file != final]
            if len(set(recorded)) != len(recorded) or set(recorded) != set(actual):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise FoundationError('CONTEST_SIMULATION_PROTECTED_CHAIN_UNVERIFIED_NO_RELEASE') from None
        if self._owned_terminal() != reservation:
            raise FoundationError('CONTEST_SIMULATION_RESERVATION_OWNERSHIP_CHANGED')
        self.path.unlink()

    def release_after_local_terminal(self):
        """Plaintext fixture path; it cannot release a protected live reservation."""
        if self.require_protected:
            raise FoundationError('CONTEST_SIMULATION_PROTECTED_CHAIN_REQUIRED_NO_RELEASE')
        self._owned_terminal()
        self.path.unlink()


@contextmanager
def official_workspace_mutex(directory, workspace_id):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(workspace_id.encode()).hexdigest() + '.official-native-mutex')
    if path.is_symlink():
        raise FoundationError('CONTEST_WORKSPACE_MUTEX_SYMLINK_REFUSED')
    with path.open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\x00')
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise FoundationError('CONTEST_WORKSPACE_BUSY') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
