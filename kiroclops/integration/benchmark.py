"""Conservative, local aggregation of explicitly selected Phase 2 sessions.

No discovery, captures, model calls or result repair happens here. Missing
evidence stays unavailable; a model argument and an allowed write request are
not treated as runtime model confirmation or proof of authorship.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import statistics
import tempfile
from urllib.parse import unquote, urlsplit

from kiroclops.common import ROOT
from .kiro import KiroError, StreamConfigurationGuard, _reported_models
from .storage import project_hashes
from .ide_evidence import IDEExportError, audit_ide_export

EXPECTED_MODEL = "claude-opus-5"
EXPECTED_EFFORT = "high"
EXPECTED_RUNS_PER_ARM = 3
MAX_METADATA_BYTES = 64 * 1024 * 1024
VALIDATION_SLOTS = {(hold, offset, repeat, 30, "validation")
                    for hold in (8, 14) for offset in (0, 24) for repeat in (1, 2, 3)}


def _slot(scenario):
    return tuple(scenario.get(key) for key in ("hold_ticks", "spawn_offset", "repeat", "start_tick", "split"))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _session_dir(value):
    base = (ROOT / "sessions").resolve()
    if base != ROOT.resolve() / "sessions":
        raise ValueError("ROOT/sessions must not redirect outside the workspace")
    path = Path(value)
    path = (path if path.is_absolute() else base / path).resolve()
    if path.parent != base or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}", path.name):
        raise ValueError("benchmark inputs must be explicit immediate children of ROOT/sessions")
    return path


def _local(session, value):
    path = Path(value)
    path = (path if path.is_absolute() else session / path).resolve()
    if not path.is_relative_to(session):
        raise ValueError("benchmark evidence path escapes the selected session")
    return path


def _read(session, value, issues, *, optional=False):
    path = _local(session, value)
    if not path.is_file():
        if not optional:
            issues.append(f"Missing: {path.relative_to(session).as_posix()}")
        return None
    try:
        if path.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError("metadata size exceeds the local reporting limit")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        issues.append(f"Invalid {path.relative_to(session).as_posix()}: {type(exc).__name__}")
        return None


def _jsonl(session, relative, issues):
    path = _local(session, relative)
    if not path.is_file():
        return [], "unavailable"
    if path.stat().st_size > MAX_METADATA_BYTES:
        issues.append(f"Oversize metadata: {relative}")
        return [], "invalid"
    result, malformed = [], 0
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    result.append(event)
                else:
                    malformed += 1
            except ValueError:
                malformed += 1
    if malformed:
        issues.append(f"{relative}: {malformed} non-JSON/object lines")
    return result, "complete" if not malformed else "partial"


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _status(value):
    if value in ("pass", "passed", "verified"):
        return "pass"
    if value in ("fail", "failed", "rejected", "invalid", "mismatch"):
        return "fail"
    return "unverified"


def _model(launch, events, stream_status):
    def nodes(event):
        if not isinstance(event, dict):
            return
        yield event
        for key in ("data", "params", "update", "result", "metadata", "_meta", "kiro", "session", "models"):
            if isinstance(event.get(key), dict):
                yield from nodes(event[key])

    current, initial, generated, changed = None, None, False, False
    observed, active = set(), set()
    for event in events:
        selected = set().union(*(_reported_models(node) for node in nodes(event)))
        observed.update(selected)
        if len(selected) == 1:
            current = next(iter(selected))
        elif len(selected) > 1:
            current = None
        if generated and selected:
            active.update(selected)
            changed = changed or selected != {EXPECTED_MODEL}
        boundary = any(node.get("sessionUpdate") in {"agent_thought_chunk", "agent_message_chunk"} for node in nodes(event))
        if boundary and not generated:
            generated, initial = True, current
            if current is not None:
                active.add(current)
    recorded = set(launch.get("model_reported") or [])
    requested = launch.get("model_requested")
    # Some launchers retain initialization choices in model_reported. Raw
    # ordered stream evidence distinguishes those from models used to generate.
    contradictory_record = recorded - observed - {EXPECTED_MODEL}
    mismatch = bool(changed or active - {EXPECTED_MODEL} or contradictory_record or
                    requested not in (None, EXPECTED_MODEL))
    confirmed = (not mismatch and initial == EXPECTED_MODEL and requested == EXPECTED_MODEL
                 and active == {EXPECTED_MODEL} and recorded in ({EXPECTED_MODEL}, observed)
                 and stream_status == "complete" and launch.get("model_confirmation") == "stream_confirmed")
    return {"status": "mismatch" if mismatch else ("verified" if confirmed else "unverified"),
            "required": EXPECTED_MODEL, "requested": requested, "stream_reported": sorted(active),
            "initialization_and_stream_models": sorted(observed), "generation_boundary_seen": generated,
            "model_at_first_generation": initial,
            "launch_reported": sorted(recorded), "stream_status": stream_status,
            "confirmation": launch.get("model_confirmation", "unavailable")}


def _tool_audit(session, events, status):
    counts, denied, outside = Counter(), [], []
    target = (session / "target").resolve()
    for event in events:
        counts[str(event.get("tool", "unknown"))] += 1
        if event.get("allow") is not False:
            continue
        row = {key: event.get(key) for key in ("at", "tool", "reason", "paths")}
        denied.append(row)
        for raw in event.get("paths", []) or []:
            if not isinstance(raw, str):
                continue
            path = Path(raw)
            if not (path if path.is_absolute() else target / path).resolve().is_relative_to(target):
                outside.append(row)
                break
    return {"status": status, "event_count": len(events), "tools": dict(sorted(counts.items())),
            "denied_count": len(denied), "denied_outside_workspace_count": len(outside),
            "denied_outside_workspace_attempts": outside, "other_denied_count": len(denied)-len(outside),
            "note": "Denied requests are blocked attempts, not evidence that outside files were read."}


def _configuration(launch, events, stream_status):
    guard = StreamConfigurationGuard(f"kiroclops-{launch.get('mode', 'unknown')}")
    error = None
    for event in events:
        try:
            guard.observe(event)
        except KiroError as exc:
            error = {"code": exc.code, "message": str(exc)}
            break
    summary = guard.summary()
    return {"status": "fail" if error else ("pass" if summary["generation_configuration_verified"] and stream_status == "complete" else "unverified"),
            "error": error, **summary}


def _launch_completion(launch, events, configuration):
    if launch.get("transport") != "acp":
        passed = launch.get("status") == "completed" and launch.get("returncode") == 0
        return {"status": "pass" if passed else "unverified", "transport": launch.get("transport", "headless"),
                "reason": "headless completion uses its recorded process return code"}
    if launch.get("cleanup_errors") or launch.get("status") == "cleanup_failed":
        return {"status": "fail", "transport": "acp", "reason": "owned-process cleanup failed"}
    result = launch.get("prompt_result") or {}
    recorded = launch.get("stream_configuration") or {}
    response_seen = any("id" in event and "error" not in event and event.get("result") == result for event in events)
    checks = {"completed_turn": launch.get("status") == "completed" and bool(launch.get("finished_at")),
              "prompt_sent": launch.get("prompt_sent") is True,
              "end_turn_response": result.get("stopReason") == "end_turn" and response_seen,
              "recorded_configuration": recorded.get("generation_started") is True
                  and recorded.get("generation_configuration_verified") is True
                  and recorded.get("selected") == configuration.get("expected"),
              "replayed_configuration": configuration.get("status") == "pass",
              # ACP is a persistent server. A bounded owned-process shutdown
              # may terminate it after a successful end_turn response; an OS
              # exit code of zero is not the prompt's success criterion.
              "process_stopped": type(launch.get("process_returncode_after_cleanup")) is int}
    return {"status": "pass" if all(checks.values()) else "unverified", "transport": "acp", "checks": checks,
            "reason": "ACP prompt result, raw response, configuration and completed process cleanup are checked independently"}


def _source_integrity(session, state, candidate, issues):
    if not candidate or not state.get("baseline_hashes") or not candidate.get("source_hashes"):
        return {"status": "unverified", "reason": "baseline or selected source snapshot is missing"}
    cid = candidate["candidate_id"]
    baseline = _local(session, state.get("baseline", "baseline"))
    selected = _local(session, f"candidates/{cid}/project")
    snapshot = _read(session, f"candidates/{cid}/candidate.json", issues)
    try:
        if snapshot is None:
            return {"status": "unverified", "reason": "selected candidate metadata is missing"}
        if snapshot != candidate:
            return {"status": "fail", "reason": "selected candidate metadata disagrees with session snapshot"}
        if state.get("final", {}).get("candidate_id") != cid:
            return {"status": "fail", "reason": "final result belongs to a different selected candidate"}
        if (project_hashes(baseline) != state["baseline_hashes"] or
                project_hashes(selected) != candidate["source_hashes"]):
            return {"status": "fail", "reason": "current source bytes differ from recorded hashes"}
        count = 0
        for runs, expected, variant in ((state.get("baseline_runs", {}).get("validation", []), state["baseline_hashes"], "before"),
                                        (candidate.get("validation_runs", []), candidate["source_hashes"], "after")):
            if len(runs) != 12 or len(set(runs)) != 12:
                return {"status": "unverified", "reason": "12 distinct held-out capture manifests per target role are required"}
            slots = []
            for run in runs:
                manifest = _read(session, _local(session, run) / "manifest.json", issues)
                if not manifest:
                    return {"status": "unverified", "reason": "capture source attestation is missing"}
                if (manifest.get("game") != "slay" or manifest.get("variant") != variant
                        or manifest.get("status") != "ok" or manifest.get("fps") != 60 or manifest.get("frame_count") != 240):
                    return {"status": "fail", "reason": "held-out manifest identity or capture protocol is incorrect"}
                slots.append(_slot(manifest.get("scenario", {})))
                provenance = manifest.get("provenance", {})
                if any(provenance.get(key) != expected for key in ("full_source_hashes_before", "full_source_hashes_after")):
                    return {"status": "fail", "reason": "capture source hashes differ from the selected snapshot"}
                count += 1
            if len(slots) != 12 or set(slots) != VALIDATION_SLOTS:
                return {"status": "fail", "reason": "held-out source manifests do not cover the exact 12 distinct scenarios"}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "fail", "reason": f"source integrity check failed: {type(exc).__name__}"}
    return {"status": "pass", "checked_capture_manifests": count,
            "reason": "current baseline/candidate bytes and before/after capture hashes match"}


def _authorship(session, candidate, issues):
    record = _read(session, "kiro/authorship.json", issues, optional=True)
    if not record:
        return {"status": "unverified", "reason": "no independent authorship review is recorded"}
    if record.get("status") in ("failed", "fail"):
        return {"status": "fail", "reason": record.get("reason", "authorship review failed")}
    if not candidate or record.get("status") != "verified" or not record.get("reviewed_by") or not record.get("reviewed_at"):
        return {"status": "unverified", "reason": "authorship review is incomplete"}
    if record.get("candidate_id") != candidate.get("candidate_id") or record.get("source_hashes") != candidate.get("source_hashes"):
        return {"status": "fail", "reason": "authorship review belongs to different candidate bytes"}
    required = {"kiro/stdout.jsonl", "kiro/tool-audit.jsonl", f"candidates/{candidate['candidate_id']}/patch.diff"}
    verified = set()
    for evidence in record.get("evidence", []):
        path = _local(session, evidence["path"])
        if not path.is_file() or _sha(path) != evidence.get("sha256"):
            return {"status": "fail", "reason": "reviewed authorship evidence is missing or changed"}
        verified.add(path.relative_to(session).as_posix())
    if not required <= verified:
        return {"status": "unverified", "reason": "review must bind model trace, tool audit and selected source diff"}
    return {"status": "pass", "reviewed_by": record["reviewed_by"], "reviewed_at": record["reviewed_at"],
            "reason": "explicit review is bound to the selected source hashes and original execution evidence"}


def _ide_evidence(session, record, candidate, issues):
    """Recompute native ZIP evidence; a saved audit label is not proof."""
    if not record:
        return {"status": "unavailable", "reason": "No native IDE export audit was supplied."}
    if record.get("surface") != "ide" or record.get("session_id") != session.name:
        return {"status": "failed", "reason": "IDE audit belongs to a different surface/session."}
    try:
        exported = record.get("export", {})
        path = _local(session, exported["path"])
        if not path.is_file() or _sha(path) != exported.get("sha256"):
            return {"status": "failed", "reason": "Original native IDE ZIP is missing or changed."}
        fresh = audit_ide_export(session, path, expected_agent="kiroclops-tools",
                                 expected_model=EXPECTED_MODEL, expected_effort=EXPECTED_EFFORT,
                                 candidate_id=(candidate or {}).get("candidate_id"))
        return {**fresh, "recomputed_from_original_zip": True,
                "evidence_report_sha256": _sha(_local(session, "ide/evidence.json"))}
    except (OSError, ValueError, KeyError, TypeError, IDEExportError) as exc:
        issues.append(f"Native IDE audit could not be verified: {type(exc).__name__}")
        return {"status": "failed", "reason": str(exc)}


def _cohort_identity(state, launch):
    values = {"baseline_sources": state.get("baseline_hashes"),
              "frozen_measurement_core": state.get("phase1_frozen_files"),
              "source_commit": state.get("source_commit"), "budget": state.get("budget"),
              "cli_version": launch.get("cli_version"), "kas_version": launch.get("kas_version"),
              "transport": launch.get("transport")}
    missing = [key for key, value in values.items() if value is None or value == {} or value == ""]
    fingerprints = {key: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    for key, value in values.items() if key not in missing}
    return {"status": "unverified" if missing else "pass", "missing": missing, "fingerprints": fingerprints}


def _ide_ui_observation(session, observation):
    if not observation or not observation.get("evidence"):
        return {"status": "unverified", "reason": "No screenshot-bound operator UI observation."}
    matched = (observation.get("agent"), observation.get("model"), observation.get("effort")) == (
        "kiroclops-tools", EXPECTED_MODEL, EXPECTED_EFFORT)
    hashes = []
    for item in observation["evidence"]:
        path = _local(session, item["path"])
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or _sha(path) != item.get("sha256"):
            return {"status": "fail", "reason": "An operator-observed settings screenshot is missing or changed."}
        hashes.append(item)
    return {"status": "pass" if matched else "fail", "evidence": hashes,
            "reason": "Hash-bound operator observation; image contents are not automatically interpreted."}


def _ide_completion(session, state, launch, native, issues):
    """Verify GUI turn receipts without pretending they are an ACP process log."""
    from .ide_evidence import _time
    if launch.get("surface") != "ide" or launch.get("transport") != "ide_gui" or launch.get("status") != "completed":
        return {"status": "unverified", "reason": "No completed native IDE GUI receipt."}
    if not native.get("recomputed_from_original_zip"):
        return {"status": "fail" if native.get("status") == "failed" else "unverified",
                "reason": "Native ZIP completion evidence is unavailable or invalid."}
    start = _read(session, "kiro/launch-start.json", issues, optional=True)
    finish = _read(session, "ide/finish-receipt.json", issues, optional=True)
    if not start or not finish:
        return {"status": "unverified", "reason": "Original start and finish receipts are both required."}
    try:
        start_ref = launch.get("start_receipt", {})
        config_path = _local(session, launch["configuration_path"])
        prompt_path = _local(session, launch["prompt_path"])
        config = _read(session, config_path, issues) or {}
        generated = config.get("generated_hashes", {})
        generated_ok = bool(generated) and all(_sha(_local(session, session / "target/.kiro" / path)) == digest
                                               for path, digest in generated.items())
        timing = native.get("timing", {})
        started, ended = _time(launch["started_at"]), _time(timing.get("last_turn_end_at"))
        # Only the fresh ZIP auditor may exclude its narrowly verified native
        # preload/postlude records. Preserve old-schema behavior when neither
        # experiment field exists; a present but invalid field must fail.
        experiment_timing = any(key in timing for key in
                                ("first_experiment_message_at", "last_experiment_message_at"))
        first_key, last_key = (("first_experiment_message_at", "last_experiment_message_at")
                               if experiment_timing else ("first_message_at", "last_message_at"))
        first, last = _time(timing.get(first_key)), _time(timing.get(last_key))
        elapsed = (ended - started).total_seconds()
        pre_ui = _ide_ui_observation(session, launch.get("ui_observed"))
        post_ui = _ide_ui_observation(session, launch.get("ui_observed_after"))
        checks = {
            "receipt_identity": finish == launch and start.get("status") == "running"
                and all(launch.get(key) == value for key, value in start.items() if key != "status")
                and launch.get("session_id") == state.get("session_id") == session.name,
            "original_start_hash": start_ref.get("path") == "kiro/launch-start.json"
                and _sha(session / "kiro/launch-start.json") == start_ref.get("sha256"),
            "unchanged_clock": launch.get("started_at") == state.get("started_at")
                and launch.get("deadline_at") == state.get("deadline_at") and not state.get("active_job_id"),
            "export_binding": launch.get("export") == native.get("export"),
            "prepared_profile": config_path == session / "target/.kiro/kiroclops-session.json"
                and _sha(config_path) == launch.get("configuration_sha256") and generated_ok
                and config.get("surface") == "ide" and config.get("integration") == "ide_power"
                and config.get("agent_name") == "kiroclops-tools" and config.get("mode") == "tools"
                and config.get("model") == EXPECTED_MODEL and config.get("effort") == EXPECTED_EFFORT
                and _local(session, config.get("target_dir", "")) == session / "target",
            "prepared_prompt": prompt_path == session / "target/.kiro/kiroclops-prompt.md"
                and _sha(prompt_path) == launch.get("prompt_sha256"),
            "startup_ui_observation": pre_ui["status"] == "pass",
            "native_end_turn": launch.get("prompt_result", {}).get("stopReason") == "end_turn"
                and timing.get("last_turn_stop_reason") == "end_turn"
                and bool(timing.get("turn_start_count")) and bool(timing.get("turn_end_count"))
                and not timing.get("later_turn_start_present") and launch.get("finished_at") == timing.get("last_turn_end_at"),
            "native_timing": started <= first <= last <= ended <= datetime.now(timezone.utc)
                and (not state.get("finished_at") or _time(state["finished_at"]) <= ended)
                and _finite(launch.get("elapsed_seconds")) and abs(launch["elapsed_seconds"] - elapsed) <= 0.000001,
            "native_identity": native.get("configuration", {}).get("status") == "pass"
                and native.get("tools", {}).get("status") == "complete",
        }
        return {"status": "pass" if all(checks.values()) else "fail", "transport": "ide_gui", "checks": checks,
                "ui_before": pre_ui, "ui_after": post_ui, "elapsed_seconds": elapsed,
                "message_timing_basis": "audited_experiment" if experiment_timing else "legacy_raw_messages",
                "reason": "Operator start and native final end_turn are hash-bound; per-generation effort remains unverified."}
    except (OSError, ValueError, KeyError, TypeError, IDEExportError) as exc:
        return {"status": "fail", "transport": "ide_gui", "reason": f"IDE completion evidence is inconsistent: {type(exc).__name__}"}


def _ide_job_bindings(session, state, native, issues):
    """Bind accepted native requests to succeeded jobs for the selected source."""
    from .ide_evidence import _time
    cid = state.get("selected_candidate_id")
    if not cid or not native.get("recomputed_from_original_zip"):
        return {"status": "unverified", "reason": "No selected candidate and freshly audited native requests."}
    evidence = {}
    try:
        start, end = _time(state.get("started_at")), _time(native.get("timing", {}).get("last_turn_end_at"))
        for action, tool_name, result_status in (("evaluate", "evaluate_candidate", "evaluated"),
                                                  ("finalize", "finalize_candidate", "finished")):
            matched = [call for call in native.get("tools", {}).get("calls", [])
                       if call.get("success") is True and call.get("power", {}).get("action") == "use"
                       and call.get("power", {}).get("powerName") == call.get("power", {}).get("serverName") == "kiroclops"
                       and call.get("power", {}).get("toolName") == tool_name
                       and call.get("structured_result", {}).get("status") == "accepted"
                       and call["structured_result"].get("candidate_id") == cid]
            if len(matched) != 1:
                return {"status": "unverified", "reason": f"Exactly one accepted native {tool_name} request must bind the selected candidate."}
            call = matched[0]
            jid = call["structured_result"].get("job_id")
            if not isinstance(jid, str) or not re.fullmatch(r"job-[A-Za-z0-9_-]{1,96}", jid):
                raise ValueError("Accepted native job ID is invalid")
            path = _local(session, f"jobs/{jid}.json")
            job = _read(session, path, issues, optional=True)
            if not job:
                return {"status": "unverified", "reason": f"Accepted {action} job record is missing."}
            result = job.get("result") or {}
            if (not isinstance(result, dict) or job.get("schema_version") != 2 or job.get("job_id") != jid or job.get("session_id") != session.name
                    or job.get("action") != action or job.get("candidate_id") != cid or job.get("status") != "succeeded"
                    or job.get("error") or result.get("error") or result.get("status") != result_status
                    or result.get("candidate_id") != cid):
                raise ValueError(f"Native {action} job did not succeed for this session and selected candidate")
            created, finished = _time(job.get("created_at")), _time(job.get("finished_at"))
            if not start <= created <= finished <= end:
                raise ValueError("Native job completion is outside the recorded IDE turn")
            if action == "evaluate":
                if call.get("tool_call_id") != native.get("authorship", {}).get("candidate_boundary", {}).get("tool_call_id"):
                    raise ValueError("Native evaluation job is not the audited source snapshot boundary")
            else:
                final = state.get("final") or {}
                if (_local(session, result.get("comparison_path", "")) != _local(session, final.get("comparison_path", "final/comparison.json"))
                        or result.get("numerical_status") != final.get("numerical_status")
                        or _time(evidence["evaluate"]["finished_at"]) > created):
                    raise ValueError("Native finalization is not bound to the selected comparison after its evaluation")
            evidence[action] = {"job_id": jid, "candidate_id": cid, "tool_call_id": call.get("tool_call_id"),
                                "path": path.relative_to(session).as_posix(), "sha256": _sha(path),
                                "created_at": job["created_at"], "finished_at": job["finished_at"]}
        return {"status": "pass", "jobs": evidence,
                "reason": "Native queued responses bind the selected snapshot evaluation and succeeded final comparison jobs."}
    except (OSError, ValueError, KeyError, TypeError, IDEExportError) as exc:
        return {"status": "fail", "reason": f"Native job binding failed: {exc}"}


def _ide_demo(native, completion, gates, budget_ok, job_bindings=None):
    calls = native.get("tools", {}).get("calls", [])
    successful = [item.get("power", {}) for item in calls if item.get("success") is True
                  and item.get("power", {}).get("powerName") == "kiroclops"
                  and item.get("structured_result", {}).get("status") not in {"error", "ambiguous"}]
    job_bindings = job_bindings or {"status": "unverified", "reason": "Native job bindings have not been checked."}
    checks = {"native_zip_reaudited": native.get("recomputed_from_original_zip") is True,
              "bounded_turn_completed": completion.get("status") == "pass", "within_budget": budget_ok,
              "ui_before_and_after": completion.get("ui_before", {}).get("status") == "pass"
                  and completion.get("ui_after", {}).get("status") == "pass",
              "observed_reasoning_model": native.get("model", {}).get("reasoning_models") == [EXPECTED_MODEL],
              "power_activated": any(item.get("action") == "activate" for item in successful),
              "power_skill_read": any(item.get("action") == "readSkill" and item.get("skillName") == "motion-calibration" for item in successful),
              "native_evaluation": job_bindings.get("status") == "pass" and "evaluate" in job_bindings.get("jobs", {}),
              "native_finalization": job_bindings.get("status") == "pass" and "finalize" in job_bindings.get("jobs", {}),
              **{f"quality_{key}": value == "pass" for key, value in gates.items()}}
    demonstrated = all(checks.values())
    failed = (any(value == "fail" for value in gates.values()) or native.get("status") == "failed"
              or native.get("model", {}).get("status") == "mismatch" or completion.get("status") == "fail"
              or completion.get("ui_after", {}).get("status") == "fail" or job_bindings.get("status") == "fail")
    return {"status": "demonstrated" if demonstrated else "failed" if failed else "unverified", "checks": checks,
            "native_jobs": job_bindings,
            "phase_acceptance": "demonstrated_with_configuration_limit" if demonstrated else "pending_or_failed",
            "strict_model_configuration": "unverified",
            "reason": "A real IDE Power demonstration and strict per-generation model/effort proof are separate claims.",
            "limitation": "Observed UI settings and native reasoning model IDs do not establish effort for every generation."}


def _no_eligible_finish(session, candidates):
    """Reuse the strict native-tool replay, including exact shell/hook binding."""
    from .authorship import audit_kiro_authorship
    replay = audit_kiro_authorship(session, candidate_id=candidates[-1]["candidate_id"])
    if replay.get("status") != "verified":
        return {"status": "unverified", "reason": "Fresh native replay did not verify the last evaluated attempt.",
                "replay_reason": replay.get("reason")}
    normalization = replay.get("normalization", {})
    commands = normalization.get("controlled_commands", [])
    captures = [item for item in commands if item.get("action") == "capture"]
    boundary = replay.get("candidate_boundary", {})
    rejected = [item for item in normalization.get("controlled_rejections", [])
                if item.get("code") == "no_eligible_candidate" and item.get("action") == "finish"
                and item.get("exit_code") == 1 and type(item.get("stdout_line")) is int
                and type(boundary.get("result_line")) is int and item["stdout_line"] > boundary["result_line"]]
    proven = (len(captures) == len(candidates) and bool(commands) and bool(rejected)
              and captures[-1].get("tool_call_id") == boundary.get("tool_call_id")
              and commands[-1].get("action") == "finish"
              and commands[-1].get("tool_call_id") == rejected[-1].get("tool_call_id"))
    return {"status": "pass" if proven else "unverified", "rejections_after_last_evaluation": rejected,
            "checked_capture_commands": len(captures), "candidate_id": replay.get("candidate_id"),
            "reason": "Exact controlled finish rejected after the last fully replayed evaluation." if proven else
                      "No bound finish rejection follows the last evaluated candidate."}


def _experiment_outcome(candidates, selected, numerical, *, completion=None, finish_rejection=None):
    if selected:
        return {"status": "held_out_evaluated", "numerical_status": numerical}
    # Three is a maximum, not a required number of attempts. A model can finish
    # early after explicit evaluated failures. Interrupted/partial work is not
    # silently promoted to an observed unsuccessful experiment.
    evaluated = 1 <= len(candidates) <= 3 and all(candidate.get("status") == "evaluated"
            and candidate.get("feedback", {}).get("status") == "ok"
            and not candidate.get("feedback", {}).get("errors")
            and candidate.get("feedback", {}).get("eligible") is False
            and candidate.get("smoke", {}).get("status") in {"pass", "passed", "fail", "failed"}
            for candidate in candidates)
    if evaluated and (completion or {}).get("status") == "pass" and (finish_rejection or {}).get("status") == "pass":
        return {"status": "ended_no_eligible_candidate", "success": False,
                "completed_ineligible_attempts": len(candidates), "finish_rejection": finish_rejection,
                "reason": "The model turn ended after evaluated ineligible candidates and an explicit finish rejection; held-out error is unavailable."}
    return {"status": "unverified", "finish_rejection": finish_rejection,
            "reason": "No finalized candidate or completed model turn with verified ineligible evaluations and finish rejection."}


def _attempt_integrity(session, state, candidates, issues):
    """Attest completed ineligible calibration attempts without inventing holdout."""
    try:
        identifiers = [candidate.get("candidate_id") for candidate in candidates]
        if len(set(identifiers)) != len(identifiers):
            return {"status": "fail", "reason": "Evaluated candidate IDs are duplicated."}
        baseline_hashes = state.get("baseline_hashes")
        if not baseline_hashes or project_hashes(_local(session, state.get("baseline", "baseline"))) != baseline_hashes:
            return {"status": "fail", "reason": "Baseline bytes changed or are missing."}
        groups = [(state.get("baseline_runs", {}).get("calibration", []), baseline_hashes, "before")]
        for candidate in candidates:
            cid = candidate["candidate_id"]
            snapshot = _read(session, f"candidates/{cid}/candidate.json", issues)
            if snapshot != candidate or project_hashes(_local(session, f"candidates/{cid}/project")) != candidate.get("source_hashes"):
                return {"status": "fail", "reason": "An evaluated attempt's snapshot or metadata changed."}
            groups.append((candidate.get("calibration_runs", []), candidate["source_hashes"], "after"))
        slots_required = {(hold, 0, repeat, 30, "calibration") for hold in (3, 24) for repeat in (1, 2, 3)}
        for runs, expected, variant in groups:
            if len(runs) != 6 or len(set(runs)) != 6:
                return {"status": "unverified", "reason": "Each evaluated attempt and baseline need six complete calibration captures."}
            slots = []
            for run in runs:
                manifest = _read(session, _local(session, run) / "manifest.json", issues) or {}
                if (manifest.get("game") != "slay" or manifest.get("variant") != variant or manifest.get("status") != "ok"
                        or manifest.get("fps") != 60 or manifest.get("frame_count") != 240
                        or any(manifest.get("provenance", {}).get(key) != expected
                               for key in ("full_source_hashes_before", "full_source_hashes_after"))):
                    return {"status": "fail", "reason": "Evaluated attempt capture identity/source hashes are invalid."}
                slots.append(_slot(manifest.get("scenario", {})))
            if set(slots) != slots_required:
                return {"status": "fail", "reason": "Evaluated attempts do not cover six distinct calibration scenarios."}
        return {"status": "pass", "checked_candidates": len(candidates), "checked_capture_manifests": 6 * len(groups)}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        return {"status": "fail", "reason": f"Attempt integrity check failed: {type(exc).__name__}"}


def _manual_evidence(session, state, candidate, comparison, issues):
    """Check the independent-review receipt against current raw/report bytes."""
    if not candidate or not state.get("final", {}).get("manual_review_path"):
        return {"status": "unverified", "reason": "No selected-candidate independent-review receipt."}
    from . import evaluation, review
    try:
        receipt = _read(session, state["final"]["manual_review_path"], issues) or {}
        if (receipt.get("candidate_id") != candidate.get("candidate_id")
                or not all(receipt.get(key) is True for key in ("measurements_unchanged", "raw_evidence_unchanged", "sources_unchanged"))):
            return {"status": "fail", "reason": "Independent review is not bound to this frozen candidate."}
        groups = review._runs(state, candidate, session, session / "candidates" / candidate["candidate_id"])
        runs = [run for group in groups.values() for run in group]
        run_ids = {str(run) for run in runs}
        if any(set(receipt.get(key, {})) != run_ids for key in ("measurement_sha256", "raw_evidence_sha256")):
            return {"status": "fail", "reason": "Independent review does not bind every current calibration/validation run."}
        reviewed = {_local(session, item["analysis_path"]).parent: item for item in receipt.get("run_reviews", [])}
        if set(reviewed) != set(runs):
            return {"status": "fail", "reason": "Independent per-run review evidence is incomplete."}
        for run in runs:
            manifest = _read(session, run / "manifest.json", issues) or {}
            if manifest.get("frame_count") != 240 or manifest.get("fps") != 60:
                return {"status": "fail", "reason": "Reviewed capture differs from the fixed 240-frame / 60Hz protocol."}
            if (review._measurements(run) != receipt["measurement_sha256"][str(run)]
                    or review._raw_fingerprint(run) != receipt["raw_evidence_sha256"][str(run)]):
                return {"status": "fail", "reason": "Raw pixels, manifests or measurements changed after independent review."}
            report = _read(session, run / "analysis.json", issues) or {}
            saved = reviewed[run]
            annotation = _local(session, saved["annotation_path"])
            if not annotation.is_file() or report.get("provenance", {}).get("annotation_sha256") != _sha(annotation):
                return {"status": "fail", "reason": "Manual annotation changed after image analysis."}
            fields = ("status", "game", "variant", "scenario", "valid_ratio", "events", "metrics", "errors")
            manual = {key: report.get("annotation_validation", {}).get(key) for key in
                      ("status", "point_status", "event_status", "control_point_count", "median_error_px", "tolerance_px")}
            if any(report.get(key) != saved.get(key) for key in fields) or manual != saved.get("manual_validation"):
                return {"status": "fail", "reason": "Per-run independent validation changed after its review receipt."}
        current = evaluation.final_comparison(groups["reference"], groups["baseline"], groups["candidate"])
        fields = ("gates", "validation_scenarios", "coverage", "alignment", "numerical_status",
                  "manual_validation_status", "measurement_status", "manual_validation", "annotation_validation")
        if any(current.get(key) != comparison.get(key) for key in fields):
            return {"status": "fail", "reason": "Saved final comparison differs from the selected reviewed analyses."}
        return {"status": "pass", "checked_runs": len(runs), "reviewed_at": receipt.get("reviewed_at"),
                "receipt_sha256": _sha(_local(session, state["final"]["manual_review_path"]))}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {"status": "fail", "reason": f"Independent review integrity check failed: {type(exc).__name__}"}


def _wall_evidence(session):
    if not _local(session, "final/gameplay-review.json").is_file():
        return {"status": "unverified", "reason": "Supplemental wall-launch evidence is missing; original wall smoke did not prove an airborne wall launch."}
    try:
        from .gameplay_review import verify_wall_review
        result = verify_wall_review(session)
        return {**result, "status": _status(result.get("status"))}
    except ImportError:
        return {"status": "unverified", "reason": "Supplemental wall-review verifier is unavailable."}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {"status": "fail", "reason": f"Supplemental wall proof failed verification: {type(exc).__name__}"}


def _collect(session):
    issues = []
    state = _read(session, "session.json", issues) or {}
    launch = _read(session, "kiro/launch.json", issues, optional=True) or {}
    ide_record = _read(session, "ide/evidence.json", issues, optional=True)
    stdout, stream_status = _jsonl(session, "kiro/stdout.jsonl", issues)
    audit, audit_status = _jsonl(session, "kiro/tool-audit.jsonl", issues)
    mode, surface = state.get("mode", "unknown"), launch.get("surface", state.get("surface", "unknown"))
    if mode not in ("tools", "baseline"):
        mode = "unknown"
    if surface not in ("cli", "ide"):
        surface = "unknown"
    if surface == "unknown" and ide_record and ide_record.get("surface") == "ide":
        surface = "ide"
    candidates = state.get("candidates") or []
    selected = next((candidate for candidate in candidates if candidate.get("candidate_id") == state.get("selected_candidate_id")), None)
    final = state.get("final") or {}
    comparison = (_read(session, final.get("comparison_path", "final/comparison.json"), issues, optional=True) or {}) if selected else {}
    rows = comparison.get("validation_scenarios", [])
    errors = [row.get("after_rmse_body") for row in rows]
    coverage = comparison.get("coverage", {})
    complete = (len(rows) == 12 and coverage.get("complete_triplets") == coverage.get("required_triplets") == 12
                and {_slot(row.get("scenario", {})) for row in rows} == VALIDATION_SLOTS)
    final_error = statistics.mean(errors) if complete and all(_finite(error) for error in errors) else None
    numerical = _status(comparison.get("numerical_status")) if complete else "unverified"
    names = {"curve_rmse", "improvement", "height", "rise", "fall"}
    if numerical == "pass" and not all(_finite(row.get("after_rmse_body")) and row.get("pass") is True and set(row.get("gates", {})) >= names
            and all(row["gates"][name] is True for name in names) for row in rows):
        numerical = "fail"
    if comparison.get("numerical_status") == "fail":
        numerical = "fail"
    manual = _status(comparison.get("manual_validation_status", comparison.get("measurement_status")))
    manual_evidence = _manual_evidence(session, state, selected, comparison, issues)
    if manual_evidence["status"] == "fail" or manual == "pass" and manual_evidence["status"] != "pass":
        manual = manual_evidence["status"]
    smoke = _status((selected or {}).get("smoke", {}).get("status"))
    wall = _wall_evidence(session)
    source = _source_integrity(session, state, selected, issues)
    authorship = _authorship(session, selected, issues)
    model = _model(launch, stdout, stream_status)
    configuration = _configuration(launch, stdout, stream_status)
    native = _ide_evidence(session, ide_record, selected, issues) if surface == "ide" else {"status": "not_applicable"}
    if surface == "ide" and native.get("recomputed_from_original_zip"):
        evidence_model = native.get("model", {})
        model = {**evidence_model, "native_model_status": evidence_model.get("status"),
                 "status": "mismatch" if evidence_model.get("status") == "mismatch" else "unverified",
                 "note": "Native export is shown separately; historical request-wide model/effort remains unverified."}
        authorship = {**native.get("authorship", {}), "status": _status(native.get("authorship", {}).get("status"))}
        configuration = {"status": "unverified", "reason": "Native IDE metadata is not historical ACP configuration proof.",
                         "current_metadata": native.get("configuration")}
    elapsed = launch.get("elapsed_seconds")
    elapsed = elapsed if _finite(elapsed) else None
    attempts = len(candidates)
    gates = {"numerical": numerical, "manual": manual, "smoke": smoke, "wall_behavior": wall["status"],
             "source_hashes": source["status"], "authorship": authorship["status"]}
    completion = _launch_completion(launch, stdout, configuration)
    if surface == "ide":
        completion = _ide_completion(session, state, launch, native, issues)
    launch_done = completion["status"] == "pass"
    model_ok = model["status"] == "verified" and launch.get("effort_requested") == EXPECTED_EFFORT and launch.get("mode") == mode
    budget_ok = attempts <= 3 and elapsed is not None and elapsed <= 1800
    demo = _ide_demo(native, completion, gates, budget_ok, _ide_job_bindings(session, state, native, issues)) if surface == "ide" else {"status": "not_applicable"}
    verified = all(value == "pass" for value in gates.values()) and launch_done and model_ok and budget_ok and configuration["status"] == "pass"
    finish_rejection = None
    if not selected and 1 <= attempts <= 3 and mode == "baseline" and launch_done:
        finish_rejection = _no_eligible_finish(session, candidates)
    experiment = _experiment_outcome(candidates, selected, numerical, completion=completion, finish_rejection=finish_rejection)
    runtime_accounted = launch_done and model_ok and budget_ok and configuration["status"] == "pass"
    if experiment["status"] == "ended_no_eligible_candidate":
        attempts_source = _attempt_integrity(session, state, candidates, issues)
        attempts_authorship = _authorship(session, candidates[-1], issues)
        experiment.update(source_integrity=attempts_source, last_candidate_authorship=attempts_authorship,
                          accountability_status="pass" if runtime_accounted and attempts_source["status"] == attempts_authorship["status"] == "pass" else "unverified")
    else:
        experiment["accountability_status"] = ("pass" if runtime_accounted and numerical in {"pass", "fail"}
                and all(gates[name] == "pass" for name in ("manual", "smoke", "wall_behavior", "source_hashes", "authorship")) else "unverified")
    failed = (any(value == "fail" for value in gates.values()) or model["status"] == "mismatch" or attempts > 3
              or configuration["status"] == "fail" or completion["status"] == "fail" or native.get("status") == "failed"
              or experiment["status"] == "ended_no_eligible_candidate")
    outcome = "pass" if verified else ("fail" if failed else ("not_run" if not launch and not ide_record else "unverified"))
    return {"session_id": session.name, "recorded_session_id": state.get("session_id"), "surface": surface, "mode": mode,
            "session_status": state.get("status", "missing"), "launch_status": launch.get("status", "not_run"),
            "status": outcome, "success": verified, "gates": gates, "model": model,
            "demo_status": demo["status"], "ide_demonstration": demo,
            "strict_model_configuration": "pass" if model_ok and configuration["status"] == "pass" else
                "fail" if model["status"] == "mismatch" or configuration["status"] == "fail" else "unverified",
            "stream_configuration": configuration,
            "launch_completion": completion,
            "effort_requested": launch.get("effort_requested"), "elapsed_seconds": elapsed, "candidate_count": attempts,
            "budget": {"within_planned_budget": budget_ok, "max_candidates": 3, "max_seconds": 1800,
                       "unused_candidate_slots": max(0, 3 - attempts),
                       "unused_seconds": max(0, 1800 - elapsed) if elapsed is not None else None,
                       "note": "Limits are maxima; a completed model turn can leave unused attempts and time."},
            "selected_candidate_id": state.get("selected_candidate_id"),
            "final_error": {"status": "available" if final_error is not None else "unavailable",
                            "mean_rmse_body": final_error, "source": "held-out 12-scenario comparison only"},
            "source_integrity": source, "authorship": authorship, "manual_evidence": manual_evidence, "wall_evidence": wall,
            "experiment_outcome": experiment, "ide_evidence": native, "cohort_identity": _cohort_identity(state, launch),
            "tool_audit": _tool_audit(session, audit, audit_status), "issues": issues}


def _aggregate(rows):
    return {"selected_runs": len(rows), "planned_runs": EXPECTED_RUNS_PER_ARM,
            "successes": sum(row["success"] for row in rows),
            "accounted_outcomes": sum(row["experiment_outcome"]["accountability_status"] == "pass" for row in rows),
            "ended_without_eligible_candidate": sum(row["experiment_outcome"]["status"] == "ended_no_eligible_candidate" for row in rows),
            "statuses": dict(Counter(row["status"] for row in rows)),
            "model_verified_runs": sum(row["model"]["status"] == "verified" for row in rows),
            "available_final_error_runs": sum(row["final_error"]["status"] == "available" for row in rows),
            "mean_final_rmse_body": statistics.mean([row["final_error"]["mean_rmse_body"] for row in rows])
                if rows and all(row["final_error"]["status"] == "available" for row in rows) else None,
            "mean_elapsed_seconds": statistics.mean([row["elapsed_seconds"] for row in rows])
                if rows and all(row["elapsed_seconds"] is not None for row in rows) else None,
            "mean_candidate_count": statistics.mean(row["candidate_count"] for row in rows) if rows else None,
            "denied_outside_workspace_attempts": sum(row["tool_audit"]["denied_outside_workspace_count"] for row in rows)}


def build_benchmark(session_ids):
    """Read an explicit session list; never discover or silently add sessions."""
    if not session_ids:
        raise ValueError("at least one explicit session is required")
    sessions = [_session_dir(value) for value in session_ids]
    if len(set(sessions)) != len(sessions):
        raise ValueError("duplicate sessions would inflate the benchmark")
    for session in sessions:
        state = _read(session, 'session.json', [], optional=True) or {}
        if state.get('experiment_id', 'legacy-jump-v1') != 'legacy-jump-v1':
            raise ValueError('Different experiment definitions cannot enter the legacy jump benchmark; use the ground-motion report')
    rows = [_collect(session) for session in sessions]
    groups = {mode: [row for row in rows if row["surface"] == "cli" and row["mode"] == mode] for mode in ("tools", "baseline")}
    arms = {mode: _aggregate(group) for mode, group in groups.items()}
    matrix_complete = all(len(group) == EXPECTED_RUNS_PER_ARM for group in groups.values())
    cli_rows = [row for group in groups.values() for row in group]
    cohort_complete = bool(cli_rows) and all(row["cohort_identity"]["status"] == "pass" for row in cli_rows)
    cohort_mismatches = [key for key in (cli_rows[0]["cohort_identity"]["fingerprints"] if cli_rows else {})
                         if len({row["cohort_identity"]["fingerprints"].get(key) for row in cli_rows}) > 1]
    same_cohort = cohort_complete and not cohort_mismatches
    comparable = matrix_complete and same_cohort and all(row["model"]["status"] == "verified" and row["effort_requested"] == EXPECTED_EFFORT
        and row["stream_configuration"]["status"] == "pass"
        and row["launch_completion"]["status"] == "pass"
        and row["source_integrity"]["status"] == row["authorship"]["status"] == "pass"
        and row["gates"]["manual"] == row["gates"]["smoke"] == row["gates"]["wall_behavior"] == "pass"
        and row["budget"]["within_planned_budget"] and row["final_error"]["status"] == "available"
        for group in groups.values() for row in group)
    delta = None
    if comparable:
        delta = {"mean_final_rmse_reduction_body": arms["baseline"]["mean_final_rmse_body"]-arms["tools"]["mean_final_rmse_body"],
                 "success_count_difference": arms["tools"]["successes"]-arms["baseline"]["successes"]}
    outcome_comparable = matrix_complete and same_cohort and all(row["experiment_outcome"]["accountability_status"] == "pass" for row in cli_rows)
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "selected_sessions": [session.name for session in sessions], "sessions": rows,
            "status": ("complete_descriptive_comparison" if comparable else
                       "complete_outcome_comparison_error_unavailable" if outcome_comparable else "incomplete_or_unverified"),
            "cli": {"matrix_complete": matrix_complete, "comparable": comparable, "arms": arms, "observed_difference": delta,
                    "outcome_comparable": outcome_comparable,
                    "outcome_difference": {"success_count_difference": arms["tools"]["successes"]-arms["baseline"]["successes"]} if outcome_comparable else None,
                    "cohort_identity": {"status": "pass" if same_cohort else ("fail" if cohort_mismatches else "unverified"),
                                        "mismatched_components": cohort_mismatches,
                                        "note": "Baseline source bytes, frozen measurement files, source revision, budget and CLI runtime must match."}},
            "ide": {"session_ids": [row["session_id"] for row in rows if row["surface"] == "ide"],
                    "successes": sum(row["success"] for row in rows if row["surface"] == "ide"),
                    "demonstrated_runs": sum(row["demo_status"] == "demonstrated" for row in rows if row["surface"] == "ide"),
                    "demo_statuses": dict(Counter(row["demo_status"] for row in rows if row["surface"] == "ide")),
                    "phase_acceptance": "demonstrated_with_configuration_limit" if any(
                        row["demo_status"] == "demonstrated" for row in rows if row["surface"] == "ide") else "not_yet_demonstrated",
                    "note": "IDE demonstration status is separate from strict per-generation model/effort proof and never counts toward the CLI comparison."},
            "unknown_surface_sessions": [row["session_id"] for row in rows if row["surface"] == "unknown"],
            "limitations": ["Three selected runs per CLI arm are descriptive evidence, not a statistical reliability estimate.",
                            "Missing baseline final error is unavailable; calibration feedback is never substituted.",
                            "Allowed tool requests do not prove authorship; explicit hash-bound review is required.",
                            "Blocked outside requests are listed separately and do not establish successful access."]}


def _regular_report_tree(folder, boundary):
    """Inventory without following symlinks, junctions or other special files."""
    folder, boundary = Path(folder).absolute(), Path(boundary).resolve()
    files = []

    def visit(path):
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not path.resolve().is_relative_to(boundary)):
            raise ValueError("report tree contains a linked or external path")
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                children = sorted((Path(entry.path) for entry in entries), key=lambda value: value.name)
            for child in children:
                visit(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            files.append(path)
        else:
            raise ValueError("report tree must contain only independent regular files and directories")

    visit(folder)
    return files


class _ReportLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "base":
            raise ValueError("report base URLs would change local asset resolution")
        for name, value in attrs:
            if name in {"href", "src", "poster"} and value:
                self.links.append(value)
            if name == "srcset" and value:
                raise ValueError("unverified responsive asset URLs are not supported in report packages")


def _validate_report_links(folder, files):
    """Generated reports use relative HTML links and inline JS/data only."""
    regular = set(files)
    for page in (path for path in files if path.suffix.lower() in {".html", ".htm"}):
        if page.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError("report HTML exceeds the local metadata limit")
        parser = _ReportLinks()
        parser.feed(page.read_text(encoding="utf-8-sig"))
        for raw in parser.links:
            link = urlsplit(raw)
            value = unquote(link.path)
            if link.scheme or link.netloc or value.startswith(("/", "\\")) or "\\" in value:
                raise ValueError("report link is not a relative package asset")
            if not value:  # Same-page fragments need no copied file.
                continue
            target = (page.parent / value).resolve()
            if not target.is_relative_to(folder) or target not in regular:
                raise ValueError("report link is missing or points outside its report folder")


def _package_reports(session_ids, directory):
    """Copy existing portable reports; source sessions are never generated/edited."""
    directory = Path(directory).resolve()
    expected = ROOT.resolve() / "artifacts/phase2-report"
    if directory != expected:
        raise ValueError("report bundle must remain inside artifacts/phase2-report")
    destination = directory / "reports"
    if destination.exists() or destination.is_symlink():
        _regular_report_tree(destination, directory)  # Verify before recursive replacement.
        if not destination.is_dir():
            raise ValueError("existing reports output must be a regular directory")
    packaged = {}
    with tempfile.TemporaryDirectory(prefix=".report-bundle-", dir=directory) as temporary:
        staging = Path(temporary) / "reports"
        staging.mkdir()
        for value in session_ids:
            session = _session_dir(value)
            source = session / "report"
            if not source.exists() and not source.is_symlink():
                packaged[session.name] = {"status": "unavailable", "reason": "No existing session report."}
                continue
            try:
                files = _regular_report_tree(source, session)
                if source / "index.html" not in files:
                    raise ValueError("existing report has no regular index.html")
                _validate_report_links(source, files)
            except (OSError, UnicodeError, ValueError) as exc:
                packaged[session.name] = {"status": "unavailable", "reason": f"Existing report cannot be bundled: {exc}"}
                continue
            target = staging / session.name
            for original in files:
                copied = target / original.relative_to(source)
                copied.parent.mkdir(parents=True, exist_ok=True)
                # The manifest is validated before any copy; check again before
                # opening each file so a newly substituted link is not followed.
                if original.is_symlink() or original.resolve() != original.absolute():
                    raise ValueError("report source path changed during packaging")
                info = original.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise ValueError("report source file changed during packaging")
                shutil.copy2(original, copied, follow_symlinks=False)
                after = original.lstat()
                if any(getattr(after, field) != getattr(info, field) for field in
                       ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")):
                    raise ValueError("report source changed during packaging; bundle not published")
            packaged[session.name] = {"status": "available", "href": f"reports/{session.name}/index.html",
                                     "file_count": len(files)}
        # Both the staging tree and existing destination are bounded, ordinary
        # generated files. Replace the whole reports tree to remove stale or
        # unselected session copies from previous deliveries.
        _regular_report_tree(staging, directory)
        if destination.exists():
            _regular_report_tree(destination, directory)
            shutil.rmtree(destination)
        staging.replace(destination)
    return packaged


def generate_benchmark_report(session_ids):
    report = build_benchmark(session_ids)
    directory = (ROOT / "artifacts/phase2-report").resolve()
    if directory != ROOT.resolve() / "artifacts/phase2-report":
        raise ValueError("benchmark output must remain at ROOT/artifacts/phase2-report")
    directory.mkdir(parents=True, exist_ok=True)
    json_path, html_path = directory / "benchmark.json", directory / "index.html"
    for output in (json_path, html_path):
        if output.exists() or output.is_symlink():
            if _regular_report_tree(output, directory) != [output]:
                raise ValueError("benchmark output must be an independent regular file")
    packaged = _package_reports(report["selected_sessions"], directory)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    json_path.write_text(serialized, encoding="utf-8")
    escape = lambda value: html.escape(str(value), quote=True)
    display = lambda value: "unavailable" if value is None else (f"{value:.4f}" if isinstance(value, float) else str(value))
    table = []
    for row in report["sessions"]:
        cells = [row["session_id"], row["surface"], row["mode"], row["status"], row["experiment_outcome"]["status"],
                 row["demo_status"], row["strict_model_configuration"],
                 ", ".join(f"{key}: {value}" for key, value in row["gates"].items()),
                 display(row["final_error"]["mean_rmse_body"]), row["candidate_count"], display(row["elapsed_seconds"]),
                 f'{row["budget"]["unused_candidate_slots"]} attempts / {display(row["budget"]["unused_seconds"])} seconds',
                 row["tool_audit"]["denied_outside_workspace_count"]]
        bundle = packaged[row["session_id"]]
        name = (f'<a href="{escape(bundle["href"])}">{escape(row["session_id"])}</a>' if bundle["status"] == "available" else
                f'{escape(row["session_id"])}<br><small>Visual report unavailable: {escape(bundle["reason"])}</small>')
        table.append("<tr><td>" + name + "</td>" + "".join(f"<td>{escape(cell)}</td>" for cell in cells[1:]) + "</tr>")
    cards = "".join(f'<section><h2>CLI {escape(mode)}</h2><pre>{escape(json.dumps(arm, indent=2))}</pre></section>'
                    for mode, arm in report["cli"]["arms"].items())
    details = "".join(f'<details><summary>{escape(row["session_id"])} evidence and blocked attempts</summary>'
                       f'<pre>{escape(json.dumps(row, ensure_ascii=False, indent=2))}</pre></details>' for row in report["sessions"])
    page = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>KiroClops Phase 2 comparison</title><style>body{font:16px system-ui;background:#111827;color:#e5e7eb;margin:0}'
            'main{max-width:1400px;margin:auto;padding:28px}table{border-collapse:collapse;width:100%}th,td{text-align:left;border:1px solid #4b5563;padding:10px}'
            'th{background:#1f2937}a{color:#93c5fd}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}'
            'section,details{padding:16px;background:#1f2937;margin:16px 0;border-radius:8px}summary{cursor:pointer}</style><body><main>'
            '<h1>KiroClops Phase 2 comparison</h1>'
            f'<p>Status: <strong>{escape(report["status"])}</strong>. Only the explicitly selected sessions are included.</p>'
            '<p>A pass requires motion gates, independent visual checks, gameplay checks, matching source hashes and reviewed Kiro authorship. '
            'Exact runtime model confirmation and the shared budget are also required. Missing evidence stays unverified.</p>'
            '<p>IDE demo status separately records a completed native Power workflow. A demonstrated IDE workflow may still have '
            'unverified historical model/effort settings because the native export does not record every generation configuration.</p>'
            '<p><a href="benchmark.json" download>Download the JSON evidence summary</a></p>'
            '<p>Linked session names open bundled local videos, source diffs and attempt histories. '
            'Keep this entire folder together; it can be opened directly without a server. Only existing reports are copied.</p>'
            f'{cards}<p>IDE sessions (separate): {escape(", ".join(report["ide"]["session_ids"]) or "none selected")}</p>'
            '<div class="scroll"><table><thead><tr><th>Session</th><th>Surface</th><th>Mode</th><th>Strict outcome</th><th>Experiment outcome</th>'
            '<th>IDE demo</th><th>Strict model/effort</th><th>Required gates</th><th>Final RMSE / body</th><th>Attempts</th><th>Seconds</th>'
            '<th>Unused budget</th><th>Blocked outside requests</th></tr></thead>'
            f'<tbody>{"".join(table)}</tbody></table></div><h2>Session evidence</h2>{details}'
            f'<h2>Limits</h2><ul>{"".join(f"<li>{escape(item)}</li>" for item in report["limitations"])}</ul></main></body></html>')
    html_path.write_text(page, encoding="utf-8")
    return {"status": report["status"], "json_path": str(json_path), "html_path": str(html_path),
            "selected_session_count": len(report["sessions"])}
