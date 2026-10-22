import os
import threading
import unittest
from unittest.mock import Mock, patch

from kiroclops.integration import power_lifetime as lifetime
from kiroclops.integration.storage import IntegrationError


class PowerLifetimeTests(unittest.TestCase):
    def test_cli_without_opt_in_has_no_watch_or_identity_probe(self):
        with patch.object(lifetime, "process_identity") as identity, patch.object(lifetime.threading, "Thread") as thread:
            self.assertIsNone(lifetime.start_launcher_watch(Mock(), environ={}))
        identity.assert_not_called()
        thread.assert_not_called()

    def test_invalid_or_dead_launcher_fails_before_stdio_starts(self):
        for value in ("", "bad", "-1", "0", str(os.getpid())):
            with self.subTest(value=value), self.assertRaises(IntegrationError):
                lifetime.start_launcher_watch(Mock(), environ={lifetime.LAUNCHER_PID_ENV: value})
        with patch.object(lifetime, "process_identity", return_value=None), self.assertRaises(IntegrationError) as caught:
            lifetime.start_launcher_watch(Mock(), environ={lifetime.LAUNCHER_PID_ENV: "12345"})
        self.assertEqual(caught.exception.code, "power_launcher_gone")

    def test_reused_pid_is_not_the_same_launcher(self):
        with patch.object(lifetime, "process_identity", return_value="new-creation"):
            self.assertFalse(lifetime._launcher_alive(12345, "original-creation"))

    def test_direct_power_binds_parent_and_rejects_wrong_python(self):
        with patch.object(lifetime.os, "getppid", return_value=12345), \
                patch.object(lifetime, "process_identity", return_value="creation"), \
                patch.object(lifetime, "_launcher_alive", return_value=True), patch.object(lifetime.threading, "Thread") as thread:
            lifetime.start_launcher_watch(Mock(), environ={"KIROCLOPS_WATCH_PARENT": "1", "KIROCLOPS_EXPECTED_PYTHON": lifetime.sys.executable})
        self.assertEqual(thread.call_args.kwargs["args"][1:3], (12345, "creation"))
        with self.assertRaises(IntegrationError) as caught:
            lifetime.start_launcher_watch(Mock(), environ={"KIROCLOPS_EXPECTED_PYTHON": "different-python.exe"})
        self.assertEqual(caught.exception.code, "power_runtime_mismatch")

    def test_launcher_death_closes_owned_session_before_exit(self):
        session, stop = Mock(), Mock()
        stop.wait.return_value = False
        order = []
        session.close.side_effect = lambda: order.append("close")
        with patch.object(lifetime, "_launcher_alive", return_value=False), patch.object(lifetime.os, "_exit", side_effect=lambda code: order.append(("exit", code))):
            lifetime._watch_launcher(session, 12345, "creation", stop)
        self.assertEqual(order, ["close", ("exit", 0)])

    def test_cleanup_error_still_terminates_only_this_mcp_child(self):
        session, stop = Mock(), Mock()
        stop.wait.return_value = False
        session.close.side_effect = RuntimeError("cleanup failed")
        with patch.object(lifetime, "_launcher_alive", return_value=False), patch.object(lifetime.os, "_exit") as exit_process:
            with self.assertRaises(RuntimeError):
                lifetime._watch_launcher(session, 12345, "creation", stop)
        exit_process.assert_called_once_with(0)

    def test_normal_stdio_shutdown_stops_watch_without_process_exit(self):
        stop = threading.Event()
        stop.set()
        session = Mock()
        with patch.object(lifetime.os, "_exit") as exit_process:
            lifetime._watch_launcher(session, 12345, "creation", stop)
        session.close.assert_not_called()
        exit_process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
