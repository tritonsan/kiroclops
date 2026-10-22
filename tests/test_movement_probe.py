"""Supplemental oracle/evidence tests; no engine, UI or model is launched."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kiroclops.integration import movement_probe as probe
from kiroclops.integration import background_capture_v1 as background
from kiroclops.integration import runner
from kiroclops.common import read_json, write_json


def recording(kind="air_idle_transition", *, changed_air_velocity=False, normal_level=False):
    frames, runtime, windows = [], [], []
    for index in range(240):
        if kind == "air_idle_transition":
            actions = ["right"] if 32 <= index < 46 else []
            state = "Idle" if index < 32 or index >= 60 else "Fall"
            floor = index < 31 or index >= 60
            rest = index <= 32 or index >= 60
        else:
            actions = (["jump"] if 30 <= index < 54 else []) + (["right"] if 5 <= index < 90 else [])
            state = "Idle" if index < 5 or index >= 110 else "Run" if index < 30 or index >= 78 else "Jump" if index < 58 else "Fall"
            floor = state in {"Idle", "Run"}
            rest = state == "Idle"
        frames.append({"frame_index": index, "physics_tick": 60 + index, "time_s": index / 60, "actions": actions})
        runtime.append({"frame_index": index, "state": state, "on_floor": floor, "horizontal_rest": rest})
        windows.append({"frame_index": index, "focused": False, "no_focus": True, "offscreen": True, "window_position": [-10000, -10000]})
    before = {"frame_index": 32, "state": "Idle", "on_floor": False, "horizontal_rest": True, "actions": ["right"]}
    after = {"frame_index": 32, "state": "Fall", "on_floor": False, "horizontal_velocity_unchanged": not changed_air_velocity}
    return {"schema_version": 1, "protocol_revision": 2, "status": "ok", "probe": kind, "normal_level": normal_level,
            "fps": 60, "viewport": [640, 360], "frame_count": 240, "warmup_ticks": 60, "settled": True,
            "frames": frames, "runtime_frames": runtime, "window_observations": windows,
            "startup_window_observations": [{**windows[0], "frame_index": -1}],
            "platform_removed_after_frame": 29 if kind == "air_idle_transition" else -1,
            "pre_transition": before if kind == "air_idle_transition" else {},
            "post_transition": after if kind == "air_idle_transition" else {}}


class RuntimeOracleTests(unittest.TestCase):
    def evaluate(self, data, kind="air_idle_transition", **kwargs):
        return probe.evaluate_recording(data, probe=kind, **kwargs)

    def test_identical_idle_fall_state_trace_distinguishes_original_and_extra_air_step(self):
        parent = recording()
        bad = recording(changed_air_velocity=True)
        self.assertEqual(parent["runtime_frames"], bad["runtime_frames"])
        self.assertEqual(self.evaluate(parent)["status"], "pass")
        verdict = self.evaluate(bad)
        self.assertEqual(verdict["status"], "fail")
        self.assertTrue(verdict["checks"]["natural_idle_to_fall"])
        self.assertFalse(verdict["checks"]["horizontal_velocity_unchanged"])

    def test_missing_real_airborne_idle_precondition_is_invalid_not_behavioral_failure(self):
        for field, value in (("state", "Run"), ("on_floor", True), ("horizontal_rest", False), ("actions", [])):
            data = recording(); data["pre_transition"][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.evaluate(data)["status"], "probe_invalid")
        data = recording(); data["platform_removed_after_frame"] = 30
        self.assertEqual(self.evaluate(data)["status"], "probe_invalid")

    def test_revision_two_waits_for_natural_contact_update_without_dynamic_retiming(self):
        data = recording()
        self.assertTrue(data["runtime_frames"][30]["on_floor"])
        self.assertFalse(data["runtime_frames"][31]["on_floor"])
        self.assertEqual(data["frames"][31]["actions"], [])
        self.assertEqual(data["frames"][32]["actions"], ["right"])
        self.assertEqual(data["frames"][45]["actions"], ["right"])
        self.assertEqual(data["frames"][46]["actions"], [])
        self.assertEqual(self.evaluate(data)["status"], "pass")
        data["runtime_frames"][31]["on_floor"] = True
        self.assertEqual(self.evaluate(data)["status"], "probe_invalid")
        data = recording(); data["protocol_revision"] = 1
        with self.assertRaises(probe.ProbeError): self.evaluate(data)

    def test_wrong_phase_input_mapping_and_injected_player_telemetry_reject(self):
        for mutate in (lambda data: data["frames"][31].update(physics_tick=92),
                       lambda data: data["frames"][30].update(actions=["right"]),
                       lambda data: data["runtime_frames"][31].update(velocity_x=10.5),
                       lambda data: data.update(player_positions=[0, 1]),
                       lambda data: data["frames"][30].update(time_s=float("nan")),
                       lambda data: data["post_transition"].update(horizontal_velocity_unchanged=1)):
            data = recording(); mutate(data)
            with self.assertRaises(probe.ProbeError): self.evaluate(data)

    def test_config_only_no_focus_cannot_pass_failed_native_window_or_warmup(self):
        for section in ("window_observations", "startup_window_observations"):
            for field, value in (("focused", True), ("no_focus", False), ("window_position", [0, 0]), ("offscreen", False)):
                data = recording(); data[section][0][field] = value
                with self.subTest(section=section, field=field), self.assertRaises(probe.ProbeError):
                    self.evaluate(data)

    def test_short_capture_and_missing_window_monitor_cannot_pass(self):
        for key in ("frames", "runtime_frames", "window_observations", "startup_window_observations"):
            data = recording(); data[key] = data[key][:-1]
            with self.subTest(key=key), self.assertRaises(probe.ProbeError): self.evaluate(data)

    def test_combined_requires_grounded_run_jump_fall_landing_and_final_brake(self):
        good = recording("run_jump_land_brake")
        self.assertEqual(self.evaluate(good, "run_jump_land_brake")["status"], "pass")
        # The old Run/Jump subsequence would pass each of these omissions.
        for first, last, updates in ((58, 78, {"state": "Jump"}),
                                     (78, 90, {"on_floor": False}),
                                     (180, 240, {"horizontal_rest": False}),
                                     (64, 65, {"state": "DoubleJump"})):
            data = deepcopy(good)
            for row in data["runtime_frames"][first:last]: row.update(updates)
            with self.subTest(first=first):
                self.assertEqual(self.evaluate(data, "run_jump_land_brake")["status"], "fail")

    def test_normal_level_identity_is_explicit_not_inferred_from_runtime_states(self):
        data = recording("run_jump_land_brake", normal_level=True)
        self.assertEqual(self.evaluate(data, "run_jump_land_brake", normal_level=True)["status"], "pass")
        with self.assertRaises(probe.ProbeError): self.evaluate(data, "run_jump_land_brake")


class DisposableRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Temp Movement café"
        real_adapter = probe.ROOT / "adapters/movement-probe-godot4"
        for module in (probe, runner, background):
            patched = patch.object(module, "ROOT", self.root); patched.start(); self.addCleanup(patched.stop)
        for name in ("Capture.tscn", "capture.gd"):
            path = self.root / "adapters/movement-probe-godot4" / name
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes((real_adapter / name).read_bytes())
        self.disposable = self.root / "artifacts/probe-copies"
        self.project = self.disposable / "parent"
        for name in runner._REQUIRED_FILES:
            path = self.project / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('[application]\nconfig/name="Disposable game"\n' if name == "project.godot" else "original", encoding="utf-8")
        self.expected = runner.source_hashes(self.project)
        self.output = self.root / "artifacts/probe-result"
        patched = patch.object(runner, "_engine", return_value=self.root / "Godot.exe")
        patched.start(); self.addCleanup(patched.stop)

    def fake_process(self, args, *, cwd, env, log_path, cancel=None, timeout=120):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("Godot synthetic process output\n", encoding="utf-8")
        if "--import" in args:
            (Path(cwd) / "generated.gd.uid").write_text("cache")
            return 0
        config = read_json(env["KIROCLOPS_CONFIG"])
        self.assertEqual((Path(cwd) / "override.cfg").read_bytes(), background.OVERRIDE_BYTES)
        self.assertTrue(Path(env["APPDATA"]).is_relative_to(self.output / "runtime"))
        self.assertNotIn("--headless", args)
        self.assertIn("--fixed-fps", args)
        for index in range(240):
            (self.output / "frames" / f"{index:06d}.png").write_bytes(b"fake native PNG" + str(index).encode())
        write_json(self.output / "recording.json", recording(config["probe"], normal_level=config["normal_level"]))
        return 0

    def capture(self, **kwargs):
        return probe.run_probe(self.project, self.output, disposable_root=self.disposable,
                               expected_source_hashes=self.expected, **kwargs)

    def test_startup_installed_before_engine_and_original_files_preserved_and_evidence_replays(self):
        with patch.object(runner, "_run_process", side_effect=self.fake_process) as process:
            run = self.capture()
        self.assertEqual(process.call_count, 2)
        for name, digest in self.expected.items():
            if name != "project.godot": self.assertEqual(probe.sha256(self.project / name), digest)
        original_settings = bytes.fromhex(read_json(run / "probe.json")["startup"]["settings_before_hex"])
        self.assertEqual((self.project / "project.godot").read_bytes(), background._autoload(original_settings))
        receipt = read_json(run / "probe.json")
        self.assertEqual(receipt["full_source_hashes_before"], receipt["full_source_hashes_after"])
        self.assertFalse(receipt["canonical_experiment_acceptance"])
        self.assertEqual(len(receipt["evidence_sha256"]), 243)
        self.assertEqual(probe.verify_probe(self.project, run, expected_source_hashes=self.expected)["status"], "pass")

    def test_mutated_png_or_controller_invalidates_replay(self):
        with patch.object(runner, "_run_process", side_effect=self.fake_process): self.capture()
        png = self.output / "frames/000031.png"; original = png.read_bytes(); png.write_bytes(b"changed")
        self.assertEqual(probe.verify_probe(self.project, self.output)["status"], "fail")
        png.write_bytes(original)
        (self.project / "source/characters/player/player.gd").write_text("changed")
        self.assertEqual(probe.verify_probe(self.project, self.output)["status"], "fail")

    def test_source_changed_during_capture_retains_failure_and_cannot_emit_receipt(self):
        def mutate(*args, **kwargs):
            result = self.fake_process(*args, **kwargs)
            if "--import" not in args[0]: (self.project / "source/characters/player/player.gd").write_text("changed")
            return result
        with patch.object(runner, "_run_process", side_effect=mutate), self.assertRaises(probe.ProbeError): self.capture()
        self.assertFalse((self.output / "probe.json").exists())
        self.assertEqual(read_json(self.output / "failure.json")["code"], "source_changed")

    def test_rejects_old_session_target_and_wrong_expected_bytes_before_engine(self):
        with patch.object(runner, "_run_process") as engine:
            original = self.expected; self.expected = {"wrong": "hash"}
            with self.assertRaises(probe.ProbeError): self.capture()
            self.expected = original
            with self.assertRaises(probe.ProbeError):
                probe.run_probe(self.root / "sessions/old/target", self.output, disposable_root=self.root / "sessions",
                                expected_source_hashes=self.expected)
            engine.assert_not_called()

    def test_requires_no_focus_offscreen_and_fresh_output(self):
        with patch.object(runner, "_run_process") as engine:
            for kwargs in ({"no_focus": False}, {"offscreen": False}, {"normal_level": True}):
                with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): self.capture(**kwargs)
            self.output.mkdir(parents=True); (self.output / "prior.txt").write_text("keep")
            with self.assertRaises(probe.ProbeError): self.capture()
            self.assertEqual((self.output / "prior.txt").read_text(), "keep")
            engine.assert_not_called()

    def test_existing_exact_background_bundle_is_supported_without_rewriting_it(self):
        background.install_disposable_project(self.project, disposable_root=self.disposable, expected_source_hashes=self.expected)
        self.expected = runner.source_hashes(self.project)
        with patch.object(runner, "_run_process", side_effect=self.fake_process): self.capture()
        self.assertTrue(read_json(self.output / "probe.json")["startup"]["already_present"])

    def test_incomplete_background_bundle_is_rejected_before_engine(self):
        (self.project / "override.cfg").write_bytes(background.OVERRIDE_BYTES)
        self.expected = runner.source_hashes(self.project)
        with patch.object(runner, "_run_process") as engine, self.assertRaises(RuntimeError): self.capture()
        engine.assert_not_called()

    def test_cancelled_before_run_has_no_side_effects_and_missing_proof_is_unverified(self):
        event = threading.Event(); event.set()
        with patch.object(runner, "_run_process") as engine, self.assertRaises(probe.ProbeError): self.capture(cancel=event)
        engine.assert_not_called(); self.assertFalse(self.output.exists())
        self.assertEqual(runner.source_hashes(self.project), self.expected)
        self.assertEqual(probe.verify_probe(self.project, self.output)["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
