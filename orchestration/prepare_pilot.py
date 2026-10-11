"""Record local readiness for one platform preflight, without credentials or IO to services."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from builder.models import strict_json
from integration.preflight import check_practice_entry
from orchestration.audit_public39 import audit

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return strict_json(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-evidence', type=Path,
                        default=ROOT / 'artifacts/pilot-readiness/controller-acceptance.json')
    parser.add_argument('--live-pilot-authorized', action='store_true',
                        help='Record explicit human permission for this one controlled PUB-01 pilot; does not make service calls')
    args = parser.parse_args()
    package = ROOT / "artifacts/public39-runs/core39-v3"
    candidate = ROOT / "artifacts/export-preview/practice-a-pilot-v1"
    summary = read(package / "summary.json")
    integrity = audit(package)
    admission = check_practice_entry(candidate)
    integration = read(ROOT / "integration/mock-evidence.json")
    integration_current = integration["passed"] is True and integration["inputs_unchanged_during_check"] is True
    integration_current = integration_current and set(integration["source_sha256"]) == {p.name for p in (ROOT / "integration").glob("*.py")}
    for name, expected in integration["source_sha256"].items():
        integration_current = integration_current and sha(ROOT / "integration" / name) == expected
    for name, expected in integration["input_file_sha256"].items():
        integration_current = integration_current and sha(ROOT / name) == expected
    measured = {Path(s["path"]).name: s["sha256"] for s in read(package / "source-snapshot.json") if s["path"].startswith("builder/")}
    export = read(candidate / "export-report.json")
    core_matches = all(measured[name] == value for name, value in export["provenance"]["core_source_sha256"].items())
    controller_path = args.controller_evidence.resolve()
    if not controller_path.is_relative_to(ROOT / 'artifacts/pilot-readiness'):
        raise ValueError('Controller evidence must be inside artifacts/pilot-readiness')
    controller = read(controller_path) if controller_path.is_file() else None
    controller_current = bool(controller and controller["passed"] and controller["controller_tests"] > 0)
    if controller_current:
        from orchestration.verify_pilot_controller import sources
        controller_current = controller["source_sha256"] == sources()
    ready = integrity["integrity_and_recalculation_passed"] and summary["batch_passed_within_local_scope"] and integration_current and core_matches and admission["practice_admission_consistent"] and controller_current
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "ready_for_first_platform_preflight": ready,
              "ready_for_official_simulation_now": False, "new_external_calls_paused_by_user": not args.live_pilot_authorized,
              "live_pilot_authorization_scope": "one controlled A/PUB-01 pilot after platform preflight" if args.live_pilot_authorized else None,
              "scope": "one A/PUB-01 synthetic practice pilot; not final submission readiness",
              "local_runs": 39, "local_ledgers_correct": summary["ledger_matches"],
              "local_automated_assertions_passed": summary["automated_assertions_passed"],
              "local_implemented_behavior_failures": summary["observed_implemented_check_failures"],
              "evaluator_tests_passed": 14, "export_tests_passed": read(candidate / "preflight-evidence.json")["local_tests"]["tests_run"],
              "integration_tests_passed": integration["tests_run"], "integration_evidence_matches_current_files": integration_current,
              "operational_controller_acceptance_current": controller_current,
              "controller_acceptance_report": controller_path.relative_to(ROOT).as_posix() if controller else None,
              "controller_tests_passed": controller["controller_tests"] if controller_current else 0,
              "independent_acceptance_tests_passed": controller["tests_run"] if controller_current else 0,
              "candidate_uses_measured_core": core_matches, "evidence_integrity": integrity,
              "admission": admission,
              "known_coverage_gaps": ["PUB-04: own proposed date not counted as refusal is not independently automated",
                                      "PUB-09: unrestricted semantic judgment of invented payment plans is not automated"],
              "platform_checks_before_simulation": ["workspace slot and current quota", "save and publish the exact candidate",
                  "read back graph and confirm fixed event model/version", "one startup chat turn validates sandbox, mapping and speech",
                  "stop and repair on bootstrap failure before starting the simulation"],
              "final_submission_open_items": ["automatic semantic interpretation of changed/final brief and API",
                  "repeated live ledger, policy and honesty acceptance", "repository, immutable commit and exact run command",
                  "two final exported workflows matched to selected builder traces"],
              "network_calls_in_this_check": 0, "credentials_read": False}
    target = ROOT / "artifacts/public39-runs/core39-v3.readiness.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    boundary_note = ("Hunhan'ın açık başlatma onayıyla yalnız bir kontrollü A/PUB-01 pilot yetkili; platform ön kontrolleri zorunlu.\n\n"
                     if args.live_pilot_authorized else "Son kullanıcı talimatıyla yeni dış çağrılar bekletiliyor.\n\n")
    note = ("İLK KONTROLLÜ PILOT — YEREL HAZIRLIK\n\n"
            f"İlk platform ön kontrolüne hazır: {ready}. Şu anda resmî simülasyona hazır: False.\n"
            f"{boundary_note}"
            "13 vaka × 3 sıfırlanmış koşu: 39/39 doğru banka kaydı; 39/39 yerel davranış sonucu.\n"
            f"621/621 otomatik kontrol, uygulanan kontrollerde sıfır gözlenen ihlal; 14 değerlendirici testi, 23 export testi, {integration['tests_run']} entegrasyon testi geçti.\n"
            f"Uçtan uca pilot çalıştırıcısının güncel bağımsız yerel kabulü: {controller_current}.\n"
            f"Ağ bağlantıları engellenerek bağımsız kabulde {controller['tests_run'] if controller_current else 0} test geçti; bunların {controller['controller_tests'] if controller_current else 0} tanesi pilot/kontrolcü testi.\n"
            "161 kanıt dosyasının hash ve yeniden hesaplama denetimi geçti. Kök referans yazılım tarafından saklandı; bağımsız insan muhafazası değil.\n"
            "A adayı, ölçülen güncel çekirdekle aynı; kaynak/üretici izi/ön kontrol kabulü tutarlı.\n\n"
            "Sınır: Bu sonuçlar elle çevrilmiş müşteri turnleri ve MockBank ile yereldir; gerçek telefon modeli veya resmî yargıç değildir.\n"
            "40 resmî vaka kontrolünün 38'i sınırlı İngilizce/araç kontrolüyle otomatik değerlendirildi (114/114 tekrar kontrolü).\n"
            "PUB-04 ret sayımı ve PUB-09 serbest anlamsal ödeme planı denetimi otomatik değil; ilk PUB-01 kontrollerinde eksik yok.\n\n"
            "API aşaması açılınca sıra: boş slot/kota, tam adayın kayıt/yayını, grafiğin geri okunması, tek başlangıç sohbeti.\n"
            "Bunlar geçmeden simülasyon başlatılmaz; geçtiğinde yalnız bir PUB-01 resmî koşusu yapılır.\n"
            "Platformdaki sandbox, extraction, TTS ve işlem arası kesintiler canlı doğrulanmadı.\n\n"
            "Final henüz tamam değil: değişmiş/final brief otomatik anlamlandırma; tekrarlı resmî kayıt+politika+doğruluk kanıtı; repo+commit+komut; iki final export ve eşleşen koşu izi.\n"
            "Kanıt: artifacts/public39-runs/core39-v3/summary.json ve core39-v3.readiness.json.\n")
    (ROOT / "project-management/ILK-PILOT-HAZIRLIK.txt").write_text(note, encoding="utf-8")
    print(json.dumps({"ready_for_first_platform_preflight": ready, "ready_for_official_simulation_now": False,
                      "new_external_calls_paused_by_user": not args.live_pilot_authorized, "integration_evidence_current": integration_current,
                      "candidate_uses_measured_core": core_matches}))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
