"""Mocked bridge integration: no real desktop, game, or child process is started."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kiroclops.integration import isolated_runtime_v2 as bridge
from kiroclops.integration import runner


class IsolatedRuntimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "game café"
        self.project.mkdir()
        (self.project / "project.godot").write_text("original project", encoding="utf-8")
        (self.project / "player.gd").write_text("original controller", encoding="utf-8")
        for scene in bridge.SCENES:
            p = self.project / scene[6:]
            p.parent.mkdir(exist_ok=True)
            p.write_text("real fixture placeholder")
        self.gui = self.root / bridge.GUI_RELATIVE
        self.gui.parent.mkdir(parents=True)
        self.gui.write_bytes(b"fake engine: never execute")
        self.console = self.gui.with_name(self.gui.stem + "_console.exe")
        self.console.write_bytes(b"fake console wrapper: never execute")
        self.ffmpeg = self.root / "ffmpeg.exe"
        self.ffmpeg.write_bytes(b"fake encoder: never execute")
        for name, value in [("ROOT", self.root), ("GUI_SHA256", bridge._sha(self.gui))]:
            p = patch.object(bridge, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(bridge.shutil, "which", return_value=str(self.ffmpeg)); p.start(); self.addCleanup(p.stop)
        self.original = Mock(side_effect=self.cli)
        p = patch.object(runner, "_run_process", self.original); p.start(); self.addCleanup(p.stop)
        self.private = Mock(side_effect=self.private_run)
        p = patch.object(bridge.private, "run_private_desktop", self.private); p.start(); self.addCleanup(p.stop)
        self.out = self.root / "results"; self.out.mkdir()
        self.log = self.out / "engine.log"
        self.config = self.out / "capture_config.json"
        self.config.write_text(json.dumps({"output_dir": str(self.out), "normal_level": False}))
        self.env = {"SystemRoot": "C:\\Windows", "KIROCLOPS_CONFIG": str(self.config)}
        self.behavior = None
        self.exit_code = 0
        self.process_status = "completed"
        self.owned = True
        self.input_desktop = False
        self.private_error = None
        self.proof_transform = None
        self.cli_code = 0
        self.before = runner.source_hashes(self.project)

    def context(self, **kwargs):
        return bridge.isolated_runtime(project_roots=[self.project], **kwargs)

    def argv(self, scene="res://kiroclops_capture/Capture.tscn"):
        return [self.console, "--path", self.project, "--fixed-fps", "60", "--resolution", "640x360",
                "--windowed", scene, "--rendering-method", "gl_compatibility"]

    def call(self, args=None, **kwargs):
        options = dict(cwd=self.project, env=self.env, log_path=self.log, timeout=120)
        options.update(kwargs)
        return runner._run_process(args or self.argv(), **options)

    def cli(self, argv, *, cwd, env, log_path, cancel=None, timeout=120):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_bytes((bridge.ENGINE_VERSION + "\r\n").encode() if "--version" in argv else b"CLI result\n")
        return self.cli_code

    def private_run(self, argv, *, cwd, env, output_dir, timeout, cancel):
        output_dir.mkdir()
        (output_dir / "stdout.log").write_bytes(b"KIROCLOPS_STATE physics_tick=90 state=Jump\n")
        (output_dir / "stderr.log").write_bytes(b"diagnostic\n")
        if self.behavior:
            self.behavior()
        if self.private_error:
            raise self.private_error
        result = {"schema_version": 1, "runner": bridge.private.VERSION,
                  "status": self.process_status, "exit_code": self.exit_code,
                  "argv": argv, "cwd": str(cwd), "executable_sha256": bridge.GUI_SHA256,
                  "timeout_seconds": timeout, "environment_keys": sorted(env), "environment_sha256": "mock-env-sha",
                  "started_at": "2026-09-19T14:00:00+00:00", "resumed_at": "2026-09-19T14:00:02+00:00",
                  "finished_at": "2026-09-19T14:00:03+00:00",
                  "process_identity": {"pid": 123, "primary_thread_id": 456, "creation_filetime_100ns": 123456789},
                  "desktop_path": "WinSta0\\KiroClops_mock",
                  "policy": bridge.PROCESS_POLICY.copy(),
                  "owned_process_termination_confirmed": True, "cleanup_errors": [],
                  "desktop_name": "KiroClops_mock", "saw_owned_window": self.owned,
                  "desktop_samples": [{"name": "KiroClops_mock", "receives_input": self.input_desktop,
                                       "owned_window_count": 1 if self.owned else 0}]}
        start = {key: result[key] for key in ("schema_version", "runner", "argv", "cwd", "executable_sha256",
                  "policy", "desktop_name", "started_at", "timeout_seconds", "environment_keys", "environment_sha256")}
        start["status"] = "starting"
        (output_dir / "process-start.json").write_text(json.dumps(start))
        identity = {"runner": bridge.private.VERSION, "status": "suspended_in_job",
                    "process_identity": result["process_identity"], "desktop_path": result["desktop_path"],
                    "recorded_at": "2026-09-19T14:00:01+00:00",
                    "start_sha256": bridge._sha(output_dir / "process-start.json")}
        (output_dir / "process-identity.json").write_text(json.dumps(identity))
        result["evidence"] = {name: {"sha256": bridge._sha(output_dir / name), "bytes": (output_dir / name).stat().st_size}
            for name in ("process-start.json", "process-identity.json", "stdout.log", "stderr.log")}
        if self.proof_transform:
            self.proof_transform(result, output_dir)
        (output_dir / "process.json").write_text(json.dumps(result))
        return result

    def receipt(self):
        return json.loads((self.out / "engine.isolated-runtime-v2/bridge.json").read_text())

    def test_real_funnels_route_to_direct_private_engine_and_restore(self):
        for scene in bridge.SCENES:
            with self.subTest(scene=scene):
                folder = self.out / scene.split("/")[2]; folder.mkdir()
                cfg = folder / "config.json"; cfg.write_text(json.dumps({"output_dir": str(folder), "normal_level": True}))
                with self.context(expected_sources={self.project: self.before}) as runtime:
                    self.assertEqual(self.call(self.argv(scene), log_path=folder / "engine.log",
                        env={**self.env, "KIROCLOPS_CONFIG": str(cfg)}), 0)
                    self.assertEqual(len(runtime.receipts), 1)
                self.assertIs(runner._run_process, self.original)
                args, kw = self.private.call_args
                self.assertEqual(args[0][0], str(self.gui))
                self.assertEqual(args[0][1:], [str(x) for x in self.argv(scene)[1:]])
                self.assertEqual(kw["cwd"], self.project)
                receipt = json.loads(runtime.receipts[0].read_text())
                self.assertEqual(receipt["source_hashes_before"], self.before)
                self.assertEqual(receipt["source_hashes_after"], self.before)
                self.assertEqual(receipt["config"]["sha256"], bridge._sha(cfg))
                self.assertEqual(receipt["status"], "completed")
                self.assertIn("process/process.json", receipt["evidence_hashes"])
        self.original.assert_not_called()

    def test_native_logs_preserve_state_parser_content(self):
        with self.context(): self.call()
        self.assertIn("state=Jump", self.log.read_text())
        self.assertIn("diagnostic", self.log.read_text())
        self.assertEqual(self.receipt()["log_sha256"], bridge._sha(self.log))

    def test_receipt_verifier_replays_hashes_and_detects_changed_raw_proof(self):
        with self.context() as runtime: self.call()
        receipt = runtime.receipts[0]
        self.assertEqual(bridge.verify_runtime_receipt(receipt, project=self.project,
            expected_source_hashes=self.before)["status"], "pass")
        (receipt.parent / "process/stdout.log").write_bytes(b"altered log")
        self.assertEqual(bridge.verify_runtime_receipt(receipt)["status"], "fail")

    def test_receipt_verifier_rejects_source_and_selected_project_mismatch(self):
        with self.context() as runtime: self.call()
        receipt = runtime.receipts[0]
        self.assertEqual(bridge.verify_runtime_receipt(receipt, expected_source_hashes={})["status"], "fail")
        (self.project / "player.gd").write_bytes(b"later mutation")
        self.assertEqual(bridge.verify_runtime_receipt(receipt)["status"], "fail")

    def test_receipt_verifier_missing_is_unverified(self):
        self.assertEqual(bridge.verify_runtime_receipt(self.out / "missing.json")["status"], "unverified")

    def test_missing_process_identity_cannot_pass_dispatch(self):
        self.proof_transform = lambda proof, _: proof.pop("process_identity")
        with self.context(), self.assertRaises(runner.RunnerError) as error:
            self.call()
        self.assertEqual(error.exception.code, "isolation_error")
        self.assertEqual(self.receipt()["status"], "error")
        self.original.assert_not_called()

    def test_missing_identity_receipt_cannot_pass_dispatch(self):
        self.proof_transform = lambda proof, directory: (directory / "process-identity.json").unlink()
        with self.context(), self.assertRaises(runner.RunnerError) as error:
            self.call()
        self.assertEqual(error.exception.code, "isolation_error")
        self.original.assert_not_called()

    def _rewrite_saved_proof(self, receipt_path, change):
        """Rebind outer digests so tests exercise identity semantics, not only SHA checks."""
        directory = receipt_path.parent / "process"
        proof = json.loads((directory / "process.json").read_text())
        change(proof, directory)
        for name in ("process-start.json", "process-identity.json", "stdout.log", "stderr.log"):
            path = directory / name
            proof["evidence"][name] = {"sha256": bridge._sha(path), "bytes": path.stat().st_size}
        (directory / "process.json").write_text(json.dumps(proof))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        for name in receipt["evidence_hashes"]:
            receipt["evidence_hashes"][name] = bridge._sha(receipt_path.parent / name)
        receipt_path.write_text(json.dumps(receipt))

    def test_verifier_rejects_identity_mismatch_even_with_rebound_outer_hashes(self):
        with self.context() as runtime: self.call()
        receipt = runtime.receipts[0]
        self._rewrite_saved_proof(receipt, lambda proof, _: proof["process_identity"].update(pid=999))
        result = bridge.verify_runtime_receipt(receipt)
        self.assertEqual(result["status"], "fail")
        self.assertIn("final identity differs", str(result["errors"]))

    def test_verifier_requires_suspended_status_desktop_and_start_binding(self):
        with self.context() as runtime: self.call()
        receipt = runtime.receipts[0]
        original = (receipt.parent / "process/process-identity.json").read_bytes()
        for key, value in (("status", "running"), ("desktop_path", "WinSta0\\Default"), ("start_sha256", "0" * 64)):
            with self.subTest(key=key):
                def change(_, directory):
                    identity = json.loads(original)
                    identity[key] = value
                    (directory / "process-identity.json").write_text(json.dumps(identity))
                self._rewrite_saved_proof(receipt, change)
                self.assertEqual(bridge.verify_runtime_receipt(receipt)["status"], "fail")

    def test_verifier_rejects_invalid_identity_numbers_and_resume_timing(self):
        with self.context() as runtime: self.call()
        receipt = runtime.receipts[0]
        original = (receipt.parent / "process/process.json").read_bytes()
        cases = [("pid", False), ("primary_thread_id", 0), ("creation_filetime_100ns", -1),
                 ("resumed_at", None), ("resumed_at", "2026-09-19T14:00:02"),
                 ("resumed_at", "2026-09-19T13:59:59+00:00")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                def change(proof, _):
                    proof.clear(); proof.update(json.loads(original))
                    if key == "resumed_at": proof[key] = value
                    else: proof["process_identity"][key] = value
                self._rewrite_saved_proof(receipt, change)
                self.assertEqual(bridge.verify_runtime_receipt(receipt)["status"], "fail")

    def test_verifier_requires_start_command_binding_and_identity_evidence_entry(self):
        with self.context() as runtime: self.call()
        receipt = runtime.receipts[0]
        def change(_, directory):
            start = json.loads((directory / "process-start.json").read_text())
            start["argv"] = ["different.exe"]
            (directory / "process-start.json").write_text(json.dumps(start))
            identity = json.loads((directory / "process-identity.json").read_text())
            identity["start_sha256"] = bridge._sha(directory / "process-start.json")
            (directory / "process-identity.json").write_text(json.dumps(identity))
        self._rewrite_saved_proof(receipt, change)
        result = bridge.verify_runtime_receipt(receipt)
        self.assertEqual(result["status"], "fail")
        self.assertIn("start differs: argv", str(result["errors"]))

    def test_verifier_requires_identity_in_bridge_evidence_map(self):
        with self.context() as runtime: self.call()
        path = runtime.receipts[0]
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["evidence_hashes"].pop("process/process-identity.json")
        path.write_text(json.dumps(receipt))
        result = bridge.verify_runtime_receipt(path)
        self.assertEqual(result["status"], "fail")
        self.assertIn("missing bound runtime evidence", result["errors"])

    def test_exact_headless_import_is_cli(self):
        args = [self.console, "--path", self.project, "--editor", "--headless", "--import"]
        with self.context(): self.assertEqual(self.call(args), 0)
        self.private.assert_not_called()
        self.assertEqual(self.original.call_args.args[0][0], str(self.gui))

    def test_only_fixed_ffmpeg_encoder_is_cli(self):
        args = [str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-framerate", "60",
                "-i", str(self.out / "frames/%06d.png"), "-c:v", "libx264", "-crf", "16", "-pix_fmt",
                "yuv420p", "-movflags", "+faststart", str(self.out / "video.mp4")]
        with self.context(): self.assertEqual(self.call(args, cwd=self.out), 0)
        self.private.assert_not_called()
        with self.context(), self.assertRaises(runner.RunnerError):
            self.call([str(self.ffmpeg), "-f", "sdl", "visible window"])
        self.assertEqual(self.original.call_count, 1)

    def test_version_cache_binds_successful_real_origin(self):
        a, b = self.out / "a/version.log", self.out / "b/version.log"
        with self.context() as runtime:
            self.assertEqual(self.call([self.console, "--version"], log_path=a), 0)
            self.assertEqual(self.call([self.gui, "--version"], log_path=b), 0)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            reused = json.loads(runtime.receipts[-1].read_text())
            self.assertEqual(reused["kind"], "version_cache_reuse")
            self.assertEqual(reused["observed_origin"]["log_sha256"], bridge._sha(a))
            self.assertEqual(reused["observed_origin"]["receipt_sha256"], bridge._sha(runtime.receipts[0]))
        self.assertEqual(self.original.call_count, 1)
        self.private.assert_not_called()

    def test_version_cache_changed_origin_fails_without_relaunch(self):
        a, b = self.out / "a.log", self.out / "b.log"
        with self.context():
            self.call([self.gui, "--version"], log_path=a)
            a.write_bytes(b"changed")
            with self.assertRaises(runner.RunnerError) as error: self.call([self.gui, "--version"], log_path=b)
            self.assertEqual(error.exception.code, "runtime_changed")
        self.assertEqual(self.original.call_count, 1)
        self.assertFalse(b.exists())

    def test_version_failure_is_not_cached(self):
        self.cli_code = 17
        with self.context():
            self.assertEqual(self.call([self.gui, "--version"], log_path=self.out / "a.log"), 17)
            self.cli_code = 0
            self.assertEqual(self.call([self.gui, "--version"], log_path=self.out / "b.log"), 0)
        self.assertEqual(self.original.call_count, 2)

    def test_version_cache_does_not_mask_cancel_or_engine_mutation(self):
        with self.context():
            self.call([self.gui, "--version"], log_path=self.out / "a.log")
            event = threading.Event(); event.set()
            with self.assertRaises(runner.RunnerError) as err: self.call([self.gui, "--version"], log_path=self.out / "b.log", cancel=event)
            self.assertEqual(err.exception.code, "cancelled")
            self.gui.write_bytes(b"replaced engine")
            with self.assertRaises(runner.RunnerError) as err: self.call([self.gui, "--version"], log_path=self.out / "c.log")
            self.assertEqual(err.exception.code, "runtime_changed")
        self.assertEqual(self.original.call_count, 1)

    def test_engine_nonzero_retained_without_normal_desktop_fallback(self):
        self.process_status, self.exit_code, self.owned = "failed", 65, False
        with self.context(): self.assertEqual(self.call(), 65)
        self.assertEqual(self.receipt()["status"], "engine_failed")
        self.original.assert_not_called()

    def test_timeout_and_cancel_are_existing_runner_errors(self):
        for status in ["timeout", "cancelled"]:
            with self.subTest(status=status):
                folder = self.out / status; folder.mkdir()
                cfg = folder / "config.json";cfg.write_text(json.dumps({"output_dir":str(folder)}))
                self.process_status, self.exit_code = status, 0xC000013A
                event = threading.Event()
                with self.context(), self.assertRaises(runner.RunnerError) as err:
                    self.call(log_path=folder / "engine.log", env={**self.env,"KIROCLOPS_CONFIG":str(cfg)}, cancel=event)
                self.assertEqual(err.exception.code, status)
                self.assertIs(self.private.call_args.kwargs["cancel"], event)
                self.assertTrue(Path(err.exception.details["runtime_receipt"]).is_file())
        self.original.assert_not_called()

    def test_private_failure_restores_context_and_keeps_logs_no_fallback(self):
        self.private_error = bridge.private.PrivateDesktopError("win32_error", "Creation failed")
        with self.assertRaises(runner.RunnerError) as err:
            with self.context(): self.call()
        self.assertEqual(err.exception.code, "isolation_error")
        self.assertIs(runner._run_process, self.original)
        self.assertIn("diagnostic", self.log.read_text())
        self.assertEqual(self.receipt()["status"], "error")
        self.original.assert_not_called()

    def test_keyboard_interrupt_preserved_after_receipt_and_restore(self):
        self.private_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            with self.context(): self.call()
        self.assertIs(runner._run_process, self.original)
        self.assertEqual(self.receipt()["error"]["type"], "KeyboardInterrupt")

    def test_source_mutation_is_detected_without_controller_repair(self):
        self.behavior = lambda: (self.project / "player.gd").write_text("mutated during capture")
        with self.context(), self.assertRaises(runner.RunnerError) as err: self.call()
        self.assertEqual(err.exception.code, "source_changed")
        self.assertEqual((self.project / "player.gd").read_text(), "mutated during capture")
        self.assertNotEqual(self.receipt()["source_hashes_before"], self.receipt()["source_hashes_after"])

    def test_config_mutation_is_detected(self):
        self.behavior = lambda: self.config.write_text("{}")
        with self.context(), self.assertRaises(runner.RunnerError) as err: self.call()
        self.assertEqual(err.exception.code, "source_changed")

    def test_expected_source_mismatch_blocks_before_launch(self):
        with self.context(expected_sources={self.project: {}}), self.assertRaises(runner.RunnerError): self.call()
        self.private.assert_not_called(); self.original.assert_not_called()

    def test_input_desktop_or_missing_owned_window_cannot_succeed(self):
        self.owned = False
        with self.context(), self.assertRaises(runner.RunnerError) as err: self.call()
        self.assertEqual(err.exception.code, "isolation_error")
        self.original.assert_not_called()

    def test_received_user_input_fails_closed(self):
        self.input_desktop = True
        with self.context(), self.assertRaises(runner.RunnerError) as err: self.call()
        self.assertEqual(err.exception.code, "isolation_error")

    def test_unknown_gui_flags_do_not_escape_via_headless_substring(self):
        with self.context():
            for args in [self.argv()+["--headless"], [self.gui,"--version","--editor"],
                         [self.root / "other.exe"], self.argv()+["--wid","123"], self.argv()+["--fullscreen"]]:
                with self.subTest(args=args), self.assertRaises(runner.RunnerError): self.call(args)
        self.private.assert_not_called();self.original.assert_not_called()

    def test_project_root_and_config_output_bindings_are_required(self):
        other = self.root / "other";other.mkdir()
        with bridge.isolated_runtime(project_roots=[other]), self.assertRaises(runner.RunnerError):self.call()
        self.config.write_text(json.dumps({"output_dir":str(other)}))
        with self.context(), self.assertRaises(runner.RunnerError):self.call()
        self.private.assert_not_called()

    def test_nested_context_rejected_and_outer_still_routes(self):
        with self.context():
            with self.assertRaises(runner.RunnerError):
                with self.context():pass
            self.assertEqual(self.call(),0)
        self.assertIs(runner._run_process,self.original)

    def test_existing_logs_and_receipts_never_overwritten(self):
        self.log.write_bytes(b"old evidence")
        with self.context(),self.assertRaises(runner.RunnerError):self.call()
        self.assertEqual(self.log.read_bytes(),b"old evidence");self.private.assert_not_called()

    def test_explicit_worker_bootstrap_wraps_only_requested_operation(self):
        with patch("kiroclops.integration.worker.run_job") as worker:
            def work(session,job):
                self.assertIsNot(runner._run_process,self.original)
                self.assertEqual(session,self.project);self.assertEqual(job,"job-123")
                return 7
            worker.side_effect=work
            self.assertEqual(bridge.main(["--session",str(self.project),"--job","job-123"]),7)
        self.assertIs(runner._run_process,self.original)


if __name__ == "__main__":
    unittest.main()
