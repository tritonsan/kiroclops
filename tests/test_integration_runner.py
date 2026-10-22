"""Phase 2 runner contracts, using fake game processes rather than Godot."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kiroclops.common import read_json, write_json
from kiroclops.integration import runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "sessions/session/target"
        self.project.mkdir(parents=True)
        self.root_patch = patch.object(runner, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        for name in runner._REQUIRED_FILES:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('[application]\nconfig/name="Slay"\n' if name == "project.godot" else "original", encoding="utf-8")
        for name in ("capture.gd", "Capture.tscn"):
            path = self.root / "adapters/godot4" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("frozen-" + name, encoding="utf-8")
        write_json(self.root / "config/sources.json", {"slay": {"commit": "pinned-source", "controller": "source/characters/player/player.gd"}})
        runner.prepare_project(self.project)
        self.engine = patch.object(runner, "engine_path", return_value=self.root / "godot.exe")
        self.engine.start()
        self.addCleanup(self.engine.stop)
        self.output = self.root / "runs/candidate"

    def fake_process(self, args, *, cwd, env, log_path, cancel=None, timeout=120):
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if "--version" in args:
            log_path.write_text("4.5.1.stable.official", encoding="utf-8")
            return 0
        if "--import" in args:
            log_path.write_text("Import completed", encoding="utf-8")
            (self.project / ".godot").mkdir(exist_ok=True)
            (self.project / ".godot/cache.bin").write_bytes(b"cache")
            (self.project / "new.gd.uid").write_text("uid", encoding="utf-8")
            return 0
        config = read_json(env["KIROCLOPS_CONFIG"])
        run = Path(config["output_dir"])
        active = set()
        frames = []
        for i in range(240):
            if i == config["start_tick"]:
                active.add("ui_up")
            if i == config["start_tick"] + config["hold_ticks"]:
                active.discard("ui_up")
            for event in config.get("extra_inputs", []):
                if event["tick"] == i:
                    (active.add if event["pressed"] else active.discard)(event["action"])
            frames.append({"frame_index": i, "physics_tick": 60+i, "time_s": i/60, "actions": sorted(active)})
            (run / f"frames/{i:06d}.png").write_bytes(b"fake PNG")
        write_json(run / "recording.json", {"status": "ok", "fps": 60, "viewport": [640, 360],
                                           "frame_count": 240, "frames": frames, "settled": True})
        log_path.write_text("ERROR: Failed to read the root certificate store.\n"
                            "KIROCLOPS_STATE physics_tick=91 state=Jump\n"
                            "KIROCLOPS_STATE physics_tick=111 state=Fall\n"
                            "KIROCLOPS_STATE physics_tick=135 state=Idle\n", encoding="utf-8")
        return 0

    def test_prepare_idempotent_and_adapter_frozen(self):
        before = runner.source_hashes(self.project)
        runner.prepare_project(self.project)
        self.assertEqual(before, runner.source_hashes(self.project))
        self.assertIn(runner._CUSTOM_DIR, (self.project / "project.godot").read_text())
        (self.project / "kiroclops_capture/capture.gd").write_text("edited")
        with self.assertRaises(runner.RunnerError) as ctx:
            runner.capture_project(self.project, self.output, hold_ticks=3, video=False)
        self.assertEqual(ctx.exception.code, "fixture_changed")

    def test_source_hash_includes_new_helpers_scenes_assets_and_config_not_caches(self):
        before = runner.source_hashes(self.project)
        for name in ("source/helper.gd", "source/helper.tscn", "source/helper.tres", "source/config.json", "assets/sprite.png"):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"content")
        for name in (".godot/imported/cache.bin", ".kiro/settings/mcp.json", "source/helper.gd.uid", "assets/sprite.png.import"):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"cache")
        added = runner.source_hashes(self.project).keys() - before.keys()
        self.assertEqual(added, {"source/helper.gd", "source/helper.tscn", "source/helper.tres", "source/config.json", "assets/sprite.png"})

    def test_valid_capture_has_compatible_manifest_and_full_before_after_hashes(self):
        with patch.object(runner, "_run_process", side_effect=self.fake_process) as process:
            result = runner.capture_project(self.project, self.output, hold_ticks=24, video=False)
        self.assertEqual(result, self.output)
        manifest = read_json(result / "manifest.json")
        self.assertEqual(manifest["status"], "ok")
        self.assertEqual(manifest["scenario"]["split"], "calibration")
        self.assertEqual(manifest["frames"][30]["actions"], ["ui_up"])
        provenance = manifest["provenance"]
        self.assertEqual(provenance["full_source_hashes_before"], provenance["full_source_hashes_after"])
        self.assertEqual(provenance["source_digest_before"], provenance["source_digest_after"])
        self.assertTrue(provenance["controller_hashes"])
        self.assertEqual(process.call_count, 2)
        env = process.call_args_list[0].kwargs["env"]
        self.assertTrue(Path(env["APPDATA"]).is_relative_to(self.root / ".runtime/phase2"))
        self.assertNotIn(str(self.root / "work"), str(process.call_args_list))

    def test_source_changes_during_capture_are_rejected_and_retained_as_failure(self):
        def changed(*args, **kwargs):
            code = self.fake_process(*args, **kwargs)
            (self.project / "source/new_helper.gd").write_text("changed")
            return code
        with patch.object(runner, "_run_process", side_effect=changed):
            with self.assertRaises(runner.RunnerError) as ctx:
                runner.capture_project(self.project, self.output, hold_ticks=3, video=False)
        self.assertEqual(ctx.exception.code, "source_changed")
        failure = read_json(self.output / "failure.json")
        self.assertEqual(failure["code"], "source_changed")
        self.assertNotEqual(failure["full_source_hashes_before"], failure["full_source_hashes_after"])
        self.assertFalse((self.output / "manifest.json").exists())

    def test_import_preserves_sources_and_isolates_supplied_runtime(self):
        before = runner.source_hashes(self.project)
        runtime = self.root / "runtime/import-one"
        with patch.object(runner, "_run_process", side_effect=self.fake_process) as process:
            runner.import_project(self.project, runtime_dir=runtime)
        self.assertEqual(before, runner.source_hashes(self.project))
        self.assertEqual(process.call_args.kwargs["env"]["APPDATA"], str(runtime / "appdata"))
        self.assertEqual(process.call_args.kwargs["log_path"], runtime / "import.log")

    def test_precancelled_job_never_starts_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(runner.subprocess, "Popen") as process:
            for action in (
                lambda: runner.capture_project(self.project, self.output, hold_ticks=3, cancel=cancel),
                lambda: runner.import_project(self.project, cancel=cancel),
                lambda: runner._run_process(["fake"], cwd=self.root, env={}, log_path=self.root / "process.log", cancel=cancel),
            ):
                with self.assertRaises(runner.RunnerError) as ctx:
                    action()
                self.assertEqual(ctx.exception.code, "cancelled")
            process.assert_not_called()

    def test_owned_process_cleanup_runs_on_cancellation_and_timeout(self):
        for kind in ("cancelled", "timeout"):
            cancel = threading.Event()
            proc = Mock()
            proc.poll.return_value = None
            def start(*args, **kwargs):
                if kind == "cancelled":
                    cancel.set()
                return proc
            with self.subTest(kind=kind), patch.object(runner.subprocess, "Popen", side_effect=start), \
                    patch.object(runner, "_terminate_owned_tree") as terminate:
                with self.assertRaises(runner.RunnerError) as ctx:
                    runner._run_process(["fake"], cwd=self.root, env={}, log_path=self.root / f"{kind}.log",
                                        cancel=cancel, timeout=.001)
                self.assertEqual(ctx.exception.code, kind)
                terminate.assert_called_once_with(proc)

    def test_windows_cleanup_targets_only_owned_pid_and_tree(self):
        if os.name != "nt":
            self.skipTest("Windows taskkill contract")
        proc = Mock(pid=43210)
        proc.poll.side_effect = [None, 0]
        with patch.object(runner.subprocess, "run") as command:
            runner._terminate_owned_tree(proc)
        self.assertEqual(command.call_args.args[0], ["taskkill", "/PID", "43210", "/T", "/F"])
        self.assertEqual(command.call_args.kwargs["creationflags"], runner.subprocess.CREATE_NO_WINDOW)

    def test_lock_serializes_threads_and_wait_is_cancellable(self):
        cancel = threading.Event()
        errors = []
        entered = threading.Event()
        def attempt():
            try:
                with runner._godot_lock(cancel=cancel, timeout=2):
                    entered.set()
            except runner.RunnerError as exc:
                errors.append(exc.code)
        with runner._godot_lock(timeout=2):
            thread = threading.Thread(target=attempt)
            thread.start()
            self.assertFalse(entered.wait(.1))
            cancel.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        with runner._godot_lock(timeout=1):
            pass

    def test_lock_also_serializes_independent_processes(self):
        code = (
            "from pathlib import Path\n"
            "import sys\n"
            "from kiroclops.integration import runner\n"
            "runner.ROOT = Path(sys.argv[1])\n"
            "try:\n"
            "    with runner._godot_lock(timeout=.2):\n"
            "        print('acquired')\n"
            "except runner.RunnerError as exc:\n"
            "    print(exc.code)\n"
        )
        with runner._godot_lock(timeout=2):
            result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                    capture_output=True, text=True, timeout=5, **runner.process_options())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "lock_timeout")
        result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                capture_output=True, text=True, timeout=5, **runner.process_options())
        self.assertEqual(result.stdout.strip(), "acquired")

    def test_error_classification_for_script_failure_missing_recording_and_bad_actions(self):
        for kind in ("script", "recording", "actions"):
            def broken(*args, **kwargs):
                code = self.fake_process(*args, **kwargs)
                if kind == "script":
                    Path(kwargs["log_path"]).write_text("SCRIPT ERROR: Parse Error: bad token")
                elif kind == "recording":
                    (Path(read_json(kwargs["env"]["KIROCLOPS_CONFIG"])["output_dir"]) / "recording.json").unlink()
                else:
                    run = Path(read_json(kwargs["env"]["KIROCLOPS_CONFIG"])["output_dir"])
                    recording = read_json(run / "recording.json")
                    recording["frames"][1]["actions"] = ["ui_right"]
                    write_json(run / "recording.json", recording)
                return code
            with self.subTest(kind=kind), patch.object(runner, "_run_process", side_effect=broken):
                with self.assertRaises(runner.RunnerError) as ctx:
                    runner.capture_project(self.project, self.root / f"runs/{kind}", hold_ticks=3, video=False)
                self.assertEqual(ctx.exception.code, "engine_error" if kind == "script" else "recording_error")

    def test_invalid_controls_existing_output_and_protected_source_rejected(self):
        for extras in ({"hold_ticks": 99}, {"unknown": True}, {"extra_inputs": None}, {"smoke_wall": 1},
                       {"extra_inputs": [{"tick": 1, "action": "bogus", "pressed": True}]}):
            with self.subTest(extras=extras), self.assertRaises(runner.RunnerError) as ctx:
                runner.capture_project(self.project, self.output, hold_ticks=3, extra_config=extras, video=False)
            self.assertEqual(ctx.exception.code, "invalid_config")
        self.output.mkdir(parents=True)
        (self.output / "existing.txt").write_text("evidence")
        with self.assertRaises(runner.RunnerError) as ctx:
            runner.capture_project(self.project, self.output, hold_ticks=3)
        self.assertEqual(ctx.exception.code, "output_exists")
        with self.assertRaises(runner.RunnerError) as ctx:
            runner.capture_project(self.project, self.project / "bad-output", hold_ticks=3)
        self.assertEqual(ctx.exception.code, "invalid_path")
        protected = self.root / "vendor/slay"
        for name in runner._REQUIRED_FILES:
            path = protected / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("pinned source")
        with self.assertRaises(runner.RunnerError) as ctx:
            runner.prepare_project(protected)
        self.assertEqual(ctx.exception.code, "protected_project")

    def test_smoke_checks_real_state_sequences_and_same_controller(self):
        traces = {
            "normal": ["Jump", "Fall", "Idle"], "ground": ["Jump", "Fall", "Idle"],
            "walk-double": ["Run", "Jump", "DoubleJump", "Fall", "Idle"],
            "wall": ["Run", "Jump", "Fall", "WallJump", "Jump", "Fall", "Idle"],
        }
        def fake_capture(project, output, **kwargs):
            output.mkdir(parents=True)
            write_json(output / "manifest.json", {"provenance": {"source_digest_before": "same-source"}})
            (output / "engine.log").write_text("\n".join(
                f"KIROCLOPS_STATE physics_tick={70+i} state={state}" for i, state in enumerate(traces[output.name])))
            return output
        with patch.object(runner, "capture_project", side_effect=fake_capture) as capture:
            result = runner.smoke_project(self.project, self.root / "smoke")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(capture.call_count, 4)
        self.assertTrue(capture.call_args_list[0].kwargs["normal_level"])
        traces["wall"] = ["Run", "Jump", "Fall", "Idle"]
        with patch.object(runner, "capture_project", side_effect=fake_capture):
            result = runner.smoke_project(self.project, self.root / "smoke-fail")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["runs"]["wall"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
