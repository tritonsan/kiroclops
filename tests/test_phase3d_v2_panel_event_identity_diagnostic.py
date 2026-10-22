"""Small public-event seams; no captured matrix, image decode or acceptance."""
from copy import deepcopy
from contextlib import ExitStack, contextmanager, nullcontext
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kiroclops.visual.schema import VisualError, digest, file_sha256, write_json
from scripts import phase3d_v2_panel_event_identity_diagnostic as diagnostic

# The V3b reader integrates this correction, so the frozen V2 code this
# diagnostic transforms exists only in the V2 snapshot. These classes run there
# (tests/test_v2_snapshot_diagnostics.py); the integrated V3b rule is tested in
# tests/test_realistic_review_v3_rules.py.
FROZEN_V2_READER = not hasattr(diagnostic.review, "canonical_event_key")
V2_ONLY = "Bound to the frozen V2 reader; verified in the V2 snapshot."


def selection():
    return {"objects": [{"id": oid} for oid in ("handle", "bookmark", "star")],
        "relative_motion": [{"object_id": oid, "relative_to": "handle"}
                            for oid in ("bookmark", "star")]}


def bundle(role="selected"):
    """Independent raw points and observed samples of three visible glyphs."""
    public = selection()
    mapping = {"handle": "h", "bookmark": "b", "star": "s"}
    frames, rows, objects = [], [], []
    positions = {"handle": [], "bookmark": [], "star": []}
    for index in range(50):
        stamp = index * .05
        parent = 20. + max(0, min(index - 4, 6)) * 3.
        points = {"h": {"x": parent, "y": 10.},
                  "b": {"x": parent + 10. + max(0, min(index - 8, 6)) * 2., "y": 20.},
                  "s": {"x": parent + 30. + max(0, min(index - 10, 6)) * 2., "y": 30.}}
        frames.append({"index": index, "time_s": stamp, "sha256": str(index)})
        rows.append({"index": index, "time_s": stamp, "image_sha256": str(index), "points": points})
        for oid, glyph in mapping.items():
            positions[oid].append({"index": index, "time_s": stamp, **points[glyph],
                                   "valid": True, "width": 8., "height": 8.})
    for oid in mapping:
        objects.append({"id": oid, "frames": positions[oid]})
    inputs = {"provenance": "recorder_verified", "events": []}
    return {"record": {"id": "fixture-" + role, "role": role, "selection": public},
            "mapping": mapping, "active": {oid: [[0., frames[-1]["time_s"]]] for oid in mapping},
            "manifest": {"frames": frames, "inputs": inputs}, "raw": {"rows": rows},
            "observations": {"objects": objects, "selection": public, "inputs": inputs}}


class EventKeyTests(unittest.TestCase):
    def key(self, ids, parent="handle", public=None):
        return diagnostic.canonical_event_key({"type": "settled", "object_ids": ids,
                                               "relative_to_object_id": parent}, public or selection())

    def test_one_and_two_id_relative_representations_are_identical(self):
        self.assertEqual(self.key(["bookmark"]), self.key(["bookmark", "handle"]))
        self.assertEqual(self.key(["bookmark"]), ("settled", ("bookmark", "handle")))

    def test_absolute_single_identity_is_not_given_a_parent(self):
        self.assertEqual(self.key(["handle"], None), ("settled", ("handle",)))

    def test_self_foreign_duplicate_and_wrong_second_identity_are_rejected(self):
        for ids, parent in ((["handle"], "handle"), (["bookmark"], "foreign"),
                            (["bookmark", "bookmark"], "handle"), (["bookmark", "star"], "handle"),
                            (["bookmark", "handle", "handle"], "handle"), (["foreign"], "handle"),
                            (["bookmark", "handle"], None)):
            with self.subTest(ids=ids, parent=parent), self.assertRaises(VisualError):
                self.key(ids, parent)

    def test_undeclared_or_ambiguous_parent_contract_is_rejected(self):
        for relations in ([], [{"object_id": "bookmark", "relative_to": "star"}],
                          selection()["relative_motion"] * 2):
            public = {**selection(), "relative_motion": relations}
            with self.subTest(relations=relations), self.assertRaises(VisualError):
                self.key(["bookmark"], public=public)

    def test_malformed_identity_is_not_silently_deduplicated(self):
        for ids in (None, [], "bookmark", [1], [""]):
            with self.subTest(ids=ids), self.assertRaises(VisualError):
                self.key(ids)


