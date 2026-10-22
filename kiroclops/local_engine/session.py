"""Local authoring sessions, immutable candidates and owned execution jobs.

This orchestrator can run applications. The analysis backend deliberately cannot.
No function here generates, patches or repairs target behavior code.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import statistics
import threading
import time
import traceback
import uuid

from kiroclops.visual.schema import VisualError, file_sha256
from .backend import TERMINAL, _alive, _atomic, _digest, _identity, _lock, _read, _terminate_owned, _validate_job_id
from .execution_policy import budget_from_policy, validate_policy, policy_from_state, execution_job_limit, REVISED_ID

DOMAINS = {"web_panel", "svg", "game"}
MODES = {"tools", "baseline"}
FINALIZE_VALIDATION_SECONDS = 1800
FAILURE_PUBLICATION_SECONDS = 1.0
LIMITS = {"max_candidates": 3, "max_seconds": 1800, "max_validation_seconds": FINALIZE_VALIDATION_SECONDS}
IGNORED = {".git", ".godot", ".import", "__pycache__", ".kiro", ".kiroclops-evidence", ".kiroclops-scratch"}
BINDING_FIELDS = ("schema_version", "session_id", "domain", "mode", "goal", "target", "baseline", "reference", "baseline_hashes", "reference_hashes", "experiment", "budget", "capture_lock", "controls_hashes")
JOB_BINDING_FIELDS = ("schema_version", "job_id", "operation", "created_at", "candidate_id", "source_path", "source_hashes", "source_sha256")
# V6 plan WP3.5: an evaluation is refused when less than this fraction of the
# measured evaluation time remains, instead of being started and then killed.
EVALUATION_TIME_MARGIN = .9
# V6 plan WP2.3: a finalization that failed only in recording may be measured
# again for the same sealed candidate, at most this many times, by the runner.
CAPTURE_FAILURE_CODES = frozenset({"capture_timing_failure", "machine_not_quiet"})
MAX_REMEASURES = 2


def _publish_owned_failure(publish, exit_code, *, exit_after=False):
    """Bound failure persistence even when a progress writer holds the job lock.

    Only the owned worker may exit here. If persistence stalls or fails, leave
    the existing durable state for the parent to reconcile as interrupted;
    never synthesize a successful result or bypass the normal job lock.
    """
    done = threading.Event()
    committed = False

    def writer():
        nonlocal committed
        try:
            publish()
            committed = True
        except BaseException:
            # Persistence itself is unavailable. The parent can still observe
            # process death and retain the incomplete job as interrupted.
            pass
        finally:
            done.set()

    worker = threading.Thread(target=writer, daemon=True, name="session-failure")
    try:
        worker.start()
        done.wait(FAILURE_PUBLICATION_SECONDS)
    finally:
        if exit_after or not committed:
            os._exit(exit_code)
    return committed


def finalize_validation_deadline(state, job):
    """Bound local validation to the frozen submission time, not the Kiro turn."""
    if job.get("operation") != "finalize":
        raise VisualError("invalid_job", "Only sealed finalization has a separate validation deadline.")
    # Older v1 sessions remain readable with the same constant protocol limit.
    return job["created_at"] + state["budget"].get("max_validation_seconds", FINALIZE_VALIDATION_SECONDS)


def _ordinary(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            attrs = getattr(part.lstat(), "st_file_attributes", 0)
            if part.is_symlink() or attrs & 0x400:
                raise VisualError("unsafe_path", "Session sources cannot use symbolic links or reparse points.")
    return path.resolve()


def source_hashes(project: Path) -> dict[str, str]:
    from .source_inventory import full_byte_inventory
    return full_byte_inventory(project, ignored=IGNORED)


def copy_project(source: Path, destination: Path) -> dict[str, str]:
    source, destination = _ordinary(source), _ordinary(destination)
    if destination.exists():
        raise VisualError("output_exists", "Never overwrite an existing project snapshot.")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise VisualError("unsafe_path", "Project source and destination cannot contain each other.")
    expected = source_hashes(source)
    destination.mkdir(parents=True)
    for relative in expected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    if source_hashes(source) != expected or source_hashes(destination) != expected:
        raise VisualError("source_changed", "Project changed while its snapshot was being copied.")
    return expected


def editable(domain: str, relative: str) -> bool:
    if not isinstance(relative, str) or "\\" in relative or ".." in relative.split("/"):
        return False
    path = PurePosixPath(relative)
    if domain == "game":
        return relative == "player.gd" or (path.parent.as_posix() == "motion" and path.suffix == ".gd")
    primary = {"web_panel": "src/panel-motion.js", "svg": "src/layout-motion.js"}.get(domain)
    return primary is not None and (relative == primary or (path.parent.as_posix() == "src/motion" and path.suffix == ".js"))


def _reference_hashes(reference):
    reference = _ordinary(reference)
    return source_hashes(reference) if reference.is_dir() else {reference.name: file_sha256(reference)}


def _scope(domain):
    return ["player.gd", "motion/*.gd"] if domain == "game" else [{"web_panel": "src/panel-motion.js", "svg": "src/layout-motion.js"}[domain], "src/motion/*.js"]


def create_session(directory, domain, baseline, reference, goal, mode="tools", experiment=None, *, execution_policy=None) -> dict:
    if domain not in DOMAINS or mode not in MODES:
        raise VisualError("invalid_session", "Unsupported domain or comparison mode.")
    if not isinstance(goal, str) or not goal.strip():
        raise VisualError("invalid_session", "A user goal is required.")
    if experiment is not None and not isinstance(experiment, dict):
        raise VisualError("invalid_session", "experiment must be a JSON object.")
    directory, baseline, reference = _ordinary(directory), _ordinary(baseline), _ordinary(reference)
    if directory.exists():
        raise VisualError("session_exists", "Every session needs a fresh output directory.")
    if directory.is_relative_to(baseline) or baseline.is_relative_to(directory) or reference.is_relative_to(directory):
        raise VisualError("unsafe_path", "Session copies and external reference must be separate.")
    if not reference.exists():
        raise VisualError("missing_reference", "The trusted external reference does not exist.")
    expected = source_hashes(baseline)
    entry = "player.gd" if domain == "game" else {"web_panel": "src/panel-motion.js", "svg": "src/layout-motion.js"}[domain]
    if not expected or entry not in expected:
        raise VisualError("invalid_project", f"Expected the real editable entry point: {entry}")
    experiment = _read_json_copy(experiment or {})
    declared = experiment.get('execution_policy')
    if execution_policy is not None:
        policy = validate_policy(execution_policy, stage=experiment.get('stage'))
        if declared is not None and declared != policy:
            raise VisualError('invalid_execution_policy', 'Suite and session execution policies differ.')
        experiment['execution_policy'] = policy
    elif declared is not None:
        policy = validate_policy(declared, stage=experiment.get('stage'))
    else:
        policy = None
    budget = budget_from_policy(policy) if policy is not None else dict(LIMITS)
    capture_lock = _ordinary(experiment.get("capture_lock", Path(__file__).resolve().parents[2] / "artifacts" / "local-engine" / "capture.lock"))
    directory.mkdir(parents=True)
    state = {"schema_version": 2 if policy is not None and policy['policy_id'] == REVISED_ID else 1, "kind": "LocalSession", "session_id": uuid.uuid4().hex,
             "domain": domain, "mode": mode, "goal": goal, "status": "provisioning", "created_at": time.time(),
             "target": str(directory / "target"), "baseline": str(directory / "baseline"), "reference": str(reference),
             "baseline_hashes": expected, "reference_hashes": _reference_hashes(reference), "experiment": experiment, "budget": budget, "capture_lock": str(capture_lock),
             "started_at": None, "deadline_at": None, "clock_origin": None, "prepared": False, "prepare_job_id": None,
             "active_job_id": None, "candidates": [], "selected_candidate_id": None, "sealed_at": None,
             "final_job_id": None, "final": None, "controls_hashes": None, "error": None}
    _atomic(directory / "session.json", state)
    try:
        copy_project(baseline, directory / "baseline")
        copy_project(directory / "baseline", directory / "target")
        if source_hashes(baseline) != expected:
            raise VisualError("source_changed", "Baseline source changed during session creation.")
        state["status"] = "new"
        _atomic(directory / "session.json", state)
        _atomic(directory / "binding.json", {"sha256": _digest({key: state[key] for key in BINDING_FIELDS})})
        return dict(state)
    except Exception as exc:
        state.update(status="setup_failed", error={"code": getattr(exc, "code", "setup_failed"), "message": str(exc)})
        _atomic(directory / "session.json", state)
        raise


def _read_json_copy(value):
    import json
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise VisualError("invalid_session", "Session configuration must be finite JSON data.") from exc


class Session:
    def __init__(self, directory):
        self.directory = _ordinary(directory)
        self._load()

    def _load(self):
        state = _read(self.directory / "session.json")
        if state.get("schema_version") not in (1, 2) or state.get("kind") != "LocalSession":
            raise VisualError("unsupported_schema", "Expected LocalSession v1 or v2.")
        if _digest({key: state[key] for key in BINDING_FIELDS}) != _read(self.directory / "binding.json")["sha256"]:
            raise VisualError("session_binding_changed", "Session scope, source binding or budget changed.")
        policy_from_state(state)
        return state

    def _save(self, state):
        _atomic(self.directory / "session.json", state)

    def _controls(self):
        result = {}
        for relative in ("target/.kiro",):
            target = self.directory / relative
            if target.is_dir():
                result.update({f"{relative}/{key}": value for key, value in source_hashes(target).items()})
        # The sibling kiro directory also holds append-only audit, ACP and
        # launch logs. Bind its immutable configuration, not live run evidence.
        config = self.directory / "kiro" / "config.json"
        if config.exists():
            result["kiro/config.json"] = file_sha256(_ordinary(config))
        return result

    def bind_controls(self):
        with _lock(self.directory / "session.lock"):
            state = self._load()
            if state["started_at"] is not None:
                raise VisualError("session_started", "Agent controls must be bound before the run starts.")
            current = self._controls()
            if state["controls_hashes"] is not None and current != state["controls_hashes"]:
                raise VisualError("control_changed", "Already bound agent controls cannot be replaced.")
            state["controls_hashes"] = current
            _atomic(self.directory / "binding.json", {"sha256": _digest({key: state[key] for key in BINDING_FIELDS})})
            self._save(state)
        return {"status": "bound", "sha256": _digest(current)}

    def start_clock(self, origin="model_generation"):
        with _lock(self.directory / "session.lock"):
            state = self._load()
            self._start_clock(state, origin)
            self._save(state)
        return {"started_at": state["started_at"], "deadline_at": state["deadline_at"]}

    @staticmethod
    def _start_clock(state, origin):
        if state["started_at"] is None:
            state["started_at"] = time.time()
            state["deadline_at"] = state["started_at"] + state["budget"]["max_seconds"]
            state["clock_origin"] = str(origin)

    def _job_dir(self, job_id):
        directory = self.directory / "jobs" / _validate_job_id(job_id)
        if not (directory / "job.json").is_file():
            raise VisualError("job_not_found", "This job does not belong to this session.")
        return directory

    def _verify_sources(self, state):
        if source_hashes(state["baseline"]) != state["baseline_hashes"]:
            raise VisualError("baseline_changed", "Frozen baseline source changed.")
        if state["controls_hashes"] is not None and self._controls() != state["controls_hashes"]:
            raise VisualError("control_changed", "Bound agent control files changed.")
        if _reference_hashes(state["reference"]) != state["reference_hashes"]:
            raise VisualError("reference_changed", "Frozen external reference source changed.")
        current = source_hashes(state["target"])
        changes = sorted(relative for relative in set(current) | set(state["baseline_hashes"]) if current.get(relative) != state["baseline_hashes"].get(relative))
        forbidden = [relative for relative in changes if not editable(state["domain"], relative)]
        if forbidden:
            raise VisualError("protected_source_changed", "Changes outside editable scope: " + ", ".join(forbidden[:12]))
        return current

    def inspect(self):
        self._reconcile_active()
        state = self._load()
        from .stage_closure import inspect_closure
        closure = inspect_closure(self.directory, state)
        deferred_final = state["sealed_at"] is not None and self._native_authoring_open()
        remaining = state["budget"]["max_seconds"] if state["deadline_at"] is None else max(0, state["deadline_at"] - time.time())
        candidates = []
        for candidate in state["candidates"]:
            public = {key: candidate.get(key) for key in ("candidate_id", "status", "eligible", "job_id", "source_sha256", "error")}
            if state["mode"] == "tools":
                public["score"] = candidate.get("score")
            candidates.append(public)
        experiment = state["experiment"]
        planning = {}
        if experiment.get('reference_mode') == 'media_suite':
            from .media_suite import inspect_suite
            planning['reference_coverage'] = inspect_suite(state['reference'],experiment['suite_sha256'])
            planning['baseline_roles'] = {'improvement': 'original_unchanged_target', 'preservation': 'authoring_parent'}
        if state["mode"] == "tools":
            from .calibration_planning import inspect_advice
            planning["calibration_planning"] = inspect_advice(self.directory, state, _digest(source_hashes(state["target"])))
        return {"schema_version": state['schema_version'], "session_id": state["session_id"], "domain": state["domain"], "mode": state["mode"], "goal": state["goal"],
                "status": "measurement_insufficient" if closure is not None else "sealed" if deferred_final else state["status"], "target": state["target"], "editable_scope": _scope(state["domain"]),
                "protected_conditions": ["camera", "appearance", "geometry", "capture", "acceptance thresholds", "previous accepted behavior"],
                "experiment": {key: experiment[key] for key in ("experiment_id", "calibration", "mechanic", "description", "reference_mode", "input_provenance", "heldout_status", "benchmark_qualified", "execution_policy", "acceptance_profile", "acceptance_policy_sha256") if key in experiment},
                "budget": {**state["budget"], "used_candidates": len(candidates), "remaining_candidates": 0 if closure is not None else state["budget"]["max_candidates"]-len(candidates), "remaining_seconds": remaining,
                           "partial_evaluations_used": state.get("partial_evaluations", 0),
                           "measured_evaluation_seconds": self._measured_evaluation_seconds(state)},
                "prepared": state["prepared"], "active_job_id": state["final_job_id"] if deferred_final else state["active_job_id"], "candidates": candidates,
                "selected_candidate_id": state["selected_candidate_id"], "sealed_at": state["sealed_at"], "final": None if deferred_final else state["final"],
                "reference_source_visible": False, "tools_write_motion_code": False,
                **({"remeasures": [dict(r) for r in state["remeasures"]]} if state.get("remeasures") else {}),
                **({"measurement_closure": closure} if closure is not None else {}), **planning}

    def _spawn(self, job_id):
        from .environment import configure_environment
        configure_environment()
        job_dir = self._job_dir(job_id)
        with _lock(job_dir / "job.lock"):
            job = _read(job_dir / "job.json")
            if job["status"] != "queued" or job.get("worker"):
                return
            environment = os.environ.copy()
            if self._load()['experiment'].get('reference_mode') == 'media_suite':
                from .media_suite import runtime_environment
                environment.update(runtime_environment(self._load()['experiment']))
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
            environment["PYTHONIOENCODING"] = "utf-8"
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            command = [sys.executable, "-X", "utf8", "-m", "kiroclops.local_engine.session", "--worker", "--session", str(self.directory), "--job-id", job_id]
            try:
                with (job_dir / "worker.log").open("ab") as log:
                    process = subprocess.Popen(command, cwd=str(self.directory), env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **flags)
                job["worker"] = {"pid": process.pid, "identity": _identity(process.pid)}
                _atomic(job_dir / "job.json", job)
                threading.Thread(target=process.wait, name="local-session-reaper", daemon=True).start()
            except OSError as exc:
                job.update(status="failed", finished_at=time.time(), error={"code": "worker_start_failed", "message": str(exc)})
                _atomic(job_dir / "job.json", job)

    def _deferred_native_report(self):
        """Keep report packaging outside an unfinished native authoring turn."""
        launch = self.directory / "kiro" / "launch.json"
        if not launch.exists():
            return None
        try:
            record = _read(_ordinary(launch))
        except (OSError, ValueError, TypeError) as exc:
            raise VisualError("invalid_native_launch", "Cannot verify native turn status before reporting.") from exc
        if not isinstance(record, dict) or not isinstance(record.get("status"), str) or not record["status"]:
            raise VisualError("invalid_native_launch", "Native turn status is missing or malformed.")
        if record["status"] not in {"starting", "running"}:
            return None
        started = record.get("started_at")
        if (type(started) not in (int, float) or not math.isfinite(started)
                or type(record.get("prompt_sent")) is not bool):
            raise VisualError("invalid_native_launch", "Active native turn metadata is malformed.")
        if "prompt_started_at" in record:
            prompt_started = record["prompt_started_at"]
            if type(prompt_started) not in (int, float) or not math.isfinite(prompt_started) or prompt_started < started:
                raise VisualError("invalid_native_launch", "Active native prompt time is malformed.")
        return {"job_id": None, "status": "deferred_to_runner", "candidate_id": None,
                "message": "The local runner builds the report after this native turn ends."}

    def _native_authoring_open(self):
        """Keep held-out completion out of the still-open native conversation."""
        launch = self.directory / "kiro" / "launch.json"
        if not launch.exists():
            return False
        record = _read(_ordinary(launch))
        return record.get("status") in {"starting", "running"}

    def _calibration_case_ids(self, state):
        ids = []
        for item in state.get("experiment", {}).get("calibration", []):
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict):
                ids.append(item.get("case_id", item.get("id")))
        return ids

    def _measured_evaluation_seconds(self, state):
        """Median wall time of this session's completed full evaluations, or None."""
        durations = []
        for candidate in state.get("candidates", []):
            try:
                job = _read(self._job_dir(candidate["job_id"]) / "job.json")
            except (OSError, ValueError, KeyError):
                continue
            if (job.get("status") == "succeeded" and not job.get("partial")
                    and all(type(job.get(k)) in (int, float) for k in ("started_at", "finished_at"))):
                durations.append(job["finished_at"]-job["started_at"])
        return statistics.median(durations) if durations else None

    def _require_time_for_evaluation(self, state, case_ids):
        """V6 plan WP3.5: refuse an evaluation the measured budget cannot hold instead of spending it."""
        estimate = self._measured_evaluation_seconds(state)
        if estimate is None or state.get("deadline_at") is None:
            return
        total = max(1, len(self._calibration_case_ids(state)))
        if case_ids is not None:
            estimate *= len(case_ids)/total
        remaining = state["deadline_at"]-time.time()
        if remaining < EVALUATION_TIME_MARGIN*estimate:
            raise VisualError("budget_insufficient",
                              f"{remaining:.0f}s remain but a comparable evaluation took {estimate:.0f}s; "
                              "finalize an eligible candidate instead of starting a run that cannot finish.")

    def _require_remeasure(self, state):
        """V6 plan WP2.3: the last finalization failed in recording, and retries remain."""
        final = _read(self._job_dir(state["final_job_id"]) / "job.json") if state.get("final_job_id") else None
        code = ((final or {}).get("error") or {}).get("code")
        if (final is None or state["sealed_at"] is None or state["status"] != "finalization_failed"
                or final["status"] != "failed" or code not in CAPTURE_FAILURE_CODES):
            raise VisualError("remeasure_rejected", "Only a finalization that failed in recording can be measured again.")
        if len(state.get("remeasures", [])) >= MAX_REMEASURES:
            raise VisualError("budget_exhausted", f"The sealed candidate was already measured again {MAX_REMEASURES} times.")
        return final

    def submit(self, operation, candidate_id=None, case_ids=None):
        if operation not in {"prepare", "evaluate", "finalize", "report", "remeasure"}:
            raise VisualError("unsupported_operation", "Unknown session operation.")
        if operation == "remeasure" and candidate_id is not None:
            raise VisualError("invalid_request", "remeasure always uses the sealed candidate.")
        if case_ids is not None:
            # V6 plan WP3.2: a partial probe re-measures named calibration cases only.
            if operation != "evaluate":
                raise VisualError("invalid_request", "case_ids applies to evaluate only.")
            if (not isinstance(case_ids, list) or not case_ids or len(set(case_ids)) != len(case_ids)
                    or any(not isinstance(c, str) or not c for c in case_ids)):
                raise VisualError("invalid_request", "case_ids must be a nonempty list of distinct calibration case identifiers.")
        if operation == "report":
            deferred = self._deferred_native_report()
            if deferred is not None:
                return deferred
        self._reconcile_active()
        if operation == "finalize":
            # Revalidate every result used for ranking, even when a different
            # client completed the earlier job and cached its eligibility.
            for candidate in self._load()["candidates"]:
                self._reconcile_job(candidate["job_id"])
        with _lock(self.directory / "session.lock", timeout=60):
            state = self._load()
            from .stage_closure import guard_operation
            guard_operation(self.directory, state, "finalize" if operation == "remeasure" else operation)
            if operation == "report":
                deferred = self._deferred_native_report()
                if deferred is not None:
                    return deferred
            if operation == "finalize" and state["final_job_id"]:
                existing = _read(self._job_dir(state["final_job_id"]) / "job.json")
                status = "deferred_to_runner" if self._native_authoring_open() else existing["status"]
                return {"job_id": existing["job_id"], "status": status, "candidate_id": existing["candidate_id"]}
            if state["active_job_id"]:
                raise VisualError("job_active", "Wait for or cancel the active job before another operation.")
            if operation == "prepare" and state["prepared"]:
                return {"job_id": state["prepare_job_id"], "status": "succeeded"}
            if state["sealed_at"] is not None and operation in {"prepare", "evaluate"}:
                raise VisualError("session_sealed", "Finalization is irreversible; candidate editing is closed.")
            if operation in {"evaluate", "finalize"} and not state["prepared"]:
                raise VisualError("experiment_not_prepared", "Prepare the reference and baseline first.")
            failed_final = self._require_remeasure(state) if operation == "remeasure" else None
            # The sealed candidate was submitted within the authoring budget; a
            # remeasurement changes no source, so the authoring clock does not apply.
            if operation not in {"report", "remeasure"}:
                self._start_clock(state, "prepare" if operation == "prepare" else "evaluation")
                if time.time() >= state["deadline_at"]:
                    self._save(state)
                    raise VisualError("budget_exhausted", "The session's frozen authoring wall-clock budget is exhausted.")
            if operation == "evaluate" and case_ids is None and len(state["candidates"]) >= state["budget"]["max_candidates"]:
                raise VisualError("budget_exhausted", "All three candidate attempts have been consumed.")
            if operation == "evaluate" and case_ids is not None:
                unknown = [c for c in case_ids if c not in self._calibration_case_ids(state)]
                if unknown:
                    raise VisualError("invalid_request", "Unknown calibration cases: " + ", ".join(unknown))
                if state.get("partial_evaluations", 0) >= state["budget"]["max_candidates"]:
                    raise VisualError("budget_exhausted", "All partial evaluation probes have been consumed.")
            if operation == "evaluate":
                self._require_time_for_evaluation(state, case_ids)
            job_id = uuid.uuid4().hex
            job_dir = self.directory / "jobs" / job_id
            job_dir.mkdir(parents=True)
            job = {"schema_version": 1, "job_id": job_id, "operation": operation, "status": "queued", "created_at": time.time(), "started_at": None,
                   "finished_at": None, "worker": None, "progress": {"phase": "queued", "completed": 0, "total": 1}, "candidate_id": None,
                   "source_path": state["baseline"], "source_hashes": state["baseline_hashes"], "source_sha256": _digest(state["baseline_hashes"]),
                   "error": None, "result": None, "result_applied": False}
            try:
                if operation == "evaluate" and case_ids is not None:
                    probe_id = f"probe-{state.get('partial_evaluations', 0)+1:02d}"
                    state["partial_evaluations"] = state.get("partial_evaluations", 0)+1
                    job.update(candidate_id=probe_id, case_ids=list(case_ids), partial=True)
                    self._save(state)  # A failed source check still consumes this probe.
                    expected = self._verify_sources(state)
                    snapshot = self.directory / "probes" / probe_id / "source"
                    actual = copy_project(state["target"], snapshot)
                    if actual != expected:
                        raise VisualError("source_changed", "Sources changed during probe submission.")
                    job.update(source_path=str(snapshot), source_hashes=actual, source_sha256=_digest(actual))
                elif operation == "evaluate":
                    candidate_id = f"candidate-{len(state['candidates'])+1:02d}"
                    candidate = {"candidate_id": candidate_id, "created_at": time.time(), "job_id": job_id, "status": "queued", "eligible": False, "score": None, "source_sha256": None, "error": None}
                    state["candidates"].append(candidate)
                    job["candidate_id"] = candidate_id
                    self._save(state)  # Failed source checks still consume this attempt.
                    expected = self._verify_sources(state)
                    snapshot = self.directory / "candidates" / candidate_id / "source"
                    actual = copy_project(state["target"], snapshot)
                    if actual != expected:
                        raise VisualError("source_changed", "Candidate changed during submission.")
                    candidate["source_sha256"] = _digest(actual)
                    job.update(source_path=str(snapshot), source_hashes=actual, source_sha256=_digest(actual))
                elif operation == "finalize":
                    self._verify_sources(state)
                    eligible = [c for c in state["candidates"] if c["status"] == "succeeded" and c["eligible"] is True]
                    if state["mode"] == "tools":
                        eligible = [c for c in eligible if type(c.get("score")) in (int, float) and math.isfinite(c["score"]) and c["score"] >= 0]
                        chosen = min(eligible, key=lambda c: (c["score"], c["candidate_id"])) if eligible else None
                        if chosen and candidate_id is not None and candidate_id != chosen["candidate_id"]:
                            raise VisualError("candidate_selection_rejected", "Choose the eligible candidate with the lowest calibration error.")
                    else:
                        chosen = next((c for c in eligible if c["candidate_id"] == candidate_id), None) if candidate_id else (eligible[-1] if eligible else None)
                    if chosen is None:
                        raise VisualError("no_eligible_candidate", "No eligible candidate is available to finalize.")
                    snapshot = self.directory / "candidates" / chosen["candidate_id"] / "source"
                    hashes = source_hashes(snapshot)
                    if _digest(hashes) != chosen["source_sha256"]:
                        raise VisualError("source_changed", "Selected candidate snapshot changed.")
                    sealed_at = time.time()
                    if sealed_at >= state["deadline_at"]:
                        raise VisualError("budget_exhausted", "The candidate was not sealed within the authoring budget.")
                    state.update(selected_candidate_id=chosen["candidate_id"], sealed_at=sealed_at, status="sealed", final_job_id=job_id)
                    job.update(candidate_id=chosen["candidate_id"], source_path=str(snapshot), source_hashes=hashes, source_sha256=chosen["source_sha256"])
                elif operation == "remeasure":
                    # The same sealed snapshot, measured again by an ordinary finalize job.
                    snapshot = self.directory / "candidates" / failed_final["candidate_id"] / "source"
                    hashes = source_hashes(snapshot)
                    if _digest(hashes) != failed_final["source_sha256"]:
                        raise VisualError("source_changed", "Sealed candidate snapshot changed.")
                    state.setdefault("remeasures", []).append({"job_id": job_id, "after_job_id": failed_final["job_id"],
                                                               "error_code": failed_final["error"]["code"], "created_at": time.time()})
                    state.update(status="sealed", final_job_id=job_id, final=None)
                    job.update(operation="finalize", candidate_id=failed_final["candidate_id"], source_path=str(snapshot),
                               source_hashes=hashes, source_sha256=failed_final["source_sha256"],
                               retry_after_capture_failure=failed_final["job_id"])
                elif operation == "prepare":
                    self._verify_sources(state)
                    state["prepare_job_id"] = job_id
            except (VisualError, OSError) as exc:
                job.update(status="failed", finished_at=time.time(), error={"code": getattr(exc, "code", "source_error"), "message": str(exc)})
                if job["candidate_id"] and operation == "evaluate" and case_ids is None:
                    candidate.update(status="failed", error=job["error"])
                job["result_applied"] = True
            _atomic(job_dir / "job.json", job)
            _atomic(job_dir / "binding.json", {"sha256": _digest({key: job[key] for key in JOB_BINDING_FIELDS}),
                                                **({"case_ids_sha256": _digest(job["case_ids"])} if job.get("case_ids") else {}),
                                                **({"retry_after_capture_failure": job["retry_after_capture_failure"]}
                                                   if job.get("retry_after_capture_failure") else {})})
            state["active_job_id"] = job_id if job["status"] not in TERMINAL else None
            self._save(state)
        if job["status"] == "queued":
            self._spawn(job_id)
        return {"job_id": job_id, "status": job["status"], "candidate_id": job["candidate_id"]}

    def _reconcile_active(self):
        state = self._load()
        if state["active_job_id"]:
            self._reconcile_job(state["active_job_id"])

    def _reconcile_job(self, job_id):
        job_dir = self._job_dir(job_id)
        job = _read(job_dir / "job.json")
        if job["status"] not in TERMINAL:
            if job.get("worker") is None:
                self._spawn(job_id)
                job = _read(job_dir / "job.json")
            elif not _alive(job["worker"]):
                with _lock(job_dir / "job.lock"):
                    job = _read(job_dir / "job.json")
                    if job["status"] not in TERMINAL and not _alive(job.get("worker")):
                        cancelled = (job_dir / "cancel.json").exists()
                        job.update(status="cancelled" if cancelled else "interrupted", finished_at=time.time(), error={"code": "job_cancelled" if cancelled else "worker_interrupted", "message": "Owned execution stopped before committing its result."})
                        _atomic(job_dir / "job.json", job)
        if job["status"] == "succeeded":
            try:
                result = _read(job_dir / "result.json")
                valid = _digest(result) == job.get("result_sha256")
            except (OSError, ValueError):
                valid = False
            if not valid:
                job.update(status="failed", result_applied=False, error={"code": "result_integrity_failure", "message": "Job result changed or disappeared."})
                _atomic(job_dir / "job.json", job)
        if job["status"] in TERMINAL and not job["result_applied"]:
            self._apply_result(job)
            job = _read(job_dir / "job.json")
        return job

    def _apply_result(self, job):
        with _lock(self.directory / "session.lock"):
            state = self._load()
            result = _read(self._job_dir(job["job_id"]) / "result.json") if job["status"] == "succeeded" else None
            if state["active_job_id"] == job["job_id"]:
                state["active_job_id"] = None
            if job["operation"] == "prepare":
                state["prepared"] = job["status"] == "succeeded"
                state["status"] = "prepared" if state["prepared"] else "preparation_failed"
            elif job["operation"] == "evaluate" and job.get("partial"):
                # A partial probe never becomes a candidate; its result stays in its job.
                pass
            elif job["operation"] == "evaluate":
                candidate = next(c for c in state["candidates"] if c["candidate_id"] == job["candidate_id"])
                candidate.update(status=job["status"], error=job["error"], eligible=False, score=None)
                if result:
                    score = result.get("score")
                    candidate["eligible"] = result.get("eligible") is True and (state["mode"] == "baseline" or (type(score) in (int, float) and math.isfinite(score) and score >= 0))
                    candidate["score"] = score if state["mode"] == "tools" and type(score) in (int, float) and math.isfinite(score) else None
            elif job["operation"] == "finalize":
                state["status"] = "finalized" if job["status"] == "succeeded" else "finalization_failed"
                state["final"] = {"job_id": job["job_id"], "status": job["status"], "pass": result.get("pass") is True if result else False,
                                  "independent_review_status": result.get("independent_review_status", "pending") if result else "pending", "error": job["error"]}
            self._save(state)
            job["result_applied"] = True
            _atomic(self._job_dir(job["job_id"]) / "job.json", job)

    def get_job(self, job_id, wait_seconds=0, *, allow_final_result=False):
        if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 60:
            raise VisualError("invalid_request", "wait_seconds must be between zero and 60.")
        initial = _read(self._job_dir(job_id) / "job.json")
        if initial["operation"] == "finalize" and not allow_final_result and self._native_authoring_open():
            return {"job_id": initial["job_id"], "operation": "finalize", "status": "deferred_to_runner",
                    "candidate_id": initial["candidate_id"], "result": None}
        until = time.monotonic() + wait_seconds
        while True:
            job = self._reconcile_job(job_id)
            if job["status"] in TERMINAL or time.monotonic() >= until:
                break
            time.sleep(.05)
        public = {key: job.get(key) for key in ("job_id", "operation", "status", "candidate_id", "source_sha256", "created_at", "started_at", "finished_at", "progress", "error")}
        public["result"] = _read(self._job_dir(job_id) / "result.json") if job["status"] == "succeeded" else None
        return public

    def get_job_feedback(self, job_id, wait_seconds=0):
        """Agent-facing bounded response; internal consumers retain full get_job."""
        from .job_feedback import compact_job_feedback
        public=self.get_job(job_id,wait_seconds)
        state=self._load()
        return compact_job_feedback(public,state["target"],previous=self._previous_candidate_result(state,public))

    def _previous_candidate_result(self, state, public):
        """V6 plan WP3.1: the nearest earlier succeeded candidate's calibration result, for the change table.

        Only a succeeded full candidate gets one; probes and final results do not.
        The earlier result is a calibration result the author has already received.
        """
        if public.get("operation") != "evaluate" or public.get("status") != "succeeded" or not public.get("candidate_id"):
            return None
        ids = [c["candidate_id"] for c in state["candidates"]]
        if public["candidate_id"] not in ids:
            return None
        for candidate in reversed(state["candidates"][:ids.index(public["candidate_id"])]):
            path = self._job_dir(candidate["job_id"]) / "result.json" if candidate.get("job_id") else None
            if candidate.get("status") == "succeeded" and path is not None and path.is_file():
                return {"candidate_id": candidate["candidate_id"], "result": _read(path)}
        return None

    def inspect_window(self, job_id, case_id, object_id, window_id=None):
        """V6 plan WP3.2: PNG strips and an error plot for one object of a completed calibration evaluation.

        Only evaluate jobs (full candidates or partial probes) are inspectable:
        held-out validation stays sealed, and every file lands inside the target.
        """
        import re
        _validate_job_id(job_id)
        for name, value in (("case_id", case_id), ("object_id", object_id), ("window_id", window_id)):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value)):
                raise VisualError("invalid_request", f"{name} must be a simple identifier.")
        job = self._reconcile_job(job_id)
        if job["operation"] != "evaluate":
            raise VisualError("not_inspectable", "Only calibration evaluations can be inspected; held-out results stay sealed.")
        if job["status"] != "succeeded":
            raise VisualError("job_not_complete", f"Job status is {job['status']}; inspect a succeeded evaluation.")
        state = self._load()
        candidates = _read(self.directory / "measurements" / job_id / "candidate.json")
        from .prepared_evidence import load_prepared
        references = _read(Path(load_prepared(self.directory, state)["reference"]))

        def pick(rows):
            return next((r for r in rows if r["scenario"].get("case_id", r["scenario"].get("id")) == case_id), None)
        candidate, reference = pick(candidates), pick(references)
        if candidate is None or reference is None:
            raise VisualError("unknown_case", f"Case {case_id} was not measured in this evaluation.")
        result = _read(self._job_dir(job_id) / "result.json")
        comparison = next((c for c in result.get("comparisons", []) if c.get("case_id") == case_id), None)
        target = Path(state["target"]).resolve()
        folder = object_id + ("-" + window_id if window_id else "")
        output = (target / ".kiroclops-evidence" / "inspect" / job_id / case_id / folder).resolve()
        if not output.is_relative_to(target):
            raise VisualError("unsafe_path", "Inspection output escaped the target workspace.")
        from .window_inspection import build_window_inspection
        return build_window_inspection(reference, candidate, comparison, object_id, window_id, output,
                                       case_id=case_id, job_id=job_id)

    def cancel_job(self, job_id):
        job = self._reconcile_job(job_id)
        if job["status"] in TERMINAL:
            return self.get_job(job_id)
        job_dir = self._job_dir(job_id)
        _atomic(job_dir / "cancel.json", {"requested_at": time.time()})
        until = time.monotonic() + 2
        while time.monotonic() < until:
            current = _read(job_dir / "job.json")
            if current["status"] in TERMINAL:
                return self.get_job(job_id)
            if not _alive(current.get("worker")):
                break
            time.sleep(.025)
        current = _read(job_dir / "job.json")
        if _alive(current.get("worker")):
            _terminate_owned(current["worker"])
        with _lock(job_dir / "job.lock"):
            current = _read(job_dir / "job.json")
            if current["status"] not in TERMINAL:
                current.update(status="cancelled", finished_at=time.time(), error={"code": "job_cancelled", "message": "Cancelled; incomplete recordings cannot be used."})
                _atomic(job_dir / "job.json", current)
        return self.get_job(job_id)

    def close(self):
        """Closing an MCP or CLI client intentionally leaves its durable job alive."""


