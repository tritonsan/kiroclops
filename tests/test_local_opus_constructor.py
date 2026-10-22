"""The local client must bind the explicitly selected model before prompting."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from kiroclops.local_engine import launcher
from kiroclops.portable_v2 import launcher as portable
from kiroclops.visual.schema import file_sha256


class LocalModelConstructorTests(unittest.TestCase):
    def test_constructor_binds_opus55_max_guard_before_any_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            ready = {"config": {"agent_name": "kiroclops-local-tools", "session_dir": tmp,
                     "target_dir": str(target), "model": "claude-opus-5.5", "effort": "max",
                     "approval_mode": launcher.APPROVAL,
                     "launcher_sha256": file_sha256(launcher.__file__)}, "executable": "kiro-cli"}
            # The constructor durably records the launcher and native-client creation
            # identities before any handshake, so the mocked child needs an integer PID.
            with patch.object(launcher.subprocess, "Popen", return_value=MagicMock(pid=987654)) as popen, \
                 patch.object(launcher.native, "_WindowsJob", return_value=MagicMock()), \
                 patch.object(portable.threading, "Thread"), \
                 patch("kiroclops.local_engine.backend._identity", side_effect=lambda pid: f"creation-{pid}"):
                client = launcher.LocalACPClient(ready, Path(tmp) / "run", 30, None)
            popen.assert_called_once()
            self.assertEqual(client.guard.expected, {"mode": "kiroclops-local-tools",
                                                     "model": "claude-opus-5.5", "effort": "max"})
            self.assertFalse(client.prompt_sent)

    def test_constructor_rejects_fallback_models_and_lower_effort_without_launching(self):
        with tempfile.TemporaryDirectory() as tmp:
            for model, effort in (("gpt-5.6-sol", "max"), ("claude-opus-5", "max"),
                                  ("auto", "max"), ("claude-opus-5.5", "high")):
                with self.subTest(model=model, effort=effort):
                    ready = {"config": {"agent_name": "kiroclops-local-tools",
                             "target_dir": tmp, "model": model, "effort": effort,
                             "approval_mode": launcher.APPROVAL}, "executable": "kiro-cli"}
                    with patch.object(launcher.subprocess, "Popen") as popen:
                        with self.assertRaises(launcher.native.KiroError) as error:
                            launcher.LocalACPClient(ready, Path(tmp) / "run", 30, None)
                    self.assertEqual(error.exception.code, "selection_changed")
                    popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
