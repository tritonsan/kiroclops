"""New contact-aware normal gameplay protocol; original probe stays immutable.

Fixed original-level inputs and native pre/post-player observations distinguish
wall sliding from an unrequested jump. No source/controller patch is supplied.
Requires prospective native positive/negative qualification before model use.
"""
from __future__ import annotations

from pathlib import Path
import math
import hashlib
import shutil

from ..common import ROOT, read_json, sha256, write_json
from . import runner
from . import background_capture_v1 as background
from . import movement_probe as original

PROBES = {"run_jump_land_brake"}
# The existing private-runtime router permits this exact disposable scene path.
# Its bytes come from the NEW adapter below and are independently hash-bound.
ADAPTER_DIR = "kiroclops_movement_probe"
FPS, WARMUP, FRAME_COUNT = 60, 60, 240
PROTOCOL_REVISION = 3
PROTOCOL_ID = "normal-gameplay-contact-v2"
ProbeError = runner.RunnerError


def _adapter_hashes():
    root = ROOT / "adapters/normal-gameplay-v2-godot4"
    return {name: sha256(root / name) for name in ("Capture.tscn", "capture.gd")}


def _actions(probe, index):
    active = []
    if 30 <= index < 54:
        active.append("jump")
    if 5 <= index < 90:
        active.append("right")
    return active


def protocol_record():
    """A fresh JSON-friendly prospective contract, never mutable shared data."""
    return {"protocol_id": PROTOCOL_ID, "protocol_revision": PROTOCOL_REVISION,
        "probe": "run_jump_land_brake", "normal_level": True,
        "fps": FPS, "frame_count": FRAME_COUNT, "warmup_ticks": WARMUP,
        "events": [{"tick": 5, "action": "right", "pressed": True},
                   {"tick": 30, "action": "jump", "pressed": True},
                   {"tick": 54, "action": "jump", "pressed": False},
                   {"tick": 90, "action": "right", "pressed": False}],
        "observation_phase": {"pre_player_physics_priority": -100, "post_player_physics_priority": 100,
            "fields": ["state", "on_floor", "on_wall", "horizontal_rest", "moving_up", "moving_down"],
            "input_edge": "native Input.is_action_just_pressed(ui_up) sampled before player update"},
        "criteria": {
            "original_six_run_jump_land_brake_rest_checks": "unchanged from the original fixed normal-level probe",
            "authorized_jump_edge": "exactly native frame30; no later input edge",
            "jump_entry_requires_requested_contact": "every transition into Jump/DoubleJump needs a new jump edge and pre-update floor or wall contact",
            "new_ascent_requires_requested_contact": "false-to-true moving_up needs a new jump edge and pre-update floor or wall contact; continued held ascent is allowed",
            "wall_slide_contact": "WallJump requires pre or post wall contact; a new post-move floor contact may retain that label for one update only, followed by grounded Idle/Run on the next native tick",
            "no_unrequested_other_special_state": "no DoubleJump, Hit or Appear in the fixed one-jump route"},
        "source_hashes": {"kiroclops/integration/normal_gameplay_v2.py": sha256(Path(__file__)),
                          "kiroclops/integration/movement_probe.py": sha256(Path(original.__file__))},
        "adapter_hashes": _adapter_hashes(),
        "canonical_experiment_acceptance": False,
        "limitations": ["This fixed route contains no intended bounce, trampoline, teleport or forced-launch mechanics.",
            "Contact/motion predicates validate gameplay events; coordinates, speed and acceleration are never exported to a fitter.",
            "State names still identify this known controller's actions; the probe is not a generic game-understanding system.",
            "A pass requires new native positive and deliberately negative qualification; old recordings lack the contact observations and cannot be upgraded."]}


def _window_valid(window, index):
    return (isinstance(window, dict)
            and set(window) == {"frame_index", "focused", "no_focus", "offscreen", "window_position"}
            and type(window["frame_index"]) is int and window["frame_index"] == index
            and window["focused"] is False and window["no_focus"] is True and window["offscreen"] is True
            and isinstance(window["window_position"], list) and len(window["window_position"]) == 2
            and all(type(value) is int and value < -5000 for value in window["window_position"]))


