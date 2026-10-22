"""Session evaluation around the immutable Phase 1 measurement implementation.

No previous fitted profile is read. Reference measurements are copied and
reanalyzed; reused manual truth must be tied to identical raw PNG bytes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics

from kiroclops.analysis import analyze_run
from kiroclops.annotation import MANUAL_SOURCES, validate_annotation
from kiroclops.comparison import compare_runs, curve_rmse
from kiroclops.fitting import fit_profile

ROOT = Path(__file__).resolve().parents[2]
MEASUREMENT_FIELDS = ("game", "variant", "scenario", "fps", "body_height_px",
                      "valid_ratio", "camera_valid", "events", "metrics", "frames")
CONTROL_FIELDS = {"control_points", "control_events"}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _measurement(annotation):
    return {key: value for key, value in annotation.items() if key not in CONTROL_FIELDS}


def _locked_annotation(game):
    if game not in ("tiny", "slay"):
        raise ValueError("only the locked tiny/slay experiment is supported")
    frozen = _read(ROOT / "artifacts/frozen_experiment.json")
    for name in ("analysis.py", "annotation.py", "fitting.py", "comparison.py"):
        relative = f"kiroclops/{name}"
        if _sha(ROOT / relative) != frozen["files"].get(relative):
            raise ValueError(f"frozen measurement implementation changed: {relative}")
    record = frozen["annotations"][game]
    annotation = _read(ROOT / record["path"])
    if _digest(_measurement(annotation)) != record["measurement_sha256"]:
        raise ValueError("frozen annotation settings changed")
    if _sha(record["template_frame"]) != record["template_sha256"]:
        raise ValueError("frozen template pixels changed")
    validate_annotation(annotation)
    return annotation


def controls_path(run_dir, annotations_dir):
    run = Path(run_dir).resolve()
    suffix = hashlib.sha256(str(run).encode()).hexdigest()[:12]
    return Path(annotations_dir).resolve() / f"{run.name}-{suffix}.json"


def _raw_sequence(run_dir):
    run = Path(run_dir)
    manifest = _read(run / "manifest.json")
    return [_sha(run / "frames" / f"{index:06d}.png") for index in range(manifest["frame_count"])]


def _allowed_origin(path, split):
    """Calibration must never read held-out reference frames or measurements."""
    path = Path(path).resolve()
    try:
        path.relative_to((ROOT / "artifacts/runs").resolve())
    except ValueError:
        return False
    if split == "calibration" and not any(token in path.name for token in ("-h03-", "-h24-")):
        return False
    return True


def _reuse_controls(run_dir, locked):
    run = Path(run_dir).resolve()
    manifest = _read(run / "manifest.json")
    split = manifest["scenario"]["split"]
    point_map, event_records = {}, []
    source_reports, source_sequences = {}, {}
    target_sequence = None
    target_hashes = {}
    skipped = {"pixel_mismatch": 0, "unverified_origin": 0}

    def source_report(origin):
        key = str(origin)
        if key not in source_reports:
            source_reports[key] = _read(origin / "analysis.json")
        return source_reports[key]

    for point in locked.get("control_points", []):
        origin = Path(point["run_id"])
        if point.get("source") not in MANUAL_SOURCES or not _allowed_origin(origin, split):
            continue
        index = point["frame_index"]
        if index >= manifest["frame_count"]:
            continue
        try:
            actual_origin_hash = _sha(origin / "frames" / f"{index:06d}.png")
            report = source_report(origin)
        except (OSError, ValueError):
            skipped["unverified_origin"] += 1
            continue
        observed = next((frame for frame in report.get("frames", []) if frame.get("frame_index") == index), {})
        verified = any(p.get("frame_index") == index and p.get("run_id") == str(origin.resolve())
                       and p.get("source") == point.get("source")
                       and p.get("error_px") is not None and p["error_px"] <= max(1., .05*locked["body_height_px"])
                       for p in report.get("annotation_validation", {}).get("points", []))
        verified = verified and observed.get("valid") is True and all(
            isinstance(observed.get(axis), (int, float)) for axis in ("x", "y")) and math.hypot(
                observed["x"]-point["x"], observed["y"]-point["y"]) <= max(1., .05*locked["body_height_px"])
        if not verified or actual_origin_hash != point.get("frame_sha256") or report.get("game") != manifest["game"]:
            skipped["unverified_origin"] += 1
            continue
        target_hashes.setdefault(index, _sha(run / "frames" / f"{index:06d}.png"))
        if target_hashes[index] != actual_origin_hash:
            skipped["pixel_mismatch"] += 1
            continue
        copied = deepcopy(point)
        copied["run_id"] = str(run)
        copied["reuse_origin"] = {"run_id": str(origin), "frame_index": index,
                                  "frame_sha256": actual_origin_hash, "method": "identical-raw-PNG"}
        existing = point_map.get(index)
        if existing and (existing["x"], existing["y"]) != (copied["x"], copied["y"]):
            raise ValueError("conflicting independent coordinates for identical pixels")
        point_map[index] = copied

    for event in locked.get("control_events", []):
        origin = Path(event["run_id"])
        if event.get("source") not in MANUAL_SOURCES or not _allowed_origin(origin, split):
            continue
        try:
            report = source_report(origin)
            verified = report.get("annotation_validation", {}).get("event_status") == "pass" and all(
                report.get("events", {}).get(name) is not None and abs(report["events"][name]-event[name]) <= 2
                for name in ("takeoff_frame", "apex_frame", "landing_frame"))
            if not verified or report.get("game") != manifest["game"]:
                continue
            if target_sequence is None:
                target_sequence = _raw_sequence(run)
            key = str(origin)
            if key not in source_sequences:
                source_sequences[key] = _raw_sequence(origin)
            if source_sequences[key] != target_sequence:
                continue
        except (OSError, ValueError):
            continue
        copied = deepcopy(event)
        copied["run_id"] = str(run)
        copied["reuse_origin"] = {"run_id": str(origin), "method": "identical-complete-raw-frame-sequence",
                                  "sequence_sha256": _digest(target_sequence)}
        event_records.append(copied)
    if event_records:
        event_values = {tuple(event[name] for name in ("takeoff_frame", "apex_frame", "landing_frame")) for event in event_records}
        if len(event_values) != 1:
            raise ValueError("conflicting independent events for identical clips")
        event_records = event_records[:1]
    return list(point_map.values()), event_records, skipped


def _build_annotation(run_dir, annotations_dir):
    run = Path(run_dir).resolve()
    manifest = _read(run / "manifest.json")
    locked = _locked_annotation(manifest["game"])
    destination = controls_path(run, annotations_dir)
    points, events, skipped = _reuse_controls(run, locked)
    point_map = {point["frame_index"]: point for point in points}
    cross_session_cache = {}
    if destination.exists():
        existing = _read(destination)
        if _measurement(existing) != _measurement(locked):
            raise ValueError("session annotation changed locked measurement settings")
        for point in existing.get("control_points", []):
            if point.get("reuse_origin"):
                from .control_reuse import POINT_METHOD, verify_reused_control
                if point["reuse_origin"].get("method") == POINT_METHOD:
                    if verify_reused_control(run, point, locked, kind="point", cache=cross_session_cache):
                        previous = point_map.get(point["frame_index"])
                        if previous and any(previous[axis] != point[axis] for axis in ("x", "y")):
                            raise ValueError("conflicting independent coordinates for identical pixels")
                        point_map.setdefault(point["frame_index"], point)
                    else:
                        skipped["unverified_origin"] += 1
                continue  # Canonical reuse was rebuilt; cross-session reuse was reverified.
            if point.get("run_id") != str(run) or point.get("source") not in MANUAL_SOURCES:
                raise ValueError("manual point needs this run's absolute ID and explicit independent source")
            expected = _sha(run / "frames" / f"{point['frame_index']:06d}.png")
            if point.get("frame_sha256") != expected:
                raise ValueError("manual control point has missing/stale raw PNG hash")
            point_map[point["frame_index"]] = point
        direct_events = [event for event in existing.get("control_events", []) if not event.get("reuse_origin")]
        for event in existing.get("control_events", []):
            from .control_reuse import EVENT_METHOD, verify_reused_control
            if event.get("reuse_origin", {}).get("method") != EVENT_METHOD:
                continue
            if verify_reused_control(run, event, locked, kind="event", cache=cross_session_cache):
                if events and any(saved[name] != event[name] for saved in events
                                  for name in ("takeoff_frame", "apex_frame", "landing_frame")):
                    raise ValueError("conflicting independent events for identical clips")
                if not events:
                    events = [event]
            else:
                skipped["unverified_origin"] += 1
        for event in direct_events:
            if event.get("run_id") != str(run) or event.get("source") not in MANUAL_SOURCES:
                raise ValueError("manual event needs this run's absolute ID and explicit independent source")
            for name in ("takeoff_frame", "apex_frame", "landing_frame"):
                if event.get("frame_sha256", {}).get(name) != _sha(run / "frames" / f"{event[name]:06d}.png"):
                    raise ValueError("manual event has missing/stale raw PNG hash")
        if direct_events:
            events = direct_events
    annotation = deepcopy(locked)
    annotation["control_points"] = sorted(point_map.values(), key=lambda point: point["frame_index"])
    annotation["control_events"] = events
    validate_annotation(annotation, manifest["viewport"])
    _write(destination, annotation)
    return destination, annotation, skipped


def _summary(report, analysis_path, annotation_path, annotation, skipped):
    validation = report["annotation_validation"]
    return {"status": report["status"], "analysis_path": str(Path(analysis_path).resolve()),
            "annotation_path": str(annotation_path), "game": report["game"], "variant": report["variant"],
            "scenario": report["scenario"], "valid_ratio": report["valid_ratio"],
            "events": report["events"], "metrics": report["metrics"], "errors": report["errors"],
            "manual_validation": {key: validation[key] for key in
                                  ("status", "point_status", "event_status", "control_point_count", "median_error_px", "tolerance_px")},
            "reuse_summary": {"points": sum(bool(p.get("reuse_origin")) for p in annotation["control_points"]),
                              "events": sum(bool(e.get("reuse_origin")) for e in annotation["control_events"]),
                              "skipped": skipped}}


def analyze_capture(run, annotations_dir, *, output_path=None):
    run = Path(run).resolve()
    annotation_path, annotation, skipped = _build_annotation(run, annotations_dir)
    output = Path(output_path) if output_path else run / "analysis.json"
    report = analyze_run(run, annotation_path, output)
    report["integration_provenance"] = {
        "locked_annotation_measurement_sha256": _digest(_measurement(annotation)),
        "manual_truth_policy": "explicit independent observations or verified identical raw PNG reuse",
        "control_origins": [{"frame_index": p["frame_index"], "source": p["source"],
                             "frame_sha256": p["frame_sha256"], "reuse_origin": p.get("reuse_origin")}
                            for p in annotation["control_points"]],
    }
    _write(output, report)
    return _summary(report, output, annotation_path, annotation, skipped)


def record_manual_controls(run, annotations_dir, points, event_frames=None, *, source="assistant_visual"):
    """Attach explicit pixel observations, never calculated tracking answers.

    points: list of {frame_index,x,y}; event_frames: mapping or triple in the
    order last-ground takeoff, apex, first contact. Call reanalyze_controls next.
    """
    if source not in MANUAL_SOURCES:
        raise ValueError("control source must be human or assistant_visual")
    run = Path(run).resolve()
    annotation_path, annotation, _ = _build_annotation(run, annotations_dir)
    manifest = _read(run / "manifest.json")
    current = {p["frame_index"]: p for p in annotation["control_points"]}
    for point in points:
        index = point["frame_index"]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < manifest["frame_count"]:
            raise ValueError("manual frame index is outside this capture")
        current[index] = {"run_id": str(run), "frame_index": index, "x": point["x"], "y": point["y"],
                          "source": source, "frame_sha256": _sha(run / "frames" / f"{index:06d}.png"),
                          "evidence": point.get("evidence", "explicit session visual annotation")}
    annotation["control_points"] = sorted(current.values(), key=lambda p: p["frame_index"])
    if event_frames is not None:
        names = ("takeoff_frame", "apex_frame", "landing_frame")
        if not isinstance(event_frames, dict):
            if len(event_frames) != 3:
                raise ValueError("event_frames needs takeoff, apex and landing")
            event_frames = dict(zip(names, event_frames))
        if set(names) - set(event_frames):
            raise ValueError("event_frames needs takeoff, apex and landing")
        for name in names:
            if isinstance(event_frames[name], bool) or not isinstance(event_frames[name], int) or not 0 <= event_frames[name] < manifest["frame_count"]:
                raise ValueError("manual event frame is outside this capture")
        annotation["control_events"] = [{"run_id": str(run), "source": source,
                                          **{name: event_frames[name] for name in names},
                                          "frame_sha256": {name: _sha(run / "frames" / f"{event_frames[name]:06d}.png") for name in names},
                                          "evidence": event_frames.get("evidence", "explicit session visual event annotation")}]
    validate_annotation(annotation, manifest["viewport"])
    _write(annotation_path, annotation)
    return {"annotation_path": str(annotation_path), "control_points": len(annotation["control_points"]),
            "control_events": len(annotation["control_events"]), "status": "pending_reanalysis"}


def reanalyze_controls(run, annotations_dir):
    """Recheck annotations while requiring all image-derived quantities unchanged."""
    run = Path(run).resolve()
    original_path = run / "analysis.json"
    original = _read(original_path)
    staged = run / "analysis.manual-review.json"
    try:
        summary = analyze_capture(run, annotations_dir, output_path=staged)
        reviewed = _read(staged)
        if any(original.get(key) != reviewed.get(key) for key in MEASUREMENT_FIELDS):
            raise ValueError("manual reanalysis changed frozen measurements; original analysis preserved")
        staged.replace(original_path)
        summary["analysis_path"] = str(original_path)
        summary["measurements_unchanged"] = True
        return summary
    finally:
        if staged.exists():
            staged.unlink()


def prepare_reference(output_root: Path, *, split="calibration"):
    if split not in ("calibration", "validation"):
        raise ValueError("reference split must be calibration or validation")
    output_root = Path(output_root).resolve()
    source_root = (ROOT / "artifacts/runs").resolve()
    if output_root == source_root or source_root in output_root.parents:
        raise ValueError("reference output must not modify canonical evidence")
    _locked_annotation("tiny")
    frozen = _read(ROOT / "artifacts/frozen_experiment.json")
    holds, offsets = ((3, 24), (0,)) if split == "calibration" else ((8, 14), (0, 24))
    runs, summaries = [], []
    for hold in holds:
        for offset in offsets:
            for repeat in (1, 2, 3):
                name = f"tiny-reference-h{hold:02d}-x{offset}-r{repeat}"
                original, copied = source_root / name, output_root / split / name
                manifest = _read(original / "manifest.json")
                if manifest.get("game") != "tiny" or manifest.get("variant") != "reference" or manifest.get("scenario", {}).get("split") != split:
                    raise ValueError("canonical reference manifest does not match requested split")
                expected_scenario = {"hold_ticks": hold, "spawn_offset": offset, "repeat": repeat, "start_tick": 30}
                if any(manifest["scenario"].get(key) != value for key, value in expected_scenario.items()):
                    raise ValueError("canonical reference scenario differs from its requested slot")
                if split == "calibration":
                    frozen_run = frozen["calibration_runs"][original.relative_to(ROOT).as_posix()]
                    old_analysis = _read(original / "analysis.json")
                    if (_sha(original / "manifest.json") != frozen_run["manifest_sha256"] or
                            _digest({key: old_analysis.get(key) for key in MEASUREMENT_FIELDS}) != frozen_run["measurement_sha256"]):
                        raise ValueError("frozen calibration reference evidence changed")
                source_sequence = _raw_sequence(original)
                origin_record = {"source_run": str(original), "manifest_sha256": _sha(original / "manifest.json"),
                                 "raw_sequence_sha256": _digest(source_sequence)}
                if copied.exists():
                    if not (copied / "copy-origin.json").is_file() or _read(copied / "copy-origin.json") != origin_record:
                        raise ValueError("reference cache origin changed; existing evidence is not overwritten")
                    if _sha(copied / "manifest.json") != origin_record["manifest_sha256"] or _raw_sequence(copied) != source_sequence:
                        raise ValueError("reference cache raw evidence changed")
                else:
                    copied.mkdir(parents=True)
                    shutil.copy2(original / "manifest.json", copied / "manifest.json")
                    shutil.copytree(original / "frames", copied / "frames")
                    for optional in ("video.mp4", "analysis.json"):
                        if (original / optional).is_file():
                            target_name = "origin-analysis.json" if optional == "analysis.json" else optional
                            shutil.copy2(original / optional, copied / target_name)
                    _write(copied / "copy-origin.json", origin_record)
                summaries.append(analyze_capture(copied, output_root / "annotations"))
                if split == "calibration":
                    fresh = _read(copied / "analysis.json")
                    if _digest({key: fresh.get(key) for key in MEASUREMENT_FIELDS}) != frozen_run["measurement_sha256"]:
                        raise ValueError("fresh reference measurements differ from frozen calibration")
                runs.append(str(copied))
    profile, profile_path = None, None
    if split == "calibration":
        profile_path = output_root / "measured-profile.json"
        profile = fit_profile([Path(run) / "analysis.json" for run in runs], profile_path)
    return {"runs": runs, "split": split, "profile": profile,
            "profile_path": str(profile_path) if profile_path else None,
            "annotation": str(output_root / "annotations"), "run_count": len(runs),
            "tracking_status": "ok" if all(s["status"] == "ok" for s in summaries) else "rejected",
            "manual_status": "fail" if any(s["manual_validation"]["status"] == "fail" for s in summaries) else (
                "pass" if all(s["manual_validation"]["status"] == "pass" for s in summaries) else "pending"),
            "measurement_source": "copied raw reference frames, reanalyzed in this session"}


def _reports(values):
    if isinstance(values, (str, Path, dict)):
        values = [values]
    result = []
    for value in values:
        if isinstance(value, dict):
            result.append(_read(value["analysis_path"]) if "analysis_path" in value and "frames" not in value else value)
        else:
            path = Path(value)
            result.append(_read(path / "analysis.json" if path.is_dir() else path))
    return result


def _scenario_key(report):
    s = report["scenario"]
    return (s["hold_ticks"], s.get("spawn_offset", 0), s.get("repeat", 1), s.get("start_tick", 30))


def _tracking_ok(report):
    # A failed human check is still a real failure; never scrub status to pass.
    return report.get("status") == "ok" and report.get("camera_valid") is True and report.get("valid_ratio", 0) >= .95


def _diagnostic_triplet(ref, before, candidate):
    if not all(_tracking_ok(a) for a in (ref, before, candidate)):
        raise ValueError("tracking or independent manual validation rejected this run")
    old_error, error = curve_rmse(ref, before), curve_rmse(ref, candidate)
    ref_metrics, target_metrics = ref["metrics"], candidate["metrics"]
    for name in ("height_body", "rise_s", "fall_s"):
        if not isinstance(ref_metrics.get(name), (int, float)) or ref_metrics[name] <= 0 or not math.isfinite(ref_metrics[name]):
            raise ValueError("invalid reference metrics")
        if not isinstance(target_metrics.get(name), (int, float)) or not math.isfinite(target_metrics[name]):
            raise ValueError("invalid candidate metrics")
    improvement = 1-error/old_error if old_error > 1e-12 else None
    height = abs(target_metrics["height_body"] / ref_metrics["height_body"] - 1)
    rise = abs(target_metrics["rise_s"] - ref_metrics["rise_s"])
    fall = abs(target_metrics["fall_s"] - ref_metrics["fall_s"])
    gates = {"curve_rmse": error <= .15, "improvement": improvement is not None and improvement >= .5,
             "height": height <= .1, "rise": rise <= max(2/60, .1*ref_metrics["rise_s"]),
             "fall": fall <= max(2/60, .1*ref_metrics["fall_s"])}
    return {"scenario": ref["scenario"], "before_rmse_body": old_error, "candidate_rmse_body": error,
            "improvement_fraction": improvement, "height_relative_error": height,
            "rise_error_s": rise, "fall_error_s": fall, "gates": gates, "pass": all(gates.values())}


def calibration_feedback(reference_runs, before_runs, candidate_runs):
    groups = [_reports(values) for values in (reference_runs, before_runs, candidate_runs)]
    errors, checks = [], []
    maps = []
    for role, reports in zip(("reference", "before", "after"), groups):
        if any(a.get("scenario", {}).get("split") != "calibration" for a in reports):
            raise ValueError("calibration feedback must not expose validation runs")
        if any(a.get("variant") != role for a in reports):
            raise ValueError(f"wrong variant in {role} calibration group")
        mapping = {_scenario_key(a): a for a in reports}
        if len(mapping) != len(reports):
            errors.append(f"duplicate {role} calibration scenario")
        maps.append(mapping)
    expected = {(hold, 0, repeat, 30) for hold in (3, 24) for repeat in (1, 2, 3)}
    if any(set(mapping) != expected for mapping in maps):
        errors.append("calibration requires both hold lengths and three repeats in all roles")
    for key in sorted(set(maps[0]) & set(maps[1]) & set(maps[2])):
        try:
            checks.append(_diagnostic_triplet(*(mapping[key] for mapping in maps)))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    return {"status": "invalid" if errors else "ok", "diagnostic_only": True,
            "mean_rmse_body": statistics.mean(c["candidate_rmse_body"] for c in checks) if checks else None,
            "mean_before_rmse_body": statistics.mean(c["before_rmse_body"] for c in checks) if checks else None,
            "diagnostic_gates_pass": len(checks) == 6 and all(c["pass"] for c in checks) and not errors,
            "scenario_checks": checks, "errors": errors,
            "manual_status": "fail" if any(a.get("annotation_validation", {}).get("status") == "fail" for group in groups for a in group) else (
                "pass" if all(groups) and all(a.get("annotation_validation", {}).get("status") == "pass" for group in groups for a in group) else "pending")}


def _role_manual(reports, *, candidate=False):
    points, checked_event_holds, failures = {}, set(), []
    applied_count = 0
    for report in reports:
        validation = report.get("annotation_validation", {})
        origins = {point["frame_index"]: point.get("reuse_origin") for point in
                   report.get("integration_provenance", {}).get("control_origins", [])}
        if validation.get("status") == "fail":
            failures.append(report.get("run_id"))
        for point in validation.get("points", []):
            if point.get("source") in MANUAL_SOURCES and point.get("error_px") is not None:
                applied_count += 1
                origin = origins.get(point["frame_index"]) or point
                key = (origin["run_id"], origin["frame_index"])
                normalized = point["error_px"] / max(1., .05*report["body_height_px"])
                # An identical repeated clip is a valid reuse, but it does not
                # create another independent visual observation for the quota.
                points[key] = max(points.get(key, 0), normalized)
        if report.get("scenario", {}).get("split") == "validation" and validation.get("event_status") == "pass":
            checked_event_holds.add(report["scenario"]["hold_ticks"])
    median = statistics.median(points.values()) if points else None
    passed = len(points) >= 20 and median is not None and median <= 1 and not failures
    if candidate:
        passed = passed and {8, 14} <= checked_event_holds
    return {"status": "fail" if failures or (median is not None and median > 1) else ("pass" if passed else "pending"),
            "checked_point_count": len(points), "required_point_count": 20,
            "applied_point_count": applied_count,
            "median_error_over_tolerance": median, "audited_validation_hold_ticks": sorted(checked_event_holds),
            "required_validation_event_holds": [8, 14] if candidate else [], "failed_runs": failures}


def final_comparison(refs, before, after):
    groups = [_reports(values) for values in (refs, before, after)]
    legacy = compare_runs(*groups)
    # Manual-only rejection must fail the overall decision, but it does not
    # retroactively change the measured motion. Normalize a private copy only
    # for numeric gates; retain the original reports and their manual failures.
    numeric_groups = deepcopy(groups)
    for group in numeric_groups:
        for report in group:
            if (report.get("status") == "rejected" and report.get("errors") == ["independent manual annotation validation failed"]
                    and report.get("camera_valid") is True and report.get("valid_ratio", 0) >= .95
                    and report.get("annotation_validation", {}).get("status") == "fail"):
                report["status"] = "ok"
    numeric_legacy = compare_runs(*numeric_groups)
    numerical_errors = [error for error in numeric_legacy["errors"] if not error.startswith("independent annotation checks failed")]
    numerical_pending = [reason for reason in numeric_legacy["pending"] if not reason.startswith("independent annotation checks incomplete")]
    numerical_status = "fail" if numerical_errors else (
        "pending" if numerical_pending or not legacy["validation_scenarios"] else "pass")
    manual = {role: _role_manual(reports, candidate=role == "candidate")
              for role, reports in zip(("reference", "baseline", "candidate"), groups)}
    manual_status = "fail" if any(e["status"] == "fail" for e in manual.values()) else (
        "pass" if manual["reference"]["status"] == manual["candidate"]["status"] == "pass"
        and all(e["status"] == "pass" for e in legacy["annotation_validation"].values()) else "pending")
    status = "fail" if "fail" in (numerical_status, manual_status) else (
        "pass" if numerical_status == manual_status == "pass" else "pending")
    pending = list(legacy["pending"])
    for role in ("reference", "candidate"):
        if manual[role]["status"] == "pending":
            pending.append(f"independent {role} visual validation incomplete")
    errors = list(legacy["errors"])
    for role, result in manual.items():
        if result["status"] == "fail" and not any("annotation checks failed" in error for error in errors):
            errors.append(f"independent {role} visual validation failed")
    return {**legacy, "status": status, "success": status == "pass", "legacy_status": legacy["status"],
            "errors": errors, "pending": pending,
            "numerical_status": numerical_status, "manual_validation_status": manual_status,
            "numerical_errors": numerical_errors, "numerical_pending": numerical_pending,
            "measurement_status": manual_status, "manual_validation": manual,
            "scope": "frozen 12-scenario numerical gates plus candidate-specific independent visual validation"}
