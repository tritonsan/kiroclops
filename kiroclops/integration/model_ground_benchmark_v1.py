"""Separate, read-only Sol cohort acceptance over the frozen ground experiment.

This module does not discover alternative runs, repair evidence, create session
reports or launch an engine/model. Missing independent evidence remains pending.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import html
import json
import math
from pathlib import Path
import shutil
import statistics

from kiroclops.common import ROOT
from . import benchmark as proof
from . import ground_benchmark as ground
from . import model_cli_v1 as launcher
from .kiro import KiroError, StreamConfigurationGuard
from .storage import identifier, project_hashes, read_json, sha256, write_json

COHORT_ID = "phase2b-sol-v1"
SESSION_IDS = tuple(f"cli-sol-ground-{n:02d}" for n in (1, 2, 3))
PROBES = {"air_idle_transition": False, "run_jump_land_brake": True}
COHORT_PATH = "config/model-trials/sol-v1.json"
OUTPUT_PATH = "artifacts/phase2b-sol-report"


def reporter_source_hashes():
    source = Path(__file__).resolve().parents[2]
    names = ["kiroclops/integration/" + name + ".py" for name in (
        "model_ground_benchmark_v1", "ground_benchmark", "ground_reporting", "benchmark",
        "authorship", "ide_evidence", "ground_review", "source_review", "movement_probe",
        "background_capture_v1", "model_cli_v1")]
    names += ["scripts/build_model_ground_report.py", "scripts/check_background_capture.py",
              "scripts/qualify_movement_probes.py", "adapters/movement-probe-godot4/Capture.tscn",
              "adapters/movement-probe-godot4/capture.gd"]
    return {name: sha256(source / name) for name in names}


def _gate(status="pending", reason=None, **fields):
    return {"status": status, **({"reason": reason} if reason else {}), **fields}


def _status(value):
    return "pass" if value in {"pass", "passed", "verified"} else "fail" if value in {"fail", "failed", "mismatch", "error"} else "pending"


def _owned(base, value, *, exists=True):
    return ground._owned(base, value, exists=exists)


def _date(value):
    if not isinstance(value, str):
        raise ValueError("A recorded ISO timestamp is required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone-aware evidence timestamps are required")
    return result


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _cohort():
    path = _owned(ROOT, COHORT_PATH)
    record = read_json(path)
    if (record.get("schema_version") != 1 or record.get("cohort_id") != COHORT_ID
            or record.get("experiment_id") != "ground-motion-v1"
            or record.get("model") != launcher.DEFAULT_MODEL or record.get("effort") != launcher.DEFAULT_EFFORT
            or record.get("cli_session_ids") != list(SESSION_IDS)
            or record.get("ide_session_ids", []) != [] or record.get("baseline_session_ids", []) != []
            or record.get("replacement_runs", False) is not False
            or record.get("max_candidates") != 3 or record.get("max_seconds") != 1800
            or record.get("cli_version") != "kiro-cli-chat 2.21.0" or record.get("kas_version") != "0.54.8"
            or not _hash(record.get("supplemental_instructions_sha256"))
            or not _hash(record.get("background_implementation_sha256"))):
        raise ValueError("Expected the explicit three-run Sol/high CLI cohort and its fixed budget")
    _date(record.get("registered_at"))
    if record.get("launcher") != launcher.launcher_binding():
        raise ValueError("The preregistered model launcher source changed")
    if record.get("reporter_source_hashes") != reporter_source_hashes():
        raise ValueError("The preregistered reporter/dependency source changed")
    locks = record.get("experiment_locks", {})
    if set(locks) != {"tracking_lock_sha256", "reference_lock_sha256"} or not all(_hash(x) for x in locks.values()):
        raise ValueError("Exact frozen experiment locks are required")
    return record, sha256(path)


def _bound_json(record):
    if not isinstance(record, dict) or not _hash(record.get("sha256")):
        raise FileNotFoundError("Hash-bound qualification evidence is not registered")
    path = _owned(ROOT, record["path"])
    if not path.is_relative_to((ROOT / "artifacts/model-trials").resolve()):
        raise ValueError("Supplemental qualification must stay under artifacts/model-trials")
    if sha256(path) != record["sha256"]:
        raise ValueError("Bound qualification bytes changed")
    return path, read_json(path)


def _background_preflight(cohort):
    """Replay all native PNG hashes and the captured Godot window observations."""
    try:
        path, record = _bound_json(cohort.get("background_preflight"))
        source, reference = _owned(ROOT, record["source"]), _owned(ROOT, record["reference"])
        run = _owned(path.parent, "capture")
        manifest, original = read_json(run / "manifest.json"), read_json(reference / "manifest.json")
        captured_sources = project_hashes(_owned(path.parent, "project"))
        if (record.get("status") != "pass" or record.get("frame_count") != 240
                or record.get("different_frames") != [] or record.get("viewport") != [640, 360]
                or record.get("preflight_script_sha256") != sha256(ROOT / "scripts/check_background_capture.py")
                or project_hashes(source) != record.get("source_hashes")
                or original.get("provenance", {}).get("full_source_hashes_before") != record["source_hashes"]
                or record.get("reference_manifest_sha256") != sha256(reference / "manifest.json")
                or record.get("capture_manifest_sha256") != sha256(run / "manifest.json")
                or manifest.get("fps") != 60 or manifest.get("frame_count") != 240 or manifest.get("status") != "ok"
                or manifest.get("game") != "slay" or manifest.get("variant") != "before"
                or any(manifest.get("provenance", {}).get(key) != captured_sources for key in (
                    "full_source_hashes_before", "full_source_hashes_after"))
                or any(manifest.get(key) != original.get(key) for key in ("game", "variant", "scenario", "fps", "frame_count", "viewport"))
                or manifest.get("frames") != original.get("frames")):
            return _gate("fail", "Background source, protocol or manifest evidence changed")
        expected = {}
        for index in range(240):
            name = f"frames/{index:06d}.png"
            old, new = sha256(_owned(reference, name)), sha256(_owned(run, name))
            expected[name] = {"reference": old, "offscreen": new}
            if old != new:
                return _gate("fail", "An offscreen native PNG differs from the reference")
        if record.get("frame_hashes") != expected:
            return _gate("fail", "Recorded PNG hash matrix differs")
        observer = _owned(run, "window-observer.json")
        samples = read_json(observer).get("samples", [])
        if (sha256(observer) != record.get("window_observer_sha256") or len(samples) < 240
                or record.get("window_samples") != len(samples) or record.get("window_status") != "pass"
                or not all(s.get("no_focus") is True and s.get("focused") is False and s.get("mode") == 0
                    and s.get("display_server") not in {None, "", "headless"}
                    and len(s.get("position", [])) == 2 and all(type(x) in (int, float) and math.isfinite(x) and x < -9000 for x in s["position"])
                    for s in samples)):
            return _gate("fail", "Recorded window samples do not establish no-focus offscreen rendering")
        return _gate("pass", path=str(path), sha256=sha256(path), native_frames_checked=240, window_samples=len(samples))
    except FileNotFoundError as exc:
        return _gate(reason=str(exc))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _gate("fail", str(exc))


def _probe(project, output, expected, name, normal):
    from .movement_probe import verify_probe
    project, output = _owned(ROOT, project), _owned(ROOT, output)
    boundary = (ROOT / "artifacts/model-trials").resolve()
    if not project.is_relative_to(boundary) or not output.is_relative_to(boundary):
        return _gate("fail", "Disposable probes must stay under artifacts/model-trials")
    receipt = read_json(_owned(output, "probe.json"))
    if receipt.get("probe") != name or receipt.get("normal_level") is not normal:
        return _gate("fail", "Supplemental probe identity or level differs")
    return verify_probe(project, output, expected_source_hashes=expected)


def _control_preflight(cohort):
    cases = {"parent-air": ("air_idle_transition", False, "pass"),
             "known-bad-air": ("air_idle_transition", False, "fail"),
             "parent-combined-normal": ("run_jump_land_brake", True, "pass")}
    try:
        path, record = _bound_json(cohort.get("control_preflight"))
        if (record.get("status") != "pass" or set(record.get("checks", {})) != set(cases)
                or record.get("script_sha256") != sha256(ROOT / "scripts/qualify_movement_probes.py")):
            return _gate("fail", "Runtime control qualification is incomplete or changed")
        checked = {}
        for key, (probe, normal, expected_status) in cases.items():
            item = record["checks"][key]
            expected = item.get("original_source_hashes")
            if not expected or project_hashes(_owned(ROOT, item["source"])) != expected:
                return _gate("fail", "Runtime control original source bytes changed")
            result = _probe(item["project"], item["output"], expected, probe, normal)
            checked[key] = result
            if (result.get("status") != expected_status or result != item.get("actual")
                    or result.get("receipt_sha256") != sha256(_owned(ROOT, item["output"]) / "probe.json")
                    or item.get("expected_status") != expected_status or item.get("source_preserved") is not True
                    or item.get("status") != "pass"):
                return _gate("fail", "Positive/negative runtime control does not replay", checks=checked)
        if (checked["known-bad-air"].get("checks") != {"airborne_idle_precondition": True,
                "natural_idle_to_fall": True, "horizontal_velocity_unchanged": False}
                or record["checks"]["parent-air"]["original_source_hashes"] != record["checks"]["parent-combined-normal"]["original_source_hashes"]
                or record["checks"]["known-bad-air"]["original_source_hashes"] == record["checks"]["parent-air"]["original_source_hashes"]):
            return _gate("fail", "The negative control must expose the actual airborne horizontal regression")
        return _gate("pass", path=str(path), sha256=sha256(path), checks=checked)
    except FileNotFoundError as exc:
        return _gate(reason=str(exc))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _gate("fail", str(exc))


def _configuration_binding(session, cohort, lock):
    config = launcher.load_model_config(session)
    source = ROOT / "power/kiroclops/ground-motion.md"
    if lock is None:
        return _gate(reason="Frozen experiment unavailable")
    base = source.read_text(encoding="utf-8")
    agent = read_json(_owned(session, config["agent_path"]))
    prefix = base + "\n\n## Supplemental cohort instructions\n\n"
    text = agent.get("prompt", "")
    if not text.startswith(prefix) or not text.endswith("\n"):
        return _gate("fail", "Agent instructions do not extend the exact frozen workflow")
    extra = text[len(prefix):-1]
    addition = text[len(base):]
    supplemental = {"kind": "cohort_instruction", "enforced_by_mcp": False,
                    "sha256": hashlib.sha256(extra.encode()).hexdigest()}
    directory = session / "target/.kiro"
    prompt = _owned(session, config["prompt_path"])
    if (sha256(source) != lock["tracking"]["files"].get("power/kiroclops/ground-motion.md")
            or config.get("model_launcher") != cohort["launcher"]
            or supplemental != config.get("supplemental_instructions")
            or supplemental["sha256"] != cohort["supplemental_instructions_sha256"]
            or config.get("instruction_sha256") != hashlib.sha256(text.encode()).hexdigest()
            or agent.get("model") != cohort["model"] or agent.get("effortLevel") != cohort["effort"]
            or any((directory / name).read_text(encoding="utf-8") != text for name in (
                "kiroclops-instructions.md", "powers/kiroclops/skills/motion-calibration/SKILL.md"))
            or not prompt.read_text(encoding="utf-8").endswith(addition)):
        return _gate("fail", "Model, supplemental instructions or source binding differs from registration")
    return _gate("pass", generated_hashes=config["generated_hashes"], prompt_sha256=sha256(prompt),
                 supplemental_instructions=supplemental, launcher=config["model_launcher"])


def _configuration(launch, events, stream_status, cohort):
    guard = StreamConfigurationGuard("kiroclops-tools", model=cohort["model"], effort=cohort["effort"])
    error = None
    advertised_efforts = set()
    for event in events:
        try:
            was_generating = guard.generation_started
            guard.observe(event)
            if not was_generating and guard.selected.get("model") == cohort["model"]:
                for node in launcher.frozen._stream_objects(event):
                    for option in node.get("configOptions", []) if isinstance(node.get("configOptions"), list) else []:
                        if isinstance(option, dict) and (option.get("category") == "thought_level" or option.get("id") in {"effortLevel", "effort", "thought_level"}):
                            advertised_efforts.update(item["value"] for item in option.get("options", [])
                                if isinstance(item, dict) and isinstance(item.get("value"), str))
        except KiroError as exc:
            error = {"code": exc.code, "message": str(exc)}
            break
    summary = guard.summary()
    selected = launch.get("acp_preflight", {})
    choices = selected.get("effort_selection", {})
    claims = (launch.get("model_requested") == cohort["model"] and launch.get("effort_requested") == cohort["effort"]
              and launch.get("model_reported") == [cohort["model"]] and launch.get("model_confirmation") == "stream_confirmed"
              and selected.get("selection_echo_verified") is True and selected.get("selected") == guard.expected
              and choices.get("observed_model") == cohort["model"] and choices.get("selected") == cohort["effort"]
              and cohort["effort"] in choices.get("values", []) and cohort["effort"] in advertised_efforts)
    mismatch = (launch.get("model_requested") not in (None, cohort["model"])
                or launch.get("effort_requested") not in (None, cohort["effort"]))
    status = "fail" if error or mismatch else "pass" if claims and summary["generation_configuration_verified"] and stream_status == "complete" else "pending"
    return {"status": status, "error": error, "launch_claims_match": claims,
            "advertised_efforts_before_generation": sorted(advertised_efforts), **summary}


def _finalization_job(session, state, candidate):
    if not candidate:
        return _gate(reason="No selected finalized candidate")
    matches = []
    for path in (session / "jobs").glob("*.json"):
        item = read_json(_owned(session, path))
        if item.get("action") == "finalize" and item.get("candidate_id") == candidate["candidate_id"]:
            matches.append((path, item))
    if len(matches) != 1:
        return _gate("fail" if matches else "pending", "Exactly one finalization job must own the selected candidate")
    path, job = matches[0]
    final, result = state.get("final") or {}, job.get("result") or {}
    if (job.get("job_id") != path.stem or job.get("session_id") != session.name or job.get("status") != "succeeded"
            or job.get("error") or result.get("error") or result.get("status") != "finished"
            or result.get("candidate_id") != candidate["candidate_id"]
            or _owned(session, result.get("comparison_path")) != _owned(session, final.get("comparison_path"))
            or any(result.get(key) != final.get(key) for key in ("numerical_status", "ground_status", "jump_status", "smoke_status"))):
        return _gate("fail", "Final result is not bound to a succeeded finalization job")
    return _gate("pass", job_id=job["job_id"], job_sha256=sha256(path))


def _timing(session, state, launch, cohort):
    jobs = []
    for path in sorted((session / "jobs").glob("*.json")):
        job = read_json(_owned(session, path))
        duration = None
        if job.get("status") == "succeeded" and job.get("started_at") and job.get("finished_at"):
            duration = (_date(job["finished_at"]) - _date(job["started_at"])).total_seconds()
            if duration < 0:
                raise ValueError("Job duration is negative")
        jobs.append({key: job.get(key) for key in ("job_id", "action", "candidate_id", "status", "started_at", "finished_at")}
                    | {"duration_seconds": duration})
    if not launch:
        return _gate(jobs=jobs)
    try:
        start, finish, clock = _date(launch["started_at"]), _date(launch["finished_at"]), _date(state["started_at"])
        wall, elapsed = (finish - start).total_seconds(), launch.get("elapsed_seconds")
        deadline = state.get("deadline_at")
        budget = (proof._finite(elapsed) and 0 < elapsed <= cohort["max_seconds"]
                  and 0 < wall <= cohort["max_seconds"] and abs(wall - elapsed) <= 1
                  and proof._finite(deadline) and abs(deadline - clock.timestamp() - cohort["max_seconds"]) <= 1
                  and _date(cohort["registered_at"]) <= start <= clock <= finish
                  and finish.timestamp() <= deadline and launch.get("timeout_seconds") == cohort["max_seconds"]
                  and 1 <= len(state.get("candidates", [])) <= cohort["max_candidates"])
        return _gate("pass" if budget else "fail", elapsed_seconds=elapsed, wall_seconds=wall, jobs=jobs,
                     note="Wall time includes model activity, tools, capture and cleanup; job time is not a measure of reasoning time.")
    except (KeyError, ValueError, TypeError, AttributeError):
        return _gate(reason="Completed launcher and immutable clock timestamps are unavailable", jobs=jobs)


def _runtime(session, candidate, cohort):
    if not candidate:
        return _gate(reason="No selected candidate for independent runtime review")
    path = _owned(session, "review/model-trial-runtime.json", exists=False)
    if not path.is_file():
        return _gate(reason="Independent selected-candidate runtime receipt is unavailable")
    try:
        record = read_json(path)
        if (record.get("schema_version") != 1 or record.get("cohort_id") != cohort["cohort_id"]
                or record.get("session_id") != session.name or record.get("candidate_id") != candidate["candidate_id"]
                or record.get("source_hashes") != candidate["source_hashes"] or set(record.get("checks", {})) != set(PROBES)
                or record.get("reviewed_by") not in {"assistant_independent_review", "human"}):
            return _gate("fail", "Runtime receipt does not bind this selected candidate")
        _date(record["reviewed_at"])
        checked = {}
        for name, normal in PROBES.items():
            item = record["checks"][name]
            result = _probe(item["project"], item["output"], candidate["source_hashes"], name, normal)
            checked[name] = result
            if result.get("receipt_sha256") != item.get("receipt_sha256"):
                return _gate("fail", "Runtime probe receipt hash differs from the independent review", checks=checked)
            if _status(result.get("status")) != "pass":
                return _gate(_status(result.get("status")), "Runtime assertion did not pass", checks=checked)
        return _gate("pass", checks=checked, path=str(path), sha256=sha256(path))
    except FileNotFoundError as exc:
        return _gate(reason=str(exc))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _gate("fail", str(exc))


def _background_session(session, cohort):
    from .background_capture_v1 import verify_background_session
    result = verify_background_session(session)
    receipt = read_json(_owned(session, "background-capture-v1.json"))
    if (receipt.get("implementation_sha256") != cohort["background_implementation_sha256"]
            or sha256(ROOT / "kiroclops/integration/background_capture_v1.py") != cohort["background_implementation_sha256"]):
        return _gate("fail", "Background installation implementation changed")
    return result


def _collect(session, cohort, lock):
    if not (session / "session.json").is_file():
        return {"session_id": session.name, "status": "not_run", "success": False, "gates": {}, "candidate_count": 0,
                "final_error": {"status": "unavailable", "mean_rmse_body": None}, "issues": []}
    state = read_json(_owned(session, "session.json"))
    if state.get("session_id") != session.name or state.get("experiment_id") != "ground-motion-v1" or state.get("mode") != "tools":
        raise ground.ExperimentMismatch("Explicit Sol session belongs to another experiment")
    issues = []
    launch = proof._read(session, "kiro/launch.json", issues, optional=True) or {}
    candidates = state.get("candidates", [])
    selected = [c for c in candidates if c.get("candidate_id") == state.get("selected_candidate_id")]
    if len(selected) > 1 or len({c.get("candidate_id") for c in candidates}) != len(candidates):
        raise ValueError("Candidate identifiers are duplicated")
    candidate = selected[0] if selected else None
    final = state.get("final") or {}
    saved = proof._read(session, final.get("comparison_path", "final/comparison.json"), issues, optional=True) if candidate else None
    evidence = {
        "experiment_sources": ground._state_integrity(session, state, candidate, lock),
        "workflow_binding": _configuration_binding(session, cohort, lock),
        "background_setup": _background_session(session, cohort),
        "capture_identity": ground._capture_identity(session, state, candidate, lock),
        "manual": ground._manual(session), "source_scope": ground._source_scope(session),
        "smoke": ground._smoke(session, candidate), "evaluation_job": ground._evaluation_job(session, candidate),
        "finalization_job": _finalization_job(session, state, candidate),
        "supplemental_runtime": _runtime(session, candidate, cohort)}
    quality, fresh = ground._quality(session, state, candidate, saved)
    fresh = fresh or {}
    evidence["comparison_integrity"] = quality
    gates = {key: _status(value.get("status")) for key, value in evidence.items()}
    gates.update(numerical=_status(fresh.get("ground", {}).get("numerical_status")),
                 jump_regression=_status(fresh.get("jump_regression", {}).get("numerical_status")))
    if gates["manual"] == "pass":
        gates["manual"] = _status(evidence["manual"].get("manual_validation_status"))
    if gates["manual"] == "pass":
        gates["manual"] = _status(fresh.get("manual_validation_status"))
    events, stream = proof._jsonl(session, "kiro/stdout.jsonl", issues)
    audit, audit_status = proof._jsonl(session, "kiro/tool-audit.jsonl", issues)
    evidence["model_configuration"] = _configuration(launch, events, stream, cohort)
    evidence["launch_completion"] = proof._launch_completion(launch, events, evidence["model_configuration"])
    from .authorship import audit_kiro_authorship
    author = audit_kiro_authorship(session, candidate_id=(candidate or {}).get("candidate_id")) if candidate else _gate(reason="No final candidate")
    if author.get("status") == "verified" and (author.get("candidate_id") != candidate["candidate_id"] or author.get("source_hashes") != candidate["source_hashes"]):
        author = _gate("fail", "Authorship belongs to a different selected source")
    evidence["authorship"] = author
    evidence["budget"] = _timing(session, state, launch, cohort)
    identity = (launch.get("surface") == "cli" and launch.get("transport") == "acp" and launch.get("mode") == "tools"
                and launch.get("model_launcher") == cohort["launcher"] and launch.get("cli_version") == cohort["cli_version"]
                and launch.get("kas_version") == cohort["kas_version"] and state.get("experiment_locks") == cohort["experiment_locks"])
    evidence["cohort_launch"] = _gate("pass" if identity else "fail" if launch else "pending")
    for key in ("model_configuration", "launch_completion", "authorship", "budget", "cohort_launch"):
        gates[key] = _status(evidence[key].get("status"))
    complete = all(value == "pass" for value in gates.values())
    failed = any(value == "fail" for value in gates.values()) or launch.get("status") in {"timeout", "cancelled", "cleanup_failed", "launch_failed"}
    outcome = ("ended_without_finalized_candidate" if not candidate and evidence["launch_completion"].get("status") == "pass"
               else "interrupted" if launch.get("status") in {"timeout", "cancelled", "cleanup_failed", "launch_failed"}
               else "final_candidate_recorded" if candidate else "pending" if launch else "not_run")
    failed = failed or outcome == "ended_without_finalized_candidate"
    error = fresh.get("mean_rmse_body")
    available = gates["capture_identity"] == gates["comparison_integrity"] == "pass" and proof._finite(error)
    return {"session_id": session.name, "cohort_id": COHORT_ID, "surface": "cli", "model": cohort["model"], "effort": cohort["effort"],
            "status": "pass" if complete else "fail" if failed else "pending" if launch else "not_run", "success": complete,
            "session_status": state.get("status"), "launch_status": launch.get("status"), "gates": gates, "evidence": evidence,
            "workflow_outcome": outcome,
            "candidate_count": len(candidates), "selected_candidate_id": state.get("selected_candidate_id"),
            "elapsed_seconds": launch.get("elapsed_seconds"), "tool_audit": proof._tool_audit(session, audit, audit_status),
            "final_error": {"status": "available" if available else "unavailable", "mean_rmse_body": error if available else None},
            "cohort_identity": {"baseline_hashes": state.get("baseline_hashes"), "experiment_locks": state.get("experiment_locks"),
                "cli_version": launch.get("cli_version"), "kas_version": launch.get("kas_version"), "launcher": launch.get("model_launcher"),
                "supplemental_instructions_sha256": cohort["supplemental_instructions_sha256"],
                "background_implementation_sha256": cohort["background_implementation_sha256"]}, "issues": issues}


def build_benchmark(session_ids=None):
    cohort, cohort_sha = _cohort()
    ids = list(SESSION_IDS) if session_ids is None else list(session_ids)
    if not ids or len(ids) != len(set(ids)) or any(name not in SESSION_IDS for name in ids):
        raise ValueError("Only explicit preregistered Sol session IDs are allowed")
    lock = None
    try:
        lock = ground.verify_ground_lock(root=ROOT)
        actual = {key: lock[key] for key in ("tracking_lock_sha256", "reference_lock_sha256")}
        freeze = _gate("pass" if actual == cohort["experiment_locks"] else "fail", "Exact frozen measurement/reference identity")
    except (FileNotFoundError, KeyError) as exc:
        freeze = _gate(reason=type(exc).__name__)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        freeze = _gate("fail", type(exc).__name__)
    qualifications = {"frozen_experiment": freeze, "background_preflight": _background_preflight(cohort),
                      "control_preflight": _control_preflight(cohort)}
    rows = []
    for name in ids:
        session = _owned(ROOT, Path("sessions") / identifier(name), exists=False)
        try:
            row = _collect(session, cohort, lock)
        except ground.ExperimentMismatch:
            raise
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
            row = {"session_id": name, "status": "pending" if isinstance(exc, (FileNotFoundError, KeyError)) else "fail",
                   "success": False, "gates": {}, "candidate_count": 0, "final_error": {"status": "unavailable", "mean_rmse_body": None},
                   "issues": [f"Incomplete or invalid evidence: {type(exc).__name__}: {exc}"]}
        rows.append(row)
    identities = [r.get("cohort_identity") for r in rows]
    same = (set(ids) == set(SESSION_IDS) and all(identities) and all(x == identities[0] for x in identities)
            and bool(identities[0].get("baseline_hashes")))
    all_gates = same and all(r["success"] for r in rows) and all(q["status"] == "pass" for q in qualifications.values())
    return {"schema_version": 1, "cohort_id": COHORT_ID, "experiment_id": "ground-motion-v1", "cohort": cohort,
            "cohort_sha256": cohort_sha, "selected_session_ids": ids, "registration_complete": set(ids) == set(SESSION_IDS),
            "qualifications": qualifications, "sessions": rows, "cli": {"planned_runs": 3, "selected_runs": len(rows),
                "same_cohort": same, "successes": sum(r["success"] for r in rows), "all_pass": all_gates,
                "mean_final_rmse_body": statistics.mean(r["final_error"]["mean_rmse_body"] for r in rows)
                    if len(rows) == 3 and all(r["final_error"]["status"] == "available" for r in rows) else None},
            "acceptance": "pass" if all_gates else "pending_or_failed", "ide": "not_part_of_this_cohort",
            "tool_free_comparison": "not_part_of_this_cohort", "cross_model_comparison": "not_a_controlled_model_only_comparison"}


def generate_report(session_ids=None):
    result = build_benchmark(session_ids)
    directory = _owned(ROOT, OUTPUT_PATH, exists=False)
    directory.mkdir(parents=True, exist_ok=True)
    packaged = {}
    for name in result["selected_session_ids"]:
        session = _owned(ROOT, Path("sessions") / name, exists=False)
        source = session / "report"
        if not source.is_dir():
            packaged[name] = {"status": "unavailable", "reason": "No existing session visual report"}
            continue
        try:
            files = proof._regular_report_tree(source, session)
            if source / "index.html" not in files:
                raise ValueError("No regular report index")
            proof._validate_report_links(source, files)
            target = _owned(directory, Path("reports") / name, exists=False)
            for original in files:
                destination = _owned(directory, target / original.relative_to(source), exists=False)
                destination.parent.mkdir(parents=True, exist_ok=True)
                info = original.lstat()
                if original.is_symlink() or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Report source changed or is linked")
                if destination.exists() and destination.stat().st_nlink != 1:
                    raise ValueError("Report destination is a shared file")
                shutil.copy2(original, destination, follow_symlinks=False)
                if sha256(original) != sha256(destination):
                    raise ValueError("Report source changed during packaging")
            packaged[name] = {"status": "available", "href": f"reports/{name}/index.html"}
        except (OSError, ValueError) as exc:
            packaged[name] = {"status": "unavailable", "reason": str(exc)}
    result["visual_reports"] = packaged
    write_json(_owned(directory, "benchmark.json", exists=False), result)
    cards = []
    escape = lambda value: html.escape(str(value), quote=True)
    for row in result["sessions"]:
        bundle = packaged[row["session_id"]]
        link = (f'<a href="{escape(bundle["href"])}">Videos, curves and code diffs</a>' if bundle["status"] == "available"
                else "Visual report not available: " + escape(bundle["reason"]))
        gates = "".join(f"<li>{escape(key)}: <b>{escape(value)}</b></li>" for key, value in row["gates"].items())
        error = row["final_error"]["mean_rmse_body"]
        cards.append(f'<section><h2>{escape(row["session_id"])}</h2><p>{escape(row["status"])} · '
                     f'{row["candidate_count"]}/3 candidates · duration {escape(row.get("elapsed_seconds", "unavailable"))} s</p>'
                     f'<p>Final horizontal error: {escape("unavailable" if error is None else round(error, 5))}</p><p>{link}</p>'
                     f'<details><summary>Acceptance gates</summary><ul>{gates}</ul></details></section>')
    document = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>KiroClops · Sol CLI experiments</title><style>body{background:#10141e;color:#edf1f7;font:16px system-ui;max-width:1100px;margin:auto;padding:32px}'
        'section,details{background:#1a2434;padding:20px;margin:18px 0;border-radius:12px}a{color:#96cdff}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
        f'<h1>KiroClops · GPT-5.6 Sol / high</h1><p>Separate CLI cohort: {COHORT_ID}. Overall acceptance: <b>{escape(result["acceptance"])}</b>.</p>'
        f'<p>{result["cli"]["successes"]}/3 sessions passed all gates. The shared qualification and the same-baseline gates are also required.</p>'
        '<p>The 18 horizontal conditions, 12 jump conditions, independent visual/source review and additional runtime checks are verified separately. '
        'This cohort has no IDE and no tool-free comparison. Compared with the earlier Opus cohort, the instructions and runtime also differ; the model effect alone is not considered measured.</p>'
        + "".join(cards) + '<p><a href="benchmark.json">Full machine-readable evidence</a></p><details><summary>Experiment record</summary><pre>'
        + escape(json.dumps(result, ensure_ascii=False, indent=2)) + '</pre></details></html>')
    index = _owned(directory, "index.html", exists=False)
    if index.exists() and index.stat().st_nlink != 1:
        raise ValueError("Report index must be an independent regular file")
    index.write_text(document, encoding="utf-8")
    return index
