"""Windows Job ownership at the native turn/final-validation boundary.

No Kiro model or game capture is started by these tests.  The process test uses
three short-lived local Python processes to mirror ACP -> MCP -> worker.
"""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kiroclops.integration import kiro
from kiroclops.local_engine import launcher, trial_cleanup
from kiroclops.visual.schema import file_sha256, write_json


@unittest.skipUnless(os.name == "nt", "Windows Job ownership")
class WindowsJobHandoffTests(unittest.TestCase):
    def test_closing_acp_job_kills_mcp_grandchild_worker(self):
        """A detached-looking worker still belongs to the ACP Windows Job."""
        with tempfile.TemporaryDirectory(prefix="job grandchild naïve ") as directory:
            root = Path(directory)
            pid_file = root / "worker.pid"
            worker = root / "worker.py"
            worker.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            mcp = root / "mcp.py"
            mcp.write_text(
                "import subprocess, sys\n"
                "from pathlib import Path\n"
                "process = subprocess.Popen([sys.executable, sys.argv[1]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "Path(sys.argv[2]).write_text(str(process.pid), encoding='ascii')\n",
                encoding="utf-8",
            )
            acp = root / "acp.py"
            acp.write_text(
                "import subprocess, sys\n"
                "process = subprocess.Popen([sys.executable, sys.argv[1], "
                "sys.argv[2], sys.argv[3]], stdin=subprocess.DEVNULL, "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
                "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "process.wait(timeout=10)\n",
                encoding="utf-8",
            )

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.TerminateProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]

            job = kiro._WindowsJob()
            parent = None
            worker_handle = None
            try:
                parent = subprocess.Popen(
                    [sys.executable, str(acp), str(mcp), str(worker), str(pid_file)],
                    creationflags=subprocess.CREATE_NO_WINDOW | 0x4,  # CREATE_SUSPENDED
                )
                job.assign_and_resume(parent)
                self.assertEqual(parent.wait(timeout=12), 0)
                until = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < until:
                    time.sleep(0.05)
                self.assertTrue(pid_file.exists(), "MCP did not publish its worker PID")
                # Keep a process handle open so a recycled PID cannot fool us.
                worker_handle = kernel.OpenProcess(0x100001, False, int(pid_file.read_text()))
                self.assertTrue(worker_handle)
                self.assertEqual(kernel.WaitForSingleObject(worker_handle, 0), 258)
                job.close()
                self.assertEqual(kernel.WaitForSingleObject(worker_handle, 5000), 0)
            finally:
                job.close()
                if worker_handle:
                    if kernel.WaitForSingleObject(worker_handle, 0) == 258:
                        kernel.TerminateProcess(worker_handle, 1)
                    kernel.CloseHandle(worker_handle)
                if parent is not None:
                    kiro._terminate_tree(parent)

    def test_sealed_finalization_is_awaited_before_acp_job_close(self):
        """The final worker must finish while its containing ACP Job is alive."""
        with tempfile.TemporaryDirectory(prefix="final order naïve ") as directory:
            root = Path(directory)
            prompt = root / "prompt.txt"
            prompt.write_text("local-only test", encoding="utf-8")
            events = []

            class FakeSession:
                def start_clock(self):
                    events.append("start_clock")
                    now = time.time()
                    return {"started_at": now, "deadline_at": now + 1800}

                def _load(self):
                    # Legacy v1 state: its frozen 30-minute policy needs no declaration.
                    return {"schema_version": 1, "experiment": {}, "session_id": "handoff-session",
                            "budget": {"max_candidates": 3, "max_seconds": 1800, "max_validation_seconds": 1800},
                            "active_job_id": "final-1", "final_job_id": "final-1", "sealed_at": 1}

            client = SimpleNamespace(
                guard=SimpleNamespace(generation_started=False, summary=lambda: {}),
                process=SimpleNamespace(pid=987656, poll=lambda: 0),
                prompt_sent=False,
                kas_version="test",
                deadline=0,
            )
            # The real client binds its startup identity before the handshake.
            # Liveness/PID-reuse checks are covered by test_owned_launcher_identity.
            identity = {"pid": 987656, "identity": "fixture-native-creation"}
            receipt = root / "kiro" / "run" / "owned-processes.json"
            write_json(receipt, {"kind": "OwnedNativeClientStartupIdentity", "status": "bound",
                                 "native_client_process": identity})
            client.owned_process_receipt = {"path": str(receipt), "sha256": file_sha256(receipt)}
            client.handshake = lambda: {"session_id": "test"}

            def send_prompt(_):
                # Startup rejects a client that generated before the task prompt.
                client.prompt_sent = client.guard.generation_started = True
                return {"stopReason": "end_turn"}

            client.prompt = send_prompt
            client.close = Mock(side_effect=lambda: events.append("close") or [])

            def begin(session, *, preserve_finalization=False):
                self.assertTrue(preserve_finalization)
                events.append("begin_cleanup")
                return {"job_id": "final-1", "had_pending_job": True}

            def await_final(session, request):
                events.append("await_final")
                return {"status": "terminal", "job_id": "final-1", "job_status": "succeeded"}

            agent = root / "agent.json"
            write_json(agent, {"mcpServers": {"kiroclops": {"fixture": True}}})
            ready = {"config": {"mode": "tools", "prompt_path": str(prompt), "agent_path": str(agent),
                                "target_dir": str(root / "target"),
                                "launcher_sha256": file_sha256(launcher.__file__)}, "cli_version": "test"}
            with patch.object(launcher, "preflight", return_value=ready), \
                    patch.object(launcher, "Session", return_value=FakeSession()), \
                    patch.object(launcher, "LocalACPClient", return_value=client), \
                    patch("kiroclops.local_engine.startup.probe_stdio", return_value={"status": "pass"}), \
                    patch.object(launcher, "_verify_saved_client_process", side_effect=lambda *a, **k: dict(identity)), \
                    patch.object(trial_cleanup, "begin_terminal_cleanup", side_effect=begin), \
                    patch.object(trial_cleanup, "finish_terminal_cleanup", return_value={"status": "pass"}), \
                    patch.object(launcher, "_await_preserved_finalization", side_effect=await_final), \
                    patch.object(launcher, "_await_preserved_worker_release", return_value={"worker_exit_verified": True}):
                record = launcher._run_native(root, {"run_kind": "test"})

            self.assertEqual(record["status"], "completed", record)
            self.assertEqual(record["postturn_finalization"]["job_status"], "succeeded")
            self.assertLess(events.index("begin_cleanup"), events.index("await_final"))
            self.assertLess(events.index("await_final"), events.index("close"))
            client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
