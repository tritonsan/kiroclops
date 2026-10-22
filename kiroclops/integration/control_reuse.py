"""Operator-approved cross-session reuse of independently observed raw pixels.

Only direct manual observations are exported. Copies keep the original run
identity for independent-control counting; further copies are never relabelled
as new observations. This module writes target annotations only.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path

from . import evaluation
from .storage import (IntegrationError, identifier, project_hashes, read_json,
                      session_path, sha256)

POINT_METHOD = "identical-raw-PNG-cross-session"
EVENT_METHOD = "identical-complete-raw-frame-sequence-cross-session"
EVENT_NAMES = ("takeoff_frame", "apex_frame", "landing_frame")


def _inside(path, base):
    path, base = Path(path).resolve(), Path(base).resolve()
    if path == base or not path.is_relative_to(base):
        raise ValueError("reuse origin is outside its approved session evidence")
    return path


def _context(directory, *, check_projects):
    directory = session_path(directory)
    state = read_json(directory / "session.json")
    selected = state.get("selected_candidate_id")
    final = state.get("final", {})
    if (state.get("status") != "finished" or not state.get("sealed_at")
            or state.get("active_job_id") or not selected
            or final.get("candidate_id") != selected
            or final.get("measurement_status") != "pass"):
        raise IntegrationError("reuse_not_ready", "Reuse requires a finished, sealed, independently reviewed source session")
    candidate_root = directory / "candidates" / identifier(selected)
    candidate_path = candidate_root / "candidate.json"
    candidate = read_json(candidate_path)
    saved = next((item for item in state.get("candidates", []) if item.get("candidate_id") == selected), None)
    if candidate != saved or candidate.get("status") != "finalized":
        raise IntegrationError("experiment_integrity", "Reuse source candidate metadata changed")
    receipt_path = _inside(final.get("manual_review_path", ""), directory / "final")
    receipt = read_json(receipt_path)
    if (receipt.get("candidate_id") != selected or receipt.get("measurement_status") != "pass"
            or not all(receipt.get(key) is True for key in
                       ("measurements_unchanged", "raw_evidence_unchanged", "sources_unchanged"))):
        raise IntegrationError("reuse_not_ready", "Reuse source has no successful independent-review receipt")
    baseline = directory / "baseline"
    if Path(state["baseline"]).resolve() != baseline:
        raise ValueError("reuse baseline is outside its session")
    if check_projects:
        for project, expected in ((baseline, state["baseline_hashes"]),
                                  (candidate_root / "project", candidate["source_hashes"])):
            if not expected or project_hashes(project) != expected:
                raise IntegrationError("experiment_integrity", f"Reuse source snapshot changed: {project}")
    definitions = {
        "reference": (state["reference"], directory / "reference", "tiny", "reference", None),
        "baseline": (state["baseline_runs"], directory / "baseline-runs", "slay", "before", state["baseline_hashes"]),
        "candidate": ({"calibration": candidate["calibration_runs"], "validation": candidate["validation_runs"]},
                      candidate_root / "runs", "slay", "after", candidate["source_hashes"]),
    }
    runs = {}
    for role, (groups, base, game, variant, expected) in definitions.items():
        for split in ("calibration", "validation"):
            for raw in groups[split]:
                run = _inside(raw if Path(raw).is_absolute() else directory / raw, base)
                runs[run] = (role, split, game, variant, expected)
    hashes = {str(path.relative_to(directory)): sha256(path)
              for path in (directory / "session.json", candidate_path, receipt_path)}
    return {"directory": directory, "state": state, "candidate": candidate,
            "runs": runs, "hashes": hashes}


def _slot(manifest):
    scenario = manifest["scenario"]
    return (manifest["game"], manifest["variant"], scenario["split"],
            scenario["hold_ticks"], scenario["spawn_offset"], scenario["start_tick"],
            manifest["fps"], tuple(manifest["viewport"]))


def _origin_bundle(context, run, locked):
    role, split, game, variant, expected = context["runs"][run]
    directory = context["directory"]
    annotation_dir = directory / ("reference/annotations" if role == "reference" else "annotations")
    annotation_path = evaluation.controls_path(run, annotation_dir)
    annotation = read_json(annotation_path)
    report, manifest = read_json(run / "analysis.json"), read_json(run / "manifest.json")
    if (manifest.get("status") != "ok" or manifest.get("game") != game
            or manifest.get("variant") != variant or manifest.get("scenario", {}).get("split") != split
            or report.get("game") != game or report.get("body_height_px") != locked["body_height_px"]
            or evaluation._measurement(annotation) != evaluation._measurement(locked)
            or report.get("provenance", {}).get("annotation_sha256") != sha256(annotation_path)
            or report.get("provenance", {}).get("manifest_sha256") != sha256(run / "manifest.json")):
        raise ValueError("reuse source evidence identity or measurement settings changed")
    evaluation.validate_annotation(annotation, manifest["viewport"])
    if expected is not None and any(manifest.get("provenance", {}).get(key) != expected for key in
                                    ("full_source_hashes_before", "full_source_hashes_after")):
        raise ValueError("reuse source capture differs from its sealed controller snapshot")
    hashes = dict(context["hashes"])
    for path in (annotation_path, run / "analysis.json", run / "manifest.json"):
        hashes[str(path.relative_to(directory))] = sha256(path)
    return {"annotation": annotation, "report": report, "manifest": manifest, "hashes": hashes}


def _verified_direct(control, kind, run, bundle, locked):
    if (control.get("reuse_origin") or control.get("source") not in evaluation.MANUAL_SOURCES
            or control.get("run_id") != str(run)):
        return False
    report = bundle["report"]
    if kind == "point":
        index = control["frame_index"]
        observed = next((frame for frame in report.get("frames", []) if frame.get("frame_index") == index), {})
        tolerance = max(1., .05 * locked["body_height_px"])
        checked = any(point.get("frame_index") == index and point.get("run_id") == str(run)
                      and point.get("source") == control["source"]
                      and isinstance(point.get("error_px"), (int, float)) and point["error_px"] <= tolerance
                      for point in report.get("annotation_validation", {}).get("points", []))
        return (checked and observed.get("valid") is True
                and all(isinstance(observed.get(axis), (int, float)) for axis in ("x", "y"))
                and math.hypot(observed["x"] - control["x"], observed["y"] - control["y"]) <= tolerance
                and control.get("frame_sha256") == sha256(run / "frames" / f"{index:06d}.png"))
    return (report.get("annotation_validation", {}).get("event_status") == "pass"
            and all(isinstance(report.get("events", {}).get(name), int)
                    and abs(report["events"][name] - control[name]) <= 2
                    and control.get("frame_sha256", {}).get(name) == sha256(run / "frames" / f"{control[name]:06d}.png")
                    for name in EVENT_NAMES))


def verify_reused_control(run, control, locked, *, kind, cache=None):
    """Reverify stored provenance on every reanalysis; stale copies are ignored."""
    cache = {} if cache is None else cache
    try:
        run = Path(run).resolve()
        origin = control["reuse_origin"]
        method = POINT_METHOD if kind == "point" else EVENT_METHOD
        if origin.get("method") != method or control.get("run_id") != str(run):
            return False
        directory = session_path(origin["session_dir"])
        if directory not in cache:
            cache[directory] = _context(directory, check_projects=False)
        context = cache[directory]
        source = Path(origin["run_id"]).resolve()
        if source not in context["runs"] or run.is_relative_to(directory):
            return False
        hashes = origin["evidence_sha256"]
        if not hashes or any(sha256(_inside(directory / relative, directory)) != expected
                             for relative, expected in hashes.items()):
            return False
        bundle = _origin_bundle(context, source, locked)
        if hashes != bundle["hashes"] or _slot(bundle["manifest"]) != _slot(read_json(run / "manifest.json")):
            return False
        original = deepcopy(control)
        original.pop("reuse_origin")
        original["run_id"] = str(source)
        collection = bundle["annotation"]["control_points" if kind == "point" else "control_events"]
        if original not in collection or not _verified_direct(original, kind, source, bundle, locked):
            return False
        if kind == "point":
            index = original["frame_index"]
            return (origin.get("frame_index") == index and origin.get("frame_sha256") == original["frame_sha256"]
                    and sha256(run / "frames" / f"{index:06d}.png") == original["frame_sha256"])
        target_sequence = evaluation._raw_sequence(run)
        return (evaluation._raw_sequence(source) == target_sequence
                and origin.get("sequence_sha256") == evaluation._digest(target_sequence))
    except (OSError, ValueError, KeyError, TypeError, IntegrationError):
        return False


def reuse_controls(source_directory, target_groups, annotations):
    """Copy only verified observations whose raw pixels exactly match targets."""
    context = _context(source_directory, check_projects=True)
    source_dir = context["directory"]
    targets = [run for group in target_groups.values() for run in group]
    if any(Path(run).resolve().is_relative_to(source_dir) for run in targets):
        raise IntegrationError("invalid_reuse_source", "Reuse source must be a different finished session")
    summary = {"source_session": str(source_dir), "points": 0, "events": 0,
               "skipped": {"pixel_mismatch": 0, "sequence_mismatch": 0, "unverified_origin": 0},
               "evidence_sha256": dict(context["hashes"])}
    bundles = []
    for run, (role, _, game, _, _) in context["runs"].items():
        locked = evaluation._locked_annotation(game)
        try:
            bundle = _origin_bundle(context, run, locked)
        except (OSError, ValueError):
            summary["skipped"]["unverified_origin"] += 1
            continue
        summary["evidence_sha256"].update(bundle["hashes"])
        bundles.append((run, role, locked, bundle))
    for role, runs in target_groups.items():
        for target in runs:
            target = Path(target).resolve()
            manifest = read_json(target / "manifest.json")
            path, annotation, _ = evaluation._build_annotation(target, annotations[target])
            current = {point["frame_index"]: point for point in annotation["control_points"]}
            events = annotation["control_events"]
            target_sequence = None
            for run, source_role, locked, bundle in bundles:
                if source_role != role or _slot(bundle["manifest"]) != _slot(manifest):
                    continue
                for kind, key in (("point", "control_points"), ("event", "control_events")):
                    for control in bundle["annotation"][key]:
                        if control.get("reuse_origin"):
                            continue  # Canonical reuse already happens independently in each target.
                        if not _verified_direct(control, kind, run, bundle, locked):
                            summary["skipped"]["unverified_origin"] += 1
                            continue
                        origin = {"session_dir": str(source_dir), "run_id": str(run),
                                  "evidence_sha256": bundle["hashes"]}
                        if kind == "point":
                            index = control["frame_index"]
                            if (index >= manifest["frame_count"] or
                                    sha256(target / "frames" / f"{index:06d}.png") != control["frame_sha256"]):
                                summary["skipped"]["pixel_mismatch"] += 1
                                continue
                            origin.update(method=POINT_METHOD, frame_index=index, frame_sha256=control["frame_sha256"])
                            summary["evidence_sha256"][str((run / "frames" / f"{index:06d}.png").relative_to(source_dir))] = control["frame_sha256"]
                            if index in current:
                                if any(current[index][axis] != control[axis] for axis in ("x", "y")):
                                    raise ValueError("conflicting independent coordinates for identical pixels")
                                continue
                        else:
                            if target_sequence is None:
                                target_sequence = evaluation._raw_sequence(target)
                            if evaluation._raw_sequence(run) != target_sequence:
                                summary["skipped"]["sequence_mismatch"] += 1
                                continue
                            origin.update(method=EVENT_METHOD, sequence_sha256=evaluation._digest(target_sequence))
                            summary["evidence_sha256"].update({str((run / "frames" / f"{index:06d}.png").relative_to(source_dir)): value
                                                                for index, value in enumerate(target_sequence)})
                            if events:
                                if any(event[name] != control[name] for event in events for name in EVENT_NAMES):
                                    raise ValueError("conflicting independent events for identical clips")
                                continue
                        copied = deepcopy(control)
                        copied.update(run_id=str(target), reuse_origin=origin)
                        if kind == "point":
                            current[index] = copied
                            summary["points"] += 1
                        else:
                            events = [copied]
                            summary["events"] += 1
            annotation["control_points"] = sorted(current.values(), key=lambda point: point["frame_index"])
            annotation["control_events"] = events
            evaluation.validate_annotation(annotation, manifest["viewport"])
            evaluation._write(path, annotation)
    verify_source_unchanged(summary)
    return summary


def verify_source_unchanged(summary):
    """Read-only end-of-transaction check; never locks or writes the source."""
    directory = session_path(summary["source_session"])
    _context(directory, check_projects=True)
    for relative, expected in summary["evidence_sha256"].items():
        if sha256(_inside(directory / relative, directory)) != expected:
            raise IntegrationError("reuse_source_changed", "Reuse source evidence changed during review")
