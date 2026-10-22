"""Input-only, source-bound Heart wall/coyote preservation checks.

Native observations stay in private evidence. Public results expose only case
IDs and failed predicates. This finite static geometry is not an all-map proof.
"""
from __future__ import annotations

import math
from pathlib import Path
import shutil
import struct

from kiroclops.common import ROOT
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, write_json

VERSION = "heart-wall-coyote-v2"
ADAPTER = "kiroclops_heart_integrity"
ADAPTER_SOURCE = "adapters/heart-wall-coyote-godot4-v2"
FILES = ("Integrity.tscn", "wall_coyote.gd")
BOOLS = ("floor", "wall_only", "wall", "air_jump", "jump_edge")
VECTORS = ("position", "velocity", "wall_normal")
TIMERS = ("coyote_seconds", "wall_grace_seconds")
STAGES = ("jump", "ground")


def case_specs():
    return [{"id": f"wall-coyote-{kind}-{side}", "kind": kind, "side": side,
             "start": i * 60, "duration": 60, "direction_start": 10 if kind == "overlap" else 1,
             "jump_trigger": "first_airborne_wall_with_live_coyote" if kind == "overlap" else "step_35"}
            for i, (kind, side) in enumerate((kind, side) for kind in ("overlap", "wall_only")
                                            for side in ("right", "left"))]


def protocol_record(stage):
    if stage not in STAGES:
        raise ValueError("Expected jump or ground")
    return {"id": VERSION, "stage": stage, "fps": 60, "warmup": 60, "viewport": [320, 180],
            "frame_count": 240, "cases": case_specs(), "module_sha256": sha256(Path(__file__)),
            "adapter_hashes": {name: sha256(ROOT / ADAPTER_SOURCE / name) for name in FILES},
            "response_samples": 5, "state_injection": False,
            "same_pre_state": "all cases in jump; wall-only controls in ground",
            "ground_overlap_comparison": "wall context and five exact native velocity/rights responses; approach position/timers may differ",
            "kinematics_supplied_to_visual_fit": False,
            "scope": "Input-only reachability in fixed mirrored ledge/wall geometry; launch plus four following native steps."}


def _snapshot(value):
    if not isinstance(value, dict) or set(value) != {*BOOLS, *VECTORS, *TIMERS}:
        raise ValueError("Invalid private native observation fields")
    if any(type(value[k]) is not bool for k in BOOLS):
        raise ValueError("Invalid private native predicate")
    for key in VECTORS:
        if not isinstance(value[key], list) or len(value[key]) != 2:
            raise ValueError("Invalid private native vector")
        if any(type(v) not in (float, int) or not math.isfinite(v) for v in value[key]):
            raise ValueError("Non-finite native observation")
    if any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or value[k] < 0 for k in TIMERS):
        raise ValueError("Invalid private native timer")


def _trigger(spec, steps):
    if spec["kind"] == "wall_only":
        return 35
    # A press at step N is chosen after the completed draw for step N-1.
    for observation in steps:
        p = observation["post"]
        if observation["step"] + 1 >= 11 and not p["floor"] and p["wall_only"] and p["coyote_seconds"] > 0:
            return observation["step"] + 1
    return -1


def _actions(spec, step, press):
    result = []
    if press >= 0 and press <= step < press + 4:
        result.append("jump")
    if step >= spec["direction_start"] and (press < 0 or step < press + 4):
        result.append("move_" + spec["side"])
    return result


