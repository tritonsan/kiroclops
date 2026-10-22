"""Supplemental real-controller probes on explicitly disposable Slay copies.

These receipts are separate from frozen experiment acceptance. The airborne
oracle exports a boolean assertion, never a coordinate or velocity sample.
"""
from __future__ import annotations

from pathlib import Path
import math
import hashlib
import shutil

from ..common import ROOT, read_json, sha256, write_json
from . import runner
from . import background_capture_v1 as background

PROBES = {"air_idle_transition", "run_jump_land_brake"}
ADAPTER_DIR = "kiroclops_movement_probe"
FPS, WARMUP, FRAME_COUNT = 60, 60, 240
PROTOCOL_REVISION = 2
ProbeError = runner.RunnerError


def _adapter_hashes():
    root = ROOT / "adapters/movement-probe-godot4"
    return {name: sha256(root / name) for name in ("Capture.tscn", "capture.gd")}


def _actions(probe, index):
    if probe == "air_idle_transition":
        return ["right"] if 32 <= index < 46 else []
    active = []
    if 30 <= index < 54:
        active.append("jump")
    if 5 <= index < 90:
        active.append("right")
    return active


def _window_valid(window, index):
    return (isinstance(window, dict)
            and set(window) == {"frame_index", "focused", "no_focus", "offscreen", "window_position"}
            and type(window["frame_index"]) is int and window["frame_index"] == index
            and window["focused"] is False and window["no_focus"] is True and window["offscreen"] is True
            and isinstance(window["window_position"], list) and len(window["window_position"]) == 2
            and all(type(value) is int and value < -5000 for value in window["window_position"]))