@unittest.skipUnless(FROZEN_V2_READER, V2_ONLY)
class FrozenEventPipelineTests(unittest.TestCase):
    def test_frozen_failure_and_corrected_count_timing_order_from_same_points(self):
        frozen = diagnostic.review._final_events
        source_before = inspect.getsource(frozen)
        old, corrected, transform = diagnostic.compare_same_raw([bundle()], {"event_options": {"rest_duration_s": .2}})
        self.assertTrue(old[3], "The unmodified frozen reader must expose the doubled-parent seam.")
        self.assertTrue(all(row["code"] == "raw_consumed_event_count_mismatch" for row in old[3]))
        self.assertTrue(any(len(row["objects"]) == 3 for row in old[3]))
        self.assertEqual(corrected[3], [], "The genuine public event algorithms should agree after the identity-only fix.")
        self.assertGreater(len(corrected[0]), len(old[0]))
        self.assertTrue(all(row["pass"] for row in corrected[0]))
        self.assertTrue(any(row.get("relation_type") == "relative_event_timing" for row in corrected[0]))
        self.assertEqual(old[1:3], corrected[1:3])
        self.assertEqual(transform["replacement_count"], 1)
        self.assertFalse(transform["frozen_module_mutated"])
        self.assertIs(diagnostic.review._final_events, frozen)
        self.assertEqual(inspect.getsource(frozen), source_before)

    def test_a_real_raw_produced_motion_difference_is_still_rejected(self):
        value = bundle()
        child = value["observations"]["objects"][1]["frames"]
        for frame in child:
            # A real observation discrepancy, not a failure-code intervention.
            frame["x"] += max(0, min(frame["index"] - 27, 6)) * 3.
        old, corrected, _ = diagnostic.compare_same_raw([value], {})
        self.assertEqual(old[1], corrected[1])
        self.assertTrue(corrected[3])
        self.assertTrue(any(row["code"] in diagnostic.EVENT_FAILURE_CODES for row in corrected[3]))

    def test_original_role_remains_diagnostic_and_unread_by_motion_gate(self):
        value = bundle("original")
        value["observations"] = None
        old, corrected, _ = diagnostic.compare_same_raw([value], {})
        self.assertEqual(old, corrected)
        self.assertEqual(corrected[0], [])
        self.assertEqual(corrected[3], [])
        self.assertEqual(corrected[2][0]["scope"], "pixel_geometry_only")

    def test_the_ast_transform_refuses_an_unrecognized_frozen_method(self):
        with patch.object(diagnostic.inspect, "getsource", return_value="def _final_events(bundles, contract):\n    return []\n"):
            with self.assertRaises(VisualError) as caught:
                diagnostic.corrected_event_method()
        self.assertEqual(caught.exception.code, "unsupported_event_reader")