def validate_recording(recording):
    fields = {"schema_version", "protocol", "status", "error", "fps", "warmup", "viewport", "frame_count", "frames", "cases"}
    if not isinstance(recording, dict) or set(recording) != fields:
        raise ValueError("Invalid combined recording schema")
    if (recording["schema_version"] != 1 or recording["protocol"] != VERSION or recording["status"] != "ok"
            or recording["error"] != "" or recording["fps"] != 60 or recording["warmup"] != 60
            or recording["viewport"] != [320, 180] or recording["frame_count"] != 240
            or len(recording["frames"]) != 240 or len(recording["cases"]) != 4):
        raise ValueError("Combined recording clock, protocol or count differs")
    if any(type(recording[key]) is not int for key in ("schema_version", "fps", "warmup", "frame_count")):
        raise ValueError("Combined recording numeric field type differs")
    invalid = []
    for spec, case in zip(case_specs(), recording["cases"]):
        if (set(case) != {"id", "kind", "side", "press", "steps"}
                or any(case[k] != spec[k] for k in ("id", "kind", "side"))
                or type(case["press"]) is not int or len(case["steps"]) != 59):
            raise ValueError("Combined case identity or count differs")
        for n, observation in enumerate(case["steps"], 1):
            if set(observation) != {"step", "pre", "post"} or type(observation["step"]) is not int or observation["step"] != n:
                raise ValueError("Missing/duplicated native physics step")
            _snapshot(observation["pre"])
            _snapshot(observation["post"])
        press = _trigger(spec, case["steps"])
        # Trigger at 60 is too late to produce any action in this case.
        press = -1 if press >= 60 else press
        if case["press"] != press:
            raise ValueError("Contact-triggered input differs from recorded native state")
        for step in range(60):
            n = spec["start"] + step
            expected = {"frame": n, "tick": 60 + n, "case_id": spec["id"], "step": step,
                        "actions": _actions(spec, step, press)}
            frame = recording["frames"][n]
            if frame != expected or any(type(frame[k]) is not int for k in ("frame", "tick", "step")):
                raise ValueError("Draw/physics/action mapping differs")
        checks = {}
        if not 1 <= press <= 55:
            checks["input_reaches_required_context"] = False
        else:
            pre = case["steps"][press - 1]["pre"]
            earlier = case["steps"][:press - 1]
            expected_normal = [-1.0, 0.0] if spec["side"] == "right" else [1.0, 0.0]
            checks["airborne_wall_jump_edge"] = not pre["floor"] and pre["wall_only"] and pre["wall"] and pre["jump_edge"]
            checks["wall_faces_expected_direction"] = pre["wall_normal"] == expected_normal
            if spec["kind"] == "overlap":
                checks["observed_floor_loss"] = any(s["pre"]["floor"] and not s["post"]["floor"] for s in earlier)
                checks["live_coyote_at_jump_edge"] = pre["coyote_seconds"] > 0
            else:
                checks["no_floor_or_coyote_in_control"] = pre["coyote_seconds"] == 0 and not any(s["pre"]["floor"] or s["post"]["floor"] for s in earlier)
        invalid.extend({"case_id": case["id"], "check": key} for key, value in checks.items() if not value)
    return {"status": "fail" if invalid else "pass", "case_count": 4, "invalid": invalid}


def compare_recordings(baseline, candidate, stage):
    protocol_record(stage)
    before, after = validate_recording(baseline), validate_recording(candidate)
    result = {"status": "pass", "stage": stage, "case_count": 4, "mismatches": [],
              "baseline_valid": before["status"] == "pass", "candidate_valid": after["status"] == "pass",
              "scope": protocol_record(stage)["scope"]}
    if before["status"] != "pass":
        return {**result, "status": "error", "code": "baseline_wall_coyote_fixture_invalid", "mismatches": before["invalid"]}
    result["mismatches"].extend(after["invalid"])
    invalid_cases = {m["case_id"] for m in after["invalid"]}
    for old, new in zip(baseline["cases"], candidate["cases"]):
        if new["id"] in invalid_cases:
            continue
        a = old["steps"][old["press"] - 1:old["press"] + 4]
        b = new["steps"][new["press"] - 1:new["press"] + 4]
        exact = stage == "jump" or new["kind"] == "wall_only"
        context = a[0]["pre"] == b[0]["pre"] if exact else all(
            a[0]["pre"][key] == b[0]["pre"][key] for key in ("floor", "wall", "wall_only", "wall_normal", "air_jump", "jump_edge"))
        if not context:
            result["mismatches"].append({"case_id": new["id"], "check": "same_required_pre_jump_context"})
        for index, (x, y) in enumerate(zip(a, b)):
            keys = ("velocity", "position", "floor", "wall", "wall_only", "air_jump") if exact else ("velocity", "floor", "wall", "wall_only", "air_jump")
            if any(x["post"][key] != y["post"][key] for key in keys):
                result["mismatches"].append({"case_id": new["id"], "step": index,
                                             "check": "native_wall_response_preserved"})
    result["status"] = "fail" if result["mismatches"] else "pass"
    result["failed_cases"] = len({m["case_id"] for m in result["mismatches"]})
    return result


