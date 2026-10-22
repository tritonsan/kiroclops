"""Opt-in process-local bridge to the qualified private Windows desktop runner.

No registered v1 file is changed. Enter this context in the *worker process*;
contexts do not propagate through subprocess creation. Real Godot4 windows use
the pinned direct GUI binary. Isolation errors never fall back to the desktop.
This routes existing probes but does not change their independent window rules.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import threading
from datetime import datetime, timezone

from ..common import ROOT
from . import runner
from . import private_desktop_v1 as private

VERSION = "isolated-runtime-v2"
GUI_RELATIVE = ".tools/godot4/Godot_v4.5.1-stable_win64.exe"
GUI_SHA256 = "a829242096d640007de9fa93ea923d0f666d13d6a3b472a0231c820be6dc7627"
PRIVATE_SHA256 = "e6faed0d06c92b6e8c4ec602702b1a7cdca9787c1d2a4f39803ce130130526ca"
ENGINE_VERSION = "4.5.1.stable.official.f62fdbde1"
SCENES = frozenset({"res://kiroclops_capture/Capture.tscn",
                    "res://kiroclops_ground/Capture.tscn",
                    "res://kiroclops_movement_probe/Capture.tscn"})
PROCESS_POLICY = {"atomic_job_assignment": True, "kill_on_job_close": True,
                  "maximum_active_processes": 1,
                  "inherited_handles": ["stdin:NUL", "stdout.log", "stderr.log"],
                  "desktop_handle_inherited": False, "job_handle_inherited": False,
                  "desktop_activation_requested": False}
_CONTEXT_LOCK = threading.Lock()
_ACTIVE = None


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise runner.RunnerError("invalid_path", f"Linked/reparse path rejected: {item}")
    return path


def _write(path, data):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _verify_process_identity(proof, directory):
    """Bind the resumed process to the helper's pre-resume identity receipt.

    This replays saved evidence only. It neither queries a live process nor
    infers that a capture passed any rendering or gameplay check.
    """
    def require(value, message):
        if not value:
            raise runner.RunnerError("isolation_error", "Private process identity: " + message)

    directory = _plain(directory)
    require(isinstance(proof, dict), "final receipt is not an object")
    evidence = proof.get("evidence")
    require(isinstance(evidence, dict), "missing helper evidence map")
    for name in ("process-start.json", "process-identity.json", "stdout.log", "stderr.log"):
        path = _plain(directory / name)
        entry = evidence.get(name)
        require(path.is_file() and isinstance(entry, dict), "missing bound " + name)
        require(entry.get("sha256") == _sha(path)
                and type(entry.get("bytes")) is int and entry["bytes"] == path.stat().st_size,
                "changed " + name)
    start = _read(directory / "process-start.json")
    identity = _read(directory / "process-identity.json")
    require(isinstance(start, dict) and isinstance(identity, dict), "invalid start/identity object")
    require(proof.get("runner") == start.get("runner") == identity.get("runner") == private.VERSION
            and proof.get("schema_version") == start.get("schema_version") == 1,
            "helper revision differs")
    require(start.get("status") == "starting" and identity.get("status") == "suspended_in_job",
            "missing suspended-in-job observation")
    for key in ("argv", "cwd", "executable_sha256", "policy", "desktop_name", "started_at",
                "timeout_seconds", "environment_keys", "environment_sha256"):
        require(key in start and key in proof and start[key] == proof[key], "start differs: " + key)
    process = proof.get("process_identity")
    require(isinstance(process, dict) and all(type(process.get(key)) is int and process[key] > 0
            for key in ("pid", "primary_thread_id", "creation_filetime_100ns")),
            "missing positive PID/thread/creation identity")
    require(identity.get("process_identity") == process, "final identity differs from suspended process")
    desktop_path = proof.get("desktop_path")
    desktop_name = proof.get("desktop_name")
    require(isinstance(desktop_name, str) and bool(desktop_name)
            and isinstance(desktop_path, str) and desktop_path.endswith("\\" + desktop_name)
            and len(desktop_path) > len(desktop_name) + 1
            and identity.get("desktop_path") == desktop_path, "desktop identity differs")
    require(identity.get("start_sha256") == _sha(directory / "process-start.json"),
            "identity does not bind the start receipt")
    try:
        times = [datetime.fromisoformat(value) for value in
                 (proof.get("started_at"), identity.get("recorded_at"),
                  proof.get("resumed_at"), proof.get("finished_at"))]
        require(all(value.utcoffset() is not None for value in times)
                and times == sorted(times), "start/identity/resume/finish times are not aware and ordered")
    except (TypeError, ValueError) as error:
        raise runner.RunnerError("isolation_error", "Private process identity: missing valid resume/timing evidence") from error


class RuntimeBridge:
    def __init__(self, project_roots, expected_sources=None):
        if not project_roots:
            raise runner.RunnerError("invalid_path", "Explicit project roots are required")
        self.roots = tuple(_plain(path) for path in project_roots)
        if any(not path.is_dir() for path in self.roots):
            raise runner.RunnerError("invalid_path", "Project roots must already exist")
        self.expected = {_plain(path): dict(value) for path, value in (expected_sources or {}).items()}
        self.gui = _plain(ROOT / GUI_RELATIVE)
        self.console = self.gui.with_name(self.gui.stem + "_console.exe")
        self.ffmpeg = _plain(shutil.which("ffmpeg")) if shutil.which("ffmpeg") else None
        self.original = None
        self.receipts = []
        self._version_cache = None
        self._dispatch_lock = threading.RLock()

    def _project(self, path):
        path = _plain(path)
        if not any(path == root or path.is_relative_to(root) for root in self.roots):
            raise runner.RunnerError("project_outside_scope", "Project is outside explicit runtime roots")
        if not (path / "project.godot").is_file():
            raise runner.RunnerError("invalid_project", "Missing project.godot")
        return path

    def _code_hashes(self):
        values = {"bridge": _sha(Path(__file__)), "private_desktop": _sha(Path(private.__file__)),
                  "legacy_runner": _sha(Path(runner.__file__))}
        if values["private_desktop"] != PRIVATE_SHA256 or _sha(self.gui) != GUI_SHA256:
            raise runner.RunnerError("runtime_changed", "Pinned private runner or Godot GUI bytes changed")
        return values

    def _classify(self, args, cwd):
        if isinstance(args, (str, bytes)) or not args:
            raise runner.RunnerError("invalid_config", "Expected an argument sequence")
        args = [str(value) for value in args]
        if any("\0" in value for value in args) or not Path(args[0]).is_absolute():
            raise runner.RunnerError("invalid_config", "Absolute executable and NUL-free arguments required")
        executable = _plain(args[0])
        if self.ffmpeg and executable == self.ffmpeg:
            directory = _plain(cwd)
            expected = ["-hide_banner", "-loglevel", "error", "-framerate", "60", "-i",
                        str(directory / "frames/%06d.png"), "-c:v", "libx264", "-crf", "16",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(directory / "video.mp4")]
            supplied = args[1:]
            if supplied != expected and supplied != [*expected[:3], "-y", *expected[3:]]:
                raise runner.RunnerError("invalid_config", "Only the fixed PNG-to-MP4 FFmpeg command is allowed")
            return "cli", args, None
        if executable not in {self.gui, self.console}:
            raise runner.RunnerError("unsupported_executable", "Only pinned Godot4 and resolved FFmpeg are routed")
        # Check the binary even for safe CLI calls; never launch a substituted alias.
        self._code_hashes()
        if args[1:] == ["--version"]:
            return "cli", [str(self.gui), "--version"], None
        if len(args) == 6 and args[1] == "--path" and args[3:] == ["--editor", "--headless", "--import"]:
            project = self._project(args[2])
            if _plain(cwd) != project:
                raise runner.RunnerError("invalid_path", "Import cwd differs from project")
            return "cli", [str(self.gui), *args[1:]], project
        values, flags, scenes = {}, set(), []
        index = 1
        while index < len(args):
            item = args[index]
            if item in {"--path", "--fixed-fps", "--resolution", "--rendering-method"}:
                if item in values or index + 1 == len(args):
                    raise runner.RunnerError("invalid_config", "Duplicate or missing Godot option")
                values[item] = args[index + 1]
                index += 2
            elif item == "--windowed" and item not in flags:
                flags.add(item)
                index += 1
            elif item in SCENES:
                scenes.append(item)
                index += 1
            else:
                raise runner.RunnerError("invalid_config", f"Unsupported Godot GUI argument: {item}")
        if (set(values) != {"--path", "--fixed-fps", "--resolution", "--rendering-method"}
                or values.get("--fixed-fps") != "60" or values.get("--resolution") != "640x360"
                or values.get("--rendering-method") != "gl_compatibility"
                or flags != {"--windowed"} or len(scenes) != 1):
            raise runner.RunnerError("invalid_config", "Expected fixed native Slay capture command")
        project = self._project(values["--path"])
        if _plain(cwd) != project or not (project / scenes[0][6:]).is_file():
            raise runner.RunnerError("invalid_path", "Capture cwd or scene does not match project")
        return "private_desktop", [str(self.gui), *args[1:]], project

    def dispatch(self, args, *, cwd, env, log_path, cancel=None, timeout=120):
        with self._dispatch_lock:
            return self._dispatch(args, cwd=cwd, env=env, log_path=log_path, cancel=cancel, timeout=timeout)

    def _dispatch(self, args, *, cwd, env, log_path, cancel, timeout):
        runner._check_cancel(cancel)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise runner.RunnerError("invalid_config", "A finite positive process timeout is required")
        mode, argv, project = self._classify(args, cwd)
        if mode == "cli":
            if argv == [str(self.gui), "--version"]:
                return self._version(argv, cwd=cwd, env=env, log_path=log_path, cancel=cancel, timeout=timeout)
            return self.original(argv, cwd=cwd, env=env, log_path=log_path, cancel=cancel, timeout=timeout)
        log_path = _plain(log_path)
        runner._assert_outside(log_path.parent, project, "Capture logs")
        config_path = _plain(env.get("KIROCLOPS_CONFIG", ""))
        if not env.get("KIROCLOPS_CONFIG") or not config_path.is_file() or config_path.parent != log_path.parent:
            raise runner.RunnerError("invalid_config", "Capture config must be beside the output log")
        config = _read(config_path)
        if not isinstance(config, dict) or _plain(config.get("output_dir", "")) != log_path.parent:
            raise runner.RunnerError("invalid_config", "Capture config output does not match log directory")
        destination = log_path.parent / (log_path.stem + "." + VERSION)
        if log_path.exists() or destination.exists():
            raise runner.RunnerError("output_exists", "Existing runtime evidence is never overwritten")
        before = runner.source_hashes(project)
        if project in self.expected and before != self.expected[project]:
            raise runner.RunnerError("source_changed", "Project differs from supplied immutable source map")
        code_hashes = self._code_hashes()
        config_hash = _sha(config_path)
        destination.mkdir()
        receipt = {"schema_version": 1, "runtime": VERSION, "status": "starting",
                   "started_at": datetime.now(timezone.utc).isoformat(), "project": str(project),
                   "requested_argv": [str(value) for value in args], "executed_argv": argv,
                   "config": {"path": str(config_path), "sha256": config_hash},
                   "source_hashes_before": before, "code_hashes": code_hashes,
                   "engine_sha256": GUI_SHA256, "private_receipt": "process/process.json",
                   "log": str(log_path), "scope": "Process routing only; game/measurement acceptance remains independent."}
        _write(destination / "bridge-start.json", receipt)
        process = None
        caught = None
        code = None
        try:
            process = private.run_private_desktop(argv, cwd=project, env=env,
                output_dir=destination / "process", timeout=timeout, cancel=cancel)
            code = process.get("exit_code")
            if (process.get("argv") != argv or process.get("cwd") != str(project)
                    or process.get("executable_sha256") != GUI_SHA256
                    or process.get("policy") != PROCESS_POLICY
                    or _read(destination / "process/process.json") != process):
                raise runner.RunnerError("isolation_error", "Private process receipt does not bind the executed command")
            if process.get("status") == "cancelled":
                raise runner.RunnerError("cancelled", "Private-desktop capture was cancelled")
            if process.get("status") == "timeout":
                raise runner.RunnerError("timeout", "Private-desktop capture timed out")
            if (process.get("status") not in {"completed", "failed"} or process.get("cleanup_errors")
                    or process.get("owned_process_termination_confirmed") is not True
                    or type(code) is not int or (process["status"] == "completed") != (code == 0)):
                raise runner.RunnerError("isolation_error", "Private process completion/cleanup proof is invalid")
            _verify_process_identity(process, destination / "process")
            samples = process.get("desktop_samples", [])
            if (not samples or any(s.get("name") != process.get("desktop_name")
                    or s.get("receives_input") is not False for s in samples)
                    or code == 0 and (process.get("saw_owned_window") is not True
                        or not any((s.get("owned_window_count") or 0) > 0 for s in samples))):
                raise runner.RunnerError("isolation_error", "Missing private non-input desktop/owned-window proof")
            receipt["status"] = "completed" if code == 0 else "engine_failed"
        except BaseException as error:
            caught = error
            receipt["status"] = "error"
            receipt["error"] = {"type": type(error).__name__, "code": getattr(error, "code", "isolation_error"), "message": str(error)}
        finally:
            try:
                # The private helper closes the dedicated handles before returning.
                with log_path.open("xb") as stream:
                    for name in ("stdout.log", "stderr.log"):
                        path = destination / "process" / name
                        if path.is_file():
                            stream.write(path.read_bytes())
                            stream.write(b"\n")
                after = runner.source_hashes(project)
                receipt["source_hashes_after"] = after
                if after != before or _sha(config_path) != config_hash or self._code_hashes() != code_hashes:
                    raise runner.RunnerError("source_changed", "Capture source/config/runtime changed during execution")
                receipt["evidence_hashes"] = {path.relative_to(destination).as_posix(): _sha(path)
                    for path in sorted(destination.rglob("*")) if path.is_file()}
                receipt["log_sha256"] = _sha(log_path)
            except BaseException as error:
                receipt["status"] = "error"
                receipt["verification_error"] = {"type": type(error).__name__, "code": getattr(error, "code", "isolation_error"), "message": str(error)}
                if caught is None:
                    caught = error
            receipt.update(exit_code=code, finished_at=datetime.now(timezone.utc).isoformat())
            _write(destination / "bridge.json", receipt)
            self.receipts.append(destination / "bridge.json")
        if isinstance(caught, (KeyboardInterrupt, SystemExit)):
            raise caught
        if caught is not None:
            if isinstance(caught, runner.RunnerError):
                caught.details["runtime_receipt"] = str(destination / "bridge.json")
                raise caught
            raise runner.RunnerError("isolation_error", str(caught), {"runtime_receipt": str(destination / "bridge.json")}) from caught
        return code

    def _version(self, argv, *, cwd, env, log_path, cancel, timeout):
        """Reuse one successful exact CLI observation, with immutable provenance."""
        log_path = _plain(log_path)
        receipt_path = log_path.with_name(log_path.name + "." + VERSION + ".json")
        if log_path.exists() or receipt_path.exists():
            raise runner.RunnerError("output_exists", "Version evidence already exists")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        key = (GUI_SHA256, tuple(argv), ENGINE_VERSION)
        cached = self._version_cache
        if cached is not None:
            if (cached["key"] != key or _sha(cached["log"]) != cached["sha256"]
                    or _sha(cached["receipt"]) != cached["receipt_sha256"]):
                raise runner.RunnerError("runtime_changed", "Original version observation changed")
            runner._check_cancel(cancel)
            with log_path.open("xb") as stream:
                stream.write(cached["bytes"])
            _write(receipt_path, {"schema_version": 1, "runtime": VERSION, "kind": "version_cache_reuse",
                "status": "completed", "exit_code": 0, "engine_sha256": GUI_SHA256,
                "argv": argv, "version": ENGINE_VERSION, "log_sha256": _sha(log_path),
                "observed_origin": {"log": str(cached["log"]), "log_sha256": cached["sha256"],
                    "receipt": str(cached["receipt"]), "receipt_sha256": cached["receipt_sha256"]}})
            self.receipts.append(receipt_path)
            return 0
        code = self.original(argv, cwd=cwd, env=env, log_path=log_path, cancel=cancel, timeout=timeout)
        runner._check_cancel(cancel)
        self._code_hashes()
        data = log_path.read_bytes()
        version = data.decode("utf-8", errors="replace").strip()
        _write(receipt_path, {"schema_version": 1, "runtime": VERSION, "kind": "version_observation",
            "status": "completed" if code == 0 else "engine_failed", "exit_code": code,
            "engine_sha256": GUI_SHA256, "argv": argv, "version": version,
            "log": str(log_path), "log_sha256": _sha(log_path)})
        self.receipts.append(receipt_path)
        if code == 0:
            if version != ENGINE_VERSION:
                raise runner.RunnerError("runtime_changed", "Pinned engine reported an unexpected version")
            self._version_cache = {"key": key, "log": log_path, "sha256": _sha(log_path),
                                   "receipt": receipt_path, "receipt_sha256": _sha(receipt_path), "bytes": data}
        return code


def verify_runtime_receipt(path, *, project=None, expected_source_hashes=None):
    """Read-only replay of one successful GUI routing receipt, not game acceptance."""
    try:
        path = _plain(path)
        if not path.is_file():
            return {"status": "unverified", "reason": "runtime receipt missing"}
        receipt = _read(path)
        errors = []
        def need(value, message):
            if not value:
                errors.append(message)
        directory = path.parent
        actual_project = _plain(receipt["project"])
        if project is not None:
            if actual_project != _plain(project):
                raise ValueError("project identity differs")
        source = receipt["source_hashes_before"]
        need(receipt.get("runtime") == VERSION and receipt.get("status") == "completed"
             and receipt.get("exit_code") == 0, "runtime did not complete successfully")
        need(source == receipt.get("source_hashes_after") == runner.source_hashes(actual_project), "source bytes differ")
        if expected_source_hashes is not None:
            need(source == expected_source_hashes, "selected immutable source map differs")
        hashes = receipt["code_hashes"]
        need(hashes == {"bridge": _sha(Path(__file__)), "private_desktop": _sha(Path(private.__file__)),
                        "legacy_runner": _sha(Path(runner.__file__))}, "runtime implementation bytes differ")
        need(hashes.get("private_desktop") == PRIVATE_SHA256, "unqualified private helper revision")
        need(receipt.get("engine_sha256") == GUI_SHA256 == _sha(ROOT / GUI_RELATIVE), "engine bytes differ")
        for relative, expected in receipt["evidence_hashes"].items():
            evidence = _plain(directory / relative)
            need(evidence.is_relative_to(directory) and evidence != path, "external/self evidence path")
            if evidence.is_relative_to(directory) and evidence != path:
                need(_sha(evidence) == expected, "evidence changed: " + relative)
        need({"bridge-start.json", "process/process.json", "process/process-start.json",
              "process/process-identity.json", "process/stdout.log", "process/stderr.log"}
             <= set(receipt["evidence_hashes"]), "missing bound runtime evidence")
        start = _read(directory / "bridge-start.json")
        for key in ("runtime", "project", "requested_argv", "executed_argv", "config", "source_hashes_before", "code_hashes", "engine_sha256", "log", "started_at"):
            need(start.get(key) == receipt.get(key), "bridge start differs: " + key)
        config = _plain(receipt["config"]["path"])
        log = _plain(receipt["log"])
        if config.parent != directory.parent or directory.parent != log.parent:
            raise ValueError("config/log output scope differs")
        need(_sha(config) == receipt["config"]["sha256"] and _plain(_read(config)["output_dir"]) == log.parent, "capture config differs")
        need(_sha(log) == receipt["log_sha256"], "merged engine log differs")
        proof_path = _plain(directory / receipt["private_receipt"])
        if proof_path != directory / "process/process.json":
            raise ValueError("private receipt path differs")
        proof = _read(proof_path)
        _verify_process_identity(proof, proof_path.parent)
        router = RuntimeBridge([actual_project])
        route, executed, bound_project = router._classify(receipt["requested_argv"], actual_project)
        need(route == "private_desktop" and executed == receipt["executed_argv"]
             and bound_project == actual_project, "recorded request is not an allowed GUI capture")
        need(proof.get("argv") == receipt["executed_argv"] and proof.get("cwd") == str(actual_project)
             and proof.get("executable_sha256") == GUI_SHA256, "private process arguments differ")
        need(proof.get("status") == "completed" and proof.get("exit_code") == 0
             and proof.get("owned_process_termination_confirmed") is True and proof.get("cleanup_errors") == []
             and proof.get("policy") == PROCESS_POLICY, "private process/cleanup policy failed")
        samples = proof.get("desktop_samples", [])
        need(bool(samples) and all(s.get("name") == proof.get("desktop_name") and s.get("receives_input") is False for s in samples), "non-input desktop proof failed")
        need(proof.get("saw_owned_window") is True and any((s.get("owned_window_count") or 0) > 0 for s in samples), "private owned-window proof missing")
        return {"status": "pass" if not errors else "fail", "errors": errors,
                "receipt_sha256": _sha(path), "project": str(actual_project),
                "scope": "Recorded process isolation and source binding; not trajectory/gameplay acceptance."}
    except (OSError, ValueError, KeyError, TypeError, runner.RunnerError) as error:
        return {"status": "fail", "errors": [str(error)]}


@contextmanager
def isolated_runtime(*, project_roots, expected_sources=None):
    """Route all shared-runner calls in this worker; restore on every exit.

    Overlapping/nested contexts are rejected. Existing Godot locking still owns
    serialization; this context must wrap an operation, not run inside its call.
    """
    global _ACTIVE
    bridge = RuntimeBridge(project_roots, expected_sources)
    with _CONTEXT_LOCK:
        if _ACTIVE is not None:
            raise runner.RunnerError("runtime_context_active", "An isolated runtime context is already active")
        bridge.original = runner._run_process
        runner._run_process = bridge.dispatch
        _ACTIVE = bridge
    try:
        yield bridge
    finally:
        with bridge._dispatch_lock, _CONTEXT_LOCK:
            runner._run_process = bridge.original
            _ACTIVE = None


def main(argv=None):
    """Explicit worker bootstrap; does not install hooks in the parent process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--job", required=True)
    parser.add_argument("--project-root", action="append", type=Path)
    args = parser.parse_args(argv)
    from .worker import run_job
    with isolated_runtime(project_roots=args.project_root or [args.session]):
        return run_job(args.session, args.job)


if __name__ == "__main__":
    raise SystemExit(main())
