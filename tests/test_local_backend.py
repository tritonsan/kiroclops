"""Real process, portable snapshot, recovery and integrity checks for local jobs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine.backend import LocalAnalysisBackend, TERMINAL, _alive, _atomic, _identity, _lock, _read, _terminate_owned, _update
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError


class LocalBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kiroclops backend café ")
        self.root = Path(self.temporary.name)
        source = self.root / "original pictures"
        source.mkdir()
        texture = np.random.default_rng(47).integers(30, 255, (12, 12, 3), dtype=np.uint8)
        paths = []
        for index in range(24):
            image = np.zeros((64, 96, 3), dtype=np.uint8)
            x = 10 + min(index, 12)
            image[20:32, x:x+12] = texture
            path = source / f"{index:03}.png"
            path.write_bytes(cv2.imencode(".png", image)[1].tobytes())
            paths.append(path)
        self.package = self.root / "video café"
        self.manifest = create_package(self.package, paths, [i/60 for i in range(len(paths))])
        self.selection = {"schema_version": 1, "objects": [{"id": "moving-block", "roi": [10, 20, 12, 12], "method": "template", "search_radius": 8}]}
        self.backend = LocalAnalysisBackend(self.root / "jobs store")

    def tearDown(self):
        for state_path in self.root.glob("**/jobs/*/state.json"):
            state = _read(state_path)
            if _alive(state.get("worker")):
                _atomic(state_path.parent / "cancel.json", {"requested_at": time.time()})
                deadline = time.monotonic() + 3
                while _alive(state.get("worker")) and time.monotonic() < deadline:
                    time.sleep(.02)
                if _alive(state.get("worker")):
                    _terminate_owned(state["worker"])
                    deadline = time.monotonic() + 2
                    while _alive(state.get("worker")) and time.monotonic() < deadline:
                        time.sleep(.02)
        # A worker can publish its terminal state just before Windows closes
        # its redirected log handle.  Wait for that handle to close rather
        # than turning a successful job into a teardown sharing violation.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.temporary.cleanup()
                break
            except (PermissionError, NotADirectoryError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def request(self, **extra):
        return {"operation": "analyze", "package_path": str(self.package), "selection": self.selection, **extra}

    def complete(self, backend=None, **extra):
        backend = backend or self.backend
        job = backend.submit(self.request(**extra))
        result = backend.get_job(job["job_id"], wait_seconds=20)
        self.assertEqual("succeeded", result["status"], result)
        return result

    def assert_code(self, code, operation):
        with self.assertRaises(VisualError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)

    def test_real_spawned_analysis_under_unicode_and_space_path(self):
        job = self.complete(candidate_sha256="a"*64)
        self.assertNotEqual(os.getpid(), job["worker"]["pid"])
        self.assertEqual("ok", job["result"]["measurement_status"])
        self.assertEqual(self.manifest["package_sha256"], job["result"]["package_sha256"])
        self.assertEqual("a"*64, job["result"]["candidate_sha256"])
        observed = job["result"]["observations"]["objects"][0]
        self.assertGreaterEqual(observed["valid_ratio"], .95)
        self.assertAlmostEqual(observed["frames"][12]["x"] - observed["frames"][0]["x"], 12, delta=.01)
        self.assertIn("events", job["result"])

    def test_snapshot_excludes_unlisted_private_data(self):
        (self.package / "private-oracle.json").write_text('{"secret":true}')
        job = self.complete()
        copied = Path(job["job_directory"]) / "input" / "package"
        self.assertFalse((copied / "private-oracle.json").exists())
        self.assertEqual(self.manifest, load_package(copied))

    def test_original_changes_do_not_change_already_frozen_candidate(self):
        with _lock(self.backend.root / "slots" / "0.lock"):
            job = self.backend.submit(self.request())
            (self.package / "frames/000002.png").write_bytes(b"changed original")
        result = self.backend.get_job(job["job_id"], wait_seconds=20)
        self.assertEqual("succeeded", result["status"], result)
        self.assertEqual(self.manifest["package_sha256"], result["result"]["package_sha256"])

    def test_snapshot_mutation_is_not_a_successful_measurement(self):
        with _lock(self.backend.root / "slots" / "0.lock"):
            job = self.backend.submit(self.request())
            frozen = self.backend.root / "jobs" / job["job_id"] / "input/package/frames/000003.png"
            frozen.write_bytes(b"invalid snapshot")
        result = self.backend.get_job(job["job_id"], wait_seconds=20)
        self.assertEqual("failed", result["status"], result)
        self.assertEqual("package_integrity_failure", result["error"]["code"])
        self.assertIsNone(result["result"])

    def test_idempotency_same_request_reuses_job(self):
        with patch.object(self.backend, "_spawn"):
            first = self.backend.submit(self.request(), idempotency_key="request 1")
            second = self.backend.submit(self.request(), idempotency_key="request 1")
        self.assertEqual(first["job_id"], second["job_id"])

    def test_idempotency_rejects_different_candidate_or_contract(self):
        with patch.object(self.backend, "_spawn"):
            self.backend.submit(self.request(candidate_sha256="a"*64), "same")
            self.assert_code("idempotency_conflict", lambda: self.backend.submit(self.request(candidate_sha256="b"*64), "same"))
            self.assert_code("idempotency_conflict", lambda: self.backend.submit(self.request(contract={"different": True}), "same"))

    def test_moved_package_has_identical_semantic_result(self):
        first = self.complete()
        moved = self.root / "moved original"
        shutil.copytree(self.package, moved)
        second = self.complete(package_path=str(moved))
        self.assertEqual(first["result"], second["result"])
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_same_content_moved_package_reuses_idempotent_job(self):
        moved = self.root / "moved original"
        shutil.copytree(self.package, moved)
        with patch.object(self.backend, "_spawn"):
            first = self.backend.submit(self.request(), "portable")
            second = self.backend.submit(self.request(package_path=str(moved)), "portable")
        self.assertEqual(first["job_id"], second["job_id"])

    def test_reconnected_client_observes_existing_worker(self):
        with _lock(self.backend.root / "slots" / "0.lock"):
            job = self.backend.submit(self.request())
            before = self.backend.get_job(job["job_id"])
            resumed = LocalAnalysisBackend(self.backend.root)
            self.assertEqual(before["worker"], resumed.get_job(job["job_id"])["worker"])
        self.assertEqual("succeeded", resumed.get_job(job["job_id"], wait_seconds=20)["status"])

    def test_worker_survives_submitting_client_exit(self):
        code = "import json,sys; from pathlib import Path; from kiroclops.local_engine.backend import LocalAnalysisBackend; print(json.dumps(LocalAnalysisBackend(Path(sys.argv[1])).submit(json.loads(sys.argv[2]))))"
        with _lock(self.backend.root / "slots" / "0.lock"):
            submitted = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(self.backend.root), json.dumps(self.request())], capture_output=True, text=True, encoding="utf-8", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.assertEqual(0, submitted.returncode, submitted.stderr)
            job = json.loads(submitted.stdout)
            self.assertEqual("queued", self.backend.get_job(job["job_id"])["status"])
        self.assertEqual("succeeded", self.backend.get_job(job["job_id"], wait_seconds=20)["status"])

    def test_unspawned_queued_job_is_recovered(self):
        with patch.object(self.backend, "_spawn"):
            job = self.backend.submit(self.request())
        resumed = LocalAnalysisBackend(self.backend.root)
        self.assertEqual("succeeded", resumed.get_job(job["job_id"], wait_seconds=20)["status"])

    def test_cancel_queued_job_keeps_other_jobs_owned(self):
        with _lock(self.backend.root / "slots" / "0.lock"):
            first = self.backend.submit(self.request())
            second = self.backend.submit(self.request())
            result = self.backend.cancel_job(first["job_id"])
            self.assertEqual("cancelled", result["status"])
            self.assertNotIn(self.backend.get_job(second["job_id"])["status"], TERMINAL)
        self.assertEqual("succeeded", self.backend.get_job(second["job_id"], wait_seconds=20)["status"])

    def test_cancel_finished_job_does_not_replace_result(self):
        job = self.complete()
        cancelled = self.backend.cancel_job(job["job_id"])
        self.assertEqual("succeeded", cancelled["status"])
        self.assertEqual(job["result"], cancelled["result"])

    def test_terminal_delivery_releases_owned_worker_and_log(self):
        job = self.complete()
        self.assertTrue(job['worker_exited'])
        self.assertFalse(_alive(job.get('worker')))
        self.assertFalse(_alive(job.get('launcher')))
        log = Path(job['job_directory']) / 'worker.log'
        moved = log.with_name('worker-closed.log')
        log.rename(moved)
        moved.unlink()

    def test_deadline_is_explicit_failure(self):
        backend = LocalAnalysisBackend(self.root / "deadline store", timeout_seconds=.00001)
        job = backend.submit(self.request())
        result = backend.get_job(job["job_id"], wait_seconds=20)
        self.assertEqual("failed", result["status"])
        self.assertEqual("job_deadline_exceeded", result["error"]["code"])

    def test_interrupted_worker_is_not_automatically_replayed(self):
        with _lock(self.backend.root / "slots" / "0.lock"):
            job = self.backend.submit(self.request())
            state = self.backend.get_job(job["job_id"])
            self.assertTrue(_terminate_owned(state["worker"]))
            deadline = time.monotonic() + 3
            while _alive(state["worker"]) and time.monotonic() < deadline:
                time.sleep(.02)
            result = LocalAnalysisBackend(self.backend.root).get_job(job["job_id"])
        self.assertEqual("interrupted", result["status"])
        self.assertEqual("worker_interrupted", result["error"]["code"])

    def test_default_concurrency_is_one(self):
        with _lock(self.backend.root / "slots" / "0.lock"):
            jobs = [self.backend.submit(self.request()) for _ in range(2)]
            time.sleep(.2)
            self.assertTrue(all(self.backend.get_job(j["job_id"])["status"] == "queued" for j in jobs))
        results = [self.backend.get_job(j["job_id"], wait_seconds=20) for j in jobs]
        self.assertTrue(all(r["status"] == "succeeded" for r in results), results)
        results.sort(key=lambda r: r["started_at"])
        self.assertGreaterEqual(results[1]["started_at"], results[0]["finished_at"])

    def test_result_tampering_is_detected_and_durable(self):
        job = self.complete()
        _atomic(Path(job["result_path"]), {"fake": True})
        result = self.backend.get_job(job["job_id"])
        self.assertEqual("failed", result["status"])
        self.assertEqual("result_integrity_failure", result["error"]["code"])
        self.assertEqual("failed", LocalAnalysisBackend(self.backend.root).get_job(job["job_id"])["status"])

    def test_wrong_package_binding_rejected_before_execution(self):
        self.assert_code("source_changed", lambda: self.backend.submit(self.request(package_sha256="0"*64)))
        self.assertEqual([], list((self.backend.root / "jobs").glob("*/state.json")))

    def test_request_mutation_is_detected(self):
        with _lock(self.backend.root / "slots" / "0.lock"):
            job = self.backend.submit(self.request())
            _atomic(self.backend.root / "jobs" / job["job_id"] / "request.json", {"operation": "different"})
        result = self.backend.get_job(job["job_id"], wait_seconds=20)
        self.assertEqual("failed", result["status"])
        self.assertEqual("source_changed", result["error"]["code"])

    def test_invalid_selection_is_analysis_error_not_motion_failure(self):
        job = self.backend.submit(self.request(selection={"schema_version": 1, "objects": []}))
        result = self.backend.get_job(job["job_id"], wait_seconds=20)
        self.assertEqual("failed", result["status"])
        self.assertEqual("invalid_selection", result["error"]["code"])

    def test_pid_reuse_identity_mismatch_never_kills_process(self):
        self.assertIsNotNone(_identity(os.getpid()))
        self.assertFalse(_terminate_owned({"pid": os.getpid(), "identity": "different creation time"}))

    def test_invalid_ids_operations_and_nonfinite_requests(self):
        self.assert_code("invalid_job_id", lambda: self.backend.get_job("../elsewhere"))
        self.assert_code("job_not_found", lambda: self.backend.get_job("a"*32))
        self.assert_code("unsupported_operation", lambda: self.backend.submit(self.request(operation="run-code")))
        self.assert_code("invalid_request", lambda: self.backend.submit(self.request(contract={"threshold": float("nan")})))


if __name__ == "__main__":
    unittest.main()
