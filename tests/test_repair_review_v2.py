"""No captures/models: exact fingerprint exclusions and real review rollback."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.integration import ground_review, repair_review_v2 as operator, storage
from tests import test_ground_review as fixtures


def comparison():
    speed = {"reference": {"analysis_content_sha256": "ref-a", "speed_body_s": 7.5,
                            "manifest_sha256": "raw-ref"},
             "target": {"analysis_content_sha256": "target-a", "speed_body_s": 7.6},
             "relative_error_limit": .1, "policy": {"minimum_held_ticks": 30},
             "status": "pass", "pass": True}
    row = {"scenario": {"id": "l30", "repeat": 1}, "after_rmse_body": .12,
           "speed_comparison": speed, "gates": {"speed": True}}
    return {"ground": {"validation_scenarios": [deepcopy(row)], "scenario_checks": [deepcopy(row)],
                       "gates": {"threshold": .15}, "numerical_status": "fail"},
            "jump_regression": {"scenario_checks": [deepcopy(row)], "numerical_status": "pass"},
            "parent_jump_drift": {"sha256": "parent"}, "numerical_status": "fail"}


class FingerprintTests(unittest.TestCase):
    def test_exact_four_leaves_only_and_input_is_unchanged(self):
        before = comparison()
        changed = deepcopy(before)
        for collection in ("validation_scenarios", "scenario_checks"):
            for role in ("reference", "target"):
                changed["ground"][collection][0]["speed_comparison"][role]["analysis_content_sha256"] += "-manual"
        saved = deepcopy(changed)
        self.assertNotEqual(operator._LEGACY_NUMERICAL(before), operator._LEGACY_NUMERICAL(changed))
        self.assertEqual(operator.numerical_fingerprint(before), operator.numerical_fingerprint(changed))
        self.assertEqual(changed, saved)

    def test_numeric_threshold_policy_identity_and_other_hash_changes_are_not_exempt(self):
        paths = [
            ("ground", "scenario_checks", 0, "after_rmse_body"),
            ("ground", "scenario_checks", 0, "speed_comparison", "relative_error_limit"),
            ("ground", "scenario_checks", 0, "speed_comparison", "policy", "minimum_held_ticks"),
            ("ground", "scenario_checks", 0, "speed_comparison", "reference", "speed_body_s"),
            ("ground", "scenario_checks", 0, "speed_comparison", "reference", "manifest_sha256"),
            ("ground", "scenario_checks", 0, "scenario", "repeat"),
            ("ground", "scenario_checks", 0, "gates", "speed"),
            ("ground", "gates", "threshold"),
            ("jump_regression", "scenario_checks", 0, "speed_comparison", "reference", "analysis_content_sha256"),
            ("parent_jump_drift", "sha256"),
            ("numerical_status",),
        ]
        for path in paths:
            with self.subTest(path=path):
                before, changed = comparison(), comparison()
                node = changed
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = "changed"
                self.assertNotEqual(operator.numerical_fingerprint(before), operator.numerical_fingerprint(changed))

    def test_same_key_elsewhere_is_preserved(self):
        for place in ("row", "speed", "nested"):
            before, after = comparison(), comparison()
            for report, digest in ((before, "a"), (after, "b")):
                row = report["ground"]["scenario_checks"][0]
                node = row if place == "row" else row["speed_comparison"]
                if place == "nested":
                    node = node["reference"].setdefault("extra", {})
                node["analysis_content_sha256"] = digest
            self.assertNotEqual(operator.numerical_fingerprint(before), operator.numerical_fingerprint(after))

    def test_context_restores_on_base_failure_body_failure_and_nested_rejection(self):
        original = ground_review._numerical
        with patch.object(operator.workflow, "verify_protocol", side_effect=storage.IntegrationError("old", "not repair")):
            with self.assertRaisesRegex(storage.IntegrationError, "not repair"):
                with operator.operator_context(Path("unused")):
                    self.fail("Old protocol entered")
        self.assertIs(ground_review._numerical, original)
        with patch.object(operator.workflow, "verify_protocol"):
            with self.assertRaisesRegex(RuntimeError, "body failure"):
                with operator.operator_context(Path("unused")):
                    self.assertIs(ground_review._numerical, operator.numerical_fingerprint)
                    with self.assertRaisesRegex(storage.IntegrationError, "already active"):
                        with operator.operator_context(Path("unused")):
                            self.fail("Nested context entered")
                    raise RuntimeError("body failure")
        self.assertIs(ground_review._numerical, original)


class ReviewTransactionTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing synthetic capture/annotation transaction fixture,
        # not its tests. Its CV calls are mocked; hashes and rollback are real.
        self.fixture = fixtures.GroundReviewTests("test_missing_receipt_unverified")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = self.fixture.directory
        storage.write_json(self.directory / "execution-protocol.json", {"protocol_id": operator.workflow.PROTOCOL_ID})
        @contextmanager
        def base_context(directory):
            yield
        patcher = patch.object(operator.workflow, "operator_context", side_effect=base_context)
        patcher.start(); self.addCleanup(patcher.stop)
        self.fixture.compare.side_effect = lambda context: self.report("pass")
        storage.write_json(self.fixture.comparison_path, self.report("pending"))
        state = storage.read_json(self.directory / "session.json")
        state["final"].update(numerical_status="fail", overall_status="fail")
        storage.write_json(self.directory / "session.json", state)

    def report(self, manual):
        value = self.fixture.make_comparison(manual)
        speed = {role: {"analysis_content_sha256": storage.sha256(self.fixture.groups["ground"][group][0] / "analysis.json"),
                        "speed_body_s": 7.5}
                 for role, group in (("reference", "reference"), ("target", "candidate"))}
        speed.update(relative_error_limit=.1, policy={"minimum_held_ticks": 30})
        for collection in ("validation_scenarios", "scenario_checks"):
            value["ground"][collection] = [{"after_rmse_body": .2, "speed_comparison": deepcopy(speed)}]
        value["ground"]["numerical_status"] = "fail"
        value["numerical_status"] = "fail"
        value["status"] = "fail"
        return value

    def test_old_review_rolls_back_new_review_preserves_numerical_failure_and_full_hashes(self):
        before = self.fixture.snapshot()
        with self.assertRaisesRegex(storage.IntegrationError, "frozen numerical"):
            ground_review.review_session(self.directory, [self.fixture.control()], source="assistant_visual")
        self.assertEqual(self.fixture.snapshot(), before)
        result = operator.review_session(self.directory, [self.fixture.control()], source="assistant_visual")
        self.assertEqual(result["numerical_status"], "fail")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["manual_validation_status"], "pass")
        saved = storage.read_json(result["comparison_path"])
        target = self.fixture.groups["ground"]["candidate"][0]
        self.assertEqual(saved["ground"]["scenario_checks"][0]["speed_comparison"]["target"]["analysis_content_sha256"],
                         storage.sha256(target / "analysis.json"))
        canonical = storage.read_json(result["review_path"])
        self.assertTrue(canonical["measurements_unchanged"])
        self.assertTrue(canonical["raw_evidence_unchanged"])
        self.assertTrue(canonical["sources_unchanged"])
        after = self.fixture.snapshot()
        verified = operator.verify_review_receipt(self.directory)
        self.assertEqual(verified["status"], "pass", verified)
        self.assertEqual(verified["numerical_status"], "fail")
        self.assertEqual(self.fixture.snapshot(), after)
        self.assertEqual(verified["operator_binding_sha256"], storage.sha256(result["operator_binding_path"]))

    def test_original_measurement_guard_and_transaction_remain(self):
        original = self.fixture.reanalyze
        def bad_measurement(run, annotation):
            data = original(run, annotation)
            data["frames"][0]["x"] += 1
            storage.write_json(run / "analysis.json", data)
            return data
        self.fixture.analyze.side_effect = bad_measurement
        before = self.fixture.snapshot()
        with self.assertRaisesRegex(storage.IntegrationError, "image-derived measurement"):
            operator.review_session(self.directory, [self.fixture.control()])
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertFalse(list((self.directory / "final").glob("*.repair-operator.json")))

    def test_numeric_or_threshold_change_still_rolls_back(self):
        for kind in ("curve", "threshold", "other_hash"):
            with self.subTest(kind=kind):
                def changed(context):
                    value = self.report("pass")
                    row = value["ground"]["scenario_checks"][0]
                    if kind == "curve": row["after_rmse_body"] += .01
                    if kind == "threshold": row["speed_comparison"]["relative_error_limit"] += .01
                    if kind == "other_hash": row["speed_comparison"]["target"]["manifest_sha256"] = "different"
                    return value
                self.fixture.compare.side_effect = changed
                before = self.fixture.snapshot()
                with self.assertRaisesRegex(storage.IntegrationError, "frozen numerical"):
                    operator.review_session(self.directory, [self.fixture.control()])
                self.assertEqual(self.fixture.snapshot(), before)

    def test_binding_is_exclusive_and_detects_code_protocol_and_evidence_changes(self):
        result = operator.review_session(self.directory, [self.fixture.control()])
        path = Path(result["operator_binding_path"])
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            operator._attach_binding(self.directory, result, operator._source_hashes())
        self.assertEqual(path.read_bytes(), original)
        for field in ("correction_id", "excluded_numerical_paths", "operator_source_hashes", "review_sha256",
                      "comparison_sha256", "numerical_sha256", "execution_protocol_sha256", "grants_new_acceptance"):
            with self.subTest(field=field):
                content = json.loads(original)
                content[field] = "tampered"
                storage.write_json(path, content)
                self.assertEqual(operator.verify_review_receipt(self.directory)["status"], "fail")
                path.write_bytes(original)
        with patch.object(operator, "_source_hashes", return_value={"new-code": "different"}):
            self.assertEqual(operator.verify_review_receipt(self.directory)["status"], "fail")
        Path(result["review_path"]).write_bytes(Path(result["review_path"]).read_bytes() + b" ")
        self.assertEqual(operator.verify_review_receipt(self.directory)["status"], "fail")

    def test_missing_operator_binding_is_unverified_not_pass(self):
        result = operator.review_session(self.directory, [self.fixture.control()])
        Path(result["operator_binding_path"]).unlink()
        checked = operator.verify_review_receipt(self.directory)
        self.assertEqual(checked["status"], "unverified")

    def test_report_uses_one_context_and_labels_inside_it(self):
        operator.review_session(self.directory, [self.fixture.control()])
        calls = []
        @contextmanager
        def base(directory):
            calls.append("enter")
            yield
            calls.append("exit")
        def generate(directory):
            self.assertIs(ground_review._numerical, operator.numerical_fingerprint)
            calls.append("generate")
            return self.directory / "report.html"
        def label(path):
            self.assertIs(ground_review._numerical, operator.numerical_fingerprint)
            calls.append("label")
            return path
        with patch.object(operator.workflow, "operator_context", side_effect=base), \
             patch("kiroclops.integration.ground_reporting.generate_ground_report", side_effect=generate), \
             patch.object(operator.workflow, "_label_report", side_effect=label):
            operator.build_report(self.directory)
        self.assertEqual(calls, ["enter", "generate", "label", "exit"])


if __name__ == "__main__":
    unittest.main()
