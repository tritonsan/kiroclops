"""Verify pinned vendors and actual fixture/normal-controller provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_integrity() -> dict:
    sources = read(ROOT / "config/sources.json")
    vendors = {}
    for game in ("tiny", "slay"):
        project = ROOT / "vendor" / game
        # This explicitly selected workspace clone may be owned by the desktop
        # user while verification runs as CodexSandboxOffline. Scope trust to
        # this one command and repository; do not alter global Git settings.
        git = ["git", "-c", "safe.directory=" + project.as_posix(), "-C", str(project)]
        status = subprocess.check_output(
            git + ["status", "--porcelain=v1"], text=True
        ).strip()
        head = subprocess.check_output(
            git + ["rev-parse", "HEAD"], text=True
        ).strip()
        vendors[game] = {
            "head": head, "expected_head": sources[game]["commit"],
            "clean": status == "", "matches_pin": head == sources[game]["commit"],
            "status_output": status,
        }

    changes = {}
    for game, work in (("tiny", "tiny"), ("slay", "slay-before"), ("slay", "slay-after")):
        source_root = ROOT / "vendor" / game
        directory = "src" if game == "tiny" else "source"
        changed = []
        for original in (source_root / directory).rglob("*"):
            if original.is_file() and original.suffix in {".gd", ".gdshader", ".tscn", ".tres"}:
                relative = original.relative_to(source_root)
                target = ROOT / "work" / work / relative
                if not target.is_file() or digest(original) != digest(target):
                    changed.append(relative.as_posix())
        changes[work] = sorted(changed)

    provenance = {}
    for variant, normal_path in (
        ("before", "artifacts/normal/slay-before-h03/manifest.json"),
        ("after", "artifacts/normal/slay-after-h24-final/manifest.json"),
    ):
        fixture_path = f"artifacts/runs/slay-{variant}-h03-x0-r1/manifest.json"
        fixture = read(ROOT / fixture_path)
        normal = read(ROOT / normal_path)
        fixture_hashes = fixture["provenance"]["controller_hashes"]
        normal_hashes = normal["provenance"]["controller_hashes"]
        provenance[variant] = {
            "fixture_manifest": fixture_path, "normal_manifest": normal_path,
            "same_controller_hashes": fixture_hashes == normal_hashes,
            "controller_hashes": fixture_hashes,
        }
    expected_changes = {
        "source/characters/player/player.gd",
        "source/characters/player/state_machine/states/jump.gd",
    }
    passed = (
        all(value["clean"] and value["matches_pin"] for value in vendors.values())
        and changes["tiny"] == [] and changes["slay-before"] == []
        and set(changes["slay-after"]) == expected_changes
        and all(value["same_controller_hashes"] for value in provenance.values())
    )
    return {
        "schema_version": 1, "status": "passed" if passed else "failed",
        "vendor": vendors, "changed_game_source_files": changes,
        "fixture_vs_normal": provenance,
        "scope_note": "Work project settings, import caches and added capture scenes are setup changes; original game source comparison covers gd/gdshader/tscn/tres under src or source.",
    }


if __name__ == "__main__":
    report = check_integrity()
    destination = ROOT / "patches/source_integrity.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(destination)}, indent=2))
    raise SystemExit(0 if report["status"] == "passed" else 1)