def _validate_images(capture):
    images = {capture / "frames" / f"{n:06d}.png" for n in range(240)}
    if set((capture / "frames").glob("*.png")) != images:
        raise ValueError("Combined checkpoint set differs")
    for image in images:
        header = image.read_bytes()[:24]
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", header[16:24]) != (320, 180):
            raise ValueError("Combined checkpoint dimensions differ")


def _run_suite(source, output, stage, cancel=None):
    from . import runtime
    source, output = Path(source).resolve(), Path(output).resolve()
    original, policy = project_hashes(source), protocol_record(stage)
    if output.exists() or source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Combined integrity output must be new and separate")
    output.mkdir(parents=True)
    write_json(output / "start.json", {"source": str(source), "sources": original, "protocol": policy})
    project = copy_project(source, output / "project")
    folder = project / ADAPTER
    folder.mkdir()
    for name in FILES:
        shutil.copy2(ROOT / ADAPTER_SOURCE / name, folder / name)
    expected = {**original, **{f"{ADAPTER}/{name}": digest for name, digest in policy["adapter_hashes"].items()}}
    if project_hashes(project) != expected:
        raise ValueError("Unexpected combined fixture source changes")
    capture = output / "capture"
    (capture / "frames").mkdir(parents=True)
    config = {"protocol": VERSION, "cases": policy["cases"], "frame_count": 240, "output_dir": str(capture)}
    config_path = capture / "capture_config.json"
    write_json(config_path, config)
    imported = runtime.import_project(project, output / "import", cancel=cancel, timeout=120)
    launched = runtime.run_adapter(project, capture, scene=f"res://{ADAPTER}/Integrity.tscn", config_path=config_path, cancel=cancel, timeout=120)
    for receipt in (imported, launched):
        if receipt.get("status") != "completed" or runtime.verify_runtime_receipt(receipt.get("receipt_path", ""), project=project,
                    expected_source_hashes=runtime.source_hashes(project)).get("status") != "pass":
            raise ValueError("Combined private execution proof failed")
    observation = read_json(capture / "recording.json")
    decision = validate_recording(observation)
    _validate_images(capture)
    if project_hashes(source) != original or project_hashes(project) != expected or protocol_record(stage) != policy:
        raise ValueError("Combined fixture or source changed during execution")
    evidence = {p.relative_to(output).as_posix(): sha256(p) for p in sorted(capture.rglob("*"))
                if p.is_file() and not any(part in {"runtime", "private"} for part in p.relative_to(capture).parts)}
    record = {"version": VERSION, "stage": stage, "protocol": policy, "source": str(source), "sources": original,
              "prepared_sources": expected, "decision": decision, "import": imported, "runtime": launched,
              "evidence": evidence, "recording_sha256": sha256(capture / "recording.json")}
    write_json(output / "suite.json", record)
    return record, observation


def run_checks(project, baseline_project, output, stage, cancel=None):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Combined output already exists")
    output.mkdir(parents=True)
    try:
        before, baseline = _run_suite(baseline_project, output / "baseline", stage, cancel)
        if before["decision"]["status"] != "pass":
            raise ValueError("Baseline wall/coyote fixture did not reach its native preconditions")
        after, candidate = _run_suite(project, output / "candidate", stage, cancel)
        result = compare_recordings(baseline, candidate, stage)
        result.update(version=VERSION, sources={"baseline": before["sources"], "candidate": after["sources"]},
                      evidence={role: {"path": str(output / role / "suite.json"), "sha256": sha256(output / role / "suite.json")}
                                for role in ("baseline", "candidate")},
                      acceptance_type="preservation_only", motor_values_supplied_to_fit=False)
    except (Exception, KeyboardInterrupt) as exc:
        result = {"version": VERSION, "stage": stage, "status": "error", "code": getattr(exc, "code", type(exc).__name__),
                  "message": str(exc), "motor_values_supplied_to_fit": False}
    write_json(output / "comparison.json", result)
    return result


