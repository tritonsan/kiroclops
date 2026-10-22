"""Sub-frame input-window rule seams; no captured matrix, image decode or acceptance."""
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kiroclops.visual.schema import VisualError, file_sha256, read_json, write_json
from kiroclops.web_local.specs import TIMING_LIMITS
from scripts import phase3d_v2_panel_subframe_window_rule as rule

# The V3b reader integrates this correction, so the frozen V2 code this
# diagnostic transforms exists only in the V2 snapshot. These classes run there
# (tests/test_v2_snapshot_diagnostics.py); the integrated V3b rule is tested in
# tests/test_realistic_review_v3_rules.py.
FROZEN_V2_READER = not hasattr(rule.review, "SUBFRAME_RULE")
V2_ONLY = "Bound to the frozen V2 reader; verified in the V2 snapshot."

FRAME_S = 1 / 60
MAPPING = {"handle": "h", "bookmark": "b", "star": "s"}
OFFSETS = {"handle": 0., "bookmark": 10., "star": 30.}


def events(uncertainty=.0006):
    # drag_start and the first move 6 ms apart, between images 30 (0.5 s) and 31.
    rows = [{"time_s": .1, "action": "click"}, {"time_s": .502, "action": "drag_start"},
            {"time_s": .508, "action": "drag_move"}, {"time_s": .56, "action": "drag_move"},
            {"time_s": .62, "action": "drag_end"}]
    return [{**row, "clock_uncertainty_s": uncertainty} for row in rows]


def bundle(times=None, inputs=None, limits=True):
    """Three glyphs that follow a drag; produced samples equal the raw points."""
    times = [index * FRAME_S for index in range(60)] if times is None else times
    frames, rows, objects = [], [], {oid: [] for oid in MAPPING}
    for index, stamp in enumerate(times):
        travel = 3. * max(0., min(stamp - .5, .12)) / .01
        frames.append({"index": index, "time_s": stamp, "sha256": f"{index:064x}", "time_interval_s": [stamp, stamp]})
        points = {glyph: {"x": 20. + OFFSETS[oid] + travel, "y": 10. + OFFSETS[oid]} for oid, glyph in MAPPING.items()}
        rows.append({"index": index, "time_s": stamp, "image_sha256": f"{index:064x}", "points": points})
        for oid, glyph in MAPPING.items():
            objects[oid].append({"index": index, "time_s": stamp, **points[glyph], "valid": True})
    selection = {"objects": [{"id": oid} for oid in MAPPING]}
    manifest = {"frames": frames, "inputs": {"provenance": "recorder_verified", "events": events() if inputs is None else inputs},
                "metadata": {"timing": {"limits": dict(TIMING_LIMITS)}} if limits else {}}
    return {"record": {"id": "fixture", "role": "selected", "selection": selection}, "mapping": dict(MAPPING),
            "active": {oid: [[times[0], times[-1]]] for oid in MAPPING}, "manifest": manifest, "raw": {"rows": rows},
            "observations": {"objects": [{"id": oid, "frames": objects[oid]} for oid in MAPPING]}}


def produced(value, oid):
    return next(obj for obj in value["observations"]["objects"] if obj["id"] == oid)["frames"]


def window(checks, oid, window_id="input-1"):
    return next(row for row in checks if row["object_id"] == oid and row.get("window_id") == window_id)


