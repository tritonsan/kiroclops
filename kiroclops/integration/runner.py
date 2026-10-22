"""Isolated Slay captures for editable Phase 2 sessions.

The caller supplies a prepared, immutable candidate snapshot. This module never
selects a Phase 1 work copy and never applies a controller patch. Runtime caches
may change; game source, assets and the frozen fixture must remain unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from ..capture import engine_path, process_options, validate_recording
from ..common import ROOT, read_json, sha256, write_json


class RunnerError(RuntimeError):
    """A stable machine-readable failure with a useful human explanation."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


_THREAD_LOCK = threading.Lock()
_CUSTOM_DIR = 'config/custom_user_dir_name="KiroClops/phase2-target"'
_CACHE_DIRS = {".git", ".godot", ".import", ".runtime", "__pycache__",
               ".kiro", ".kiroclops-reference", ".kiroclops-evidence"}
_REQUIRED_FILES = (
    "project.godot", "source/characters/player/player.gd",
    "source/characters/player/player.tscn", "source/maps/map_scenes/map_demo.tscn",
)
_SCRIPT_FAILURE = re.compile(
    r"SCRIPT ERROR:|(?:Parse|Compile) Error:|Failed to load script|"
    r"Error loading resource:|Failed loading resource:|Failed to instantiate", re.I)


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise RunnerError("cancelled", "Godot job was cancelled")


def _engine() -> Path:
    try:
        return engine_path("slay")
    except FileNotFoundError as exc:
        raise RunnerError("engine_missing", str(exc)) from exc


def _project_path(project: Path) -> Path:
    project = Path(project).resolve()
    if not project.is_dir() or any(not (project / name).is_file() for name in _REQUIRED_FILES):
        raise RunnerError("invalid_project", "Expected a complete Slay project snapshot", {"project": str(project)})
    # These are published evidence/source copies, never Phase 2 scratch space.
    for protected in (ROOT / "vendor", ROOT / "work"):
        if project.is_relative_to(protected.resolve()):
            raise RunnerError("protected_project", "Phase 2 requires a fresh session project, not vendor/work")
    return project


def _assert_outside(path: Path, project: Path, name: str) -> None:
    if path.is_relative_to(project) or project.is_relative_to(path):
        raise RunnerError("invalid_path", f"{name} must be separate from the candidate project")


def source_hashes(project: Path) -> dict[str, str]:
    """Hash source, helpers, scenes, config and assets, excluding generated caches.

    Godot creates .uid and .import sidecars during import; these are excluded along
    with cache directories. Their underlying source/asset bytes remain covered.
    Symlinks/junctions are rejected rather than silently hashing external files.
    """
    project = Path(project).resolve()
    hashes = {}
    for base, dirs, files in os.walk(project, followlinks=False):
        base_path = Path(base)
        dirs[:] = sorted(d for d in dirs if d not in _CACHE_DIRS)
        for name in dirs + files:
            item = base_path / name
            if item.is_symlink() or not item.resolve().is_relative_to(project):
                raise RunnerError("invalid_project", "Project snapshot contains a linked or external path", {"path": str(item)})
        for name in sorted(files):
            path = base_path / name
            if path.suffix.lower() in {".uid", ".import", ".log", ".tmp", ".pyc"}:
                continue
            if name in {".DS_Store", "Thumbs.db"}:
                continue
            hashes[path.relative_to(project).as_posix()] = sha256(path)
    return hashes


def _digest(hashes: dict) -> str:
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prepare_project(project: Path) -> None:
    """Prepare a new session target before its baseline is frozen; idempotent."""
    project = _project_path(project)
    source_hashes(project)  # Check links before writing into the supplied tree.
    adapter = ROOT / "adapters/godot4"
    destination = project / "kiroclops_capture"
    for original in sorted(adapter.rglob("*")):
        if original.is_file():
            target = destination / original.relative_to(adapter)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or sha256(target) != sha256(original):
                shutil.copy2(original, target)
    path = project / "project.godot"
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if "[application]" not in lines:
        raise RunnerError("invalid_project", "project.godot has no application section")
    if "config/use_custom_user_dir=true" not in lines or _CUSTOM_DIR not in lines:
        lines = [line for line in lines if not line.startswith(
            ("config/use_custom_user_dir=", "config/custom_user_dir_name="))]
        index = lines.index("[application]") + 1
        lines[index:index] = ["config/use_custom_user_dir=true", _CUSTOM_DIR]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _assert_prepared(project)


