"""Persistent single-slot and budget guard around an explicitly supplied transport.

This module does not implement HTTP, read environment variables, load credentials,
or call a model. integration.pilot is the explicit opt-in controller/entrypoint;
tests inject scripted local replies into this guard.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid


class PreparationError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def one_case_payload(agent_id, persona_id, variables, *, name, agent_model, judge_model):
    """Models are explicit inputs; the later coordinator must use the kit's models."""
    return {"agentId": agent_id, "name": name, "mode": "chat",
            "selectedPersonaIds": [persona_id], "customerVariablesMap": {persona_id: variables},
            "maxConcurrentCalls": 1, "runsPerPersona": 1, "maxRetries": 0,
            "maxCallDurationSeconds": 420,
            "assistantLlmOverride": dict(agent_model), "routingLlmOverride": dict(agent_model),
            "extractionLlmOverride": dict(agent_model), "personaLlmOverride": dict(judge_model),
            "analysisLlmOverride": dict(judge_model)}


@dataclass(frozen=True)
class Budget:
    # Local pilot policy, NOT Freya's quota or a guaranteed monetary ceiling.
    max_requests: int = 75
    max_mutations: int = 8
    max_simulations: int = 1
    deadline_seconds: int = 900
    http_timeout_seconds: int = 30

    def __post_init__(self):
        for value in vars(self).values():
            if type(value) is not int or value < 1:
                raise PreparationError("Budget values must be positive integers")
        if self.max_simulations != 1 or self.deadline_seconds > 1800:
            raise PreparationError("Pilot permits one simulation and at most 30 minutes")