def evaluate_recording(recording, *, probe, normal_level=False):
    """Validate the fixed protocol and replay exported runtime predicates."""
    if probe not in PROBES or type(normal_level) is not bool:
        raise ProbeError("invalid_config", "Unknown movement probe")
    expected_keys = {"schema_version", "protocol_revision", "status", "probe", "normal_level", "fps", "viewport", "frame_count",
                     "warmup_ticks", "settled", "frames", "runtime_frames", "window_observations",
                     "startup_window_observations", "platform_removed_after_frame", "pre_transition", "post_transition"}
    if not isinstance(recording, dict) or set(recording) != expected_keys:
        raise ProbeError("invalid_recording", "Unexpected recording fields; player physics telemetry is prohibited")
    if (recording.get("status") != "ok" or recording.get("probe") != probe
            or recording.get("protocol_revision") != PROTOCOL_REVISION
            or recording.get("normal_level") is not normal_level
            or recording.get("fps") != FPS or recording.get("viewport") != [640, 360]
            or recording.get("frame_count") != FRAME_COUNT or recording.get("warmup_ticks") != WARMUP
            or recording.get("settled") is not True):
        raise ProbeError("invalid_recording", "Probe identity, native viewport or fixed clock changed")
    frames, runtime = recording.get("frames", []), recording.get("runtime_frames", [])
    windows = recording.get("window_observations", [])
    if len(frames) != FRAME_COUNT or len(runtime) != FRAME_COUNT or len(windows) != FRAME_COUNT:
        raise ProbeError("invalid_recording", "Expected 240 rendered and post-player observations")
    startup_windows = recording.get("startup_window_observations", [])
    if not startup_windows or any(not isinstance(window, dict) or not _window_valid(window, window.get("frame_index")) or window["frame_index"] >= 0 for window in startup_windows):
        raise ProbeError("window_contract", "Warmup native-window observation is missing or failed")
    for index, (frame, observed) in enumerate(zip(frames, runtime)):
        if (set(frame) != {"frame_index", "physics_tick", "time_s", "actions"}
                or type(frame["frame_index"]) is not int or frame["frame_index"] != index
                or type(frame["physics_tick"]) is not int or frame["physics_tick"] != WARMUP + index
                or type(frame["time_s"]) not in (int, float) or not math.isfinite(frame["time_s"])
                or abs(frame["time_s"] - index / FPS) > 1e-6
                or frame["actions"] != _actions(probe, index)
                or set(observed) != {"frame_index", "state", "on_floor", "horizontal_rest"}
                or type(observed["frame_index"]) is not int or observed["frame_index"] != index
                or not isinstance(observed["state"], str)
                or type(observed["on_floor"]) is not bool or type(observed["horizontal_rest"]) is not bool):
            raise ProbeError("invalid_recording", "Input, observation phase or permitted telemetry schema changed")
        window = windows[index]
        if not _window_valid(window, index):
            raise ProbeError("window_contract", "Actual native window focused or failed to stay offscreen")
    if probe == "air_idle_transition":
        before, after = recording.get("pre_transition", {}), recording.get("post_transition", {})
        if (set(before) != {"frame_index", "state", "on_floor", "horizontal_rest", "actions"}
                or set(after) != {"frame_index", "state", "on_floor", "horizontal_velocity_unchanged"}
                or before["frame_index"] != 32 or after["frame_index"] != 32
                or type(before["on_floor"]) is not bool or type(before["horizontal_rest"]) is not bool
                or type(after["on_floor"]) is not bool or type(after["horizontal_velocity_unchanged"]) is not bool):
            raise ProbeError("invalid_recording", "Missing private-oracle boolean observations")
        precondition = (recording.get("platform_removed_after_frame") == 29
                        and before["state"] == "Idle" and before["on_floor"] is False
                        and before["horizontal_rest"] is True and before["actions"] == ["right"]
                        and runtime[31]["state"] == "Idle" and runtime[31]["on_floor"] is False
                        and runtime[31]["horizontal_rest"] is True)
        if not precondition:
            return {"status": "probe_invalid", "checks": {"airborne_idle_precondition": False},
                    "reason": "Fixed frame32 did not naturally reach airborne Idle at rest; no behavioral verdict"}
        if after["state"] != runtime[32]["state"] or after["on_floor"] != runtime[32]["on_floor"]:
            raise ProbeError("invalid_recording", "Post-transition and post-player observations disagree")
        checks = {"airborne_idle_precondition": True,
                  "natural_idle_to_fall": after["state"] == "Fall" and after["on_floor"] is False,
                  "horizontal_velocity_unchanged": after["horizontal_velocity_unchanged"]}
    else:
        if recording.get("platform_removed_after_frame") != -1 or recording.get("pre_transition") != {} or recording.get("post_transition") != {}:
            raise ProbeError("invalid_recording", "Combined movement probe must not remove its floor")
        fell = [index for index in range(31, 90) if runtime[index]["state"] == "Fall" and not runtime[index]["on_floor"]]
        def rest(row):
            return row["state"] == "Idle" and row["on_floor"] and row["horizontal_rest"]
        checks = {
            "grounded_run_before_jump": runtime[29]["state"] == "Run" and runtime[29]["on_floor"],
            "run_to_airborne_jump": runtime[30]["state"] == "Jump" and not runtime[30]["on_floor"],
            "fall_after_jump": bool(fell),
            "grounded_run_after_landing": bool(fell) and any(runtime[index]["state"] == "Run" and runtime[index]["on_floor"] for index in range(min(fell) + 1, 90)),
            "settles_after_release": any(rest(row) for row in runtime[90:131]),
            "final_grounded_rest": all(rest(row) for row in runtime[180:]),
            "no_unrequested_special_state": not any(row["state"] in {"DoubleJump", "WallJump", "Hit", "Appear"} for row in runtime),
        }
    return {"status": "pass" if all(checks.values()) else "fail", "checks": checks}


def _prepare(project, *, disposable_root, expected_source_hashes):
    root, project = background._ordinary(disposable_root), background._ordinary(project)
    if (project == root or not project.is_relative_to(root) or not root.is_dir()
            or any(project.is_relative_to((ROOT / name).resolve()) for name in ("sessions", "vendor", "work", "config", "adapters", "kiroclops"))):
        raise ProbeError("protected_project", "Use a copied project inside an explicit disposable root")
    runner._project_path(project)
    before = runner.source_hashes(project)
    if not expected_source_hashes or before != expected_source_hashes:
        raise ProbeError("source_changed", "Disposable input differs from the caller's exact source map")
    original_settings = (project / "project.godot").read_bytes()
    had_background = background.SUPPORT_PATH in before
    startup = background.ensure_disposable_project(project, disposable_root=root, expected_source_hashes=before)
    startup = {**startup, "already_present": had_background, "settings_before_hex": original_settings.hex()}
    settings_expected = before if had_background else background._expected_after(before, original_settings)
    if startup["before_source_hashes"] != before or startup["after_source_hashes"] != settings_expected:
        raise ProbeError("source_changed", "Background preparation exceeded its canonical settings transformation")
    expected_adapter = _adapter_hashes()
    destination = project / ADAPTER_DIR
    for name, expected in expected_adapter.items():
        target = destination / name
        if target.exists() and sha256(target) != expected:
            raise ProbeError("fixture_changed", "A different supplemental fixture already exists")
    for name in expected_adapter:
        target = destination / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "adapters/movement-probe-godot4" / name, target)
    after = runner.source_hashes(project)
    allowed = {**settings_expected,
               **{f"{ADAPTER_DIR}/{name}": digest for name, digest in expected_adapter.items()}}
    if after != allowed:
        raise ProbeError("source_changed", "Preparation changed an original source file")
    return before, after, startup


