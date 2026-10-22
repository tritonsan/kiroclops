"""Versioned combined-state contract; no engine, GUI or model is launched."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.portable_v2 import integrity, wall_coyote as module
from kiroclops.integration.storage import project_hashes, sha256, write_json


def recording():
    cases, frames = [], []
    for spec in module.case_specs():
        observations = []
        side = 1 if spec["side"] == "right" else -1
        for step in range(1, 60):
            snapshots = {}
            for phase in ("pre", "post"):
                actual = step - 1 if phase == "pre" else step
                floor = spec["kind"] == "overlap" and actual < 32
                wall = (spec["kind"] == "wall_only" or actual >= 34) and actual < 35
                snapshots[phase] = {"position": [160.0, 144.0], "velocity": [0.0, -250.0 if actual >= 35 else 0.0],
                    "floor": floor, "wall_only": wall and not floor, "wall": wall,
                    "wall_normal": [-1.0 * side, 0.0] if wall else [0.0, 0.0],
                    "coyote_seconds": 0.05 if spec["kind"] == "overlap" and 32 <= actual < 35 else 0.0,
                    "wall_grace_seconds": 0.0, "air_jump": True, "jump_edge": step == 35}
            observations.append({"step": step, **snapshots})
        cases.append({"id": spec["id"], "kind": spec["kind"], "side": spec["side"], "press": 35, "steps": observations})
        for step in range(60):
            n = spec["start"] + step
            frames.append({"frame": n, "tick": n + 60, "case_id": spec["id"], "step": step,
                           "actions": module._actions(spec, step, 35)})
    return {"schema_version": 1, "protocol": module.VERSION, "status": "ok", "error": "", "fps": 60,
            "warmup": 60, "viewport": [320, 180], "frame_count": 240, "frames": frames, "cases": cases}


class WallCoyoteTests(unittest.TestCase):
    def test_both_stages_valid_controls_and_overlap(self):
        for stage in ("jump", "ground"):
            data = recording()
            self.assertEqual(module.validate_recording(data)["status"], "pass")
            result = module.compare_recordings(data, deepcopy(data), stage)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["case_count"], 4)

    def test_overwritten_wall_impulse_rejected_both_directions_and_stages(self):
        for stage in ("jump", "ground"):
            data = recording()
            for case in data["cases"][:2]:
                case["steps"][34]["post"]["velocity"][1] = -195.0
            result = module.compare_recordings(recording(), data, stage)
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["failed_cases"], 2)
            self.assertEqual({m["check"] for m in result["mismatches"]}, {"native_wall_response_preserved"})

    def test_launch_and_all_four_following_steps_are_checked(self):
        for n in range(34, 39):
            data = recording()
            data["cases"][0]["steps"][n]["post"]["velocity"][0] = 5
            self.assertEqual(module.compare_recordings(recording(), data, "ground")["status"], "fail")
        data = recording(); data["cases"][0]["steps"][39]["post"]["velocity"][0] = 5
        self.assertEqual(module.compare_recordings(recording(), data, "ground")["status"], "pass")

    def test_rights_are_preserved_even_with_equal_motion(self):
        data = recording(); data["cases"][0]["steps"][36]["post"]["air_jump"] = False
        self.assertEqual(module.compare_recordings(recording(), data, "ground")["status"], "fail")

    def test_jump_requires_identical_pre_state(self):
        data = recording(); data["cases"][0]["steps"][34]["pre"]["position"][0] += 1
        result = module.compare_recordings(recording(), data, "jump")
        self.assertEqual(result["status"], "fail")
        self.assertIn("same_required_pre_jump_context", {m["check"] for m in result["mismatches"]})

    def test_ground_approach_position_and_timer_may_differ(self):
        data = recording()
        for case in data["cases"][:2]:
            case["steps"][34]["pre"]["position"][1] += 2
            case["steps"][34]["pre"]["coyote_seconds"] = 0.02
            for sample in case["steps"][34:39]:
                sample["post"]["position"][1] += 2
        self.assertEqual(module.compare_recordings(recording(), data, "ground")["status"], "pass")
        self.assertEqual(module.compare_recordings(recording(), data, "jump")["status"], "fail")

    def test_ground_controls_still_require_exact_pre_state(self):
        data = recording(); data["cases"][2]["steps"][34]["pre"]["position"][1] += 1
        self.assertEqual(module.compare_recordings(recording(), data, "ground")["status"], "fail")

    def test_ground_approach_cannot_change_wall_context_or_rights(self):
        for field, value in (("air_jump", False), ("wall_normal", [0.0, 1.0])):
            data = recording(); data["cases"][0]["steps"][34]["pre"][field] = value
            self.assertEqual(module.compare_recordings(recording(), data, "ground")["status"], "fail")

    def test_missing_context_is_candidate_failure_but_baseline_error(self):
        data = recording()
        data["cases"][0]["steps"][34]["pre"]["coyote_seconds"] = 0
        result = module.compare_recordings(recording(), data, "jump")
        self.assertEqual(result["status"], "fail")
        self.assertIn("live_coyote_at_jump_edge", {m["check"] for m in result["mismatches"]})
        self.assertEqual(module.compare_recordings(data, recording(), "jump")["status"], "error")

    def test_never_reached_context_is_behavior_failure(self):
        data = recording(); target = data["cases"][0]; target["press"] = -1
        for obs in target["steps"]:
            for phase in ("pre", "post"):
                obs[phase]["wall"] = obs[phase]["wall_only"] = False
        spec = module.case_specs()[0]
        for step in range(60): data["frames"][step]["actions"] = module._actions(spec, step, -1)
        result = module.compare_recordings(recording(), data, "ground")
        self.assertEqual(result["status"], "fail")
        self.assertIn("input_reaches_required_context", {m["check"] for m in result["mismatches"]})

    def test_trigger_must_be_first_context_and_actions_bound(self):
        for edit in (lambda d: d["cases"][0].update(press=36),
                     lambda d: d["cases"][0]["steps"][32]["post"].update(wall_only=True),
                     lambda d: d["frames"][35].update(actions=[])):
            data = recording(); edit(data)
            with self.assertRaises(ValueError): module.validate_recording(data)

    def test_missing_floor_loss_and_invalid_wall_only_control_rejected(self):
        data = recording()
        for obs in data["cases"][0]["steps"]:
            obs["pre"]["floor"] = obs["post"]["floor"] = False
        self.assertEqual(module.validate_recording(data)["status"], "fail")
        data = recording(); data["cases"][2]["steps"][2]["post"]["floor"] = True
        self.assertEqual(module.validate_recording(data)["status"], "fail")

    def test_broken_clock_missing_steps_nonfinite_and_schema_rejected(self):
        for edit in (lambda d: d["frames"].pop(), lambda d: d["frames"][1].update(tick=60),
                     lambda d: d["frames"][1].update(step=True), lambda d: d["cases"][0]["steps"].pop(),
                     lambda d: d["cases"][0]["steps"][0]["pre"].update(position=[float("nan"), 0]),
                     lambda d: d["cases"][0]["steps"][0]["pre"].update(floor=1),
                     lambda d: d["cases"][0]["steps"][0]["pre"].update(extra=True)):
            data = recording(); edit(data)
            with self.assertRaises(ValueError): module.validate_recording(data)

    def test_compact_feedback_does_not_reveal_native_values_or_response_digests(self):
        data = recording(); data["cases"][0]["steps"][34]["post"]["velocity"][1] = -195.12345
        result = module.compare_recordings(recording(), data, "jump")
        text = json.dumps(result)
        for forbidden in ("-195", "-250", "position", "coyote_seconds", "velocity", "timer", "player.gd"):
            self.assertNotIn(forbidden, text)

    def test_protocol_binds_exact_new_adapter_and_module(self):
        policy = module.protocol_record("ground")
        self.assertEqual(policy["module_sha256"], sha256(Path(module.__file__)))
        self.assertEqual(set(policy["adapter_hashes"]), set(module.FILES))
        self.assertFalse(policy["state_injection"])
        with self.assertRaises(ValueError): module.protocol_record("other")

    def test_fixture_never_injects_player_state_or_calls_player_physics(self):
        source = (module.ROOT / module.ADAPTER_SOURCE / "wall_coyote.gd").read_text(encoding="utf-8")
        self.assertEqual(source.count("player.position ="), 1)  # sole initial spawn
        for forbidden in ("player.velocity =", "player.air_jump =", "player.move_and_slide", "player._physics_process",
                          "coyote_jump_timer.start", "wall_jump_timer.start", "coyote_jump_timer.stop"):
            self.assertNotIn(forbidden, source)
        self.assertIn("Input.action_press", source)
        self.assertIn("RenderingServer.frame_post_draw", source)

    def test_source_mismatch_fails_replay_before_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            output, source = Path(temp) / "out", Path(temp) / "source"
            source.mkdir(); (source / "player.gd").write_text("extends CharacterBody2D\n")
            sources = project_hashes(source)
            suite = output / "baseline/suite.json"
            write_json(suite, {"version": module.VERSION, "stage": "jump", "protocol": module.protocol_record("jump"),
                               "source": str(source), "sources": sources})
            write_json(output / "comparison.json", {"version": module.VERSION, "stage": "jump", "status": "pass",
                       "sources": {"baseline": sources}, "evidence": {"baseline": {"path": str(suite), "sha256": sha256(suite)}}})
            result = module.verify_checks(output, expected_baseline_sources={"player.gd": "0" * 64})
            self.assertEqual(result["status"], "fail")
            self.assertIn("source or protocol", result["reason"])

    def test_suite_failure_is_error_and_candidate_not_started(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(module, "_run_suite", side_effect=ValueError("broken runtime")) as run:
            result = module.run_checks("candidate", "baseline", Path(temp) / "out", "jump")
            self.assertEqual(result["status"], "error")
            self.assertEqual(run.call_count, 1)

    def test_composite_keeps_old_air_cases_and_merges_failure(self):
        legacy = {"status": "pass", "case_count": 34, "air_cases": 18, "air_steps": 72,
                  "baseline_valid": True, "candidate_valid": True, "mismatches": [], "scope": "legacy"}
        bad = recording(); bad["cases"][0]["steps"][34]["post"]["velocity"][1] = -195
        combined = module.compare_recordings(recording(), bad, "ground")
        result = integrity._merge_combined(legacy, combined)
        self.assertEqual(result["status"], "fail")
        self.assertEqual((result["air_cases"], result["air_steps"], result["case_count"]), (18, 72, 38))
        self.assertEqual(result["failed_cases"], 1)
        self.assertEqual(legacy["mismatches"], [])

    def test_old_protocol_files_remain_separate(self):
        from kiroclops.portable import integrity as old
        self.assertEqual(old.VERSION, "heart-integrity-v1")
        self.assertEqual(old.case_specs("jump"), integrity.case_specs("jump"))
        self.assertNotIn("wall_coyote", old.protocol_record("jump"))
        self.assertEqual(integrity.protocol_record("jump")["wall_coyote"]["id"], module.VERSION)


if __name__ == "__main__":
    unittest.main()
