"""Durable local analysis jobs; no game, project, or cloud dependencies.

The request is a local transport envelope.  Only the portable video manifest,
its listed images, the selection and the behavior contract cross into a worker.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator, Protocol
import uuid

from kiroclops.visual.schema import VisualError


TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
JOB_SCHEMA = 1


class AnalysisBackend(Protocol):
    def submit(self, request: dict, idempotency_key: str | None = None) -> dict: ...
    def get_job(self, job_id: str, wait_seconds: float = 0) -> dict: ...
    def cancel_job(self, job_id: str) -> dict: ...


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read(path: Path) -> dict:
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.025)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _lock(path: Path, timeout: float = 10.0) -> Iterator[Any]:
    """An OS-released lease, including after a killed worker or disconnected CLI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Lease unavailable: {path.name}")
                time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
        yield handle
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _identity(pid: int) -> str | None:
    """PID plus kernel creation time avoids killing a later, unrelated PID reuse."""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                return None
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        # Linux start ticks are stable for the lifetime of a process.
        record = Path(f"/proc/{pid}/stat").read_text()
        fields = record[record.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
        return None


def _alive(worker: dict | None) -> bool:
    return bool(worker and worker.get("identity") and _identity(int(worker["pid"])) == worker["identity"])


def _terminate_owned(worker: dict | None) -> bool:
    if not _alive(worker):
        return False
    pid = int(worker["pid"])
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000 | 0x0001, False, pid)
        if not handle:
            return False
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                return False
            actual = str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
            if actual != worker["identity"]:
                return False
            return bool(kernel.TerminateProcess(handle, 130))
        finally:
            kernel.CloseHandle(handle)
    try:
        # Workers are session leaders; only their own descendants share this group.
        os.killpg(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


def _update(job_dir: Path, **changes: Any) -> dict:
    with _lock(job_dir / "state.lock"):
        state = _read(job_dir / "state.json")
        state.update(changes)
        state["updated_at"] = time.time()
        _atomic(job_dir / "state.json", state)
        return state


def _validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise VisualError("invalid_job_id", "Job ID must be a server-issued identifier.")
    return job_id


class LocalAnalysisBackend:
    """One durable store, bounded analysis concurrency, and independently owned workers."""

    def __init__(self, root: Path, *, max_workers: int = 1, timeout_seconds: float = 1800):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 8:
            raise VisualError("invalid_configuration", "max_workers must be an integer from 1 to 8.")
        if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 1800:
            raise VisualError("invalid_configuration", "Analysis timeout must be greater than zero and at most 1800 seconds.")
        self.root = Path(root).expanduser().resolve()
        self.timeout_seconds = float(timeout_seconds)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(exist_ok=True)
        with _lock(self.root / "store.lock"):
            config_path = self.root / "config.json"
            if config_path.exists():
                config = _read(config_path)
                if config.get("schema_version") != JOB_SCHEMA or config.get("max_workers") != max_workers:
                    raise VisualError("configuration_mismatch", "Existing store uses a different schema or concurrency setting.")
            else:
                _atomic(config_path, {"schema_version": JOB_SCHEMA, "max_workers": max_workers})
        self.max_workers = max_workers

    def _job_dir(self, job_id: str) -> Path:
        path = self.root / "jobs" / _validate_job_id(job_id)
        if not (path / "state.json").is_file():
            raise VisualError("job_not_found", f"Unknown analysis job: {job_id}")
        return path

    def _spawn(self, job_dir: Path) -> None:
        with _lock(job_dir / "state.lock"):
            state = _read(job_dir / "state.json")
            if state["status"] != "queued" or _alive(state.get("worker")):
                return
            if state.get("worker"):
                state.update(status="interrupted", finished_at=time.time(), error={"code": "worker_interrupted", "message": "Queued worker exited before completing the job."})
                _atomic(job_dir / "state.json", state)
                return
            environment = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[2])
            environment["PYTHONPATH"] = source_root + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
            environment["PYTHONIOENCODING"] = "utf-8"
            command = [sys.executable, "-X", "utf8", "-m", "kiroclops.local_engine.worker", "--root", str(self.root), "--job-id", job_dir.name]
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            try:
                with (job_dir / "worker.log").open("ab") as log:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.root), env=environment, **kwargs)
                identity = _identity(process.pid)
                if identity is None:
                    # Keep the child handle until the creation timestamp is observable.
                    until = time.monotonic() + 1.0
                    while identity is None and process.poll() is None and time.monotonic() < until:
                        time.sleep(0.01)
                        identity = _identity(process.pid)
                state["worker"] = {"pid": process.pid, "identity": identity}
                # On Windows the venv executable can launch another Python
                # interpreter. Both processes inherit the redirected log.
                state["launcher"] = dict(state["worker"])
                state["updated_at"] = time.time()
                _atomic(job_dir / "state.json", state)
                # Reap the launch process without tying the worker lifetime to this client.
                threading.Thread(target=process.wait, name=f"analysis-reaper-{job_dir.name[:8]}", daemon=True).start()
            except OSError as exc:
                state.update(status="failed", finished_at=time.time(), error={"code": "worker_start_failed", "message": str(exc)})
                _atomic(job_dir / "state.json", state)

    def _reconcile(self, job_dir: Path) -> dict:
        state = _read(job_dir / "state.json")
        if state["status"] in TERMINAL:
            return state
        if not state.get("worker") and state["status"] == "queued":
            self._spawn(job_dir)
            return _read(job_dir / "state.json")
        if not _alive(state.get("worker")):
            with _lock(job_dir / "state.lock"):
                state = _read(job_dir / "state.json")
                if state["status"] not in TERMINAL and not _alive(state.get("worker")):
                    cancelled = (job_dir / "cancel.json").exists()
                    state.update(status="cancelled" if cancelled else "interrupted", finished_at=time.time(), error={"code": "job_cancelled" if cancelled else "worker_interrupted", "message": "Job was cancelled." if cancelled else "Worker exited without committing a complete result."})
                    _atomic(job_dir / "state.json", state)
        return state

    def submit(self, request: dict, idempotency_key: str | None = None) -> dict:
        from kiroclops.visual.package import frame_paths, load_package

        if not isinstance(request, dict) or request.get("operation", "analyze") != "analyze":
            raise VisualError("unsupported_operation", "This backend supports the analyze operation.")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256):
            raise VisualError("invalid_idempotency_key", "Idempotency key must contain 1 to 256 characters.")
        raw_path = request.get("package_path", request.get("package"))
        if not isinstance(raw_path, (str, Path)):
            raise VisualError("invalid_request", "A local package_path is required.")
        package_dir = Path(raw_path).expanduser().resolve()
        if package_dir == self.root or package_dir in self.root.parents:
            raise VisualError("invalid_request", "The job store cannot be inside its input package.")
        manifest = load_package(package_dir, verify=True)
        package_sha = manifest["package_sha256"]
        if request.get("package_sha256") not in (None, package_sha):
            raise VisualError("source_changed", "Request package hash does not match the supplied package.")
        selection, contract = request.get("selection", {}), request.get("contract")
        if not isinstance(selection, dict) or (contract is not None and not isinstance(contract, dict)):
            raise VisualError("invalid_request", "selection and contract must be JSON objects.")
        semantic = {"operation": "analyze", "package_sha256": package_sha, "selection": selection, "contract": contract, "candidate_sha256": request.get("candidate_sha256")}
        try:
            semantic = json.loads(_canonical(semantic))
        except (ValueError, TypeError) as exc:
            raise VisualError("invalid_request", f"Request must contain finite JSON data: {exc}") from exc
        request_sha = _digest(semantic)
        with _lock(self.root / "store.lock", timeout=60):
            key_path = None
            if idempotency_key is not None:
                key_path = self.root / "idempotency" / f"{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()}.json"
                if key_path.exists():
                    previous = _read(key_path)
                    if previous["request_sha256"] != request_sha:
                        raise VisualError("idempotency_conflict", "This idempotency key belongs to a different analysis request.")
                    state = self._reconcile(self._job_dir(previous["job_id"]))
                    return {"job_id": state["job_id"], "status": state["status"]}
            job_id = uuid.uuid4().hex
            job_dir = self.root / "jobs" / job_id
            staging = Path(tempfile.mkdtemp(prefix=".incoming-", dir=self.root / "jobs"))
            try:
                snapshot = staging / "input" / "package"
                snapshot.mkdir(parents=True)
                # Deliberately exclude private or unrelated files beside the listed images.
                for source in [package_dir / "manifest.json", *frame_paths(package_dir, manifest)]:
                    source = Path(source)
                    relative = source.relative_to(package_dir)
                    resolved = source.resolve(strict=True)
                    if not resolved.is_relative_to(package_dir):
                        raise VisualError("unsafe_package_path", "Package image escapes its package directory.")
                    if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if parent != package_dir and parent.is_relative_to(package_dir)):
                        raise VisualError("unsafe_package_path", "Symbolic links cannot be copied into a portable package.")
                    target = snapshot / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                if load_package(package_dir, verify='hashes')["package_sha256"] != package_sha or load_package(snapshot, verify='hashes')["package_sha256"] != package_sha:
                    raise VisualError("source_changed", "Input package changed while its immutable snapshot was created.")
                _atomic(staging / "request.json", semantic)
                now = time.time()
                state = {"schema_version": JOB_SCHEMA, "kind": "AnalysisJob", "job_id": job_id, "status": "queued", "created_at": now, "updated_at": now, "started_at": None, "finished_at": None, "timeout_seconds": self.timeout_seconds, "request_sha256": request_sha, "package_sha256": package_sha, "candidate_sha256": semantic["candidate_sha256"], "worker": None, "progress": {"phase": "queued", "completed": 0, "total": 1}, "error": None, "result": None}
                _atomic(staging / "state.json", state)
                os.replace(staging, job_dir)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            if key_path is not None:
                _atomic(key_path, {"job_id": job_id, "request_sha256": request_sha})
            self._spawn(job_dir)
            return {"job_id": job_id, "status": _read(job_dir / "state.json")["status"]}

    def get_job(self, job_id: str, wait_seconds: float = 0) -> dict:
        if not isinstance(wait_seconds, (int, float)) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 60:
            raise VisualError("invalid_request", "wait_seconds must be between zero and 60.")
        job_dir = self._job_dir(job_id)
        deadline = time.monotonic() + wait_seconds
        while True:
            state = self._reconcile(job_dir)
            if state["status"] in TERMINAL or time.monotonic() >= deadline:
                break
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        answer = dict(state)
        answer["job_directory"] = str(job_dir)
        if state["status"] in TERMINAL:
            # A committed result precedes interpreter shutdown. Wait for our
            # exact owned identities, so a caller may move/remove the job
            # immediately without a Windows log sharing violation. No PID-only
            # kill or global process lookup is used, including on reconnect.
            until = time.monotonic() + 2.0
            owners = [state.get("worker"), state.get("launcher")]
            while any(_alive(owner) for owner in owners) and time.monotonic() < until:
                time.sleep(.01)
            answer["worker_exited"] = not any(_alive(owner) for owner in owners)
        if state["status"] == "succeeded":
            result_path = job_dir / "result.json"
            try:
                result = _read(result_path)
                valid_result = _digest(result) == state.get("result_sha256")
                if state.get('delivery_schema_version') == 2:
                    from kiroclops.cloud.results import legacy_result
                    delivery = _read(job_dir / 'delivery-v2.json')
                    request = _read(job_dir / 'request.json')
                    valid_result = (valid_result and _digest(delivery) == state.get('delivery_sha256')
                                    and _digest(request) == state['request_sha256']
                                    and legacy_result(delivery, request, logical_job_id=job_id) == result)
            except (OSError, ValueError, TypeError):
                result, valid_result = None, False
            if not valid_result:
                error = {"code": "result_integrity_failure", "message": "Committed result is missing or changed."}
                _update(job_dir, status="failed", result=None, error=error)
                answer.update(status="failed", result=None, error=error)
            else:
                answer["result"] = result
                answer["result_path"] = str(result_path)
        return answer

    def cancel_job(self, job_id: str) -> dict:
        job_dir = self._job_dir(job_id)
        state = self._reconcile(job_dir)
        if state["status"] in TERMINAL:
            return self.get_job(job_id)
        _atomic(job_dir / "cancel.json", {"requested_at": time.time()})
        until = time.monotonic() + 2.0
        while _alive(state.get("worker")) and time.monotonic() < until:
            if _read(job_dir / "state.json")["status"] in TERMINAL:
                return self.get_job(job_id)
            time.sleep(0.025)
        # Windows venv launchers can hand off to a distinct interpreter PID.
        state = _read(job_dir / "state.json")
        if _alive(state.get("worker")):
            _terminate_owned(state["worker"])
        # Never replace a result already committed before the cancellation request.
        with _lock(job_dir / "state.lock"):
            current = _read(job_dir / "state.json")
            if current["status"] not in TERMINAL:
                current.update(status="cancelled", finished_at=time.time(), error={"code": "job_cancelled", "message": "Job was cancelled; partial outputs are not valid results."}, result=None)
                _atomic(job_dir / "state.json", current)
        return self.get_job(job_id)
