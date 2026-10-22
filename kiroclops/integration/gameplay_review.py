"""Additive wall-launch proof for an already sealed, unchanged candidate.

The original cohort's recipe and eligibility results remain untouched. A new
capture presses jump while the character is still sliding; an adjacent runtime
WallJump -> Jump transition is required, never a later grounded jump.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import runner
from .session import Session
from .storage import IntegrationError, file_lock, identifier, project_hashes, read_json, sha256, utc_now, write_json

EXTRA_INPUTS = [
    {"tick": 5, "action": "ui_right", "pressed": True},
    {"tick": 80, "action": "ui_right", "pressed": False},
    {"tick": 55, "action": "ui_up", "pressed": True},
    {"tick": 58, "action": "ui_up", "pressed": False},
]
COMMAND_TICK = 115  # 60 warmup physics ticks + capture tick 55.


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _context(directory):
    session = Session(Path(directory).resolve())
    directory = session.directory
    state = session.read()
    cid = state.get("selected_candidate_id")
    if (state.get("status") != "finished" or not state.get("sealed_at") or not cid
            or state.get("active_job_id") or state.get("final", {}).get("candidate_id") != cid):
        raise IntegrationError("review_not_ready", "Supplemental gameplay review requires a finished sealed candidate and no active job")
    root = directory / "candidates" / identifier(cid)
    candidate = read_json(root / "candidate.json")
    saved = next((item for item in state.get("candidates", []) if item.get("candidate_id") == cid), None)
    if candidate != saved or candidate.get("status") != "finalized":
        raise IntegrationError("experiment_integrity", "Selected candidate metadata is inconsistent")
    baseline = directory / "baseline"
    if (Path(state["baseline"]).resolve() != baseline or not state.get("baseline_hashes")
            or project_hashes(baseline) != state["baseline_hashes"]
            or not candidate.get("source_hashes") or project_hashes(root / "project") != candidate["source_hashes"]):
        raise IntegrationError("experiment_integrity", "Sealed baseline/candidate source bytes changed")
    return directory, root, candidate


def wall_transition_checks(trace):
    """Replay adjacent state changes; logging occurs before each physics update."""
    ordered = bool(trace) and all(
        type(event.get("physics_tick")) is int and isinstance(event.get("state"), str)
        and (index == 0 or event["physics_tick"] > trace[index - 1]["physics_tick"])
        for index, event in enumerate(trace))
    transition, landed = None, False
    if ordered:
        for index in range(1, len(trace)):
            previous, current = trace[index - 1], trace[index]
            if (previous["state"] == "WallJump" and current["state"] == "Jump"
                    and 60 <= previous["physics_tick"] < current["physics_tick"]
                    and COMMAND_TICK <= current["physics_tick"] <= COMMAND_TICK + 2):
                transition = {"wall_tick": previous["physics_tick"], "jump_tick": current["physics_tick"],
                              "command_physics_tick": COMMAND_TICK}
                remaining = trace[index + 1:]
                landed = any(event["state"] == "Fall" and any(later["state"] == "Idle" for later in remaining[j + 1:])
                             for j, event in enumerate(remaining))
                break
    checks = {"ordered_trace": ordered, "adjacent_wall_to_jump_at_input": transition is not None,
              "fall_and_ground_after_launch": landed}
    return checks, transition


def _expected_config(run):
    return {"output_dir": str(run), "hold_ticks": 3, "start_tick": 30, "warmup_ticks": 60,
            "frame_count": 240, "spawn_offset": 0, "normal_level": False,
            "smoke_wall": True, "extra_inputs": EXTRA_INPUTS}


def _capture_evidence(directory, run, expected):
    manifest_path, log_path, config_path = run / "manifest.json", run / "engine.log", run / "capture_config.json"
    manifest, config = read_json(manifest_path), read_json(config_path)
    if config != _expected_config(run):
        raise IntegrationError("wall_protocol_changed", "Supplemental wall capture must use the exact earlier-jump input recipe")
    if (manifest.get("status") != "ok" or manifest.get("game") != "slay" or manifest.get("variant") != "after"
            or manifest.get("fps") != 60 or manifest.get("frame_count") != 240 or manifest.get("normal_level") is not False
            or manifest.get("viewport") != [640, 360] or manifest.get("settled") is not True
            or manifest.get("scenario") != {"hold_ticks": 3, "start_tick": 30, "split": "calibration", "repeat": 1, "spawn_offset": 0}
            or any(manifest.get("provenance", {}).get(key) != expected for key in
                   ("full_source_hashes_before", "full_source_hashes_after"))):
        raise IntegrationError("experiment_integrity", "Supplemental capture source or protocol does not match the sealed candidate")
    runner.validate_recording(manifest, config, "slay")
    runner._validate_actions(manifest, config)
    evidence = {name: {"path": path.relative_to(directory).as_posix(), "sha256": sha256(path)}
                for name, path in (("capture_config", config_path), ("manifest", manifest_path), ("engine_log", log_path))}
    sequence = [sha256(run / "frames" / f"{index:06d}.png") for index in range(240)]
    trace = runner._state_trace(run)
    checks, transition = wall_transition_checks(trace)
    return {"evidence": evidence, "raw_frames_sha256": _digest(sequence), "state_trace": trace,
            "checks": checks, "transition": transition}


def review_wall_behavior(directory, *, cancel=None):
    """Run one additive probe. Do not call while the cohort's captures are active."""
    directory, root, candidate = _context(directory)
    with file_lock(directory / ".supplemental-gameplay.lock"):
        directory, root, candidate = _context(directory)
        receipt_path = directory / "final/gameplay-review.json"
        run = root / "supplemental/wall"
        if receipt_path.exists() or run.exists():
            raise IntegrationError("review_exists", "Existing supplemental gameplay evidence is never overwritten")
        before = project_hashes(root / "project")
        receipt = {"schema_version": 1, "kind": "supplemental_wall_launch", "candidate_id": candidate["candidate_id"],
                   "reviewed_at": utc_now(), "source_hashes": candidate["source_hashes"],
                   "full_source_hashes_before": before, "run_dir": run.relative_to(directory).as_posix(),
                   "scope": "Earlier input while sliding; adjacent WallJump to Jump and later Fall/Idle. Does not prove original wall-jump speed, trajectory or full-game completion."}
        try:
            runner.capture_project(root / "project", run, hold_ticks=3, variant="after", repeat=1,
                spawn_offset=0, normal_level=False,
                extra_config={"smoke_wall": True, "extra_inputs": EXTRA_INPUTS}, cancel=cancel, video=True)
            receipt.update(_capture_evidence(directory, run, before))
            receipt["status"] = "passed" if all(receipt["checks"].values()) else "failed"
        except Exception as exc:
            receipt.update(status="error", error={"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)})
        after = project_hashes(root / "project")
        receipt["full_source_hashes_after"] = after
        if after != before:
            receipt.update(status="error", error={"code": "experiment_integrity", "message": "Candidate source changed during supplemental capture"})
        write_json(receipt_path, receipt)
        return receipt


def verify_wall_review(directory):
    """Read-only benchmark gate; rehash sources and evidence and replay states."""
    directory = Path(directory).resolve()
    receipt_path = directory / "final/gameplay-review.json"
    if not receipt_path.exists():
        return {"status": "unverified", "reason": "No supplemental wall-launch review is recorded"}
    try:
        directory, root, candidate = _context(directory)
        receipt = read_json(receipt_path)
        if (receipt.get("schema_version") != 1 or receipt.get("kind") != "supplemental_wall_launch"
                or receipt.get("candidate_id") != candidate["candidate_id"]
                or any(receipt.get(key) != candidate["source_hashes"] for key in
                       ("source_hashes", "full_source_hashes_before", "full_source_hashes_after"))):
            raise IntegrationError("experiment_integrity", "Supplemental review belongs to different candidate bytes")
        if receipt.get("status") not in {"passed", "failed"}:
            return {"status": "fail", "reason": "Supplemental wall probe did not complete", "error": receipt.get("error")}
        run = root / "supplemental/wall"
        if receipt.get("run_dir") != run.relative_to(directory).as_posix():
            raise IntegrationError("invalid_evidence", "Supplemental wall evidence path is not the fixed selected-candidate location")
        verified = _capture_evidence(directory, run, candidate["source_hashes"])
        if any(receipt.get(key) != value for key, value in verified.items()):
            raise IntegrationError("evidence_changed", "Supplemental wall source/log/frame evidence changed or its conclusions do not replay")
        passed = all(verified["checks"].values())
        if receipt["status"] != ("passed" if passed else "failed"):
            raise IntegrationError("evidence_changed", "Supplemental wall status contradicts its recorded transitions")
        return {"status": "pass" if passed else "fail", "reason": "Adjacent wall-launch transition verified" if passed else "The earlier-input probe did not produce a direct wall launch",
                "candidate_id": candidate["candidate_id"], "receipt_path": str(receipt_path),
                "receipt_sha256": sha256(receipt_path), "checks": verified["checks"], "transition": verified["transition"]}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {"status": "fail", "reason": str(exc), "error_code": getattr(exc, "code", type(exc).__name__)}