def verify_checks(output, *, expected_candidate_sources=None, expected_baseline_sources=None):
    """Replay exact source, fixture, observations and private runtime receipts."""
    from . import runtime
    try:
        output = Path(output).resolve()
        saved = read_json(output / "comparison.json")
        stage = saved["stage"]
        if saved.get("version") != VERSION or saved.get("status") not in ("pass", "fail"):
            raise ValueError("Completed combined preservation result required")
        observations = {}
        for role, expected_sources in (("baseline", expected_baseline_sources), ("candidate", expected_candidate_sources)):
            folder = output / role
            suite_path = folder / "suite.json"
            if saved["evidence"][role] != {"path": str(suite_path), "sha256": sha256(suite_path)}:
                raise ValueError("Combined suite identity differs")
            record = read_json(suite_path)
            original = project_hashes(Path(record["source"]))
            if (record["version"] != VERSION or record["protocol"] != protocol_record(stage) or record["stage"] != stage
                    or original != record["sources"] or original != saved["sources"][role]
                    or expected_sources is not None and original != expected_sources):
                raise ValueError("Combined source or protocol differs")
            expected = {**original, **{f"{ADAPTER}/{name}": digest for name, digest in record["protocol"]["adapter_hashes"].items()}}
            if project_hashes(folder / "project") != expected or record["prepared_sources"] != expected:
                raise ValueError("Combined prepared source differs")
            capture = folder / "capture"
            if read_json(capture / "capture_config.json") != {"protocol": VERSION, "cases": case_specs(),
                    "frame_count": 240, "output_dir": str(capture)}:
                raise ValueError("Combined capture configuration differs")
            _validate_images(capture)
            for key, directory in (("import", folder / "import"), ("runtime", folder / "capture")):
                receipt = record[key]
                receipt_path = Path(receipt["receipt_path"]).resolve()
                if not receipt_path.is_relative_to(directory):
                    raise ValueError("Combined receipt escaped suite")
                if key == "import":
                    summary_path = directory / "import-result.json"
                    if receipt.get("import_summary_path") != str(summary_path) or read_json(summary_path) != receipt:
                        raise ValueError("Combined import summary differs")
                    attempts = receipt["import_attempts"]
                    if len(attempts) not in (1, 2) or attempts[-1]["path"] != str(receipt_path) or attempts[-1]["status"] != "completed":
                        raise ValueError("Combined import sequence differs")
                    for attempt in attempts:
                        path = Path(attempt["path"]).resolve()
                        if not path.is_relative_to(directory) or sha256(path) != attempt["sha256"]:
                            raise ValueError("Combined import attempt differs")
                    receipt = {k: v for k, v in receipt.items() if k not in ("import_attempts", "import_summary_path")}
                if read_json(receipt_path) != receipt or runtime.verify_runtime_receipt(receipt_path, project=folder / "project",
                        expected_source_hashes=runtime.source_hashes(folder / "project")).get("status") != "pass":
                    raise ValueError("Combined private runtime replay failed")
            evidence = {p.relative_to(folder).as_posix(): sha256(p) for p in sorted((folder / "capture").rglob("*"))
                        if p.is_file() and not any(part in {"runtime", "private"} for part in p.relative_to(folder / "capture").parts)}
            if evidence != record["evidence"]:
                raise ValueError("Combined native evidence differs")
            observation_path = folder / "capture/recording.json"
            if sha256(observation_path) != record["recording_sha256"]:
                raise ValueError("Combined recording identity differs")
            observations[role] = read_json(observation_path)
            if validate_recording(observations[role]) != record["decision"]:
                raise ValueError("Combined native validity differs")
        result = compare_recordings(observations["baseline"], observations["candidate"], stage)
        if any(saved.get(key) != value for key, value in result.items()):
            raise ValueError("Combined saved differential result differs")
        return {"status": "pass", "preservation_status": result["status"], "stage": stage, "result": result,
                "result_path": str(output / "comparison.json"), "result_sha256": sha256(output / "comparison.json"),
                "source_binding_verified": True}
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        return {"status": "fail", "reason": str(exc)}