def run_session_job(directory, job_id, runner=None, *, owned_process=False):
    """Execute one frozen job. runner injection is trusted in-process testing only."""
    session = Session(directory)
    job_dir = session._job_dir(job_id)
    lock = threading.RLock()
    stop = threading.Event()

    def update(**changes):
        with lock, _lock(job_dir / "job.lock"):
            current = _read(job_dir / "job.json")
            if current["status"] not in TERMINAL:
                current.update(changes)
                _atomic(job_dir / "job.json", current)

    def cancelled():
        return (job_dir / "cancel.json").exists()

    def check():
        if cancelled():
            raise VisualError("job_cancelled", "Owned job was cancelled.")
        from .stage_closure import guard_execution
        guard_execution(session.directory, state, job)
        if job["operation"] == "finalize" and time.time() >= finalize_validation_deadline(state, job):
            raise VisualError("validation_budget_exhausted", "Sealed validation exceeded its bound wall-clock budget.")
        if job["operation"] not in {"finalize", "report"} and time.time() >= state["deadline_at"]:
            raise VisualError("budget_exhausted", "Session execution deadline reached.")
        if time.monotonic()-execution_started > execution_job_limit(state, job['operation']):
            raise VisualError("job_deadline_exceeded", "Execution job exceeded its frozen phase deadline.")

    def progress(phase, completed=0, total=1):
        check()
        update(progress={"phase": str(phase), "completed": int(completed), "total": int(total)})

    def fail(exc, *, exit_after=False):
        code = getattr(exc, "code", "execution_error")
        changes = {"status": "cancelled" if code == "job_cancelled" else "failed",
                   "finished_at": time.time(), "error": {"code": code, "message": str(exc), "type": type(exc).__name__}}
        # V6 plan WP3.3: the full traceback goes to the job's own log so an
        # execution error can be diagnosed; evidence I/O never masks the failure.
        try:
            # The stamp comes from the job's own finish time, so a replaced
            # ``time`` module (tests) never breaks failure recording.
            stamp = datetime.fromtimestamp(changes["finished_at"], timezone.utc).isoformat()
            with (job_dir / "worker.log").open("a", encoding="utf-8") as log:
                log.write(f"\n[{stamp}] job {job_id} failed: {code}\n")
                log.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            changes["error"]["traceback_logged"] = True
        except (OSError, ValueError, TypeError, OverflowError):
            changes["error"]["traceback_logged"] = False
        if owned_process:
            exit_code = 130 if code == "job_cancelled" else 124 if exit_after else 1
            return _publish_owned_failure(lambda: update(**changes), exit_code, exit_after=exit_after)
        # Trusted in-process evaluators retain synchronous error semantics and
        # must never terminate the host interpreter.
        update(**changes)

    def watchdog():
        while not stop.wait(.1):
            try:
                check()
            except VisualError as exc:
                fail(exc, exit_after=True)
                return

    execution_started = time.monotonic()
    # V6 plan WP3.3: only a duplicate worker (held worker.lock) returns
    # silently; every other timeout inside the job is a recorded failure.
    worker_lease = _lock(job_dir / "worker.lock", timeout=0)
    try:
        worker_lease.__enter__()
    except TimeoutError:
        stop.set()
        return
    try:
        try:
            state, job = session._load(), _read(job_dir / "job.json")
            if job["status"] in TERMINAL:
                return
            if owned_process:
                from .worker import _contain_windows_children
                containment = _contain_windows_children()
                update(worker={"pid": os.getpid(), "identity": _identity(os.getpid())})
                threading.Thread(target=watchdog, daemon=True, name="session-deadline").start()
            check()
            binding = _read(job_dir / "binding.json")
            expected_binding = binding["sha256"]
            if _digest({key: job[key] for key in JOB_BINDING_FIELDS}) != expected_binding:
                raise VisualError("job_binding_changed", "Job operation or frozen source binding changed.")
            if job.get("case_ids") is not None and binding.get("case_ids_sha256") != _digest(job["case_ids"]):
                raise VisualError("job_binding_changed", "Partial evaluation case list changed.")
            if binding.get("retry_after_capture_failure") != job.get("retry_after_capture_failure"):
                raise VisualError("job_binding_changed", "Remeasurement lineage changed.")
            lease = None
            while lease is None:
                check()
                attempted = _lock(Path(state["capture_lock"]), timeout=0)
                try:
                    attempted.__enter__()
                    lease = attempted
                except TimeoutError:
                    time.sleep(.05)
            try:
                update(status="running", started_at=time.time())
                if job["operation"] != "report" and source_hashes(job["source_path"]) != job["source_hashes"]:
                    raise VisualError("source_changed", "Frozen job source changed before execution.")
                if job["operation"] != "report" and _reference_hashes(state["reference"]) != state["reference_hashes"]:
                    raise VisualError("reference_changed", "Reference changed before execution.")
                if runner is None:
                    from .evaluation import run_job
                    runner = run_job
                result = runner(session.directory, dict(job), check, progress, cancelled)
                check()
                if not isinstance(result, dict):
                    raise VisualError("invalid_result", "Evaluator must return a JSON result object.")
                if job["operation"] != "report" and source_hashes(job["source_path"]) != job["source_hashes"]:
                    raise VisualError("source_changed", "Frozen job source changed during execution.")
                if job["operation"] != "report" and _reference_hashes(state["reference"]) != state["reference_hashes"]:
                    raise VisualError("reference_changed", "Reference changed during execution.")
                current_job = _read(job_dir / "job.json")
                if _digest({key: current_job[key] for key in JOB_BINDING_FIELDS}) != expected_binding:
                    raise VisualError("job_binding_changed", "Job binding changed during execution.")
                result = _read_json_copy(result)
                with lock, _lock(job_dir / "job.lock"):
                    check()
                    current = _read(job_dir / "job.json")
                    if current["status"] in TERMINAL:
                        return
                    _atomic(job_dir / "result.json", result)
                    current.update(status="succeeded", finished_at=time.time(), result_sha256=_digest(result), progress={"phase": "complete", "completed": 1, "total": 1})
                    _atomic(job_dir / "job.json", current)
            finally:
                lease.__exit__(None, None, None)
        finally:
            worker_lease.__exit__(None, None, None)
    except Exception as exc:
        fail(exc)
    finally:
        stop.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    run_session_job(args.session, args.job_id, owned_process=True)


if __name__ == "__main__":
    main()
