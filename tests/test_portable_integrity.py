"""Finite preservation contract tests; no GUI/model/engine process is started."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.portable import integrity as module


CHECKS = {
    "air": ("fresh_air_contacts", "seed_valid"),
    "ground": ("settled", "remained_grounded", "moved", "stopped"),
    "jump": ("settled", "took_off", "returned", "single_takeoff"),
    "double": ("settled", "first_takeoff", "second_available", "second_consumed", "third_rejected", "landed", "reset_jump"),
    "wall": ("contact_established", "jump_context", "response"),
    "coyote": ("settled", "real_contact_loss", "timer_context", "response", "rights"),
    "hazard": ("displaced", "area_contact", "returned"),
}


def recording(stage="jump"):
    specs = module.case_specs(stage)
    cases, frames = [], []
    for spec in specs:
        sample = {**{key: "0" * 64 for key in module.HASH_KEYS}, **{key: False for key in module.BOOL_KEYS}}
        cases.append({"id": spec["id"], "kind": spec["kind"],
                      "steps": [{"step": j, "pre": deepcopy(sample), "post": deepcopy(sample)}
                                for j in range(1, spec["duration"])],
                      "checks": {key: True for key in CHECKS[spec["kind"]]}})
        for j in range(spec["duration"]):
            i = spec["start"] + j
            frames.append({"frame": i, "tick": 60 + i, "case_id": spec["id"], "step": j,
                           "actions": module.expected_actions(spec, j)})
    return {"schema_version": 1, "protocol": module.VERSION, "stage": stage, "status": "ok", "fps": 60,
            "warmup": 60, "viewport": [320, 180], "frame_count": len(frames), "frames": frames, "cases": cases}


def case(value, name):
    return next(c for c in value["cases"] if c["id"] == name)


class RecordingContractTests(unittest.TestCase):
    def test_complete_baseline_pair_passes_both_stages(self):
        for stage in module.STAGES:
            a = recording(stage)
            result = module.compare_recordings(a, deepcopy(a), stage)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["air_cases"], 18)
            self.assertEqual(result["air_steps"], 72)

    def test_third_velocity_seed_dimension_is_not_slay_state_name(self):
        specs = [s for s in module.case_specs("jump") if s["kind"] == "air"]
        self.assertEqual({s["vx"] for s in specs}, {"rest", "left", "right"})
        self.assertFalse(any("state" in s for s in specs))

    def test_missing_tick_duplicate_tick_and_bad_actions_fail_closed(self):
        for edit in (lambda v: v["frames"].pop(), lambda v: v["frames"][3].update(tick=60),
                     lambda v: v["frames"][3].update(actions=["jump"])):
            data = recording(); edit(data)
            with self.assertRaises(ValueError):
                module.validate_recording(data, "jump")

    def test_wrong_stage_or_case_or_missing_observation_fails(self):
        for edit in (lambda v: v.update(stage="ground"), lambda v: v["cases"][1].update(id=v["cases"][0]["id"]),
                     lambda v: v["cases"][0]["steps"].pop()):
            data = recording(); edit(data)
            with self.assertRaises(ValueError):
                module.validate_recording(data, "jump")

    def test_boolean_step_is_not_a_physics_index(self):
        for edit in (lambda v: v["frames"][1].update(step=True),
                     lambda v: v["cases"][0]["steps"][0].update(step=True)):
            data = recording(); edit(data)
            with self.assertRaises(ValueError): module.validate_recording(data, "jump")

    def test_raw_kinematics_and_invalid_digest_rejected(self):
        for edit in (lambda o: o.update(velocity_x=100), lambda o: o.update(vx="123"), lambda o: o.update(floor=0)):
            data = recording(); edit(data["cases"][0]["steps"][0]["pre"])
            with self.assertRaises(ValueError):
                module.validate_recording(data, "jump")

    def test_airborne_contact_is_an_explicit_failed_precondition(self):
        data = recording(); data["cases"][0]["steps"][2]["post"]["floor"] = True
        result = module.compare_recordings(recording(), data, "jump")
        self.assertEqual(result["status"], "fail")
        self.assertIn("unexpected_contact", [m.get("check") for m in result["mismatches"]])

    def test_invalid_baseline_is_tool_error_not_candidate_motion_failure(self):
        data = recording(); data["cases"][0]["checks"]["seed_valid"] = False
        result = module.compare_recordings(data, recording(), "jump")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "baseline_probe_invalid")

    def test_changed_air_vx_and_displacement_both_rejected(self):
        for field in ("vx", "dx"):
            data = recording(); data["cases"][7]["steps"][2]["post"][field] = "1" * 64
            result = module.compare_recordings(recording(), data, "jump")
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["mismatches"][0]["fields"], [field])

    def test_shared_speed_mutation_is_seen_in_air_and_ground(self):
        data = recording()
        data["cases"][2]["steps"][3]["post"]["vx"] = "2" * 64
        case(data, "ground-long")["steps"][30]["post"]["vx"] = "2" * 64
        result = module.compare_recordings(recording(), data, "jump")
        self.assertEqual(result["failed_cases"], 2)

    def test_shared_jump_mutation_is_rejected_by_wall_and_second_jump(self):
        data = recording()
        case(data, "double-third-landing-reset")["steps"][19]["post"]["vy"] = "3" * 64
        case(data, "wall-right-contact")["steps"][4]["post"]["vy"] = "3" * 64
        result = module.compare_recordings(recording(), data, "jump")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["failed_cases"], 2)

    def test_normal_jump_vertical_change_is_allowed_only_in_jump_stage(self):
        for stage, expected in (("jump", "pass"), ("ground", "fail")):
            data = recording(stage)
            case(data, "normal-jump-24")["steps"][40]["post"]["vy"] = "4" * 64
            result = module.compare_recordings(recording(stage), data, stage)
            self.assertEqual(result["status"], expected)

    def test_double_world_height_rounding_is_not_response_change(self):
        data = recording()
        case(data, "double-third-landing-reset")["steps"][19]["post"]["dy"] = "5" * 64
        self.assertEqual(module.compare_recordings(recording(), data, "jump")["status"], "pass")

    def test_every_behavioral_check_is_mandatory(self):
        for name, key in (("double-third-landing-reset", "third_rejected"), ("wall-left-grace", "jump_context"),
                          ("coyote-outside", "rights"), ("hazard-return", "area_contact")):
            data = recording(); case(data, name)["checks"][key] = False
            result = module.compare_recordings(recording(), data, "jump")
            self.assertEqual(result["status"], "fail")
            self.assertIn({"case_id": name, "check": key}, result["mismatches"])

    def test_missing_required_check_cannot_be_replaced_with_success_field(self):
        data = recording(); checks = case(data, "hazard-return")["checks"]
        checks.pop("area_contact"); checks["success"] = True
        with self.assertRaises(ValueError): module.validate_recording(data, "jump")

    def test_compact_feedback_contains_no_response_digests_or_values(self):
        import json
        result = module.compare_recordings(recording(), recording(), "jump")
        encoded = json.dumps(result)
        self.assertNotIn("0" * 64, encoded)
        self.assertNotIn("velocity_x", encoded)

    def test_checkpoints_cover_each_case_and_are_unique(self):
        for stage in module.STAGES:
            frames = module.checkpoint_frames(stage)
            self.assertEqual(len(frames), len(set(frames)))
            for spec in module.case_specs(stage):
                self.assertIn(spec["start"] + 3, frames)
                self.assertIn(spec["start"] + spec["duration"] - 1, frames)

    def test_run_checks_preserves_explicit_error_and_does_not_launch_candidate_after_baseline_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(module, "_run_suite", side_effect=ValueError("bad source")) as suite:
                result = module.run_checks(Path(temp) / "a", Path(temp) / "b", Path(temp) / "out", "ground")
            self.assertEqual(result["status"], "error")
            self.assertEqual(suite.call_count, 1)
            self.assertTrue((Path(temp) / "out/comparison.json").exists())

    def test_adapter_calls_native_physics_and_uses_actual_input_edges(self):
        source = (module.ROOT / "adapters/heart-integrity-godot4/integrity.gd").read_text()
        self.assertIn('load("res://player.tscn").instantiate()', source)
        self.assertIn("Input.action_press", source)
        self.assertIn("RenderingServer.frame_post_draw", source)
        self.assertNotIn("player._physics_process", source)
        self.assertNotIn("player.move_and_slide", source)
        self.assertNotIn("coyote_jump_timer.start", source)
        self.assertNotIn("wall_jump_timer.start", source)

    def test_final_replay_rejects_other_baseline_before_runtime_verification(self):
        from kiroclops.integration.storage import write_json, sha256, project_hashes
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "integrity"
            source = Path(temp) / "source"; source.mkdir()
            (source / "player.gd").write_text("extends CharacterBody2D\n")
            sources = project_hashes(source)
            suite = output / "baseline/suite.json"
            write_json(suite, {"protocol": module.protocol_record("jump"), "stage": "jump",
                               "source": str(source), "sources": sources})
            write_json(output / "comparison.json", {"version": module.VERSION, "stage": "jump", "status": "pass",
                       "sources": {"baseline": sources}, "evidence": {"baseline": {"path": str(suite), "sha256": sha256(suite)}}})
            result = module.verify_checks(output, expected_baseline_sources={"player.gd": "0" * 64})
            self.assertEqual(result["status"], "fail")
            self.assertIn("Original input source", result["reason"])


if __name__ == "__main__":
    unittest.main()
