"""Heart preservation probes; motor observations never enter visual fitting.

This is a finite differential contract, not proof of arbitrary-map equivalence.
Both inputs are copied before running the same immutable fixture on the private
desktop. Only case names, failed predicates and source bindings leave this API.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import struct

from kiroclops.common import ROOT
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, write_json

VERSION = "heart-integrity-v1"
ADAPTER = "kiroclops_heart_integrity"
FPS = 60
WARMUP = 60
STAGES = ("jump", "ground")
HASH_KEYS = ("vx", "vy", "dx", "dy")
BOOL_KEYS = ("floor", "wall", "up", "down", "air_jump", "coyote", "wall_grace")


def case_specs(stage):
    if stage not in STAGES:
        raise ValueError("Expected stage jump or ground")
    result = [dict(id=f"air-{vx}-{vy}-{action}", kind="air", duration=8,
                   vx=vx, vy=vy, action=action)
              for vx in ("rest", "left", "right") for vy in ("rising", "falling")
              for action in ("neutral", "left", "right")]
    if stage == "jump":
        result.extend(dict(id=f"ground-{action}", kind="ground", duration=120, action=action)
                      for action in ("short", "long", "left", "reverse"))
    result.extend(dict(id=f"normal-jump-{hold}", kind="jump", duration=180, hold=hold)
                  for hold in (3, 24))
    result.append(dict(id="double-third-landing-reset", kind="double", duration=180))
    result.extend(dict(id=f"wall-{side}-{mode}", kind="wall", duration=32, side=side, mode=mode)
                  for side in ("left", "right") for mode in ("contact", "grace", "expired"))
    result.extend(dict(id=f"coyote-{mode}", kind="coyote", duration=48, mode=mode)
                  for mode in ("inside", "outside"))
    result.append(dict(id="hazard-return", kind="hazard", duration=60))
    offset = 0
    for spec in result:
        spec["start"] = offset
        offset += spec["duration"]
    return result


def protocol_record(stage):
    cases = case_specs(stage)
    return {"id": VERSION, "stage": stage, "fps": FPS, "warmup": WARMUP,
            "viewport": [320, 180], "cases": cases,
            "frame_count": sum(case["duration"] for case in cases),
            "module_sha256": sha256(Path(__file__)),
            "adapter_hashes": {name: sha256(ROOT / "adapters/heart-integrity-godot4" / name)
                               for name in ("Integrity.tscn", "integrity.gd")},
            "observations": "native float32 vx,vy,per-step-dx,per-step-dy SHA256 and predicates",
            "kinematics_supplied_to_visual_fit": False,
            "scope": "Seeded airborne and controlled contact/input preservation; normal-map reachability is separate."}


def expected_actions(spec, step):
    left = right = jump = False
    if spec["kind"] == "air" and 3 <= step <= 6:
        left, right = spec["action"] == "left", spec["action"] == "right"
    elif spec["kind"] == "ground":
        action = spec["action"]
        right = (action == "short" and 10 <= step < 16 or action == "long" and 10 <= step < 58
                 or action == "reverse" and 10 <= step < 34)
        left = action == "left" and 10 <= step < 40 or action == "reverse" and 34 <= step < 58
    elif spec["kind"] == "jump":
        jump = 10 <= step < 10 + spec["hold"]
    elif spec["kind"] == "double":
        jump = any(start <= step < end for start, end in ((10, 13), (20, 24), (27, 30), (130, 133)))
    elif spec["kind"] == "wall" and step:
        left = spec["side"] == "left" and step <= 5
        right = spec["side"] == "right" and step <= 5
        press = {"contact": 5, "grace": 8, "expired": 20}[spec["mode"]]
        jump = press <= step < press + 4
    elif spec["kind"] == "coyote":
        press = 13 if spec["mode"] == "inside" else 24
        jump = press <= step < press + 3
    return [name for name, active in (("left", left), ("right", right), ("jump", jump)) if active]


def checkpoint_frames(stage):
    return [spec["start"] + step for spec in case_specs(stage) for step in range(spec["duration"])
            if step in (3, 6, 10, 20, 27, spec["duration"] - 1)]


def _observation(value):
    if not isinstance(value, dict) or set(value) != {*HASH_KEYS, *BOOL_KEYS}:
        raise ValueError("Invalid observation schema; raw motor values are prohibited")
    if any(type(value[k]) is not bool for k in BOOL_KEYS):
        raise ValueError("Invalid predicate type")
    if any(not isinstance(value[k], str) or re.fullmatch(r"[0-9a-f]{64}", value[k]) is None for k in HASH_KEYS):
        raise ValueError("Invalid native observation digest")


def validate_recording(recording, stage):
    """Fail closed on missing ticks, cases, phases or invalid fixture conditions."""
    specs = case_specs(stage)
    required = {"schema_version", "protocol", "stage", "status", "fps", "warmup", "viewport",
                "frame_count", "frames", "cases"}
    if not isinstance(recording, dict) or set(recording) != required:
        raise ValueError("Invalid recording fields")
    total = sum(spec["duration"] for spec in specs)
    if (recording["schema_version"] != 1 or recording["protocol"] != VERSION or recording["stage"] != stage
            or recording["status"] != "ok" or recording["fps"] != FPS or recording["warmup"] != WARMUP
            or recording["viewport"] != [320, 180] or recording["frame_count"] != total
            or len(recording["frames"]) != total or len(recording["cases"]) != len(specs)):
        raise ValueError("Recording clock, protocol or case count differs")
    invalid = []
    for i, item in enumerate(recording["frames"]):
        if (set(item) != {"frame", "tick", "case_id", "step", "actions"}
                or type(item["frame"]) is not int or item["frame"] != i
                or type(item["tick"]) is not int or item["tick"] != WARMUP + i
                or not isinstance(item["actions"], list) or any(a not in ("left", "right", "jump") for a in item["actions"])
                or len(set(item["actions"])) != len(item["actions"])):
            raise ValueError("Missing/duplicated tick or malformed action")
    for spec, case in zip(specs, recording["cases"]):
        if (set(case) != {"id", "kind", "steps", "checks"} or case["id"] != spec["id"] or case["kind"] != spec["kind"]
                or len(case["steps"]) != spec["duration"] - 1
                or not isinstance(case["checks"], dict) or not case["checks"]
                or any(type(v) is not bool for v in case["checks"].values())):
            raise ValueError("Case identity, observations or predicates differ")
        required_checks = {
            "air": {"fresh_air_contacts", "seed_valid"},
            "ground": {"settled", "remained_grounded", "moved", "stopped"},
            "jump": {"settled", "took_off", "returned", "single_takeoff"},
            "double": {"settled", "first_takeoff", "second_available", "second_consumed", "third_rejected", "landed", "reset_jump"},
            "wall": {"contact_established", "jump_context", "response"},
            "coyote": {"settled", "real_contact_loss", "timer_context", "response", "rights"},
            "hazard": {"displaced", "area_contact", "returned"},
        }[spec["kind"]]
        if set(case["checks"]) != required_checks:
            raise ValueError("Required behavior checks are missing or substituted")
        for check, passed in case["checks"].items():
            if not passed:
                invalid.append({"case_id": spec["id"], "check": check})
        for j, obs in enumerate(case["steps"], 1):
            if set(obs) != {"step", "pre", "post"} or type(obs["step"]) is not int or obs["step"] != j:
                raise ValueError("Missing pre/post physics observation")
            _observation(obs["pre"])
            _observation(obs["post"])
        for j in range(spec["duration"]):
            frame = recording["frames"][spec["start"] + j]
            if (frame["case_id"] != spec["id"] or type(frame["step"]) is not int or frame["step"] != j
                    or frame["actions"] != expected_actions(spec, j)):
                raise ValueError("Frame assigned to wrong case")
        if spec["kind"] == "air":
            for obs in case["steps"]:
                if any(obs[p][c] for p in ("pre", "post") for c in ("floor", "wall")):
                    invalid.append({"case_id": spec["id"], "check": "unexpected_contact"})
    return {"status": "pass" if not invalid else "fail", "case_count": len(specs),
            "air_cases": 18, "air_steps": 72, "invalid": invalid}


def _comparison_fields(spec, step, stage):
    kind = spec["kind"]
    if kind == "air" and 3 <= step <= 6:
        return ("vx", "dx", "floor", "wall")
    if kind == "ground" and step >= 3:
        return ("vx", "dx", "floor", "wall")
    if kind == "jump" and stage == "ground" and step >= 3:
        return ("vy", "dy", "floor", "air_jump", "coyote")
    if kind == "double" and 20 <= step <= 23:
        # The redesigned first jump can put the double jump at another world
        # height; float32 subtraction then rounds dy differently. Compare the
        # native vertical response and rights, not that translation artifact.
        return ("vy", "air_jump")
    if kind == "wall":
        press = {"contact": 5, "grace": 8, "expired": 20}[spec["mode"]]
        if press <= step <= press + 3:
            return ("vx", "vy", "dx", "dy", "air_jump")
    return ()


def compare_recordings(baseline, candidate, stage):
    a = validate_recording(baseline, stage)
    b = validate_recording(candidate, stage)
    result = {"status": "pass", "stage": stage, "case_count": a["case_count"], "air_cases": 18,
              "air_steps": 72, "mismatches": [], "baseline_valid": a["status"] == "pass",
              "candidate_valid": b["status"] == "pass", "scope": protocol_record(stage)["scope"]}
    if a["status"] != "pass":
        return {**result, "status": "error", "code": "baseline_probe_invalid", "mismatches": a["invalid"]}
    result["mismatches"].extend(b["invalid"])
    for spec, before, after in zip(case_specs(stage), baseline["cases"], candidate["cases"]):
        for old, new in zip(before["steps"], after["steps"]):
            fields = _comparison_fields(spec, old["step"], stage)
            # Post-step values measure the real controller; pre-state can differ
            # at normal jump->double jump transitions when jump was redesigned.
            mismatch = [f for f in fields if old["post"][f] != new["post"][f]]
            if mismatch:
                result["mismatches"].append({"case_id": spec["id"], "step": old["step"], "fields": mismatch})
    result["status"] = "fail" if result["mismatches"] else "pass"
    result["failed_cases"] = len({m["case_id"] for m in result["mismatches"]})
    return result


def _run_suite(source, output, stage, cancel=None):
    from . import runtime
    source, output = Path(source).resolve(), Path(output).resolve()
    original = project_hashes(source)
    policy = protocol_record(stage)
    if output.exists() or source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Integrity output must be a new separate directory")
    output.mkdir(parents=True)
    write_json(output / "start.json", {"source": str(source), "sources": original, "protocol": policy})
    project = copy_project(source, output / "project")
    folder = project / ADAPTER
    if folder.exists():
        raise ValueError("Source already contains integrity adapter")
    folder.mkdir()
    for name in policy["adapter_hashes"]:
        shutil.copy2(ROOT / "adapters/heart-integrity-godot4" / name, folder / name)
    prepared = project_hashes(project)
    expected = {**original, **{f"{ADAPTER}/{name}": digest for name, digest in policy["adapter_hashes"].items()}}
    if prepared != expected:
        raise ValueError("Unexpected source changes in integrity copy")
    capture = output / "capture"
    capture.mkdir()
    (capture / "frames").mkdir()
    config_path = capture / "capture_config.json"
    write_json(config_path, {"protocol": VERSION, "stage": stage, "cases": policy["cases"],
                            "frame_count": policy["frame_count"], "output_dir": str(capture)})
    imported = runtime.import_project(project, output / "import", cancel=cancel, timeout=120)
    launched = runtime.run_adapter(project, capture, scene=f"res://{ADAPTER}/Integrity.tscn",
                                  config_path=config_path, cancel=cancel, timeout=180)
    for receipt in (imported, launched):
        if receipt.get("status") != "completed" or runtime.verify_runtime_receipt(
                receipt.get("receipt_path", ""), project=project,
                expected_source_hashes=runtime.source_hashes(project)).get("status") != "pass":
            raise ValueError("Private runtime verification failed")
    recording = read_json(capture / "recording.json")
    decision = validate_recording(recording, stage)
    expected_images = {capture / "frames" / f"{i:06d}.png" for i in checkpoint_frames(stage)}
    if set((capture / "frames").glob("*.png")) != expected_images:
        raise ValueError("Missing, duplicate or unexpected native checkpoint image")
    for path in expected_images:
        header = path.read_bytes()[:24]
        if header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) < 24 or struct.unpack(">II", header[16:24]) != (320, 180):
            raise ValueError("Invalid native checkpoint dimensions")
    if project_hashes(source) != original or project_hashes(project) != expected or protocol_record(stage) != policy:
        raise ValueError("Integrity source or fixture changed during execution")
    evidence = {p.relative_to(output).as_posix(): sha256(p) for p in sorted(capture.rglob("*"))
                if p.is_file() and not any(part in {"runtime", "private"} for part in p.relative_to(capture).parts)}
    record = {"version": VERSION, "stage": stage, "protocol": policy, "source": str(source), "sources": original,
              "prepared_sources": expected, "decision": decision, "import": imported, "runtime": launched,
              "evidence": evidence, "recording_sha256": sha256(capture / "recording.json")}
    write_json(output / "suite.json", record)
    return record, recording


def run_checks(project: Path, baseline_project: Path, output: Path, stage: str, cancel=None) -> dict:
    """Fresh immutable baseline/candidate, private runtime, compact feedback.

    A failed behavior is ``fail``; malformed capture, runtime, cancellation or
    source identity failures are ``error`` and cannot count as motion mismatch.
    """
    case_specs(stage)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Integrity output exists")
    output.mkdir(parents=True)
    try:
        before, baseline = _run_suite(baseline_project, output / "baseline", stage, cancel)
        if before["decision"]["status"] != "pass":
            raise ValueError("Baseline preservation fixture did not meet its native preconditions")
        after, candidate = _run_suite(project, output / "candidate", stage, cancel)
        result = compare_recordings(baseline, candidate, stage)
        result.update(version=VERSION, sources={"baseline": before["sources"], "candidate": after["sources"]},
                      evidence={"baseline": {"path": str(output / "baseline/suite.json"), "sha256": sha256(output / "baseline/suite.json")},
                                "candidate": {"path": str(output / "candidate/suite.json"), "sha256": sha256(output / "candidate/suite.json")}},
                      acceptance_type="preservation_only", motor_values_supplied_to_fit=False)
    except (Exception, KeyboardInterrupt) as exc:
        result = {"version": VERSION, "status": "error", "stage": stage,
                  "code": getattr(exc, "code", type(exc).__name__), "message": str(exc),
                  "acceptance_type": "preservation_only", "motor_values_supplied_to_fit": False}
    write_json(output / "comparison.json", result)
    return result


def verify_checks(output: Path, *, expected_candidate_sources=None, expected_baseline_sources=None) -> dict:
    """Read-only replay for reports; never accepts another candidate's results."""
    from . import runtime
    try:
        output = Path(output).resolve()
        saved = read_json(output / "comparison.json")
        stage = saved["stage"]
        if saved.get("version") != VERSION or saved.get("status") not in ("pass", "fail"):
            raise ValueError("A completed preservation result is required")
        records, observations = {}, {}
        for role, expected_sources in (("baseline", expected_baseline_sources), ("candidate", expected_candidate_sources)):
            folder = output / role
            suite_path = folder / "suite.json"
            binding = {"path": str(suite_path), "sha256": sha256(suite_path)}
            if saved["evidence"][role] != binding:
                raise ValueError("Suite identity/digest differs")
            record = read_json(suite_path)
            original = project_hashes(Path(record["source"]))
            if (record["protocol"] != protocol_record(stage) or record["stage"] != stage
                    or original != record["sources"] or original != saved["sources"][role]
                    or expected_sources is not None and original != expected_sources):
                raise ValueError("Original input source or protocol differs")
            prepared = project_hashes(folder / "project")
            expected_prepared = {**original, **{f"{ADAPTER}/{name}": digest
                                 for name, digest in record["protocol"]["adapter_hashes"].items()}}
            if prepared != expected_prepared or prepared != record["prepared_sources"]:
                raise ValueError("Prepared actor/fixture sources differ")
            for receipt_key, directory in (("import", folder / "import"), ("runtime", folder / "capture")):
                receipt = record[receipt_key]
                receipt_path = Path(receipt["receipt_path"]).resolve()
                if not receipt_path.is_relative_to(directory):
                    raise ValueError("Runtime receipt escaped its suite")
                if receipt_key == "import":
                    summary_path = directory / "import-result.json"
                    if receipt.get("import_summary_path") != str(summary_path) or read_json(summary_path) != receipt:
                        raise ValueError("Import attempt summary differs")
                    attempts = receipt["import_attempts"]
                    if len(attempts) not in (1, 2) or attempts[-1]["path"] != str(receipt_path) or attempts[-1]["status"] != "completed":
                        raise ValueError("Import attempt sequence differs")
                    for attempt in attempts:
                        attempt_path = Path(attempt["path"]).resolve()
                        if not attempt_path.is_relative_to(directory) or sha256(attempt_path) != attempt["sha256"]:
                            raise ValueError("Preserved import attempt evidence differs")
                    receipt = {key: value for key, value in receipt.items() if key not in ("import_attempts", "import_summary_path")}
                if read_json(receipt_path) != receipt or runtime.verify_runtime_receipt(
                        receipt_path, project=folder / "project",
                        expected_source_hashes=runtime.source_hashes(folder / "project")).get("status") != "pass":
                    raise ValueError("Private execution replay failed")
            for relative, digest in record["evidence"].items():
                path = (folder / relative).resolve()
                if not path.is_relative_to(folder) or sha256(path) != digest:
                    raise ValueError("Raw evidence digest differs")
            observation_path = folder / "capture/recording.json"
            if sha256(observation_path) != record["recording_sha256"]:
                raise ValueError("Recording digest differs")
            observations[role] = read_json(observation_path)
            if validate_recording(observations[role], stage) != record["decision"]:
                raise ValueError("Saved native predicates differ")
            records[role] = record
        result = compare_recordings(observations["baseline"], observations["candidate"], stage)
        if any(saved.get(key) != value for key, value in result.items()):
            raise ValueError("Saved differential result differs")
        return {"status": "pass", "preservation_status": result["status"], "stage": stage,
                "result_path": str(output / "comparison.json"), "result_sha256": sha256(output / "comparison.json"),
                "source_binding_verified": True}
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        return {"status": "fail", "reason": str(exc)}
