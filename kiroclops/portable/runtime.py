"""Heart-specific Godot transport. Every engine call uses an owned private desktop.

This is an additive adapter, not a modification of a historical runner. Import
is headless but uses the same non-input desktop/job transport as rendering.
Process isolation does not imply correct pixels or successful gameplay tests.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import threading
import time

from ..common import ROOT
from ..integration import private_desktop_v1 as private

VERSION = "portable-heart-runtime-v1"
ENGINE_VERSION = "4.5.1.stable.official.f62fdbde1"
ENGINE_RELATIVE = ".tools/godot4/Godot_v4.5.1-stable_win64.exe"
ENGINE_SHA256 = "a829242096d640007de9fa93ea923d0f666d13d6a3b472a0231c820be6dc7627"
PRIVATE_SHA256 = "e6faed0d06c92b6e8c4ec602702b1a7cdca9787c1d2a4f39803ce130130526ca"
FPS, FRAME_COUNT, WARMUP = 60, 240, 60
VIEWPORT = [320, 180]
CAPTURE_SCENE = "res://kiroclops_heart/Capture.tscn"
SCENES = frozenset({CAPTURE_SCENE, "res://kiroclops_heart_integrity/Integrity.tscn"})
CUSTOM_DIR = 'config/custom_user_dir_name="KiroClops/portable-heart"'
NORMAL_RECIPES = {
    "normal-combined": ((30, "right", True), (36, "jump", True), (60, "jump", False), (78, "right", False)),
    "normal-slope": ((30, "right", True), (78, "right", False)),
}
_ADAPTER_FILES = ("Capture.tscn", "capture.gd")
_REQUIRED = ("project.godot", "player.gd", "player.tscn", "level_one.tscn",
             "level_tile_map.tscn", "PlayerMovementData.gd", "DefaultMovementData.tres")
_CACHE_DIRS = frozenset({".git", ".godot", ".import", ".runtime", "__pycache__",
                         ".kiro", ".kiroclops-reference", ".kiroclops-evidence"})
_THREAD_LOCK = threading.Lock()
_SCRIPT_ERROR = re.compile(r"SCRIPT ERROR:|(?:Parse|Compile) Error:|Failed to load script|"
                           r"Error loading resource:|Failed loading resource:|Failed to instantiate", re.I)
PROCESS_POLICY = {"atomic_job_assignment": True, "kill_on_job_close": True,
                  "maximum_active_processes": 1,
                  "inherited_handles": ["stdin:NUL", "stdout.log", "stderr.log"],
                  "desktop_handle_inherited": False, "job_handle_inherited": False,
                  "desktop_activation_requested": False}


class PortableRuntimeError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def _need(condition, code, message):
    if not condition:
        raise PortableRuntimeError(code, message)


def _plain(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                  "invalid_path", f"Linked/reparse path rejected: {item}")
            if item.is_file():
                _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                      "invalid_path", f"Only independent regular files are allowed: {item}")
    return path


def _sha(path):
    value = hashlib.sha256()
    with _plain(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _read(path):
    return json.loads(_plain(path).read_text(encoding="utf-8"))


def _write(path, value):
    with _plain(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise PortableRuntimeError("cancelled", "Heart operation cancelled")


def source_hashes(project: Path) -> dict[str, str]:
    """Source/assets/config, excluding only generated caches and sidecars."""
    project = _plain(project)
    _need(project.is_dir(), "invalid_project", "Project directory missing")
    result = {}
    for base, dirs, files in os.walk(project, followlinks=False):
        for name in dirs + files:
            _plain(Path(base) / name)
        dirs[:] = sorted(name for name in dirs if name not in _CACHE_DIRS)
        for name in sorted(files):
            path = Path(base) / name
            if path.suffix.lower() in {".uid", ".import", ".log", ".tmp", ".pyc"} or name in {".DS_Store", "Thumbs.db"}:
                continue
            result[path.relative_to(project).as_posix()] = _sha(path)
    return dict(sorted(result.items()))


def _project(project):
    project = _plain(project)
    _need(project.is_dir() and all((project / name).is_file() for name in _REQUIRED),
          "invalid_project", "A complete Heart project snapshot is required")
    _need(not any(project.is_relative_to(_plain(ROOT / name)) for name in ("vendor", "work")),
          "protected_project", "Use a disposable copy, never vendor/work")
    return project


def _separate(output, project):
    _need(not output.is_relative_to(project) and not project.is_relative_to(output),
          "invalid_path", "Output and project must be disjoint directories")


def _adapter_hashes():
    return {name: _sha(ROOT / "adapters/heart-godot4" / name) for name in _ADAPTER_FILES}


def protocol_record():
    return {"runtime": VERSION, "engine": ENGINE_VERSION, "engine_sha256": ENGINE_SHA256,
            "private_desktop_sha256": PRIVATE_SHA256, "runtime_sha256": _sha(Path(__file__)),
            "adapter_hashes": _adapter_hashes(), "viewport": list(VIEWPORT), "fps": FPS,
            "warmup_ticks": WARMUP, "frame_count": FRAME_COUNT, "fresh_process_per_call": True,
            "import_transport": "private_desktop_headless", "capture_transport": "private_desktop",
            "normal_desktop_fallback": False, "disable_vsync": True,
            "integrity_scene_owns_its_recording_length": True,
            "normal_recipes": {key: [list(event) for event in value] for key, value in NORMAL_RECIPES.items()},
            "slope_checks": "three grounded moving samples per incline/decline, with actual elevation change; no jumps/hazard reset; final grounded rest",
            "source_policy": {"excluded_directories": sorted(_CACHE_DIRS),
                "generated_suffixes": [".uid", ".import", ".log", ".tmp", ".pyc"]}}


def prepare_project(project: Path) -> None:
    """Install the capture fixture/user-directory setting on a fresh source copy."""
    project = _project(project)
    source_hashes(project)
    _need(not (project.parent / "candidate.json").exists(), "protected_project", "Candidate snapshots cannot be prepared")
    for parent in project.parents:
        if (parent / "session.json").is_file():
            state = _read(parent / "session.json")
            _need(not state.get("sealed_at") and not state.get("baseline_hashes"),
                  "protected_project", "Session source is already frozen")
            break
        if parent == ROOT:
            break
    destination = _plain(project / "kiroclops_heart")
    expected = _adapter_hashes()
    if destination.exists():
        _need(source_hashes(destination) == expected, "fixture_changed", "Existing fixture differs; never replace it silently")
    else:
        destination.mkdir()
        for name in _ADAPTER_FILES:
            shutil.copyfile(ROOT / "adapters/heart-godot4" / name, destination / name)
    config = project / "project.godot"
    lines = config.read_text(encoding="utf-8-sig").splitlines()
    _need("[application]" in lines, "invalid_project", "Missing application section")
    if CUSTOM_DIR not in lines or "config/use_custom_user_dir=true" not in lines:
        lines = [line for line in lines if not line.startswith(("config/use_custom_user_dir=", "config/custom_user_dir_name="))]
        index = lines.index("[application]") + 1
        lines[index:index] = ["config/use_custom_user_dir=true", CUSTOM_DIR]
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _assert_prepared(project)


def _assert_prepared(project):
    lines = (project / "project.godot").read_text(encoding="utf-8-sig").splitlines()
    _need(CUSTOM_DIR in lines and "config/use_custom_user_dir=true" in lines,
          "not_prepared", "Prepare the Heart project before source registration")
    _need(source_hashes(project / "kiroclops_heart") == _adapter_hashes(),
          "fixture_changed", "Capture fixture differs from its registered implementation")


def canonical_scenario(scenario):
    _need(isinstance(scenario, dict), "invalid_config", "Scenario must be an object")
    required = {"id", "events", "split", "repeat", "spawn_offset"}
    _need(required <= scenario.keys() and not set(scenario) - required - {"warmup_ticks", "frame_count", "variant", "start_tick", "hold_ticks"},
          "invalid_config", "Unexpected or missing scenario fields")
    value = deepcopy(scenario)
    value.setdefault("warmup_ticks", WARMUP)
    value.setdefault("frame_count", FRAME_COUNT)
    _need(isinstance(value["id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["id"])
          and isinstance(value["split"], str) and value["split"] in {"calibration", "validation", "smoke", "regression"}
          and type(value["repeat"]) is int and 1 <= value["repeat"] <= 3
          and type(value["spawn_offset"]) in {int, float} and math.isfinite(value["spawn_offset"])
          and abs(value["spawn_offset"]) <= 48
          and type(value["warmup_ticks"]) is int and value["warmup_ticks"] == WARMUP
          and type(value["frame_count"]) is int and value["frame_count"] == FRAME_COUNT
          and isinstance(value["events"], list) and len(value["events"]) <= 120,
          "invalid_config", "Invalid deterministic capture scenario")
    _need(value.get("variant", "before") in {"before", "after"}, "invalid_config", "Unknown target variant")
    if "hold_ticks" in value or "start_tick" in value:
        _need(type(value.get("start_tick")) is int and type(value.get("hold_ticks")) is int
              and 0 <= value["start_tick"] < FRAME_COUNT and 1 <= value["hold_ticks"]
              and value["start_tick"] + value["hold_ticks"] < FRAME_COUNT,
              "invalid_config", "Jump timing metadata is invalid")
    for event in value["events"]:
        _need(isinstance(event, dict) and set(event) == {"tick", "action", "pressed"}
              and type(event["tick"]) is int and 0 <= event["tick"] < FRAME_COUNT
              and isinstance(event["action"], str) and event["action"] in {"left", "right", "jump"}
              and type(event["pressed"]) is bool, "invalid_config", "Malformed input event")
    value["events"].sort(key=lambda event: (event["tick"], event["pressed"], event["action"]))
    active = set()
    for event in value["events"]:
        action, pressed = event["action"], event["pressed"]
        _need(pressed != (action in active), "invalid_config", "Duplicate or unmatched input transition")
        (active.add if pressed else active.remove)(action)
        _need(not {"left", "right"} <= active, "invalid_config", "Overlapping directional input")
    _need(not active, "invalid_config", "Every action must be released before the last frame")
    if "hold_ticks" in value:
        expected_jump = [{"tick": value["start_tick"], "action": "jump", "pressed": True},
                         {"tick": value["start_tick"] + value["hold_ticks"], "action": "jump", "pressed": False}]
        _need([event for event in value["events"] if event["action"] == "jump"] == expected_jump,
              "invalid_config", "Jump metadata differs from actual event schedule")
    return value


def _environment(directory, config_path=None):
    directory.mkdir(parents=True, exist_ok=False)
    # Explicit allowlist: AWS/model credentials and parent KIROCLOPS_* never reach Godot.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE",
               "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    for key, name in (("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"),
                      ("USERPROFILE", "profile"), ("TEMP", "temp"), ("TMP", "tmp"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache")):
        path = directory / name
        path.mkdir()
        env[key] = str(path)
    if config_path is not None:
        env["KIROCLOPS_CONFIG"] = str(config_path)
    return env


@contextmanager
def _engine_lock(cancel, timeout):
    """Same workspace lock file as historical runners, without patching them."""
    started, owned, stream, locked = time.monotonic(), False, None, False
    try:
        while not owned:
            _cancel(cancel)
            _need(time.monotonic() - started < timeout, "timeout", "Waiting for the engine lock exhausted timeout")
            owned = _THREAD_LOCK.acquire(timeout=.05)
        path = _plain(ROOT / ".runtime/phase2-godot.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        while not locked:
            _cancel(cancel)
            _need(time.monotonic() - started < timeout, "timeout", "Waiting for another engine exhausted timeout")
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                time.sleep(.05)
        yield max(.001, timeout - (time.monotonic() - started))
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        if stream is not None:
            stream.close()
        if owned:
            _THREAD_LOCK.release()


def _engine():
    path = _plain(ROOT / ENGINE_RELATIVE)
    _need(path.is_file() and _sha(path) == ENGINE_SHA256, "engine_changed", "Pinned Godot 4.5.1 executable differs")
    _need(_sha(Path(private.__file__)) == PRIVATE_SHA256, "runtime_changed", "Audited private transport differs")
    return path


def _argv(project, scene):
    engine = str(_engine())
    if scene is None:
        return [engine, "--path", str(project), "--editor", "--headless", "--import"]
    _need(scene in SCENES and _plain(project / scene[6:]).is_file(),
          "invalid_scene", "Only the registered Heart capture/integrity scene is allowed")
    return [engine, "--path", str(project), "--fixed-fps", "60", "--resolution", "320x180",
            "--rendering-method", "gl_compatibility", "--windowed", "--disable-vsync", scene]


def _verify_process(proof, directory, argv, project, require_window):
    _need(isinstance(proof, dict) and proof.get("runner") == private.VERSION
          and proof.get("status") == "completed" and proof.get("exit_code") == 0
          and proof.get("owned_process_termination_confirmed") is True
          and proof.get("cleanup_errors") == [] and proof.get("policy") == PROCESS_POLICY,
          "isolation_error", "Private process completion/cleanup policy failed")
    _need(proof.get("argv") == argv and proof.get("cwd") == str(project)
          and proof.get("executable_sha256") == ENGINE_SHA256,
          "isolation_error", "Private process command or executable differs")
    evidence = proof.get("evidence", {})
    for name in ("process-start.json", "process-identity.json", "stdout.log", "stderr.log"):
        path = directory / name
        entry = evidence.get(name, {})
        _need(path.is_file() and entry.get("sha256") == _sha(path)
              and type(entry.get("bytes")) is int and entry["bytes"] == path.stat().st_size,
              "isolation_error", "Missing or changed process evidence: " + name)
    start, identity = _read(directory / "process-start.json"), _read(directory / "process-identity.json")
    _need(start.get("runner") == identity.get("runner") == private.VERSION
          and start.get("status") == "starting" and identity.get("status") == "suspended_in_job",
          "isolation_error", "Missing suspended process identity")
    for key in ("argv", "cwd", "executable_sha256", "policy", "desktop_name", "started_at",
                "timeout_seconds", "environment_keys", "environment_sha256"):
        _need(key in start and start[key] == proof.get(key), "isolation_error", "Process-start mismatch: " + key)
    pid = proof.get("process_identity", {})
    _need(isinstance(pid, dict) and all(type(pid.get(key)) is int and pid[key] > 0
          for key in ("pid", "primary_thread_id", "creation_filetime_100ns"))
          and identity.get("process_identity") == pid, "isolation_error", "Invalid process identity")
    desktop = proof.get("desktop_name")
    desktop_path = proof.get("desktop_path")
    _need(isinstance(desktop, str) and bool(desktop) and isinstance(desktop_path, str)
          and desktop_path.endswith("\\" + desktop) and desktop_path == identity.get("desktop_path")
          and identity.get("start_sha256") == _sha(directory / "process-start.json"),
          "isolation_error", "Private desktop identity differs")
    try:
        times = [datetime.fromisoformat(item) for item in (proof["started_at"], identity["recorded_at"],
                                                          proof["resumed_at"], proof["finished_at"])]
        _need(all(item.utcoffset() is not None for item in times) and times == sorted(times),
              "isolation_error", "Process timings are not ordered aware dates")
    except (KeyError, TypeError, ValueError) as error:
        raise PortableRuntimeError("isolation_error", "Missing process timing evidence") from error
    samples = proof.get("desktop_samples")
    _need(isinstance(samples, list) and bool(samples) and all(isinstance(item, dict)
          and item.get("name") == desktop and item.get("receives_input") is False for item in samples),
          "isolation_error", "Non-input desktop proof failed")
    if require_window:
        _need(proof.get("saw_owned_window") is True and any(type(item.get("owned_window_count")) is int
              and item["owned_window_count"] > 0 for item in samples),
              "isolation_error", "Rendered call has no owned private window proof")


def _execute(project, output, *, scene, config_path, cancel, timeout):
    _cancel(cancel)
    _need(type(timeout) in {int, float} and math.isfinite(timeout) and timeout > 0,
          "invalid_config", "Finite positive timeout required")
    project, output = _project(project), _plain(output)
    _separate(output, project)
    _assert_prepared(project)
    argv = _argv(project, scene)
    output.mkdir(parents=True, exist_ok=True)
    directory = output / ("engine." + VERSION)
    _need(not directory.exists() and not (output / "engine.log").exists(), "output_exists", "Runtime evidence already exists")
    config = None
    if scene is not None:
        config_path = _plain(config_path)
        _need(config_path == output / "capture_config.json" and config_path.is_file(),
              "invalid_config", "Capture config must be beside the runtime output")
        data = _read(config_path)
        _need(isinstance(data, dict) and _plain(data.get("output_dir", "")) == output,
              "invalid_config", "Capture output differs from its configuration")
        config = {"path": str(config_path), "sha256": _sha(config_path)}
    before, protocol = source_hashes(project), protocol_record()
    directory.mkdir()
    env = _environment(directory / "environment", config_path)
    receipt = {"schema_version": 1, "runtime": VERSION, "status": "starting", "started_at": _utc(),
               "project": str(project), "scene": scene, "kind": "capture" if scene else "import",
               "argv": argv, "config": config, "protocol": protocol, "source_hashes_before": before,
               "environment_sha256": hashlib.sha256(private.environment_block(env).encode("utf-16-le")).hexdigest(),
               "receipt_path": str(directory / "bridge.json")}
    _write(directory / "bridge-start.json", receipt)
    caught = None
    try:
        with _engine_lock(cancel, timeout) as remaining:
            _cancel(cancel)
            proof = private.run_private_desktop(argv, cwd=project, env=env,
                output_dir=directory / "process", timeout=remaining, cancel=cancel)
        _need(_read(directory / "process/process.json") == proof, "isolation_error", "Returned and saved process receipts differ")
        if proof.get("status") in {"cancelled", "timeout"}:
            raise PortableRuntimeError(proof["status"], "Private Heart process " + proof["status"])
        _verify_process(proof, directory / "process", argv, project, scene is not None)
        _need(proof.get("environment_sha256") == receipt["environment_sha256"], "isolation_error", "Environment binding differs")
        _cancel(cancel)
        receipt["status"] = "completed"
    except BaseException as error:
        caught = error
        receipt["status"] = "error"
        receipt["error"] = {"code": getattr(error, "code", "runtime_error"), "message": str(error)}
    finally:
        try:
            merged = b"\n".join((directory / "process" / name).read_bytes()
                                  for name in ("stdout.log", "stderr.log") if (directory / "process" / name).is_file())
            with (output / "engine.log").open("xb") as stream:
                stream.write(merged)
            receipt["log_sha256"] = _sha(output / "engine.log")
            receipt["source_hashes_after"] = source_hashes(project)
            _need(receipt["source_hashes_after"] == before and protocol_record() == protocol
                  and (config is None or _sha(config_path) == config["sha256"]),
                  "source_changed", "Source/config/runtime changed during the engine operation")
            text = merged.decode("utf-8", errors="replace")
            if caught is None:
                _need(ENGINE_VERSION in text and not _SCRIPT_ERROR.search(text),
                      "engine_error", "Engine version missing or script/resource error in engine log")
        except BaseException as error:
            receipt["status"] = "error"
            receipt["verification_error"] = {"code": getattr(error, "code", "runtime_error"), "message": str(error)}
            caught = caught or error
        receipt["evidence_hashes"] = {p.relative_to(directory).as_posix(): _sha(p)
            for p in [directory / "bridge-start.json", *sorted((directory / "process").glob("*"))] if p.is_file()}
        receipt["finished_at"] = _utc()
        _write(directory / "bridge.json", receipt)
    if caught is not None:
        if isinstance(caught, PortableRuntimeError):
            caught.details["runtime_receipt"] = receipt["receipt_path"]
        raise caught
    return receipt


def _cold_import_dependency_failure(project, output, error):
    """Only Heart's startup theme/font loading before first asset import may retry.

    A completed private process, unchanged source, newly present import products,
    and the exact resource-only error families are required. Script/compiler,
    cancellation, timeout, isolation and source errors never take this route.
    The original error receipt and logs are retained, not relabelled successful.
    """
    if not isinstance(error, PortableRuntimeError) or error.code != "engine_error":
        return False
    try:
        directory = output / ("engine." + VERSION)
        first = _read(directory / "bridge.json")
        proof = _read(directory / "process/process.json")
        _verify_process(proof, directory / "process", _argv(project, None), project, False)
        if (first.get("source_hashes_before") != first.get("source_hashes_after")
                or first["source_hashes_after"] != source_hashes(project)
                or first.get("protocol") != protocol_record()):
            return False
        text = (output / "engine.log").read_text(encoding="utf-8", errors="replace")
        if ENGINE_VERSION not in text or re.search(r"SCRIPT ERROR:|Compile Error:|Failed to load script", text, re.I):
            return False
        lines = [line for line in text.splitlines() if line.startswith("ERROR:")]
        allowed_assets = r"(?:black_9slice\.(?:png|tres)|white_9slice\.(?:png|tres)|default_theme\.tres|pixel_font\.ttf)"
        product = r"\.godot/imported/[A-Za-z0-9_.-]+\.(?:ctex|fontdata)"
        patterns = [r"ERROR: Unable to open file: res://" + product + r"\.",
                    r"ERROR: Cannot open file 'res://" + product + r"'\.",
                    r"ERROR: Failed loading resource: res://(?:" + product + "|" + allowed_assets + r")\.",
                    r"ERROR: res://" + allowed_assets + r":\d+ - Parse Error: \[ext_resource\] referenced non-existent resource at: res://" + allowed_assets + r"\.",
                    r"ERROR: Error loading custom project (?:theme|font) 'res://" + allowed_assets + "'"]
        if not lines or not all(any(re.fullmatch(pattern, line) for pattern in patterns) for line in lines):
            return False
        products = set(re.findall(r"res://(\.godot/imported/[A-Za-z0-9_.-]+\.(?:ctex|fontdata))", text))
        return bool(products) and all(_plain(project / item).is_file() and (project / item).stat().st_size > 0 for item in products)
    except (OSError, KeyError, TypeError, ValueError, PortableRuntimeError):
        return False


def import_project(project: Path, runtime_dir: Path, cancel=None, timeout=180):
    started = time.monotonic()
    project, runtime_dir = _project(project), _plain(runtime_dir)
    attempts = []
    try:
        result = _execute(project, runtime_dir, scene=None, config_path=None, cancel=cancel, timeout=timeout)
    except PortableRuntimeError as error:
        _cancel(cancel)
        if not _cold_import_dependency_failure(project, runtime_dir, error):
            raise
        remaining = timeout - (time.monotonic() - started)
        _need(remaining > 0, "timeout", "Import retry has no remaining operation budget")
        first = runtime_dir / ("engine." + VERSION) / "bridge.json"
        attempts.append({"path": str(first), "sha256": _sha(first), "status": "initial_dependency_load_failed"})
        result = _execute(project, runtime_dir / "clean-retry", scene=None, config_path=None,
                          cancel=cancel, timeout=remaining)
    attempts.append({"path": result["receipt_path"], "sha256": _sha(result["receipt_path"]), "status": "completed"})
    summary = dict(result, import_attempts=attempts, import_summary_path=str(runtime_dir / "import-result.json"))
    _write(runtime_dir / "import-result.json", summary)
    return summary


def run_adapter(project: Path, output_dir: Path, *, scene: str, config_path: Path,
                cancel=None, timeout=120) -> dict:
    """Route a registered scene only; integrity adapters validate their own outputs."""
    return _execute(project, output_dir, scene=scene, config_path=config_path, cancel=cancel, timeout=timeout)


def validate_recording(recording, scenario, normal_level=False):
    scenario = canonical_scenario(scenario)
    _need(isinstance(recording, dict) and recording.get("status") == "ok"
          and recording.get("fps") == FPS and recording.get("viewport") == VIEWPORT
          and recording.get("frame_count") == FRAME_COUNT and recording.get("settled") is True
          and recording.get("normal_level") is normal_level
          and recording.get("active_warmup_ticks") == WARMUP,
          "recording_error", "Capture did not meet viewport/active-warmup/settling contract")
    frames = recording.get("frames")
    _need(isinstance(frames, list) and len(frames) == FRAME_COUNT, "recording_error", "Exactly 240 frames are required")
    active = set()
    for index, frame in enumerate(frames):
        for event in scenario["events"]:
            if event["tick"] == index:
                (active.add if event["pressed"] else active.remove)(event["action"])
        _need(isinstance(frame, dict) and set(frame) == {"frame_index", "physics_tick", "time_s", "actions"}
              and type(frame["frame_index"]) is int and frame["frame_index"] == index
              and type(frame["physics_tick"]) is int and frame["physics_tick"] == WARMUP + index
              and type(frame["time_s"]) in {int, float} and math.isfinite(frame["time_s"])
              and abs(frame["time_s"] - index / FPS) < 1e-6 and frame["actions"] == sorted(active),
              "recording_error", f"Frame {index} timing/input differs from declared schedule")
    observations = recording.get("observations")
    required = {"frame_index", "physics_tick", "on_floor", "on_wall", "moving_up",
                "moving_horizontal", "jump_edge", "hazard_entries", "ground_incline", "ground_decline",
                "ground_rise", "ground_descent"}
    _need(isinstance(observations, list) and len(observations) == FRAME_COUNT,
          "recording_error", "Contact observations missing")
    prior_hazards = 0
    for index, item in enumerate(observations):
        _need(isinstance(item, dict) and set(item) == required
              and type(item["frame_index"]) is int and item["frame_index"] == index
              and type(item["physics_tick"]) is int and item["physics_tick"] == WARMUP + index
              and all(type(item[key]) is bool for key in ("on_floor", "on_wall", "moving_up", "moving_horizontal", "jump_edge",
                                                         "ground_incline", "ground_decline", "ground_rise", "ground_descent"))
              and type(item["hazard_entries"]) is int and item["hazard_entries"] >= prior_hazards,
              "recording_error", "Contact observation format or tick mapping differs")
        prior_hazards = item["hazard_entries"]


def gameplay_checks(recording, scenario):
    """Observed normal-route smoke, not exact trajectory or all-mechanic proof."""
    validate_recording(recording, scenario, normal_level=True)
    observations, frames = recording["observations"], recording["frames"]
    recipe = NORMAL_RECIPES.get(scenario["id"])
    _need(recipe is not None and scenario["spawn_offset"] == 0
          and [(event["tick"], event["action"], event["pressed"]) for event in canonical_scenario(scenario)["events"]] == list(recipe),
          "invalid_config", "Normal gameplay acceptance requires its exact qualified route")
    jump_edges = [event["tick"] for event in scenario["events"] if event["action"] == "jump" and event["pressed"]]
    releases = [event["tick"] for event in scenario["events"] if event["action"] in {"left", "right"} and not event["pressed"]]
    takeoffs = [index for index in range(1, FRAME_COUNT) if observations[index - 1]["on_floor"]
                and not observations[index]["on_floor"] and observations[index]["moving_up"]
                and any(0 <= index - edge <= 1 for edge in jump_edges)]
    landings = [index for index in range(1, FRAME_COUNT) if not observations[index - 1]["on_floor"]
                and observations[index]["on_floor"] and any(index > takeoff for takeoff in takeoffs)]
    checks = {
        "ground_run": any(item["on_floor"] and item["moving_horizontal"]
                          and bool(set(frames[index]["actions"]) & {"left", "right"})
                          for index, item in enumerate(observations)),
        "requested_jump_takeoff": bool(takeoffs),
        "landing_after_jump": bool(landings),
        "brake_to_ground_rest": bool(releases) and bool(landings)
            and max(releases) < FRAME_COUNT - 5
            and all(item["on_floor"] and not item["moving_horizontal"] for item in observations[-5:]),
        "no_hazard_reset": all(item["hazard_entries"] == 0 for item in observations),
    }
    if scenario["id"] == "normal-slope":
        checks = {"ground_run": checks["ground_run"],
                  "grounded_incline_with_elevation": sum(item["on_floor"] and item["moving_horizontal"]
                      and item["ground_incline"] and item["ground_rise"] for item in observations) >= 3,
                  "grounded_decline_with_elevation": sum(item["on_floor"] and item["moving_horizontal"]
                      and item["ground_decline"] and item["ground_descent"] for item in observations) >= 3,
                  "no_jump_input": not jump_edges and not any(item["jump_edge"] for item in observations),
                  "grounded_slope_route": all(item["on_floor"] for item in observations[30:84]),
                  "brake_to_ground_rest": all(item["on_floor"] and not item["moving_horizontal"] for item in observations[-5:]),
                  "no_hazard_reset": checks["no_hazard_reset"]}
    return {"status": "pass" if all(checks.values()) else "fail", "checks": checks,
            "takeoff_frames": takeoffs, "landing_frames": landings,
            "scope": ("Original-map grounded incline/decline with elevation change, input-free braking, no jump or hazard reset; finite route only."
                      if scenario["id"] == "normal-slope" else
                      "Normal route run/jump/land/brake and no observed hazard reset; slopes/wall/coyote require separate probes.")}


def _frames(run):
    directory = _plain(run / "frames")
    expected = [f"{index:06d}.png" for index in range(FRAME_COUNT)]
    _need(directory.is_dir() and sorted(p.name for p in directory.iterdir()) == expected,
          "recording_error", "Frame directory must contain exactly the 240 native PNGs")
    hashes = {}
    for name in expected:
        path = _plain(directory / name)
        with path.open("rb") as stream:
            header = stream.read(24)
        _need(len(header) == 24 and header[:8] == b"\x89PNG\r\n\x1a\n"
              and header[12:16] == b"IHDR" and list(struct.unpack(">II", header[16:24])) == VIEWPORT,
              "recording_error", "Native PNG dimensions/header differ: " + name)
        hashes[name] = _sha(path)
    return hashes


def capture_project(project: Path, run: Path, scenario, normal_level=False, cancel=None, timeout=120) -> dict:
    _cancel(cancel)
    project, run = _project(project), _plain(run)
    _separate(run, project)
    _need(type(normal_level) is bool and not run.exists(), "invalid_config", "New run directory and boolean normal_level required")
    scenario = canonical_scenario(scenario)
    _assert_prepared(project)
    before = source_hashes(project)
    run.mkdir(parents=True)
    config = dict(scenario, output_dir=str(run), game="heart", normal_level=normal_level,
                  protocol=VERSION, viewport=list(VIEWPORT), fps=FPS)
    _write(run / "capture_config.json", config)
    try:
        receipt = run_adapter(project, run, scene=CAPTURE_SCENE,
                              config_path=run / "capture_config.json", cancel=cancel, timeout=timeout)
        recording = _read(run / "recording.json")
        validate_recording(recording, scenario, normal_level)
        gameplay = gameplay_checks(recording, scenario) if normal_level else None
        frame_hashes = _frames(run)
        _cancel(cancel)
        after = source_hashes(project)
        _need(after == before == receipt["source_hashes_before"] == receipt["source_hashes_after"],
              "source_changed", "Candidate source changed during capture")
        manifest = {"schema_version": 1, "run_id": run.name, "status": "ok", "game": "heart",
            "variant": scenario.get("variant", "before"), "fps": FPS, "viewport": list(VIEWPORT), "frame_count": FRAME_COUNT,
            "warmup_ticks": WARMUP, "scenario": scenario, "normal_level": normal_level,
            "gameplay_status": gameplay["status"] if gameplay else "not_applicable", "gameplay": gameplay,
            "settled": True, "frames": recording["frames"], "raw_frame_hashes": frame_hashes,
            "raw_frames_sha256": _digest(frame_hashes),
            "provenance": {"project": str(project), "engine": ENGINE_VERSION, "engine_sha256": ENGINE_SHA256,
                "full_source_hashes_before": before, "full_source_hashes_after": after,
                "source_digest_before": _digest(before), "source_digest_after": _digest(after),
                "runtime_receipt": receipt["receipt_path"], "runtime_receipt_sha256": _sha(receipt["receipt_path"]),
                "recording_sha256": _sha(run / "recording.json"), "capture_config_sha256": _sha(run / "capture_config.json"),
                "protocol": protocol_record(), "coordinates_from_engine": False}}
        _write(run / "manifest.json", manifest)
        return manifest
    except BaseException as error:
        _write(run / "failure.json", {"status": "error", "code": getattr(error, "code", "capture_error"),
            "message": str(error), "source_hashes_before": before})
        raise


def verify_runtime_receipt(path: Path, *, project=None, expected_source_hashes=None):
    """Recheck private route, current source, exact command and saved identity."""
    try:
        path = _plain(path)
        if not path.is_file():
            return {"status": "unverified", "errors": ["Runtime receipt missing"]}
        receipt = _read(path)
        directory, output = path.parent, path.parent.parent
        actual = _project(receipt["project"])
        _need(path == output / ("engine." + VERSION) / "bridge.json"
              and receipt.get("receipt_path") == str(path), "invalid_path", "Receipt location differs")
        _separate(output, actual)
        _assert_prepared(actual)
        _need(project is None or _plain(project) == actual, "source_changed", "Project identity differs")
        source = source_hashes(actual)
        _need(source == receipt["source_hashes_before"] == receipt["source_hashes_after"]
              and (expected_source_hashes is None or source == expected_source_hashes),
              "source_changed", "Source map differs from the immutable snapshot")
        _need(receipt["status"] == "completed" and receipt["runtime"] == VERSION
              and receipt["protocol"] == protocol_record(), "runtime_changed", "Runtime/protocol not verified")
        scene = receipt["scene"]
        argv = _argv(actual, scene)
        _need(receipt["argv"] == argv and receipt["kind"] == ("capture" if scene else "import"),
              "invalid_config", "Command or route differs")
        required = {"bridge-start.json", "process/process.json", "process/process-start.json",
                    "process/process-identity.json", "process/stdout.log", "process/stderr.log"}
        _need(required <= receipt["evidence_hashes"].keys(), "isolation_error", "Required process evidence missing")
        for relative, expected in receipt["evidence_hashes"].items():
            evidence = _plain(directory / relative)
            _need(evidence.is_relative_to(directory) and evidence != path and _sha(evidence) == expected,
                  "isolation_error", "Runtime evidence changed")
        start = _read(directory / "bridge-start.json")
        for key in ("runtime", "started_at", "project", "scene", "kind", "argv", "config", "protocol",
                    "source_hashes_before", "environment_sha256", "receipt_path"):
            _need(start.get(key) == receipt.get(key), "isolation_error", "Bridge-start mismatch: " + key)
        if scene is not None:
            config = _plain(receipt["config"]["path"])
            _need(config == output / "capture_config.json" and _sha(config) == receipt["config"]["sha256"]
                  and _plain(_read(config)["output_dir"]) == output, "invalid_config", "Capture config differs")
        else:
            _need(receipt["config"] is None, "invalid_config", "Import must not bind capture config")
        proof = _read(directory / "process/process.json")
        _verify_process(proof, directory / "process", argv, actual, scene is not None)
        _need(proof["environment_sha256"] == receipt["environment_sha256"], "isolation_error", "Environment proof differs")
        log = output / "engine.log"
        _need(_sha(log) == receipt["log_sha256"], "isolation_error", "Engine log changed")
        text = log.read_text(encoding="utf-8", errors="replace")
        merged = b"\n".join((directory / "process" / name).read_bytes() for name in ("stdout.log", "stderr.log"))
        _need(log.read_bytes() == merged, "isolation_error", "Merged log is not the actual process output")
        _need(ENGINE_VERSION in text and not _SCRIPT_ERROR.search(text), "engine_error", "Engine log failed")
        return {"status": "pass", "errors": [], "receipt_path": str(path), "receipt_sha256": _sha(path),
                "scope": "Process/source isolation only; recording/gameplay acceptance is separate."}
    except (OSError, KeyError, TypeError, ValueError, PortableRuntimeError) as error:
        return {"status": "fail", "errors": [str(error)]}
