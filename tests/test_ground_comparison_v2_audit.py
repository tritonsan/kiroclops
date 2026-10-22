"""Independent synthetic acceptance-boundary checks; no session or engine runs."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.ground import comparison
from kiroclops.integration import ground_comparison_v2 as repair
from test_ground_comparison_v2 import report, matrix


def heldout_matrix():
    groups = []
    for role in ("reference", "before", "after"):
        reports = []
        for sid in ("r18", "l30", "reverse24"):
            for offset in (0, 24):
                for repeat in (1, 2, 3):
                    value = report(role, sid, repeat)
                    value["scenario"]["spawn_offset"] = offset
                    if sid == "l30":
                        for frame in value["frames"]:
                            frame["x"] = -frame["x"]
                            frame["x_body"] = -frame["x_body"]
                    reports.append(value)
        groups.append(reports)
    return groups


class IndependentComparisonAuditTests(unittest.TestCase):
    def test_all_eighteen_slots_preserve_manual_pending_and_explicit_speed_na(self):
        groups = heldout_matrix()
        original = deepcopy(groups)
        with repair.repaired_measurements():
            result = comparison.compare_runs(*groups, split="validation")
        self.assertEqual(result["coverage"]["complete_triplets"], 18)
        self.assertEqual(result["coverage"]["required_triplets"], 18)
        self.assertEqual(result["numerical_status"], "pass")
        self.assertEqual(result["manual_validation_status"], "pending")
        self.assertFalse(result["success"])
        applicable = [r for r in result["scenario_checks"] if r["speed_eligible"]]
        self.assertEqual(len(applicable), 6)
        self.assertTrue(all(r["scenario"]["id"] == "l30" for r in applicable))
        self.assertEqual(groups, original)
        self.assertEqual(comparison.compare_runs(*groups, split="validation")["numerical_status"], "fail")

    def test_same_short_scenario_id_with_changed_events_is_rejected(self):
        groups = heldout_matrix()
        groups[2][0]["scenario"]["events"][1]["tick"] += 1
        with repair.repaired_measurements():
            result = comparison.compare_runs(*groups, split="validation")
        self.assertEqual(result["numerical_status"], "fail")
        self.assertFalse(result["success"])
        self.assertTrue(any("invalid after measurement" in error for error in result["errors"]))

    def test_one_missing_short_slot_cannot_be_exempted_with_its_speed(self):
        groups = heldout_matrix()
        groups[2].pop(0)
        with repair.repaired_measurements():
            result = comparison.compare_runs(*groups, split="validation")
        self.assertEqual(result["coverage"]["complete_triplets"], 17)
        self.assertEqual(result["numerical_status"], "pending")
        self.assertFalse(result["success"])

    def test_heldout_data_cannot_enter_calibration_feedback_through_wrapper(self):
        groups = matrix()
        groups[2].append(report("after", "l30"))
        with repair.repaired_measurements(), self.assertRaises(ValueError):
            comparison.calibration_feedback(*groups)

    def test_real_feedback_path_cannot_promote_mocked_speed_uncertainty(self):
        original = repair.compare_speeds
        def uncertain(reference, target, **kwargs):
            result = original(reference, target, **kwargs)
            if result["speed_eligible"]:
                result.update(status="indeterminate", **{"pass": None})
            return result
        with patch.object(repair, "compare_speeds", side_effect=uncertain), repair.repaired_measurements():
            result = comparison.calibration_feedback(*matrix())
        self.assertEqual(result["numerical_status"], "pending")
        self.assertFalse(result["eligible"])
        self.assertFalse(result["diagnostic_gates_pass"])

    def test_speed_uncertainty_preserves_independent_manual_failure(self):
        original = repair.compare_speeds
        def uncertain(reference, target, **kwargs):
            result = original(reference, target, **kwargs)
            if result["speed_eligible"]:
                result.update(status="indeterminate", **{"pass": None})
            return result
        with patch.object(repair, "compare_speeds", side_effect=uncertain), patch.object(comparison, "_manual", return_value={"status":"fail"}), repair.repaired_measurements():
            result = comparison.calibration_feedback(*matrix())
        self.assertEqual(result["manual_validation_status"], "fail")
        self.assertEqual(result["comparison_status"], "fail")
        self.assertFalse(result["eligible"])

    def test_pending_ground_cannot_hide_a_definite_jump_failure(self):
        from kiroclops.integration import ground_worker
        ground = {"numerical_status":"pending", "manual_validation_status":"pass"}
        jump = {"numerical_status":"fail", "manual_validation_status":"pass"}
        with repair.repaired_measurements():
            result = ground_worker.combine_comparisons(ground, jump, {"status":"diagnostic"})
        self.assertEqual(result["numerical_status"], "fail")
        self.assertFalse(result["success"])

    def test_pending_ground_and_successful_jump_stay_pending(self):
        from kiroclops.integration import ground_worker
        ground = {"numerical_status":"pending", "manual_validation_status":"pass"}
        jump = {"numerical_status":"pass", "manual_validation_status":"pass"}
        with repair.repaired_measurements():
            result = ground_worker.combine_comparisons(ground, jump, {"status":"diagnostic"})
        self.assertEqual(result["numerical_status"], "pending")
        self.assertFalse(result["success"])


class OperatorContextAuditTests(unittest.TestCase):
    def test_invalid_protocol_cannot_enter_new_measurement_context(self):
        from kiroclops.integration import repair_workflow_v2 as workflow
        from kiroclops.integration.storage import IntegrationError
        original = comparison.compare_runs
        with patch.object(workflow, "verify_protocol", side_effect=IntegrationError("invalid_protocol", "changed source")), patch.object(repair, "repaired_measurements") as context:
            with self.assertRaises(IntegrationError), workflow.operator_context(Path("unused-session")):
                self.fail("Invalid protocol must not reach operator work")
        context.assert_not_called()
        self.assertIs(comparison.compare_runs, original)

    def test_operator_failure_restores_legacy_measurements(self):
        from kiroclops.integration import repair_workflow_v2 as workflow
        original = comparison.compare_runs
        directory = Path("unused-session")
        with patch.object(workflow, "verify_protocol") as verify:
            with self.assertRaisesRegex(RuntimeError, "operator failed"):
                with workflow.operator_context(directory):
                    verify.assert_called_once_with(directory)
                    self.assertIsNot(comparison.compare_runs, original)
                    raise RuntimeError("operator failed")
        self.assertIs(comparison.compare_runs, original)

    def test_report_recomputes_in_verified_context_and_labels_after_restore(self):
        from kiroclops.integration import repair_workflow_v2 as workflow
        from kiroclops.integration import ground_reporting
        original = comparison.compare_runs
        directory = Path("unused-session")
        report_path = directory / "report/index.html"
        events = []
        def verify(value):
            self.assertEqual(value, directory)
            self.assertIs(comparison.compare_runs, original)
            events.append("verified")
        def generate(value):
            self.assertEqual(value, directory)
            self.assertIsNot(comparison.compare_runs, original)
            events.append("generated-v2")
            return report_path
        def label(value):
            self.assertEqual(value, report_path)
            self.assertIs(comparison.compare_runs, original)
            events.append("labelled")
            return value
        with patch.object(workflow, "verify_protocol", side_effect=verify), patch.object(ground_reporting, "generate_ground_report", side_effect=generate), patch.object(workflow, "_label_report", side_effect=label):
            self.assertEqual(workflow.build_report(directory), report_path)
        self.assertEqual(events, ["verified", "generated-v2", "labelled"])
        self.assertIs(comparison.compare_runs, original)


if __name__ == "__main__":
    unittest.main()
