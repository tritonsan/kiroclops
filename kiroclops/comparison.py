"""Held-out transfer gates, preserving elapsed time and character-body scale."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np


def _load_many(value):
    if isinstance(value, (str, Path, dict)):
        value = [value]
    return [item if isinstance(item, dict) else json.loads(Path(item).read_text(encoding="utf-8")) for item in value]


def _key(report):
    s = report["scenario"]
    return (s.get("hold_ticks"), s.get("repeat", 1), s.get("spawn_offset", 0), s.get("start_tick", 30))


def _curve(report):
    events = report.get("events", {})
    takeoff, landing = events.get("takeoff_frame"), events.get("landing_frame")
    if takeoff is None or landing is None or takeoff >= landing:
        raise ValueError("missing/invalid observed flight events")
    frames = {f["frame_index"]: f for f in report["frames"]}
    if takeoff not in frames or landing not in frames:
        raise ValueError("event frame absent")
    origin = frames[takeoff]["time_s"]
    selected = [frames[index] for index in sorted(frames) if takeoff <= index <= landing]
    if not all(f["valid"] and f.get("height_body") is not None for f in selected):
        raise ValueError("flight contains uncertain observations; cannot fill with a fitted curve")
    times = np.asarray([f["time_s"]-origin for f in selected], dtype=float)
    values = np.asarray([f["height_body"] for f in selected], dtype=float)
    if not np.isfinite(values).all() or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError("invalid flight curve")
    return times, values


def curve_rmse(reference, candidate):
    """Align takeoff only; compare union duration, grounded after landing.

    Linear interpolation changes sampling locations, never motion duration or
    jump amplitude. A shorter jump remains at ground for the remaining window.
    """
    rt, ry = _curve(reference)
    ct, cy = _curve(candidate)
    if reference.get("fps") != 60 or candidate.get("fps") != 60:
        raise ValueError("comparison requires 60 Hz observations")
    duration = max(rt[-1], ct[-1])
    ticks = np.arange(int(round(duration*60))+1) / 60.
    rvalues = np.interp(ticks, rt, ry, left=0., right=0.)
    cvalues = np.interp(ticks, ct, cy, left=0., right=0.)
    return float(np.sqrt(np.mean((rvalues-cvalues)**2)))


def _manual_validation(reports):
    games = {}
    for report in reports:
        game = report["game"]
        group = games.setdefault(game, {"points": {}, "event_count": 0, "event_failures": 0, "annotation_failures": 0})
        validation = report.get("annotation_validation", {})
        if validation.get("status") == "fail":
            group["annotation_failures"] += 1
        for point in validation.get("points", []):
            if point.get("source") not in {"human", "assistant_visual"}:
                continue
            key = (point["run_id"], point["frame_index"])
            if point.get("error_px") is not None:
                group["points"][key] = (point["error_px"], max(1., .05*report["body_height_px"]))
        for event in validation.get("event_checks", []):
            group["event_count"] += 1
            group["event_failures"] += int(not event.get("pass", False))
    result = {}
    for game, group in games.items():
        points = list(group["points"].values())
        normalized_errors = [error/tolerance for error, tolerance in points]
        median = float(np.median(normalized_errors)) if points else None
        result[game] = {"checked_point_count": len(points), "required_point_count": 20,
                        "median_error_over_tolerance": median,
                        "event_check_count": group["event_count"],
                        "event_failures": group["event_failures"],
                        "annotation_failures": group["annotation_failures"],
                        "status": "fail" if group["event_failures"] or group["annotation_failures"] or (median is not None and median > 1) else (
                            "pass" if len(points) >= 20 and group["event_count"] > 0 else "pending")}
    return result


def compare_runs(reference, before, after):
    """Accept a path/report or lists including calibration evidence per role.

    Only validation-split triplets count toward transfer success. Calibration
    reports may contribute independent manually checked tracking evidence.
    """
    refs, befores, afters = map(_load_many, (reference, before, after))
    all_reports = refs + befores + afters
    if not all_reports:
        raise ValueError("comparison needs run analyses")
    errors, pending, checks = [], [], []
    for label, group in [("reference", refs), ("before", befores), ("after", afters)]:
        if not group:
            errors.append(f"missing {label} analyses")
        for report in group:
            if report.get("variant") != label:
                errors.append(f"wrong variant in {label} group")
            if report.get("status") != "ok" or report.get("valid_ratio", 0) < .95 or report.get("camera_valid") is not True:
                errors.append(f"unreliable tracking: {report.get('run_id')}")
    manual = _manual_validation(all_reports)
    for game, result in manual.items():
        if result["status"] == "fail":
            errors.append(f"independent annotation checks failed for {game}")
        elif result["status"] != "pass":
            pending.append(f"independent annotation checks incomplete for {game}")
    validation_maps = []
    for group in (refs, befores, afters):
        mapping = {}
        for report in group:
            if report.get("scenario", {}).get("split") == "validation":
                key = _key(report)
                if key in mapping:
                    errors.append(f"duplicate validation scenario: {key}")
                mapping[key] = report
        validation_maps.append(mapping)
    rmap, bmap, amap = validation_maps
    if not rmap:
        pending.append("no held-out validation scenarios")
    if set(rmap) != set(bmap) or set(rmap) != set(amap):
        errors.append("validation scenario sets differ")
    planned = {(hold, repeat, offset, 30) for hold in (8, 14)
               for repeat in (1, 2, 3) for offset in (0, 24)}
    observed = set(rmap) & set(bmap) & set(amap)
    missing_scenarios = sorted(planned - observed)
    if missing_scenarios:
        pending.append(f"planned validation matrix incomplete: {len(missing_scenarios)} of 12 triplets missing")
    for key in sorted(set(rmap) & set(bmap) & set(amap)):
        ref, old, new = rmap[key], bmap[key], amap[key]
        try:
            before_rmse, after_rmse = curve_rmse(ref, old), curve_rmse(ref, new)
            improvement = (before_rmse-after_rmse)/before_rmse if before_rmse > 1e-12 else None
            rm, nm = ref["metrics"], new["metrics"]
            if not all(isinstance(rm.get(name), (int, float)) and isinstance(nm.get(name), (int, float))
                       and math.isfinite(rm[name]) and math.isfinite(nm[name]) and rm[name] > 0
                       for name in ("height_body", "rise_s", "fall_s")):
                raise ValueError("invalid measured flight metrics")
            height_error = abs(nm["height_body"]-rm["height_body"])/rm["height_body"]
            rise_error, fall_error = abs(nm["rise_s"]-rm["rise_s"]), abs(nm["fall_s"]-rm["fall_s"])
            gates = {"curve_rmse": after_rmse <= .15,
                     "improvement": improvement is not None and improvement >= .5,
                     "height": height_error <= .10,
                     "rise": rise_error <= max(2/60., .10*rm["rise_s"]),
                     "fall": fall_error <= max(2/60., .10*rm["fall_s"])}
            checks.append({"scenario": ref["scenario"], "before_rmse_body": before_rmse,
                           "after_rmse_body": after_rmse, "improvement_fraction": improvement,
                           "height_relative_error": height_error, "rise_error_s": rise_error,
                           "fall_error_s": fall_error, "gates": gates, "pass": all(gates.values())})
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"validation scenario {key}: {exc}")
    if any(not check["pass"] for check in checks):
        errors.append("one or more held-out transfer gates failed")
    status = "fail" if errors else ("pending" if pending or not checks else "pass")
    return {"schema_version": 1, "status": status, "success": status == "pass",
            "gates": {"valid_ratio_min": .95, "manual_points_per_game": 20,
                      "manual_median_tolerance_px": "max(1, 0.05*body_height_px)",
                      "manual_event_tolerance_frames": 2, "curve_rmse_body_max": .15,
                      "improvement_fraction_min": .5, "height_relative_error_max": .10,
                      "rise_fall_tolerance_s": "max(2/60, 0.10*reference_duration)"},
            "annotation_validation": manual, "validation_scenarios": checks,
            "coverage": {"required_triplets": 12, "complete_triplets": len(planned & observed),
                         "missing": [{"hold_ticks": h, "repeat": r, "spawn_offset": o, "start_tick": s}
                                     for h, r, o, s in missing_scenarios]},
            "errors": errors, "pending": pending,
            "alignment": "observed takeoff only; body scale; actual elapsed time; union flight duration"}
