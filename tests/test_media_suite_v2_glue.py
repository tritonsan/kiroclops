"""V2 orchestration receipts over real temporary PNGs, without applications."""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.cloud.results import envelope, legacy_result, make_measurement, request_binding
from kiroclops.local_engine import execution_policy, media_suite, realistic_policy, suite_evidence, suite_revision, suite_web_v2
from kiroclops.local_engine.session import Session, source_hashes
from kiroclops.visual.events import extract_events
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.web_local.capture import project_hashes


class GlueFixture:
    def __init__(self, root):
        self.root = root
        self.original = self.project(root / "original", "original")
        self.parent = self.project(root / "parent", "parent")
        self.reference = self.project(root / "reference", "reference")
        self.canonical = next(s for s in suite_web_v2.scenarios("panel", "calibration") if s["recipe"] == "interrupt")
        self.frames = []
        for index in range(3):
            image = root / (str(index) + ".png")
            cv2.imencode(".png", np.full((8, 8, 3), index * 30, np.uint8))[1].tofile(image)
            self.frames.append(image)
        points = [{"object_id": "handle", "frame_sha256": digest(["control", index]), "error_px": 1.,
                   "role": "reference" if index % 2 == 0 else "original", "split": "calibration", "independent": True}
                  for index in range(20)]
        self.policy = realistic_policy.build_policy("panel", {"status": "pass", "basis": "independent_raw_pixels", "points": points})
        self.execution_policy = execution_policy.resolve_policy(execution_policy.REVISED_ID, "panel")
        self.sources = {"kiroclops/visual/tracking.py": digest("fixture tracker"),
                        "kiroclops/visual/events.py": digest("fixture events")}
        identity = {"opencv_version": "fixture", "threads": 1}
        self.engine = {"identity": identity, "engine_sha256": digest(identity)}

    @staticmethod
    def project(path, content):
        (path / "src").mkdir(parents=True)
        (path / "index.html").write_text("<html>" + content + "</html>", encoding="utf-8")
        (path / "src/panel-motion.js").write_text(content, encoding="utf-8")
        return path

    def record(self, folder, project=None, *, reference_record=None):
        project = project or self.reference
        canonical = deepcopy(self.canonical)
        if reference_record is not None:
            execution, replay = realistic_policy.replay_scenario(canonical, reference_record["observations"])
            actual = deepcopy(reference_record["observations"]["inputs"]["events"])
            for event in actual:
                event["time_s"] += .008
        else:
            execution, replay = deepcopy(canonical), None
            actual = deepcopy(canonical["events"])
            actual[0]["time_s"] += .019
            actual[1]["time_s"] += .040
            for index, event in enumerate(actual):
                event.update(client_x=100. + index * 10., client_y=120.)
        manifest = create_package(folder / "package", self.frames, [0., .4, .8],
            {"schema_version": 1, "kind": "InputTimeline", "provenance": "recorder_verified", "events": actual},
            {"scenario": execution, "source_hashes": project_hashes(project)})
        selection = {"objects": [{"id": "handle", "roi": [0, 0, 4, 4]}]}
        observed = {"schema_version": 1, "kind": "ObservationSet", "package_sha256": manifest["package_sha256"],
            "image_size": manifest["image_size"], "status": "ok", "measurement_status": "pass",
            "inputs": manifest["inputs"], "selection": selection,
            "objects": [{"id": "handle", "frames": [{"index": index, "time_s": frame["time_s"], "valid": True,
                "x": 2., "y": 2., "width": 4., "height": 4.} for index, frame in enumerate(manifest["frames"])]}]}
        event_contract = suite_web_v2.event_contract("panel")
        events = extract_events(observed, selection, event_contract)
        analysis = {"kind": "AnalysisResult", "package_sha256": manifest["package_sha256"],
                    "candidate_sha256": digest(project_hashes(project)), "observations": observed, "events": events,
                    "provenance": {"observation_sha256": digest(observed)}}
        for name, value in (("analysis", analysis), ("observations", observed), ("events", events), ("selection", selection)):
            write_json(folder / "measurement" / (name + ".json"), value)
        record = {"scenario": canonical, "execution_scenario": execution, "package": str(folder / "package"),
                  "analysis": str(folder / "measurement/analysis.json"), "observations": observed, "events": events,
                  "selection": selection, "source_sha256": digest(project_hashes(project)), "measurement_status": "ok",
                  "functional": {"status": "pass"}, "suite_event_contract": event_contract,
                  "independent_review_contract": deepcopy(event_contract), "event_contract_sha256": digest(event_contract)}
        if replay is not None:
            record["input_replay"] = replay
        return record

    def create_suite(self):
        record = self.record(self.root / "reference-record")
        return media_suite.create_suite(self.root / "suite", domain="web_panel", stage="panel",
            records=[{"case_id": self.canonical["case_id"], "role": "calibration", "stage": "panel", "record": record}],
            required_cases=[{"case_id": self.canonical["case_id"], "role": "calibration"}],
            original=self.original, authoring_parent=self.parent, goal="Match the observed panel",
            event_contract=suite_web_v2.event_contract("panel"), acceptance_policy=self.policy,
            execution_policy=self.execution_policy)

    def worker_record(self):
        record = self.record(self.root / "worker-record", self.parent)
        contract = suite_web_v2.event_contract("panel")
        request = request_binding(record["observations"]["package_sha256"], record["selection"], contract, record["source_sha256"])
        delivery = envelope(make_measurement(record["observations"], record["events"]), request, logical_job_id="worker1",
                            engine=self.engine, sources=self.sources, timings={"execution_wall_s": 0.})
        result = legacy_result(delivery, request, logical_job_id="worker1")
        destination = Path(record["analysis"]).parent
        write_json(record["analysis"], result)
        write_json(destination / "analysis-operation.json", {"job_id": "worker1", "status": "succeeded", "request_sha256": digest(request)})
        write_json(destination / "jobs/jobs/worker1/state.json", {"status": "succeeded", "request_sha256": digest(request),
            "result_sha256": digest(result), "delivery_sha256": digest(delivery), "started_at": time.time() - 1, "finished_at": time.time()})
        write_json(destination / "jobs/jobs/worker1/request.json", request)
        write_json(destination / "jobs/jobs/worker1/delivery-v2.json", delivery)
        return record


