"""Contact-v2 semantic and evidence regressions; no real engine or model."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import normal_gameplay_v2 as probe
from kiroclops.integration import movement_probe as original
from kiroclops.integration import background_capture_v1 as background
from kiroclops.integration import runner
from kiroclops.common import read_json, write_json


def observation(state="Idle", *, floor=True, wall=False, rest=True, up=False, down=False):
    return {"state": state, "on_floor": floor, "on_wall": wall, "horizontal_rest": rest,
            "moving_up": up, "moving_down": down}


def recording(*, slide=False):
    frames, runtime, windows = [], [], []
    previous = observation()
    for i in range(240):
        if i < 5 or i >= 110:
            post = observation()
        elif i < 30 or i >= 78:
            post = observation("Run", rest=False)
        elif i < 58:
            post = observation("Jump", floor=False, rest=False, up=True)
        else:
            post = observation("Fall", floor=False, rest=False, down=True)
        if slide and 64 <= i < 74:
            post = observation("WallJump", floor=False, wall=True, rest=False, down=True)
        runtime.append({"frame_index": i, "pre": deepcopy(previous), "post": post, "jump_just_pressed": i == 30})
        previous = post
        frames.append({"frame_index": i, "physics_tick": 60+i, "time_s": i/60,
                       "actions": (["jump"] if 30 <= i < 54 else []) + (["right"] if 5 <= i < 90 else [])})
        windows.append({"frame_index": i, "focused": False, "no_focus": True, "offscreen": True,
                        "window_position": [-10000, -10000]})
    return {"schema_version": 1, "protocol_revision": 3, "status": "ok", "probe": "run_jump_land_brake",
            "normal_level": True, "fps": 60, "viewport": [640, 360], "frame_count": 240, "warmup_ticks": 60,
            "settled": True, "frames": frames, "runtime_frames": runtime, "window_observations": windows,
            "startup_window_observations": [{**windows[0], "frame_index": -1}],
            "platform_removed_after_frame": -1, "pre_transition": {}, "post_transition": {}}


class ContactDecisionTests(unittest.TestCase):
    def test_normal_ground_jump_landing_braking_pass(self):
        result = probe.evaluate_recording(recording())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(result["checks"]), 10)

    def test_contact_supported_slide_passes_but_original_blanket_rule_still_fails(self):
        data = recording(slide=True)
        result = probe.evaluate_recording(data)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["contact_supported_wall_slide_frames"], list(range(64, 74)))
        old_view = {**data, "protocol_revision": 2, "runtime_frames": [{"frame_index": row["frame_index"],
            **{k: row["post"][k] for k in ("state", "on_floor", "horizontal_rest")}} for row in data["runtime_frames"]]}
        self.assertEqual(original.evaluate_recording(old_view, probe="run_jump_land_brake", normal_level=True)["status"], "fail")

    def test_wall_state_without_contact_is_not_excused(self):
        data = recording(slide=True)
        for phase in ("pre", "post"):
            data["runtime_frames"][67][phase]["on_wall"] = False
        result = probe.evaluate_recording(data)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["violations"]["wall_slide_contact"], [67])

    def test_unrequested_wall_launch_fails_even_with_real_contact(self):
        data = recording(slide=True)
        data["runtime_frames"][67]["post"] = observation("Jump", floor=False, rest=False, up=True)
        result = probe.evaluate_recording(data)
        self.assertEqual(result["status"], "fail")
        self.assertIn(67, result["violations"]["jump_entry_requires_requested_contact"])
        self.assertIn(67, result["violations"]["new_ascent_requires_requested_contact"])

    def test_post_move_landing_may_keep_slide_label_for_exactly_one_update(self):
        data = recording(slide=True)
        data["runtime_frames"][77]["pre"] = observation("WallJump", floor=False, wall=True, rest=False, down=True)
        data["runtime_frames"][77]["post"] = observation("WallJump", floor=True, wall=True, rest=False)
        data["runtime_frames"][78]["pre"] = deepcopy(data["runtime_frames"][77]["post"])
        self.assertEqual(probe.evaluate_recording(data)["status"], "pass")
        # The same state remaining grounded through a further update is not
        # excused by the asynchronous contact/state observation envelope.
        data["runtime_frames"][78]["post"] = deepcopy(data["runtime_frames"][77]["post"])
        result = probe.evaluate_recording(data)
        self.assertEqual(result["status"], "fail")
        self.assertIn(77, result["violations"]["wall_slide_contact"])
        self.assertIn(78, result["violations"]["wall_slide_contact"])

    def test_unrequested_lift_cannot_hide_under_unchanged_fall_state(self):
        data = recording()
        data["runtime_frames"][65]["post"].update(moving_up=True, moving_down=False)
        result = probe.evaluate_recording(data)
        self.assertFalse(result["checks"]["new_ascent_requires_requested_contact"])

    def test_continued_held_ascent_is_not_an_extra_impulse(self):
        # No velocity magnitude is exported; repeated held-jump resets are not
        # incorrectly inferred to be new jumps from continued upward motion.
        data = recording()
        self.assertTrue(all(row["pre"]["moving_up"] and row["post"]["moving_up"] for row in data["runtime_frames"][31:54]))
        self.assertEqual(probe.evaluate_recording(data)["status"], "pass")

    def test_jump_label_requires_actual_precontact(self):
        data = recording()
        data["runtime_frames"][30]["pre"]["on_floor"] = False
        self.assertEqual(probe.evaluate_recording(data)["status"], "fail")

    def test_double_jump_hit_and_appear_remain_failures(self):
        for state in ("DoubleJump", "Hit", "Appear"):
            data = recording(); data["runtime_frames"][65]["post"]["state"] = state
            with self.subTest(state=state):
                self.assertFalse(probe.evaluate_recording(data)["checks"]["no_unrequested_other_special_state"])

    def test_no_new_native_edge_and_no_telemetry_or_incoherent_signs(self):
        for mutate in (lambda d: d["runtime_frames"][66].update(jump_just_pressed=True),
                       lambda d: d["runtime_frames"][30]["post"].update(velocity_y=-300),
                       lambda d: d["runtime_frames"][30]["post"].update(moving_down=True),
                       lambda d: d["runtime_frames"][30]["pre"].update(on_wall=0)):
            data = recording(); mutate(data)
            with self.assertRaises(probe.ProbeError): probe.evaluate_recording(data)

    def test_old_missing_contact_wrong_inputs_and_window_focus_reject(self):
        for mutate in (lambda d: d.update(protocol_revision=2),
                       lambda d: d["runtime_frames"][30].pop("pre"),
                       lambda d: d["frames"][30].update(actions=["right"]),
                       lambda d: d["startup_window_observations"][0].update(focused=True)):
            data = recording(); mutate(data)
            with self.assertRaises(probe.ProbeError): probe.evaluate_recording(data)

    def test_positive_landing_brake_and_rest_checks_remain_required(self):
        data = recording()
        for row in data["runtime_frames"][180:]: row["post"]["horizontal_rest"] = False
        result = probe.evaluate_recording(data)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["checks"]["final_grounded_rest"])

    def test_normal_level_and_preregistered_record_are_explicit_and_fresh(self):
        with self.assertRaises(probe.ProbeError): probe.evaluate_recording(recording(), normal_level=False)
        first = probe.protocol_record(); first["events"][0]["tick"] = 99
        self.assertEqual(probe.protocol_record()["events"][0]["tick"], 5)
        self.assertFalse(first["canonical_experiment_acceptance"])


class DisposableEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "contact-v2"
        actual_adapter = probe.ROOT / "adapters/normal-gameplay-v2-godot4"
        for module in (probe, runner, background):
            patched = patch.object(module, "ROOT", self.root); patched.start(); self.addCleanup(patched.stop)
        for name in ("Capture.tscn", "capture.gd"):
            path = self.root / "adapters/normal-gameplay-v2-godot4" / name
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes((actual_adapter / name).read_bytes())
        self.disposable = self.root / "artifacts/copies"
        self.project = self.disposable / "selected"
        for name in runner._REQUIRED_FILES:
            path = self.project / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('[application]\nconfig/name="Synthetic"\n' if name == "project.godot" else "original", encoding="utf-8")
        self.expected = runner.source_hashes(self.project)
        self.output = self.root / "artifacts/output"
        patched = patch.object(runner, "_engine", return_value=self.root / "Godot.exe")
        patched.start(); self.addCleanup(patched.stop)

    def fake_process(self, args, *, cwd, env, log_path, cancel=None, timeout=120):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("Synthetic Godot output\n", encoding="utf-8")
        if "--import" in args: return 0
        self.assertIn(f"res://{probe.ADAPTER_DIR}/Capture.tscn", args)
        for index in range(240):
            (self.output / "frames" / f"{index:06d}.png").write_bytes(b"synthetic-png-" + str(index).encode())
        write_json(self.output / "recording.json", recording(slide=True))
        return 0

    def capture(self):
        return probe.run_probe(self.project, self.output, disposable_root=self.disposable,
                               expected_source_hashes=self.expected)

    def test_new_adapter_and_protocol_bind_fresh_raw_evidence(self):
        with patch.object(runner, "_run_process", side_effect=self.fake_process) as process:
            run = self.capture()
        self.assertEqual(process.call_count, 2)
        receipt = read_json(run / "probe.json")
        self.assertEqual(receipt["protocol"], probe.protocol_record())
        self.assertEqual(receipt["full_source_hashes_before"], receipt["full_source_hashes_after"])
        self.assertEqual(probe.verify_probe(self.project, run, expected_source_hashes=self.expected)["status"], "pass")
        for name, digest in self.expected.items():
            if name != "project.godot": self.assertEqual(probe.sha256(self.project / name), digest)

    def test_raw_or_protocol_mutation_fails_replay(self):
        with patch.object(runner, "_run_process", side_effect=self.fake_process): self.capture()
        png = self.output / "frames/000064.png"; data = png.read_bytes(); png.write_bytes(b"different")
        self.assertEqual(probe.verify_probe(self.project, self.output)["status"], "fail")
        png.write_bytes(data)
        receipt_path = self.output / "probe.json"; receipt = read_json(receipt_path)
        receipt["protocol"]["events"][0]["tick"] = 6; write_json(receipt_path, receipt)
        self.assertEqual(probe.verify_probe(self.project, self.output)["status"], "fail")

    def test_session_source_and_non_normal_config_reject_before_process(self):
        with patch.object(runner, "_run_process") as engine:
            with self.assertRaises(probe.ProbeError):
                probe.run_probe(self.root / "sessions/old/target", self.output,
                    disposable_root=self.root / "sessions", expected_source_hashes=self.expected)
            with self.assertRaises(probe.ProbeError):
                probe.run_probe(self.project, self.output, disposable_root=self.disposable,
                    expected_source_hashes=self.expected, normal_level=False)
            engine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
