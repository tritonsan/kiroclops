"""Prepare, then explicitly run one supplemental probe after the model run ends.

This standalone operator script does not change a session or its acceptance.
The prepared copy is external; engine execution is only in the `run` action.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.integration import background_capture_v1 as background
from kiroclops.integration import movement_probe
from kiroclops.integration.import_cache_v1 import seed_import_cache
from kiroclops.integration.isolated_runtime_v2 import isolated_runtime, verify_runtime_receipt
from kiroclops.integration.repair_workflow_v2 import verify_protocol
from kiroclops.integration.storage import copy_project, project_hashes, sha256


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _code_hashes():
    names = ["scripts/run_repair_candidate_probe.py",
        "kiroclops/integration/movement_probe.py", "kiroclops/integration/background_capture_v1.py",
        "kiroclops/integration/isolated_runtime_v2.py", "kiroclops/integration/private_desktop_v1.py",
        "kiroclops/integration/import_cache_v1.py", "kiroclops/integration/runner.py",
        "kiroclops/integration/storage.py", "adapters/movement-probe-godot4/Capture.tscn",
        "adapters/movement-probe-godot4/capture.gd"]
    return {name: sha256(ROOT / name) for name in names}


def _context(session, candidate_id, *, require_stopped):
    session = background._ordinary(session)
    if session.parent != (ROOT / "sessions").resolve():
        raise ValueError("Expected one workspace session directory")
    if candidate_id not in {"candidate-01", "candidate-02", "candidate-03"}:
        raise ValueError("Invalid candidate ID")
    protocol = verify_protocol(session)
    state = _read(session / "session.json")
    candidate = _read(session / "candidates" / candidate_id / "candidate.json")
    if (state.get("selected_candidate_id") != candidate_id or not state.get("sealed_at")
            or candidate.get("candidate_id") != candidate_id
            or candidate.get("status") not in {"evaluated", "finalized"}):
        raise ValueError("Expected the sealed, evaluated selected candidate")
    source = session / "candidates" / candidate_id / "project"
    expected = candidate["source_hashes"]
    if project_hashes(source) != expected:
        raise ValueError("Selected candidate source changed")
    launch = _read(session / "kiro/launch.json")
    if require_stopped:
        if launch.get("status") not in {"completed", "failed", "timeout", "cancelled"}:
            raise ValueError("Main model run is still active; do not start another engine")
        if state.get("active_job_id") is not None:
            raise ValueError("Session still records an active job")
        for path in (session / "jobs").glob("*.json"):
            if _read(path).get("status") not in {"succeeded", "failed", "cancelled"}:
                raise ValueError("A session job is not terminal")
    return source, expected, protocol, state, launch


def _output(path):
    path = background._ordinary(path)
    allowed = (ROOT / "artifacts/model-trials/reviews").resolve()
    if path == allowed or not path.is_relative_to(allowed):
        raise ValueError("Use a fresh output beneath artifacts/model-trials/reviews")
    return path


def prepare(session, output, candidate_id="candidate-01"):
    session = Path(session).resolve()
    output = _output(output)
    if output.exists():
        raise ValueError("Refusing to overwrite an existing supplemental directory")
    source, expected, protocol, state, _ = _context(session, candidate_id, require_stopped=False)
    output.mkdir(parents=True)
    project = copy_project(source, output / "project")
    if project_hashes(project) != expected or project_hashes(source) != expected:
        raise ValueError("The copied source does not match the immutable candidate")
    record = {"schema_version": 1, "kind": "supplemental_selected_candidate_probe",
        "status": "prepared_no_engine", "prepared_at": datetime.now(timezone.utc).isoformat(),
        "session": str(session), "candidate_id": candidate_id, "source_project": str(source),
        "source_hashes": expected, "sealed_at": state["sealed_at"], "output": str(output),
        "project": str(project), "code_hashes": _code_hashes(),
        "protocol_sha256": sha256(session / "execution-protocol.json"),
        "asset_cache": protocol["asset_cache"],
        "probe": "run_jump_land_brake", "normal_level": True, "video": True,
        "canonical_experiment_acceptance": False,
        "note": "Run only after the operator confirms the main model run ended. The run action independently refuses nonterminal launch/jobs."}
    _write(output / "setup.json", record)
    return record


def run_prepared(output):
    output = _output(output)
    setup_path = output / "setup.json"
    setup = _read(setup_path)
    if (setup.get("kind") != "supplemental_selected_candidate_probe"
            or setup.get("status") != "prepared_no_engine"
            or setup.get("output") != str(output)
            or setup.get("project") != str(output / "project")
            or setup.get("probe") != "run_jump_land_brake"
            or setup.get("normal_level") is not True or setup.get("video") is not True
            or setup.get("canonical_experiment_acceptance") is not False
            or setup.get("code_hashes") != _code_hashes()):
        raise ValueError("Prepared supplemental identity or implementation changed")
    source, expected, protocol, state, launch = _context(
        Path(setup["session"]), setup["candidate_id"], require_stopped=True)
    if (expected != setup["source_hashes"] or str(source) != setup["source_project"]
            or state["sealed_at"] != setup["sealed_at"]
            or sha256(Path(setup["session"]) / "execution-protocol.json") != setup["protocol_sha256"]
            or protocol["asset_cache"] != setup["asset_cache"]):
        raise ValueError("The selected source or registered cache differs from preparation")
    project = output / "project"
    if project_hashes(project) != expected or (project / ".godot").exists():
        raise ValueError("Expected an untouched, fresh disposable source copy")
    if any((output / name).exists() for name in ("run-start.json", "seed.json", "capture", "result.json", "failure.json")):
        raise ValueError("Supplemental attempt already started; use a new output")
    started = datetime.now(timezone.utc).isoformat()
    _write(output / "run-start.json", {"started_at": started, "setup_sha256": sha256(setup_path),
        "source_hashes": expected, "launch_status_observed": launch["status"],
        "session_status_observed": state["status"], "canonical_experiment_acceptance": False})
    try:
        cache = setup["asset_cache"]
        seed = seed_import_cache(cache["cache_dir"], project,
            engine=ROOT / ".tools/godot4/Godot_v4.5.1-stable_win64.exe",
            expected_source_hashes=expected, expected_manifest_sha256=cache["manifest_sha256"])
        _write(output / "seed.json", seed)
        # Only this external worker's shared runner is intercepted. The original
        # recorder still performs its normal headless import and fixed input run.
        with isolated_runtime(project_roots=[output]):
            capture = movement_probe.run_probe(project, output / "capture",
                disposable_root=output, expected_source_hashes=expected,
                probe="run_jump_land_brake", normal_level=True, video=True, timeout=120)
        replay = movement_probe.verify_probe(project, capture, expected_source_hashes=expected)
        proof = _read(capture / "probe.json")
        private = verify_runtime_receipt(capture / "engine.isolated-runtime-v2/bridge.json",
            project=project, expected_source_hashes=proof["full_source_hashes_after"])
        if private.get("status") != "pass":
            raise ValueError("Private desktop proof failed")
        if project_hashes(source) != expected:
            raise ValueError("Original selected source changed during supplemental run")
        video = capture / "video.mp4"
        if not video.is_file() or video.stat().st_size == 0:
            raise ValueError("Normal-level video missing")
        record = {"status": "pass" if replay.get("status") == "pass" else "fail",
            "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
            "session": setup["session"], "candidate_id": setup["candidate_id"],
            "setup_sha256": sha256(setup_path), "source_hashes": expected,
            "probe_verification": replay, "private_runtime_verification": private,
            "video": str(video), "video_sha256": sha256(video),
            "canonical_experiment_acceptance": False,
            "scope": "Supplemental fixed run/jump/land/brake sequence on the original normal level; no trajectory or full-game acceptance."}
        _write(output / "result.json", record)
        return record
    except BaseException as error:
        _write(output / "failure.json", {"status": "error", "error": str(error),
            "error_type": type(error).__name__, "canonical_experiment_acceptance": False})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run"])
    parser.add_argument("--session")
    parser.add_argument("--candidate", default="candidate-01")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        if not args.session:
            parser.error("prepare requires --session")
        session = Path(args.session)
        if session.parent == Path("."):
            session = ROOT / "sessions" / session
        result = prepare(session, args.output, args.candidate)
    else:
        result = run_prepared(args.output)
    print(json.dumps({key: result[key] for key in ("status", "project", "video") if key in result}, ensure_ascii=False))
    return 0 if result["status"] in {"prepared_no_engine", "pass"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
