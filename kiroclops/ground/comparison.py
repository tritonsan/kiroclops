"""Frozen horizontal scenario coverage and separate numerical/manual decisions."""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path

import numpy as np

from .analysis import FRAME_COUNT, FPS, scenario_phases
from .annotation import MANUAL_SOURCES


def _reports(values):
    if isinstance(values, (str, Path, dict)):
        values = [values]
    reports = []
    for value in values:
        if isinstance(value, dict):
            reports.append(value)
        else:
            path = Path(value)
            reports.append(json.loads((path / "analysis.json" if path.is_dir() else path).read_text(encoding="utf-8")))
    return reports


def _key(report):
    s = report["scenario"]
    return (s["id"], s.get("spawn_offset", 0), s["repeat"])


def _planned(split):
    if split == "calibration":
        return {(sid, 0, repeat) for sid in ("r06", "r48") for repeat in (1, 2, 3)}
    if split == "validation":
        return {(sid, offset, repeat) for sid in ("r18", "l30", "reverse24") for offset in (0, 24) for repeat in (1, 2, 3)}
    raise ValueError("split must be calibration or validation")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _usable(report):
    phases = scenario_phases(report["scenario"])
    if (report.get("fps") != FPS or report.get("camera_valid") is not True
            or not _finite(report.get("body_height_px")) or report["body_height_px"] <= 0
            or not _finite(report.get("valid_ratio")) or report["valid_ratio"] < .95):
        raise ValueError("unreliable camera, timing, body scale or tracking coverage")
    if report.get("status") != "ok" and not (report.get("status") == "rejected" and report.get("errors") == ["independent manual annotation validation failed"]):
        raise ValueError("measurement is rejected")
    frames = report.get("frames", [])
    if len(frames) != FRAME_COUNT:
        raise ValueError("native frame sequence is incomplete")
    for i, frame in enumerate(frames):
        if (type(frame.get("frame_index")) is not int or frame["frame_index"] != i
                or not _finite(frame.get("time_s")) or abs(frame["time_s"]-i/FPS) > 1e-5
                or type(frame.get("valid")) is not bool
                or (frame["valid"] and not _finite(frame.get("x_body")))):
            raise ValueError("frames contain nonfinite, reordered or missing native measurements")
    if sum(f["valid"] for f in frames)/FRAME_COUNT < .95:
        raise ValueError("actual whole-run coverage below 95%")
    for phase in phases:
        if sum(frames[t]["valid"] for t in range(phase["start"], phase["end"]))/(phase["end"]-phase["start"]) < .95:
            raise ValueError("actual phase coverage below 95%")
    return phases


def curve_rmse(reference, target):
    if _key(reference) != _key(target):
        raise ValueError("input-aligned scenario slots differ")
    phases = _usable(reference)
    _usable(target)
    mse = []
    for phase in phases:
        pairs = [(reference["frames"][i]["x_body"], target["frames"][i]["x_body"])
                 for i in range(phase["start"], phase["end"])
                 if reference["frames"][i]["valid"] and target["frames"][i]["valid"]]
        if len(pairs)/(phase["end"]-phase["start"]) < .95:
            raise ValueError("common native samples cover less than 95% of a phase")
        mse.append(float(np.mean([(a-b)**2 for a,b in pairs])))
    return float(np.sqrt(np.mean(mse)))


def _triplet(reference, before, target, *, require_improvement=True):
    old = curve_rmse(reference, before)
    new = curve_rmse(reference, target)
    improvement = 1-new/old if old > 1e-12 else None
    ref, after = reference["metrics"], target["metrics"]
    for field in ("stop_distance_body", "end_position_body"):
        if not _finite(ref.get(field)) or not _finite(after.get(field)):
            raise ValueError("unobservable/nonfinite coast or endpoint metric")
    speed_error = None
    phases = scenario_phases(reference["scenario"])
    speed_eligible = phases[-2]["end"]-phases[-2]["start"] >= 18
    if speed_eligible:
        if not _finite(ref.get("speed_body_s")) or not _finite(after.get("speed_body_s")) or abs(ref["speed_body_s"]) <= 1e-12:
            raise ValueError("eligible last-eight-frame speed is unobservable or zero")
        speed_error = abs(after["speed_body_s"]-ref["speed_body_s"])/abs(ref["speed_body_s"])
    tolerance = max(.15, 1/reference["body_height_px"]+1/target["body_height_px"])
    stop_error = abs(after["stop_distance_body"]-ref["stop_distance_body"])
    endpoint_error = abs(after["end_position_body"]-ref["end_position_body"])
    gates = {"curve_rmse": new <= .15, "improvement": not require_improvement or improvement is not None and improvement >= .5,
             "speed": not speed_eligible or speed_error <= .1,
             "stop_distance": stop_error <= tolerance, "end_position": endpoint_error <= tolerance}
    return {"scenario": reference["scenario"], "before_rmse_body": old, "after_rmse_body": new,
            "candidate_rmse_body": new, "improvement_fraction": improvement,
            "improvement_required": require_improvement,
            "speed_relative_error": speed_error, "speed_eligible": speed_eligible,
            "stop_distance_error_body": stop_error, "end_position_error_body": endpoint_error,
            "position_tolerance_body": tolerance, "gates": gates, "pass": all(gates.values())}


