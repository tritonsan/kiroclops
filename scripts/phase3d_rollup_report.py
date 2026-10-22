"""Build a read-only Phase 3D snapshot from explicitly named local evidence.

This presentation builder does not capture, author code, certify admission, or
change historical acceptance. It replays a named current source review and, when
explicitly requested, an existing unchanged-SVG result. Use a fresh output.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
from urllib.parse import quote


PREPARATIONS = (
    ("p01", "panel", "Reference preparation and successful authoring parent"),
    ("p02", "panel", "Original target and authoring-parent measurements"),
    ("p03", "panel", "Private baseline captures; historical measurements"),
    ("p04", "panel", "Canonical revision: 48 reanalyzed and 48 verified current measurements"),
    ("gj01", "game", "Reference preparation and successful authoring parent"),
    ("gj02", "game", "Original target and authoring-parent measurements"),
    ("gj03", "game", "Private baseline captures; historical measurements"),
    ("gj04", "game", "Canonical reanalysis of preserved pixels"),
    ("sv01", "svg", "Reference preparation for unchanged-source acceptance"),
    ("sv02", "svg", "Original target and unchanged authoring-parent measurements"),
    ("sv03", "svg", "Private fixed baseline preparations, if recorded"),
    ("sv04", "svg", "Canonical SVG measurement revision, if recorded"),
)
RAW_REVIEWS = (("p02", "raw-cal-01"), ("p02", "raw-cal-02"),
               ("p02", "raw-cal-03"), ("gj03", "raw-cal-01"),
               ("gj03", "raw-cal-02"))
STAGES = ("preparation", "measurement_admission", "native_run", "native_evidence_audit",
          "current_native_audit", "current_source_review", "numeric_behavior",
          "independent_final_review", "source_and_function", "full_acceptance")


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _invalid_number(value):
    raise ValueError("Non-finite JSON number: " + value)


def _status(value):
    if value in ("pass", "fail", "pending", "partial", "invalid", "running"):
        return value
    return "pending"


class Snapshot:
    """Read each small named receipt once, retaining its byte hash and errors."""

    def __init__(self):
        self.inputs = []
        self.warnings = []
        self.cache = {}

    def read(self, path):
        path = Path(path)
        if path in self.cache:
            return self.cache[path]
        value = {}
        try:
            before = path.stat()
            raw = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("File changed while the snapshot was read; regenerate later.")
            value = json.loads(raw.decode("utf-8-sig"), parse_constant=_invalid_number)
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object.")
            self.inputs.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                                "size_bytes": len(raw), "mtime_ns": after.st_mtime_ns})
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError) as exc:
            self.warnings.append({"path": str(path), "message": str(exc)})
            value = {}
        self.cache[path] = value
        return value

    def stable(self):
        stable = True
        for row in self.inputs:
            try:
                stat = Path(row["path"]).stat()
                unchanged = (stat.st_size, stat.st_mtime_ns) == (row["size_bytes"], row["mtime_ns"])
            except OSError:
                unchanged = False
            if not unchanged:
                stable = False
                self.warnings.append({"path": row["path"], "message": "Named evidence changed during collection; regenerate the snapshot."})
        return stable

    def file(self, path):
        """Bind a validator's explicitly named non-JSON input; never discover files."""
        path = Path(path)
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Validator input changed during collection.")
        sha = hashlib.sha256(raw).hexdigest()
        self.inputs.append({"path": str(path), "sha256": sha,
                            "size_bytes": len(raw), "mtime_ns": after.st_mtime_ns})
        return sha


def _link(path, label, category="evidence"):
    path = Path(path)
    return {"path": str(path.absolute()), "label": label, "category": category} if path.is_file() else None


def _links(folder, names):
    return [link for name in names if (link := _link(folder / name, name))]


def _raw_summary(snapshot, folder, identifier):
    result = snapshot.read(folder / "result.json")
    if not result:
        return None
    attestation = snapshot.read(folder / "attestation.json")
    admission = snapshot.read(folder / "admission-attestation.json")
    proposal = _mapping(result.get("admission"))
    failures = _rows(result.get("failures"))
    result_sha = _digest(result)
    # These are recorded decisions linked to this result, not fresh validators.
    admitted = (proposal.get("status") == admission.get("status") == "pass"
                and admission.get("admitted") is True
                and admission.get("images_inspected") is True
                and admission.get("result_sha256") == result_sha
                and admission.get("whole_raw_review_accepted") is False
                and admission.get("original_event_measurements_qualified") is False)
    accepted = (result.get("numeric_status") == "pass" and not failures
                and not result.get("unsupported")
                and attestation.get("status") == "pass" and attestation.get("accepted") is True
                and attestation.get("images_inspected") is True
                and attestation.get("result_sha256") == result_sha
                and attestation.get("inspected_sheets") == result.get("sheets")
                and bool(result.get("sheets")))
    return {"id": identifier, "version": result.get("version"),
            "numeric_status": _status(result.get("numeric_status")),
            "failure_count": len(failures), "failure_codes": dict(Counter(x.get("code", "unknown") for x in failures)),
            "original_event_diagnostic_count": len(_rows(proposal.get("original_event_diagnostics"))),
            "whole_raw_review": "pass" if accepted else "fail" if result.get("numeric_status") == "fail" else "pending",
            "scoring_admission": "pass" if admitted else _status(proposal.get("status")) if not admission else "pending",
            "admission_visual_decision_recorded": admitted,
            "raw_visual_decision_recorded": accepted,
            "original_event_measurements_qualified": False if proposal else None,
            "behavior_acceptance_granted": False,
            "scope": "Pre-model raw measurement evidence; it cannot establish candidate behavior.",
            "diagnostic_scope": proposal.get("diagnostic_scope"),
            "links": _links(folder, ("result.json", "attestation.json", "admission-attestation.json",
                                      "visual-inspection.json", "evidence-receipt.json"))}


