"""Detached, bounded local analysis worker; invoked only with an owned job ID."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
from pathlib import Path
import threading
import time

from .backend import JOB_SCHEMA, TERMINAL, _atomic, _digest, _identity, _lock, _read, _update, _validate_job_id
from kiroclops.visual.schema import VisualError


def _contain_windows_children():
    """The worker owns its job object, so its death also kills only its descendants."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IOCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise VisualError("worker_containment_failed", "Could not create an owned Windows job object.")
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        kernel.CloseHandle(handle)
        raise VisualError("worker_containment_failed", "Could not contain analysis descendants in an owned Windows job object.")
    # Kept alive until process exit. Closing this handle earlier would kill this process.
    return handle


def run(root: Path, job_id: str) -> int:
    root = root.resolve()
    job_dir = root / "jobs" / _validate_job_id(job_id)
    stop = threading.Event()
    mutex = threading.RLock()
    started = None

    def check():
        if (job_dir / "cancel.json").exists():
            raise VisualError("job_cancelled", "Analysis was cancelled.")
        if started is not None and time.monotonic() - started >= state["timeout_seconds"]:
            raise VisualError("job_deadline_exceeded", "Analysis exceeded its configured execution deadline.")

    def finish_error(code: str, message: str):
        with mutex, _lock(job_dir / "state.lock"):
            current = _read(job_dir / "state.json")
            if current["status"] not in TERMINAL:
                current.update(status="cancelled" if code == "job_cancelled" else "failed", finished_at=time.time(), updated_at=time.time(), result=None, error={"code": code, "message": message})
                _atomic(job_dir / "state.json", current)

    def watchdog():
        while not stop.wait(0.1):
            try:
                check()
            except VisualError as exc:
                finish_error(exc.code, str(exc))
                # Interrupt a stalled native call as well as normal Python work.
                os._exit(130 if exc.code == "job_cancelled" else 124)

    try:
        containment_handle = _contain_windows_children()
        state = _read(job_dir / "state.json")
        if state.get("schema_version") != JOB_SCHEMA:
            raise VisualError("unsupported_schema", "Worker does not support this job schema.")
        if state["status"] in TERMINAL:
            return 0
        with _lock(job_dir / "worker.lock", timeout=0):
            _update(job_dir, worker={"pid": os.getpid(), "identity": _identity(os.getpid())})
            watcher = threading.Thread(target=watchdog, name="analysis-deadline", daemon=True)
            watcher.start()
            config = _read(root / "config.json")
            with contextlib.ExitStack() as stack:
                while True:
                    check()
                    claimed = False
                    for slot in range(config["max_workers"]):
                        try:
                            stack.enter_context(_lock(root / "slots" / f"{slot}.lock", timeout=0))
                            claimed = True
                            break
                        except TimeoutError:
                            pass
                    if claimed:
                        break
                    time.sleep(0.05)
                started = time.monotonic()
                _update(job_dir, status="running", started_at=time.time(), deadline_at=time.time() + state["timeout_seconds"], progress={"phase": "validating", "completed": 0, "total": 1})
                check()
                from kiroclops.visual.package import load_package
                from kiroclops.visual.tracking import analyze_package
                from kiroclops.visual.events import extract_events
                from kiroclops.cloud.identity import service_sources, engine_identity
                from kiroclops.cloud.results import envelope, make_measurement

                source_identity = service_sources()
                execution_start = time.perf_counter()

                request = _read(job_dir / "request.json")
                if _digest(request) != state["request_sha256"]:
                    raise VisualError("source_changed", "Analysis request changed after submission.")
                package = job_dir / "input" / "package"
                if load_package(package, verify='hashes')["package_sha256"] != state["package_sha256"]:
                    raise VisualError("source_changed", "Input snapshot changed before analysis.")

                last_progress = [None, 0.0]

                def progress(phase: str, completed: int, total: int):
                    check()
                    # Only UI progress is coalesced. Every frame still checks
                    # cancellation/deadlines, and all terminal evidence is fsynced.
                    if phase != last_progress[0] or completed == total or time.monotonic() - last_progress[1] >= .25:
                        with mutex:
                            _update(job_dir, progress={"phase": str(phase), "completed": int(completed), "total": int(total)})
                        last_progress[:] = [phase, time.monotonic()]

                observations = analyze_package(package, request["selection"], request.get("contract"), check=check, progress=progress)
                check()
                events = extract_events(observations, request["selection"], request.get("contract"))
                check()
                if load_package(package, verify='hashes')["package_sha256"] != state["package_sha256"] or _digest(_read(job_dir / "request.json")) != state["request_sha256"]:
                    raise VisualError("source_changed", "Input snapshot or request changed during analysis.")
                if observations.get("package_sha256") not in (None, state["package_sha256"]):
                    raise VisualError("result_binding_mismatch", "Observation result belongs to a different input package.")
                result = {"schema_version": 1, "kind": "AnalysisResult", "status": observations.get("status", "ok"), "measurement_status": observations.get("status", "ok"), "package_sha256": state["package_sha256"], "candidate_sha256": state.get("candidate_sha256"), "request_sha256": state["request_sha256"], "observations": observations, "events": events}
                if service_sources() != source_identity:
                    raise VisualError('source_changed', 'Analysis implementation changed during execution.')
                delivery = envelope(make_measurement(observations, events), request, logical_job_id=job_id,
                                    engine=engine_identity(), sources=source_identity,
                                    timings={'execution_wall_s': time.perf_counter() - execution_start})
                with mutex, _lock(job_dir / "state.lock"):
                    check()
                    current = _read(job_dir / "state.json")
                    if current["status"] in TERMINAL:
                        return 1
                    _atomic(job_dir / "delivery-v2.json", delivery)
                    _atomic(job_dir / "result.json", result)
                    current.update(status="succeeded", finished_at=time.time(), updated_at=time.time(), result_sha256=_digest(result), delivery_schema_version=2, delivery_sha256=_digest(delivery), progress={"phase": "complete", "completed": 1, "total": 1}, error=None)
                    _atomic(job_dir / "state.json", current)
                return 0
    except VisualError as exc:
        finish_error(exc.code, str(exc))
        return 1
    except TimeoutError:
        # A duplicate worker must not mutate the state owned by the first worker.
        return 2
    except BaseException as exc:
        finish_error("analysis_worker_error", f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    return run(args.root, args.job_id)


if __name__ == "__main__":
    raise SystemExit(main())