def _evidence(run, video=False):
    paths = [run / name for name in ("probe_config.json", "recording.json", "engine.log")]
    if video:
        paths.append(run / "video.mp4")
    frames = [run / "frames" / f"{index:06d}.png" for index in range(FRAME_COUNT)]
    if set((run / "frames").glob("*.png")) != set(frames):
        raise ProbeError("invalid_evidence", "Expected exactly 240 native PNG frames")
    for path in [*paths, *frames]:
        background._ordinary(path)
        if not path.is_file() or not path.stat().st_size:
            raise ProbeError("invalid_evidence", "Missing or empty runtime evidence")
    return {path.relative_to(run).as_posix(): sha256(path) for path in [*paths, *frames]}


def run_probe(copyproject, output, *, disposable_root, expected_source_hashes,
              probe="air_idle_transition", no_focus=True, offscreen=True,
              normal_level=False, cancel=None, timeout=120, video=False):
    """Run one fixed probe; only the explicit disposable copy gains an adapter.

    All process execution uses the shared Godot lock and owned-tree cancellation.
    Root/operator must first qualify actual offscreen rendering independently.
    """
    runner._check_cancel(cancel)
    if (probe not in PROBES or no_focus is not True or offscreen is not True
            or type(normal_level) is not bool or type(video) is not bool
            or probe == "air_idle_transition" and normal_level):
        raise ProbeError("invalid_config", "Fixed probe and no-focus/offscreen startup are required")
    project, run = Path(copyproject).resolve(), Path(output).resolve()
    runner._assert_outside(run, project, "Probe output")
    if any(run.is_relative_to((ROOT / name).resolve()) for name in ("sessions", "vendor", "work", "config", "adapters", "kiroclops")):
        raise ProbeError("protected_output", "Supplemental receipts belong outside experiment sessions and source directories")
    if run.exists() and (not run.is_dir() or any(run.iterdir())):
        raise ProbeError("output_exists", "Supplemental evidence is never overwritten")
    before, prepared = {}, {}
    try:
        with runner._godot_lock(cancel=cancel, timeout=timeout):
            before, prepared, startup = _prepare(project, disposable_root=disposable_root, expected_source_hashes=expected_source_hashes)
            (run / "frames").mkdir(parents=True, exist_ok=True)
            config = {"schema_version": 1, "protocol_revision": PROTOCOL_REVISION, "probe": probe, "output_dir": str(run), "no_focus": True,
                      "offscreen": True, "normal_level": normal_level}
            config_path = write_json(run / "probe_config.json", config)
            env = runner._runtime_environment(run / "runtime")
            env["KIROCLOPS_CONFIG"] = str(config_path)
            engine = runner._engine()
            result = runner._run_process([engine, "--path", project, "--editor", "--headless", "--import"],
                cwd=project, env=env, log_path=run / "import.log", cancel=cancel, timeout=timeout)
            runner._engine_failure(result, run / "import.log", "import_error")
            runner._ensure_unchanged(project, prepared)
            result = runner._run_process([engine, "--path", project, "--fixed-fps", "60", "--resolution", "640x360",
                "--windowed", "--rendering-method", "gl_compatibility", f"res://{ADAPTER_DIR}/Capture.tscn"],
                cwd=project, env=env, log_path=run / "engine.log", cancel=cancel, timeout=timeout)
            runner._engine_failure(result, run / "engine.log", "engine_error")
            runner._ensure_unchanged(project, prepared)
            recording = read_json(run / "recording.json")
            decision = evaluate_recording(recording, probe=probe, normal_level=normal_level)
        if video:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise ProbeError("video_error", "FFmpeg unavailable; raw evidence retained")
            result = runner._run_process([ffmpeg, "-hide_banner", "-loglevel", "error", "-framerate", "60", "-i", run / "frames/%06d.png",
                "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", run / "video.mp4"],
                cwd=run, env=env, log_path=run / "video.log", cancel=cancel, timeout=timeout)
            if result:
                raise ProbeError("video_error", "Video encoding failed")
        runner._check_cancel(cancel)
        evidence = _evidence(run, video)
        after = runner._ensure_unchanged(project, prepared)
        receipt = {"schema_version": 1, "protocol_revision": PROTOCOL_REVISION, "kind": "supplemental_runtime_integrity", "probe": probe,
                   "status": decision["status"], "decision": decision, "normal_level": normal_level,
                   "project": str(project), "output": str(run), "video": video,
                   "original_source_hashes": before, "full_source_hashes_before": prepared,
                   "full_source_hashes_after": after, "adapter_hashes": _adapter_hashes(),
                   "startup": startup, "evidence_sha256": evidence,
                   "runner_sha256": sha256(Path(__file__)), "canonical_experiment_acceptance": False,
                   "scope": "Real character callbacks and move_and_slide. Only runtime boolean predicates, states, contacts and scheduled inputs are exported; no physics values enter the fitter.",
                   "limitations": "Requires separate parent-pass/known-bad-fail and actual offscreen preflight qualification. A supplemental pass does not establish trajectory matching or full-game completion."}
        write_json(run / "probe.json", receipt)
        return run
    except BaseException as exc:
        if run.exists():
            write_json(run / "failure.json", {"status": "error", "code": getattr(exc, "code", type(exc).__name__),
                "error": str(exc), "original_source_hashes": before, "prepared_source_hashes": prepared,
                "source_hashes_after": runner.source_hashes(project)})
        raise


