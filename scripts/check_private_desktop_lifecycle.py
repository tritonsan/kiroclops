"""Owned Python sleepers only: timeout, event cancellation and parent-death proof.

No GUI, engine, model, desktop activation or unrelated process is involved.
This qualification is separate from registered experiments and their evidence.
"""
from __future__ import annotations

import argparse
import ctypes as c
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.integration.private_desktop_v1 import run_private_desktop

SLEEP_CODE = "import time; print('private-sleeper-ready', flush=True); time.sleep(120)"
READY = b"private-sleeper-ready"
HELPER_SHA = "e6faed0d06c92b6e8c4ec602702b1a7cdca9787c1d2a4f39803ce130130526ca"


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path): return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


class ProcessProof:
    """Query/wait only. This class has no process termination operation."""
    def __init__(self, identity):
        self.k = c.WinDLL("kernel32", use_last_error=True)
        self.k.OpenProcess.argtypes, self.k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        self.k.GetProcessTimes.argtypes = [w.HANDLE, *([c.POINTER(w.FILETIME)] * 4)]
        self.k.GetProcessTimes.restype = w.BOOL
        self.k.WaitForSingleObject.argtypes, self.k.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
        self.k.GetExitCodeProcess.argtypes, self.k.GetExitCodeProcess.restype = [w.HANDLE, c.POINTER(w.DWORD)], w.BOOL
        self.k.CloseHandle.argtypes, self.k.CloseHandle.restype = [w.HANDLE], w.BOOL
        self.handle = self.k.OpenProcess(0x100000 | 0x1000, False, identity["pid"])
        if not self.handle:
            raise OSError(f"Cannot open the identified owned process: Win32 {c.get_last_error()}")
        try:
            stamps = [w.FILETIME() for _ in range(4)]
            if not self.k.GetProcessTimes(self.handle, *[c.byref(value) for value in stamps]):
                raise OSError("GetProcessTimes failed")
            created = (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime
            if created != identity["creation_filetime_100ns"]:
                raise ValueError("PID was reused; creation identity differs")
            self.identity = dict(identity)
        except BaseException:
            self.close()
            raise

    def wait(self, milliseconds):
        status = self.k.WaitForSingleObject(self.handle, milliseconds)
        if status not in (0, 258):
            raise OSError(f"Owned process wait failed: {status}")
        return status == 0

    def exit_code(self):
        value = w.DWORD()
        if not self.k.GetExitCodeProcess(self.handle, c.byref(value)):
            raise OSError("GetExitCodeProcess failed")
        return value.value

    def close(self):
        if self.handle:
            if not self.k.CloseHandle(self.handle):
                raise OSError("CloseHandle(owned proof) failed")
            self.handle = None


def wait_ready(directory, stop=None, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            raise RuntimeError("Readiness observation stopped before an owned live sleeper was found")
        identity_file, stdout = directory / "process-identity.json", directory / "stdout.log"
        if identity_file.is_file() and stdout.is_file() and READY in stdout.read_bytes():
            identity = load(identity_file)["process_identity"]
            proof = ProcessProof(identity)
            if proof.wait(0):
                proof.close()
                raise RuntimeError("Sleeper ended before the alive observation")
            return proof
        time.sleep(0.025)
    raise TimeoutError("Owned sleeper did not report readiness")


def verify_record(record, expected):
    if (record.get("status") != expected or record.get("cleanup_errors")
            or not record.get("owned_process_termination_confirmed") or not record.get("resumed_at")):
        raise ValueError("Unexpected private process lifecycle result")
    if not record.get("desktop_samples") or any(value.get("receives_input") is not False
            for value in record["desktop_samples"]):
        raise ValueError("Owned private desktop lacks non-input evidence")
    if record.get("saw_owned_window"):
        raise ValueError("Python sleeper unexpectedly created a GUI window")


def check_one(output, python, kind):
    directory = output / kind
    cancel, stop = threading.Event(), threading.Event()
    observed = {}
    def observe():
        try:
            observed["proof"] = wait_ready(directory, stop)
            observed["alive_before_stop"] = True
            if kind == "cancel":
                observed["cancel_requested_at_monotonic"] = time.monotonic()
                cancel.set()
        except BaseException as error:
            observed["error"] = f"{type(error).__name__}: {error}"
    watcher = threading.Thread(target=observe, daemon=True)
    watcher.start()
    try:
        record = run_private_desktop([str(python), "-B", "-c", SLEEP_CODE], cwd=ROOT,
            env=dict(os.environ), output_dir=directory, timeout=3 if kind == "timeout" else 15,
            cancel=cancel if kind == "cancel" else None)
    finally:
        stop.set()
        watcher.join(timeout=2)
    proof = observed.get("proof")
    try:
        if watcher.is_alive() or observed.get("error") or proof is None:
            raise ValueError(f"Independent alive observation missing: {observed.get('error')}")
        verify_record(record, "timeout" if kind == "timeout" else "cancelled")
        ended = proof.wait(0)
        result = {"status": "pass" if ended else "fail", "case": kind,
                  "process_identity": proof.identity, "observed_alive_before_stop": True,
                  "independent_process_handle_signalled_after_return": ended,
                  "exit_code": proof.exit_code(), "process_receipt_sha256": sha(directory / "process.json"),
                  "runtime_status": record["status"], "elapsed_seconds": record["elapsed_seconds"],
                  "own_desktop_non_input_samples": len(record["desktop_samples"]),
                  "no_gui_window_observed": not record["saw_owned_window"]}
        write_new(directory / "lifecycle-proof.json", result)
        if not ended: raise ValueError("Owned sleeper remained live after runner returned")
        return result
    finally:
        if proof: proof.close()


def check_parent_death(output, python):
    directory = output / "parent-death"
    directory.mkdir()
    child_directory = directory / "private-child"
    parent = proof = None
    try:
        with (directory / "launcher-stdout.log").open("xb") as stdout, (directory / "launcher-stderr.log").open("xb") as stderr:
            parent = subprocess.Popen([str(python), "-B", str(Path(__file__).resolve()), "--parent-sleeper", str(child_directory)],
                cwd=ROOT, env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
            proof = wait_ready(child_directory)
            if parent.poll() is not None:
                raise ValueError("The owned launcher exited before intentional termination")
            write_new(directory / "before-parent-stop.json", {
                "launcher_pid": parent.pid, "child_identity": proof.identity,
                "child_alive_by_verified_handle": not proof.wait(0),
                "child_identity_receipt_sha256": sha(child_directory / "process-identity.json"),
                "child_start_sha256": sha(child_directory / "process-start.json"),
                "action": "Terminate only the owned Popen launcher handle; never terminate the child directly"})
            started = time.monotonic()
            parent.terminate()  # Popen owns this exact process HANDLE; no PID lookup or tree kill.
            parent.wait(timeout=5)
            ended = proof.wait(5000)
            result = {"status": "pass" if ended else "fail", "case": "parent_death",
                "launcher_pid": parent.pid, "launcher_exit_code": parent.returncode,
                "child_identity": proof.identity, "child_observed_alive_before_parent_stop": True,
                "child_handle_signalled_after_parent_stop": ended,
                "child_exit_code": proof.exit_code(), "stop_to_confirmation_seconds": time.monotonic() - started,
                "child_directly_terminated_by_qualifier": False,
                "native_runner_final_receipt_present": (child_directory / "process.json").exists(),
                "scope": "Actual owned launcher termination; OS job kill-on-close, no fabricated native completion"}
            if result["native_runner_final_receipt_present"]:
                raise ValueError("Unexpected normal runner completion during parent-death test")
            write_new(directory / "lifecycle-proof.json", result)
            if not ended:
                raise ValueError("Kill-on-job-close not confirmed; owned child still live")
            return result
    finally:
        if parent is not None and parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=5)
        if proof: proof.close()


def check(output):
    output = Path(output).resolve()
    allowed_parent = ROOT / "artifacts/model-trials/preflight"
    if output.parent != allowed_parent.resolve() or output.exists():
        raise ValueError("Output must be a fresh immediate preflight child")
    helper = ROOT / "kiroclops/integration/private_desktop_v1.py"
    if sha(helper) != HELPER_SHA:
        raise ValueError("The reviewed private runner bytes changed")
    python = Path(sys._base_executable).resolve()  # Direct interpreter, not a Windows venv redirector.
    if not python.is_file() or python.suffix.lower() != ".exe":
        raise ValueError("A direct Windows Python executable is required")
    lock_path = ROOT / "config/phase2b/tracking-lock.json"
    frozen = load(lock_path)["files"]
    if any(sha(ROOT / name) != expected for name, expected in frozen.items()):
        raise ValueError("Registered frozen input changed")
    output.mkdir()
    result = {"status": "running", "scope": "Owned Python sleepers only; no GUI/engine/model",
        "runner_sha256": sha(helper), "qualification_script_sha256": sha(__file__),
        "python_path": str(python), "python_sha256": sha(python), "sleep_code": SLEEP_CODE,
        "frozen_lock_sha256": sha(lock_path), "cases": [],
        "limitations": ["No game renderer or pixel/timing qualification is implied.",
                       "Normal process samples query only each newly created non-input desktop.",
                       "Parent-death case has immutable start/identity evidence, not a fabricated final runner receipt."]}
    write_new(output / "qualification-start.json", result)
    try:
        for case in ("timeout", "cancel"):
            result["cases"].append(check_one(output, python, case))
        result["cases"].append(check_parent_death(output, python))
        result["status"] = "pass"
    except BaseException as error:
        result.update(status="fail", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        result["runner_unchanged"] = sha(helper) == HELPER_SHA
        result["frozen_mismatches"] = [name for name, expected in frozen.items() if sha(ROOT / name) != expected]
        if not result["runner_unchanged"] or result["frozen_mismatches"]:
            result["status"] = "fail"
        result["evidence"] = {path.relative_to(output).as_posix():sha(path)
                              for path in sorted(output.rglob("*")) if path.is_file()}
        write_new(output / "result.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--parent-sleeper", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.parent_sleeper is not None:
        # Called only by the owned qualification parent above. This launcher
        # intentionally receives no child process handles beyond its own runner.
        run_private_desktop([str(Path(sys._base_executable).resolve()), "-B", "-c", SLEEP_CODE],
            cwd=ROOT, env=dict(os.environ), output_dir=args.parent_sleeper, timeout=30)
    elif args.output is None:
        parser.error("--output is required")
    else:
        raise SystemExit(check(args.output))
