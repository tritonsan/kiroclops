"""Operator-only independent visual review of a sealed experiment.

Only explicitly supplied image-space points/events are recorded. Reanalysis
may update independent-validation results, never the measured motion, source
code, raw frames or numerical acceptance thresholds.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

from . import evaluation
from .session import Session
from .storage import (IntegrationError, _retry_permission, file_lock, identifier,
                      project_hashes, read_json, sha256, utc_now, write_json)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _measurements(run: Path) -> str:
    report = read_json(run / "analysis.json")
    return _digest({key: report.get(key) for key in evaluation.MEASUREMENT_FIELDS})


def _inside(raw, base: Path, *, relative_base: Path | None = None) -> Path:
    if not isinstance(raw, (str, Path)) or not str(raw):
        raise IntegrationError("invalid_review_path", "Expected a run path")
    path = Path(raw)
    resolved = (path if path.is_absolute() else (relative_base or base) / path).resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise IntegrationError("invalid_review_path", f"Review path is outside its permitted evidence directory: {raw}")
    return resolved


def _raw_fingerprint(run: Path) -> str:
    manifest = read_json(run / "manifest.json")
    count = manifest.get("frame_count")
    if type(count) is not int or count < 1:
        raise IntegrationError("invalid_evidence", "Capture has no complete frame sequence")
    return _digest({"manifest": sha256(run / "manifest.json"),
                    "frames": [sha256(run / "frames" / f"{index:06d}.png") for index in range(count)]})


def _require_source(project: Path, expected: dict) -> None:
    if not expected or project_hashes(project) != expected:
        raise IntegrationError("experiment_integrity", f"Sealed source changed: {project}")


def _runs(state: dict, candidate: dict, directory: Path, candidate_root: Path) -> dict[str, list[Path]]:
    definitions = {
        "reference": (state.get("reference", {}), directory / "reference", "tiny", "reference", None),
        "baseline": (state.get("baseline_runs", {}), directory / "baseline-runs", "slay", "before", state["baseline_hashes"]),
        "candidate": ({"calibration": candidate.get("calibration_runs"), "validation": candidate.get("validation_runs")},
                      candidate_root / "runs", "slay", "after", candidate["source_hashes"]),
    }
    groups = {}
    for role, (saved, base, game, variant, expected_source) in definitions.items():
        group = []
        for split in ("calibration", "validation"):
            paths = saved.get(split)
            if not isinstance(paths, list) or not paths:
                raise IntegrationError("incomplete_experiment", f"Missing {role} {split} captures")
            observed = set()
            for raw in paths:
                run = _inside(raw, base.resolve(), relative_base=directory)
                if run in group:
                    raise IntegrationError("invalid_evidence", "Duplicate capture in sealed experiment")
                manifest = read_json(run / "manifest.json")
                scenario = manifest.get("scenario", {})
                if (manifest.get("game") != game or manifest.get("variant") != variant
                        or scenario.get("split") != split or manifest.get("status") != "ok"):
                    raise IntegrationError("invalid_evidence", f"Capture identity differs from sealed slot: {run}")
                slot = (scenario.get("hold_ticks"), scenario.get("spawn_offset"), scenario.get("repeat"), scenario.get("start_tick"))
                if slot in observed:
                    raise IntegrationError("invalid_evidence", "Duplicate scenario in sealed experiment")
                observed.add(slot)
                if expected_source is not None:
                    provenance = manifest.get("provenance", {})
                    if any(provenance.get(key) != expected_source for key in
                           ("full_source_hashes_before", "full_source_hashes_after")):
                        raise IntegrationError("experiment_integrity", f"Capture source does not match sealed snapshot: {run}")
                report = read_json(run / "analysis.json")
                recorded_hash = report.get("provenance", {}).get("manifest_sha256")
                if recorded_hash is not None and recorded_hash != sha256(run / "manifest.json"):
                    raise IntegrationError("invalid_evidence", f"Manifest changed after measurement: {run}")
                group.append(run)
            expected = {(hold, offset, repeat, 30)
                        for hold in ((3, 24) if split == "calibration" else (8, 14))
                        for offset in ((0,) if split == "calibration" else (0, 24))
                        for repeat in (1, 2, 3)}
            if observed != expected:
                raise IntegrationError("incomplete_experiment", f"Incomplete {role} {split} matrix")
        groups[role] = group
    return groups


def _controls(value, candidate_runs: list[Path], base: Path) -> tuple[list[dict], str | None]:
    input_hash = None
    if value is None:
        return [], input_hash
    if isinstance(value, (str, Path)):
        path = Path(value).resolve()
        input_hash = sha256(path)
        value = read_json(path)
    if not isinstance(value, list):
        raise IntegrationError("invalid_controls", "Controls JSON must contain a list")
    records, seen = [], set()
    event_names = {"takeoff_frame", "apex_frame", "landing_frame"}
    for item in value:
        if not isinstance(item, dict) or set(item) - {"run", "points", "event_frames"} or "run" not in item:
            raise IntegrationError("invalid_controls", "Each control entry needs run, points and/or event_frames")
        run = _inside(item["run"], base)
        if run not in candidate_runs or run in seen:
            raise IntegrationError("invalid_controls", "Controls must name unique captures of the selected candidate")
        seen.add(run)
        manifest = read_json(run / "manifest.json")
        count, viewport = manifest["frame_count"], manifest["viewport"]
        points = item.get("points", [])
        events = item.get("event_frames")
        if not isinstance(points, list) or (not points and events is None):
            raise IntegrationError("invalid_controls", "Provide explicit points or event frames")
        point_indices = set()
        for point in points:
            if (not isinstance(point, dict) or not {"frame_index", "x", "y"} <= set(point)
                    or set(point) - {"frame_index", "x", "y", "evidence"}):
                raise IntegrationError("invalid_controls", "Points need frame_index, x, y and optional evidence")
            index = point["frame_index"]
            if type(index) is not int or not 0 <= index < count or index in point_indices:
                raise IntegrationError("invalid_controls", "Point frames must be unique and inside the capture")
            point_indices.add(index)
            for key, limit in zip(("x", "y"), viewport):
                number = point[key]
                if (isinstance(number, bool) or not isinstance(number, (int, float))
                        or not math.isfinite(number) or not 0 <= number < limit):
                    raise IntegrationError("invalid_controls", "Point coordinates must be finite viewport coordinates")
        if events is not None:
            if (not isinstance(events, dict) or not event_names <= set(events)
                    or set(events) - event_names - {"evidence"}):
                raise IntegrationError("invalid_controls", "Events need takeoff_frame, apex_frame and landing_frame")
            values = [events[key] for key in ("takeoff_frame", "apex_frame", "landing_frame")]
            if any(type(index) is not int or not 0 <= index < count for index in values) or not values[0] < values[1] < values[2]:
                raise IntegrationError("invalid_controls", "Event frames must follow takeoff < apex < landing inside the capture")
        records.append({"run": run, "points": deepcopy(points), "event_frames": deepcopy(events)})
    return records, input_hash


def _restore(backups: dict[Path, bytes | None]) -> None:
    for path, content in backups.items():
        if content is None:
            _retry_permission(lambda path=path: path.unlink(missing_ok=True))
            continue
        temporary = path.with_name(path.name + ".review-restore-" + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(content)
            _retry_permission(lambda: os.replace(temporary, path))
        finally:
            _retry_permission(lambda: temporary.unlink(missing_ok=True))


def review_session(directory: Path, controls=None, *, source="human", reuse_controls_from=None) -> dict:
    """Review a finished, sealed session using explicitly observed controls.

    Relative control paths are resolved from selected-candidate/runs. Controls
    default to source='human'; an assistant's own visual observations must use
    source='assistant_visual'. Without controls, existing observations and
    verified raw-pixel reuse are rechecked; no new observations are invented.
    reuse_controls_from explicitly approves a different, finished and manually
    reviewed session as a source of exact-pixel-matched direct observations.
    """
    if source not in {"human", "assistant_visual"}:
        raise IntegrationError("invalid_controls", "Review source must be human or assistant_visual")
    session = Session(directory)
    directory = session.directory
    with file_lock(directory / ".session.lock"):
        state = session.read()
        selected = state.get("selected_candidate_id")
        if (state.get("status") != "finished" or not selected or not state.get("sealed_at")
                or not state.get("final") or state["final"].get("candidate_id") != selected):
            raise IntegrationError("review_not_ready", "Independent review requires a finished, sealed candidate")
        if state.get("active_job_id"):
            raise IntegrationError("session_busy", "Finish or cancel the active job before manual review")
        candidate_root = directory / "candidates" / identifier(selected)
        candidate = read_json(candidate_root / "candidate.json")
        saved = next((entry for entry in state.get("candidates", []) if entry.get("candidate_id") == selected), None)
        if (candidate.get("candidate_id") != selected or candidate.get("status") != "finalized"
                or saved != candidate):
            raise IntegrationError("experiment_integrity", "Selected candidate metadata differs from the finalized session")
        baseline = directory / "baseline"
        if Path(state["baseline"]).resolve() != baseline:
            raise IntegrationError("invalid_review_path", "Baseline is outside its fixed session location")
        _require_source(baseline, state["baseline_hashes"])
        _require_source(candidate_root / "project", candidate["source_hashes"])
        groups = _runs(state, candidate, directory, candidate_root)
        all_runs = [run for group in groups.values() for run in group]
        records, controls_hash = _controls(controls, groups["candidate"], candidate_root / "runs")
        comparison_path = _inside(state["final"]["comparison_path"], directory / "final", relative_base=directory)
        original_comparison = read_json(comparison_path)
        measurements = {run: _measurements(run) for run in all_runs}
        raw = {run: _raw_fingerprint(run) for run in all_runs}
        annotations = {run: (directory / "reference/annotations" if run in groups["reference"] else directory / "annotations")
                       for run in all_runs}
        paths = [session.path, comparison_path]
        paths += [run / "analysis.json" for run in all_runs]
        paths += [evaluation.controls_path(run, annotations[run]) for run in all_runs]
        backups = {path: path.read_bytes() if path.exists() else None for path in paths}
        summaries = []
        reused = None
        receipt_path = directory / "final" / ("manual-review-" + uuid.uuid4().hex[:12] + ".json")
        try:
            if reuse_controls_from is not None:
                from .control_reuse import reuse_controls
                reused = reuse_controls(reuse_controls_from, groups, annotations)
            for record in records:
                evaluation.record_manual_controls(record["run"], annotations[record["run"]], record["points"],
                                                  record["event_frames"], source=source)
            for run in all_runs:
                summaries.append(evaluation.reanalyze_controls(run, annotations[run]))
                if _measurements(run) != measurements[run]:
                    raise IntegrationError("measurement_changed", "Manual review changed an image-derived measurement")
            _require_source(baseline, state["baseline_hashes"])
            _require_source(candidate_root / "project", candidate["source_hashes"])
            if any(_raw_fingerprint(run) != raw[run] for run in all_runs):
                raise IntegrationError("raw_evidence_changed", "Raw pixels or capture manifest changed during review")
            if reused is not None:
                from .control_reuse import verify_source_unchanged
                verify_source_unchanged(reused)
            comparison = evaluation.final_comparison(groups["reference"], groups["baseline"], groups["candidate"])
            for key in ("gates", "validation_scenarios", "coverage", "alignment", "numerical_status"):
                if comparison.get(key) != original_comparison.get(key):
                    raise IntegrationError("numerical_result_changed", f"Manual review changed the frozen numerical result: {key}")
            reviewed_at = utc_now()
            receipt = {"schema_version": 1, "reviewed_at": reviewed_at, "candidate_id": selected,
                       "source": source, "controls_file_sha256": controls_hash,
                       "reused_controls": reused,
                       "supplied_controls": [{"run": str(record["run"]), "points": record["points"],
                                              "event_frames": record["event_frames"]} for record in records],
                       "measurements_unchanged": True, "raw_evidence_unchanged": True, "sources_unchanged": True,
                       "measurement_sha256": {str(run): value for run, value in measurements.items()},
                       "raw_evidence_sha256": {str(run): value for run, value in raw.items()},
                       "run_reviews": summaries, "numerical_status": comparison["numerical_status"],
                       "measurement_status": comparison["measurement_status"], "status": comparison["status"]}
            state["final"].update(numerical_status=comparison["numerical_status"],
                                  measurement_status=comparison["measurement_status"],
                                  manual_validation_status=comparison["manual_validation_status"],
                                  overall_status=comparison["status"], reviewed_at=reviewed_at,
                                  manual_review_path=str(receipt_path))
            write_json(receipt_path, receipt)
            write_json(comparison_path, comparison)
            write_json(session.path, state)
        except Exception:
            _restore(backups)
            _retry_permission(lambda: receipt_path.unlink(missing_ok=True))
            raise
        return {"status": comparison["status"], "numerical_status": comparison["numerical_status"],
                "measurement_status": comparison["measurement_status"], "candidate_id": selected,
                "comparison_path": str(comparison_path), "review_path": str(receipt_path),
                "reviewed_run_count": len(all_runs), "supplied_control_run_count": len(records),
                "reused_controls": ({key: reused[key] for key in ("source_session", "points", "events", "skipped")}
                                    if reused is not None else None),
                "measurements_unchanged": True, "sources_unchanged": True}
