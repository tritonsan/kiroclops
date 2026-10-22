"""Operator receipts for a GUI-driven IDE run; never starts a model or game."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from . import ide_evidence
from .kiro import _load_config
from .session import Session
from .storage import IntegrationError, file_lock, project_hashes, read_json, sha256, utc_now, write_json

AGENT, MODEL, EFFORT = "kiroclops-tools", "claude-opus-5", "high"
LIMITATIONS = [
    "UI settings are explicit operator observations backed by screenshots, not a per-generation configuration history.",
    "Native IDE metadata and reasoning records do not prove historical effort for every generation.",
    "The prompt hash binds the prepared generated file; this receipt alone does not prove the text submitted through the GUI.",
    "This is an IDE GUI receipt, not a CLI ACP stream or automatic IDE process-timeout attestation.",
    "Completed means the exported model turn ended; experiment acceptance is recorded separately.",
]


def _exclusive(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _config(directory):
    config = _load_config(directory, "tools")
    target = directory / "target"
    expected_prompt = target / ".kiro/kiroclops-prompt.md"
    if (config.get("surface") != "ide" or config.get("integration") != "ide_power"
            or config.get("agent_name") != AGENT or config.get("model") != MODEL or config.get("effort") != EFFORT
            or Path(config.get("target_dir", "")).resolve() != target
            or Path(config.get("prompt_path", "")).resolve() != expected_prompt):
        raise IntegrationError("ide_configuration", "Expected the generated tools/IDE Power profile for this exact target")
    prompt_hash = sha256(expected_prompt)
    if (prompt_hash != config.get("generated_hashes", {}).get("kiroclops-prompt.md")
            or not expected_prompt.read_text(encoding="utf-8").strip()):
        raise IntegrationError("prompt_changed", "The exact generated experiment prompt is required")
    return config, expected_prompt, prompt_hash


def _screenshots(directory, paths):
    if not paths:
        raise IntegrationError("missing_ui_evidence", "Provide at least one existing session-owned UI screenshot")
    records, seen = [], set()
    for raw in paths:
        path = ide_evidence._owned(directory, raw)
        if (not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
                or not 0 < path.stat().st_size <= 32 * 1024 * 1024 or path in seen):
            raise IntegrationError("invalid_ui_evidence", "UI screenshots must be distinct nonempty image files inside this session")
        seen.add(path)
        records.append({"path": path.relative_to(directory).as_posix(), "sha256": sha256(path)})
    return records


def start_ide_run(directory, *, screenshots, agent, model, effort):
    """Record observed UI settings and start the fresh experiment clock once."""
    directory = ide_evidence._session(directory)
    session = Session(directory)
    if (agent, model, effort) != (AGENT, MODEL, EFFORT):
        raise IntegrationError("ui_configuration", "Observed UI must show kiroclops-tools, claude-opus-5 and high")
    with file_lock(directory / ".ide-run.lock"):
        state = session.read()
        if (state.get("mode") != "tools" or state.get("status") != "new"
                or state.get("started_at") or state.get("deadline_at") or state.get("candidates")
                or state.get("selected_candidate_id") or state.get("active_job_id")
                or state.get("reference") or state.get("baseline_runs")):
            raise IntegrationError("ide_not_fresh", "IDE start needs an unused tools session with no previous clock or job")
        if any((directory / name).exists() for name in
               ("kiro/launch.json", "kiro/launch-start.json", "ide/finish-receipt.json")):
            raise IntegrationError("launch_exists", "An existing launch/clock is never overwritten or reset")
        config, prompt, prompt_hash = _config(directory)
        if (Path(state["target"]).resolve() != directory / "target"
                or Path(state["baseline"]).resolve() != directory / "baseline"
                or not state.get("baseline_hashes")
                or project_hashes(directory / "baseline") != state["baseline_hashes"]
                or project_hashes(directory / "target") != state["baseline_hashes"]):
            raise IntegrationError("experiment_integrity", "Fresh target and baseline must match their recorded source hashes")
        evidence = _screenshots(directory, screenshots)
        marker = directory / "target/.kiro/kiroclops-session.json"
        marker_hash = sha256(marker)
        # All fallible input validation precedes this one-way budget transition.
        # An interrupted receipt write never resets or refunds the clock.
        started = session.start_clock()
        record = {"schema_version": 2, "surface": "ide", "transport": "ide_gui", "mode": "tools",
                  "status": "running", "session_id": directory.name, "started_at": started["started_at"],
                  "deadline_at": started["deadline_at"], "cwd": str(directory / "target"),
                  "model_requested": MODEL, "effort_requested": EFFORT,
                  "prompt_path": str(prompt), "prompt_sha256": prompt_hash,
                  "configuration_path": str(marker), "configuration_sha256": marker_hash,
                  "ui_observed": {"agent": agent, "model": model, "effort": effort, "evidence": evidence},
                  "recorded_at": utc_now(), "strict_configuration_verified": False,
                  "limitations": LIMITATIONS}
        _exclusive(directory / "kiro/launch-start.json", record)
        _exclusive(directory / "kiro/launch.json", record)
        return record


def finish_ide_run(directory, *, export, screenshots=None, agent=None, model=None, effort=None):
    """Bind completion to the audited ZIP's final turn, preserving start history."""
    directory = ide_evidence._session(directory)
    session = Session(directory)
    with file_lock(directory / ".ide-run.lock"):
        launch_path = directory / "kiro/launch.json"
        record = read_json(launch_path)
        start_path = directory / "kiro/launch-start.json"
        if (record.get("schema_version") != 2 or record.get("surface") != "ide"
                or record.get("transport") != "ide_gui" or record.get("status") != "running"
                or record != read_json(start_path)):
            raise IntegrationError("ide_not_running", "Only this helper's unchanged running IDE receipt can be finished once")
        state = session.read()
        if (state.get("mode") != "tools" or state.get("started_at") != record["started_at"]
                or state.get("deadline_at") != record["deadline_at"] or state.get("active_job_id")):
            raise IntegrationError("ide_clock_changed", "The original clock must remain intact and all experiment jobs must finish")
        _, prompt, prompt_hash = _config(directory)
        if (str(prompt) != record["prompt_path"] or prompt_hash != record["prompt_sha256"]
                or sha256(record["configuration_path"]) != record["configuration_sha256"]):
            raise IntegrationError("ide_configuration_changed", "Generated prompt/profile changed after the recorded UI start")
        for item in record["ui_observed"]["evidence"]:
            if sha256(ide_evidence._owned(directory, item["path"])) != item["sha256"]:
                raise IntegrationError("ui_evidence_changed", "A startup UI screenshot changed")
        observed_after = None
        if any(value is not None for value in (screenshots, agent, model, effort)):
            if (agent, model, effort) != (AGENT, MODEL, EFFORT):
                raise IntegrationError("ui_configuration", "Post-run UI observation needs explicit kiroclops-tools, claude-opus-5 and high")
            after_evidence = _screenshots(directory, screenshots)
            before_paths = {item["path"] for item in record["ui_observed"]["evidence"]}
            if any(item["path"] in before_paths for item in after_evidence):
                raise IntegrationError("invalid_ui_evidence", "Post-run screenshots must be separate preserved files from startup evidence")
            observed_after = {"agent": agent, "model": model, "effort": effort, "evidence": after_evidence}
        export_path = ide_evidence._owned(directory, export)
        audit = ide_evidence.audit_ide_export(directory, export_path, expected_agent=AGENT,
            expected_model=MODEL, expected_effort=EFFORT, candidate_id=state.get("selected_candidate_id"))
        if (audit["configuration"]["status"] != "pass" or audit["model"]["status"] == "mismatch"
                or audit["tools"]["status"] != "complete"):
            raise IntegrationError("ide_export_configuration", "Native export identity/configuration or tool completion is inconsistent")
        timing = audit["timing"]
        if (timing.get("last_turn_stop_reason") != "end_turn" or timing.get("later_turn_start_present")
                or not timing.get("turn_start_count") or not timing.get("turn_end_count")):
            raise IntegrationError("ide_turn_incomplete", "Export must end with end_turn and contain no later pending turn")
        started = ide_evidence._time(record["started_at"])
        ended = ide_evidence._time(timing.get("last_turn_end_at"))
        first = ide_evidence._time(timing.get("first_experiment_message_at"))
        last = ide_evidence._time(timing.get("last_experiment_message_at"))
        if not started <= first <= last <= ended <= datetime.now(timezone.utc):
            raise IntegrationError("ide_timing", "Exported turn timestamps must follow the recorded start, with no later messages")
        users = timing.get("user_messages", [])
        if len(users) != 1 or not users[0].get("content_sha256"):
            raise IntegrationError("ide_prompt_ambiguous", "Exactly one native text user prompt must bind this single experiment")
        prompt_text = prompt.read_text(encoding="utf-8")
        exact_prompt = users[0]["content_sha256"] == ide_evidence._sha(prompt_text.encode("utf-8"))
        whitespace_prompt = users[0]["whitespace_normalized_sha256"] == ide_evidence._sha(" ".join(prompt_text.split()).encode("utf-8"))
        prompt_input = {"status": "exact" if exact_prompt else ("whitespace_only_difference" if whitespace_prompt else "mismatch"),
                        "prepared_file_sha256": prompt_hash, "native": users[0],
                        "note": "Exported user text is compared independently; whitespace equivalence is not a byte-exact prompt claim."}
        if state.get("finished_at") and ide_evidence._time(state["finished_at"]) > ended:
            raise IntegrationError("ide_timing", "The experiment finished after the claimed model turn; its completion is not bound")
        if sha256(export_path) != audit["export"]["sha256"]:
            raise IntegrationError("export_changed", "Native export changed during its audit")
        finish_path = directory / "ide/finish-receipt.json"
        if finish_path.exists():
            raise IntegrationError("finish_exists", "A previous finish receipt is preserved and never overwritten")
        evidence_path = directory / "ide/evidence.json"
        archive_path = directory / "ide/evidence-before-finish.json"
        if evidence_path.exists() and archive_path.exists():
            raise IntegrationError("evidence_history_exists", "Existing audit history must not be overwritten")
        completed = deepcopy(record)
        if observed_after is not None:
            completed["ui_observed_after"] = observed_after
        completed.update(status="completed", finished_at=timing["last_turn_end_at"],
                         elapsed_seconds=round((ended - started).total_seconds(), 6),
                         finish_recorded_at=utc_now(), export=audit["export"],
                         prompt_result={"stopReason": "end_turn"},
                         timing={**timing, "elapsed_basis": "recorded operator start to native IDE last turn_end"},
                         native_evidence_path=str(evidence_path),
                         start_receipt={"path": "kiro/launch-start.json", "sha256": sha256(start_path)},
                         prompt_input=prompt_input,
                         experiment_outcome={"status": "not_started" if not state.get("candidates") and not list((directory / "jobs").glob("*.json")) else "recorded_separately",
                                             "candidate_count": len(state.get("candidates", [])),
                                             "job_count": len(list((directory / "jobs").glob("*.json"))),
                                             "selected_candidate_id": state.get("selected_candidate_id")},
                         historical_model_status="unverified", historical_effort_status="unverified")
        if evidence_path.exists():
            # Preserve an earlier independent audit byte-for-byte before updating
            # the canonical pointer to this newly re-audited completion export.
            with archive_path.open("xb") as stream:
                stream.write(evidence_path.read_bytes())
        write_json(evidence_path, audit)
        _exclusive(finish_path, completed)
        write_json(launch_path, completed)
        return completed