def verify_probe(copyproject, output, *, expected_source_hashes=None):
    """Read-only evidence replay; missing/error/invalid never becomes a pass."""
    run, project = Path(output).resolve(), Path(copyproject).resolve()
    if not (run / "probe.json").is_file():
        return {"status": "unverified", "reason": "No completed supplemental probe receipt"}
    try:
        receipt = read_json(run / "probe.json")
        current = runner.source_hashes(project)
        if (receipt.get("kind") != "supplemental_runtime_integrity" or receipt.get("project") != str(project)
                or receipt.get("protocol_revision") != PROTOCOL_REVISION
                or receipt.get("output") != str(run) or receipt.get("adapter_hashes") != _adapter_hashes()
                or receipt.get("runner_sha256") != sha256(Path(__file__))
                or current != receipt.get("full_source_hashes_before") or current != receipt.get("full_source_hashes_after")
                or expected_source_hashes is not None and expected_source_hashes != receipt.get("original_source_hashes")):
            raise ProbeError("source_changed", "Supplemental source or implementation binding changed")
        background.verify_disposable_project(project, disposable_root=project.parent, expected_source_hashes=current)
        original_settings = bytes.fromhex(receipt["startup"]["settings_before_hex"])
        if hashlib.sha256(original_settings).hexdigest() != receipt["original_source_hashes"]["project.godot"]:
            raise ProbeError("source_changed", "Original project settings do not match the supplied source map")
        settings_expected = (receipt["original_source_hashes"] if receipt["startup"]["already_present"] else
                             background._expected_after(receipt["original_source_hashes"], original_settings))
        expected = {**settings_expected,
                    **{f"{ADAPTER_DIR}/{name}": digest for name, digest in _adapter_hashes().items()}}
        if (current != expected or (project / "override.cfg").read_bytes() != background.OVERRIDE_BYTES
                or (project / background.SUPPORT_PATH).read_bytes() != background.SUPPORT_BYTES):
            raise ProbeError("source_changed", "Preparation exceeded its permitted fixture/startup additions")
        if _evidence(run, receipt["video"]) != receipt["evidence_sha256"]:
            raise ProbeError("evidence_changed", "Supplemental raw evidence changed")
        config = read_json(run / "probe_config.json")
        if config != {"schema_version": 1, "protocol_revision": PROTOCOL_REVISION, "probe": receipt["probe"], "output_dir": str(run), "no_focus": True,
                      "offscreen": True, "normal_level": receipt["normal_level"]}:
            raise ProbeError("invalid_config", "Fixed supplemental protocol changed")
        decision = evaluate_recording(read_json(run / "recording.json"), probe=receipt["probe"], normal_level=receipt["normal_level"])
        if decision != receipt["decision"] or decision["status"] != receipt["status"]:
            raise ProbeError("evidence_changed", "Runtime verdict does not replay")
        return {**decision, "receipt": str(run / "probe.json"), "receipt_sha256": sha256(run / "probe.json"),
                "canonical_experiment_acceptance": False}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        return {"status": "fail", "reason": str(exc), "code": getattr(exc, "code", type(exc).__name__)}