@unittest.skipUnless(FROZEN_V2_READER, V2_ONLY)
class SubframeRuleTests(unittest.TestCase):
    def outcome(self, value):
        old, new, transform, changed = rule.compare_same_raw([value])
        return old, new, transform, changed

    def test_frozen_reader_fails_the_subframe_window_and_the_rule_reclassifies_it(self):
        frozen = rule.review._position_checks
        source_before = inspect.getsource(frozen)
        (_, old_failures), (new_checks, new_failures), transform, changed = self.outcome(bundle())
        self.assertEqual([(row["object_id"], row["reason"]) for row in old_failures],
                         [(oid, rule.UNSAMPLED_REASON) for oid in MAPPING])
        self.assertEqual(new_failures, [])
        self.assertEqual(len(changed), 3)
        for oid in MAPPING:
            after = window(new_checks, oid)
            self.assertEqual((after["status"], after["reason"], after["pass"]), ("not_observable", rule.SUBFRAME_REASON, None))
            evidence = after["subframe_evidence"]
            self.assertEqual([image["index"] for image in evidence["bracketing_images"]], [30, 31])
            self.assertAlmostEqual(evidence["window_duration_s"], .006)
            self.assertAlmostEqual(evidence["bracketing_interval_s"], FRAME_S)
            self.assertTrue(evidence["response_checked_in_following_input_window"])
            self.assertTrue(window(new_checks, oid, "input-2")["pass"])
        self.assertEqual(transform["replacement_count"], 1)
        self.assertFalse(transform["frozen_module_mutated"])
        self.assertIs(rule.review._position_checks, frozen)
        self.assertEqual(inspect.getsource(frozen), source_before)

    def test_an_empty_window_as_long_as_an_image_interval_still_fails(self):
        times = [index * FRAME_S for index in range(60) if not 34 <= index <= 37]
        _, (new_checks, new_failures), _, changed = self.outcome(bundle(times))
        self.assertEqual({row["before"]["window_id"] for row in changed}, {"input-1"})
        self.assertEqual({(row["window_id"], row["reason"]) for row in new_failures}, {("input-3", rule.UNSAMPLED_REASON)})
        self.assertFalse(window(new_checks, "star", "input-3")["pass"])

    def test_a_short_window_inside_a_capture_stall_still_fails(self):
        times = [index * FRAME_S for index in range(60) if not 25 <= index <= 35]
        _, (new_checks, new_failures), _, _ = self.outcome(bundle(times))
        for oid in MAPPING:
            self.assertEqual(window(new_checks, oid)["reason"], rule.UNSAMPLED_REASON)
        self.assertEqual(sum(row["window_id"] == "input-1" for row in new_failures), 3)

    def test_every_bracketing_image_condition_is_required_per_object(self):
        mutations = {
            "star": lambda value: produced(value, "star")[31].update(valid=False),
            "bookmark": lambda value: value["raw"]["rows"][30]["points"].update(b={}),
            "handle": lambda value: produced(value, "handle")[31].update(x=produced(value, "handle")[31]["x"] + 5.),
        }
        for oid, mutate in mutations.items():
            with self.subTest(object_id=oid):
                value = bundle()
                mutate(value)
                _, (new_checks, new_failures), _, changed = self.outcome(value)
                self.assertEqual(window(new_checks, oid)["reason"], rule.UNSAMPLED_REASON)
                self.assertIn(oid, {row["object_id"] for row in new_failures if row["window_id"] == "input-1"})
                self.assertEqual(len(changed), 2)

    def test_an_object_inactive_in_a_bracketing_image_is_not_reclassified(self):
        value = bundle()
        value["active"]["star"] = [[.505, value["manifest"]["frames"][-1]["time_s"]]]
        _, (new_checks, _), _, changed = self.outcome(value)
        self.assertEqual(window(new_checks, "star")["reason"], rule.UNSAMPLED_REASON)
        self.assertEqual({row["before"]["object_id"] for row in changed}, {"handle", "bookmark"})

    def test_timing_contract_and_clock_uncertainty_guards(self):
        def missing_uncertainty(value):
            del value["manifest"]["inputs"]["events"][1]["clock_uncertainty_s"]

        def wide_uncertainty(value):
            value["manifest"]["inputs"]["events"][1]["clock_uncertainty_s"] = .003

        def wide_image_time(value):
            value["manifest"]["frames"][31]["time_interval_s"] = [.507, value["manifest"]["frames"][31]["time_s"]]

        def other_limits(value):
            value["manifest"]["metadata"]["timing"]["limits"]["maximum_motion_gap_s"] = .2

        cases = {"no_limits": lambda value: value["manifest"].update(metadata={}),
                 "other_limits": other_limits, "missing_uncertainty": missing_uncertainty,
                 "wide_uncertainty": wide_uncertainty, "wide_image_time": wide_image_time}
        for name, mutate in cases.items():
            with self.subTest(case=name):
                value = bundle()
                mutate(value)
                _, (_, new_failures), _, changed = self.outcome(value)
                self.assertEqual(changed, [])
                self.assertEqual(len(new_failures), 3)

    def test_a_real_position_difference_survives_the_rule(self):
        value = bundle()
        for index in range(34, 38):
            produced(value, "bookmark")[index]["x"] += 3.
        _, (_, new_failures), _, changed = self.outcome(value)
        self.assertEqual(len(changed), 3)
        failed = window(new_failures, "bookmark", "input-3")
        self.assertEqual(failed["code"], rule.WINDOW_CODE)
        self.assertNotIn("reason", failed)

    def test_the_ast_transform_refuses_an_unrecognized_or_altered_frozen_method(self):
        real = inspect.getsource(rule.review._position_checks)
        for text in ("def _position_checks(bundles):\n    return [], []\n",
                     real.replace("object_not_active_in_input_window", "object_hidden"),
                     real.replace("def _position_checks", "def _other_checks")):
            with self.subTest(text=text[:40]):
                with patch.object(rule.inspect, "getsource", return_value=text):
                    with self.assertRaises(VisualError) as caught:
                        rule.corrected_position_method()
                self.assertEqual(caught.exception.code, "unsupported_position_reader")

    def test_any_change_beyond_empty_windows_is_rejected(self):
        def tampered(bundles):
            checks, failures = rule.review._position_checks(bundles)
            checks = deepcopy(checks)
            checks[0]["tolerance_px"] = 9.
            return checks, failures

        with patch.object(rule, "corrected_position_method", return_value=(tampered, {})):
            with self.assertRaises(VisualError) as caught:
                rule.compare_same_raw([bundle()])
        self.assertEqual(caught.exception.code, "wrong_window_transform")


