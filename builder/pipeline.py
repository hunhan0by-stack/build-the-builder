"""Explicit local inputs only. No environment, credential loader or HTTP client."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .api_contract import BankContract
from .extract import extract_policy
from .ingest import PROTECTED_DIRECTORIES, collect_sources, line_spans, read_source
from .models import FoundationError, strict_json
from .plan import coverage, workflow_plan
from .validate import validate_foundation
from .semantics import compile_practice_policy
from .conversation import conversation_plan
from .turns import turn_contract


class LocalTrace:
    def __init__(self, deadline_seconds):
        if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
            raise FoundationError("Local build time budget must be 1..1800 seconds")
        self.start = time.monotonic()
        self.deadline = self.start + deadline_seconds
        self.events = []

    def record(self, stage, **details):
        if time.monotonic() > self.deadline:
            raise FoundationError("Local build time budget exhausted")
        self.events.append({"stage": stage, "elapsed_ms": round((time.monotonic() - self.start) * 1000), **details})


def practice_index(source):
    cases = strict_json(source.text)
    if not isinstance(cases, list):
        raise FoundationError("Practice test set must be a list")
    result, seen = [], set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in seen:
            raise FoundationError("Practice cases need unique string IDs")
        seen.add(case["id"])
        result.append({"id": case["id"], "brief": case.get("brief"), "family": case.get("family"),
                       "title": case.get("title"), "checks": case.get("checks", []),
                       "status": "indexed_not_executed"})
    return {"source_id": source.source_id, "source_sha256": source.sha256,
            "policy_input": False, "expected_results_used_for_generation": False,
            "evaluated": False, "cases": result}


def build(*, brief_paths, tool_api_path, output_path, context_paths=(), cases_path=None, deadline_seconds=120):
    trace = LocalTrace(deadline_seconds)
    output = Path(output_path).resolve()
    workspace = Path.cwd().resolve()
    if not output.is_relative_to(workspace) or output == workspace or set(p.lower() for p in output.parts) & PROTECTED_DIRECTORIES:
        raise FoundationError("Build outputs must be in a nonprotected subdirectory of the current workspace")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FoundationError("Output already contains files; choose a new directory to preserve previous runs")
    if not brief_paths:
        raise FoundationError("At least one explicit policy source is required")
    briefs = collect_sources([Path(path) for path in brief_paths])
    contexts = collect_sources([Path(path) for path in context_paths])
    api_source = read_source(Path(tool_api_path))
    contract = BankContract(strict_json(api_source.text), api_source.source_id, api_source.sha256)
    case_source = read_source(Path(cases_path)) if cases_path else None
    trace.record("ingest", policy_sources=len(briefs), context_sources=len(contexts), credential_sources_read=0)
    policy = extract_policy(briefs, contract)
    trace.record("extract", requirement_blocks=len(policy.requirements), parameter_facts=len(policy.facts))
    plan = workflow_plan(policy)
    semantics = compile_practice_policy(briefs, policy, contract)
    documents = briefs + contexts + [api_source] + ([case_source] if case_source else [])
    validation = validate_foundation(documents, policy, contract, plan)
    validation["reviewed_practice_decision_binding"] = semantics["complete_for_supplied_practice"]
    validation["general_semantic_compiler"] = False
    trace.record("validate", local_structure_valid=validation["local_structure_valid"], publish_allowed=False)
    index = practice_index(case_source) if case_source else None

    # Complete admission/validation before making an output directory. No source files are changed.
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []

    def write(name, content):
        trace.record("write", artifact=name)
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = content.encode("utf-8")
        with destination.open("xb") as stream:
            stream.write(raw)
        artifacts.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})

    def write_json(name, value):
        write(name, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")

    role_records = []
    for role, group in (("policy", briefs), ("research_and_context_not_policy", contexts), ("api_contract", [api_source]), ("evaluation_only_not_policy", [case_source] if case_source else [])):
        role_records.extend({**doc.inventory(), "role": role} for doc in group)
    write_json("sources.json", role_records)
    unique = {doc.source_id: doc for doc in documents}
    for doc in unique.values():
        write("source-text/" + doc.source_id + ".txt", doc.text)
    write("source-spans.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for doc in unique.values() for row in line_spans(doc)))
    write_json("requirements.json", [asdict(rule) for rule in policy.requirements])
    write_json("policy.ir.json", policy.serializable())
    write_json("tables.json", policy.tables)
    write_json("api-contract.json", contract.serializable())
    write_json("freya-tool-templates.json", contract.freya_templates())
    write_json("workflow-plan.json", plan)
    write_json("semantics.json", semantics)
    write_json("conversation-plan.json", conversation_plan(semantics))
    write_json("turn-contract.json", turn_contract())
    coverage_report = coverage(policy)
    coverage_report["reviewed_practice_decisions"] = len(semantics["decisions"])
    coverage_report["source_fragments_bound"] = len(semantics["atoms"])
    coverage_report["reviewed_policy_effects"] = len(semantics["constraints"])
    coverage_report["complete_practice_inventory_bound"] = semantics["complete_for_supplied_practice"]
    coverage_report["general_semantic_compiler"] = False
    write_json("coverage.json", coverage_report)
    write_json("validation.json", validation)
    if index is not None:
        write_json("practice-index.json", index)
    write("OKU.txt", (
        "BUILD THE BUILDER — YEREL TEMEL ÇIKTILARI\n\n"
        f"Kaynaklardan {len(policy.requirements)} numaralı/ham kural bloğu ve {len(policy.facts)} parametre çıkarıldı.\n"
        "Bunlar kural bloklarıdır; alt cümleler ve istisnalar semantics.json içinde kaynaklarına bağlıdır.\n"
        "Bu anlam bağlaması yalnız incelenmiş A/B/API sürümü için geçerlidir; yeni final belgesi genel olarak derlenmiş sayılmaz.\n"
        "Kaynak metinleri, satır/karakter konumları ve SHA-256 özetleri saklanmıştır.\n"
        "Araştırma/mimari notları politika girdisi değildir. Testlerin beklenen sonuçları üretime verilmez.\n\n"
        "policy.ir.json: kaynağa bağlı parametreler, tablolar ve açık kalan işler.\n"
        "workflow-plan.json: incelenecek akış tasarımı; Freya'ya yüklenebilir agent değildir.\n"
        "freya-tool-templates.json: bağlanmamış araç şablonları; çağrı veya yayın yapılmadı.\n"
        "semantics.json: incelenmiş kararlar, alt cümleler, öncelikler ve B katmanı.\n"
        "conversation-plan.json: MockBank ile çalışan yerel konuşma durumları.\n"
        "turn-contract.json: müşteri yanıtı arayüzü; belirsiz yanıt onay değildir.\n"
        "coverage.json: genel anlamsal derleyici kapsamı ile incelenmiş practice bağlaması ayrı gösterilir.\n"
        "validation.json: yerel kontroller ve yayın engelleri.\n"
        "practice-index.json: yalnızca mevcutsa, yürütülmemiş resmi örnek testlerin indeksi.\n\n"
        "API anahtarı okunmadı/kullanılmadı. Model, banka ve platform çağrısı: 0.\n"
        "Sonraki iş: final belge için genel anlamsal derleyici, Freya dışa aktarımı ve canlı platform doğrulaması.\n"
    ))
    trace.record("complete", model_calls=0, network_calls=0, api_key_read=False)
    write("trace.jsonl", "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in list(trace.events)))
    manifest = {"schema_version": "foundation-run-1", "created_utc": datetime.now(timezone.utc).isoformat(),
                "mode": "offline", "model_calls": 0, "network_calls": 0, "api_key_read": False,
                "local_structure_valid": validation["local_structure_valid"], "semantic_completion": False,
                "publish_allowed": False, "requirement_blocks": len(policy.requirements),
                "parameter_facts": len(policy.facts), "artifacts": list(artifacts)}
    write_json("run-manifest.json", manifest)
    return manifest


def verify_run(directory):
    directory = Path(directory).resolve()
    manifest = strict_json(read_source(directory / "run-manifest.json").text)
    for artifact in manifest["artifacts"]:
        target = (directory / artifact["path"]).resolve()
        if not target.is_relative_to(directory):
            raise FoundationError("Artifact path escapes run directory")
        source = read_source(target, max_bytes=20_000_000)
        if source.sha256 != artifact["sha256"] or source.byte_count != artifact["bytes"]:
            raise FoundationError(f"Artifact changed: {artifact['path']}")
    return {"artifact_hashes_valid": True, "checked_artifacts": len(manifest["artifacts"]), "publish_allowed": False,
            "note": "Local integrity check only; this is not platform or policy certification."}