def _assert_prepared(project: Path) -> None:
    lines = (project / "project.godot").read_text(encoding="utf-8-sig").splitlines()
    if "config/use_custom_user_dir=true" not in lines or _CUSTOM_DIR not in lines:
        raise RunnerError("not_prepared", "Call prepare_project before freezing the session baseline")
    adapter = ROOT / "adapters/godot4"
    expected = {p.relative_to(adapter).as_posix(): sha256(p) for p in adapter.rglob("*") if p.is_file()}
    actual = source_hashes(project / "kiroclops_capture")
    if actual != expected:
        raise RunnerError("fixture_changed", "Candidate capture adapter differs from the frozen fixture")


@contextmanager
def _godot_lock(cancel=None, timeout: float = 180):
    """Serialize threads and independent MCP/server processes on this workspace."""
    started = time.monotonic()
    acquired = False
    stream = None
    locked = False
    try:
        while not acquired:
            _check_cancel(cancel)
            if time.monotonic() - started >= timeout:
                raise RunnerError("lock_timeout", "Timed out waiting for the Godot process lock")
            acquired = _THREAD_LOCK.acquire(timeout=min(.05, timeout))
        lock_path = ROOT / ".runtime/phase2-godot.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        while not locked:
            _check_cancel(cancel)
            if time.monotonic() - started >= timeout:
                raise RunnerError("lock_timeout", "Timed out waiting for another Godot process")
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                time.sleep(.05)
        _check_cancel(cancel)
        yield
    finally:
        if locked and stream is not None:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        if stream is not None:
            stream.close()
        if acquired:
            _THREAD_LOCK.release()