class DiagnosticReceiptTests(unittest.TestCase):
    """Mock only canonical matrix validation; never assert real evidence passed."""
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session, self.aid = self.root / "session", self.root / "aid"
        self.session.mkdir(); self.aid.mkdir()
        self.context = {"event_contract": {}}
        self.records = [{"id": f"row-{index:03}", "package": self.root / f"package-{index}"} for index in range(128)]
        self.before = [{"record_id": f"row-{index:03}", "object_id": "bookmark", "role": "selected",
                        "window_id": "input-1", "window_s": [1., 1.006], "valid_ratio": 0., "median_error_px": None,
                        "tolerance_px": 1.5, "pass": False, "observable": True, "status": "fail",
                        "reason": rule.UNSAMPLED_REASON} for index in range(36)]
        self.after = [{**{key: row[key] for key in rule.IDENTITY}, "status": "not_observable", "observable": False,
                       "reason": rule.SUBFRAME_REASON, "valid_ratio": None, "median_error_px": None, "pass": None,
                       "subframe_evidence": {"window_duration_s": .006, "bracketing_interval_s": .0167,
                                             "bracketing_images": [{"error_px": .5}, {"error_px": .75}]}}
                      for row in self.before]
        self.windows = [{"code": rule.WINDOW_CODE, **row} for row in self.before]
        self.event_failure = {"code": "raw_consumed_event_count_mismatch", "objects": ["bookmark", "handle", "handle"]}
        self.old_events = ([{"old_matched_event": True}], [{"independent_prediction": 1}], [{"role_relevance": True}],
                           [self.event_failure])
        self.corrected_events = ([{"new_matched_event": True}], self.old_events[1], self.old_events[2], [])
        self.old_result = {"context": self.context, "numeric_status": "fail", "positions": self.before,
            "event_checks": self.old_events[0], "event_relevance": self.old_events[2],
            "failures": self.windows + [self.event_failure]}
        self.proof = {"kind": "IndependentFinalWebEnvelopeRawVerification", "status": "proposal_verified",
            "numeric_status": "fail", "domain": "panel", "record_count": 128, "verified_scopes": {},
            "full_behavior_acceptance": False, "consumed_files": {}, "candidate_sha256": "c" * 64,
            "suite_sha256": "d" * 64, "selected_candidate_id": "candidate-02", "evaluation_job_id": "eval",
            "final_job_id": "final", "required_observations": [["e" * 64, "f" * 64]]}
        write_json(self.aid / "evidence-receipt.json", {"fixture": "unsigned complete failed proposal"})
        self.pin = file_sha256(self.aid / "evidence-receipt.json")
        self.proof["consumed_files"] = {str(self.aid / "evidence-receipt.json"): self.pin}
        self.write_fixture()

    def write_fixture(self):
        write_json(self.aid / "result.json", self.old_result)
        write_json(self.aid / "records.json", [{**row, "package": str(row["package"])} for row in self.records])
        write_json(self.aid / "extraction-complete.json", {
            "all_pixel_extraction_finished_before_measurement_read": True,
            "raw_points_sha256": {row["id"]: "a" * 64 for row in self.records}})

    def position_outcome(self):
        changed = [{"before": b, "after": a} for b, a in zip(self.before, self.after)]
        return (self.before, self.windows), (self.after, []), {"fixture_transform": True}, changed

    @contextmanager
    def fixture_gates(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(rule.predecessor_reader, "validate", return_value=self.proof))
            stack.enter_context(patch.object(rule.predecessor_reader, "_controller", side_effect=nullcontext))
            stack.enter_context(patch.object(rule.review, "_session_context", return_value=(self.context, self.records)))
            stack.enter_context(patch.object(rule.review, "_records", side_effect=lambda rows, *args: rows))
            stack.enter_context(patch.object(rule.event_key, "_raw_bundle", side_effect=lambda record, *args: {"record": record}))
            stack.enter_context(patch.object(rule.review, "_measurement", side_effect=lambda value, inventory:
                inventory.update({str(self.aid / "result.json"): file_sha256(self.aid / "result.json")})))
            stack.enter_context(patch.object(rule, "compare_same_raw", side_effect=lambda bundles: self.position_outcome()))
            stack.enter_context(patch.object(rule.event_key, "compare_same_raw",
                                             return_value=(self.old_events, self.corrected_events, {"fixture_event": True})))
            yield

    def derive(self):
        return rule._derive(self.session, self.aid, self.pin, rule.report.Inputs())

    def test_reclassified_windows_and_event_keys_leave_no_numeric_failure_but_grant_nothing(self):
        with self.fixture_gates():
            result = self.derive()
        self.assertEqual(result["canonical_numeric_status"], "fail")
        self.assertEqual(result["post_hoc_numeric_status_with_event_key_and_subframe_rules"], "pass")
        self.assertEqual((result["old_unsampled_window_failure_count"], result["reclassified_window_count"],
                          result["retained_unsampled_window_failure_count"], result["remaining_failure_count"]), (36, 36, 0, 0))
        self.assertEqual((result["old_event_failure_count"], result["corrected_event_failure_count"]), (1, 0))
        self.assertEqual(result["reclassified_bracket_range_s"], [.0167, .0167])
        self.assertEqual(result["maximum_bracketing_image_error_px"], .75)
        for key in ("raw_acceptance_granted", "native_acceptance_granted", "source_acceptance_granted",
                    "human_inspection_claimed", "full_behavior_acceptance", "new_recording_or_model_started",
                    "canonical_result_changed", "historical_outcomes_rewritten", "contact_sheets_inspected"):
            self.assertIs(result[key], False)
        self.assertTrue(result["applies_prospectively_only"])

    def test_unrelated_failures_stay_in_the_remaining_list(self):
        geometry = {"code": "raw_body_geometry_unqualified", "object_id": "star"}
        self.old_result["failures"] = self.windows + [geometry, self.event_failure]
        self.write_fixture()
        with self.fixture_gates():
            result = self.derive()
        self.assertEqual(result["remaining_failures"], [geometry])
        self.assertEqual(result["post_hoc_numeric_status_with_event_key_and_subframe_rules"], "fail")

    def test_stale_saved_positions_or_events_are_rejected(self):
        for name in ("positions", "event_checks"):
            with self.subTest(field=name):
                saved = deepcopy(self.old_result)
                self.old_result[name] = []
                self.write_fixture()
                with self.fixture_gates(), self.assertRaises(VisualError) as caught:
                    self.derive()
                self.assertEqual(caught.exception.code, "stale_predecessor")
                self.old_result = saved
                self.write_fixture()

    def test_pin_attestation_and_incomplete_matrix_are_not_waived(self):
        with patch.object(rule.predecessor_reader, "validate") as validator:
            for pin in (None, "", "a" * 63, "A" * 64):
                with self.subTest(pin=pin), self.assertRaises(VisualError):
                    rule._derive(self.session, self.aid, pin, rule.report.Inputs())
            validator.assert_not_called()
        with self.fixture_gates():
            with self.assertRaises(VisualError):
                rule._derive(self.session, self.aid, "0" * 64, rule.report.Inputs())
            self.proof["record_count"] = 127
            with self.assertRaises(VisualError):
                self.derive()
            self.proof["record_count"] = 128
            write_json(self.aid / "attestation.json", {"accepted": True})
            with self.assertRaises(VisualError):
                self.derive()

    def test_fresh_output_must_not_be_inside_session_or_predecessor(self):
        for output in (self.session / "diagnostic", self.aid / "diagnostic", self.root):
            with self.subTest(output=output), self.assertRaises(VisualError):
                rule.run(self.session, self.aid, self.pin, output)

    def test_append_only_roundtrip_byte_receipt_and_archive_tamper(self):
        output = self.root / "diagnostic"
        with self.fixture_gates():
            summary = rule.run(self.session, self.aid, self.pin, output)
            self.assertEqual(rule.validate(output), summary)
            self.assertEqual(summary["reclassified_window_count"], 36)
            self.assertFalse(summary["raw_acceptance_granted"])
            value = read_json(output / "result.json")
            names = {Path(name).name for name in value["code_snapshot"]}
            self.assertTrue({"phase3d_v2_panel_subframe_window_rule.py", "phase3d_v2_panel_event_identity_diagnostic.py",
                             "phase3d_v2_web_final_raw.py", "phase3d_v2_rollup_report.py"} <= names)
            archive = next(iter(value["code_snapshot"].values()))
            path = output / archive["relative_path"]
            path.write_bytes(path.read_bytes() + b"\n# tampered\n")
            with self.assertRaises(VisualError) as caught:
                rule.validate(output)
            self.assertEqual(caught.exception.code, "source_changed")
            with self.assertRaises(VisualError) as caught:
                rule.run(self.session, self.aid, self.pin, output)
            self.assertEqual(caught.exception.code, "output_exists")


if __name__ == "__main__":
    unittest.main()