def evaluate_recording(recording, *, probe="run_jump_land_brake", normal_level=True):
    """Replay native contact/event predicates; never reinterpret old evidence."""
    if probe not in PROBES or normal_level is not True:
        raise ProbeError("invalid_config", "Contact-v2 requires the fixed original normal level")
    if not isinstance(recording, dict) or recording.get("protocol_revision") != PROTOCOL_REVISION:
        raise ProbeError("invalid_recording", "New native contact-v2 evidence is required")
    runtime = recording.get("runtime_frames", [])
    if len(runtime) != FRAME_COUNT:
        raise ProbeError("invalid_recording", "Expected 240 pre/post-player observations")
    fields = {"state", "on_floor", "on_wall", "horizontal_rest", "moving_up", "moving_down"}
    projected = []
    for index, row in enumerate(runtime):
        if (not isinstance(row, dict) or set(row) != {"frame_index", "pre", "post", "jump_just_pressed"}
                or type(row["frame_index"]) is not int or row["frame_index"] != index
                or type(row["jump_just_pressed"]) is not bool
                or row["jump_just_pressed"] != (index == 30)):
            raise ProbeError("invalid_recording", "Pre/post observation or native jump edge differs from fixed inputs")
        for phase in ("pre", "post"):
            observation = row[phase]
            if (not isinstance(observation, dict) or set(observation) != fields
                    or not isinstance(observation["state"], str)
                    or any(type(observation[key]) is not bool for key in fields - {"state"})
                    or observation["moving_up"] and observation["moving_down"]):
                raise ProbeError("invalid_recording", "Only coherent boolean contacts/motion and state names may be exported")
        projected.append({"frame_index": index, **{key: row["post"][key] for key in
            ("state", "on_floor", "horizontal_rest")}})
    # Reuse fixed timing/actions/windows and the original six positive checks.
    # This is an in-memory view only; the new recording/schema/hash stays intact.
    old_view = {**recording, "protocol_revision": original.PROTOCOL_REVISION, "runtime_frames": projected}
    original_decision = original.evaluate_recording(old_view, probe=probe, normal_level=True)
    checks = {key: value for key, value in original_decision["checks"].items()
              if key != "no_unrequested_special_state"}
    violations = {key: [] for key in ("jump_entry_requires_requested_contact", "new_ascent_requires_requested_contact",
                                       "wall_slide_contact", "no_unrequested_other_special_state")}
    wall_slide_frames = []
    for row in runtime:
        pre, post, index = row["pre"], row["post"], row["frame_index"]
        authorized = row["jump_just_pressed"] and (pre["on_floor"] or pre["on_wall"])
        if post["state"] in {"Jump", "DoubleJump"} and pre["state"] != post["state"] and not authorized:
            violations["jump_entry_requires_requested_contact"].append(index)
        if not pre["moving_up"] and post["moving_up"] and not authorized:
            violations["new_ascent_requires_requested_contact"].append(index)
        if post["state"] == "WallJump":
            contact = pre["on_wall"] or post["on_wall"]
            # Collision flags update in move_and_slide AFTER the state callback.
            # Landing can retain WallJump for that one post-move observation;
            # the following native callback must actually leave the slide.
            landing_exit = False
            if post["on_floor"] and not pre["on_floor"] and index + 1 < len(runtime):
                following = runtime[index + 1]
                landing_exit = (following["pre"]["on_floor"] and following["post"]["on_floor"]
                    and following["post"]["state"] in {"Idle", "Run"}
                    and not post["moving_up"] and not following["post"]["moving_up"])
            if not contact or post["on_floor"] and not landing_exit:
                violations["wall_slide_contact"].append(index)
            else:
                wall_slide_frames.append(index)
        if post["state"] in {"DoubleJump", "Hit", "Appear"}:
            violations["no_unrequested_other_special_state"].append(index)
    checks.update({key: not frames for key, frames in violations.items()})
    return {"status": "pass" if all(checks.values()) else "fail", "checks": checks,
            "violations": violations, "contact_supported_wall_slide_frames": wall_slide_frames,
            "protocol_id": PROTOCOL_ID,
            "scope": "Fixed normal-level gameplay predicates; no trajectory matching or full-game claim"}


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
            shutil.copy2(ROOT / "adapters/normal-gameplay-v2-godot4" / name, target)
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
              probe="run_jump_land_brake", no_focus=True, offscreen=True,
              normal_level=True, cancel=None, timeout=120, video=False):
    """Run one fixed probe; only the explicit disposable copy gains an adapter.

    All process execution uses the shared Godot lock and owned-tree cancellation.
    Root/operator must first qualify actual offscreen rendering independently.
    """
    runner._check_cancel(cancel)
    if (probe not in PROBES or no_focus is not True or offscreen is not True
            or normal_level is not True or type(video) is not bool):
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
        receipt = {"schema_version": 1, "protocol_revision": PROTOCOL_REVISION, "kind": "supplemental_normal_contact_v2", "probe": probe,
                   "status": decision["status"], "decision": decision, "normal_level": normal_level,
                   "project": str(project), "output": str(run), "video": video,
                   "original_source_hashes": before, "full_source_hashes_before": prepared,
                   "full_source_hashes_after": after, "adapter_hashes": _adapter_hashes(),
                   "startup": startup, "evidence_sha256": evidence,
                   "runner_sha256": sha256(Path(__file__)), "protocol": protocol_record(), "canonical_experiment_acceptance": False,
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
        if (receipt.get("kind") != "supplemental_normal_contact_v2" or receipt.get("project") != str(project)
                or receipt.get("protocol_revision") != PROTOCOL_REVISION
                or receipt.get("output") != str(run) or receipt.get("adapter_hashes") != _adapter_hashes()
                or receipt.get("runner_sha256") != sha256(Path(__file__)) or receipt.get("protocol") != protocol_record()
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
