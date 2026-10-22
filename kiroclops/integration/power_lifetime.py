"""Opt-in lifetime binding for a Power's shell wrapper and its MCP child."""
from __future__ import annotations

import os
import threading
from pathlib import Path
import sys

from .storage import IntegrationError, process_identity

LAUNCHER_PID_ENV = "KIROCLOPS_LAUNCHER_PID"


def _launcher_alive(pid: int, identity: str) -> bool:
    if process_identity(pid) != identity:
        return False
    if os.name == "nt":
        # A dead Windows process can retain its creation identity while some
        # client still owns a process handle. Check exit status as well as PID.
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _watch_launcher(session, pid: int, identity: str, stop: threading.Event) -> None:
    while not stop.wait(.25):
        if not _launcher_alive(pid, identity):
            try:
                # Cancels only this MCP connection's workers and waits for its
                # bounded owned-tree cleanup before terminating this MCP child.
                session.close()
            finally:
                os._exit(0)
            return


def start_launcher_watch(session, *, environ=None) -> threading.Event | None:
    """CLI MCP servers are unchanged unless this explicit Power env is present."""
    environment = os.environ if environ is None else environ
    expected = environment.get("KIROCLOPS_EXPECTED_PYTHON")
    if expected and Path(sys.executable).resolve() != Path(expected).resolve():
        raise IntegrationError("power_runtime_mismatch", "Power resolved a different Python than its verified venv runtime")
    raw = environment.get(LAUNCHER_PID_ENV)
    if raw is None and environment.get("KIROCLOPS_WATCH_PARENT") == "1":
        raw = str(os.getppid())
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.isdecimal() or int(raw) < 1 or int(raw) == os.getpid():
        raise IntegrationError("invalid_power_launcher", "Power launcher PID is invalid")
    pid = int(raw)
    identity = process_identity(pid)
    if identity is None or not _launcher_alive(pid, identity):
        raise IntegrationError("power_launcher_gone", "Power launcher ended before MCP initialization")
    stop = threading.Event()
    threading.Thread(target=_watch_launcher, args=(session, pid, identity, stop),
                     name="kiroclops-power-parent", daemon=True).start()
    return stop
