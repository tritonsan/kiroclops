"""Opt-in seeded airborne differential integrity, isolated from visual fitting.

One new private process per suite, eighteen freshly instantiated actors. This
supplemental protocol does not replace canonical fresh-process motion captures
or prove that seeded states are naturally reached in the normal game map.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import struct

from ..common import ROOT
from . import runner
from . import isolated_runtime_v3 as runtime
from . import import_cache_v1 as asset_cache_module
from .storage import copy_project, project_hashes, read_json, sha256, write_json

VERSION = "airborne-regression-v1"
FPS, WARMUP, FRAME_COUNT, STEPS = 60, 60, 240, 4
EPSILON = 0.000001
ADAPTER_DIR = "kiroclops_movement_probe"
ADAPTER_SOURCE = "adapters/airborne-regression-v1-godot4"
CASES = tuple({"case_id": f"{state.lower()}-{motion}-{action}", "state": state,
               "motion": motion, "input": action}
              for state in ("Idle", "Run", "Jump")
              for motion in ("rising", "falling")
              for action in ("neutral", "left", "right"))
SNAPSHOT_KEYS = {"state", "on_floor", "on_wall", "horizontal_rest", "moving_up", "moving_down",
                 "actions", "jump_just_pressed", "horizontal_response_sha256"}
PREDICATES = {"horizontal_velocity_unchanged", "horizontal_position_unchanged", "horizontal_speed_increased",
              "horizontal_speed_decreased", "horizontal_direction_changed", "vertical_direction_changed"}
STATES = {"Idle", "Run", "Jump", "Fall", "DoubleJump", "WallJump", "Appear", "Hit", "None"}
SuiteError = runner.RunnerError


def _adapter_hashes():
    return {name: sha256(ROOT / ADAPTER_SOURCE / name) for name in ("Capture.tscn", "capture.gd")}


def protocol_record():
    return {"id": VERSION, "fps": FPS, "warmup_ticks": WARMUP, "frame_count": FRAME_COUNT,
            "viewport": [640, 360], "cases": list(CASES), "observed_steps_per_case": STEPS,
            "contact_refresh_steps": 2, "case_spacing_ticks": 8, "first_observation_offset": 3,
            "epsilon": EPSILON, "entry_kind": "seeded_controller_contract",
            "horizontal_response_encoding": "sha256_of_native_float32_little_endian_velocity_x",
            "module_sha256": sha256(Path(__file__)), "adapter_hashes": _adapter_hashes(),
            "runtime_module_sha256": sha256(Path(runtime.__file__)), "runtime_policy": runtime.PRESENTATION_POLICY,
            "asset_cache_module_sha256": sha256(Path(asset_cache_module.__file__)),
            "fresh_private_processes_per_suite": 1, "fresh_actors_per_suite": len(CASES),
            "kinematics_supplied_to_visual_fit": False,
            "scope": "Finite seeded airborne contract; not normal-map reachability or complete gameplay equivalence."}


def _write_new(path, data):
    path = runtime._plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _actions(frame):
    case_index, offset = divmod(frame, 8)
    if case_index < len(CASES) and 3 <= offset <= 6:
        action = CASES[case_index]["input"]
        return [] if action == "neutral" else [action]
    return []


def _snapshot(value):
    if (not isinstance(value, dict) or set(value) != SNAPSHOT_KEYS or value["state"] not in STATES
            or any(type(value[k]) is not bool for k in SNAPSHOT_KEYS - {"state", "actions", "horizontal_response_sha256"})
            or not isinstance(value["actions"], list) or value["actions"] not in ([], ["left"], ["right"])
            or not isinstance(value["horizontal_response_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", value["horizontal_response_sha256"]) is None):
        raise SuiteError("invalid_recording", "Unexpected predicate schema; raw kinematics are prohibited")


def evaluate_recording(recording):
    """Validate exact schema/timing and seeded preconditions, never fit motion."""
    required = {"schema_version", "protocol", "status", "fps", "viewport", "frame_count", "warmup_ticks", "frames", "cases"}
    if (not isinstance(recording, dict) or set(recording) != required or recording["schema_version"] != 1
            or recording["protocol"] != VERSION or recording["status"] != "ok"
            or recording["fps"] != FPS or recording["viewport"] != [640, 360]
            or recording["frame_count"] != FRAME_COUNT or recording["warmup_ticks"] != WARMUP
            or not isinstance(recording["frames"], list) or len(recording["frames"]) != FRAME_COUNT
            or not isinstance(recording["cases"], list) or len(recording["cases"]) != len(CASES)):
        raise SuiteError("invalid_recording", "Suite clock, native viewport, case count or fields differ")
    for i, frame in enumerate(recording["frames"]):
        if (not isinstance(frame, dict) or set(frame) != {"frame_index", "physics_tick", "time_s", "actions"}
                or type(frame["frame_index"]) is not int or frame["frame_index"] != i
                or type(frame["physics_tick"]) is not int or frame["physics_tick"] != WARMUP + i
                or type(frame["time_s"]) not in (int, float) or not math.isfinite(frame["time_s"])
                or abs(frame["time_s"] - i / FPS) > EPSILON or frame["actions"] != _actions(i)):
            raise SuiteError("invalid_recording", "Input or physics/render mapping differs")
    invalid = []
    for index, (case, spec) in enumerate(zip(recording["cases"], CASES)):
        if (not isinstance(case, dict) or set(case) != {"case_id", "contact_refresh", "steps"}
                or case["case_id"] != spec["case_id"] or not isinstance(case["contact_refresh"], list)
                or len(case["contact_refresh"]) != 2 or not isinstance(case["steps"], list) or len(case["steps"]) != STEPS):
            raise SuiteError("invalid_recording", "Case identity or observation count differs")
        for offset, refresh in enumerate(case["contact_refresh"], 1):
            if (not isinstance(refresh, dict) or set(refresh) != {"frame_index", "airborne", "no_wall"}
                    or type(refresh["frame_index"]) is not int or refresh["frame_index"] != 8 * index + offset
                    or type(refresh["airborne"]) is not bool or type(refresh["no_wall"]) is not bool):
                raise SuiteError("invalid_recording", "Real contact refresh observation is missing")
            if not refresh["airborne"] or not refresh["no_wall"]:
                invalid.append({"case_id": spec["case_id"], "stage": "contact_refresh", "step_index": offset})
        for step_index, step in enumerate(case["steps"]):
            frame = 8 * index + 3 + step_index
            if (not isinstance(step, dict) or set(step) != {"step_index", "frame_index", "physics_tick", "pre", "post", "predicates"}
                    or type(step["step_index"]) is not int or step["step_index"] != step_index
                    or type(step["frame_index"]) is not int or step["frame_index"] != frame
                    or type(step["physics_tick"]) is not int or step["physics_tick"] != WARMUP + frame
                    or not isinstance(step["predicates"], dict) or set(step["predicates"]) != PREDICATES
                    or any(type(v) is not bool for v in step["predicates"].values())):
                raise SuiteError("invalid_recording", "Pre/post observer phase or predicate shape differs")
            for snapshot in (step["pre"], step["post"]):
                _snapshot(snapshot)
                if (snapshot["on_floor"] or snapshot["on_wall"] or snapshot["actions"] != _actions(frame)
                        or snapshot["jump_just_pressed"]):
                    invalid.append({"case_id": spec["case_id"], "stage": "unexpected_contact_or_input", "step_index": step_index})
            if step_index == 0:
                pre = step["pre"]
                if (pre["state"] != spec["state"] or pre["horizontal_rest"] != (spec["state"] == "Idle")
                        or pre["moving_up"] != (spec["motion"] == "rising")
                        or pre["moving_down"] != (spec["motion"] == "falling")):
                    invalid.append({"case_id": spec["case_id"], "stage": "seed_precondition", "step_index": 0})
    return {"status": "valid" if not invalid else "probe_invalid", "invalid_preconditions": invalid,
            "case_count": len(CASES), "observed_steps": len(CASES) * STEPS}


def _evidence(output, *, video):
    capture = output / "capture"
    paths = [capture / name for name in ("capture_config.json", "recording.json", "import.log", "engine.log")]
    if video:
        paths.append(capture / "video.mp4")
    images = [capture / "frames" / f"{i:06d}.png" for i in range(FRAME_COUNT)]
    if set((capture / "frames").glob("*.png")) != set(images):
        raise SuiteError("invalid_evidence", "Exactly 240 native PNGs required")
    for path in [*paths, *images]:
        runtime._plain(path)
        if not path.is_file() or not path.stat().st_size or path.stat().st_nlink > 1:
            raise SuiteError("invalid_evidence", "Missing, empty or hardlinked evidence")
    for path in images:
        with path.open("rb") as stream:
            data = stream.read(24)
        if len(data) != 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != (640, 360):
            raise SuiteError("invalid_evidence", "Non-native PNG dimensions")
    return {path.relative_to(output).as_posix(): sha256(path) for path in [*paths, *images]}


def run_suite(source_project, output, *, expected_source_hashes, cancel=None, timeout=120, video=False, asset_cache=None):
    """Copy one immutable source, then observe original actors in a private process.

    A compatible enclosing v3 context may be reused. The output must be inside
    its declared roots; otherwise the existing classifier rejects execution.
    """
    runner._check_cancel(cancel)
    source, output = runtime._plain(source_project), runtime._plain(output)
    if type(video) is not bool or not expected_source_hashes or project_hashes(source) != expected_source_hashes:
        raise SuiteError("source_changed", "Explicit exact immutable input source map required")
    runner._assert_outside(output, source, "Integrity output")
    if source.is_relative_to(output) or output.exists():
        raise SuiteError("output_exists", "A new separate suite output is required")
    if any(output.is_relative_to((ROOT / name).resolve()) for name in ("vendor", "work", "config", "adapters", "kiroclops")):
        raise SuiteError("protected_output", "Use a session runtime or external review output")
    policy = protocol_record()
    original = dict(expected_source_hashes)
    if any(name.startswith(ADAPTER_DIR + "/") for name in original):
        raise SuiteError("fixture_exists", "Input source already contains another supplemental adapter")
    output.mkdir(parents=True)
    _write_new(output / "suite-start.json", {"protocol": policy, "source_project": str(source),
        "source_hashes": original, "started_at": datetime.now(timezone.utc).isoformat(), "acceptance": False})
    prepared = {}
    try:
        project = copy_project(source, output / "project")
        if project_hashes(project) != original:
            raise SuiteError("source_changed", "Disposable copy differs")
        seed = None
        if asset_cache is not None:
            if (not isinstance(asset_cache, dict) or not isinstance(asset_cache.get("cache_dir"), str)
                    or not isinstance(asset_cache.get("manifest_sha256"), str)):
                raise SuiteError("invalid_cache", "Explicit qualified asset-cache binding required")
            # Seed the exact original copy BEFORE adding our protected adapter.
            # Only texture products are reused; normal editor import still runs.
            seed = asset_cache_module.seed_import_cache(asset_cache["cache_dir"], project,
                engine=ROOT / runtime.GUI_RELATIVE, expected_source_hashes=original,
                expected_manifest_sha256=asset_cache["manifest_sha256"], cancel=cancel)
            _write_new(output / "asset-seed.json", seed)
        folder = project / ADAPTER_DIR
        folder.mkdir()
        for name in policy["adapter_hashes"]:
            shutil.copy2(ROOT / ADAPTER_SOURCE / name, folder / name)
        prepared = {**original, **{ADAPTER_DIR + "/" + k: v for k, v in policy["adapter_hashes"].items()}}
        if project_hashes(project) != prepared:
            raise SuiteError("source_changed", "Only new integrity fixture files may differ")
        capture = output / "capture"
        (capture / "frames").mkdir(parents=True)
        config = {"schema_version": 1, "protocol": VERSION, "cases": list(CASES), "output_dir": str(output / "capture")}
        _write_new(output / "capture/capture_config.json", config)
        env = runner._runtime_environment(output / "capture/runtime")
        env["KIROCLOPS_CONFIG"] = str(output / "capture/capture_config.json")
        active = runtime.previous._ACTIVE
        if active is not None and not isinstance(active, runtime.RuntimeBridge):
            raise SuiteError("runtime_context_active", "Only the qualified v3 private runtime may enclose a suite")
        context = nullcontext(active) if active else runtime.isolated_runtime(project_roots=[output], expected_sources={project: prepared})
        with context, runner._godot_lock(cancel=cancel, timeout=timeout):
            engine = runner._engine()
            code = runner._run_process([engine, "--path", project, "--editor", "--headless", "--import"],
                cwd=project, env=env, log_path=output / "capture/import.log", cancel=cancel, timeout=timeout)
            runner._engine_failure(code, output / "capture/import.log", "import_error")
            runner._ensure_unchanged(project, prepared)
            code = runner._run_process([engine, "--path", project, "--fixed-fps", "60", "--resolution", "640x360",
                "--windowed", "--rendering-method", "gl_compatibility", f"res://{ADAPTER_DIR}/Capture.tscn"],
                cwd=project, env=env, log_path=output / "capture/engine.log", cancel=cancel, timeout=timeout)
            runner._engine_failure(code, output / "capture/engine.log", "engine_error")
            runner._ensure_unchanged(project, prepared)
            if video:
                ffmpeg = shutil.which("ffmpeg")
                if not ffmpeg:
                    raise SuiteError("video_error", "FFmpeg unavailable")
                code = runner._run_process([ffmpeg, "-hide_banner", "-loglevel", "error", "-framerate", "60", "-i", str(output / "capture/frames/%06d.png"),
                    "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output / "capture/video.mp4")],
                    cwd=capture, env=env, log_path=output / "capture/video.log", cancel=cancel, timeout=timeout)
                if code:
                    raise SuiteError("video_error", "Supplemental encoding failed")
        decision = evaluate_recording(read_json(output / "capture/recording.json"))
        proofpath = output / "capture" / ("engine." + runtime.VERSION) / "bridge.json"
        private = runtime.verify_runtime_receipt(proofpath, project=project, expected_source_hashes=prepared)
        if private.get("status") != "pass":
            raise SuiteError("isolation_error", "Private process proof did not pass")
        runner._check_cancel(cancel)
        if project_hashes(source) != original or runner._ensure_unchanged(project, prepared) != prepared or protocol_record() != policy:
            raise SuiteError("source_changed", "Original/copied source or harness changed during run")
        record = {"schema_version": 1, "kind": VERSION, "protocol": policy, "status": decision["status"],
            "source_project": str(source), "output": str(output), "project": str(project),
            "original_source_hashes": original, "prepared_source_hashes": prepared, "decision": decision,
            "runtime_receipt": {"path": str(proofpath), "sha256": sha256(proofpath)}, "video": video,
            "asset_seed": None if seed is None else {"path": "asset-seed.json", "sha256": sha256(output / "asset-seed.json")},
            "evidence_sha256": _evidence(output, video=video), "started_receipt_sha256": sha256(output / "suite-start.json"),
            "completed_at": datetime.now(timezone.utc).isoformat(), "canonical_experiment_acceptance": False}
        _write_new(output / "suite.json", record)
        return output
    except BaseException as error:
        _write_new(output / "failure.json", {"status": "error", "code": getattr(error, "code", type(error).__name__),
            "message": str(error), "original_source_hashes": original, "prepared_source_hashes": prepared,
            "protocol": policy, "canonical_experiment_acceptance": False})
        raise


def verify_suite(output, *, expected_source_hashes=None):
    """Read-only complete evidence replay; a valid suite is not a differential pass."""
    try:
        output = runtime._plain(output)
        path = output / "suite.json"
        if not path.is_file():
            return {"status": "unverified", "reason": "Completed suite receipt missing"}
        record = read_json(path)
        project, source = runtime._plain(record["project"]), runtime._plain(record["source_project"])
        original = record["original_source_hashes"]
        prepared = {**original, **{ADAPTER_DIR + "/" + k: v for k, v in _adapter_hashes().items()}}
        if (record.get("schema_version") != 1 or record.get("kind") != VERSION or record.get("protocol") != protocol_record()
                or record.get("output") != str(output) or project != output / "project"
                or record.get("prepared_source_hashes") != prepared or project_hashes(project) != prepared
                or project_hashes(source) != original or expected_source_hashes is not None and expected_source_hashes != original
                or type(record.get("video")) is not bool or record.get("canonical_experiment_acceptance") is not False):
            raise SuiteError("invalid_evidence", "Suite source, fixture, identity or protocol differs")
        start = read_json(output / "suite-start.json")
        if (sha256(output / "suite-start.json") != record["started_receipt_sha256"]
                or start.get("protocol") != record["protocol"] or start.get("source_project") != str(source)
                or start.get("source_hashes") != original):
            raise SuiteError("invalid_evidence", "Original suite-start binding differs")
        config = read_json(output / "capture/capture_config.json")
        if config != {"schema_version": 1, "protocol": VERSION, "cases": list(CASES), "output_dir": str(output / "capture")}:
            raise SuiteError("invalid_evidence", "Capture config differs")
        if record["evidence_sha256"] != _evidence(output, video=record["video"]):
            raise SuiteError("invalid_evidence", "Raw suite evidence differs")
        proofpath = output / "capture" / ("engine." + runtime.VERSION) / "bridge.json"
        if record["runtime_receipt"] != {"path": str(proofpath), "sha256": sha256(proofpath)}:
            raise SuiteError("invalid_evidence", "Private routing receipt differs")
        if runtime.verify_runtime_receipt(proofpath, project=project, expected_source_hashes=prepared).get("status") != "pass":
            raise SuiteError("isolation_error", "Private routing verification failed")
        binding = record.get("asset_seed")
        if binding is not None:
            if binding != {"path": "asset-seed.json", "sha256": sha256(output / "asset-seed.json")}:
                raise SuiteError("invalid_evidence", "Asset seed receipt differs")
            seed = read_json(output / "asset-seed.json")
            if (seed.get("status") != "seeded" or seed.get("project") != str(project)
                    or seed.get("full_source_hashes") != original
                    or seed.get("requires_editor_import") is not True or seed.get("capture_acceptance") is not False
                    or sha256(Path(seed["cache_dir"]) / "manifest.json") != seed["manifest_sha256"]):
                raise SuiteError("invalid_evidence", "Asset seed source binding differs")
            asset_cache_module._validate_entry(runtime._plain(seed["cache_dir"]))
        decision = evaluate_recording(read_json(output / "capture/recording.json"))
        if record.get("decision") != decision or record.get("status") != decision["status"]:
            raise SuiteError("invalid_evidence", "Saved precondition result differs")
        return {"status": "pass" if decision["status"] == "valid" else "probe_invalid", "decision": decision,
                "suite_path": str(path), "suite_sha256": sha256(path), "source_hashes": original,
                "scope": "Integrity/seed validity only; compare_suites must establish response equality."}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        return {"status": "fail", "reason": f"{type(error).__name__}: {error}"}


def compare_suites(baseline_output, candidate_output):
    """Expose mismatch names and booleans, never private kinematic hash values."""
    baseline = verify_suite(baseline_output)
    candidate = verify_suite(candidate_output)
    info = lambda r: {k: r[k] for k in ("suite_path", "suite_sha256", "source_hashes") if k in r}
    result = {"status": "unverified", "baseline": info(baseline), "candidate": info(candidate),
              "summary": {"case_count": len(CASES), "compared_steps": 0, "failed_cases": 0, "mismatch_count": 0},
              "mismatches": [], "entry_kind": "seeded_controller_contract",
              "scope": "Finite seeded differential; not normal-map reachability or a visual-fit measurement."}
    if baseline["status"] != "pass" or candidate["status"] != "pass":
        result["status"] = "unverified" if "unverified" in (baseline["status"], candidate["status"]) else "probe_invalid"
        result["verification"] = {"baseline": baseline["status"], "candidate": candidate["status"]}
        return result
    a, b = (read_json(Path(path) / "capture/recording.json") for path in (baseline_output, candidate_output))
    mismatch = []
    for before, after in zip(a["cases"], b["cases"]):
        for step_a, step_b in zip(before["steps"], after["steps"]):
            fields = []
            for phase in ("pre", "post", "predicates"):
                for key in sorted(step_a[phase]):
                    if step_a[phase][key] != step_b[phase][key]:
                        fields.append(phase + "." + key)
            if fields:
                mismatch.append({"case_id": before["case_id"], "step_index": step_a["step_index"], "fields": fields})
    # The immediately preceding verified source/data hashes must still bind the
    # exact recording bytes used for this comparison.
    for path, verified in ((baseline_output, baseline), (candidate_output, candidate)):
        current = verify_suite(path)
        if current != verified:
            result["status"] = "probe_invalid"
            result["verification"] = {"reason": "Evidence changed across comparison"}
            return result
    result.update(status="fail" if mismatch else "pass", mismatches=mismatch)
    result["summary"].update(compared_steps=len(CASES) * STEPS,
        failed_cases=len({m["case_id"] for m in mismatch}), mismatch_count=len(mismatch))
    return result


def verify_qualification(output):
    """Read-only proof of positive and known-negative native controls."""
    try:
        output = runtime._plain(output); path = output / "qualification.json"
        if not path.is_file():
            return {"status": "unverified", "reason": "Native qualification missing"}
        data = read_json(path)
        if data.get("protocol") != protocol_record() or data.get("status") != "pass":
            raise SuiteError("qualification_changed", "Qualification protocol/status differs")
        required = {"baseline_a", "baseline_b", "preserved_candidate01", "extra_update_candidate02"}
        if set(data.get("suites", {})) != required:
            raise SuiteError("qualification_changed", "Native control inventory differs")
        for name, bound in data["suites"].items():
            checked = verify_suite(bound["output"], expected_source_hashes=bound["source_hashes"])
            if checked.get("status") != "pass" or checked.get("suite_sha256") != bound["suite_sha256"]:
                raise SuiteError("qualification_changed", "A native control no longer verifies: " + name)
        a = data["suites"]["baseline_a"]["output"]
        expected = {"self": "pass", "ground_only": "pass", "extra_air_update": "fail"}
        others = {"self": "baseline_b", "ground_only": "preserved_candidate01", "extra_air_update": "extra_update_candidate02"}
        for label, name in others.items():
            comparison = compare_suites(a, data["suites"][name]["output"])
            if comparison != data["comparisons"][label] or comparison["status"] != expected[label]:
                raise SuiteError("qualification_changed", "Native differential control differs: " + label)
        failed = data["comparisons"]["extra_air_update"]["mismatches"]
        if not {"idle-rising-left", "idle-rising-right"} <= {row["case_id"] for row in failed if row["step_index"] == 0}:
            raise SuiteError("qualification_changed", "Decisive seeded negative cases missing")
        return {"status": "pass", "path": str(path), "sha256": sha256(path), "protocol": protocol_record(),
                "limits": "Self + preserved positive + extra-update negative only; no natural reachability claim."}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        return {"status": "fail", "reason": str(error)}