def _axis(status, note, **facts):
    return {"status": status, "note": note, **facts}


def _native_status(launch, report, binding, started):
    """Keep recorded execution failure separate from candidate measurement."""
    statuses = (launch.get("native_turn_status"), launch.get("status"))
    for failure in ("timeout", "cancelled"):
        if failure in statuses:
            return failure
    turn_status = statuses[0] or statuses[1]
    if launch.get("finished_at") is not None and turn_status:
        if turn_status != "completed":
            return "fail"
    if launch.get("status") in ("starting", "running") and launch.get("finished_at") is None:
        return "running"
    if (launch.get("status") == "completed"
            and _mapping(launch.get("response")).get("stopReason") == "end_turn"
            and launch.get("response_received_at") is not None):
        return "completed"
    if launch.get("status") == "completed":
        return "pending"
    if binding:
        return _status(report.get("native_run_status"))
    return "running" if started and launch.get("finished_at") is None else "pending"


def _current_source_axes(snapshot, directory, selected, candidate):
    """A saved PASS alone is insufficient: replay the actual current validator."""
    path = directory / "source_review.json"
    receipt = snapshot.read(path)
    source_note = "Current canonical source receipt replay; separate from saved report and executed functionality."
    native_note = "Native trace replay under the current source-supported reviewer; the original report verdict is retained."
    pending = {"current_source_review": _axis("pending", source_note),
               "current_native_audit": _axis("pending", native_note)}
    if not receipt:
        return pending
    try:
        from kiroclops.local_engine import review
        from kiroclops.local_engine.session import _ordinary
        _ordinary(path)
        code_sha = snapshot.file(Path(review.__file__))
        proof_path = Path(review.__file__).resolve().parents[2] / "reviews/staged-native227v3/source-proof-v3.json"
        policy_sha = snapshot.file(proof_path)
        expected = candidate.get("source_sha256")
        if (receipt.get("kind") != "IndependentSourceReview" or receipt.get("status") != "pass"
                or not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", selected)
                or receipt.get("candidate_id") != selected or not isinstance(expected, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected)
                or receipt.get("candidate_sha256") != expected
                or receipt.get("reviewer_engine_sha256") != code_sha
                or not isinstance(receipt.get("session_directory"), str)
                or Path(receipt["session_directory"]).absolute() != directory.absolute()
                or receipt.get("source_policy_sha256") != policy_sha
                or policy_sha != review._NATIVE_BUILTIN_POLICY_227["source_proof_sha256"]):
            raise ValueError("Current source receipt has a stale or foreign session, source, reviewer, or policy binding.")
        actual = review.validate_source_review(path, directory, candidate_sha256=expected)
        native = _mapping(actual.get("native_validation"))
        plain = {k: v for k, v in actual.items() if k != "native_validation"}
        if (_digest(plain) != _digest(receipt) or actual.get("status") != "pass"
                or native.get("status") != "pass"):
            raise ValueError("Current source/native validator did not return this exact receipt as PASS.")
        # Record the exact native bytes returned by the real replay. The
        # validator resolves native config, proof, guard and hook bindings.
        for name, expected_sha in _mapping(native.get("evidence_hashes")).items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or snapshot.file(directory / relative) != expected_sha:
                raise ValueError("Native evidence changed after validator replay.")
        for name, expected_sha in _mapping(actual.get("source_hashes")).items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or snapshot.file(directory / "candidates" / selected / "source" / relative) != expected_sha:
                raise ValueError("Selected source changed after validator replay.")
        facts = {"candidate_id": selected, "candidate_sha256": expected,
                 "reviewer_engine_sha256": code_sha, "source_policy_sha256": receipt["source_policy_sha256"],
                 "receipt_sha256": snapshot.file(path), "receipt_digest": _digest(receipt), "validator_replayed": True}
        return {"current_source_review": _axis("pass", source_note, **facts),
                "current_native_audit": _axis("pass", native_note, **facts,
                    application_call_count=_mapping(native.get("tool_outcomes")).get("application_call_count"),
                    model=native.get("model"), effort=native.get("effort"))}
    except Exception as exc:
        snapshot.warnings.append({"path": str(path), "message": "Current source/native replay failed: " + str(exc)})
        return {name: _axis("invalid", axis["note"], validator_replayed=False) for name, axis in pending.items()}


