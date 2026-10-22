"""Prospective pixel calibration and final review for realistic-behavior-v1.

Pixel extraction is completed before produced coordinates are opened.  A
proposal is never an inspection: the operator must actually view every bound
contact sheet and attest it.  Historical final aids are not imported as passes.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
from pathlib import Path
import re
import statistics
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.visual.package import frame_paths, load_package, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from . import evidence, suite_raw_review as legacy, suite_raw_review_v3 as painted
from . import suite_generic_raw_review as generic
from .session import Session, _ordinary, source_hashes

VERSION = "realistic-independent-pixel-review-v1"
ROLES = ("reference", "original", "before", "selected")
POINTS_PER_OBJECT = 20


def _need(value, message, code="incomplete_review"):
    if not value:
        raise VisualError(code, message)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _code_sources():
    # Close the actual evaluator/review call graph by pinning the small source
    # directories, never by scanning historical artifact trees.
    root = Path(__file__).resolve().parents[2]
    paths = {Path(__file__).resolve()}
    for name in ("kiroclops/local_engine", "kiroclops/visual", "kiroclops/web_local"):
        paths.update((root / name).glob("*.py"))
    for module in (legacy, painted, generic):
        paths.add(Path(module.__file__).resolve())
    for name in ("raw_panel_review_aid", "raw_panel_centroid_aid", "draft_panel_scope_review"):
        paths.add(root / "scripts" / (name + ".py"))
    paths.add(root / "scripts/phase3d_v2_review.py")
    for name in ("raw_svg_sequence_aid", "raw_svg_geometry_aid_v4", "raw_motion_event_proposals_v4"):
        path = root / "artifacts/phase3a/tools" / (name + ".py")
        if path.is_file():
            paths.add(path)
    return {str(path.resolve()): file_sha256(path) for path in sorted(paths)}


def _records(values, domain, phase):
    _need(domain in ("panel", "web_panel", "svg"), "Only local panel/SVG pixel reviews are supported.", "invalid_review")
    _need(isinstance(values, (list, tuple)) and values, "Actual recording rows are required.", "invalid_review")
    result = []
    for index, value in enumerate(values):
        row = legacy._record(value, index)
        row["domain"] = "panel" if domain in ("panel", "web_panel") else "svg"
        split = row.get("split_role", row.get("scenario", {}).get("suite_split", row.get("scenario", {}).get("split")))
        _need(split in ("calibration", "regression", "validation"), "Bind every row to its real suite split.")
        declared_split = row.get("scenario", {}).get("suite_split", row.get("scenario", {}).get("split"))
        qualification = (row.get("purpose") == "measurement_qualification"
            and row.get("qualification_kind") == "static_geometry" and row.get("behavior_fit_eligible") is False
            and declared_split == "qualification" and row.get("scenario", {}).get("recipe") == "six-node-measurement"
            and row.get("scenario", {}).get("initial_node_count") == 6
            and row.get("scenario", {}).get("expected_final_node_count") == 6)
        _need(declared_split is None or declared_split == split or split == "calibration" and qualification,
              "A row cannot relabel held-out images as calibration.", "invalid_review")
        row["split_role"] = split
        if phase == "calibration":
            _need(split == "calibration" and row["role"] in ("reference", "original", "before"),
                  "Premodel review uses the complete reference/original/before calibration only; held-out pixels are excluded.", "calibration_unqualified")
        _need(isinstance(row.get("selection"), dict), "A separately bound public first-image selection is required.", "needs_public_selection")
        _need(_sha(row.get("source_sha256")), "Every recording needs its producer source digest.", "needs_source_binding")
        result.append(row)
    _need(len({r["id"] for r in result}) == len(result), "Recording IDs must be unique.", "invalid_review")
    return result


def _measurement(bundle, inventory):
    """Open produced coordinates only after all records have been extracted."""
    record, manifest = bundle["record"], bundle["manifest"]
    value = record.get("analysis")
    if isinstance(value, (str, Path)):
        path = _ordinary(value)
        inventory[str(path)] = file_sha256(path)
        analysis = read_json(path)
    elif isinstance(value, dict):
        analysis = deepcopy(value)
    else:
        analysis = {"observations": record.get("observations"), "events": record.get("events"),
                    "candidate_sha256": record.get("source_sha256")}
    observed = analysis.get("observations")
    if observed is None:
        observed = record.get("observations")
    if isinstance(observed, (str, Path)):
        path = _ordinary(observed)
        inventory[str(path)] = file_sha256(path)
        observed = read_json(path)
    _need(isinstance(observed, dict), "Actual produced observations are required.")
    duplicate = record.get("observations")
    if isinstance(duplicate, (str, Path)):
        path = _ordinary(duplicate)
        inventory[str(path)] = file_sha256(path)
        duplicate = read_json(path)
    _need(duplicate is None or duplicate == observed, "Inline/index observations disagree with the actual analysis.", "wrong_result")
    _need(observed.get("package_sha256") == manifest["package_sha256"]
          and observed.get("inputs") == manifest["inputs"] and observed.get("image_size") == manifest["image_size"]
          and observed.get("selection") == record["selection"],
          "Observation pixels, input history, image dimensions or selection changed.", "wrong_result")
    _need(analysis.get("candidate_sha256") == record["source_sha256"],
          "Produced analysis belongs to another candidate/source.", "wrong_result")
    recorded_sources = manifest.get("metadata", {}).get("source_hashes")
    _need(isinstance(recorded_sources, dict) and digest(recorded_sources) == record["source_sha256"],
          "Producer source digest is not bound by the pixel package.", "needs_source_binding")
    if record.get("source_project"):
        project = _ordinary(record["source_project"])
        expected = record.get("full_source_hashes", recorded_sources)
        _need(source_hashes(project) == expected, "Producer source bytes changed.", "source_changed")
        for name, sha in expected.items():
            inventory[str(safe_path(project, name))] = sha
    objects = observed.get("objects")
    _need(isinstance(objects, list) and objects and len({o.get("id") for o in objects}) == len(objects),
          "Distinct measured object identities are required.")
    mapping = record.get("object_map") or {o["id"]: o["id"] for o in objects}
    _need(isinstance(mapping, dict) and set(mapping) == {o["id"] for o in objects}
          and len(set(mapping.values())) == len(mapping), "Every selected object must map to one distinct raw glyph.")
    for obj in objects:
        frames = obj.get("frames", [])
        _need(len(frames) == len(manifest["frames"]) and all(
            f.get("index") == frame["index"] and f.get("time_s") == frame["time_s"]
            for f, frame in zip(frames, manifest["frames"])), "Produced frame identities do not match the raw PNGs.", "wrong_result")
    times = [f["time_s"] for f in manifest["frames"]]
    bundle.update(observations=deepcopy(observed), mapping=mapping,
                  active={oid: legacy._active(record["selection"], oid, times) for oid in mapping})
    return bundle


def _point(bundle, object_id, index):
    return bundle["raw"]["rows"][index]["points"].get(bundle["mapping"][object_id], {})


# V3 review corrections, moved verbatim from the pinned V2 diagnostics
# (scripts/phase3d_v2_panel_event_identity_diagnostic.py and
# scripts/phase3d_v2_panel_subframe_window_rule.py).
SUBFRAME_RULE = "panel-subframe-input-window-rule-v1"
SUBFRAME_REASON = "input_window_between_consecutive_images"


def canonical_event_key(event, selection):
    """Normalize either public relative representation without deduplicating IDs."""
    ids = event.get("object_ids")
    _need(isinstance(ids, list) and 1 <= len(ids) <= 2
          and all(isinstance(value, str) and value for value in ids) and len(set(ids)) == len(ids),
          "Motion event IDs must be one/two distinct public identities.", "invalid_relative_event_identity")
    selected = {obj["id"] for obj in selection.get("objects", [])}
    _need(set(ids) <= selected, "Event contains an unselected identity.", "invalid_relative_event_identity")
    parent = event.get("relative_to_object_id")
    if parent is None:
        _need(len(ids) == 1, "A two-object motion event requires its public relative parent.", "invalid_relative_event_identity")
        return event.get("type"), tuple(ids)
    _need(isinstance(parent, str) and parent in selected and parent != ids[0],
          "Relative parent must be a distinct selected identity.", "invalid_relative_event_identity")
    matches = [spec for spec in selection.get("relative_motion", []) if spec.get("object_id") == ids[0]]
    _need(len(matches) == 1 and matches[0].get("relative_to") == parent,
          "Relative parent differs from the unique public child/parent contract.", "invalid_relative_event_identity")
    _need(len(ids) == 1 or ids[1] == parent,
          "The second explicit event identity is not its declared parent.", "invalid_relative_event_identity")
    return event.get("type"), tuple(ids + [parent] if len(ids) == 1 else ids)


def _finite_all(*values):
    return all(type(value) in (int, float) and math.isfinite(value) for value in values)


def _image_interval(frame):
    value = frame.get("time_interval_s", [frame["time_s"], frame["time_s"]])
    if not (isinstance(value, (list, tuple)) and len(value) == 2 and _finite_all(*value)
            and value[0] <= frame["time_s"] <= value[1]):
        return None
    return float(value[0]), float(value[1])


def _input_uncertainty(events, stamp):
    values = [event.get("clock_uncertainty_s") for event in events if event.get("time_s") == stamp]
    if not values or not all(_finite_all(value) and value >= 0 for value in values):
        return None
    return max(values)


def _subframe_window(bundle, object_id, produced, left, right, tolerance):
    """Evidence that an empty active input window lies between two consecutive images, else None."""
    from kiroclops.web_local.specs import TIMING_LIMITS
    manifest = bundle["manifest"]
    frames, events = manifest["frames"], manifest.get("inputs", {}).get("events", [])
    if manifest.get("metadata", {}).get("timing", {}).get("limits") != TIMING_LIMITS:
        return None
    if len(frames) < 3 or len(produced) != len(frames):
        return None
    times = [frame["time_s"] for frame in frames]
    intervals = [value for value in map(_image_interval, frames) if value is not None]
    lead, trail = _input_uncertainty(events, left), _input_uncertainty(events, right)
    if len(intervals) != len(frames) or lead is None or trail is None or not _finite_all(left, right, tolerance):
        return None
    low, high = left - lead, right + trail
    if any(a <= high and b >= low for a, b in intervals):
        return None
    prior = max((index for index, (_, b) in enumerate(intervals) if b < low), default=None)
    following = min((index for index, (a, _) in enumerate(intervals) if a > high), default=None)
    if prior is None or following is None or following != prior + 1:
        return None
    median = statistics.median(b - a for a, b in zip(times, times[1:]))
    duration, bracket = right - left, times[following] - times[prior]
    limit = TIMING_LIMITS["maximum_motion_gap_s"]
    if not (0 <= duration < median and bracket <= limit):
        return None
    images = []
    for index in (prior, following):
        frame, sample = frames[index], produced[index]
        point = _point(bundle, object_id, frame["index"])
        if (sample.get("index") != frame["index"] or sample.get("time_s") != frame["time_s"]
                or sample.get("valid") is not True
                or not _finite_all(point.get("x"), point.get("y"), sample.get("x"), sample.get("y"))
                or not any(a <= frame["time_s"] <= b for a, b in bundle["active"][object_id])):
            return None
        error = math.hypot(point["x"] - sample["x"], point["y"] - sample["y"])
        if error > tolerance + 1e-9:
            return None
        images.append({"index": frame["index"], "time_s": frame["time_s"], "image_sha256": frame.get("sha256"),
                       "raw": [point["x"], point["y"]], "produced": [sample["x"], sample["y"]],
                       "error_px": error})
    return {"rule": SUBFRAME_RULE, "window_duration_s": duration, "recording_median_image_interval_s": median,
            "bracketing_interval_s": bracket, "maximum_bracket_s": limit,
            "input_clock_uncertainty_s": [lead, trail], "bracketing_images": images,
            "response_checked_in_following_input_window": True}

def _position_checks(bundles):
    checks, failures = [], []
    for bundle in bundles:
        record = bundle["record"]
        for obj in bundle["observations"]["objects"]:
            oid, errors, checked, raw_positions = obj["id"], [], [], []
            for frame in obj["frames"]:
                stamp, index = frame["time_s"], frame["index"]
                if not any(a <= stamp <= b for a, b in bundle["active"][oid]):
                    continue
                point = _point(bundle, oid, index)
                raw_valid = all(type(point.get(k)) in (int, float) and math.isfinite(point[k]) for k in ("x", "y"))
                valid = raw_valid and frame.get("valid") is True
                checked.append(valid)
                if valid:
                    error = math.hypot(point["x"] - frame["x"], point["y"] - frame["y"])
                    errors.append(error)
                    raw_positions.append([point["x"], point["y"]])
            travel = math.hypot(*(max(p[i] for p in raw_positions) - min(p[i] for p in raw_positions)
                                 for i in (0, 1))) if raw_positions else 0.
            tolerance = max(1.5, travel * .005)
            ratio = sum(checked) / len(checked) if checked else 0.
            median = statistics.median(errors) if errors else None
            passed = ratio >= .95 and median is not None and median <= tolerance + 1e-9
            row = {"record_id": record["id"], "object_id": oid, "role": record["role"],
                   "valid_ratio": ratio, "median_error_px": median, "tolerance_px": tolerance, "pass": passed}
            checks.append(row)
            if not passed:
                failures.append({"code": "raw_position_unqualified", **row})
            # A short failed response cannot hide inside a long static tail.
            inputs = bundle["manifest"].get("inputs", {}).get("events", [])
            starts = sorted({event["time_s"] for event in inputs})
            if starts:
                ends = starts[1:] + [bundle["manifest"]["frames"][-1]["time_s"]]
                for occurrence, (left, right) in enumerate(zip(starts, ends)):
                    validity, segment_errors = [], []
                    for frame in obj["frames"]:
                        if not left <= frame["time_s"] <= right or not any(a <= frame["time_s"] <= b for a, b in bundle["active"][oid]):
                            continue
                        point = _point(bundle, oid, frame["index"])
                        valid = frame.get("valid") is True and all(type(point.get(k)) in (int, float) and math.isfinite(point[k]) for k in ("x", "y"))
                        validity.append(valid)
                        if valid:
                            segment_errors.append(math.hypot(point["x"]-frame["x"], point["y"]-frame["y"]))
                    ratio = sum(validity)/len(validity) if validity else 0.
                    median = statistics.median(segment_errors) if segment_errors else None
                    if not validity:
                        active_overlap = any(min(right, b) > max(left, a) for a, b in bundle["active"][oid])
                        if not active_overlap:
                            # A node intentionally removed before this input
                            # window has no required pixels in the window.
                            # This is neither a tracked success nor a failure.
                            checks.append({"record_id": record["id"], "object_id": oid, "role": record["role"],
                                "window_id": f"input-{occurrence}", "window_s": [left, right],
                                "status": "not_observable", "observable": False,
                                "reason": "object_not_active_in_input_window", "valid_ratio": None,
                                "median_error_px": None, "tolerance_px": tolerance, "pass": None})
                            continue
                        # V3: a window between two consecutive images cannot hold a
                        # sample; its response is checked in the following window.
                        subframe = _subframe_window(bundle, oid, obj["frames"], left, right, tolerance)
                        if subframe is not None:
                            checks.append({"record_id": record["id"], "object_id": oid, "role": record["role"],
                                "window_id": f"input-{occurrence}", "window_s": [left, right],
                                "status": "not_observable", "observable": False,
                                "reason": SUBFRAME_REASON, "valid_ratio": None,
                                "median_error_px": None, "tolerance_px": tolerance, "pass": None,
                                "subframe_evidence": subframe})
                            continue
                    passed = ratio >= .95 and median is not None and median <= tolerance + 1e-9
                    row = {"record_id": record["id"], "object_id": oid, "role": record["role"],
                           "window_id": f"input-{occurrence}", "window_s": [left, right], "valid_ratio": ratio,
                           "median_error_px": median, "tolerance_px": tolerance, "pass": passed,
                           "observable": True, "status": "pass" if passed else "fail"}
                    if not validity:
                        row["reason"] = "expected_active_interval_unsampled"
                    checks.append(row)
                    if not passed:
                        failures.append({"code": "raw_motion_window_unqualified", **row})
    return checks, failures


def _geometry_checks(bundles, *, final=False):
    """Independently painted body boxes must remain separate from glyphs."""
    checks, failures = [], []
    for bundle in bundles:
        if bundle["record"]["domain"] == "svg":
            svg_checks, svg_failures = _svg_geometry_checks(bundle, require_relationships=final)
            checks.extend(svg_checks)
            failures.extend(svg_failures)
            continue
        for obj in bundle["observations"]["objects"]:
            errors, active_count = [], 0
            for frame in obj["frames"]:
                if not any(a <= frame["time_s"] <= b for a, b in bundle["active"][obj["id"]]):
                    continue
                active_count += 1
                raw = _point(bundle, obj["id"], frame["index"])
                bounds = raw.get("geometry_bounds")
                if frame.get("valid") is True and isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                    measured = [frame.get(k) for k in ("left", "top", "width", "height")]
                    if all(type(v) in (int, float) and math.isfinite(v) for v in measured):
                        errors.append(max(abs(a-b) for a, b in zip(bounds, measured)))
            ratio = len(errors)/active_count if active_count else 0.
            median = statistics.median(errors) if errors else None
            passed = ratio >= .95 and median is not None and median <= 2. + 1e-9
            row = {"record_id": bundle["record"]["id"], "object_id": obj["id"], "valid_ratio": ratio,
                   "median_body_geometry_error_px": median, "tolerance_px": 2., "pass": passed,
                   "geometry_source": "independent_painted_body_contour_and_first_public_roi_padding"}
            checks.append(row)
            if not passed:
                failures.append({"code": "raw_body_geometry_unqualified", **row})
        lookup = {obj["id"]: obj for obj in bundle["observations"]["objects"]}
        for relation in bundle["record"]["selection"].get("relative_motion", []):
            child, parent = relation["object_id"], relation["relative_to"]
            _need(child in lookup and parent in lookup, "Relative glyph identities are not selected.")
            errors, active_count = [], 0
            for a, b in zip(lookup[child]["frames"], lookup[parent]["frames"]):
                if not all(any(left <= a["time_s"] <= right for left, right in bundle["active"][oid]) for oid in (child, parent)):
                    continue
                active_count += 1
                p, q = _point(bundle, child, a["index"]), _point(bundle, parent, a["index"])
                if a.get("valid") is True and b.get("valid") is True and all("x" in r and "y" in r for r in (p, q)):
                    errors.append(math.hypot((p["x"]-q["x"])-(a["x"]-b["x"]), (p["y"]-q["y"])-(a["y"]-b["y"])))
            ratio = len(errors)/active_count if active_count else 0.
            median = statistics.median(errors) if errors else None
            passed = ratio >= .95 and median is not None and median <= 2. + 1e-9
            row = {"record_id": bundle["record"]["id"], "object_ids": [child, parent], "valid_ratio": ratio,
                   "median_relative_anchor_error_px": median, "tolerance_px": 2., "pass": passed}
            checks.append(row)
            if not passed:
                failures.append({"code": "raw_relative_anchor_unqualified", **row})
    return checks, failures


def _svg_raw_rows(bundle):
    """Map actual independently detected cards/strokes to public identities."""
    result = []
    for row in bundle["raw"]["rows"]:
        result.append({**row,
            "points": {oid: deepcopy(row["points"].get(glyph, {})) for oid, glyph in bundle["mapping"].items()},
            "objects": {oid: deepcopy(row.get("objects", {})[glyph]) for oid, glyph in bundle["mapping"].items()
                        if glyph in row.get("objects", {})}})
    return result


def _svg_geometry_checks(bundle, *, require_relationships=True):
    """Check real outer boxes, pair geometry and visible stroke endpoints.

    Original baseline strokes are not consumed by prospective scoring.  Their
    diagnostic exclusion is subject to the complete original consumer proof;
    original card bounds and anchors still undergo independent pixel checks.
    """
    from itertools import combinations
    from .geometry_acceptance import validate_svg_selection
    from kiroclops.visual.events import extract_events
    record, observed = bundle["record"], bundle["observations"]
    selection = record["selection"]
    validate_svg_selection(selection, expected_ids=bundle["mapping"])
    rows = _svg_raw_rows(bundle)
    checks, failures = [], []
    lookup = {obj["id"]: obj for obj in observed["objects"]}
    for oid, obj in lookup.items():
        samples = []
        for frame in obj["frames"]:
            if not any(a <= frame["time_s"] <= b for a, b in bundle["active"][oid]):
                continue
            box = rows[frame["index"]].get("objects", {}).get(oid)
            values = [frame.get(k) for k in ("left", "top", "width", "height")]
            if box and frame.get("valid") is True and all(type(v) in (int, float) and math.isfinite(v) for v in values):
                l, t, r, b = box["bounds"]
                samples.append((frame["time_s"], max(abs(a-b) for a, b in zip([l,t,r-l,b-t], values))))
            else:
                samples.append((frame["time_s"], None))
        windows = [("whole_active_object", -math.inf, math.inf)]
        inputs = sorted({e["time_s"] for e in bundle["manifest"]["inputs"].get("events", [])})
        windows.extend((f"input-{i}", a, b) for i,(a,b) in enumerate(zip(inputs, inputs[1:]+[rows[-1]["time_s"]])))
        for name, left, right in windows:
            support = [error for stamp,error in samples if left <= stamp <= right]
            if not support:
                overlap = any(min(right,b)>max(left,a) for a,b in bundle["active"][oid])
                if name != "whole_active_object" and not overlap:
                    checks.append({"record_id":record["id"],"object_id":oid,"window_id":name,
                        "status":"not_observable","observable":False,"pass":None,
                        "reason":"object_not_active_in_input_window"})
                    continue
            errors = [error for error in support if error is not None]
            ratio = len(errors)/len(support) if support else 0.
            median = statistics.median(errors) if errors else None
            passed = ratio>=.95 and median is not None and median<=2.+1e-9
            value = {"record_id":record["id"],"object_id":oid,"window_id":name,"valid_ratio":ratio,
                "median_outer_box_error_px":median,"tolerance_px":2.,"pass":passed,
                "geometry_source":"independent_raw_painted_svg_card_bounds"}
            checks.append(value)
            if not passed:failures.append({"code":"raw_svg_outer_geometry_unqualified",**value})
    if not require_relationships:
        # Premodel floor qualification certifies pixel measurement only.  It
        # cannot declare graph behavior or consumed events accepted.
        return checks, failures
    # Actual pair measurements consume outer boxes; an icon patch cannot stand
    # in for a card. Missing active geometry remains explicitly unqualified.
    public_contract = record.get("suite_event_contract") or {}
    produced = extract_events(observed, selection, public_contract)
    produced_pairs = {(tuple(sorted(relation["object_ids"])),relation["index"]):relation
                      for relation in produced.get("relations",[]) if relation.get("type")=="object_pair_geometry"}
    for row in rows:
        active = [oid for oid in lookup if any(a<=row["time_s"]<=b for a,b in bundle["active"][oid])]
        for a,b in combinations(sorted(active),2):
            aa,bb = row["objects"].get(a),row["objects"].get(b)
            wanted = produced_pairs.get(((a,b),row["index"]))
            valid = aa is not None and bb is not None and wanted is not None
            actual = None
            if valid:
                actual = generic.pair(*[{"anchor":box["anchor"],"box":[box["bounds"][0],box["bounds"][1],
                    box["bounds"][2]-box["bounds"][0],box["bounds"][3]-box["bounds"][1]]} for box in (aa,bb)])
                valid = all(abs(actual[field]-wanted[field])<=limit+1e-9 for field,limit in (
                    ("anchor_distance_px",2.),("edge_distance_px",2.),("intersection_over_union",.02)))
                valid = valid and actual["visually_overlapping"]==wanted["visually_overlapping"]
            value = {"record_id":record["id"],"type":"object_pair_geometry","object_ids":[a,b],"index":row["index"],
                     "raw":actual,"produced":wanted,"pass":bool(valid)}
            checks.append(value)
            if not valid:failures.append({"code":"raw_svg_pair_geometry_unqualified",**value})
    if record["role"] == "original":
        checks.append({"record_id":record["id"],"type":"connection_attachment",
            "scope":"diagnostic_original_strokes_only","requires_complete_consumer_exclusion_proof":True,
            "observable":False,"pass":None})
        return checks, failures
    connections = {edge["id"]:edge for edge in observed.get("connections",[])}
    for edge in selection["connections"]:
        a,b = edge["from_object"],edge["to_object"]
        measured = connections.get(edge["id"])
        _need(measured is not None and measured.get("from_object")==a and measured.get("to_object")==b,
              "A scored connection has no bound visible endpoints.","incomplete_review")
        indexed = {frame["index"]:frame for frame in measured["frames"]}
        _need(len(indexed)==len(measured["frames"]),"Connection sample identities are duplicated.")
        for row in rows:
            if not all(any(left<=row["time_s"]<=right for left,right in bundle["active"][oid]) for oid in (a,b)):
                continue
            aa,bb=row["objects"].get(a),row["objects"].get(b)
            glyph_a,glyph_b=bundle["mapping"][a],bundle["mapping"][b]
            raw=row.get("connections",{}).get(glyph_a+"-"+glyph_b)
            reverse=False
            if raw is None:
                raw=row.get("connections",{}).get(glyph_b+"-"+glyph_a);reverse=True
            frame=indexed.get(row["index"])
            passed = bool(raw and aa and bb and frame and frame.get("valid") is True
                and frame.get("time_s")==row["time_s"])
            endpoints = distances = differences = occluded = measured_occluded = None
            coordinate_agreement = raw_attached = measured_attached = None
            if passed:
                endpoints=[raw["end"],raw["start"]] if reverse else [raw["start"],raw["end"]]
                occluded=([raw.get("end_occluded") is True,raw.get("start_occluded") is True] if reverse
                          else [raw.get("start_occluded") is True,raw.get("end_occluded") is True])
                distances=[generic._distance(point,box["bounds"]) for point,box in zip(endpoints,(aa,bb))]
                differences=[math.dist(point,frame[key]) for point,key in zip(endpoints,("start","end"))]
                # The independent review confirms the measured attachment, not
                # that a deliberately poor before/reference curve is repaired.
                measured_distances=[generic._distance(frame[key],[node["frames"][row["index"]]["left"],
                    node["frames"][row["index"]]["top"],node["frames"][row["index"]]["left"]+node["frames"][row["index"]]["width"],
                    node["frames"][row["index"]]["top"]+node["frames"][row["index"]]["height"]])
                    for key,node in zip(("start","end"),(lookup[a],lookup[b]))]
                # V3 (user decision 2026-10-09): the frame gate is the attachment
                # decision. Endpoint accuracy is qualified separately by the blind
                # requalification; coordinate agreement is reported, not gated.
                coordinate_agreement = (max(differences)<=2.+1e-9
                    and max(abs(x-y) for x,y in zip(distances,measured_distances))<=2.+1e-9)
                limits=[max(2.,(box["bounds"][2]-box["bounds"][0])*.01) for box in (aa,bb)]
                raw_attached=all(hidden or distance<=limit+1e-9
                                 for distance,limit,hidden in zip(distances,limits,occluded))
                # V5: the ring-aware production reader flags an end whose whole
                # gap to its box is painted over by an occluder. Each such
                # production decision is checked here against the raw reader.
                measured_occluded=[frame.get("start_occluded") is True,frame.get("end_occluded") is True]
                measured_attached=all(hidden or distance<=limit+1e-9
                                      for distance,limit,hidden in zip(measured_distances,limits,measured_occluded))
                passed=raw_attached==measured_attached
            value={"record_id":record["id"],"type":"connection_attachment","connection_id":edge["id"],
                "object_ids":[a,b],"index":row["index"],"raw_endpoints":endpoints,
                "endpoint_distances_px":distances,"endpoint_measurement_error_px":differences,
                "raw_occluded_ends":occluded,"measured_occluded_ends":measured_occluded,
                "coordinate_agreement_2px":coordinate_agreement,
                "raw_attached":raw_attached,"measured_attached":measured_attached,
                "decision_rule":"attachment_decision_v5",
                "pass":bool(passed),"scope":"independent_visible_stroke_ends_and_raw_outer_node_bounds"}
            checks.append(value)
            if not passed:failures.append({"code":"raw_svg_connection_attachment_unqualified",**value})
    return checks, failures


def _controls(bundles):
    """Select pixel-only control points; produced positions never choose them."""
    pools = defaultdict(dict)
    for bundle in bundles:
        for oid in bundle["mapping"]:
            for row in bundle["raw"]["rows"]:
                if not any(a <= row["time_s"] <= b for a, b in bundle["active"][oid]):
                    continue
                point = _point(bundle, oid, row["index"])
                if all(type(point.get(k)) in (int, float) and math.isfinite(point[k]) for k in ("x", "y")):
                    # A repeated static PNG cannot manufacture distinct control points.
                    pools[oid, bundle["record"]["role"]].setdefault(row["image_sha256"], (bundle, row, point))
    selected, failures = [], []
    for (oid, role), pool in sorted(pools.items()):
        values = list(pool.values())
        if len(values) < POINTS_PER_OBJECT:
            failures.append({"code": "insufficient_distinct_control_images", "object_id": oid,
                             "role": role, "distinct_images": len(values), "required": POINTS_PER_OBJECT})
        count = min(POINTS_PER_OBJECT, len(values))
        for index in sorted(set(int(i) for i in np.linspace(0, len(values) - 1, count))) if count else []:
            bundle, row, point = values[index]
            obj = next(o for o in bundle["observations"]["objects"] if o["id"] == oid)
            produced = obj["frames"][row["index"]]
            if produced.get("valid") is not True:
                failures.append({"code": "control_point_not_valid", "object_id": oid, "record_id": bundle["record"]["id"], "index": row["index"]})
                continue
            selected.append({"record_id": bundle["record"]["id"], "object_id": oid, "role": role,
                "split": bundle["record"]["split_role"], "index": row["index"], "frame_sha256": row["image_sha256"],
                "raw": [point["x"], point["y"]], "produced": [produced["x"], produced["y"]],
                "error_px": math.hypot(point["x"] - produced["x"], point["y"] - produced["y"]), "independent": True})
    expected = {(oid, b["record"]["role"]) for b in bundles for oid in b["mapping"]}
    if set(pools) != expected:
        failures.append({"code": "object_without_raw_control_support"})
    return selected, failures


def _sheets(bundles, controls, output, events=()):
    selected = defaultdict(set)
    for point in controls:
        selected[point["record_id"]].add(point["index"])
    for event in events:
        for index in event.get("indices", []) + event.get("support_indices", []):
            for neighbor in (index - 1, index, index + 1):
                selected[event["record_id"]].add(neighbor)
    contacts, sheets = [], []
    for bundle in bundles:
        record, manifest = bundle["record"], bundle["manifest"]
        indices = selected[record["id"]]
        indices.update((0, len(manifest["frames"]) - 1))
        tiles = []
        paths = frame_paths(record["package"], manifest)
        for index in sorted(i for i in indices if 0 <= i < len(paths)):
            frame, path = manifest["frames"][index], paths[index]
            image = read_image(path)
            _need(file_sha256(path) == frame["sha256"], "A contact image changed.", "source_changed")
            row = bundle["raw"]["rows"][index]
            tile = painted._tile(image, frame, record["id"], row["points"]) if record["domain"] == "panel" else generic._tile(image, frame, record["id"], row, "svg")
            tiles.append(tile)
            contacts.append({"record_id": record["id"], "index": index, "image_sha256": frame["sha256"], "source_path": str(path)})
        for start in range(0, len(tiles), 20):
            group = tiles[start:start + 20]
            group.extend(np.full(group[0].shape, 24, np.uint8) for _ in range((-len(group)) % 4))
            sheet = np.vstack([np.hstack(group[i:i + 4]) for i in range(0, len(group), 4)])
            name = f"sheets/{record['id']}-{start // 20:03}.png"
            legacy._save_image(output / name, sheet)
            sheets.append({"path": name, "sha256": file_sha256(output / name)})
    write_json(output / "contact-selection.json", {"controls": controls, "images": contacts,
        "selection_input": "independent_raw_pixels_and_public_visibility", "produced_positions_used_for_selection": False})
    return sheets


def _prepare(records, output, *, domain, phase, context=None, event_contract=None):
    output = _ordinary(output)
    _need(not output.exists(), "Keep every prior proposal unchanged; choose a fresh directory.", "output_exists")
    rows = _records(records, domain, phase)
    _need(not any(output.is_relative_to(r["package"]) or r["package"].is_relative_to(output) for r in rows),
          "Review output and pixel inputs cannot contain one another.", "unsafe_path")
    inventory, bundles = {}, []
    output.mkdir(parents=True)
    for record in rows:
        bundle = painted._extract(record, output, inventory, "control_and_event_v1", "first_palette_mixture_v1") if record["domain"] == "panel" else generic._extract(record, output, inventory)
        bundles.append(bundle)
    write_json(output / "extraction-complete.json", {"all_pixel_extraction_finished_before_measurement_read": True,
        "raw_points_sha256": {b["record"]["id"]: file_sha256(output / "raw" / b["record"]["id"] / "points.json") for b in bundles}})
    for bundle in bundles:
        _measurement(bundle, inventory)
    positions, failures = _position_checks(bundles)
    geometry, geometry_failures = _geometry_checks(bundles, final=phase == "final")
    failures.extend(geometry_failures)
    controls, control_failures = _controls(bundles)
    failures.extend(control_failures)
    event_checks, raw_events, event_relevance = [], [], []
    consumer_proof = None
    if phase == "final":
        event_checks, raw_events, event_relevance, event_failures = (_final_svg_events(bundles, event_contract or {}, context)
            if domain == "svg" else _final_events(bundles, event_contract or {}))
        failures.extend(event_failures)
        consumer_proof = _prove_original_events(context, rows)
        inventory.update(consumer_proof.pop("files"))
        _need(consumer_proof["status"] == "pass", "Original diagnostic event exclusion is not proven against the current complete consumer.")
        from . import suite_evidence
        authority = Path(context["session_directory"])
        state = Session(authority)._load()
        required = suite_evidence.required_bindings(authority, state, require_complete=True)
        actual = {(b["manifest"]["package_sha256"], digest(b["observations"])) for b in bundles}
        _need(actual == required, "The independent matrix omits a production-required observation.")
    sheets = _sheets(bundles, controls, output, raw_events)
    snapshots = [{**r, "package": str(r["package"])} for r in rows]
    write_json(output / "records.json", snapshots)
    result = {"kind": "RealisticIndependentPixelProposal", "schema_version": 2, "version": VERSION,
        "phase": phase, "domain": "panel" if domain in ("panel", "web_panel") else "svg", "status": "pending_visual_review",
        "numeric_status": "pass" if not failures else "fail", "basis": "independent_raw_pixels", "points": controls,
        "positions": positions, "geometry": geometry, "event_checks": event_checks, "event_relevance": event_relevance,
        "original_event_consumer_proof": consumer_proof, "failures": failures, "sheets": sheets,
        "context": deepcopy(context), "records_sha256": digest(snapshots),
        "records": [{"record_id": b["record"]["id"], "role": b["record"]["role"],
                     "case_id": b["record"].get("case_id", b["record"].get("scenario", {}).get("case_id", b["record"].get("scenario", {}).get("id"))),
                     "purpose": b["record"].get("purpose", "primary"),
                     "split_role": b["record"]["split_role"], "source_sha256": b["record"]["source_sha256"],
                     "package_sha256": b["manifest"]["package_sha256"], "observations_sha256": digest(b["observations"]),
                     "selection_sha256": digest(b["record"]["selection"])} for b in bundles],
        "production_coordinates_used_for_extraction": False, "production_events_used_for_extraction": False,
        "target_code_or_dom_used_for_extraction": False, "smoothing_or_interpolation_used": False,
        "historical_acceptance_changed": False, "full_behavior_acceptance": False}
    if phase == "calibration":
        groups = defaultdict(list)
        # Floors are object-wide, so an identical PNG in both roles is counted once.
        seen = set()
        points = []
        for point in controls:
            if point["role"] not in ("reference", "original"):
                continue
            key = point["object_id"], point["frame_sha256"]
            if key not in seen:
                seen.add(key)
                points.append(point)
                groups[point["object_id"]].append(point["error_px"])
        result["points"] = points
        from .realistic_policy import percentile95
        result["floors_px"] = {oid: max(2., percentile95(values)) for oid, values in sorted(groups.items())}
        for oid, values in groups.items():
            if len(values) < 20 or percentile95(values) > 3. + 1e-9:
                failures.append({"code": "calibration_unqualified", "object_id": oid, "point_count": len(values), "p95_error_px": percentile95(values)})
        result["numeric_status"] = "pass" if not failures else "fail"
    write_json(output / "result.json", result)
    generated = {p.relative_to(output).as_posix(): file_sha256(p) for p in output.rglob("*") if p.is_file()}
    receipt = {"kind": "RealisticPixelEvidenceReceipt", "schema_version": 2, "result_sha256": digest(result),
        "source_files": inventory, "code_sources": _code_sources(), "generated_files": generated,
        "runtime": {"opencv": cv2.__version__, "numpy": np.__version__,
                    "opencv_build_sha256": digest(cv2.getBuildInformation())}}
    write_json(output / "evidence-receipt.json", receipt)
    return {"status": result["status"], "numeric_status": result["numeric_status"], "output": str(output),
            "sheet_count": len(sheets), "point_count": len(result["points"]), "failures": failures,
            "floors_px": result.get("floors_px"), "full_behavior_acceptance": False}


def prepare_calibration_review(records, output, *, domain="panel"):
    return _prepare(records, output, domain=domain, phase="calibration")


def validate_review(output, *, require_attested=True):
    output = _ordinary(output)
    result, receipt = read_json(output / "result.json"), read_json(output / "evidence-receipt.json")
    _need(result.get("kind") == "RealisticIndependentPixelProposal" and result.get("schema_version") == 2
          and result.get("version") == VERSION and digest(result) == receipt.get("result_sha256"), "Review proposal identity changed.", "stale_review")
    _need(receipt.get("code_sources") == _code_sources(), "Review/evaluator source closure changed.", "source_changed")
    for collection, root in ((receipt["source_files"], None), (receipt["code_sources"], None), (receipt["generated_files"], output)):
        for name, sha in collection.items():
            path = safe_path(root, name) if root else _ordinary(name)
            _need(file_sha256(path) == sha, "Bound image, source, or generated proposal changed.", "source_changed")
    _need(receipt.get("runtime") == {"opencv": cv2.__version__, "numpy": np.__version__,
                                    "opencv_build_sha256": digest(cv2.getBuildInformation())}, "Independent review runtime changed.", "source_changed")
    if require_attested:
        attestation = read_json(output / "attestation.json")
        _need(attestation.get("kind") == "RealisticPixelInspectionAttestation" and attestation.get("accepted") is True
              and attestation.get("images_inspected") is True and attestation.get("status") == "pass"
              and attestation.get("result_sha256") == digest(result)
              and attestation.get("evidence_receipt_sha256") == file_sha256(output / "evidence-receipt.json")
              and attestation.get("inspected_sheets") == result["sheets"]
              and result.get("numeric_status") == "pass", "A complete, genuinely inspected passing attestation is required.")
    return result


def attest_review(output, *, reviewer, inspected_sheets, accepted, images_inspected):
    output = _ordinary(output)
    _need(not (output / "attestation.json").exists(), "Prior attestation cannot be replaced.", "output_exists")
    result = validate_review(output, require_attested=False)
    _need(isinstance(reviewer, str) and reviewer.strip() and type(accepted) is bool and type(images_inspected) is bool,
          "Explicit reviewer and inspection decision are required.", "invalid_review")
    _need(images_inspected is True and inspected_sheets == result["sheets"],
          "Every bound contact sheet must actually be inspected; partial or hash-substituted views are insufficient.")
    _need(not accepted or result["numeric_status"] == "pass", "Numeric failures cannot be overridden by visual attestation.")
    value = {"kind": "RealisticPixelInspectionAttestation", "schema_version": 2, "reviewer": reviewer,
        "accepted": accepted, "images_inspected": images_inspected, "status": "pass" if accepted else "fail",
        "result_sha256": digest(result), "evidence_receipt_sha256": file_sha256(output / "evidence-receipt.json"),
        "inspected_sheets": deepcopy(inspected_sheets), "external_human_review": False, "full_behavior_acceptance": False}
    write_json(output / "attestation.json", value)
    return value


def calibration_review(output):
    value = validate_review(output)
    _need(value["phase"] == "calibration", "Final or held-out pixels cannot supply the calibration floor.", "calibration_unqualified")
    return {"status": "pass", "basis": "independent_raw_pixels", "points": value["points"],
        "proposal_sha256": digest(value), "attestation_sha256": file_sha256(Path(output) / "attestation.json"),
        "source_closure_sha256": digest(read_json(Path(output) / "evidence-receipt.json")["code_sources"])}


def _session_context(directory):
    from . import media_suite, suite_evidence
    directory = _ordinary(directory)
    state = Session(directory)._load()
    stage = state.get("experiment", {}).get("stage")
    domain = "web_panel" if stage == "panel" else "svg" if stage == "svg" else None
    _need(domain is not None and state.get("domain") in (("web_panel", "panel") if stage == "panel" else ("svg",))
          and state["experiment"].get("reference_mode") == "media_suite", "A canonical panel/SVG reference-suite Session is required.")
    suite_directory = _ordinary(state["reference"])
    suite = media_suite.load_suite(suite_directory)
    _need(suite.get("schema_version") == 2 and suite.get("domain") == domain and suite.get("stage") == stage
          and suite.get("acceptance_policy", {}).get("domain") == stage,
          "Only the corresponding prospective v2 panel/SVG suite can use this bridge.")
    selected, layout = suite_evidence._matrix(suite, state)
    _need(selected is not None and state.get("sealed_at") is not None and state.get("final", {}).get("status") == "succeeded",
          "A completed selected/sealed final matrix is required.", "source_not_sealed")
    sealed = directory / "candidates" / media_suite._name(selected["candidate_id"]) / "source"
    hashes = source_hashes(sealed)
    _need(digest(hashes) == selected["source_sha256"] and digest(suite) == state["experiment"]["suite_sha256"], "Selected source or suite changed.", "source_changed")
    projects = {"original": suite_directory / suite["original"], "before": _ordinary(state["baseline"]), "selected": sealed}
    full = {"original": suite["original_source_hashes"], "before": suite["authoring_parent_source_hashes"], "selected": hashes}
    rows = []
    for job_id, capture_role, cases in layout:
        role = "selected" if capture_role in ("candidate", "final") else capture_role
        for record, case in suite_evidence._records(directory / "measurements" / job_id / (capture_role + ".json"), cases, complete=True):
            row = {"id": f"{role}-{len(rows):03}-{digest(case['case_id'])[:10]}", "record": deepcopy(record),
                "case_id": case["case_id"], "split_role": case["role"], "role": role, "domain": stage,
                "reference": role == "reference", "selection": deepcopy(record["selection"]),
                "object_map": deepcopy(case.get("object_map") or {})}
            if role in projects:
                row.update(source_project=str(projects[role]), full_source_hashes=full[role])
            rows.append(row)
    expected = {(role, case["case_id"]) for role in ROLES for case in suite["cases"]}
    _need(len(rows) == len(expected) and {(r["role"], r["case_id"]) for r in rows} == expected,
          "Every prospective calibration/regression/validation case needs the exact four-role raw matrix.")
    return {"session_directory": str(directory), "suite_directory": str(suite_directory), "suite_sha256": digest(suite),
        "candidate_sha256": selected["source_sha256"], "selected_candidate_id": selected["candidate_id"],
        "cases": deepcopy(suite["cases"]), "stage": stage, "domain": domain,
        "acceptance_policy": deepcopy(suite["acceptance_policy"]),
        "evaluation_job_id": selected["job_id"], "final_job_id": state["final_job_id"],
        "event_contract": deepcopy(suite.get("event_contract")), "full_behavior_acceptance": False}, rows


class _EventReadGuard(dict):
    """Any original stored EventSet access from the scoring call is a defect."""
    def __getitem__(self, key):
        if key == "events":
            raise VisualError("event_dependency_unclosed", "The current decision consumer reads original stored events.")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == "events":
            raise VisualError("event_dependency_unclosed", "The current decision consumer reads original stored events.")
        return super().get(key, default)

    def setdefault(self, key, default=None):
        if key == "events":
            raise VisualError("event_dependency_unclosed", "The current decision consumer reads original stored events.")
        return super().setdefault(key, default)


def _prove_original_events(context, rows):
    from . import media_suite
    from kiroclops.visual import events as events_module, comparison as comparison_module
    _need(isinstance(context, dict) and context.get("session_directory"), "The full final consumer needs an authoritative Session.")
    suite = read_json(Path(context["suite_directory"]) / "suite.json")
    stage = context.get("stage")
    _need(stage in ("panel", "svg") and suite.get("stage") == stage
          and suite.get("domain") == ("web_panel" if stage == "panel" else "svg")
          and suite.get("schema_version") == 2 and suite.get("acceptance_policy") == context["acceptance_policy"]
          and context["acceptance_policy"].get("domain") == stage,
          "Event relevance belongs to a different prospective policy.")
    groups = defaultdict(dict)
    for row in rows:
        _need(row.get("domain", stage) in (("panel", "web_panel") if stage == "panel" else ("svg",)),
              "The final consumer contains another visual domain.", "wrong_result")
        _need(row["role"] not in groups[row["case_id"]], "The consumer matrix duplicates a role.")
        groups[row["case_id"]][row["role"]] = row
    outputs, interventions, traces, files = [], [], [], {}
    saved_comparisons, saved_preservation = {}, {}
    for job_id in (context["evaluation_job_id"], context["final_job_id"]):
        directory = Path(context["session_directory"]) / "measurements" / job_id
        for name, destination in (("comparison.json", saved_comparisons), ("parent-preservation.json", saved_preservation)):
            path = directory / name
            files[str(path)] = file_sha256(path)
            for result in read_json(path):
                _need(result["case_id"] not in destination, "Saved consumer output contains duplicate cases.")
                destination[result["case_id"]] = result
        for name in ("job.json", "binding.json", "result.json"):
            path = Path(context["session_directory"]) / "jobs" / job_id / name
            files[str(path)] = file_sha256(path)
    expected_cases = {case["case_id"] for case in context["cases"]}
    _need(set(saved_comparisons) == set(saved_preservation) == expected_cases,
          "Complete actual evaluation and final consumer outputs are required.")
    _need(set(groups) == expected_cases and all(set(group) == set(ROLES) for group in groups.values()),
          "The final consumer requires all four bound roles for every case.")
    actual_event_provider = events_module.extract_events
    for case in context["cases"]:
        records = groups[case["case_id"]]
        original = records["original"]
        before = records["before"]
        ref, selected = records["reference"], records["selected"]
        def compare(orig):
            result = media_suite._compare(ref, orig, selected, stage, require_improvement=case.get("purpose", "primary") == "primary", acceptance_policy=context["acceptance_policy"])
            result.update(case_id=case["case_id"], scenario_id=case["case_id"], role=case["role"], purpose=case.get("purpose", "primary"))
            # Match the actual v2 production preservation branch exactly.
            preservation = deepcopy(result)
            preservation["preservation_reference_basis"] = "primary_reference_quality"
            preservation.update(case_id=case["case_id"], role=case["role"], purpose="authoring_parent_preservation")
            parent = media_suite._compare(ref, orig, before, stage, require_improvement=False, acceptance_policy=context["acceptance_policy"])
            preservation["parent_reference_acceptance"] = parent
            preservation["previously_accepted_case"] = parent.get("pass") is True
            preservation["retained_reference_behavior"] = not preservation["previously_accepted_case"] or result.get("pass") is True
            return {"comparison": result, "parent_preservation": preservation, "parent_reference_acceptance": parent}
        provider_calls = []
        original_observation = original["observations"]
        def provider(observed, *args, **kwargs):
            _need(observed is not original_observation, "Original baseline observations enter the scored event provider.", "event_dependency_unclosed")
            ids = {id(row["observations"]): role for role, row in records.items()}
            role = ids.get(id(observed))
            _need(role in ("reference", "before", "selected"), "A foreign observation entered the decision event provider.", "event_dependency_unclosed")
            provider_calls.append(role)
            return actual_event_provider(observed, *args, **kwargs)
        with patch.object(events_module, "extract_events", provider), patch.object(comparison_module, "extract_events", provider):
            baseline = compare(_EventReadGuard(original))
        _need(baseline["comparison"] == saved_comparisons[case["case_id"]]
              and baseline["parent_preservation"] == saved_preservation[case["case_id"]],
              "Replayed complete decision differs from saved actual consumer output.", "wrong_result")
        _need(provider_calls and set(provider_calls) == {"reference", "before", "selected"},
              "All live scored event provider roles must be exercised; zero calls are not proof.", "event_dependency_unclosed")
        traces.append({"case_id": case["case_id"], "provider_roles": provider_calls,
                       "original_provider_calls": 0, "saved_output_equal": True})
        outputs.append({"case_id": case["case_id"], "sha256": digest(baseline)})
        for treatment in ("absent", "shifted", "unresolved"):
            changed = deepcopy(original)
            if treatment == "absent":
                changed.pop("events", None)
            else:
                ev = deepcopy(changed.get("events", {}))
                for event in ev.get("events", []):
                    if treatment == "shifted":
                        event["time_interval_s"] = [n + 9. for n in event.get("time_interval_s", [0., 0.])]
                    else:
                        event["timing_status"] = "unresolved"
                changed["events"] = ev
            trial_calls = []
            def trial_provider(observed, *args, **kwargs):
                _need(observed is not changed["observations"], "Original events derived inside an intervention.", "event_dependency_unclosed")
                allowed = {id(row["observations"]): role for role, row in records.items() if role != "original"}
                _need(id(observed) in allowed, "Unknown provider role during intervention.", "event_dependency_unclosed")
                trial_calls.append(allowed[id(observed)])
                return actual_event_provider(observed, *args, **kwargs)
            with patch.object(events_module, "extract_events", trial_provider), patch.object(comparison_module, "extract_events", trial_provider):
                actual = compare(changed)
            _need(actual == baseline and trial_calls == provider_calls,
                  "Original stored events alter an actual output/provider read path.", "event_dependency_unclosed")
            interventions.append({"case_id": case["case_id"], "treatment": treatment, "identical": True})
    source = _code_sources()
    files[str(Path(context["suite_directory"]) / "suite.json")] = file_sha256(Path(context["suite_directory"]) / "suite.json")
    return {"kind": "CompleteProspectiveVisualEventReadProof", "stage": stage, "status": "pass", "cases": outputs,
        "interventions": interventions, "provider_traces": traces,
        "complete_scoring_branches": ["comparison", "parent_preservation", "parent_reference_acceptance"],
        "original_stored_events_read": False, "source_closure_sha256": digest(source), "files": files,
        "historical_v3_json_proof_reused": False}


def _final_events(bundles, contract):
    """Only live measurement roles enter the declared event accuracy gate.

    Original stored events are diagnostic in the new consumer, but their
    exclusion is conditional on _prove_original_events, never on a role label.
    """
    from .suite_raw_review_v2 import candidates_v3
    options = contract.get("event_options", contract)
    checks, all_raw, relevance, failures = [], [], [], []
    for bundle in bundles:
        role = bundle["record"]["role"]
        if role == "original":
            relevance.append({"record_id": bundle["record"]["id"], "role": role,
                              "scope": "pixel_geometry_only", "event_relevance": "diagnostic_proven_by_complete_consumer"})
            continue
        record = bundle["record"]
        public = record["selection"]
        groups = [(oid,) for oid in bundle["mapping"]]
        groups.extend((r["object_id"], r["relative_to"]) for r in public.get("relative_motion", []))
        raw_rows = [{**row, "points": {oid: deepcopy(row["points"].get(glyph, {})) for oid, glyph in bundle["mapping"].items()}}
                    for row in bundle["raw"]["rows"]]
        predicted = []
        for group in groups:
            predicted.extend(candidates_v3(raw_rows, list(group), options.get("rest_duration_s", .2),
                active_intervals_s=bundle["active"], max_gap_s=options.get("max_gap_s", .12),
                rest_radius_px=options.get("rest_radius_px", 1.5), speed_threshold_px_s=options.get("speed_threshold_px_s", 5.),
                overshoot_min_px=options.get("overshoot_min_px", 1.5), axis=options.get("axis", "auto"),
                inputs=bundle["manifest"]["inputs"].get("events", []), policy=options.get("motion_policy", "observed-confirmed-rest-departure-v3")))
        frames = {frame["index"]: frame for frame in bundle["manifest"]["frames"]}
        for event in predicted:
            a, b = [frames[index] for index in event["indices"]]
            event["observed_interval_s"] = [a.get("time_interval_s", [a["time_s"], a["time_s"]])[0],
                                            b.get("time_interval_s", [b["time_s"], b["time_s"]])[1]]
        # The actual scorer derives events from the live ObservationSet and
        # this shared public policy, so stale stored event files cannot qualify it.
        from kiroclops.visual.events import extract_events
        produced = extract_events(bundle["observations"], public, contract)
        consumed = {"motion_start", "settled", "overshoot"}
        pools = defaultdict(list)
        for event in predicted:
            event = {**event, "record_id": record["id"]}
            all_raw.append(event)
            if event["type"] in consumed:
                pools[event["type"], tuple(event["object_ids"])].append(event)
        wanted = defaultdict(list)
        for event in produced.get("events", []):
            if event.get("type") in consumed:
                # V3: produced relative events already carry [child, parent];
                # appending the parent again split one group into two keys.
                wanted[canonical_event_key(event, public)].append(event)
        cadence = [b["time_s"] - a["time_s"] for a, b in zip(bundle["manifest"]["frames"], bundle["manifest"]["frames"][1:])]
        tolerance = min(.1, max(.05, 1.5 * statistics.median(cadence))) if cadence else .05
        for key in sorted(set(pools) | set(wanted)):
            raw, actual = pools[key], wanted[key]
            if len(raw) != len(actual):
                failures.append({"code": "raw_consumed_event_count_mismatch", "record_id": record["id"],
                                 "event_type": key[0], "objects": list(key[1]), "raw_count": len(raw), "produced_count": len(actual)})
            for index, (a, b) in enumerate(zip(raw, actual)):
                interval = a["observed_interval_s"]
                target = b["time_interval_s"]
                difference = max(abs(x-y) for x, y in zip(interval, target))
                uncertainty = max(interval[1]-interval[0], target[1]-target[0], b.get("uncertainty_s", 0.))
                passed = difference <= tolerance + 1e-9 and uncertainty <= .1 + 1e-9 and b.get("timing_status", "resolved") == "resolved"
                row = {"record_id": record["id"], "event_type": key[0], "objects": list(key[1]), "occurrence": index,
                    "raw_interval_s": interval, "produced_interval_s": target, "difference_s": difference,
                    "uncertainty_s": uncertainty, "tolerance_s": tolerance, "pass": passed}
                checks.append(row)
                if not passed:
                    failures.append({"code": "raw_consumed_event_disagreement", **row})
        from kiroclops.visual.raw_event_review import relative_timing
        for relation in produced.get("relations", []):
            if relation.get("type") != "relative_event_timing" or relation.get("event_type") != "motion_start" or relation.get("relative_to_object_id") is None:
                continue
            parent = relation["relative_to_object_id"]
            children = relation["object_ids"]
            occurrence = relation.get("occurrence", 0)
            _need(len(children) == 2 and type(occurrence) is int and occurrence >= 0, "Relative onset identity is malformed.")
            support = [pools["motion_start", (oid, parent)] for oid in children]
            raw = relative_timing(*(group[occurrence]["observed_interval_s"] for group in support), cadence) if all(len(group) > occurrence for group in support) else None
            target = relation["lag_interval_s"]
            difference = max(abs(a-b) for a, b in zip(raw["lag_interval_s"], target)) if raw else None
            uncertainty = max(raw.get("uncertainty_s", 0.), relation.get("uncertainty_s", 0.)) if raw else None
            passed = (raw is not None and difference <= tolerance + 1e-9 and uncertainty <= .1 + 1e-9
                      and raw.get("timing_status") == relation.get("timing_status") == "resolved"
                      and raw.get("order") == relation.get("order"))
            row = {"record_id": record["id"], "relation_type": "relative_event_timing", "objects": children,
                "relative_to_object_id": parent, "occurrence": occurrence, "raw": raw,
                "produced": relation, "difference_s": difference, "uncertainty_s": uncertainty,
                "tolerance_s": tolerance, "pass": bool(passed)}
            checks.append(row)
            if not passed:
                failures.append({"code": "raw_consumed_relative_order_disagreement", **row})
        relevance.append({"record_id": record["id"], "role": role, "scope": "raw_positions_geometry_and_consumed_events",
                          "consumed_types": sorted(consumed), "event_contract_sha256": digest(contract)})
    return checks, all_raw, relevance, failures


def _svg_raw_record(bundle):
    """Construct observations entirely from independent painted pixels.

    Only public identities/lifetimes and the recorder's time/input manifest
    are reused. Missing glyphs, cards or strokes remain invalid samples.
    """
    rows = _svg_raw_rows(bundle)
    objects = []
    for oid in bundle["mapping"]:
        frames = []
        for row in rows:
            point = row["points"].get(oid, {})
            box = row["objects"].get(oid)
            active = any(a <= row["time_s"] <= b for a,b in bundle["active"][oid])
            valid = bool(active and box and all(type(point.get(k)) in (int,float) and math.isfinite(point[k]) for k in ("x","y")))
            sample = {"index":row["index"], "time_s":row["time_s"], "valid":valid,
                "present":valid, "expected_active":active, "geometry_method":"contour"}
            if valid:
                l,t,r,b = box["bounds"]
                sample.update(x=point["x"], y=point["y"], left=l, top=t, width=r-l, height=b-t)
            frames.append(sample)
        objects.append({"id":oid, "method":"contour", "frames":frames,
                        "active_intervals_s":deepcopy(bundle["active"][oid])})
    connections = []
    for edge in bundle["record"]["selection"]["connections"]:
        a,b = edge["from_object"], edge["to_object"]
        ga,gb = bundle["mapping"][a], bundle["mapping"][b]
        frames = []
        for row in rows:
            stroke = row.get("connections", {}).get(ga+"-"+gb)
            reverse = False
            if stroke is None:
                stroke = row.get("connections", {}).get(gb+"-"+ga); reverse=True
            sample = {"index":row["index"], "time_s":row["time_s"], "valid":bool(stroke)}
            if stroke:
                sample.update(start=deepcopy(stroke["end"] if reverse else stroke["start"]),
                              end=deepcopy(stroke["start"] if reverse else stroke["end"]),
                              start_occluded=stroke.get("end_occluded" if reverse else "start_occluded") is True,
                              end_occluded=stroke.get("start_occluded" if reverse else "end_occluded") is True)
            frames.append(sample)
        connections.append({"id":edge["id"], "from_object":a, "to_object":b, "frames":frames})
    manifest = bundle["manifest"]
    observed = {"kind":"ObservationSet", "schema_version":1, "status":"ok", "measurement_status":"pass",
        "package_sha256":manifest.get("package_sha256"), "image_size":manifest.get("image_size"),
        "inputs":deepcopy(manifest["inputs"]), "selection":deepcopy(bundle["record"]["selection"]),
        "objects":objects, "connections":connections}
    return {**bundle["record"], "observations":observed, "events":None}


def _final_svg_events(bundles, contract, context):
    """Verify the SVG decision's consumed dynamics and geometry from pixels.

    The legacy independent motion detector supplies contact selection and
    diagnostics. SVG scoring consumes coordinate dynamics and attachment
    relations, not its unused motion-start EventSet, so those have explicit
    role-scoped checks. The same frozen predicates are rerun on independently
    extracted coordinates; this is independent input, not a second algorithm.
    """
    from . import suite_web_v2
    from .geometry_acceptance import svg_preservation
    _need(context.get("stage") == "svg" and contract == suite_web_v2.event_contract("svg"),
          "SVG raw review must use the actual prospective SVG event contract.", "wrong_result")
    groups = defaultdict(dict)
    checks, raw_events, relevance, failures = [], [], [], []
    raw_records = {}
    options = contract["event_options"]
    for bundle in bundles:
        record = bundle["record"]
        _need(record["role"] not in groups[record["case_id"]], "Duplicate SVG consumer role.")
        groups[record["case_id"]][record["role"]] = bundle
        raw_records[record["id"]] = _svg_raw_record(bundle)
        if record["role"] == "original":
            relevance.append({"record_id":record["id"], "role":"original", "scope":"pixel_geometry_only",
                "event_relevance":"diagnostic_proven_by_complete_consumer"})
            continue
        predicted, _ = generic._raw_events({**bundle, "rows":_svg_raw_rows(bundle), "selection":record["selection"]}, options)
        raw_events.extend({**event, "record_id":record["id"]} for event in predicted)
        relevance.append({"record_id":record["id"], "role":record["role"],
            "scope":"raw_coordinates_visible_node_bounds_and_connection_endpoints",
            "consumed_relations":["connection_attachment", "final_no_overlap"],
            "consumed_coordinate_metrics":["settling_time", "overshoot"],
            "motion_event_relevance":"contact_selection_diagnostic_only",
            "event_contract_sha256":digest(contract)})
    _need(set(groups) == {case["case_id"] for case in context["cases"]}
          and all(set(group) == set(ROLES) for group in groups.values()), "Incomplete SVG raw decision matrix.")
    for case_id, group in sorted(groups.items()):
        ref = group["reference"]
        raw_ref = raw_records[ref["record"]["id"]]
        measured_ref = {**ref["record"], "observations":ref["observations"]}
        for role in ("before", "selected"):
            target = group[role]
            raw_target = raw_records[target["record"]["id"]]
            measured_target = {**target["record"], "observations":target["observations"]}
            frozen = suite_web_v2.comparison_contract(measured_ref, measured_target, context["acceptance_policy"])
            produced = suite_web_v2.dynamic_checks(measured_ref, measured_target, frozen)
            raw = suite_web_v2.dynamic_checks(raw_ref, raw_target, frozen)
            _need(len(raw) == len(produced), "Independent SVG dynamics omitted a consumed window.", "incomplete_review")
            cadence = [b["time_s"]-a["time_s"] for a,b in zip(target["manifest"]["frames"], target["manifest"]["frames"][1:])]
            time_limit = min(.1, max(.05, 1.5*statistics.median(cadence))) if cadence else .05
            for a,b in zip(raw, produced):
                identity = all(a.get(key) == b.get(key) for key in ("object_id", "window_id", "reference_interval_s", "observable"))
                passed = identity and a.get("gates") == b.get("gates") and a.get("reason") == b.get("reason")
                differences = {}
                if a["observable"] and b["observable"]:
                    for key,limit in (("reference_settling_s",time_limit),("candidate_settling_s",time_limit),
                                      ("reference_overshoot_px",2.),("candidate_overshoot_px",2.)):
                        x,y = a.get(key),b.get(key)
                        difference = abs(x-y) if type(x) in (int,float) and type(y) in (int,float) else None
                        differences[key] = difference
                        passed = passed and (difference is not None and difference <= limit+1e-9 or x is None and y is None)
                row = {"case_id":case_id, "record_id":target["record"]["id"], "role":role,
                    "type":"consumed_coordinate_dynamics", "raw":a, "produced":b,
                    "differences":differences, "time_tolerance_s":time_limit, "pixel_tolerance_px":2.,
                    "contract_sha256":digest(frozen), "method":"same_frozen_predicate_independent_raw_coordinates", "pass":bool(passed)}
                checks.append(row)
                if not passed:failures.append({"code":"raw_svg_consumed_dynamics_disagreement", **row})
            raw_geometry = svg_preservation(raw_ref["observations"], raw_target["observations"], ref["record"]["selection"]["connections"])
            measured_geometry = svg_preservation(ref["observations"], target["observations"], ref["record"]["selection"]["connections"])
            def decisions(value):
                return {key:value[key] for key in ("status","pass","measurement_status","failure_class","observed_preservation_violation")} | {
                    "final_no_overlap":{key:value["final_no_overlap"][key] for key in ("pass","time_s","expected_object_ids")},
                    "pairs":[{key:row.get(key) for key in ("object_ids","pass","reason")} for row in value["final_no_overlap"]["pairs"]],
                    "connections":[{"id":edge["id"], "pass":edge["pass"], "reason":edge.get("reason"),
                        "samples":[{key:sample.get(key) for key in ("index","pass","reason")} for sample in edge["samples"]]}
                        for edge in value["visible_connections"]]}
            passed = decisions(raw_geometry) == decisions(measured_geometry) and raw_geometry["measurement_status"] == "ok"
            row = {"case_id":case_id,"record_id":target["record"]["id"],"role":role,"type":"consumed_svg_relationships",
                "raw":raw_geometry,"produced":measured_geometry,"pass":passed,
                "method":"same_frozen_predicate_independent_raw_outer_bounds_and_visible_endpoints"}
            checks.append(row)
            if not passed:failures.append({"code":"raw_svg_consumed_relationship_disagreement", **row})
    return checks, raw_events, relevance, failures


def prepare_final_review(session_directory, output):
    context, rows = _session_context(session_directory)
    result = _prepare(rows, output, domain=context["stage"], phase="final", context=context, event_contract=context["event_contract"])
    return result


def attach_final_review(output, path, *, session_directory):
    output, path = _ordinary(output), _ordinary(path)
    _need(not path.exists(), "Preserve earlier final review attachments.", "output_exists")
    result = validate_review(output)
    _need(result.get("phase") == "final", "A calibration aid cannot accept final pixels.")
    context, _ = _session_context(session_directory)
    _need(result["context"] == context, "The inspected final review belongs to another suite/source/seal.", "wrong_final_seal")
    receipt = {"kind": "IndependentRealisticVisualReview", "schema_version": 2, "status": "pass",
        "raw_review_directory": str(output), "session_directory": str(_ordinary(session_directory)),
        "candidate_sha256": context["candidate_sha256"], "selected_candidate_id": context["selected_candidate_id"],
        "suite_sha256": context["suite_sha256"], "acceptance_policy_sha256": context["acceptance_policy"]["policy_sha256"],
        "acceptance_policy_document_sha256": digest(context["acceptance_policy"]),
        "raw_result_sha256": digest(result), "attestation_sha256": file_sha256(output / "attestation.json"),
        "evidence_receipt_sha256": file_sha256(output / "evidence-receipt.json"),
        "verified_scopes": {scope: "pass" for scope in ("position", "events", "relationships")},
        "source_code_review_status": "pending", "full_behavior_acceptance": False}
    write_json(path, receipt)
    return validate_final_review(path, session_directory=session_directory)


def validate_final_review(path, *, candidate_sha256=None, session_directory=None):
    path = _ordinary(path)
    receipt = read_json(path)
    _need(receipt.get("kind") == "IndependentRealisticVisualReview" and receipt.get("schema_version") == 2
          and receipt.get("status") == "pass", "A v2 realistic independent receipt is required.")
    authority = _ordinary(session_directory or receipt["session_directory"])
    _need(authority == _ordinary(receipt["session_directory"]), "Final raw review belongs to another Session.", "wrong_final_seal")
    if (path.parent / "session.json").is_file():
        _need(authority == path.parent, "A Session cannot accept another Session's receipt.", "wrong_final_seal")
    context, _ = _session_context(authority)
    result = validate_review(receipt["raw_review_directory"])
    _need(result.get("phase") == "final" and result.get("context") == context and receipt.get("raw_result_sha256") == digest(result)
          and receipt.get("candidate_sha256") == context["candidate_sha256"]
          and receipt.get("selected_candidate_id") == context["selected_candidate_id"]
          and receipt.get("suite_sha256") == context["suite_sha256"]
          and receipt.get("acceptance_policy_sha256") == context["acceptance_policy"]["policy_sha256"]
          and receipt.get("acceptance_policy_document_sha256") == digest(context["acceptance_policy"])
          and (candidate_sha256 is None or candidate_sha256 == context["candidate_sha256"]), "Final candidate/policy/seal binding differs.", "stale_review")
    root = Path(receipt["raw_review_directory"])
    _need(receipt.get("attestation_sha256") == file_sha256(root / "attestation.json")
          and receipt.get("evidence_receipt_sha256") == file_sha256(root / "evidence-receipt.json"), "Final inspection receipt changed.", "stale_review")
    records = read_json(root / "records.json")
    required = []
    for record in records:
        obs = evidence.observations(record)
        if isinstance(record.get("observations"), str):
            obs = read_json(record["observations"])
        required.append([load_package(record["package"], verify="hashes")["package_sha256"], digest(obs)])
    _need(receipt.get("verified_scopes") == {scope: "pass" for scope in ("position", "events", "relationships")}, "Incomplete independent scopes.")
    from .suite_evidence import required_bindings
    current_required = required_bindings(authority, Session(authority)._load(), require_complete=True)
    _need({tuple(pair) for pair in required} == current_required,
          "Fresh native producer/source evidence differs from the independently reviewed matrix.", "stale_review")
    return {**receipt, "required_observations": sorted({tuple(pair) for pair in required}),
            "record_count": len(records), "verification_scope": "independent_realistic_pixels_only",
            "new_model_generation_claimed_by_bridge": False}
