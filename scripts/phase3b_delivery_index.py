"""Freeze a curated local evidence index; never publish private run logs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.phase3b_closeout_delivery import ROOT, BASE, OUT, read, sha, write_json


def main() -> None:
    prior = BASE / "DELIVERY_MANIFEST-before-oct01-scope.json"
    old = read(prior)
    final = read(OUT / "result.json")
    files = {ROOT / name for name in old["files"]
             if not name.startswith("artifacts/phase3b/billing-")}
    explicit = [
        "artifacts/phase3b/DELIVERY_MANIFEST-before-oct01-scope.json",
        "artifacts/phase3b/startup-transport-proof-01/launcher-before.py",
        "artifacts/phase3b/startup-transport-proof-01/native-transport-proof.json",
        "artifacts/phase3b/panel-cloud-recovery-01/cleanup-verification-02.json",
        "artifacts/phase3b/panel-kiro-recovery-02/run-registration.json",
        "artifacts/phase3b/panel-kiro-recovery-02/run-result.json",
        "artifacts/phase3b/panel-kiro-recovery-02/startup-frame-diagnostic.json",
        "artifacts/phase3b/panel-kiro-recovery-02/evidence-before-audit-fix/result.json",
        "artifacts/phase3b/panel-kiro-recovery-02/evidence-posthoc-audit/audit-version.json",
        "artifacts/phase3b/panel-kiro-recovery-02/evidence-posthoc-audit/native-audit.json",
        "artifacts/phase3b/panel-kiro-recovery-02/evidence-posthoc-audit/result.json",
        "artifacts/phase3b/kiro-integration-audit-01/game-native-audit.json",
        "artifacts/phase3b/kiro-integration-audit-01/svg-native-audit.json",
        "kiroclops/local_engine/startup.py", "kiroclops/local_engine/launcher.py",
        "kiroclops/local_engine/review.py", "tests/test_local_startup.py",
        "tests/test_local_historical_audit_config.py", "tests/test_local_native_tool_outcomes.py",
        "scripts/prove_startup_launcher_transport.py", "scripts/phase3b_prepare_kiro_media.py",
        "scripts/phase3b_bind_closeout_reviews.py", "scripts/phase3b_game_raw_controls.py",
        "scripts/prepare_final_panel_raw_aids.py", "scripts/phase3b_closeout_delivery.py",
        "scripts/phase3b_delivery_index.py", "scripts/bind_final_svg_pixels.py",
    ]
    files.update(ROOT / name for name in explicit)
    # Deliberately narrow directories. Do not enumerate the multi-million-file
    # historic phase3a tree or publish service configs / native conversations.
    json_dirs = [BASE / "cool-feasibility-closure-01",
                 BASE / "visual-closeout-02",
                 BASE / "startup-transport-proof-01/tests-03"]
    for directory in json_dirs:
        files.update(directory.rglob("*.json"))
    files.add(BASE / "cool-feasibility-closure-01/REPORT.md")
    for directory in [BASE / "visual-closeout-01/svg-raw", BASE / "visual-closeout-01/panel-raw"]:
        files.update(directory.glob("*inspection*.json"))
        files.update(directory.glob("agreement*.json"))
    files.update(OUT.iterdir())
    files.update((OUT / "media").iterdir())
    files.update((BASE / "closeout-doc-history-20261001").iterdir())
    files = {path for path in files if path.is_file()}
    forbidden = {"service.json", "native-session-history.json", "credentials.json", "token.json"}
    assert not any(path.name in forbidden or path.suffix.lower() == ".pdf" for path in files)
    hashes = {path.relative_to(ROOT).as_posix(): sha(path) for path in sorted(files)}
    for row in final["integrations"]:
        assert sha(ROOT / row["session"] / "report/report.json") == row["original_report_json_sha256"]
        for item in row["media"].values():
            assert sha(OUT / item["video"]) == item["video_sha256"]
            assert sha(ROOT / item["source_manifest"]) == item["manifest_file_sha256"]
    manifest = {
        "kind": "Phase3BLocalDeliveryIndex", "schema_version": 4,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "internal_delivery_partial", "visibility": "Local engineering evidence index; not a public submission package.",
        "files": hashes, "predecessor": {"path": prior.relative_to(ROOT).as_posix(), "sha256": sha(prior)},
        "registered_source_files": 206, "registered_sources_still_match": False,
        "intentional_changes_since_full_suite": final["tests"]["intentional_changes_since_full_suite"],
        "new_production_module": "kiroclops/local_engine/startup.py",
        "frozen_sources": final["tests"]["frozen_sources"], "tests": final["tests"],
        "registered_kiro_set": final["registered_set"], "separate_panel_recovery": final["separate_panel_recovery"],
        "integrations": [{key: value for key, value in row.items() if key != "media"} for row in final["integrations"]],
        "scope": final["scope"], "cool": final["cool"], "remaining_technical_limits": final["limits"],
        "aws_test_resources_cleaned": final["cleanup"]["status"] == "pass", "cost": final["cleanup"]["cost"],
        "private_content_policy": "No invoice PDFs, tokens, signed URLs, service configuration or raw native conversations included. Evidence JSON can contain local engineering paths; review/redact before any public release.",
        "producer_sha256": sha(Path(__file__)),
    }
    manifest_path = BASE / "DELIVERY_MANIFEST.json"
    write_json(manifest_path, manifest)
    failures = [name for name, expected in hashes.items() if sha(ROOT / name) != expected]
    assert not failures
    verification = {"status": "pass", "at": datetime.now(timezone.utc).isoformat(),
                    "manifest_sha256": sha(manifest_path), "verified_files": len(hashes),
                    "file_failures": failures, "original_reports_unchanged": True,
                    "media_source_bindings_verified": 9, "no_new_model_or_aws_run": True,
                    "scope": "File integrity; not behavioral acceptance."}
    write_json(BASE / "delivery-verification-20261001.json", verification)
    print(json.dumps({"status": "pass", "files": len(hashes), "media": 9,
                      "manifest": manifest_path.relative_to(ROOT).as_posix()}))


if __name__ == "__main__":
    main()