class V2GlueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kc-v2-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = GlueFixture(self.root)
        # Freeze a unit-test source epoch against concurrent engineering work.
        self.analysis_sources = media_suite.analysis_source_hashes(2)
        identity = patch.object(media_suite, "analysis_source_hashes", return_value=self.analysis_sources)
        identity.start()
        self.addCleanup(identity.stop)
        for target in ("kiroclops.local_engine.evaluation._capture_web", "kiroclops.local_engine.suite_game.capture_target"):
            no_capture = patch(target, side_effect=AssertionError("Unit glue tests may not capture applications"))
            no_capture.start()
            self.addCleanup(no_capture.stop)

    def test_new_suite_keeps_policy_source_epoch_and_game_limits(self):
        value = self.fixture.create_suite()
        self.assertEqual(2, value["schema_version"])
        self.assertEqual(self.fixture.policy, value["acceptance_policy"])
        self.assertEqual(self.fixture.execution_policy, value["execution_policy"])
        self.assertEqual(digest(self.analysis_sources), value["source_epoch"])
        self.assertEqual(.15, value["limits"]["game_curve_error_bodies"])
        self.assertEqual(.10, value["limits"]["game_height_difference_fraction"])
        self.assertIsNone(value["limits"]["required_improvement_fraction"])
        self.assertEqual(.05, value["limits"]["web_curve_error_fraction"])
        self.assertEqual(value, media_suite.load_suite(self.root / "suite"))

    def test_session_policy_stamp_uses_canonical_policy_digest(self):
        self.fixture.create_suite()
        runtime = {"capture_lock": str(self.root / "capture.lock"), "environment": {}, "driver": {}}
        with patch("kiroclops.local_engine.launcher.configure", return_value={}) as configure:
            media_suite.create_suite_session(self.root / "s", self.root / "suite", self.fixture.parent, runtime=runtime)
        configure.assert_called_once()
        state = Session(self.root / "s")._load()
        self.assertEqual(2, state["schema_version"])
        self.assertEqual(self.fixture.policy["policy_sha256"], state["experiment"]["acceptance_policy_sha256"])
        self.assertEqual(self.fixture.execution_policy, state["experiment"]["execution_policy"])
        self.assertEqual({"max_candidates": 3, "max_seconds": 3600, "max_validation_seconds": 3600}, state["budget"])
        self.assertEqual("new", state["status"])
        self.assertIsNone(state["started_at"])

    def test_source_epoch_change_rejects_saved_suite(self):
        self.fixture.create_suite()
        changed = deepcopy(self.analysis_sources)
        changed["kiroclops/local_engine/suite_web_v2.py"] = digest("changed comparator source")
        with patch.object(media_suite, "analysis_source_hashes", return_value=changed):
            with self.assertRaises(VisualError) as error:
                media_suite.load_suite(self.root / "suite")
        self.assertEqual("analysis_source_changed", error.exception.code)

    def test_canonical_case_mutation_rejected_even_if_manifest_binding_rehashed(self):
        self.fixture.create_suite()
        value = read_json(self.root / "suite/suite.json")
        value["cases"][0]["scenario"]["events"][-1]["time_s"] += .01
        write_json(self.root / "suite/suite.json", value)
        write_json(self.root / "suite/suite-binding.json", {"suite_sha256": digest(value)})
        with self.assertRaises(VisualError) as error:
            media_suite.load_suite(self.root / "suite")
        self.assertEqual("suite_changed", error.exception.code)

    def test_policy_rule_change_rejected_even_with_all_digests_rehashed(self):
        self.fixture.create_suite()
        value = read_json(self.root / "suite/suite.json")
        value["acceptance_policy"]["rules"]["curve_fraction"] = .20
        payload = {k: v for k, v in value["acceptance_policy"].items() if k != "policy_sha256"}
        value["acceptance_policy"]["policy_sha256"] = digest(payload)
        write_json(self.root / "suite/suite.json", value)
        write_json(self.root / "suite/suite-binding.json", {"suite_sha256": digest(value)})
        with self.assertRaises(VisualError) as error:
            media_suite.load_suite(self.root / "suite")
        self.assertEqual("invalid_acceptance_policy", error.exception.code)

    def test_real_replay_package_is_checked_with_execution_recipe(self):
        reference = self.fixture.record(self.root / "reference-record")
        actual = self.fixture.record(self.root / "replayed-record", self.fixture.parent, reference_record=reference)
        self.assertNotEqual(actual["scenario"], actual["execution_scenario"])
        case = {"stage": "panel", "scenario": actual["scenario"]}
        suite = {"schema_version": 2, "domain": "web_panel", "event_contract": suite_web_v2.event_contract("panel")}
        expected = {"kind": "web", "sources": project_hashes(self.fixture.parent)}
        binding = suite_evidence._binding(actual, case, suite, expected_source=expected)
        self.assertEqual((load_package(actual["package"])["package_sha256"], digest(actual["observations"])), binding)

    def test_real_package_rejects_different_execution_recipe(self):
        reference = self.fixture.record(self.root / "reference-record")
        actual = self.fixture.record(self.root / "replayed-record", self.fixture.parent, reference_record=reference)
        actual["execution_scenario"]["events"][-1]["time_s"] += .010
        actual["input_replay"]["execution_scenario_sha256"] = digest(actual["execution_scenario"])
        case = {"stage": "panel", "scenario": actual["scenario"]}
        suite = {"schema_version": 2, "domain": "web_panel", "event_contract": suite_web_v2.event_contract("panel")}
        with self.assertRaises(VisualError) as error:
            suite_evidence._binding(actual, case, suite, expected_source={"kind": "web", "sources": project_hashes(self.fixture.parent)})
        self.assertEqual("incomplete_review", error.exception.code)

    def test_real_package_rejects_mutated_physical_pointer_execution(self):
        reference = self.fixture.record(self.root / "reference-record")
        actual = self.fixture.record(self.root / "replayed-record", self.fixture.parent, reference_record=reference)
        actual["execution_scenario"]["events"][0]["replay_client_point"][0] += 5.
        actual["input_replay"]["execution_scenario_sha256"] = digest(actual["execution_scenario"])
        case = {"stage": "panel", "scenario": actual["scenario"]}
        suite = {"schema_version": 2, "domain": "web_panel", "event_contract": suite_web_v2.event_contract("panel")}
        with self.assertRaises(VisualError) as error:
            suite_evidence._binding(actual, case, suite, expected_source={"kind": "web", "sources": project_hashes(self.fixture.parent)})
        self.assertEqual("incomplete_review", error.exception.code)

    def test_measurement_receipt_detects_changed_temporary_source_bytes(self):
        source = self.root / "tracker_fixture.py"
        source.write_text("original tracker", encoding="utf-8")
        def sources(stage):
            return {**self.fixture.sources, "tracker_fixture.py": file_sha256(source)}
        snapshot = sources("panel")
        record = self.fixture.worker_record()
        with patch.object(suite_revision, "measurement_source_hashes", side_effect=sources), \
             patch.object(suite_revision, "service_sources", return_value=self.fixture.sources), \
             patch.object(suite_revision, "engine_identity", return_value=self.fixture.engine):
            stamped = suite_revision.stamp_new_measurement(record, "panel", expected_sources=snapshot, capture_started_at=time.time() - 2)
            suite_revision.verify_measurement_record(stamped, "panel", expected_sources=snapshot)
            source.write_text("changed tracker", encoding="utf-8")
            with self.assertRaises(VisualError) as error:
                suite_revision.verify_measurement_record(stamped, "panel", expected_sources=sources("panel"))
        self.assertEqual("analysis_source_changed", error.exception.code)

    def test_both_measurement_and_experiment_closures_include_new_modules(self):
        observed = suite_revision.measurement_source_hashes("panel")
        for name in ("realistic_policy", "suite_web_v2", "execution_policy"):
            relative = "kiroclops/local_engine/" + name + ".py"
            self.assertIn(relative, observed)
            self.assertIn(relative, self.analysis_sources)
        self.assertIn("kiroclops/local_engine/realistic_review.py", self.analysis_sources)

    def calibration_bundle(self):
        from scripts import phase3d_v2_prepare
        previous = self.root / "calibration"
        reference = self.fixture.record(self.root / "review-reference")
        original = self.fixture.record(self.root / "review-original", self.fixture.original, reference_record=reference)
        parent = self.fixture.record(self.root / "review-before", self.fixture.parent, reference_record=reference)
        saved = {"stage": "panel", "records": [{"case_id": self.fixture.canonical["case_id"],
                  "role": "calibration", "purpose": "primary", "stage": "panel", "source_role": role,
                  "scenario": deepcopy(self.fixture.canonical), "record": record}
                  for role, record in (("reference", reference), ("original", original), ("before", parent))]}
        write_json(previous / "calibration.json", saved)
        write_json(previous / "start-binding.json", {"parent": str(self.fixture.parent), "original": str(self.fixture.original)})
        rows = phase3d_v2_prepare.review_records(previous)
        reviewed = {"phase": "calibration", "records": [{"role": row["role"], "case_id": row["case_id"],
            "split_role": row["split_role"], "purpose": row["purpose"],
            "package_sha256": load_package(row["record"]["package"])["package_sha256"],
            "observations_sha256": digest(row["record"]["observations"]),
            "source_sha256": row["record"]["source_sha256"], "selection_sha256": digest(row["record"]["selection"])} for row in rows]}
        return previous, reviewed

    def test_calibration_review_binds_exact_current_three_source_roles(self):
        from scripts import phase3d_v2_prepare
        previous, reviewed = self.calibration_bundle()
        with patch("kiroclops.local_engine.realistic_review.validate_review", return_value=reviewed):
            returned = phase3d_v2_prepare.verify_calibration_review(previous, self.root / "review")
        self.assertEqual(reviewed, returned)

    def test_foreign_calibration_review_cannot_set_current_policy_floor(self):
        from scripts import phase3d_v2_prepare
        previous, reviewed = self.calibration_bundle()
        for field in ("package_sha256", "observations_sha256", "source_sha256", "selection_sha256"):
            with self.subTest(field=field):
                foreign = deepcopy(reviewed)
                foreign["records"][0][field] = digest("another passing calibration")
                with patch("kiroclops.local_engine.realistic_review.validate_review", return_value=foreign):
                    with self.assertRaises(VisualError) as error:
                        phase3d_v2_prepare.verify_calibration_review(previous, self.root / "review")
                self.assertEqual("wrong_result", error.exception.code)

    def test_missing_or_relabeled_calibration_role_is_rejected(self):
        from scripts import phase3d_v2_prepare
        previous, reviewed = self.calibration_bundle()
        bad = [deepcopy(reviewed), deepcopy(reviewed), deepcopy(reviewed)]
        bad[0]["records"].pop()
        bad[1]["records"][0]["split_role"] = "validation"
        bad[2]["records"][0]["role"] = "before"
        for changed in bad:
            with patch("kiroclops.local_engine.realistic_review.validate_review", return_value=changed):
                with self.assertRaises(VisualError) as error:
                    phase3d_v2_prepare.verify_calibration_review(previous, self.root / "review")
            self.assertEqual("wrong_result", error.exception.code)


if __name__ == "__main__":
    unittest.main()