def _job_path(directory, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", job_id):
        return None
    current = directory / "jobs" / job_id / "job.json"
    return current if current.exists() else directory / "jobs/jobs" / job_id / "state.json"


def _final_numeric_status(snapshot, directory, state, candidate):
    """Promote only the named, terminal, selected-source-bound job result."""
    final = _mapping(state.get("final"))
    if not final:
        return "pending"
    job_id = state.get("final_job_id")
    path = _job_path(directory, job_id)
    if path is None or final.get("job_id") != job_id:
        return "invalid"
    job = snapshot.read(path)
    if not job:
        return "invalid" if path.exists() else "pending"
    if job.get("status") in ("queued", "running"):
        return "pending"
    if job.get("status") in ("failed", "cancelled", "interrupted"):
        return "invalid"
    source = candidate.get("source_sha256")
    hashes = _mapping(job.get("source_hashes"))
    if (job.get("status") != "succeeded" or job.get("operation") != "finalize"
            or job.get("job_id") != job_id or job.get("candidate_id") != state.get("selected_candidate_id")
            or not source or job.get("source_sha256") != source or not hashes or _digest(hashes) != source
            or state.get("sealed_at") is None or final.get("status") != "succeeded"
            or type(final.get("pass")) is not bool):
        return "invalid"
    result_path = path.parent / "result.json"
    result = snapshot.read(result_path)
    if not result:
        return "invalid" if result_path.exists() else "pending"
    if (_digest(result) != job.get("result_sha256") or type(result.get("pass")) is not bool
            or result["pass"] is not final["pass"]):
        return "invalid"
    return "pass" if final["pass"] else "fail"


def _run_summary(snapshot, root, run_result=None, *, directory=None, preparation=None):
    directory = Path(directory) if directory is not None else root / "s/gj04"
    preparation = Path(preparation) if preparation is not None else root / "p/gj04"
    state = snapshot.read(directory / "session.json")
    admission = snapshot.read(directory / "admission.json")
    readiness = snapshot.read(directory / "environment-readiness.json")
    launch = snapshot.read(directory / "kiro/launch.json")
    report = snapshot.read(directory / "report/report.json")
    data_report = snapshot.read(directory / "report/data.json")
    if not report and data_report.get("kind") == "LocalEvidenceReport":
        report = data_report
    extra = snapshot.read(run_result) if run_result else {}
    interruption = snapshot.read(directory / "operator-interruption.json")
    cleanup = snapshot.read(directory / "operator-terminal-cleanup.json")
    final = _mapping(state.get("final"))
    numerical = _mapping(report.get("numerical"))
    selected = state.get("selected_candidate_id")
    candidates = _rows(state.get("candidates"))
    candidate = next((row for row in candidates
                      if row.get("candidate_id") == selected), {})
    numeric = _final_numeric_status(snapshot, directory, state, candidate)
    binding = bool(report.get("kind") == "LocalEvidenceReport" and selected
                   and report.get("selected_candidate_id") == selected
                   and report.get("candidate_sha256") == candidate.get("source_sha256")
                   and candidate.get("source_sha256")
                   and numerical.get("job_id") == final.get("job_id") == state.get("final_job_id")
                   and final.get("job_id")
                   and numeric in ("pass", "fail")
                   and numerical.get("pass") is final.get("pass"))
    suite_sha = _mapping(state.get("experiment")).get("suite_sha256")
    admitted = (admission.get("kind") == "ReferenceSuiteAdmission" and admission.get("status") == "pass"
                and suite_sha and admission.get("suite_sha256") == suite_sha
                and admission.get("model_started") is False
                and admission.get("behavior_acceptance_granted") is False
                and readiness.get("measurement_revision_required") is not True)
    started = state.get("started_at") is not None or launch.get("prompt_sent") is True
    native = _native_status(launch, report, binding, started)
    behavior_axis = _mapping(_mapping(report.get("acceptance_axes")).get("observed_behavior"))
    function_axis = _mapping(_mapping(report.get("acceptance_axes")).get("application_integrity"))
    if (started or native in ("timeout", "cancelled", "fail")) and not candidates and not final:
        numeric = "not_evaluated"
    visual = "pending"
    source = "pending"
    if binding:
        visual = ("pass" if report.get("independent_review_status") == report.get("review_coverage_status") == "pass"
                  else "fail" if "fail" in (report.get("independent_review_status"), report.get("review_coverage_status")) else "pending")
        source = ("pass" if report.get("source_review_status") == function_axis.get("status") == "pass"
                  else "fail" if "fail" in (report.get("source_review_status"), function_axis.get("status")) else "pending")
    native_gate = _status(report.get("native_run_status")) if native == "completed" and binding else native
    required = ["pass" if admitted else "pending", native_gate, numeric, visual, source]
    full = ("fail" if any(value in ("fail", "invalid", "timeout", "cancelled") for value in required) else
            "pass" if binding and all(x == "pass" for x in required)
            and behavior_axis.get("status") == "pass" and report.get("overall_acceptance") == "pass"
            else "partial" if binding and report.get("overall_acceptance") == "partial" else "pending")
    if interruption:
        native, numeric, visual, source, full = "operator_interrupted", "not_evaluated", "pending", "pending", "pending"
    native_note = ("Recorded operator interruption; candidate behavior was not evaluated." if interruption else
                   "Recorded native execution timed out; candidate evaluation is reported separately." if native == "timeout" else
                   "Recorded native execution was cancelled; candidate evaluation is reported separately." if native == "cancelled" else
                   "Recorded native execution ended with an error; candidate evaluation is reported separately." if native == "fail" else
                   "Recorded authoring turn completed with end_turn; native evidence audit and finalization are separate gates." if native == "completed" else
                   "Native execution and its saved audit are separate from numeric behavior.")
    numeric_note = ("No candidate evaluation or sealed final numeric verdict was recorded." if numeric == "not_evaluated" else
                    "Only the sealed final job supplies this numeric verdict.")
    axes = {
        "preparation": _axis("pass" if snapshot.read(preparation / "result.json").get("status") == "ready_for_raw_review" and not readiness.get("measurement_revision_required") else "pending", "Canonical preparation of preserved pixels; no behavior claim.", session_status=state.get("status")),
        "measurement_admission": _axis("pass" if admitted else "pending", "Saved ReferenceSuiteAdmission must match this session's suite revision.", suite_sha256=suite_sha),
        "native_run": _axis(native, native_note, launch_status=launch.get("status"), native_turn_status=launch.get("native_turn_status"), error=launch.get("error"), model=launch.get("model"), effort=launch.get("effort"), started=started, finished_at=launch.get("finished_at"), recorded_terminal_cleanup_status=_mapping(launch.get("terminal_cleanup")).get("status"), operator_interruption=interruption, operator_cleanup_status=cleanup.get("status")),
        "native_evidence_audit": _axis(_status(report.get("native_run_status")) if binding else "pending", "Historical native audit verdict at the time of the saved video report; retained separately from current replay. Completed authoring alone does not pass native evidence audit.", historical_report_verdict=True, authoring_response=_mapping(launch.get("response")).get("stopReason"), response_received_at=launch.get("response_received_at")),
        "numeric_behavior": _axis(numeric, numeric_note, final_job_id=final.get("job_id"), candidate_id=selected, candidate_count=len(candidates), saved_observed_behavior=behavior_axis.get("status")),
        "independent_final_review": _axis(visual, "Requires saved final visual review and exact observation coverage; pre-model sheets do not qualify it."),
        "source_and_function": _axis(source, "Requires saved source/native-trace audit and executed application-integrity checks."),
        "full_acceptance": _axis(full, "Conjunction of recorded final video-report gates, linked to the selected session result; new source replay alone cannot promote it.", session_report_binding=binding),
    }
    axes.update(_current_source_axes(snapshot, directory, selected, candidate))
    links = _links(directory, ("session.json", "admission.json", "environment-readiness.json", "independent_review.json", "source_review.json",
                               "operator-interruption.json", "operator-terminal-cleanup.json",
                               "kiro/launch.json", "kiro/tool-audit.jsonl", "kiro/run/trace.jsonl", "kiro/run/events.jsonl",
                               "kiro/run/stdout.jsonl", "kiro/run/outbound.jsonl", "kiro/run/stderr.log",
                               "report/index.html", "report/report.json", "report/data.json", "report/candidate.diff"))
    jobs = []
    job_ids = {state.get(name) for name in ("active_job_id", "prepare_job_id", "final_job_id")}
    job_ids.update(row.get("job_id") for row in _rows(state.get("candidates")))
    for job_id in sorted(x for x in job_ids if isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", x)):
        job_path = _job_path(directory, job_id)
        job = snapshot.read(job_path)
        jobs.append({"job_id": job_id, "status": job.get("status", "pending"), "operation": job.get("operation")})
        if link := _link(job_path, "Job " + job_id):
            links.append(link)
    if run_result and (link := _link(run_result, "Explicit native run result")):
        links.append(link)
    # Media paths are taken from the named report; no artifact tree is scanned.
    media = []
    for group in _rows(report.get("groups")):
        for role, item in _mapping(group.get("roles")).items():
            for key, category in (("video", "video"), ("events", "events"), ("event_file", "events")):
                value = _mapping(item).get(key)
                if isinstance(value, str) and "://" not in value:
                    link = _link(directory / "report" / value, f"{group.get('id', '')} · {role} · {key}", category)
                    if link:
                        media.append(link)
    return {"session": str(directory), "axes": axes, "links": links, "media": media, "jobs": jobs,
            "session_evidence_present": bool(state or launch or report or admission or readiness),
            "explicit_run_result_status": extra.get("status"), "saved_report_overall_acceptance": report.get("overall_acceptance")}


def _svg_initial_summary(snapshot, directory):
    """Replay only an explicitly named existing initial measurement; never run it."""
    directory = Path(directory).absolute()
    saved = snapshot.read(directory / "result.json")
    plan_saved = snapshot.read(directory / "initial-plan.json")
    start = snapshot.read(directory / "run-start.json")
    links = _links(directory, ("initial-plan.json", "initial-plan-binding.json", "run-start.json",
                               "result.json", "result-binding.json", "comparisons.json", "preservation.json",
                               "functional.json", "report.html", "index.html"))
    axes = {
        "measurement_run": _axis("running" if start and not saved else "pending", "Operator measurement of unchanged source; no fresh model or authoring run."),
        "unchanged_source": _axis("pending", "Requires current plan/code/source verification."),
        "measurement_qualification": _axis("pending", "All 112 exact source-role/case records must pass current bindings."),
        "numeric_behavior": _axis("pending", "Only a durable bound result with current comparison replay supplies a numeric verdict."),
        "parent_preservation": _axis("pending", "All 28 exact parent comparisons remain required."),
        "executed_functional_checks": _axis("pending", "Bound initial functional outputs are separate from the independent execution witness."),
        "independent_final_review": _axis("pending", "Initial numerical evidence does not certify visual inspection."),
        "current_source_review": _axis("pending", "Historical source authorship is separate from a new unchanged-matrix source closure."),
        "historical_generation": _axis("pending", "The historical link is not a fresh Kiro run or native audit."),
        "full_acceptance": _axis("pending", "This initial-result presentation cannot certify full behavior acceptance."),
    }
    summary = {"directory": str(directory), "axes": axes, "links": links, "groups": {},
               "model_started": False, "new_model_generation": False, "candidate_count": 0,
               "full_behavior_acceptance": False, "validator_replayed": False, "metadata_bindings": []}
    if not saved:
        if (directory / "result.json").exists():
            axes["measurement_run"] = _axis("invalid", "The named result could not be read as a finite JSON object.")
        return summary
    try:
        from kiroclops.local_engine import suite_initial_acceptance as initial
        from kiroclops.local_engine import suite_svg_final_acceptance as closure
        from kiroclops.visual.schema import VisualError
        actual = initial.load_result(directory)
        plan, suite = initial.load_plan(directory)
        if _digest(actual) != _digest(saved) or _digest(plan) != _digest(plan_saved):
            raise ValueError("Named SVG result or plan changed during verification.")
        for name in ("initial-plan-binding.json", "result-binding.json"):
            snapshot.read(directory / name)
        original = initial.media_suite.safe_path(plan["suite"], suite["original"], must_exist=False)
        for project, hashes in ((plan["unchanged_source"], plan["authoring_parent_source_hashes"]),
                                (plan["authoring_parent"], plan["authoring_parent_source_hashes"]),
                                (original, plan["original_source_hashes"])):
            for name, expected in hashes.items():
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or snapshot.file(Path(project) / relative) != expected:
                    raise ValueError("SVG source changed after authoritative plan verification.")
        for field in ("measurement_source_hashes", "orchestration_source_hashes", "workflow_source_hashes"):
            for name, expected in _mapping(plan.get(field)).items():
                if name.startswith("@"):
                    # suite_revision.measurement_source_hashes has exactly
                    # this metadata sentinel. load_plan has already compared
                    # the complete dictionary with the current producer API.
                    if (field != "measurement_source_hashes" or name != "@stage"
                            or plan.get("stage") != "svg" or plan.get("domain") != "svg"
                            or expected != _digest("svg")):
                        raise ValueError("Unknown or contradictory SVG source-inventory metadata sentinel.")
                    summary["metadata_bindings"].append({"field": field, "key": name, "value": expected,
                                                         "authoritative_loader_verified": True})
                    continue
                semantic = name.endswith("#measurement_ast")
                filename = name[:-len("#measurement_ast")] if semantic else name
                path = Path(filename)
                if not path.is_absolute():
                    path = initial.media_suite.ROOT / path
                current = snapshot.file(path)
                # The authoritative loader checks the approved semantic slice;
                # retain full-byte stability during this report as well.
                if not semantic and current != expected:
                    raise ValueError("SVG frozen code changed after authoritative plan verification.")
        summary.update(validator_replayed=True, source_sha256=plan["unchanged_source_sha256"],
                       suite_sha256=plan["suite_sha256"], saved_status=actual.get("status"))
        axes["unchanged_source"] = _axis("pass", "Current authoritative loader verified exact unchanged parent, frozen code, inputs and thresholds.", source_sha256=plan["unchanged_source_sha256"])
        axes["historical_generation"] = _axis("historical_linked", "Current plan verifies the historical candidate/source link; no fresh generation audit is asserted.", historical_only=True, evidence=actual.get("historical_generation"))
        if actual.get("complete") is not True:
            axes["measurement_run"] = _axis("fail", "The durable initial measurement did not complete.", code=actual.get("code"))
            axes["numeric_behavior"] = _axis("not_evaluated", "An incomplete matrix cannot establish behavior failure or success.")
            return summary
        cases = initial._matrix(suite)
        if len(cases) != 28:
            raise ValueError("The complete unchanged SVG protocol requires 28 cases.")
        rows = {}
        for role in closure.ROLES:
            path = directory / "indexes" / (role + ".json")
            expected = snapshot.file(path)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("SVG matrix index changed during verification.")
            values = json.loads(raw.decode("utf-8-sig"), parse_constant=_invalid_number)
            if not isinstance(values, list):
                raise ValueError("An SVG role index must be an array.")
            for record in values:
                key = (role, record["case_id"])
                if key in rows:
                    raise ValueError("Duplicate SVG role/case binding.")
                rows[key] = record
        if set(rows) != {(role, case["case_id"]) for role in closure.ROLES for case in cases}:
            raise ValueError("The exact 112-record SVG matrix is incomplete or foreign.")
        producers = {"original": initial._source_expectation(original, plan["original_source_hashes"], "svg"),
                     "before": initial._source_expectation(plan["authoring_parent"], plan["authoring_parent_source_hashes"], "svg"),
                     "unchanged": initial._source_expectation(plan["unchanged_source"], plan["authoring_parent_source_hashes"], "svg")}
        for case in cases:
            for role in closure.ROLES:
                initial._qualified(rows[role, case["case_id"]], case, suite, producers.get(role), sources=plan["measurement_source_hashes"])
        for name in ("comparisons.json", "preservation.json", "functional.json"):
            snapshot.file(directory / name)
        try:
            closure._numerics(directory, plan, actual, cases, rows)
        except VisualError as exc:
            if exc.code != "svg_numerical_failure":
                raise
            # This specific exception follows complete equality/replay checks;
            # it means genuine numerical/functional failure, not unknown data.
        final_plan, final_suite = initial.load_plan(directory)
        if _digest(final_plan) != _digest(plan) or _digest(final_suite) != _digest(suite):
            raise ValueError("Current SVG plan/source/code changed during qualification or replay.")
        groups = {role: {"required": sum(case["role"] == role for case in cases),
                        "passed": sum(row["role"] == role and row.get("pass") is True for row in actual["comparisons"]),
                        "pass": all(row.get("pass") is True for row in actual["comparisons"] if row["role"] == role)}
                  for role in initial.media_suite.ROLES}
        measured = all(initial._measurement_ok(row) for row in actual["comparisons"] + actual["parent_preservation"])
        if _digest(groups) != _digest(actual.get("groups")) or actual.get("measurement_qualified") is not measured:
            raise ValueError("Saved SVG group/qualification summary contradicts current replay.")
        passed = actual.get("numerical_pass") is True
        failure = measured and not passed
        status = "numerical_pass_pending_review" if passed else "behavior_fail_pending_review" if failure else "measurement_failed"
        if (actual.get("status") != status or type(actual.get("numerical_pass")) is not bool
                or actual.get("behavior_failure_observed") is not failure
                or actual.get("repair_preregistration_allowed") is not failure):
            raise ValueError("Saved SVG behavior/status projection contradicts current replay.")
        summary["groups"] = groups
        axes["measurement_run"] = _axis("completed", "Recorded operator measurement completed; zero new model candidates.")
        axes["measurement_qualification"] = _axis("pass" if measured else "fail", "All 112 source/worker/pixel bindings replayed; comparison coverage/order/geometry qualification remains mandatory.")
        axes["numeric_behavior"] = _axis("pass" if passed else "fail" if measured else "not_evaluated", "Current exact comparisons and functional-output projection replayed; an unqualified comparison is not behavior failure.", groups=groups)
        preserved = all(initial._measurement_ok(row) and row.get("pass") is True for row in actual["parent_preservation"])
        axes["parent_preservation"] = _axis("pass" if preserved else "fail", "All 28 exact parent comparisons were replayed.", required=28, passed=sum(row.get("pass") is True for row in actual["parent_preservation"]))
        functional = actual["functional"]
        functional_pass = functional.get("status") == "pass" and not functional.get("page_errors") and all(row.get("pass") is True for row in functional["checks"])
        axes["executed_functional_checks"] = _axis("pending" if functional_pass else "fail", "Exact saved five-check output replayed; an independent owned-execution witness remains required.", saved_functional_output_status="pass" if functional_pass else "fail", independent_execution_witness_status="pending")
    except Exception as exc:
        snapshot.warnings.append({"path": str(directory), "message": "Unchanged SVG result replay failed: " + str(exc)})
        for name in ("measurement_qualification", "numeric_behavior", "parent_preservation", "executed_functional_checks"):
            axes[name] = _axis("invalid", "Current authoritative SVG verification/replay did not establish this gate.")
        axes["measurement_run"] = _axis("invalid", "The named initial result did not survive current verification.")
        if axes["unchanged_source"]["status"] != "pass":
            axes["unchanged_source"] = _axis("invalid", "Current plan/code/source verification did not establish immutable source.")
    return summary


def collect(root=Path("C:/kc3d"), *, run_result=None, recovery_session=None, recovery_preparation=None, svg_initial=None):
    if (recovery_session is None) != (recovery_preparation is None):
        raise ValueError("Provide both recovery session and preparation explicitly.")
    root = Path(root).absolute()
    snapshot = Snapshot()
    preparations = []
    for identifier, domain, scope in PREPARATIONS:
        folder = root / "p" / identifier
        result = snapshot.read(folder / "result.json")
        progress = snapshot.read(folder / "progress.json")
        preparations.append({"id": identifier, "domain": domain, "scope": scope,
                             "status": result.get("status", "missing"),
                             "model_started": result.get("model_started"),
                             "progress": {k: progress.get(k) for k in ("phase", "completed", "total", "updated_at")},
                             "counts": {k: result[k] for k in ("reference_cases", "initial_cases", "private_baseline_records", "reanalyzed_records") if k in result},
                             "measurement_revision_required": result.get("measurement_revision_required"),
                             "links": _links(folder, ("result.json", "progress.json", "revision.json", "cache.json"))})
    raw = [row for identifier, name in RAW_REVIEWS
           if (row := _raw_summary(snapshot, root / "p" / identifier / name, identifier + "/" + name))]
    qualification_path = root / "q/pool01-qualification.json"
    qualification = snapshot.read(qualification_path)
    run = _run_summary(snapshot, root, run_result)
    recovery = (_run_summary(snapshot, root, directory=recovery_session, preparation=recovery_preparation)
                if recovery_session is not None else None)
    panel_raw = next((row for row in raw if row["id"] == "p02/raw-cal-03"), {})
    panel_run = _run_summary(snapshot, root, directory=root / "s/p04", preparation=root / "p/p04")
    svg = _svg_initial_summary(snapshot, svg_initial) if svg_initial is not None else None
    panel = panel_run["axes"]
    if not panel_run["session_evidence_present"]:
        # Preserve the historical preparation-only view when no p04 run exists.
        panel["preparation"] = _axis("pass" if snapshot.read(root / "p/p02/result.json").get("status") == "ready_for_raw_review" else "pending", "Panel original/authoring-parent preparation.")
        panel["measurement_admission"] = _axis("pass" if panel_raw.get("admission_visual_decision_recorded") else "pending", "Recorded panel v3 scoring admission only; whole raw review and original-event diagnostics remain separate.")
    active = recovery or run
    stable = snapshot.stable()
    if not stable:
        for recorded in [run, panel_run] + ([recovery] if recovery else []):
            recorded["axes"]["full_acceptance"] = _axis("pending", "Evidence changed during this snapshot; regenerate before interpreting acceptance.")
            for name in ("current_source_review", "current_native_audit"):
                recorded["axes"][name] = _axis("pending", "Evidence changed during collection; regenerate before interpreting current review.")
        if svg:
            for name in svg["axes"]:
                svg["axes"][name] = _axis("pending", "Evidence changed during collection; regenerate this initial-result snapshot.")
    full_statuses = [recorded["axes"]["full_acceptance"]["status"] for recorded in (active, panel_run)]
    aggregate = "fail" if "fail" in full_statuses else "partial" if any(value in ("pass", "partial") for value in full_statuses) else "pending"
    return {"kind": "Phase3DRollupSnapshot", "schema_version": 3,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(), "root": str(root),
            "historical_3a_cohort_changed": False, "status": aggregate, "full_phase3d_acceptance_granted": False,
            "scope": "Named Phase 3D panel/game/SVG preparations, gj04, any explicitly supplied recovery, the p04 panel native session and an optional explicit unchanged-SVG initial result. This is a read-only presentation snapshot, not a new acceptance receipt.",
            "baseline_roles": {"original": "Original unchanged target; baseline for required improvement.",
                               "before": "Successful authoring parent; baseline for preservation.",
                               "candidate_final": "Model-authored candidate and its sealed final validation."},
            "preparations": preparations, "raw_reviews": raw,
            "prepared_pool_qualification": {"status": qualification.get("status", "pending"), "kind": qualification.get("kind"),
                "excluded_from_fitting_and_admission": qualification.get("excluded_from_fitting_and_admission"),
                "model_started": qualification.get("model_started"), "behavior_acceptance_granted": False,
                "scope": "Prepared-target qualification only.", "links": [link] if (link := _link(qualification_path, "Pool qualification")) else []},
            "game_run": run, "recovery_run": recovery, "panel_run": panel_run, "panel_progression": panel,
            "svg_initial": svg, "snapshot_stable": stable, "inputs": snapshot.inputs, "warnings": snapshot.warnings,
            "limitations": ["No artifact discovery, capture, analysis, video encoding, review certification, or acceptance mutation is performed.",
                            "Missing, unreadable, changing, or not-yet-written live evidence remains pending; recorded execution failures and absent candidate evaluations are shown separately.",
                            "Raw pre-model measurement passes do not establish native execution or behavior acceptance.",
                            "The original video-report native/source verdicts are retained. Current source receipts use the actual current validator and separate axes; they do not pass functionality or visual review.",
                            "Explicit unchanged-SVG results replay their current immutable plan, matrix bindings and numeric outputs; historical generation, independent visual/source closure and execution witnesses remain separate.",
                            "Earlier 3A acceptance is unchanged. This report covers only the explicitly named Phase 3D evidence."]}


def _href(path, output):
    path = Path(path)
    try:
        return quote(Path(os.path.relpath(path, output)).as_posix(), safe="/:")
    except ValueError:  # Windows evidence and output may be on different drives.
        return path.as_uri()


def render(data, output):
    def esc(value):
        return html.escape(str(value))

    def links(rows):
        return " · ".join(f'<a href="{esc(_href(row["path"], output))}">{esc(row["label"])}</a>' for row in rows)

    axes = "".join(f'<tr><th>{esc(name.replace("_", " "))}</th><td>{esc(data["game_run"]["axes"][name]["status"])}</td><td>{esc(data["game_run"]["axes"][name]["note"])}</td></tr>' for name in STAGES)
    panel_axes = "".join(f'<tr><th>{esc(name.replace("_", " "))}</th><td>{esc(data["panel_progression"][name]["status"])}</td><td>{esc(data["panel_progression"][name]["note"])}</td></tr>' for name in STAGES)
    recovery = data.get("recovery_run")
    recovery_html = ""
    if recovery:
        recovery_axes = "".join(f'<tr><th>{esc(name.replace("_", " "))}</th><td>{esc(recovery["axes"][name]["status"])}</td><td>{esc(recovery["axes"][name]["note"])}</td></tr>' for name in STAGES)
        recovery_html = f'<h2>Separately registered recovery · {esc(recovery["session"])}</h2><div class="table"><table><tbody>{recovery_axes}</tbody></table></div><p>{links(recovery["links"])}</p>'
    preparations = "".join(f'<tr><th>{esc(row["id"])}</th><td>{esc(row["scope"])}</td><td>{esc(row["status"])}</td><td>{esc(row["progress"])}</td><td>{links(row["links"])}</td></tr>' for row in data["preparations"])
    raw = "".join(f'<tr><th>{esc(row["id"])}</th><td>{esc(row["whole_raw_review"])} ({row["failure_count"]} failures)</td><td>{esc(row["scoring_admission"])}; visual decision: {esc(row["admission_visual_decision_recorded"])}</td><td>{row["original_event_diagnostic_count"]}; original event timing qualified: {esc(row["original_event_measurements_qualified"])}</td><td>{links(row["links"])}</td></tr>' for row in data["raw_reviews"])
    panel_run = data.get("panel_run", {})
    panel_links = links(panel_run.get("links", []))
    media = "".join(f'<li>{links([row])}</li>' for row in data["game_run"]["media"] + (recovery["media"] if recovery else []) + panel_run.get("media", []))
    notes = "".join(f'<li>{esc(note)}</li>' for note in data["limitations"])
    warnings = "".join(f'<li>{esc(row["path"])}: {esc(row["message"])}</li>' for row in data["warnings"])
    roles = "".join(f'<li><strong>{esc(role)}</strong>: {esc(note)}</li>' for role, note in data["baseline_roles"].items())
    pool = data["prepared_pool_qualification"]
    svg = data.get("svg_initial")
    svg_html = ""
    if svg:
        svg_axes = "".join(f'<tr><th>{esc(name.replace("_", " "))}</th><td>{esc(axis["status"])}</td><td>{esc(axis["note"])}</td></tr>' for name, axis in svg["axes"].items())
        svg_html = f'<h2>SVG unchanged-source initial measurement</h2><p>{esc(svg["directory"])} · No fresh model generation; full behavior acceptance: false. {links(svg["links"])}</p><div class="table"><table><tbody>{svg_axes}</tbody></table></div>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Phase 3D evidence rollup</title>
<style>body{{font:16px system-ui;margin:2rem;line-height:1.5;color:#17202a;background:#fafafa}}main{{max-width:1200px;margin:auto}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}th,td{{text-align:left;vertical-align:top;border:1px solid #ccd2d8;padding:.6rem}}th{{background:#eef1f4}}a{{color:#07549b}}.table{{overflow:auto}}small{{color:#52606d}}</style></head><body><main>
<h1>Phase 3D evidence rollup</h1><p><strong>Phase 3D completion: {esc(data['status'])}</strong> · Recorded game full acceptance: {esc(data['game_run']['axes']['full_acceptance']['status'])}</p><p>{esc(data['scope'])}</p><small>Snapshot: {esc(data['generated_at_utc'])}</small>
<h2>Baselines</h2><ul>{roles}</ul><h2>Game progression · gj04</h2><div class="table"><table><thead><tr><th>Stage</th><th>Status</th><th>Scope</th></tr></thead><tbody>{axes}</tbody></table></div>
<p>{links(data['game_run']['links'])}</p>{recovery_html}<h2>Panel progression</h2><p>{esc(panel_run.get('session', ''))} {panel_links}</p><div class="table"><table><thead><tr><th>Stage</th><th>Status</th><th>Scope</th></tr></thead><tbody>{panel_axes}</tbody></table></div><h2>Preparation history</h2><div class="table"><table><thead><tr><th>Record</th><th>Scope</th><th>Saved status</th><th>Progress</th><th>Evidence</th></tr></thead><tbody>{preparations}</tbody></table></div>
{svg_html}<h2>Pre-model raw measurement review</h2><p>Panel scoring admission and whole raw review are separate. Original panel event timings remain unqualified and do not feed scoring. Raw passes do not grant behavior acceptance.</p><div class="table"><table><thead><tr><th>Review</th><th>Whole raw review</th><th>Scoring admission proposal</th><th>Original-event diagnostics</th><th>Evidence</th></tr></thead><tbody>{raw}</tbody></table></div>
<h2>Prepared pool qualification</h2><p>{esc(pool['status'])} · {esc(pool['scope'])} {links(pool['links'])}</p><h2>Existing videos and event evidence</h2><ul>{media or '<li>No presentation media recorded yet.</li>'}</ul>
<h2>Scope and limits</h2><ul>{notes}</ul><h2>Incomplete reads</h2><ul>{warnings or '<li>No read errors in the named files.</li>'}</ul><p><a href="status.json">JSON snapshot and input SHA256 hashes</a></p></main></body></html>'''


def build(output, root=Path("C:/kc3d"), *, run_result=None, recovery_session=None, recovery_preparation=None, svg_initial=None):
    output, root = Path(output).resolve(), Path(root).resolve()
    for branch in ("p", "s", "q"):
        if output.is_relative_to(root / branch):
            raise ValueError("Report output must be outside preparation, session, and qualification evidence.")
    if output.exists():
        raise ValueError("Preserve existing reports; use a fresh output directory.")
    if recovery_session is not None and recovery_preparation is None:
        raise ValueError("Provide the recovery preparation explicitly; do not infer a registered revision.")
    if recovery_session is not None and output.is_relative_to(Path(recovery_session).resolve()):
        raise ValueError("Report output must be outside recovery evidence.")
    if recovery_preparation is not None and output.is_relative_to(Path(recovery_preparation).resolve()):
        raise ValueError("Report output must be outside recovery evidence.")
    if svg_initial is not None and (output.is_relative_to(Path(svg_initial).resolve()) or Path(svg_initial).resolve().is_relative_to(output)):
        raise ValueError("Report output and unchanged-SVG evidence must not contain each other.")
    data = collect(root, run_result=run_result, recovery_session=recovery_session, recovery_preparation=recovery_preparation, svg_initial=svg_initial)
    output.mkdir(parents=True)
    (output / "status.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "index.html").write_text(render(data, output), encoding="utf-8")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("C:/kc3d"))
    parser.add_argument("--output", type=Path, required=True, help="Fresh report directory outside evidence roots")
    parser.add_argument("--run-result", type=Path, help="Optional explicit native-run summary; linked, never promoted to final acceptance")
    parser.add_argument("--recovery-session", type=Path, help="Explicit separately registered recovery session; original gj04 remains visible")
    parser.add_argument("--recovery-preparation", type=Path, help="Explicit canonical preparation belonging to the recovery")
    parser.add_argument("--svg-initial", type=Path, help="Explicit existing unchanged-SVG initial result; verifies current bytes and replays saved comparisons, never starts measurement")
    args = parser.parse_args()
    data = build(args.output, args.root, run_result=args.run_result,
                 recovery_session=args.recovery_session, recovery_preparation=args.recovery_preparation, svg_initial=args.svg_initial)
    print(json.dumps({"status": data["status"], "output": str(args.output), "warnings": len(data["warnings"])}))


if __name__ == "__main__":
    main()