class ReuseBoundaryTests(unittest.TestCase):
    def test_no_decode_loader_reuses_official_hash_verifier_and_restores_aliases(self):
        modules = (diagnostic.package, diagnostic.evidence, diagnostic.prepared_evidence,
                   diagnostic.media_suite, diagnostic.suite_evidence, diagnostic.review,
                   diagnostic.review.legacy, diagnostic.review.painted, diagnostic.review.generic)
        aliases = {module: module.load_package for module in modules if hasattr(module, "load_package")}
        with patch.object(diagnostic, "ORIGINAL_LOAD_PACKAGE", wraps=diagnostic.ORIGINAL_LOAD_PACKAGE) as loader:
            # The integrity guard intentionally rejects replacing the frozen loader.
            with self.assertRaises(VisualError):
                with diagnostic.no_png_decode():
                    pass
        for module, function in aliases.items():
            self.assertIs(module.load_package, function)

    def test_no_decode_guard_blocks_pixel_decode_and_restores_after_failure(self):
        aliases = {module: module.load_package for module in (diagnostic.package,
            diagnostic.evidence, diagnostic.prepared_evidence, diagnostic.media_suite)}
        before = diagnostic.package.cv2.imdecode
        with self.assertRaises(VisualError) as caught:
            with diagnostic.no_png_decode():
                diagnostic.prepared_evidence.load_package("unused", verify=False)
        self.assertEqual(caught.exception.code, "package_integrity_failure")
        for module, function in aliases.items():
            self.assertIs(module.load_package, function)
        self.assertIs(diagnostic.package.cv2.imdecode, before)
        with diagnostic.no_png_decode():
            with self.assertRaises(VisualError) as caught:
                diagnostic.package.cv2.imdecode(None, 1)
        self.assertEqual(caught.exception.code, "png_decode_forbidden")

    def test_no_decode_verifies_real_package_hash_header_without_decoding(self):
        import struct
        import zlib
        with TemporaryDirectory() as temporary:
            path = Path(temporary)
            header = b"IHDR" + struct.pack(">IIBBBBB", 2, 3, 8, 2, 0, 0, 0)
            png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + header + struct.pack(">I", zlib.crc32(header) & 0xffffffff)
            (path / "frames").mkdir()
            frames = []
            for index in range(2):
                item = path / "frames" / f"{index:06}.png"
                item.write_bytes(png)
                frames.append({"index": index, "time_s": index * .05,
                               "path": f"frames/{index:06}.png", "sha256": file_sha256(item)})
            value = {"kind": "VideoPackage", "schema_version": 1, "frames": frames,
                     "image_size": [2, 3], "inputs": {"provenance": "unknown", "events": []}}
            value["package_sha256"] = digest(value)
            write_json(path / "manifest.json", value)
            with diagnostic.no_png_decode():
                self.assertEqual(diagnostic.prepared_evidence.load_package(path), value)
                frames_path = path / frames[0]["path"]
                frames_path.write_bytes(png + b"tampered")
                with self.assertRaises(VisualError):
                    diagnostic.evidence.load_package(path)

    def test_external_receipt_pin_is_required_before_predecessor_validation(self):
        with patch.object(diagnostic.predecessor_reader, "validate") as validator:
            for pin in (None, "", "a" * 63, "A" * 64):
                with self.subTest(pin=pin), self.assertRaises(VisualError):
                    diagnostic._derive(Path("unused"), Path("unused"), pin, diagnostic.report.Inputs())
            validator.assert_not_called()

    def test_fresh_output_must_not_be_inside_session_or_predecessor(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session, aid = root / "session", root / "aid"
            session.mkdir(); aid.mkdir()
            for output in (session / "diagnostic", aid / "diagnostic", root):
                with self.subTest(output=output), self.assertRaises(VisualError):
                    diagnostic.run(session, aid, "a" * 64, output)


class DiagnosticReceiptTests(unittest.TestCase):
    """Mock only canonical matrix validation; never assert real evidence passed."""
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session, self.aid = self.root / "session", self.root / "aid"
        self.session.mkdir(); self.aid.mkdir()
        self.context = {"event_contract": {}}
        self.records = [{"id": f"row-{index:03}", "package": self.root / f"package-{index}"}
                        for index in range(128)]
        self.old_failure = {"code": "raw_consumed_event_count_mismatch", "objects": ["bookmark", "handle", "handle"]}
        self.windows = [{"code": "raw_motion_window_unqualified", "reason": "expected_active_interval_unsampled",
                         "record_id": f"row-{index:03}", "object_id": "bookmark"} for index in range(36)]
        self.old = ([{"old_matched_event": True}], [{"independent_prediction": 1}],
                    [{"role_relevance": True}], [self.old_failure])
        self.corrected = ([{"new_matched_event": True}], self.old[1], self.old[2], [])
        self.old_result = {"context": self.context, "event_checks": self.old[0],
            "event_relevance": self.old[2], "failures": self.windows + self.old[3]}
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

    @contextmanager
    def fixture_gates(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(diagnostic.predecessor_reader, "validate", return_value=self.proof))
            stack.enter_context(patch.object(diagnostic.predecessor_reader, "_controller", side_effect=nullcontext))
            stack.enter_context(patch.object(diagnostic.review, "_session_context", return_value=(self.context, self.records)))
            stack.enter_context(patch.object(diagnostic.review, "_records", side_effect=lambda rows, *args: rows))
            stack.enter_context(patch.object(diagnostic, "_raw_bundle", side_effect=lambda record, *args: {"record": record}))
            stack.enter_context(patch.object(diagnostic.review, "_measurement", side_effect=lambda value, inventory:
                inventory.update({str(self.aid / "result.json"): file_sha256(self.aid / "result.json")})))
            stack.enter_context(patch.object(diagnostic, "compare_same_raw", return_value=(self.old, self.corrected, {"fixture_transform": True})))
            yield

    def derive(self):
        return diagnostic._derive(self.session, self.aid, self.pin, diagnostic.report.Inputs())

    def test_all_36_unsampled_failures_and_fail_axes_remain_after_key_only_correction(self):
        with self.fixture_gates():
            result = self.derive()
        self.assertEqual(result["unchanged_other_failures"], self.windows)
        self.assertEqual(result["remaining_failures"], self.windows)
        self.assertEqual(result["remaining_failure_count"], 36)
        self.assertEqual(result["numeric_status"], "fail")
        for key in ("raw_acceptance_granted", "native_acceptance_granted", "source_acceptance_granted",
                    "human_inspection_claimed", "full_behavior_acceptance", "new_recording_or_model_started"):
            self.assertIs(result[key], False)

    def test_external_pin_mismatch_and_fresh_predecessor_validation_failure_are_not_waived(self):
        with self.fixture_gates():
            with self.assertRaises(VisualError):
                diagnostic._derive(self.session, self.aid, "0" * 64, diagnostic.report.Inputs())
            with patch.object(diagnostic.predecessor_reader, "validate", side_effect=VisualError("source_changed", "actual byte gate")):
                with self.assertRaises(VisualError) as caught:
                    self.derive()
        self.assertEqual(caught.exception.code, "source_changed")

    def test_incomplete_or_attested_or_wrong_context_matrix_is_rejected(self):
        with self.fixture_gates():
            self.proof["record_count"] = 127
            with self.assertRaises(VisualError):
                self.derive()
            self.proof["record_count"] = 128
            self.context = {"event_contract": {"altered": True}}
            with patch.object(diagnostic.review, "_session_context", return_value=(self.context, self.records)):
                with self.assertRaises(VisualError):
                    self.derive()
            write_json(self.aid / "attestation.json", {"accepted": True})
            with self.assertRaises(VisualError):
                self.derive()

    def test_new_event_disagreement_survives_and_stale_old_replay_is_rejected(self):
        failure = {"code": "raw_consumed_event_disagreement", "difference_s": .5}
        self.corrected[3].append(failure)
        with self.fixture_gates():
            result = self.derive()
            self.assertEqual(result["remaining_failures"], self.windows + [failure])
            self.old_result["event_checks"] = []
            self.write_fixture()
            with self.assertRaises(VisualError) as caught:
                self.derive()
        self.assertEqual(caught.exception.code, "stale_predecessor")

    def test_unrelated_failure_cannot_be_removed_by_this_diagnostic(self):
        self.old_result["failures"].append({"code": "raw_body_geometry_unqualified"})
        self.write_fixture()
        with self.fixture_gates(), self.assertRaises(VisualError) as caught:
            self.derive()
        self.assertEqual(caught.exception.code, "unsupported_predecessor_failure")

    def test_append_only_roundtrip_byte_receipt_and_archive_tamper(self):
        output = self.root / "diagnostic"
        with self.fixture_gates():
            summary = diagnostic.run(self.session, self.aid, self.pin, output)
            self.assertEqual(diagnostic.validate(output), summary)
            self.assertEqual(summary["remaining_failure_count"], 36)
            self.assertFalse(summary["raw_acceptance_granted"])
            from kiroclops.visual.schema import read_json
            value = read_json(output / "result.json")
            archive = next(iter(value["code_snapshot"].values()))
            path = output / archive["relative_path"]
            path.write_bytes(path.read_bytes() + b"\n# tampered\n")
            with self.assertRaises(VisualError) as caught:
                diagnostic.validate(output)
            self.assertEqual(caught.exception.code, "source_changed")
            with self.assertRaises(VisualError) as caught:
                diagnostic.run(self.session, self.aid, self.pin, output)
            self.assertEqual(caught.exception.code, "output_exists")


if __name__ == "__main__":
    unittest.main()
