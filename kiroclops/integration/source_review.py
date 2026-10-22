"""Explicit operator source-scope decisions bound to a sealed ground candidate.

This module verifies identity and bytes, not program behavior. Only a human or
an independent assistant's static diff review can supply the three decisions.
It never derives an approval from source patterns, tracking or numerical tests.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from .experiments import GROUND_ID
from .ground_review import _local_file
from .storage import (IntegrationError, file_lock, identifier, project_hashes,
                      read_json, session_path, sha256, utc_now)

CHECKS = frozenset({"ground_only", "airborne_horizontal_preserved", "jump_paths_preserved"})
RECEIPT = "final/source-review.json"
SOURCES = frozenset({"assistant_review", "human"})


def _context(directory):
    directory = session_path(directory)
    state = read_json(_local_file(directory / "session.json", directory))
    cid = identifier(state.get("selected_candidate_id"))
    candidates = [c for c in state.get("candidates", []) if c.get("candidate_id") == cid]
    if (state.get("schema_version") != 3 or state.get("experiment_id") != GROUND_ID
            or state.get("session_id") != directory.name or state.get("status") != "finished"
            or not state.get("sealed_at") or state.get("active_job_id") is not None
            or (state.get("final") or {}).get("candidate_id") != cid or len(candidates) != 1):
        raise IntegrationError("invalid_source_review", "Source review requires one sealed, finished ground candidate")
    metadata_path = _local_file(directory / f"candidates/{cid}/candidate.json", directory)
    candidate = read_json(metadata_path)
    if candidate != candidates[0] or candidate.get("status") != "finalized":
        raise IntegrationError("invalid_source_review", "Selected candidate metadata differs from its final snapshot")
    baseline, project = directory / "baseline", metadata_path.parent / "project"
    if Path(state["baseline"]).resolve() != baseline.resolve():
        raise IntegrationError("invalid_source_review", "Baseline is not the session's prepared source snapshot")
    for root in (baseline, project):
        # Checking the real project manifest also rejects a linked root/parent.
        if not _local_file(root / "project.godot", directory).is_file():
            raise IntegrationError("invalid_source_review", "Source snapshot has no project manifest")
    before, after = project_hashes(baseline), project_hashes(project)
    if not before or before != state.get("baseline_hashes") or after != candidate.get("source_hashes"):
        raise IntegrationError("source_changed", "Baseline or selected source bytes changed")
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    return {"session_id": directory.name, "experiment_id": GROUND_ID, "candidate_id": cid,
            "baseline_source_hashes": before, "candidate_source_hashes": after,
            "candidate_metadata_sha256": sha256(metadata_path), "changed_files": changed}


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4000


def _decisions(source, reviewer, checks, files, changed):
    if source not in SOURCES or not _text(reviewer):
        raise IntegrationError("invalid_source_review", "An explicit human/assistant reviewer identity is required")
    if not isinstance(checks, dict) or set(checks) != CHECKS:
        raise IntegrationError("invalid_source_review", "Ground-only, airborne-horizontal and jump-path decisions are required")
    for decision in checks.values():
        if (not isinstance(decision, dict) or set(decision) != {"status", "reasoning"}
                or decision["status"] not in {"pass", "fail"} or not _text(decision["reasoning"])):
            raise IntegrationError("invalid_source_review", "Each source-scope decision needs an explicit verdict and reasoning")
    if not isinstance(files, list):
        raise IntegrationError("invalid_source_review", "Per-changed-file reasoning is required")
    names = []
    for entry in files:
        if (not isinstance(entry, dict) or set(entry) != {"path", "reasoning"}
                or not _text(entry["path"]) or not _text(entry["reasoning"])):
            raise IntegrationError("invalid_source_review", "Every changed file needs a path and static-review reasoning")
        names.append(entry["path"])
    if len(set(names)) != len(names) or sorted(names) != changed:
        raise IntegrationError("invalid_source_review", "Review must cover exactly every changed source file")
    return "pass" if all(check["status"] == "pass" for check in checks.values()) else "fail"


def write_source_review(directory, *, candidate_id, source, reviewer, checks, files):
    """Write once after independent static review; never replace an old verdict.

    checks maps CHECKS to {status: pass|fail, reasoning: nonempty text}.
    The ground_only reasoning must address live-input control, rather than
    recorded-coordinate playback or branches on known scenario ticks.
    files is [{path: project-relative changed path, reasoning: nonempty text}].
    No session state, sources, measurements or earlier receipts are changed.
    """
    directory = session_path(directory)
    path = _local_file(directory / RECEIPT, directory)
    lock = _local_file(directory / ".session.lock", directory)
    with file_lock(lock):
        context = _context(directory)
        if candidate_id != context["candidate_id"]:
            raise IntegrationError("invalid_source_review", "Review names a different selected candidate")
        status = _decisions(source, reviewer, checks, files, context["changed_files"])
        record = {"schema_version": 1, **context, "source": source, "reviewer": reviewer,
                  "reviewed_at": utc_now(), "checks": checks, "files": files, "status": status,
                  "method": "explicit_static_diff_review"}
        payload = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        # Close the inspection/write gap, including mutable candidate metadata.
        if _context(directory) != context:
            raise IntegrationError("source_changed", "Source snapshot changed before recording its review")
        _local_file(path, directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    return path


def verify_source_review(directory):
    """Read-only integrity verification; absent decisions remain pending."""
    try:
        directory = session_path(directory)
        path = _local_file(directory / RECEIPT, directory)
        if not path.exists():
            return {"status": "pending", "reason": "Independent ground/air/jump source review has not been recorded"}
        record = read_json(path)
        context = _context(directory)
        if (record.get("schema_version") != 1 or record.get("method") != "explicit_static_diff_review"
                or any(record.get(key) != value for key, value in context.items())):
            raise IntegrationError("invalid_source_review", "Receipt belongs to different source bytes, session or candidate")
        reviewed_at = datetime.fromisoformat(record["reviewed_at"].replace("Z", "+00:00"))
        if reviewed_at.tzinfo is None:
            raise IntegrationError("invalid_source_review", "Review timestamp must include its timezone")
        status = _decisions(record.get("source"), record.get("reviewer"), record.get("checks"),
                            record.get("files"), context["changed_files"])
        if record.get("status") != status:
            raise IntegrationError("invalid_source_review", "Saved verdict differs from explicit review decisions")
        return {"status": status, "candidate_id": context["candidate_id"], "session_id": context["session_id"],
                "source": record["source"], "reviewer": record["reviewer"], "checks": record["checks"],
                "changed_files": context["changed_files"], "receipt_path": RECEIPT, "receipt_sha256": sha256(path),
                "baseline_source_hashes": context["baseline_source_hashes"],
                "candidate_source_hashes": context["candidate_source_hashes"],
                "note": "Explicit static review; source hashes verify its binding, not the truth of its judgments."}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError) as exc:
        return {"status": "fail", "reason": f"Source review verification failed: {type(exc).__name__}: {exc}"}