def _terminate_owned_tree(proc) -> None:
    """Terminate only this subprocess and its descendants, never other Godot jobs."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, **process_options())
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise RunnerError("cleanup_error", "Owned child process did not stop", {"pid": proc.pid}) from exc


def _run_process(args, *, cwd, env, log_path, cancel=None, timeout=120) -> int:
    _check_cancel(cancel)
    if timeout <= 0:
        raise RunnerError("invalid_config", "Process timeout must be positive")
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    options = process_options()
    if os.name != "nt":
        options["start_new_session"] = True
    with log_path.open("wb") as log:
        try:
            proc = subprocess.Popen([str(arg) for arg in args], cwd=cwd, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, **options)
        except OSError as exc:
            raise RunnerError("launch_error", f"Cannot launch {args[0]}", {"error": str(exc)}) from exc
        started = time.monotonic()
        try:
            while True:
                _check_cancel(cancel)
                result = proc.poll()
                if result is not None:
                    return result
                if time.monotonic() - started >= timeout:
                    raise RunnerError("timeout", "Child process exceeded its time limit", {"log": str(log_path)})
                if cancel is not None:
                    cancel.wait(.05)
                else:
                    time.sleep(.05)
        finally:
            if proc.poll() is None:
                _terminate_owned_tree(proc)


def _runtime_environment(runtime_dir: Path) -> dict:
    env = dict(os.environ)
    env.pop("KIROCLOPS_CONFIG", None)
    for key, directory in (("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"),
                           ("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"),
                           ("XDG_CACHE_HOME", "cache")):
        path = runtime_dir / directory
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    return env


def _runtime_path(runtime_dir=None) -> Path:
    return (Path(runtime_dir).resolve() if runtime_dir is not None else
            ROOT / ".runtime/phase2" / uuid.uuid4().hex)


def _ensure_unchanged(project: Path, before: dict) -> dict:
    after = source_hashes(project)
    if before != after:
        changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        raise RunnerError("source_changed", "Candidate source changed during execution; discard this result", {"changed": changed})
    return after


def _engine_failure(returncode: int, log_path: Path, code: str) -> None:
    log = log_path.read_text(encoding="utf-8", errors="replace")
    # Godot can log a sandbox certificate-store warning without affecting the
    # game. Only script/resource failures or a nonzero exit invalidate a run.
    if returncode or _SCRIPT_FAILURE.search(log):
        raise RunnerError(code, f"Godot failed; see {log_path}", {"returncode": returncode, "log": str(log_path)})


def import_project(project: Path, *, runtime_dir=None, cancel=None, timeout=180) -> None:
    _check_cancel(cancel)
    project = _project_path(project)
    _assert_prepared(project)
    runtime = _runtime_path(runtime_dir)
    _assert_outside(runtime, project, "Runtime directory")
    env = _runtime_environment(runtime)
    log_path = runtime / "import.log"
    with _godot_lock(cancel=cancel, timeout=timeout):
        before = source_hashes(project)
        try:
            result = _run_process([_engine(), "--path", project, "--editor", "--headless", "--import"],
                                  cwd=project, env=env, log_path=log_path, cancel=cancel, timeout=timeout)
            _engine_failure(result, log_path, "import_error")
        finally:
            _ensure_unchanged(project, before)


def _validate_config(hold_ticks, variant, repeat, spawn_offset, normal_level, extra_config) -> dict:
    if type(hold_ticks) is not int or hold_ticks not in {3, 24, 8, 14}:
        raise RunnerError("invalid_config", "Supported hold ticks are 3, 24, 8 and 14")
    if not isinstance(variant, str) or variant not in {"before", "after"} or type(repeat) is not int or repeat not in {1, 2, 3}:
        raise RunnerError("invalid_config", "Expected before/after variant and repeat 1, 2 or 3")
    if isinstance(spawn_offset, bool) or not isinstance(spawn_offset, (int, float)) or not math.isfinite(spawn_offset):
        raise RunnerError("invalid_config", "spawn_offset must be finite")
    if type(normal_level) is not bool:
        raise RunnerError("invalid_config", "normal_level must be boolean")
    if extra_config is not None and not isinstance(extra_config, dict):
        raise RunnerError("invalid_config", "extra_config must be an object")
    extras = dict(extra_config or {})
    if set(extras) - {"extra_inputs", "smoke_wall"}:
        raise RunnerError("invalid_config", "extra_config may only contain extra_inputs and smoke_wall")
    if "smoke_wall" in extras and type(extras["smoke_wall"]) is not bool:
        raise RunnerError("invalid_config", "smoke_wall must be boolean")
    if "extra_inputs" in extras and not isinstance(extras["extra_inputs"], list):
        raise RunnerError("invalid_config", "extra_inputs must be a list")
    for event in extras.get("extra_inputs", []):
        if (not isinstance(event, dict) or set(event) != {"tick", "action", "pressed"}
                or type(event["tick"]) is not int or not 0 <= event["tick"] < 240
                or event["action"] not in {"ui_left", "ui_right", "ui_up", "ui_down"}
                or type(event["pressed"]) is not bool):
            raise RunnerError("invalid_config", "Malformed deterministic input event")
    return extras


def _validate_actions(recording: dict, config: dict) -> None:
    """Also validate every commanded action when regression inputs are present."""
    active = set()
    for index, frame in enumerate(recording["frames"]):
        if index == config["start_tick"]:
            active.add("ui_up")
        if index == config["start_tick"] + config["hold_ticks"]:
            active.discard("ui_up")
        for event in config.get("extra_inputs", []):
            if event["tick"] == index:
                (active.add if event["pressed"] else active.discard)(event["action"])
        if set(frame["actions"]) != active:
            raise RunnerError("recording_error", "Recorded action schedule differs from the requested inputs", {"frame": index})


def capture_project(project: Path, output: Path, *, hold_ticks, variant="after", repeat=1,
                    spawn_offset=0, normal_level=False, extra_config=None, cancel=None,
                    timeout=120, video=True) -> Path:
    _check_cancel(cancel)
    extras = _validate_config(hold_ticks, variant, repeat, spawn_offset, normal_level, extra_config)
    project = _project_path(project)
    _assert_prepared(project)
    run = Path(output).resolve()
    _assert_outside(run, project, "Capture output")
    if run.exists() and (not run.is_dir() or any(run.iterdir())):
        raise RunnerError("output_exists", "Refusing to overwrite an existing capture", {"output": str(run)})
    (run / "frames").mkdir(parents=True, exist_ok=True)
    config = dict(output_dir=str(run), hold_ticks=hold_ticks, start_tick=30, warmup_ticks=60,
                  frame_count=240, spawn_offset=spawn_offset, normal_level=normal_level, **extras)
    config_path = write_json(run / "capture_config.json", config)
    runtime = _runtime_path()
    env = _runtime_environment(runtime)
    env["KIROCLOPS_CONFIG"] = str(config_path)
    started = time.perf_counter()
    before, after = {}, {}
    try:
        with _godot_lock(cancel=cancel, timeout=timeout):
            _assert_prepared(project)
            before = source_hashes(project)
            engine = _engine()
            args = [engine, "--path", project, "--fixed-fps", "60", "--resolution", "640x360", "--windowed",
                    "res://kiroclops_capture/Capture.tscn", "--rendering-method", "gl_compatibility"]
            try:
                result = _run_process(args, cwd=project, env=env, log_path=run / "engine.log", cancel=cancel, timeout=timeout)
            finally:
                after = source_hashes(project)
                _ensure_unchanged(project, before)
            _engine_failure(result, run / "engine.log", "engine_error")
            try:
                recording = read_json(run / "recording.json")
                validate_recording(recording, config, "slay")
                _validate_actions(recording, config)
                for index in range(240):
                    frame = run / f"frames/{index:06d}.png"
                    if not frame.is_file() or frame.stat().st_size == 0:
                        raise ValueError(f"Missing or empty frame {index}")
            except RunnerError:
                raise
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                raise RunnerError("recording_error", str(exc), {"output": str(run)}) from exc
            version_log = runtime / "version.log"
            version_result = _run_process([engine, "--version"], cwd=project, env=env,
                                          log_path=version_log, cancel=cancel, timeout=15)
            if version_result:
                raise RunnerError("engine_error", "Could not read Godot version")
            version = version_log.read_text(encoding="utf-8", errors="replace").strip()
        sources = read_json(ROOT / "config/sources.json")
        controller_prefix = "source/characters/player/"
        controller = {name: digest for name, digest in before.items()
                      if name.startswith(controller_prefix) and name.endswith(".gd")}
        fixture_paths = ("project.godot", "kiroclops_capture/Capture.tscn", "kiroclops_capture/capture.gd",
                         "source/maps/map_scenes/map_demo.tscn", "source/characters/player/player.tscn")
        provenance = {"source_commit": sources["slay"]["commit"], "engine": version,
                      "controller_hashes": controller, "fixture_hashes": {name: before[name] for name in fixture_paths},
                      "capture_script_sha256": sha256(ROOT / "adapters/godot4/capture.gd"),
                      "full_source_hashes_before": before, "full_source_hashes_after": after,
                      "source_digest_before": _digest(before), "source_digest_after": _digest(after),
                      "project": str(project), "runtime_dir": str(runtime),
                      "wall_seconds": round(time.perf_counter() - started, 3)}
        manifest = {"schema_version": 1, "run_id": run.name, "status": "ok", "game": "slay", "variant": variant,
                    "fps": 60, "viewport": recording["viewport"], "frame_count": 240, "normal_level": normal_level,
                    "scenario": {"hold_ticks": hold_ticks, "start_tick": 30,
                                 "split": "calibration" if hold_ticks in {3, 24} else "validation",
                                 "repeat": repeat, "spawn_offset": spawn_offset},
                    "frames": recording["frames"], "settled": recording["settled"], "provenance": provenance}
        if video:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RunnerError("video_error", "FFmpeg is missing; recorded PNGs remain available")
            args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "60", "-i", str(run / "frames/%06d.png"),
                    "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(run / "video.mp4")]
            result = _run_process(args, cwd=run, env=env, log_path=run / "video.log", cancel=cancel, timeout=timeout)
            if result or not (run / "video.mp4").is_file() or not (run / "video.mp4").stat().st_size:
                raise RunnerError("video_error", "Video encoding failed; recorded PNGs remain available")
        _check_cancel(cancel)
        # Detect edits made while frames were validated or a preview was encoded.
        after = _ensure_unchanged(project, before)
        provenance["full_source_hashes_after"] = after
        provenance["source_digest_after"] = _digest(after)
        write_json(run / "manifest.json", manifest)
        return run
    except Exception as exc:
        if not isinstance(exc, RunnerError):
            exc = RunnerError("io_error", str(exc))
        write_json(run / "failure.json", {"status": "error", "code": exc.code, "error": str(exc),
                   "details": exc.details, "full_source_hashes_before": before, "full_source_hashes_after": after})
        raise exc


def _state_trace(run: Path) -> list[dict]:
    text = (run / "engine.log").read_text(encoding="utf-8", errors="replace")
    return [{"physics_tick": int(tick), "state": state} for tick, state in
            re.findall(r"KIROCLOPS_STATE physics_tick=(\d+) state=(\w+)", text)]


def _state_sequence(trace: list[dict], expected: list[str], *, after_tick=60) -> bool:
    remaining = iter(expected)
    wanted = next(remaining, None)
    for event in trace:
        if event["physics_tick"] >= after_tick and event["state"] == wanted:
            wanted = next(remaining, None)
    return wanted is None


def smoke_project(project: Path, output_root: Path, *, cancel=None) -> dict:
    """Check actual runtime states on original normal level and three fixtures.

    State traces check movement features, not numeric similarity or completion
    of the whole game. Coordinate/velocity telemetry never leaves the adapter.
    """
    _check_cancel(cancel)
    root = Path(output_root).resolve()
    recipes = {
        "normal": dict(hold_ticks=24, normal_level=True),
        "ground": dict(hold_ticks=3, video=False),
        "walk-double": dict(hold_ticks=3, video=False, extra_config={"extra_inputs": [
            {"tick": 0, "action": "ui_right", "pressed": True}, {"tick": 20, "action": "ui_right", "pressed": False},
            {"tick": 40, "action": "ui_up", "pressed": True}, {"tick": 43, "action": "ui_up", "pressed": False}]}),
        "wall": dict(hold_ticks=3, video=False, extra_config={"smoke_wall": True, "extra_inputs": [
            {"tick": 5, "action": "ui_right", "pressed": True}, {"tick": 80, "action": "ui_right", "pressed": False},
            {"tick": 70, "action": "ui_up", "pressed": True}, {"tick": 73, "action": "ui_up", "pressed": False}]}),
    }
    expected = {"normal": ["Jump", "Fall", "Idle"], "ground": ["Jump", "Fall", "Idle"],
                "walk-double": ["Run", "Jump", "DoubleJump", "Fall", "Idle"],
                "wall": ["Run", "Jump", "WallJump", "Jump", "Fall", "Idle"]}
    runs = {}
    digests = set()
    for name, kwargs in recipes.items():
        run = capture_project(project, root / name, cancel=cancel, **kwargs)
        manifest = read_json(run / "manifest.json")
        trace = _state_trace(run)
        digest = manifest["provenance"]["source_digest_before"]
        digests.add(digest)
        passed = _state_sequence(trace, expected[name])
        if name == "ground":
            passed = passed and sum(event["state"] == "Jump" and event["physics_tick"] >= 60 for event in trace) == 1
        runs[name] = {"status": "passed" if passed else "failed", "run_dir": str(run),
                      "manifest": str(run / "manifest.json"), "engine_log": str(run / "engine.log"),
                      "expected_states": expected[name], "state_trace": trace, "source_digest": digest}
    same_source = len(digests) == 1
    passed = same_source and all(run["status"] == "passed" for run in runs.values())
    result = {"status": "passed" if passed else "failed", "same_source_all_runs": same_source,
              "normal_video": str(root / "normal/video.mp4"), "runs": runs,
              "scope": "Normal-level startup, walk, jump, double jump, wall launch and return to ground; not full-game completion."}
    write_json(root / "smoke.json", result)
    return result
