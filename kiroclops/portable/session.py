"""Portable experiment sessions: immutable candidates and owned asynchronous jobs.

This module does not import the SlayHorizon session implementation and never
changes a controller. The evaluator receives one immutable source snapshot.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from kiroclops.common import ROOT
from kiroclops.capture import process_options
from kiroclops.integration.storage import (
    IntegrationError, copy_project, file_lock, identifier, process_identity,
    project_hashes, read_json, sha256, utc_now, write_json,
)

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
EXPERIMENTS = {"heart-jump-v1", "heart-ground-v1"}
LIMITS = {"max_candidates": 3, "max_seconds": 1800}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def ordinary(path):
    """Reject reparse points even when they resolve back into the workspace."""
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() and (part.is_symlink() or getattr(part.stat(), "st_file_attributes", 0) & 0x400):
            raise IntegrationError("unsafe_path", "Linked experiment paths are forbidden")
    return path.resolve()


def editable(relative):
    path = Path(relative)
    return relative == "player.gd" or (path.parent.as_posix() == "motion" and path.suffix == ".gd")


def create_session(name, source, target_spec, experiment_spec, *, parent=None,
                   original_baseline=None, protocol=None, root=ROOT):
    """Prepare source copies only. Setup, model invocation and motion changes are separate."""
    name = identifier(name)
    if not name.startswith("heart-"):
        raise IntegrationError("invalid_identifier", "Portable Heart sessions start with heart-")
    experiment_id = experiment_spec.get("experiment_id")
    if experiment_id not in EXPERIMENTS:
        raise IntegrationError("invalid_experiment", "Unknown portable experiment")
    if experiment_id == "heart-ground-v1" and not parent:
        raise IntegrationError("missing_parent", "Ground needs its own sealed jump parent")
    directory = ordinary(Path(root) / "sessions" / name)
    if directory.exists():
        raise IntegrationError("session_exists", "Never replace an existing session")
    source = ordinary(source)
    expected = project_hashes(source)
    if not expected or "project.godot" not in expected or "player.gd" not in expected:
        raise IntegrationError("invalid_target", "Expected the real Heart project")
    directory.mkdir(parents=True)
    state = {"schema_version": 4, "session_id": name, "mode": "tools", "status": "provisioning",
             "target_id": "heart", "target_spec": target_spec, "target_spec_sha256": digest(target_spec),
             "experiment_id": experiment_id, "experiment_spec": experiment_spec,
             "experiment_spec_sha256": digest(experiment_spec), "protocol": protocol or {},
             "parent": parent, "original_baseline": original_baseline,
             "created_at": utc_now(), "target": str(directory / "target"),
             "baseline": str(directory / "baseline"), "baseline_hashes": expected,
             "prepared_baseline_hashes": expected, "budget": dict(LIMITS), "candidates": [],
             "reference": {}, "baseline_runs": {}, "started_at": None, "deadline_at": None,
             "active_job_id": None, "selected_candidate_id": None, "sealed_at": None, "final": None}
    write_json(directory / "session.json", state)
    try:
        copy_project(source, directory / "baseline")
        copy_project(directory / "baseline", directory / "target")
        if any(project_hashes(p) != expected for p in (source, directory / "baseline", directory / "target")):
            raise IntegrationError("source_changed", "Source changed during provisioning")
        state["status"] = "new"
        write_json(directory / "session.json", state)
        write_json(directory / "session-binding.json", {k: state[k] for k in (
            "schema_version", "session_id", "target_id", "target_spec_sha256", "experiment_id",
            "experiment_spec_sha256", "baseline_hashes", "parent", "original_baseline", "protocol", "budget")})
        return directory
    except Exception as exc:
        state.update(status="setup_failed", error=str(exc))
        write_json(directory / "session.json", state)
        raise


class Session:
    def __init__(self, directory):
        self.directory = ordinary(directory)
        if self.directory.parent.name != "sessions":
            raise IntegrationError("invalid_session_path", "Session must be directly inside sessions")
        identifier(self.directory.name)
        self.path = self.directory / "session.json"
        self._children = {}
        self.verify()

    def read(self):
        return read_json(self.path)

    def verify(self):
        state = self.read()
        if (state.get("schema_version") != 4 or state.get("session_id") != self.directory.name
                or state.get("experiment_id") not in EXPERIMENTS or state.get("budget") != LIMITS
                or ordinary(state.get("target", "")) != self.directory / "target"
                or ordinary(state.get("baseline", "")) != self.directory / "baseline"
                or digest(state.get("target_spec")) != state.get("target_spec_sha256")
                or digest(state.get("experiment_spec")) != state.get("experiment_spec_sha256")):
            raise IntegrationError("session_integrity", "Session identity or experiment definition changed")
        binding = read_json(self.directory / "session-binding.json")
        if binding != {k: state.get(k) for k in binding}:
            raise IntegrationError("session_integrity", "Immutable session binding changed")
        if project_hashes(state["baseline"]) != state["baseline_hashes"]:
            raise IntegrationError("source_changed", "Baseline source changed")
        return state

    def update(self, callback):
        with file_lock(self.directory / ".session.lock"):
            state = self.read()
            callback(state)
            write_json(self.path, state)
            return state

    def start_clock(self):
        def start(state):
            if state.get("started_at") or state["candidates"] or state.get("active_job_id"):
                raise IntegrationError("budget_locked", "The stage clock cannot be reset")
            state.update(started_at=utc_now(), deadline_at=time.time() + LIMITS["max_seconds"])
        return self.update(start)

    def inspect(self):
        self.recover()
        state = self.verify()
        # Parent identity, paths, old messages and held-out results are deliberately absent.
        return {"session_id": state["session_id"], "target_id": "heart", "experiment_id": state["experiment_id"],
                "status": state["status"], "target_project": state["target"],
                "active_job_id": state.get("active_job_id"), "candidate_count": len(state["candidates"]),
                "max_candidates": 3, "seconds_remaining": max(0, state["deadline_at"] - time.time())
                if state.get("deadline_at") else 1800, "selected_candidate_id": state.get("selected_candidate_id"),
                "calibration_ids": state["experiment_spec"].get("calibration_ids") or sorted(
                    {s["id"] for s in state["experiment_spec"].get("calibration", [])}),
                "editable_files": ["player.gd", "motion/*.gd"],
                "preservation_requirements": state["experiment_spec"].get("preservation_requirements")
                    or state["experiment_spec"].get("scope", ""),
                "inherited_jump": "sealed; independent review pending" if state.get("parent") else None,
                "candidates": [{k: c.get(k) for k in ("candidate_id", "status", "feedback", "error")}
                               for c in state["candidates"]], "final": state.get("final")}

    def recover(self):
        state = self.read()
        jid = state.get("active_job_id")
        if not jid:
            return
        path = self.directory / "jobs" / f"{identifier(jid)}.json"
        if not path.exists():
            raise IntegrationError("job_missing", "An active job record is missing")
        job = read_json(path)
        if job["status"] in TERMINAL:
            self.update(lambda s: s.update(active_job_id=None) if s.get("active_job_id") == jid else None)
        elif job.get("worker_pid") and process_identity(job["worker_pid"]) != job.get("worker_identity"):
            self.finish_job(jid, error={"code": "worker_interrupted", "message": "Worker exited without a terminal record"},
                            status="interrupted")
        elif not job.get("worker_pid") and process_identity(job.get("owner_pid")) != job.get("owner_identity"):
            self.finish_job(jid, error={"code": "worker_interrupted", "message": "Owner exited before spawning the reserved worker"},
                            status="interrupted")

    def _snapshot(self, cid):
        state = self.verify()
        before = project_hashes(state["target"])
        deleted = set(state["baseline_hashes"]) - set(before)
        protected = [n for n in set(before) | set(state["baseline_hashes"])
                     if before.get(n) != state["baseline_hashes"].get(n) and not editable(n)]
        if deleted or protected:
            raise IntegrationError("source_scope", f"Protected or deleted files: {sorted(deleted | set(protected))}")
        dest = self.directory / "candidates" / cid
        dest.mkdir(parents=True, exist_ok=False)
        copy_project(state["target"], dest / "project")
        if project_hashes(state["target"]) != before or project_hashes(dest / "project") != before:
            raise IntegrationError("source_changed_during_snapshot", "Concurrent edits invalidated this attempt")
        patch = []
        for name in sorted(before):
            if before[name] != state["baseline_hashes"].get(name):
                old = Path(state["baseline"]) / name
                patch.extend(difflib.unified_diff(old.read_text(encoding="utf-8").splitlines(True) if old.exists() else [],
                    (dest / "project" / name).read_text(encoding="utf-8").splitlines(True),
                    fromfile="before/" + name, tofile="after/" + name))
        (dest / "patch.diff").write_text("".join(patch), encoding="utf-8")
        candidate = {"candidate_id": cid, "session_id": state["session_id"], "status": "capturing",
                     "source_hashes": before, "source_sha256": digest(before), "snapshot_at": utc_now(),
                     "project_path": str(dest / "project"), "diff_path": str(dest / "patch.diff")}
        write_json(dest / "candidate.json", candidate)
        self.update(lambda s: s["candidates"].__setitem__(int(cid[-2:]) - 1, candidate))
        return candidate

    def verify_candidate(self, cid):
        cid = identifier(cid)
        candidate = read_json(self.directory / "candidates" / cid / "candidate.json")
        state_candidate = next((c for c in self.read()["candidates"] if c["candidate_id"] == cid), {})
        source = self.directory / "candidates" / cid / "project"
        if (candidate.get("session_id") != self.directory.name or candidate.get("candidate_id") != cid
                or candidate.get("source_hashes") != state_candidate.get("source_hashes")
                or candidate.get("source_sha256") != digest(candidate.get("source_hashes"))
                or ordinary(candidate.get("project_path", "")) != source
                or project_hashes(source) != candidate.get("source_hashes")):
            raise IntegrationError("candidate_integrity", "Candidate source or result identity changed")
        return candidate

    def submit(self, action, candidate_id=None):
        if action not in {"prepare", "evaluate", "finalize", "report"}:
            raise IntegrationError("unknown_action", "Unknown portable action")
        self.recover()
        self.verify()
        jid = "job-" + uuid.uuid4().hex[:12]
        def reserve(state):
            if state.get("active_job_id"):
                raise IntegrationError("session_busy", "Wait for the active job")
            if action == "prepare" and state["status"] != "new":
                raise IntegrationError("already_prepared", "Preparation is single-use")
            if action in {"evaluate", "finalize"}:
                if state.get("sealed_at"):
                    raise IntegrationError("validation_sealed", "No tuning after finalization")
                if state["status"] != "ready":
                    raise IntegrationError("not_ready", "Complete preparation first")
            if action != "report" and state.get("deadline_at") and time.time() >= state["deadline_at"]:
                raise IntegrationError("budget_exhausted", "Stage deadline reached")
            if not state.get("started_at"):
                state.update(started_at=utc_now(), deadline_at=time.time() + 1800)
            cid = None
            if action == "evaluate":
                if len(state["candidates"]) >= 3:
                    raise IntegrationError("budget_exhausted", "Three attempts already used")
                cid = f'candidate-{len(state["candidates"]) + 1:02d}'
                state["candidates"].append({"candidate_id": cid, "status": "queued", "created_at": utc_now()})
            if action == "finalize":
                complete = {}
                for p in (self.directory / "jobs").glob("job-*.json"):
                    j = read_json(p)
                    if j.get("action") == "evaluate" and j.get("status") == "succeeded" and j.get("session_id") == state["session_id"]:
                        complete[j.get("candidate_id")] = j
                eligible = [c for c in state["candidates"] if c.get("status") == "evaluated"
                    and c.get("feedback", {}).get("eligible") is True
                    and c.get("feedback", {}).get("automatic_integrity_status") == "pass"
                    and type(c["feedback"].get("mean_rmse_body")) in (int, float)
                    and math.isfinite(c["feedback"]["mean_rmse_body"])
                    and c["candidate_id"] in complete
                    and complete[c["candidate_id"]].get("candidate_source_sha256") == c.get("source_sha256")
                    and complete[c["candidate_id"]].get("result") == c.get("feedback")]
                if not eligible:
                    raise IntegrationError("no_eligible_candidate", "No completed candidate passed every calibration and integrity gate")
                chosen = min(eligible, key=lambda c: (c["feedback"]["mean_rmse_body"], c["candidate_id"]))
                cid = chosen["candidate_id"]
                if candidate_id is not None and candidate_id != cid:
                    raise IntegrationError("candidate_not_best", f"Best eligible calibration candidate is {cid}")
                self.verify_candidate(cid)
                state.update(selected_candidate_id=cid, sealed_at=utc_now())
            job = {"schema_version": 4, "job_id": jid, "session_id": state["session_id"], "action": action,
                   "status": "queued", "created_at": utc_now(), "candidate_id": cid,
                   "owner_pid": os.getpid(), "owner_identity": process_identity(os.getpid()),
                   "progress": {"message": "Queued"}}
            write_json(self.directory / "jobs" / f"{jid}.json", job)
            state["active_job_id"] = jid
            if action != "report":
                state["status"] = {"prepare": "preparing", "evaluate": "evaluating", "finalize": "finalizing"}[action]
        self.update(reserve)
        job_path = self.directory / "jobs" / f"{jid}.json"
        try:
            job = read_json(job_path)
            if action == "evaluate":
                self._snapshot(job["candidate_id"])
            if job.get("candidate_id"):
                candidate = self.verify_candidate(job["candidate_id"])
                job["candidate_source_sha256"] = candidate["source_sha256"]
                write_json(job_path, job)
            process = self._spawn_worker(jid)
            self._children[jid] = process
            with file_lock(self.directory / ".session.lock"):
                job = read_json(job_path)
                if job["status"] == "queued":
                    job.update(worker_pid=process.pid, worker_identity=process_identity(process.pid))
                    write_json(job_path, job)
        except Exception as exc:
            self.finish_job(jid, error={"code": getattr(exc, "code", "worker_start_failed"), "message": str(exc)})
        return {"job_id": jid, "status": read_json(job_path)["status"], "candidate_id": read_json(job_path).get("candidate_id")}

    def _spawn_worker(self, jid):
        with (self.directory / "jobs" / f"{jid}.log").open("xb") as log:
            return subprocess.Popen([sys.executable, "-m", "kiroclops.portable.session", "worker",
                "--session", str(self.directory), "--job", jid], cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"},
                stdout=log, stderr=subprocess.STDOUT, **process_options())

    def finish_job(self, jid, *, result=None, error=None, status=None):
        with file_lock(self.directory / ".session.lock"):
            path = self.directory / "jobs" / f"{identifier(jid)}.json"
            job, state = read_json(path), self.read()
            if job["status"] in TERMINAL:
                return job
            status = status or ("failed" if error else "succeeded")
            job.update(status=status, finished_at=utc_now())
            if error:
                job["error"] = error
            if result is not None:
                job["result"] = result
            success = status == "succeeded"
            cid = job.get("candidate_id")
            if cid and job["action"] == "evaluate":
                candidate = state["candidates"][int(cid[-2:]) - 1]
                candidate["status"] = "evaluated" if success else status
                if success:
                    candidate["feedback"] = result
                else:
                    candidate["error"] = error
                candidate_path = self.directory / "candidates" / cid / "candidate.json"
                if candidate_path.parent.exists():
                    write_json(candidate_path, candidate)
            if job["action"] == "prepare" and success:
                state["reference"] = result.get("reference", {})
                state["baseline_runs"] = result.get("baseline_runs", {})
                state["preparation"] = result
                state["status"] = "ready"
            elif job["action"] == "finalize":
                state["status"] = "finished" if success else "closed_failed"
                state["final"] = result if success else {"status": status, "error": error}
                state["finished_at"] = utc_now()
            elif job["action"] == "evaluate":
                state["status"] = "ready"
            elif job["action"] == "prepare":
                state["status"] = "setup_failed"
            if state.get("active_job_id") == jid:
                state["active_job_id"] = None
            write_json(path, job)
            write_json(self.path, state)
            return job

    def get_job(self, job_id, wait_seconds=10):
        if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds):
            raise IntegrationError("invalid_wait", "Wait must be finite")
        path = self.directory / "jobs" / f"{identifier(job_id)}.json"
        if not path.is_file():
            raise IntegrationError("unknown_job", "Unknown job in this session")
        deadline = time.monotonic() + min(20, max(0, wait_seconds))
        while True:
            self.recover()
            job = read_json(path)
            if job.get("session_id") != self.directory.name or job.get("job_id") != job_id:
                raise IntegrationError("job_identity", "Job identity differs")
            if job["status"] in TERMINAL or time.monotonic() >= deadline:
                return {k: v for k, v in job.items() if k not in {"owner_pid", "owner_identity", "worker_pid", "worker_identity"}}
            time.sleep(.1)

    def cancel_job(self, job_id):
        job = self.get_job(job_id, 0)
        if job["status"] in TERMINAL:
            return {"job_id": job_id, "status": job["status"]}
        (self.directory / "jobs" / f"{identifier(job_id)}.cancel").touch()
        return {"job_id": job_id, "status": "cancellation_requested"}

    def close(self):
        for jid, child in self._children.items():
            if child.poll() is None:
                self.cancel_job(jid)
        for child in self._children.values():
            if child.poll() is None:
                try:
                    child.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    from kiroclops.integration.runner import _terminate_owned_tree
                    _terminate_owned_tree(child)


def run_worker(directory, job_id):
    session = Session(directory)
    path = session.directory / "jobs" / f"{identifier(job_id)}.json"
    with file_lock(session.directory / ".session.lock"):
        job = read_json(path)
        if job["status"] != "queued" or job["session_id"] != session.directory.name:
            raise IntegrationError("job_identity", "Worker requires its own queued job")
        job.update(status="running", worker_pid=os.getpid(), worker_identity=process_identity(os.getpid()), started_at=utc_now())
        write_json(path, job)
    cancel, stop = threading.Event(), threading.Event()
    reason = {"code": "cancelled"}
    def watch():
        while not stop.wait(.2):
            try:
                state = session.read()
                code = ("cancelled" if path.with_suffix(".cancel").exists() else
                        "owner_disconnected" if process_identity(job["owner_pid"]) != job["owner_identity"] else
                        "budget_exhausted" if job["action"] != "report" and time.time() >= state["deadline_at"] else None)
            except Exception:
                code = "session_unreadable"
            if code:
                reason["code"] = code
                cancel.set()
                return
    threading.Thread(target=watch, daemon=True).start()
    def check():
        if path.with_suffix(".cancel").exists():
            reason["code"] = "cancelled"
            cancel.set()
        if cancel.is_set():
            raise IntegrationError(reason["code"], "Job cancelled, disconnected or expired")
        state = session.verify()
        if job["action"] != "report" and time.time() >= state["deadline_at"]:
            raise IntegrationError("budget_exhausted", "Stage deadline expired")
        if job.get("candidate_id"):
            candidate = session.verify_candidate(job["candidate_id"])
            if candidate["source_sha256"] != job.get("candidate_source_sha256"):
                raise IntegrationError("candidate_integrity", "Job is bound to another candidate source")
    def progress(message, completed=None, total=None):
        check()
        with file_lock(session.directory / ".session.lock"):
            current = read_json(path)
            current["progress"] = {"message": message, "completed": completed, "total": total}
            write_json(path, current)
    try:
        check()
        from .evaluation import run_job
        result = run_job(session.directory, job_id, cancel=cancel, check=check, progress=progress)
        check()
        if not isinstance(result, dict):
            raise IntegrationError("invalid_result", "Evaluator must return a result object")
        session.finish_job(job_id, result=result)
    except Exception as exc:
        code = getattr(exc, "code", "worker_failed")
        session.finish_job(job_id, error={"code": code, "message": str(exc)},
                           status="cancelled" if code in {"cancelled", "owner_disconnected", "budget_exhausted"} else "failed")
    finally:
        stop.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["worker"])
    parser.add_argument("--session", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    run_worker(args.session, args.job)
