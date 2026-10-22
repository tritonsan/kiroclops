"""Complete source-bound suite matrices, using local PNGs and no capture."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import suite_evidence as se, suite_web
from kiroclops.local_engine.media_suite import create_suite, clone_record, record_from_suite, analysis_source_hashes
from kiroclops.local_engine.prepared_evidence import store_prepared
from kiroclops.local_engine.session import JOB_BINDING_FIELDS, source_hashes
from kiroclops.visual.events import extract_events
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.web_local.capture import project_hashes


class SuiteFixture:
    def __init__(self, root):
        self.root = root
        self.image = root / "image.png"
        cv2.imencode(".png", np.zeros((4, 4, 3), np.uint8))[1].tofile(self.image)
        self.parent = self.project(root / "baseline", "parent")
        self.original = self.project(root / "original", "original")
        self.selected = self.project(root / "candidates/c1/source", "selected")
        self.reference_source = self.project(root / "reference-source", "reference")
        self.suite_root = root / "suite"
        descriptors = []
        for role, split in (("calibration", "calibration"), ("regression", "known_regression"), ("validation", "validation")):
            scenario = suite_web.scenarios("panel", split)[0]
            name = scenario["case_id"]
            record = self.record(root / "input" / name, scenario, self.reference_source)
            descriptors.append({"case_id": name, "role": role, "stage": "panel", "purpose": "primary", "record": record})
        required = [{key: item[key] for key in ("case_id", "role", "stage", "purpose")} for item in descriptors]
        self.suite = create_suite(self.suite_root, domain="web_panel", stage="panel", records=descriptors,
            required_cases=required, original=self.original, authoring_parent=self.parent,
            goal="Match these visible responses", event_contract=suite_web.event_contract("panel"))
        self.state = {"session_id": "unit", "domain": "web_panel", "mode": "tools", "baseline": str(self.parent),
            "baseline_hashes": source_hashes(self.parent), "reference": str(self.suite_root), "reference_hashes": source_hashes(self.suite_root),
            "experiment": {"reference_mode": "media_suite", "suite_sha256": digest(self.suite), "stage": "panel"},
            "prepare_job_id": "prepare", "final_job_id": "sealed", "sealed_at": 1., "selected_candidate_id": "c1",
            "candidates": [{"candidate_id": "c1", "job_id": "chosen", "status": "succeeded", "eligible": True,
                            "source_sha256": digest(source_hashes(self.selected))},
                           {"candidate_id": "c2", "job_id": "foreign", "status": "succeeded"}],
            "final": {"status": "succeeded", "job_id": "sealed", "pass": True}}
        self.expected = set()
        cal = [c for c in self.suite["cases"] if c["role"] == "calibration"]
        val = [c for c in self.suite["cases"] if c["role"] != "calibration"]
        for job, role, cases in ([("prepare", r, cal) for r in ("reference", "original", "before")]
                                 + [("chosen", "candidate", cal)]
                                 + [("sealed", r, val) for r in ("reference", "original", "before", "final")]):
            records = []
            for case in cases:
                folder = root / "measurements" / job / ("candidate" if role == "final" else role) / case["case_id"]
                if role == "reference":
                    record = clone_record(record_from_suite(self.suite_root, case), folder)
                else:
                    project = {"original": self.suite_root / "comparison-original", "before": self.parent,
                               "candidate": self.selected, "final": self.selected}[role]
                    record = self.record(folder, case["scenario"], project)
                records.append(record)
                self.expected.add((record["observations"]["package_sha256"], digest(record["observations"])))
            write_json(root / "measurements" / job / (role + ".json"), records)
        self.results = {
            "prepare": {"suite_sha256": digest(self.suite)},
            "chosen": {"suite_sha256": digest(self.suite), "eligible": True, "comparisons": self.comparisons(cal)},
            "sealed": {"suite_sha256": digest(self.suite), "pass": True, "heldout_status": "provided", "comparisons": self.comparisons(val)},
        }
        for name, operation in (("prepare", "prepare"), ("chosen", "evaluate"), ("sealed", "finalize")):
            self.job(name, operation)
        indexes = {role: root / "measurements/prepare" / (role + ".json") for role in ("reference", "original", "before")}
        receipt = store_prepared(root, read_json(root / "jobs/prepare/job.json"), self.state,
                                 {role: str(path) for role, path in indexes.items()}, indexes)
        self.results["prepare"]["prepared_evidence_sha256"] = receipt
        self.job("prepare", "prepare")

    @staticmethod
    def project(path, content):
        path.mkdir(parents=True)
        (path / "index.html").write_text("<html>" + content + "</html>", encoding="utf-8")
        (path / "src").mkdir()
        (path / "src/panel-motion.js").write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def comparisons(cases):
        return [{"case_id": c["case_id"], "role": c["role"], "purpose": c["purpose"], "pass": True} for c in cases]

    def record(self, folder, scenario, project):
        hashes = project_hashes(project)
        manifest = create_package(folder / "package", [self.image] * 3, [0., .1, .2],
            {"provenance": "recorder_verified", "events": []}, {"scenario": scenario, "source_hashes": hashes})
        selection = {"objects": [{"id": "handle", "roi": [0, 0, 2, 2]}]}
        observed = {"schema_version": 1, "kind": "ObservationSet", "package_sha256": manifest["package_sha256"],
            "image_size": [4, 4], "inputs": manifest["inputs"], "selection": selection,
            "objects": [{"id": "handle", "frames": [{"index": i, "time_s": i / 10, "valid": True, "x": 1., "y": 1., "width": 2., "height": 2.}
                                                     for i in range(3)]}]}
        policy = suite_web.event_contract("panel")
        events = extract_events(observed, selection, policy)
        analysis = {"kind": "AnalysisResult", "package_sha256": manifest["package_sha256"], "candidate_sha256": digest(hashes),
                    "observations": observed, "events": events, "provenance": {"observation_sha256": digest(observed)}}
        for name, value in (("analysis", analysis), ("observations", observed), ("selection", selection), ("events", events)):
            write_json(folder / "measurement" / (name + ".json"), value)
        return {"scenario": deepcopy(scenario), "package": str(folder / "package"), "analysis": str(folder / "measurement/analysis.json"),
                "observations": observed, "events": events, "selection": selection, "source_sha256": digest(hashes), "measurement_status": "ok",
                "suite_event_contract": deepcopy(policy), "independent_review_contract": deepcopy(policy), "event_contract_sha256": digest(policy)}

    def job(self, name, operation):
        selected = name != "prepare"
        project = self.selected if selected else self.parent
        hashes = source_hashes(project)
        result = self.results[name]
        job = {"schema_version": 1, "job_id": name, "operation": operation, "created_at": 0., "status": "succeeded",
               "candidate_id": "c1" if selected else None, "source_sha256": digest(hashes), "source_hashes": hashes,
               "source_path": str(project), "result_sha256": digest(result)}
        write_json(self.root / "jobs" / name / "job.json", job)
        write_json(self.root / "jobs" / name / "result.json", result)
        write_json(self.root / "jobs" / name / "binding.json", {"sha256": digest({k: job[k] for k in JOB_BINDING_FIELDS})})

    def check(self, complete=True):
        return se.required_bindings(self.root, self.state, require_complete=complete)


class CompleteSuiteEvidenceTests(unittest.TestCase):
    def setUp(self):
        # Agents implement separate modules concurrently. This test exercises
        # data/source receipts against one fixed analysis-code identity; code
        # fingerprint mutation itself belongs to media_suite's own tests.
        code = patch("kiroclops.local_engine.media_suite.analysis_source_hashes", return_value=analysis_source_hashes())
        code.start()
        self.addCleanup(code.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = SuiteFixture(self.root)

    def rejected(self, fn=None):
        with self.assertRaises(VisualError) as caught:
            (fn or self.fixture.check)()
        self.assertEqual("incomplete_review", caught.exception.code)

    def test_all_four_source_roles_and_dynamic_cases_are_required(self):
        self.assertEqual(12, len(self.fixture.expected))
        self.assertEqual(self.fixture.expected, self.fixture.check())
        write_json(self.root / "measurements/foreign/candidate.json", [{"garbage": "unselected"}])
        self.assertEqual(self.fixture.expected, self.fixture.check())

    def test_partial_display_does_not_claim_completed_or_sealed_acceptance(self):
        self.fixture.state["sealed_at"] = None
        self.fixture.state["final"] = None
        (self.root / "measurements/sealed/final.json").unlink()
        self.assertEqual(10, len(self.fixture.check(False)))
        self.rejected()

    def test_partial_display_does_not_adopt_an_unselected_final_source(self):
        self.fixture.state["selected_candidate_id"] = None
        self.assertEqual(9, len(self.fixture.check(False)))
        self.rejected()

    def test_missing_original_role_cannot_shrink_review_matrix(self):
        (self.root / "measurements/sealed/original.json").unlink()
        self.rejected()

    def test_duplicate_and_changed_case_recipe_are_rejected(self):
        index = self.root / "measurements/sealed/final.json"
        rows = read_json(index)
        write_json(index, [rows[0], rows[0]])
        self.rejected()
        rows[0]["scenario"]["events"][0]["time_s"] += .01
        write_json(index, rows)
        self.rejected()

    def test_extra_role_index_and_foreign_job_package_are_rejected(self):
        write_json(self.root / "measurements/sealed/extra/candidate.json", [])
        self.rejected()
        (self.root / "measurements/sealed/extra/candidate.json").unlink()
        index = self.root / "measurements/sealed/final.json"
        rows = read_json(index)
        rows[0]["package"] = str(self.root / "measurements/foreign/candidate" / rows[0]["scenario"]["case_id"] / "package")
        write_json(index, rows)
        self.rejected()

    def test_immutable_candidate_and_original_parent_roles_are_not_interchangeable(self):
        (self.fixture.selected / "src/panel-motion.js").write_text("other candidate", encoding="utf-8")
        self.rejected()
        (self.fixture.selected / "src/panel-motion.js").write_text("selected", encoding="utf-8")
        self.fixture.state["baseline_hashes"] = self.fixture.suite["original_source_hashes"]
        self.rejected()

    def test_valid_foreign_source_package_cannot_replace_selected_capture(self):
        index = self.root / "measurements/sealed/final.json"
        rows = read_json(index)
        folder = Path(rows[0]["package"]).parent
        foreign = self.fixture.record(folder / "foreign", rows[0]["scenario"], self.fixture.parent)
        rows[0] = foreign
        write_json(index, rows)
        self.rejected()
        self.rejected(lambda: self.fixture.check(False))

    def test_actual_png_observation_frames_and_worker_source_are_bound(self):
        rows = read_json(self.root / "measurements/sealed/final.json")
        package = Path(rows[0]["package"])
        (package / "frames/000001.png").write_bytes(b"changed")
        self.rejected()

    def test_frame_identity_and_worker_candidate_binding_are_required(self):
        index = self.root / "measurements/sealed/final.json"
        rows = read_json(index)
        analysis_path = Path(rows[0]["analysis"])
        analysis = read_json(analysis_path)
        analysis["candidate_sha256"] = "other candidate"
        write_json(analysis_path, analysis)
        self.rejected()
        analysis["candidate_sha256"] = rows[0]["source_sha256"]
        rows[0]["observations"]["objects"][0]["frames"][1]["index"] = 0
        analysis["observations"] = rows[0]["observations"]
        analysis["provenance"]["observation_sha256"] = digest(rows[0]["observations"])
        write_json(analysis_path, analysis)
        write_json(index, rows)
        self.rejected()

    def test_independent_review_must_use_actual_shared_event_policy(self):
        index = self.root / "measurements/sealed/final.json"
        rows = read_json(index)
        rows[0]["events"]["options"]["rest_duration_s"] = .25
        write_json(index, rows)
        self.rejected()

    def test_final_result_and_job_source_binding_cannot_be_rewritten(self):
        result = read_json(self.root / "jobs/sealed/result.json")
        result["pass"] = False
        write_json(self.root / "jobs/sealed/result.json", result)
        self.rejected()


class NativeSuiteBindingTests(unittest.TestCase):
    def test_native_short_runtime_receipt_and_separate_worker_result(self):
        from kiroclops.local_engine.suite_game import scenarios
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "image.png"
            cv2.imencode(".png", np.zeros((4, 4, 3), np.uint8))[1].tofile(image)
            scenario = scenarios("ground", "calibration")[0]
            raw = root / "short-runtime/raw"
            (raw / "frames").mkdir(parents=True)
            frames = []
            active = set()
            for i in range(240):
                for event in scenario["events"]:
                    if event["tick"] == i:
                        (active.add if event["pressed"] else active.remove)(event["action"])
                frames.append({"frame_index": i, "physics_tick": 60 + i, "time_s": i / 60, "actions": sorted(active)})
                (raw / "frames" / f"{i:06d}.png").write_bytes(image.read_bytes())
            hashes = {f"{i:06d}.png": file_sha256(image) for i in range(240)}
            project = root / "immutable-source"
            project.mkdir()
            (project / "player.gd").write_text("controller", encoding="utf-8")
            (project / "player.gd.uid").write_text("full session sidecar", encoding="utf-8")
            full_sources = source_hashes(project)
            expected = se._source_expectation(project, full_sources, "game")
            sources = expected["sources"]
            self.assertIn("player.gd.uid", expected["session_sources"])
            self.assertNotIn("player.gd.uid", sources)
            write_json(raw / "recording.json", {"frames": frames})
            write_json(raw / "capture_config.json", scenario)
            receipt = raw / "engine/receipt.json"
            write_json(receipt, {"status": "completed", "receipt_path": str(receipt), "source_hashes_before": sources, "source_hashes_after": sources})
            provenance = {"full_source_hashes_before": sources, "full_source_hashes_after": sources,
                "source_digest_before": digest(sources), "source_digest_after": digest(sources), "runtime_receipt": str(receipt),
                "runtime_receipt_sha256": file_sha256(receipt), "recording_sha256": file_sha256(raw / "recording.json"),
                "capture_config_sha256": file_sha256(raw / "capture_config.json")}
            write_json(raw / "manifest.json", {"status": "ok", "game": "heart", "fps": 60, "frames": frames,
                "scenario": scenario, "raw_frame_hashes": hashes, "provenance": provenance})
            folder = root / "measurements/final/candidate" / scenario["case_id"]
            manifest = create_package(folder / "package", [raw / "frames" / f"{i:06d}.png" for i in range(240)],
                [f["time_s"] for f in frames], {"provenance": "recorder_verified", "events": [
                    {"time_s": e["tick"] / 60, "action": e["action"], "pressed": e["pressed"]} for e in scenario["events"]]},
                {"adapter": "legacy-godot-v1", "scenario": scenario, "source_manifest_sha256": file_sha256(raw / "manifest.json")})
            selection = {"objects": [{"id": "player", "roi": [0, 0, 2, 2]}]}
            observed = {"package_sha256": manifest["package_sha256"], "image_size": [4, 4], "inputs": manifest["inputs"], "selection": selection,
                        "objects": [{"id": "player", "frames": [{"index": i, "time_s": i / 60} for i in range(240)]}]}
            projection = {"scenario": scenario, "candidate_sha256": digest(full_sources),
                          "provenance": {"package_sha256": manifest["package_sha256"], "observation_sha256": digest(observed)}}
            worker = {"kind": "AnalysisResult", "observations": observed, "package_sha256": manifest["package_sha256"], "candidate_sha256": digest(full_sources)}
            write_json(folder / "analysis.json", projection)
            write_json(folder / "common-analysis/analysis.json", worker)
            record = {"scenario": scenario, "package": str(folder / "package"), "analysis": str(folder / "analysis.json"),
                      "raw_run": str(raw), "source_sha256": digest(full_sources), "observations": observed, "selection": selection, "measurement_status": "ok"}
            case = {"case_id": scenario["case_id"], "stage": "ground", "scenario": scenario}
            se._origin(root, "final", "final", record, case)
            self.assertEqual((manifest["package_sha256"], digest(observed)), se._binding(record, case, {"domain": "game"}, expected_source=expected))
            worker["candidate_sha256"] = "wrong candidate"
            write_json(folder / "common-analysis/analysis.json", worker)
            with self.assertRaises(VisualError):
                se._binding(record, case, {"domain": "game"}, expected_source=expected)
            worker["candidate_sha256"] = digest(full_sources)
            write_json(folder / "common-analysis/analysis.json", worker)
            (raw / "frames/000000.png").write_bytes(b"foreign raw pixel")
            with self.assertRaises(VisualError):
                se._binding(record, case, {"domain": "game"}, expected_source=expected)


if __name__ == "__main__":
    unittest.main()