class SessionGuard:
    """A retained lock is deliberately NOT cleared on exception, timeout or restart.

The transport accepts (method, path, body=..., params=..., timeout=...) and returns
the decoded result payload. It must honor timeout; this synchronous guard cannot
forcibly stop a transport that ignores it. It must reject success:false envelopes.
All calls use this guard; direct kit calls would bypass its budgets and evidence.
"""
    def __init__(self, directory, workspace_id, transport, *, budget=None, clock=time.monotonic):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.workspace_id = workspace_id
        self.transport = transport
        self.budget = budget or Budget()
        self.clock = clock
        self.start = clock()
        self.deadline = self.start + self.budget.deadline_seconds
        self.run_id = str(uuid.uuid4())
        self.requests = self.mutations = self.simulations = 0
        self.pending = set()
        self.simulation_id = None
        self.simulation_terminal = False
        self.remote_idle_at = None
        self.closed = False
        self.mutex = threading.Lock()
        slot = hashlib.sha256(workspace_id.encode()).hexdigest()
        self.lock_path = self.directory / (slot + ".slot.json")
        self.journal_path = self.directory / (self.run_id + ".jsonl")
        try:
            with self.lock_path.open('x', encoding='utf-8') as stream:
                json.dump({"workspace_id": workspace_id, "run_id": self.run_id,
                           "journal": self.journal_path.name}, stream)
                stream.flush()
                import os
                os.fsync(stream.fileno())
        except FileExistsError:
            raise PreparationError("Workspace slot retained; reconcile its run before a new attempt") from None
        self._record("opened", budget=vars(self.budget), mode="prepared_transport_injection")

    def _record(self, event, **fields):
        row = {"at_utc": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id,
               "workspace_id": self.workspace_id, "elapsed_ms": round((self.clock() - self.start) * 1000),
               "event": event, **fields}
        with self.journal_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=True, allow_nan=False) + "\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())

    @staticmethod
    def _check_pilot(body):
        if not isinstance(body, dict):
            raise PreparationError("Pilot needs an explicit body")
        personas = body.get("selectedPersonaIds")
        if (body.get("mode") != "chat" or not isinstance(personas, list) or len(personas) != 1
                or not isinstance(personas[0], str) or not personas[0]
                or type(body.get("maxConcurrentCalls")) is not int or body.get("maxConcurrentCalls") != 1
                or type(body.get("runsPerPersona")) is not int or body.get("runsPerPersona") != 1
                or type(body.get("maxRetries")) is not int or body.get("maxRetries") != 0
                or type(body.get("maxCallDurationSeconds")) is not int
                or not 60 <= body["maxCallDurationSeconds"] <= 420):
            raise PreparationError("Pilot requires one chat persona, one run, no retries, at most 420 seconds")
        if set(body.get("customerVariablesMap", {})) != set(personas):
            raise PreparationError("Pilot persona and session variables do not match")
        models = [body.get(key) for key in ("assistantLlmOverride", "routingLlmOverride", "extractionLlmOverride")]
        for model in models + [body.get("personaLlmOverride"), body.get("analysisLlmOverride")]:
            if not isinstance(model, dict) or not model.get("provider") or not model.get("model"):
                raise PreparationError("Every model override must be explicit")
        if models[0] != models[1] or models[0] != models[2]:
            raise PreparationError("Agent model overrides must agree")

    def call(self, method, path, *, body=None, params=None, validate_result=None, persist_result=None):
        with self.mutex:
            if self.closed or self.clock() >= self.deadline:
                raise PreparationError("Session closed or local deadline exhausted")
            if method not in {"GET", "POST", "PUT"} or not path.startswith(("/api/v2/", "/v1/practice/")):
                raise PreparationError("Unsupported pilot operation")
            if "?" in path or "#" in path:
                raise PreparationError("Pass query fields separately, never in the logged path")
            mutation = method != "GET"
            simulation = method == "POST" and path == "/api/v2/simulations"
            if path.endswith("/rerun"):
                raise PreparationError("Automatic rerun is outside the pilot budget")
            if mutation and self.pending:
                raise PreparationError("Unresolved mutation; only reconciliation reads are allowed")
            if self.requests >= self.budget.max_requests or (mutation and self.mutations >= self.budget.max_mutations):
                raise PreparationError("Local request/mutation budget exhausted")
            if simulation:
                self._check_pilot(body)
                if self.simulations >= self.budget.max_simulations:
                    raise PreparationError("One simulation attempt already reserved")
                if self.remote_idle_at is None or self.clock() - self.remote_idle_at > 30:
                    raise PreparationError("A fresh complete remote simulation inventory is required")
            if self.simulations and mutation:
                raise PreparationError("After simulation reservation only evidence reads are allowed")
            request_id = str(uuid.uuid4())
            # Serialize/hash before consuming a reservation. No headers, bodies, query
            # values, transcripts or exception text are written into this journal.
            request_hash = digest({"body": body, "params": params or {}})
            timeout = min(self.budget.http_timeout_seconds, self.deadline - self.clock())
            if not math.isfinite(timeout) or timeout <= 0:
                raise PreparationError("No request time remains")
            self.requests += 1
            self.mutations += int(mutation)
            self.simulations += int(simulation)
            if mutation:
                self.pending.add(request_id)
            logged_path = '/v1/practice/ledger/[REDACTED]' if path.startswith('/v1/practice/ledger/') else path
            self._record("request_reserved", request_id=request_id, method=method, path=logged_path,
                         request_sha256=request_hash, timeout_seconds=timeout,
                         requests=self.requests, mutations=self.mutations, simulations=self.simulations)
            if method == 'GET' and path == '/api/v2/simulations':
                self.remote_idle_at = None
            if method == 'GET' and self.simulation_id and path == f'/api/v2/simulations/{self.simulation_id}':
                self.simulation_terminal = False
            try:
                result = self.transport(method, path, body=body, params=params or {}, timeout=timeout)
                response_hash = digest(result)
                if mutation and not isinstance(result, dict):
                    raise PreparationError("Mutation response is not a result object")
                if validate_result is not None:
                    validate_result(result)
                # A durable ID callback must finish before any later operation.
                # Lost/malformed responses or disk failure keep the mutation pending.
                if persist_result is not None:
                    persist_result(result)
                if simulation:
                    item = result.get("simulation", result)
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                        raise PreparationError("Simulation creation has no usable identifier")
                    self.simulation_id = item["id"]
                observed_status = None
                if method == "GET" and path == "/api/v2/simulations":
                    # Simulation enum: draft/running/paused/completed (platform docs),
                    # plus failed terminal in kit API notes. queued/pending are not
                    # documented simulation filter values; any unfamiliar state blocks.
                    query = params or {}
                    if (query.get("workspaceId") == self.workspace_id and 'status' not in query
                            and query.get('offset', 0) == 0 and not any(query.get(key) for key in ('search', 'filters'))):
                        rows = result.get('simulations') if isinstance(result, dict) else None
                        total = result.get('total') if isinstance(result, dict) else None
                        if (isinstance(rows, list) and type(total) is int and total == len(rows)
                                and all(isinstance(row, dict) and row.get('id')
                                        and row.get('status') in {'completed', 'failed'}
                                        and row.get('workspaceId', self.workspace_id) == self.workspace_id for row in rows)):
                            self.remote_idle_at = self.clock()
                if method == "GET" and self.simulation_id and path == f"/api/v2/simulations/{self.simulation_id}":
                    item = result.get("simulation", result) if isinstance(result, dict) else {}
                    if not isinstance(item, dict) or item.get('id') != self.simulation_id:
                        raise PreparationError("Simulation status does not match the reserved identifier")
                    observed_status = item.get("status") if isinstance(item, dict) else None
                    self.simulation_terminal = observed_status in {"completed", "failed"}
                # Persist completion before allowing the next mutation.
                self._record("request_completed", request_id=request_id, response_sha256=response_hash,
                             simulation_id=self.simulation_id, observed_status=observed_status)
                self.pending.discard(request_id)
                return result
            except Exception as exc:
                status = getattr(exc, 'http_status', None)
                self._record("mutation_unknown" if mutation else "read_failed", request_id=request_id,
                             http_status=status if type(status) is int and 100 <= status <= 599 else None)
                raise PreparationError("Request failed; inspect the retained journal, do not blindly replay") from None

    def close(self):
        with self.mutex:
            if self.closed:
                raise PreparationError("Session already closed")
            if self.pending or (self.simulations and not self.simulation_terminal):
                raise PreparationError("Keep slot until mutation uncertainty and remote simulation state are reconciled")
            self._record("closed", requests=self.requests, mutations=self.mutations, simulations=self.simulations)
            owner = json.loads(self.lock_path.read_text(encoding='utf-8'))
            if owner.get("run_id") != self.run_id:
                raise PreparationError("Slot owner changed; refusing to release it")
            self.lock_path.unlink()
            self.closed = True