def _manual(reports, require_coverage):
    observations, categories, failures = {}, set(), []
    for report in reports:
        validation = report.get("annotation_validation", {})
        # Position tolerance is a role-wide median. A small run-level sample
        # may contain an outlier; it is not a second, stricter acceptance gate.
        # Unbound/invalid observations still fail individually below.
        if validation.get("status") == "fail" and not validation.get("points"):
            failures.append(report.get("run_id"))
        height = report.get("body_height_px")
        if not _finite(height) or height <= 0:
            failures.append(report.get("run_id"))
            continue
        for point in validation.get("points", []):
            if point.get("source") not in MANUAL_SOURCES:
                continue
            error = point.get("error_px")
            if (point.get("hash_bound") is not True or not isinstance(point.get("frame_sha256"), str)
                    or not _finite(error) or error < 0 or not _finite(point.get("x")) or not _finite(point.get("y"))):
                failures.append(report.get("run_id"))
                continue
            # One PNG is one independent observation, even with another label
            # or slightly different clicks in a deterministic repeat run.
            signature = point["frame_sha256"]
            normalized = error/max(1., .05*height)
            observations[signature] = max(observations.get(signature, 0), normalized)
            categories.update(point.get("categories", []))
    missing = sorted({"flip", "brake", "reverse"}-categories) if require_coverage else []
    maximum = max(observations.values()) if observations else None
    median = float(np.median(list(observations.values()))) if observations else None
    failed = bool(failures) or (median is not None and median > 1)
    passed = len(observations) >= 20 and not missing and not failed
    return {"status": "fail" if failed else ("pass" if passed else "pending"),
            "checked_point_count": len(observations), "required_point_count": 20,
            "median_error_over_tolerance": median, "max_error_over_tolerance": maximum,
            "categories": sorted(categories), "missing_categories": missing, "failed_runs": failures}


def compare_runs(refs, before, after, *, split="validation"):
    groups = [_reports(values) for values in (refs, before, after)]
    planned = _planned(split)
    errors, pending, maps = [], [], []
    scales = defaultdict(set)
    for role, reports in zip(("reference", "before", "after"), groups):
        mapping = {}
        for report in reports:
            if report.get("variant") != role:
                errors.append(f"wrong variant in {role} group")
            try:
                _usable(report)
                scales[report["game"]].add(report["body_height_px"])
                if report.get("scenario", {}).get("split") != split:
                    continue
                key = _key(report)
                if key in mapping:
                    errors.append(f"duplicate {role} scenario {key}")
                mapping[key] = report
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(f"invalid {role} measurement: {exc}")
        if set(mapping)-planned:
            errors.append(f"unexpected {role} scenario slots")
        maps.append(mapping)
    if any(len(values) != 1 for values in scales.values()):
        errors.append("body scale changes within a game")
    common = set(maps[0]) & set(maps[1]) & set(maps[2]) & planned
    missing = sorted(planned-common)
    if missing:
        pending.append(f"planned {split} matrix incomplete: {len(missing)} triplets missing")
    rows = []
    for key in sorted(common):
        try:
            rows.append(_triplet(*(mapping[key] for mapping in maps), require_improvement=split == "validation"))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"invalid comparison {key}: {exc}")
    if any(not row["pass"] for row in rows):
        errors.append("one or more horizontal transfer gates failed")
    numerical = "fail" if errors else ("pending" if missing or len(rows) != len(planned) else "pass")
    manual = {role: _manual(reports, split == "validation") for role, reports in zip(("reference", "baseline", "candidate"), groups)}
    manual_status = "fail" if any(m["status"] == "fail" for m in manual.values()) else (
        "pass" if all(m["status"] == "pass" for m in manual.values()) else "pending")
    status = "fail" if "fail" in (numerical, manual_status) else ("pass" if numerical == manual_status == "pass" else "pending")
    if manual_status == "pending":
        pending.append("independent role-specific pixel observations or flip/brake/reverse coverage incomplete")
    return {"schema_version": 1, "mechanic": "ground", "status": status, "success": status == "pass",
            "numerical_status": numerical, "manual_validation_status": manual_status, "measurement_status": manual_status,
            "mean_rmse_body": float(np.mean([row["after_rmse_body"] for row in rows])) if rows else None,
            "mean_before_rmse_body": float(np.mean([row["before_rmse_body"] for row in rows])) if rows else None,
            "validation_scenarios": rows, "scenario_checks": rows, "manual_validation": manual,
            "annotation_validation": manual,
            "coverage": {"complete_triplets": len(rows), "required_triplets": len(planned),
                         "missing": [{"id": sid, "spawn_offset": offset, "repeat": repeat} for sid,offset,repeat in missing]},
            "gates": {"valid_ratio_total_and_phase_min": .95, "curve_rmse_body_max": .15,
                      "minimum_improvement": .5 if split == "validation" else None, "speed_relative_error_max": .1,
                      "stop_end_tolerance_body": "max(0.15,1/H_reference+1/H_target)",
                      "manual_points_per_role": 20, "manual_tolerance_px": "max(1,0.05*body_height_px)"},
            "errors": errors, "pending": pending,
            "scope": "input-aligned native x/body; equal phase MSE; no interpolation, time normalization or ground-truth telemetry"}


def calibration_feedback(refpaths, beforepaths, afterpaths):
    groups = [_reports(paths) for paths in (refpaths, beforepaths, afterpaths)]
    if any(report.get("scenario", {}).get("split") != "calibration" for group in groups for report in group):
        raise ValueError("calibration feedback must not consume held-out observations")
    result = compare_runs(*groups, split="calibration")
    eligible = result["numerical_status"] == "pass" and result["manual_validation_status"] != "fail"
    return {**result, "comparison_status": result["status"],
            "status": "ok" if result["coverage"]["complete_triplets"] == 6 and not any(e.startswith(("invalid", "wrong", "duplicate", "body", "unexpected")) for e in result["errors"]) else "invalid",
            "diagnostic_only": True, "diagnostic_gates_pass": eligible, "eligible": eligible,
            "manual_status": result["manual_validation_status"]}
