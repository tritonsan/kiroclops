"""Standalone Windows 10+ private-desktop launcher; not a cohort integration.

Run a trusted, direct GUI executable without activating its desktop. This is
window-surface isolation, NOT a security sandbox or proof of renderer fidelity.
The executable must not switch desktops itself. Child creation is blocked by a
one-process job limit because arbitrary child launchers can omit lpDesktop.

No interactive desktop/window is opened, enumerated, switched or focused here.
Only the newly owned desktop's name, UOI_IO and owned-PID window count are queried. Rendering,
GPU availability and capture timing still require separate qualification.

Primary contracts:
https://learn.microsoft.com/en-us/windows/win32/winstation/thread-connection-to-a-desktop
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import time
from typing import Mapping, Sequence
import uuid


VERSION = "private-desktop-v1"
CREATE_FLAGS = 0x00000004 | 0x00000400 | 0x00080000 | 0x08000000
# SUSPENDED | UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | NO_WINDOW
JOB_LIMITS = 0x2000 | 0x0008  # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS (one)
HANDLE_LIST = 0x00020002
JOB_LIST = 0x0002000D
DESKTOP_ACCESS = 0x0001 | 0x0002 | 0x0004 | 0x0040 | 0x0080
# READOBJECTS | CREATEWINDOW | CREATEMENU | ENUMERATE | WRITEOBJECTS. No SWITCHDESKTOP.


class PrivateDesktopError(RuntimeError):
    def __init__(self, code: str, message: str, *, receipt_path: Path | None = None):
        super().__init__(message)
        self.code = code
        self.receipt_path = receipt_path


class _Security(c.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]


class _Startup(c.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR), ("x", w.DWORD), ("y", w.DWORD),
                ("xsize", w.DWORD), ("ysize", w.DWORD), ("xchars", w.DWORD),
                ("ychars", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved2", w.LPBYTE),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class _StartupEx(c.Structure):
    _fields_ = [("startup", _Startup), ("attributes", w.LPVOID)]


class _ProcessInfo(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE),
                ("pid", w.DWORD), ("tid", w.DWORD)]


class _BasicLimits(c.Structure):
    _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                ("flags", w.DWORD), ("min_working", c.c_size_t),
                ("max_working", c.c_size_t), ("active_limit", w.DWORD),
                ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]


class _Counters(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _Limits(c.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _Counters),
                ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]


@dataclass
class _Child:
    process: int
    thread: int
    pid: int
    tid: int


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exclusive_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def environment_block(env: Mapping[str, str]) -> str:
    """Validated explicit Unicode block; no implicit parent environment merge."""
    if not isinstance(env, Mapping) or not env:
        raise ValueError("An explicit nonempty environment mapping is required.")
    normalized = set()
    entries = []
    for key, value in env.items():
        if (not isinstance(key, str) or not isinstance(value, str) or not key
                or "=" in key or "\0" in key or "\0" in value):
            raise ValueError("Environment names/values must be strings without NUL; names cannot contain '='.")
        if key.upper() in normalized:
            raise ValueError("Environment names must be unique ignoring case.")
        normalized.add(key.upper())
        entries.append((key, value))
    if "SYSTEMROOT" not in normalized:
        raise ValueError("Explicit environment must include SystemRoot for Windows side-by-side loading.")
    return "\0".join(f"{key}={value}" for key, value in sorted(entries, key=lambda item: item[0].upper())) + "\0\0"


def _plain_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Linked or reparse paths are not supported: {part}")
    return path


class _WindowsAPI:
    """Small injectable Win32 boundary. Construction never starts a process."""
    def __init__(self, *, kernel=None, user=None):
        if kernel is None or user is None:
            if os.name != "nt":
                raise PrivateDesktopError("windows_required", "Windows 10 or newer is required.")
            kernel = c.WinDLL("kernel32", use_last_error=True)
            user = c.WinDLL("user32", use_last_error=True)
        self.k, self.u = kernel, user
        self.enum_callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        declarations = [
            (user, "CreateDesktopW", [w.LPCWSTR, w.LPCWSTR, w.LPVOID, w.DWORD, w.DWORD, w.LPVOID], w.HANDLE),
            (user, "CloseDesktop", [w.HANDLE], w.BOOL),
            (user, "GetProcessWindowStation", [], w.HANDLE),
            (user, "GetUserObjectInformationW", [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
            (user, "EnumDesktopWindows", [w.HANDLE, self.enum_callback_type, w.LPARAM], w.BOOL),
            (user, "GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            (kernel, "CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE),
            (kernel, "SetInformationJobObject", [w.HANDLE, c.c_int, w.LPVOID, w.DWORD], w.BOOL),
            (kernel, "IsProcessInJob", [w.HANDLE, w.HANDLE, c.POINTER(w.BOOL)], w.BOOL),
            (kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            (kernel, "InitializeProcThreadAttributeList", [w.LPVOID, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)], w.BOOL),
            (kernel, "UpdateProcThreadAttribute", [w.LPVOID, w.DWORD, c.c_size_t, w.LPVOID, c.c_size_t, w.LPVOID, w.LPVOID], w.BOOL),
            (kernel, "DeleteProcThreadAttributeList", [w.LPVOID], None),
            (kernel, "CreateProcessW", [w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID, w.BOOL, w.DWORD,
                                       w.LPVOID, w.LPCWSTR, c.POINTER(_Startup), c.POINTER(_ProcessInfo)], w.BOOL),
            (kernel, "GetProcessTimes", [w.HANDLE, c.POINTER(w.FILETIME), c.POINTER(w.FILETIME),
                                         c.POINTER(w.FILETIME), c.POINTER(w.FILETIME)], w.BOOL),
            (kernel, "ResumeThread", [w.HANDLE], w.DWORD),
            (kernel, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            (kernel, "GetExitCodeProcess", [w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
            (kernel, "TerminateProcess", [w.HANDLE, w.UINT], w.BOOL),
            (kernel, "TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
            (kernel, "CloseHandle", [w.HANDLE], w.BOOL),
        ]
        for dll, name, arguments, result in declarations:
            function = getattr(dll, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def _require(value, operation):
        if not value:
            number = c.get_last_error()
            raise PrivateDesktopError("win32_error", f"{operation} failed (Win32 {number}).")
        return value

    def _name(self, handle) -> str:
        needed = w.DWORD()
        self.u.GetUserObjectInformationW(handle, 2, None, 0, c.byref(needed))
        if not 2 <= needed.value <= 65536:
            raise PrivateDesktopError("object_name_failed", "Invalid window-station/desktop name length.")
        value = c.create_unicode_buffer((needed.value + c.sizeof(c.c_wchar) - 1) // c.sizeof(c.c_wchar))
        self._require(self.u.GetUserObjectInformationW(handle, 2, value, c.sizeof(value), c.byref(needed)), "GetUserObjectInformationW(UOI_NAME)")
        return value.value

    def station_name(self) -> str:
        handle = self._require(self.u.GetProcessWindowStation(), "GetProcessWindowStation")
        name = self._name(handle)  # borrowed handle: never close it
        if not name or "\\" in name:
            raise PrivateDesktopError("station_name_failed", "Unexpected current window-station name.")
        return name

    def create_desktop(self, name: str):
        return self._require(self.u.CreateDesktopW(name, None, None, 0, DESKTOP_ACCESS, None), "CreateDesktopW")

    def desktop_state(self, handle) -> dict:
        receiving = w.BOOL()
        self._require(self.u.GetUserObjectInformationW(handle, 6, c.byref(receiving), c.sizeof(receiving), None), "GetUserObjectInformationW(UOI_IO)")
        return {"name": self._name(handle), "receives_input": bool(receiving.value)}

    def owned_window_count(self, desktop, pid: int) -> int:
        # This handle was returned by our CreateDesktopW. Never pass NULL (which
        # would enumerate the calling thread's desktop) or another desktop.
        if not desktop or pid <= 0:
            raise PrivateDesktopError("invalid_owned_desktop", "An owned desktop and direct child PID are required.")
        count = 0
        callbacks = 0
        errors = []
        @self.enum_callback_type
        def visit(window, ignored):
            nonlocal count, callbacks
            callbacks += 1
            owner = w.DWORD()
            c.set_last_error(0)
            if not self.u.GetWindowThreadProcessId(window, c.byref(owner)):
                error = c.get_last_error()
                # A window may close between enumeration and this identity read.
                # A destroyed HWND proves nothing; skip only that documented
                # race, never count it as owned or suppress unrelated errors.
                if error == 1400:  # ERROR_INVALID_WINDOW_HANDLE
                    return True
                errors.append(error)
                return False
            count += int(owner.value == pid)
            return True
        # UOI_NAME's size query can leave ERROR_INSUFFICIENT_BUFFER behind.
        # Modern Windows can return zero for a valid empty desktop without
        # setting a new error. That is zero observed windows, never attachment
        # proof. All callback failures and nonzero OS errors remain failures.
        c.set_last_error(0)
        result = self.u.EnumDesktopWindows(desktop, visit, 0)
        enum_error = c.get_last_error()
        if errors:
            raise PrivateDesktopError("window_owner_failed", f"Owned-desktop window identity query failed (Win32 {errors[0]}).")
        if not result and callbacks == 0 and enum_error == 0:
            return 0
        self._require(result, "EnumDesktopWindows(owned desktop)")
        return count

    def create_job(self):
        return self._require(self.k.CreateJobObjectW(None, None), "CreateJobObjectW")

    def configure_job(self, job):
        limits = _Limits()
        limits.basic.flags, limits.basic.active_limit = JOB_LIMITS, 1
        self._require(self.k.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)), "SetInformationJobObject")

    def open_stdio(self, path: Path | None):
        security = _Security(c.sizeof(_Security), None, True)
        # Dedicated inheritable NUL input and CREATE_NEW output files. Never a console handle.
        handle = self.k.CreateFileW(str(path) if path else "NUL", 0x40000000 if path else 0x80000000,
                                   1 if path else 3, c.byref(security), 1 if path else 3, 0x80, None)
        if handle == c.c_void_p(-1).value or not handle:
            self._require(False, "CreateFileW(stdio)")
        return handle

    def create_process(self, argv, *, cwd, env_block, desktop, stdio, job) -> _Child:
        size = c.c_size_t()
        self.k.InitializeProcThreadAttributeList(None, 2, 0, c.byref(size))
        if not 0 < size.value <= 1024 * 1024:
            raise PrivateDesktopError("attribute_list_failed", "Invalid process attribute-list size.")
        attributes = c.create_string_buffer(size.value)
        self._require(self.k.InitializeProcThreadAttributeList(attributes, 2, 0, c.byref(size)), "InitializeProcThreadAttributeList")
        try:
            handles = (w.HANDLE * 3)(*stdio)
            jobs = (w.HANDLE * 1)(job)
            self._require(self.k.UpdateProcThreadAttribute(attributes, 0, HANDLE_LIST, handles, c.sizeof(handles), None, None), "UpdateProcThreadAttribute(HANDLE_LIST)")
            # Atomic assignment during creation eliminates the parent-death gap of
            # CreateProcess -> AssignProcessToJobObject. Unsupported hosts fail closed.
            self._require(self.k.UpdateProcThreadAttribute(attributes, 0, JOB_LIST, jobs, c.sizeof(jobs), None, None), "UpdateProcThreadAttribute(JOB_LIST)")
            startup = _StartupEx()
            startup.startup.cb = c.sizeof(_StartupEx)
            startup.startup.desktop = desktop
            startup.startup.flags = 0x100 | 0x80  # USESTDHANDLES | FORCEOFFFEEDBACK
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = stdio
            startup.attributes = c.cast(attributes, w.LPVOID)
            # Explicit UTF-16 storage also handles astral characters on Windows,
            # where one Python character can occupy two wchar_t code units.
            command = c.create_string_buffer((subprocess.list2cmdline(argv) + "\0").encode("utf-16-le"))
            environment = c.create_string_buffer(env_block.encode("utf-16-le"))
            info = _ProcessInfo()
            self._require(self.k.CreateProcessW(argv[0], c.cast(command, w.LPWSTR), None, None, True, CREATE_FLAGS,
                environment, str(cwd), c.cast(c.byref(startup), c.POINTER(_Startup)), c.byref(info)), "CreateProcessW")
            return _Child(info.process, info.thread, info.pid, info.tid)
        finally:
            self.k.DeleteProcThreadAttributeList(attributes)

    def verify_job(self, process, job):
        member = w.BOOL()
        self._require(self.k.IsProcessInJob(process, job, c.byref(member)), "IsProcessInJob")
        if not member.value:
            raise PrivateDesktopError("job_not_assigned", "Child is not in the private job; refusing to resume.")

    def creation_time(self, process) -> int:
        created, ended, kernel, user = (w.FILETIME() for _ in range(4))
        self._require(self.k.GetProcessTimes(process, c.byref(created), c.byref(ended), c.byref(kernel), c.byref(user)), "GetProcessTimes")
        return (created.dwHighDateTime << 32) | created.dwLowDateTime

    def resume(self, thread):
        if self.k.ResumeThread(thread) != 1:
            raise PrivateDesktopError("resume_failed", "Primary thread was not suspended exactly once.")

    def wait(self, process, milliseconds) -> bool:
        result = self.k.WaitForSingleObject(process, milliseconds)
        if result not in (0, 258):
            self._require(False, "WaitForSingleObject")
        return result == 0

    def exit_code(self, process) -> int:
        code = w.DWORD()
        self._require(self.k.GetExitCodeProcess(process, c.byref(code)), "GetExitCodeProcess")
        return code.value

    def terminate_job(self, job):
        self._require(self.k.TerminateJobObject(job, 0xC000013A), "TerminateJobObject")

    def terminate_process(self, process):
        self._require(self.k.TerminateProcess(process, 0xC000013A), "TerminateProcess")

    def close_handle(self, handle):
        self._require(self.k.CloseHandle(handle), "CloseHandle")

    def close_desktop(self, handle):
        self._require(self.u.CloseDesktop(handle), "CloseDesktop")


def run_private_desktop(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
                        output_dir: Path, timeout: float, cancel=None) -> dict:
    """Run one direct executable on a new non-input desktop, with exclusive logs.

    Returns a final receipt for completed/nonzero/timeout/cancelled outcomes.
    Setup/API/cleanup failures raise PrivateDesktopError with a receipt_path.
    KeyboardInterrupt is re-raised after cleanup and an interrupted receipt.
    Explicit environment, absolute executable and a NEW output directory required.
    Runtime budget begins before OS setup and is polled at most every 200 ms.
    Cleanup has an additional bounded wait; this is reported separately.
    """
    if isinstance(argv, (str, bytes)) or not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ValueError("argv must be a nonempty sequence of NUL-free strings.")
    argv = list(argv)
    executable = Path(argv[0])
    if not executable.is_absolute() or executable.suffix.lower() != ".exe" or not executable.is_file():
        raise ValueError("argv[0] must be an existing absolute direct .exe path (no shell/PATH search).")
    executable = _plain_path(executable)
    argv[0] = str(executable)
    if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
        raise ValueError("Windows command line exceeds 32767 UTF-16 code units.")
    cwd, output_dir = _plain_path(Path(cwd)), _plain_path(Path(output_dir))
    if not cwd.is_dir() or not output_dir.parent.is_dir() or output_dir.exists():
        raise ValueError("cwd and output parent must exist; output directory must be new.")
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive.")
    block = environment_block(env)
    if cancel is not None and cancel.is_set():
        raise PrivateDesktopError("cancelled_before_start", "Cancellation requested before desktop creation.")
    api = _WindowsAPI()
    output_dir.mkdir()  # exclusive, after validation; never overwrite a prior receipt
    started = time.monotonic()
    desktop_name = "KiroClops_" + uuid.uuid4().hex
    record = {"schema_version": 1, "runner": VERSION, "status": "starting", "started_at": _utc(),
              "argv": argv, "cwd": str(cwd), "executable_sha256": _hash(executable),
              "timeout_seconds": timeout, "environment_keys": sorted(env, key=str.upper),
              "environment_sha256": hashlib.sha256(block.encode("utf-16-le")).hexdigest(),
              "desktop_name": desktop_name, "desktop_samples": [], "saw_owned_window": False,
              "process_identity": None, "exit_code": None,
              "policy": {"atomic_job_assignment": True, "kill_on_job_close": True,
                         "maximum_active_processes": 1, "inherited_handles": ["stdin:NUL", "stdout.log", "stderr.log"],
                         "desktop_handle_inherited": False, "job_handle_inherited": False,
                         "desktop_activation_requested": False},
              "limits": ["Not a security sandbox; trusted direct executable only.",
                         "No renderer/pixel/timing qualification is implied by process completion.",
                         "Input isolation samples concern only the owned desktop, not user windows.",
                         "Parent-death cleanup is the configured Windows job contract, not a tested event in this receipt."]}
    _exclusive_json(output_dir / "process-start.json", record)
    desktop = job = child = None
    handles = []
    caught = None
    cleanup_errors = []

    def sample():
        value = api.desktop_state(desktop)
        value["owned_window_count"] = api.owned_window_count(desktop, child.pid) if child is not None else None
        if value["owned_window_count"]:
            record["saw_owned_window"] = True
        value["elapsed_seconds"] = time.monotonic() - started
        record["desktop_samples"].append(value)
        if value.get("name") != desktop_name or value.get("receives_input") is not False:
            raise PrivateDesktopError("desktop_isolation_failed", "Owned desktop identity/input state failed validation.")

    def cleanup(operation, name):
        try:
            operation()
            return True
        except BaseException as error:
            cleanup_errors.append({"operation": name, "type": type(error).__name__, "message": str(error)})
            return False

    try:
        station = api.station_name()
        record["desktop_path"] = station + "\\" + desktop_name
        desktop = api.create_desktop(desktop_name)
        sample()
        job = api.create_job()
        api.configure_job(job)
        for path in (None, output_dir / "stdout.log", output_dir / "stderr.log"):
            handles.append(api.open_stdio(path))
        if cancel is not None and cancel.is_set():
            record["status"] = "cancelled"
        elif time.monotonic() - started >= timeout:
            record["status"] = "timeout"
        else:
            child = api.create_process(argv, cwd=cwd, env_block=block, desktop=record["desktop_path"], stdio=handles, job=job)
            api.verify_job(child.process, job)
            record["process_identity"] = {"pid": child.pid, "primary_thread_id": child.tid,
                "creation_filetime_100ns": api.creation_time(child.process)}
            sample()
            _exclusive_json(output_dir / "process-identity.json", {
                "runner": VERSION, "status": "suspended_in_job", "process_identity": record["process_identity"],
                "desktop_path": record["desktop_path"], "recorded_at": _utc(),
                "start_sha256": _hash(output_dir / "process-start.json")})
            if cancel is not None and cancel.is_set():
                record["status"] = "cancelled"
            elif time.monotonic() - started >= timeout:
                record["status"] = "timeout"
            else:
                api.resume(child.thread)
                record["resumed_at"] = _utc()
                record["status"] = "running"
                while True:
                    sample()
                    if cancel is not None and cancel.is_set():
                        record["status"] = "cancelled"
                        break
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        record["status"] = "timeout"
                        break
                    if api.wait(child.process, max(1, min(200, math.ceil(remaining * 1000)))):
                        sample()
                        record["exit_code"] = api.exit_code(child.process)
                        record["status"] = "completed" if record["exit_code"] == 0 else "failed"
                        break
    except BaseException as error:
        caught = error
        record["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "error"
        record["error"] = {"code": getattr(error, "code", "runner_error"),
                           "type": type(error).__name__, "message": str(error)}
    finally:
        record["execution_elapsed_seconds"] = time.monotonic() - started
        # Kill even if the direct parent already exited. Sole non-inherited job
        # handle also guarantees OS cleanup after abrupt launcher death.
        if job is not None:
            cleanup(lambda: api.terminate_job(job), "terminate_job")
            cleanup(lambda: api.close_handle(job), "close_job")
        if child is not None:
            ended = [False]
            def wait_owned():
                ended[0] = api.wait(child.process, 5000)
                if not ended[0]:
                    raise PrivateDesktopError("cleanup_timeout", "Owned process did not terminate within cleanup wait.")
            if not cleanup(wait_owned, "wait_owned_process"):
                cleanup(lambda: api.terminate_process(child.process), "terminate_owned_process_fallback")
                cleanup(wait_owned, "wait_owned_process_fallback")
            record["owned_process_termination_confirmed"] = ended[0]
            if ended[0] and record["exit_code"] is None:
                cleanup(lambda: record.update(exit_code=api.exit_code(child.process)), "read_exit_code")
            cleanup(lambda: api.close_handle(child.thread), "close_primary_thread")
            cleanup(lambda: api.close_handle(child.process), "close_owned_process")
        for handle in reversed(handles):
            cleanup(lambda handle=handle: api.close_handle(handle), "close_stdio")
        if desktop is not None:
            cleanup(lambda: api.close_desktop(desktop), "close_desktop")
        record["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            record["execution_status"] = record["status"]
            record["status"] = "cleanup_failed"
        record["finished_at"] = _utc()
        record["elapsed_seconds"] = time.monotonic() - started
        record["evidence"] = {}
        for name in ("process-start.json", "process-identity.json", "stdout.log", "stderr.log"):
            path = output_dir / name
            if path.is_file():
                record["evidence"][name] = {"sha256": _hash(path), "bytes": path.stat().st_size}
        _exclusive_json(output_dir / "process.json", record)
    if isinstance(caught, (KeyboardInterrupt, SystemExit)):
        raise caught
    if caught is not None or cleanup_errors:
        code = getattr(caught, "code", "cleanup_failed" if cleanup_errors else "runner_error")
        raise PrivateDesktopError(code, str(caught) if caught is not None else "Private runner cleanup failed.",
                                  receipt_path=output_dir / "process.json") from caught
    return record
