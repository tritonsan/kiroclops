"""Contract tests for portable measurements and worker decisions; no engines."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration.storage import IntegrationError, project_hashes, read_json, write_json
from kiroclops.portable_v2 import evaluation as worker, measurement
from kiroclops.portable_v2.specs import scenarios


def reports(role="reference", *, split="calibration"):
    result = []
    scale = 2 if role == "before" else 1
    for spec in scenarios("jump", split):
        result.append({"status": "ok", "camera_valid": True, "valid_ratio": 1.0, "fps": 60,
            "game": "tiny" if role == "reference" else "heart", "variant": role,
            "body_height_px": 7 if role == "reference" else 15, "scenario": spec,
            "run_id": f"fixture-{role}-{spec['id']}-{spec['repeat']}",
            "frames": [{"frame_index": i, "physics_tick": 60+i, "time_s": i/60, "valid": True,
                        "height_body": scale*max(0, 1-((i-45)/15)**2)} for i in range(240)],
            "events": {"takeoff_frame": 30, "apex_frame": 45, "landing_frame": 60},
            "metrics": {"height_body": scale, "rise_s": .25, "fall_s": .25, "input_latency_s": 1/60}})
    return result


class MeasurementContractTests(unittest.TestCase):
    def test_calibration_and_heldout_matrices_keep_same_thresholds(self):
        for split, required in (("calibration", 6), ("validation", 12)):
            result = measurement.compare_motion("jump", reports(split=split), reports("before", split=split), reports("after", split=split), split)
            self.assertTrue(result["pass"])
            self.assertEqual(result["required"], required)
            self.assertEqual(result["measurement_status"], "pending")

    def test_duplicate_or_missing_record_cannot_improve_average(self):
        for after in (reports("after")[:-1], reports("after") + [reports("after")[0]]):
            with self.assertRaises(IntegrationError):
                measurement.compare_motion("jump", reports(), reports("before"), after, "calibration")

    def test_wrong_split_rejected(self):
        after = reports("after")
        after[0]["scenario"]["split"] = "validation"
        with self.assertRaises(IntegrationError):
            measurement.compare_motion("jump", reports(), reports("before"), after, "calibration")

    def test_nan_confidence_rejected(self):
        after = reports("after")
        after[0]["valid_ratio"] = float("nan")
        with self.assertRaises((IntegrationError, ValueError)):
            measurement.compare_motion("jump", reports(), reports("before"), after, "calibration")

    def test_nan_timestamp_outside_flight_rejected(self):
        after = reports("after")
        after[0]["frames"][200]["time_s"] = float("nan")
        with self.assertRaises((IntegrationError, ValueError)):
            measurement.compare_motion("jump", reports(), reports("before"), after, "calibration")

    def test_physics_tick_corruption_rejected(self):
        after = reports("after")
        after[0]["frames"][100]["physics_tick"] = 101
        with self.assertRaises((IntegrationError, ValueError)):
            measurement.compare_motion("jump", reports(), reports("before"), after, "calibration")

    def test_reference_cannot_be_reused_as_target_measurement(self):
        with self.assertRaises((IntegrationError, ValueError)):
            measurement.compare_motion("jump", reports(), reports("before"), reports(), "calibration")

    def test_scenario_label_cannot_hide_wrong_hold_duration(self):
        after = reports("after")
        after[0]["scenario"]["hold_ticks"] = 24
        with self.assertRaises((IntegrationError, ValueError)):
            measurement.compare_motion("jump", reports(), reports("before"), after, "calibration")


class EvaluationDecisions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.project = self.directory / "candidates/candidate-01/project"
        self.project.mkdir(parents=True)
        (self.project / "player.gd").write_text("original", encoding="utf-8")
        self.cid = "candidate-01"
        write_json(self.project.parent / "candidate.json", {"candidate_id": self.cid,
                   "project_path": str(self.project), "source_sha256": "a"*64})
        self.state = {"experiment_spec": {"stage": "ground"}, "reference": {"ground": ["rg"], "jump": ["rj"]},
                      "baseline_runs": {"ground": ["bg"], "jump": ["bj"]},
                      "preparation": {"profile_path": str(self.directory / "profile.json")}}
        write_json(self.directory / "profile.json", {})
        self.job = {"job_id": "job-evaluate", "candidate_id": self.cid}
        self.kw = {"cancel": None, "check": lambda: None, "progress": lambda *a: None}
        self.comparison = {"stage": "ground", "split": "calibration", "numerical_status": "pass", "pass": True,
                           "passed": 6, "required": 6, "mean_rmse_body": .02, "scenario_checks": [], "measurement_status": "pending"}

    def test_preservation_failure_short_circuits_motion(self):
        with patch.object(worker, "_integrity", return_value={"status": "fail", "mismatches": [{"case_id": "air-left", "fields": ["vx"]}]}), \
             patch.object(worker, "_matrix", side_effect=AssertionError("Motion should not run")):
            result = worker._evaluate(self.directory, self.job, self.state, **self.kw)
        self.assertFalse(result["eligible"])
        self.assertIsNone(result["mean_rmse_body"])
        self.assertEqual(result["failure_class"], "gameplay_integrity_violation")
        self.assertEqual(result["motion_calibration"], "not_run")

    def test_invalid_integrity_capture_is_tool_failure(self):
        with patch("kiroclops.portable_v2.integrity.run_checks", return_value={"status": "error", "code": "missing_frame"}):
            with self.assertRaises(IntegrationError) as caught:
                worker._integrity(self.project, self.project, self.directory/"integrity", "ground", cancel=None, check=lambda: None)
        self.assertEqual(caught.exception.code, "integrity_tool_failure")

    def test_candidate_compiler_diagnostic_keeps_res_line_without_private_path(self):
        output=self.directory/'bad-integrity'
        log=output/'candidate/engine.log'; log.parent.mkdir(parents=True)
        log.write_text('SCRIPT ERROR: Parse Error: Unknown identifier motion_speed\n at: res://player.gd:42\n Compile Error: C:/private/candidate/player.gd\n',encoding='utf-8')
        with patch('kiroclops.portable_v2.integrity.run_checks',return_value={'status':'error','code':'engine_error'}):
            with self.assertRaises(IntegrationError) as caught:
                worker._integrity(self.project,self.project,output,'jump',cancel=None,check=lambda:None)
        self.assertEqual(caught.exception.code,'candidate_compilation_error')
        self.assertIn('res://player.gd:42',str(caught.exception))
        self.assertNotIn('C:/private',str(caught.exception))

    def test_fresh_snapshot_is_imported_before_capture_with_canonical_schedule(self):
        calls=[]
        def imported(*args,**kwargs): calls.append('import')
        def captured(project,run,scenario,**kwargs):
            self.assertEqual(calls[0],'import')
            self.assertNotIn('mechanic',scenario); self.assertNotIn('fps',scenario)
            self.assertEqual(scenario['variant'],'after'); calls.append('capture')
        with patch('kiroclops.portable_v2.runtime.import_project',side_effect=imported), \
             patch('kiroclops.portable_v2.runtime.capture_project',side_effect=captured), \
             patch.object(measurement,'analyze'):
            result=worker._matrix(self.project,'jump','calibration',self.directory/'matrix','after',**self.kw)
        self.assertEqual(len(result),6)
        self.assertEqual(calls,['import']+['capture']*6)

    def test_hundreds_of_preservation_steps_produce_one_useful_case_summary(self):
        raw={'status':'fail','mismatches':[{'case_id':'air-right','step':i,'fields':['vx'],'baseline_vx':'secret-hash'} for i in range(100)]}
        value=worker._public_integrity(raw)
        self.assertEqual(len(value['mismatches']),1)
        self.assertEqual(value['mismatches'][0]['mismatch_count'],100)
        self.assertEqual(value['mismatches'][0]['step_range'],[0,99])
        self.assertNotIn('secret-hash',str(value))

    def test_ground_cannot_ignore_regressed_jump_calibration(self):
        jump = {**self.comparison, "stage": "jump", "pass": False, "numerical_status": "fail", "passed": 0}
        with patch.object(worker, "_integrity", return_value={"status": "pass"}), \
             patch.object(worker, "_matrix", return_value=["after"]) as capture, \
             patch.object(measurement, "compare_motion", side_effect=[self.comparison, jump]) as compare, \
             patch.object(measurement, "diagnostics", return_value={}):
            result = worker._evaluate(self.directory, self.job, self.state, **self.kw)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["failure_class"], "motion_mismatch")
        self.assertEqual([call.args[1:3] for call in capture.call_args_list], [("ground", "calibration"), ("jump", "calibration")])
        self.assertTrue(all(call.args[-1] == "calibration" for call in compare.call_args_list))

    def test_unsealed_candidate_cannot_request_heldout(self):
        with patch.object(worker, "_refs", side_effect=AssertionError("Held-out accessed")):
            with self.assertRaises(IntegrationError) as caught:
                worker._finalize(self.directory, self.job, self.state, **self.kw)
        self.assertEqual(caught.exception.code, "validation_unsealed")

    def test_ground_jump_improvement_uses_original_clean_source(self):
        original = self.directory / "clean"
        original.mkdir()
        (original/"player.gd").write_text("clean", encoding="utf-8")
        self.state.update(sealed_at="sealed", selected_candidate_id=self.cid,
                          original_baseline={"project": str(original), "source_hashes": project_hashes(original)})
        with patch.object(worker, "_integrity", return_value={"status": "pass"}), \
             patch.object(worker, "_refs", return_value=["ref"]), \
             patch.object(worker, "_matrix", return_value=["analysis"]) as capture, \
             patch.object(measurement, "compare_motion", return_value=self.comparison), \
             patch("kiroclops.portable_v2.runtime.capture_project", return_value={"gameplay_status": "pass"}):
            result = worker._finalize(self.directory, self.job, self.state, **self.kw)
        before_calls = [c for c in capture.call_args_list if c.args[4] == "before"]
        self.assertEqual(before_calls[0].args[0], self.directory/"baseline")
        self.assertEqual(before_calls[1].args[0], original)
        self.assertEqual(result["measurement_status"], "pending")
        self.assertEqual(result["automatic_integrity_status"], "pass")

    def test_motor_hashes_and_backend_paths_are_not_public_feedback(self):
        raw = {"status": "fail", "scope": "finite", "sources": {"player.gd": "abc"},
               "seed_velocity": [100, -200], "mismatches": [{"case_id": "air-right", "step": 3,
               "fields": ["vx"], "baseline_vx": "hash", "candidate_vx": "hash2"}], "evidence": {"path": "parent/results"}}
        public = worker._public_integrity(raw)
        self.assertEqual(public, {"status": "fail", "scope": "finite", "mismatches": [{"case_id": "air-right", "step_range": [3,3], "mismatch_count":1, "checks":[], "fields": ["vx"]}]})


if __name__ == "__main__":
    unittest.main()
