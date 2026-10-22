"""Mock-only Windows launcher checks. Never starts a real process or desktop."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kiroclops.integration import private_desktop_v1 as pd


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.name = None
        self.fault = None
        self.clock = 0.0
        self.complete = True
        self.code = 0
        self.input_samples = []
        self.cancel_on_wait = None

    def note(self, name, *values):
        self.calls.append((name, *values))
        if self.fault == name:
            raise pd.PrivateDesktopError(name, "Injected failure")

    def station_name(self): self.note("station_name"); return "WinSta0"
    def create_desktop(self, name): self.note("create_desktop", name); self.name = name; return 10
    def desktop_state(self, desktop):
        self.note("desktop_state", desktop)
        return {"name": self.name, "receives_input": self.input_samples.pop(0) if self.input_samples else False}
    def owned_window_count(self, desktop, pid): self.note("owned_window_count", desktop, pid); return 1
    def create_job(self): self.note("create_job"); return 20
    def configure_job(self, job): self.note("configure_job", job)
    def open_stdio(self, path):
        self.note("open_stdio", path)
        if path: path.write_bytes(b"child log\n")
        return 30 + len([call for call in self.calls if call[0] == "open_stdio"])
    def create_process(self, argv, **kwargs): self.note("create_process", argv, kwargs); return pd._Child(40, 41, 1234, 1235)
    def verify_job(self, process, job): self.note("verify_job", process, job)
    def creation_time(self, process): self.note("creation_time", process); return 133700000000000000
    def resume(self, thread): self.note("resume", thread)
    def wait(self, process, milliseconds):
        self.note("wait", process, milliseconds)
        if milliseconds == 5000: return True
        self.clock += milliseconds / 1000
        if self.cancel_on_wait: self.cancel_on_wait.set()
        return self.complete
    def exit_code(self, process): self.note("exit_code", process); return self.code
    def terminate_job(self, job): self.note("terminate_job", job)
    def terminate_process(self, process): self.note("terminate_process", process)
    def close_handle(self, handle): self.note("close_handle", handle)
    def close_desktop(self, handle): self.note("close_desktop", handle)


class PrivateDesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.root / "game café.exe"
        self.executable.write_bytes(b"fake executable; never run")
        self.output = self.root / "new-run"
        self.env = {"SystemRoot": "C:\\Windows", "TOKEN": "do-not-print-value", "UNICODE": "Montréal 🌒"}
        self.api = FakeAPI()
        factory = patch.object(pd, "_WindowsAPI", return_value=self.api)
        self.factory = factory.start(); self.addCleanup(factory.stop)
        clock = patch.object(pd.time, "monotonic", side_effect=lambda: self.api.clock)
        clock.start(); self.addCleanup(clock.stop)

    def run_fake(self, **kwargs):
        args = dict(cwd=self.root, env=self.env, output_dir=self.output, timeout=1.0)
        args.update(kwargs)
        return pd.run_private_desktop([str(self.executable), "space value", 'embedded"quote', ""], **args)

    def result(self): return json.loads((self.output / "process.json").read_text(encoding="utf-8"))
    def names(self): return [call[0] for call in self.api.calls]

    def test_success_job_and_identity_precede_resume_and_all_handles_close(self):
        result = self.run_fake()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["process_identity"]["pid"], 1234)
        self.assertEqual(result["process_identity"]["creation_filetime_100ns"], 133700000000000000)
        self.assertTrue(result["owned_process_termination_confirmed"])
        self.assertTrue(result["saw_owned_window"])
        for name in ("configure_job", "verify_job", "creation_time"):
            self.assertLess(self.names().index(name), self.names().index("resume"))
        self.assertLess(self.names().index("terminate_job"), self.names().index("close_desktop"))
        self.assertEqual([call[1] for call in self.api.calls if call[0] == "close_handle"], [20, 41, 40, 33, 32, 31])
        self.assertGreaterEqual(len(result["desktop_samples"]), 4)
        self.assertTrue(all(sample["receives_input"] is False for sample in result["desktop_samples"]))
        self.assertRegex(result["desktop_name"], r"^KiroClops_[0-9a-f]{32}$")
        identity = json.loads((self.output / "process-identity.json").read_text())
        self.assertEqual(identity["status"], "suspended_in_job")
        self.assertNotIn("do-not-print-value", (self.output / "process.json").read_text())
        self.assertEqual(result["policy"]["maximum_active_processes"], 1)

    def test_desktop_failure_never_creates_or_resumes_process(self):
        self.api.fault = "create_desktop"
        with self.assertRaises(pd.PrivateDesktopError) as caught: self.run_fake()
        self.assertNotIn("create_process", self.names())
        self.assertNotIn("resume", self.names())
        self.assertEqual(self.result()["status"], "error")
        self.assertEqual(caught.exception.receipt_path, self.output / "process.json")

    def test_job_configuration_failure_closes_desktop_no_process(self):
        self.api.fault = "configure_job"
        with self.assertRaises(pd.PrivateDesktopError): self.run_fake()
        self.assertNotIn("create_process", self.names())
        self.assertIn(("close_handle", 20), self.api.calls)
        self.assertIn(("close_desktop", 10), self.api.calls)

    def test_failed_atomic_job_verification_never_resumes(self):
        self.api.fault = "verify_job"
        with self.assertRaises(pd.PrivateDesktopError): self.run_fake()
        self.assertNotIn("resume", self.names())
        self.assertIn(("terminate_job", 20), self.api.calls)
        self.assertTrue(self.result()["owned_process_termination_confirmed"])

    def test_pre_cancel_is_read_only(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(pd.PrivateDesktopError): self.run_fake(cancel=cancel)
        self.assertFalse(self.output.exists())
        self.factory.assert_not_called()

    def test_runtime_cancel_kills_owned_job_and_preserves_logs(self):
        cancel = threading.Event()
        self.api.complete = False
        self.api.cancel_on_wait = cancel
        result = self.run_fake(cancel=cancel)
        self.assertEqual(result["status"], "cancelled")
        self.assertIn(("terminate_job", 20), self.api.calls)
        self.assertIn("stdout.log", result["evidence"])

    def test_timeout_has_bounded_wait_and_job_cleanup(self):
        self.api.complete = False
        result = self.run_fake(timeout=0.3)
        self.assertEqual(result["status"], "timeout")
        self.assertLessEqual(result["execution_elapsed_seconds"], 0.301)
        self.assertTrue(all(call[2] <= 200 for call in self.api.calls if call[0] == "wait" and call[2] != 5000))
        self.assertIn(("terminate_job", 20), self.api.calls)

    def test_nonzero_exit_is_not_success(self):
        self.api.code = 17
        result = self.run_fake()
        self.assertEqual((result["status"], result["exit_code"]), ("failed", 17))

    def test_own_desktop_receives_input_at_creation_fail_closed(self):
        self.api.input_samples = [True]
        with self.assertRaises(pd.PrivateDesktopError): self.run_fake()
        self.assertNotIn("create_process", self.names())
        self.assertIn(("close_desktop", 10), self.api.calls)

    def test_input_state_change_after_resume_is_error_and_kills(self):
        self.api.input_samples = [False, False, True]
        with self.assertRaises(pd.PrivateDesktopError): self.run_fake()
        self.assertIn("resume", self.names())
        self.assertEqual(self.result()["error"]["code"], "desktop_isolation_failed")
        self.assertIn(("terminate_job", 20), self.api.calls)

    def test_keyboard_interrupt_cleanup_and_receipt_before_reraise(self):
        def interrupted(*args): raise KeyboardInterrupt()
        self.api.wait = interrupted
        with self.assertRaises(KeyboardInterrupt): self.run_fake()
        record = self.result()
        self.assertEqual(record["execution_status"], "interrupted")
        self.assertIn(("close_desktop", 10), self.api.calls)
        self.assertIn(("terminate_process", 40), self.api.calls)

    def test_cleanup_failure_cannot_report_completed(self):
        self.api.fault = "terminate_job"
        with self.assertRaises(pd.PrivateDesktopError): self.run_fake()
        self.assertEqual(self.result()["status"], "cleanup_failed")
        self.assertIn(("close_handle", 20), self.api.calls)
        self.assertIn(("close_desktop", 10), self.api.calls)

    def test_output_is_exclusive(self):
        self.output.mkdir()
        (self.output / "keep.txt").write_text("old evidence")
        with self.assertRaises(ValueError): self.run_fake()
        self.assertEqual((self.output / "keep.txt").read_text(), "old evidence")
        self.factory.assert_not_called()

    def test_invalid_timeout_environment_and_executable_do_not_touch_os(self):
        for value in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=value), self.assertRaises(ValueError): self.run_fake(timeout=value)
        for env in ({}, {"bad": "x"}, {"SystemRoot": "x", "PATH": "a", "path": "b"}, {"SystemRoot": "a\0b"}):
            with self.subTest(env=env), self.assertRaises(ValueError): self.run_fake(env=env)
        with self.assertRaises(ValueError):
            pd.run_private_desktop(["cmd.exe"], cwd=self.root, env=self.env, output_dir=self.output, timeout=1)
        self.factory.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_explicit_environment_sorted_unicode_and_double_nul(self):
        block = pd.environment_block({"z": "🌒", "SystemRoot": "C:\\Windows", "a": "É"})
        self.assertEqual(block, "a=É\0SystemRoot=C:\\Windows\0z=🌒\0\0")
        self.run_fake()
        call = next(call for call in self.api.calls if call[0] == "create_process")
        self.assertEqual(call[2]["env_block"], pd.environment_block(self.env))
        self.assertEqual(call[2]["stdio"], [31, 32, 33])
        self.assertEqual(call[2]["desktop"], "WinSta0\\" + self.api.name)


class FakeFunction:
    def __init__(self, callback=lambda *args: 1): self.callback = callback
    def __call__(self, *args): return self.callback(*args)


class FakeDLL:
    def __getattr__(self, name):
        value = FakeFunction()
        setattr(self, name, value)
        return value


class Win32BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.kernel, self.user = FakeDLL(), FakeDLL()
        self.api = pd._WindowsAPI(kernel=self.kernel, user=self.user)

    def test_native_process_attributes_exclude_job_desktop_handles_and_bind_job_atomically(self):
        attributes = {}
        deleted = []
        observed = {}
        def initialize(buffer, count, flags, size):
            self.assertEqual(count, 2)
            c.cast(size, c.POINTER(c.c_size_t)).contents.value = 256
            return bool(buffer)
        def update(buffer, flags, key, values, size, previous, required):
            attributes[key] = list(c.cast(values, c.POINTER(w.HANDLE * (size // c.sizeof(w.HANDLE)))).contents)
            return 1
        argv = ["C:\\unicodé 🌒\\godot.exe", "space value", 'x"y', "", "C:\\tail slash\\"]
        env = pd.environment_block({"SystemRoot": "C:\\Windows", "STAR": "🌒"})
        def create(exe, command, proc_security, thread_security, inherit, flags, environment, cwd, startup, info):
            observed.update(exe=exe, command=c.wstring_at(command), flags=flags, inherit=inherit)
            self.assertEqual(c.string_at(environment, len(env.encode("utf-16-le"))), env.encode("utf-16-le"))
            value = c.cast(startup, c.POINTER(pd._StartupEx)).contents
            self.assertEqual(value.startup.cb, c.sizeof(pd._StartupEx))
            self.assertEqual(value.startup.desktop, "WinSta0\\KiroClops_private")
            self.assertEqual((value.startup.stdin, value.startup.stdout, value.startup.stderr), (31,32,33))
            target = c.cast(info, c.POINTER(pd._ProcessInfo)).contents
            target.process, target.thread, target.pid, target.tid = 40,41,1234,1235
            return 1
        self.kernel.InitializeProcThreadAttributeList.callback = initialize
        self.kernel.UpdateProcThreadAttribute.callback = update
        self.kernel.CreateProcessW.callback = create
        self.kernel.DeleteProcThreadAttributeList.callback = lambda pointer: deleted.append(True)
        result = self.api.create_process(argv, cwd=Path("C:\\project"), env_block=env,
                                        desktop="WinSta0\\KiroClops_private", stdio=[31,32,33], job=20)
        self.assertEqual(attributes, {pd.HANDLE_LIST:[31,32,33], pd.JOB_LIST:[20]})
        self.assertEqual(observed["command"], subprocess.list2cmdline(argv))
        self.assertTrue(observed["inherit"])
        self.assertEqual(observed["flags"], pd.CREATE_FLAGS)
        self.assertTrue(observed["flags"] & 0x4)
        self.assertFalse(observed["flags"] & 0x01000000)  # no BREAKAWAY_FROM_JOB
        self.assertEqual(deleted, [True])
        self.assertEqual(result, pd._Child(40,41,1234,1235))

    def test_native_job_has_no_breakaway_and_only_one_active_process(self):
        observed = {}
        def set_limits(job, kind, pointer, size):
            value = c.cast(pointer, c.POINTER(pd._Limits)).contents
            observed.update(job=job, kind=kind, flags=value.basic.flags, count=value.basic.active_limit)
            return 1
        self.kernel.SetInformationJobObject.callback = set_limits
        self.api.configure_job(20)
        self.assertEqual(observed, {"job":20,"kind":9,"flags":0x2008,"count":1})

    def test_desktop_and_job_are_non_inheritable_and_no_switch_right_requested(self):
        observed = []
        self.user.CreateDesktopW.callback = lambda *args: observed.append(args) or 10
        self.kernel.CreateJobObjectW.callback = lambda *args: observed.append(args) or 20
        self.api.create_desktop("KiroClops_new")
        self.api.create_job()
        self.assertIsNone(observed[0][5])
        self.assertEqual(observed[0][3], 0)
        self.assertFalse(observed[0][4] & 0x100)  # DESKTOP_SWITCHDESKTOP
        self.assertEqual(observed[1], (None, None))

    def test_stdio_handles_dedicated_inheritable_and_outputs_create_new(self):
        observed = []
        def create(*args):
            security = c.cast(args[3], c.POINTER(pd._Security)).contents
            observed.append((args[0], args[1], args[4], bool(security.inherit)))
            return 31
        self.kernel.CreateFileW.callback = create
        self.api.open_stdio(None)
        self.api.open_stdio(Path("C:/logs/stdout.log"))
        self.assertEqual(observed[0], ("NUL", 0x80000000, 3, True))
        self.assertEqual(observed[1][1:], (0x40000000, 1, True))

    def test_window_inventory_is_limited_to_owned_desktop_and_direct_pid_only(self):
        visited = []
        def windows(desktop, callback, argument):
            visited.append(desktop)
            for window in (100, 101, 102): callback(window, argument)
            return 1
        def owner(window, pointer):
            c.cast(pointer, c.POINTER(w.DWORD)).contents.value = 1234 if window != 101 else 5678
            return 99
        self.user.EnumDesktopWindows.callback = windows
        self.user.GetWindowThreadProcessId.callback = owner
        self.assertEqual(self.api.owned_window_count(10, 1234), 2)
        self.assertEqual(visited, [10])
        with self.assertRaises(pd.PrivateDesktopError): self.api.owned_window_count(None, 1234)
        self.assertEqual(visited, [10])

    def test_destroyed_window_race_is_not_counted_and_other_errors_fail_closed(self):
        self.user.EnumDesktopWindows.callback = lambda desktop, callback, argument: callback(100, argument)
        def destroyed(window, pointer):
            c.set_last_error(1400)
            return 0
        self.user.GetWindowThreadProcessId.callback = destroyed
        self.assertEqual(self.api.owned_window_count(10, 1234), 0)
        def denied(window, pointer):
            c.set_last_error(5)
            return 0
        self.user.GetWindowThreadProcessId.callback = denied
        with self.assertRaises(pd.PrivateDesktopError) as caught:
            self.api.owned_window_count(10, 1234)
        self.assertEqual(caught.exception.code, "window_owner_failed")

    def test_empty_desktop_clears_stale_error_and_returns_zero_unattested_windows(self):
        c.set_last_error(122)
        observed = []
        def empty(desktop, callback, argument):
            observed.append((desktop, c.get_last_error()))
            return 0
        self.user.EnumDesktopWindows.callback = empty
        self.assertEqual(self.api.owned_window_count(10, 1234), 0)
        self.assertEqual(observed, [(10, 0)])

    def test_empty_enumeration_with_real_error_is_still_fatal(self):
        for code in (5, 6, 122):
            def failure(desktop, callback, argument):
                c.set_last_error(code)
                return 0
            self.user.EnumDesktopWindows.callback = failure
            with self.subTest(error=code), self.assertRaises(pd.PrivateDesktopError):
                self.api.owned_window_count(10, 1234)

    def test_partial_enumeration_false_is_not_accepted_as_empty(self):
        def owner(window, pointer):
            c.cast(pointer, c.POINTER(w.DWORD)).contents.value = 1234
            return 99
        def partial(desktop, callback, argument):
            callback(100, argument)
            c.set_last_error(0)
            return 0
        self.user.GetWindowThreadProcessId.callback = owner
        self.user.EnumDesktopWindows.callback = partial
        with self.assertRaises(pd.PrivateDesktopError): self.api.owned_window_count(10, 1234)


if __name__ == "__main__":
    unittest.main()
