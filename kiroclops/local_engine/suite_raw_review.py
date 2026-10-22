"""Prospective, unsigned panel pixel aids with an explicit inspection gate.

All packages are marked from pixels before any production measurement is read.
The aid never runs an application, model, production tracker, or event detector.
Its hashes bind an operator-owned experiment; they are not digital signatures.
SVG/game are deliberately reported as unsupported in this first version.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
from pathlib import Path
import re
import statistics

import cv2
import numpy as np

from kiroclops.visual import package as package_code, png as png_code, raw_event_review, schema as schema_code
from kiroclops.visual.package import frame_paths, load_package, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, finite, read_json, safe_path, write_json
from .session import _ordinary
from scripts import raw_panel_review_aid

VERSION = "prospective-panel-raw-review-v1"
MIN_DISTINCT_IMAGES = 20
MAX_UNCERTAINTY_S = .1
PANEL_OBJECTS = {"bookmark", "handle", "star"}
EVENT_TYPES = {"motion_start", "reversal", "stop", "settled", "overshoot"}
CONTACT_POLICIES = {"all_frames", "control_and_event_v1"}


def _public_contract(value):
    supplied = deepcopy(value or {})
    if not isinstance(supplied, dict):
        raise VisualError("invalid_event_definition", "Public event contract must be an object.")
    options = supplied.get("event_options", supplied)
    policy = options.get("motion_policy", options.get("policy", raw_event_review.CONFIRMED))
    rest = finite(options.get("rest_duration_s", .2), "rest duration", minimum=0)
    if policy != raw_event_review.CONFIRMED or abs(rest - .2) > 1e-9:
        raise VisualError("unsupported_event_definition", "Prospective panel review requires v3 and .20s rest support.")
    result = {"motion_policy": policy, "rest_duration_s": rest,
              "max_gap_s": finite(options.get("max_gap_s", .12), "maximum gap", minimum=0),
              "rest_radius_px": finite(options.get("rest_radius_px", 1.5), "rest radius", minimum=0),
              "event_tolerance_s": finite(supplied.get("event_tolerance_s", .05), "frozen review tolerance", minimum=0),
              "max_event_uncertainty_s": MAX_UNCERTAINTY_S}
    if result["max_gap_s"] <= 0 or result["rest_radius_px"] <= 0 or result["event_tolerance_s"] > .1:
        raise VisualError("invalid_event_definition", "Positive gap/radius and at most .1s frozen tolerance are required.")
    if options.get("max_event_uncertainty_s", MAX_UNCERTAINTY_S) != MAX_UNCERTAINTY_S:
        raise VisualError("invalid_event_definition", "The 100ms uncertainty limit cannot be changed.")
    return result


def _record(value, number):
    if not isinstance(value, dict):
        raise VisualError("invalid_review", "Each recording must be an object.")
    nested = value.get("record", value)
    if not isinstance(nested, dict):
        raise VisualError("invalid_review", "A nested recording must be an object.")
    result = {**nested, **{k: v for k, v in value.items() if k != "record"}}
    result["id"] = result.get("id", result.get("case_id", f"record-{number:03}"))
    if not isinstance(result["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", result["id"]):
        raise VisualError("unsafe_path", "Recording IDs must be short plain names.")
    domain = result.get("domain", result.get("scenario", {}).get("domain", "panel"))
    result["domain"] = "panel" if domain in ("panel", "web", "web_panel", "web-panel") else domain
    role = result.get("role", "reference")
    if not isinstance(role, str) or not role.strip() or len(role) > 100:
        raise VisualError("invalid_review", "A nonempty recording role is required.")
    result["role"] = role
    result["reference"] = result.get("reference", role in ("reference", "calibration")) is True
    if "package" not in result:
        raise VisualError("invalid_review", "Actual recording package path is required.")
    result["package"] = _ordinary(Path(result["package"]))
    return result


def _save_image(path, image):
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise VisualError("invalid_frame", "Could not encode a raw review sheet.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded.tobytes())


def _tile(image, frame, record_id, raw):
    """Raw frame plus unsmoothed raw glyph crops; no produced coordinate overlay."""
    tile = np.full((210, 400, 3), 24, np.uint8)
    cv2.putText(tile, f"{record_id} f{frame['index']} {frame['time_s']:.4f}s", (4, 15),
                cv2.FONT_HERSHEY_SIMPLEX, .37, (240, 240, 240), 1)
    height, width = image.shape[:2]
    scale = min(270 / width, 165 / height)
    preview = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                         interpolation=cv2.INTER_AREA)
    tile[22:22 + preview.shape[0], 3:3 + preview.shape[1]] = preview
    for number, name in enumerate(("bookmark", "handle", "star")):
        point = raw[name]
        y = 23 + number * 59
        cv2.putText(tile, name, (278, y + 10), cv2.FONT_HERSHEY_SIMPLEX, .32, (240, 240, 240), 1)
        if "x" not in point:
            cv2.putText(tile, "ambiguous", (278, y + 30), cv2.FONT_HERSHEY_SIMPLEX, .30, (90, 90, 255), 1)
            continue
        x0, y0, w, h = point["color_bounds"]
        crop = image[max(0, y0 - 2):min(height, y0 + h + 2), max(0, x0 - 2):min(width, x0 + w + 2)]
        factor = min(40 / crop.shape[0], 48 / crop.shape[1], 2)
        crop = cv2.resize(crop, (max(1, round(crop.shape[1] * factor)), max(1, round(crop.shape[0] * factor))),
                          interpolation=cv2.INTER_NEAREST)
        tile[y + 14:y + 14 + crop.shape[0], 278:278 + crop.shape[1]] = crop
        cv2.putText(tile, f"{point['x']:.1f},{point['y']:.1f}", (330, y + 31),
                    cv2.FONT_HERSHEY_SIMPLEX, .29, (240, 240, 240), 1)
    return tile


def _extract(record, output, inventory, contact_policy):
    manifest = load_package(record["package"])
    manifest_path = record["package"] / "manifest.json"
    inventory[str(manifest_path)] = file_sha256(manifest_path)
    if record.get("package_sha256") not in (None, manifest["package_sha256"]):
        raise VisualError("wrong_result", "Recording package binding does not match actual pixels.")
    rows, sheets, tiles = [], [], []
    for frame, image_path in zip(manifest["frames"], frame_paths(record["package"], manifest)):
        image = read_image(image_path)
        # This is the complete extraction boundary: only the raw image enters.
        points = raw_panel_review_aid.raw_anchors(image)
        rows.append({"index": frame["index"], "time_s": frame["time_s"],
                     "image_sha256": frame["sha256"], "points": points})
        actual_hash = file_sha256(image_path)
        if actual_hash != frame["sha256"]:
            raise VisualError("source_changed", "Raw image changed during independent extraction.")
        inventory[str(image_path)] = actual_hash
        if contact_policy == "all_frames":
            tiles.append(_tile(image, frame, record["id"], points))
        if len(tiles) == 20:
            name = f"sheets/{record['id']}-{len(sheets):03}.png"
            _save_image(output / name, np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, 20, 4)]))
            sheets.append(name)
            tiles = []
    if tiles:
        tiles.extend(np.full((210, 400, 3), 24, np.uint8) for _ in range((-len(tiles)) % 4))
        name = f"sheets/{record['id']}-{len(sheets):03}.png"
        _save_image(output / name, np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)]))
        sheets.append(name)
    raw = {"kind": "UnsignedIndependentPanelPixelProposals", "status": "unsigned",
           "package_sha256": manifest["package_sha256"], "rows": rows,
           "method": "Independent HSV glyph bounds, without smoothing or interpolation.",
           "production_coordinates_used": False, "production_events_used": False,
           "target_code_or_dom_read": False, "sheets": sheets}
    write_json(output / "raw" / record["id"] / "points.json", raw)
    return {"record": record, "manifest": manifest, "raw": raw, "sheets": sheets}


def _measurement(record, inventory):
    value = record.get("analysis", record.get("measurement"))
    if isinstance(value, (str, Path)):
        path = _ordinary(Path(value))
        inventory[str(path)] = file_sha256(path)
        value = read_json(path)
    elif value is None:
        value = {"observations": record.get("observations"), "events": record.get("events")}
    if not isinstance(value, dict):
        raise VisualError("invalid_review", "An actual analysis path or measurement object is required.")
    observations = value.get("observations")
    if observations is None and value.get("kind") == "ObservationSet":
        observations = value
    if observations is None and isinstance(record.get("analysis"), (str, Path)):
        path = _ordinary(Path(record["analysis"]).parent / "observations.json")
        inventory[str(path)] = file_sha256(path)
        observations = read_json(path)
    events = value.get("events", record.get("events"))
    if events is None and isinstance(record.get("analysis"), (str, Path)):
        path = _ordinary(Path(record["analysis"]).parent / "events.json")
        inventory[str(path)] = file_sha256(path)
        events = read_json(path)
    if not isinstance(observations, dict) or not isinstance(events, dict):
        raise VisualError("invalid_review", "Bound observations and an EventSet are required.")
    # Freeze caller-owned inline values too, rather than comparing a live dict.
    return deepcopy(observations), deepcopy(events)


def _interval(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise VisualError("invalid_event_interval", f"{name} requires two endpoints.")
    a, b = [finite(v, name) for v in value]
    if b < a:
        raise VisualError("invalid_event_interval", f"{name} is reversed.")
    return [a, b]


def _active(selection, object_id, times):
    objects = {o["id"]: o for o in selection.get("objects", [])}
    intervals = objects.get(object_id, {}).get("active_intervals_s", [[times[0], times[-1]]])
    if not isinstance(intervals, list) or not intervals:
        raise VisualError("invalid_review", "Public object visibility intervals are required.")
    return [_interval(i, "active interval") for i in intervals]


def _scope(bundle, observations, events, contract):
    record, manifest, raw = bundle["record"], bundle["manifest"], bundle["raw"]
    if observations.get("package_sha256") != manifest["package_sha256"] or events.get("package_sha256") != manifest["package_sha256"]:
        raise VisualError("wrong_result", "Production measurement belongs to another raw package.")
    if events.get("observation_sha256") not in (None, digest(observations)):
        raise VisualError("wrong_result", "EventSet belongs to another observation set.")
    selection = record.get("selection", observations.get("selection", {}))
    if not isinstance(selection, dict):
        raise VisualError("invalid_review", "Public selection must be a value, not another unbound file.")
    if record.get("selection") is not None and digest(selection) != digest(observations.get("selection", {})):
        raise VisualError("wrong_result", "Public selection differs from the measurement's bound selection.")
    objects = observations.get("objects")
    if not isinstance(objects, list) or not objects or len({o["id"] for o in objects}) != len(objects):
        raise VisualError("invalid_review", "Production object observations are missing.")
    mapping = record.get("object_map", {o["id"]: o["id"] for o in objects})
    if not isinstance(mapping, dict) or set(mapping) != {o["id"] for o in objects} or set(mapping.values()) - PANEL_OBJECTS:
        raise VisualError("invalid_review", "Bind every selected object ID to a known raw panel glyph.")
    if len(set(mapping.values())) != len(mapping):
        raise VisualError("invalid_review", "Two selected IDs cannot bind the same physical glyph.")
    times = [f["time_s"] for f in manifest["frames"]]
    active = {oid: _active(selection, oid, times) for oid in mapping}
    rows = [{**row, "points": {oid: deepcopy(row["points"][glyph]) for oid, glyph in mapping.items()}} for row in raw["rows"]]
    failures = []
    measured_policy = events.get("motion_start_policy", {}).get("version")
    options = events.get("options", {})
    if measured_policy != contract["motion_policy"] or any(abs(finite(options.get(key), key) - contract[key]) > 1e-9
            for key in ("rest_duration_s", "max_gap_s", "rest_radius_px")):
        failures.append({"code": "event_definition_mismatch", "record_id": record["id"]})
    bundle.update(observations=observations, event_set=events, mapping=mapping, rows=rows, active=active)
    return failures


def _event_proposals(bundle, contract):
    ids = list(bundle["mapping"])
    groups = [(oid,) for oid in ids]
    selection = bundle["record"].get("selection", bundle["observations"].get("selection", {}))
    for relation in selection.get("relative_motion", []):
        group = (relation["object_id"], relation["relative_to"])
        if not set(group).issubset(ids) or group[0] == group[1]:
            raise VisualError("invalid_review", "Public relative-motion IDs are not selected objects.")
        if group not in groups:
            groups.append(group)
    frames = {f["index"]: f for f in bundle["manifest"]["frames"]}
    proposed = []
    for group in groups:
        proposed.extend(raw_event_review.candidates(bundle["rows"], list(group), contract["rest_duration_s"],
            active_intervals_s=bundle["active"], max_gap_s=contract["max_gap_s"],
            policy=contract["motion_policy"], rest_radius_px=contract["rest_radius_px"]))
    for event in proposed:
        a, b = [frames[i] for i in event["indices"]]
        # Preserve recorder uncertainty instead of treating sample times as exact.
        event["observed_interval_s"] = [a.get("time_interval_s", [a["time_s"], a["time_s"]])[0],
                                        b.get("time_interval_s", [b["time_s"], b["time_s"]])[1]]
        event["uncertainty_s"] = event["observed_interval_s"][1] - event["observed_interval_s"][0]
        event["timing_status"] = "resolved" if event["uncertainty_s"] <= MAX_UNCERTAINTY_S + 1e-9 else "unresolved"
    return sorted(proposed, key=lambda e: (e["observed_interval_s"][0], e["object_ids"], e["type"]))


def _event_key(event):
    ids = list(event.get("object_ids", []))
    parent = event.get("relative_to_object_id")
    if parent and len(ids) == 1:
        ids.append(parent)
    return event.get("type"), tuple(ids)


def _compare_events(bundle, contract):
    proposed = _event_proposals(bundle, contract)
    pools, expected = defaultdict(list), defaultdict(list)
    failures, checks, relations = [], [], []
    name = bundle["record"]["id"]
    for event in proposed:
        pools[_event_key(event)].append(event)
    for event in bundle["event_set"].get("events", []):
        interval = _interval(event.get("time_interval_s"), "produced event")
        width = max(interval[1] - interval[0], finite(event.get("uncertainty_s", interval[1] - interval[0]),
                                                     "event uncertainty", minimum=0))
        if width > MAX_UNCERTAINTY_S + 1e-9 or event.get("timing_status", "resolved") != "resolved":
            failures.append({"code": "produced_event_uncertainty_exceeds_limit", "record_id": name,
                             "event_type": event.get("type"), "uncertainty_s": width})
        if event.get("type") in EVENT_TYPES:
            expected[_event_key(event)].append(event)
        else:
            failures.append({"code": "unsupported_event_type", "record_id": name, "event_type": event.get("type")})
    for key in sorted(set(pools) | set(expected)):
        produced = sorted(expected[key], key=lambda e: _interval(e.get("time_interval_s"), "produced event"))
        actual = pools[key]
        if len(produced) != len(actual):
            failures.append({"code": "event_occurrence_mismatch", "record_id": name,
                             "event_type": key[0], "objects": list(key[1]), "produced": len(produced), "raw": len(actual)})
        for occurrence, (value, independent) in enumerate(zip(produced, actual)):
            wanted = _interval(value.get("time_interval_s"), "produced event")
            observed = independent["observed_interval_s"]
            difference = max(abs(a - b) for a, b in zip(wanted, observed))
            width = max(wanted[1] - wanted[0], independent["uncertainty_s"],
                        finite(value.get("uncertainty_s", wanted[1] - wanted[0]), "event uncertainty", minimum=0))
            within = difference <= contract["event_tolerance_s"] + 1e-9
            resolved = width <= MAX_UNCERTAINTY_S + 1e-9 and value.get("timing_status", "resolved") == "resolved"
            checks.append({"type": key[0], "objects": list(key[1]), "occurrence": occurrence,
                           "produced_interval_s": wanted, "raw_interval_s": observed, "difference_s": difference,
                           "within_frozen_tolerance": within, "max_uncertainty_s": width, "resolved": resolved})
            if not within:
                failures.append({"code": "event_interval_disagreement", "record_id": name, "event_type": key[0], "occurrence": occurrence})
            if not resolved:
                failures.append({"code": "event_uncertainty_exceeds_limit", "record_id": name, "event_type": key[0], "uncertainty_s": width})
    for event in proposed:
        if event["uncertainty_s"] > MAX_UNCERTAINTY_S + 1e-9:
            failures.append({"code": "raw_event_uncertainty_exceeds_limit", "record_id": name, "uncertainty_s": event["uncertainty_s"]})
    cadence = [b["time_s"] - a["time_s"] for a, b in zip(bundle["rows"], bundle["rows"][1:])]
    for value in bundle["event_set"].get("relations", []):
        if value.get("type") != "relative_event_timing":
            failures.append({"code": "unsupported_relation_type", "record_id": name, "relation_type": value.get("type")})
            continue
        object_ids = value.get("object_ids", [])
        occurrence = value.get("occurrence", 0)
        if len(object_ids) != 2 or type(occurrence) is not int or occurrence < 0:
            raise VisualError("invalid_review", "Relative event timing needs two IDs and a nonnegative occurrence.")
        parent = value.get("relative_to_object_id")
        choices = [pools[(value.get("event_type"), tuple([oid] + ([parent] if parent else [])))] for oid in object_ids]
        actual = raw_event_review.relative_timing(*(p[occurrence]["observed_interval_s"] for p in choices), cadence,
                   policy=contract["motion_policy"]) if all(len(p) > occurrence for p in choices) else None
        wanted = _interval(value.get("lag_interval_s"), "produced lag")
        within = actual is not None and actual["order"] == value.get("order") and max(
            abs(a - b) for a, b in zip(wanted, actual["lag_interval_s"])) <= contract["event_tolerance_s"] + 1e-9
        width = max(wanted[1] - wanted[0], finite(value.get("uncertainty_s", wanted[1] - wanted[0]), "relation uncertainty", minimum=0),
                    actual["uncertainty_s"] if actual else 0)
        resolved = actual is not None and width <= MAX_UNCERTAINTY_S + 1e-9 and value.get("timing_status") == "resolved" and actual["timing_status"] == "resolved"
        relations.append({"objects": object_ids, "event_type": value.get("event_type"), "occurrence": occurrence,
                          "produced_lag_interval_s": wanted, "produced_order": value.get("order"), "raw": actual,
                          "within_frozen_tolerance": within, "resolved": resolved, "max_uncertainty_s": width})
        if not within:
            failures.append({"code": "relation_disagreement", "record_id": name, "event_type": value.get("event_type")})
        if not resolved:
            failures.append({"code": "relation_uncertainty_exceeds_limit", "record_id": name, "uncertainty_s": width})
    return {"record_id": name, "raw_events": proposed, "event_checks": checks, "relation_checks": relations}, failures


def _compare_positions(bundles):
    travel = defaultdict(list)
    for bundle in bundles:
        if not bundle["record"]["reference"]:
            continue
        for oid in bundle["mapping"]:
            points = [r["points"][oid] for r in bundle["rows"] if "x" in r["points"][oid] and any(
                a <= r["time_s"] <= b for a, b in bundle["active"][oid])]
            if points:
                travel[oid].append(math.hypot(max(p["x"] for p in points) - min(p["x"] for p in points),
                                             max(p["y"] for p in points) - min(p["y"] for p in points)))
    groups, failures = {}, []
    for bundle in bundles:
        record = bundle["record"]
        frames = {f["index"]: f for f in bundle["manifest"]["frames"]}
        for obj in bundle["observations"]["objects"]:
            oid = obj["id"]
            key = record["role"], oid
            group = groups.setdefault(key, {"role": key[0], "object_id": oid, "raw_images": set(), "paired_images": set(),
                                           "points": [], "missing_or_ambiguous": []})
            produced = {f["index"]: f for f in obj.get("frames", [])}
            if len(produced) != len(obj.get("frames", [])):
                raise VisualError("invalid_review", "Duplicate produced frame indexes.")
            for row in bundle["rows"]:
                if not any(a <= row["time_s"] <= b for a, b in bundle["active"][oid]):
                    continue
                point = row["points"][oid]
                target = produced.get(row["index"])
                reason = None
                if "x" not in point:
                    reason = "raw_pixel_ambiguity" if point.get("boxes") else "missing_raw_object"
                else:
                    group["raw_images"].add(row["image_sha256"])
                if target is None or target.get("valid") is not True:
                    reason = reason or "missing_produced_position"
                elif target.get("time_s") != row["time_s"] or target.get("image_sha256", target.get("frame_sha256", frames[row["index"]]["sha256"])) != row["image_sha256"]:
                    reason = reason or "produced_frame_binding_mismatch"
                elif not all(type(target.get(k)) in (int, float) and math.isfinite(target[k]) for k in ("x", "y")):
                    reason = reason or "invalid_produced_position"
                if reason:
                    group["missing_or_ambiguous"].append({"record_id": record["id"], "index": row["index"], "reason": reason})
                    continue
                error = math.hypot(point["x"] - target["x"], point["y"] - target["y"])
                group["paired_images"].add(row["image_sha256"])
                group["points"].append({"record_id": record["id"], "index": row["index"], "image_sha256": row["image_sha256"],
                                        "raw": [point["x"], point["y"]], "produced": [target["x"], target["y"]], "error_px": error})
    result = []
    for key, group in sorted(groups.items()):
        reference_travel = max(travel[key[1]], default=None)
        threshold = max(1.5, reference_travel * .005) if reference_travel is not None else None
        median_error = statistics.median(p["error_px"] for p in group["points"]) if group["points"] else None
        raw_count, pair_count = len(group.pop("raw_images")), len(group.pop("paired_images"))
        passed = raw_count >= MIN_DISTINCT_IMAGES and pair_count >= MIN_DISTINCT_IMAGES and not group["missing_or_ambiguous"] and threshold is not None and median_error <= threshold + 1e-9
        group.update(raw_distinct_images=raw_count, paired_distinct_images=pair_count, minimum_distinct_images=MIN_DISTINCT_IMAGES,
                     reference_motion_travel_px=reference_travel, tolerance_px=threshold, median_error_px=median_error, pass_=passed)
        group["pass"] = group.pop("pass_")
        result.append(group)
        if not passed:
            failures.append({"code": "position_review_failed", "role": key[0], "object_id": key[1],
                             "insufficient_distinct_images": min(raw_count, pair_count) < MIN_DISTINCT_IMAGES,
                             "missing_reference": threshold is None, "missing_or_ambiguous": len(group["missing_or_ambiguous"]),
                             "median_error_px": median_error, "tolerance_px": threshold})
    return result, failures


def _compact_sheets(bundles, event_results, output):
    """Select public/raw critical support, never production coordinates/events."""
    selected = {b["record"]["id"]: set() for b in bundles}
    pools = defaultdict(dict)
    controls, reasons = [], defaultdict(set)
    by_id = {b["record"]["id"]: b for b in bundles}
    raw_events = {e["record_id"]: e["raw_events"] for e in event_results}

    def neighbors(bundle, indices, reason):
        name = bundle["record"]["id"]
        available = {f["index"] for f in bundle["manifest"]["frames"]}
        for index in indices:
            for near in (index - 1, index, index + 1):
                if near in available:
                    selected[name].add(near)
                    reasons[name, near].add(reason)

    for bundle in bundles:
        record = bundle["record"]
        for row in bundle["rows"]:
            for oid in bundle["mapping"]:
                if not any(a <= row["time_s"] <= b for a, b in bundle["active"][oid]):
                    continue
                if "x" in row["points"][oid]:
                    pools[record["role"], oid].setdefault(row["image_sha256"], {
                        "record_id": record["id"], "role": record["role"], "object_id": oid,
                        "index": row["index"], "time_s": row["time_s"], "image_sha256": row["image_sha256"]})
                else:
                    neighbors(bundle, [row["index"]], "raw_missing_or_ambiguity_neighborhood")
        # These candidates were made from independent raw rows and the frozen
        # public contract. Produced event intervals cannot influence selection.
        for event in raw_events[record["id"]]:
            neighbors(bundle, event["indices"] + event.get("support_indices", []), "raw_event_support_neighborhood")
        times = [f["time_s"] for f in bundle["manifest"]["frames"]]
        for event in bundle["manifest"]["inputs"]["events"]:
            nearest = min(range(len(times)), key=lambda i: abs(times[i] - event["time_s"]))
            neighbors(bundle, [nearest], "public_input_neighborhood")
        # Retain both clip edges even for a static/short clip with no proposals.
        neighbors(bundle, [0, len(times) - 1], "clip_boundary_neighborhood")
    for key, pool in sorted(pools.items()):
        unique = list(pool.values())
        count = min(MIN_DISTINCT_IMAGES, len(unique))
        indices = sorted(set(int(i) for i in np.linspace(0, len(unique) - 1, count))) if count else []
        for index in indices:
            control = unique[index]
            controls.append(control)
            selected[control["record_id"]].add(control["index"])
            reasons[control["record_id"], control["index"]].add("uniform_distinct_object_role_control")
    identities = []
    for name, indices in selected.items():
        bundle = by_id[name]
        manifest = bundle["manifest"]
        row_by_index = {row["index"]: row for row in bundle["raw"]["rows"]}
        paths = frame_paths(bundle["record"]["package"], manifest)
        tiles, sheets = [], []
        for index in sorted(indices):
            frame = manifest["frames"][index]
            path = paths[index]
            image = read_image(path)
            if file_sha256(path) != frame["sha256"]:
                raise VisualError("source_changed", "Selected raw image changed while making its contact sheet.")
            tiles.append(_tile(image, frame, name, row_by_index[index]["points"]))
            identities.append({"record_id": name, "role": bundle["record"]["role"], "index": index,
                               "time_s": frame["time_s"], "image_sha256": frame["sha256"],
                               "source_path": str(path), "reasons": sorted(reasons[name, index])})
            if len(tiles) == 20:
                relative = f"sheets/{name}-{len(sheets):03}.png"
                _save_image(output / relative, np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, 20, 4)]))
                sheets.append(relative)
                tiles = []
        if tiles:
            tiles.extend(np.full((210, 400, 3), 24, np.uint8) for _ in range((-len(tiles)) % 4))
            relative = f"sheets/{name}-{len(sheets):03}.png"
            _save_image(output / relative, np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)]))
            sheets.append(relative)
        bundle["sheets"] = sheets
    return {"kind": "FrozenRawContactSelection", "policy": "control_and_event_v1",
            "minimum_distinct_control_images_per_object_role": MIN_DISTINCT_IMAGES,
            "controls": controls, "selected_images": identities,
            "production_coordinates_or_events_used": False,
            "all_frames_extracted_hashed_and_numerically_checked": True}


def _code_inventory():
    # Include every local code source used in this aid, not historical receipts.
    from . import session as session_code
    return {str(Path(module.__file__).resolve()): file_sha256(module.__file__) for module in
            (raw_panel_review_aid, raw_event_review, package_code, png_code, schema_code, session_code)} | {
                str(Path(__file__).resolve()): file_sha256(__file__)}


def review_records(records, output, event_contract=None, *, contact_policy="all_frames"):
    """Create raw sheets and numerical checks; return pending until inspection.

    A record accepts ``id/case_id, role, domain, package, analysis``; analysis can
    be a path or inline ``{observations, events}``. Nested capture-index ``record``
    values work too. ``object_map`` maps public IDs to bookmark/handle/star.
    Reference/calibration roles supply independent reference travel; other roles
    can explicitly set ``reference=True``. No measurement can choose raw points.
    ``control_and_event_v1`` changes required visual sheets only; extraction,
    image hashes, position/event comparisons and numerical gates cover all frames.
    """
    output = _ordinary(Path(output))
    if output.exists():
        raise VisualError("output_exists", "Preserve earlier raw aids; choose a fresh output.")
    if not isinstance(records, (list, tuple)) or not records:
        raise VisualError("insufficient_review", "At least one actual pixel recording is required.")
    if contact_policy not in CONTACT_POLICIES:
        raise VisualError("invalid_review", "Choose a supported frozen raw contact policy.")
    contract = _public_contract(event_contract)
    normalized = [_record(r, i) for i, r in enumerate(records)]
    if len({r["id"] for r in normalized}) != len(normalized):
        raise VisualError("invalid_review", "Recording IDs must be unique.")
    if any(output.is_relative_to(r["package"]) or r["package"].is_relative_to(output) for r in normalized):
        raise VisualError("unsafe_path", "Raw aid output and source packages cannot contain each other.")
    code_inventory = _code_inventory()
    inventory, bundles, unsupported, failures = {}, [], [], []
    output.mkdir(parents=True)
    # Deliberately complete this pass for every record before opening analysis.
    for record in normalized:
        if record["domain"] != "panel":
            unsupported.append({"record_id": record["id"], "domain": record["domain"], "status": "unsupported"})
            manifest = load_package(record["package"])
            inventory[str(record["package"] / "manifest.json")] = file_sha256(record["package"] / "manifest.json")
            for frame, path in zip(manifest["frames"], frame_paths(record["package"], manifest)):
                inventory[str(path)] = frame["sha256"]
            failures.append({"code": "unsupported_domain", "record_id": record["id"], "domain": record["domain"]})
        else:
            bundles.append(_extract(record, output, inventory, contact_policy))
    write_json(output / "extraction-complete.json", {"status": "unsigned", "all_pixel_extraction_finished_before_measurement_read": True,
               "raw_records": [b["record"]["id"] for b in bundles], "raw_hashes": {
                   b["record"]["id"]: file_sha256(output / "raw" / b["record"]["id"] / "points.json") for b in bundles}})
    event_results, measurement_bindings, common_mapping = [], [], {}
    for bundle in bundles:
        observations, events = _measurement(bundle["record"], inventory)
        failures.extend(_scope(bundle, observations, events, contract))
        for oid, glyph in bundle["mapping"].items():
            if oid in common_mapping and common_mapping[oid] != glyph:
                raise VisualError("invalid_review", "The same selected ID changed physical glyph between recordings.")
            common_mapping[oid] = glyph
        write_json(output / "measurements" / bundle["record"]["id"] / "measurement.json",
                   {"observations": observations, "events": events})
        comparison, errors = _compare_events(bundle, contract)
        event_results.append(comparison)
        failures.extend(errors)
        measurement_bindings.append({"record_id": bundle["record"]["id"], "role": bundle["record"]["role"],
            "package_sha256": bundle["manifest"]["package_sha256"], "observations_sha256": digest(observations),
            "events_sha256": digest(events), "object_map": bundle["mapping"], "public_active_intervals_s": bundle["active"]})
    for bundle in bundles:
        missing = sorted(set(common_mapping) - set(bundle["mapping"]))
        if missing:
            failures.append({"code": "missing_selected_objects", "record_id": bundle["record"]["id"], "objects": missing})
    positions, errors = _compare_positions(bundles)
    failures.extend(errors)
    if contact_policy == "control_and_event_v1":
        contact_selection = _compact_sheets(bundles, event_results, output)
    else:
        contact_selection = {"kind": "FrozenRawContactSelection", "policy": "all_frames", "controls": [],
            "selected_images": [{"record_id": b["record"]["id"], "role": b["record"]["role"], "index": f["index"],
                "time_s": f["time_s"], "image_sha256": f["sha256"], "source_path": str(path), "reasons": ["all_frames"]}
                for b in bundles for f, path in zip(b["manifest"]["frames"], frame_paths(b["record"]["package"], b["manifest"]))],
            "production_coordinates_or_events_used": False, "all_frames_extracted_hashed_and_numerically_checked": True}
    write_json(output / "contact-selection.json", contact_selection)
    sheets = [{"path": name, "sha256": file_sha256(output / name)} for b in bundles for name in b["sheets"]]
    result = {"schema_version": 1, "kind": "ProspectiveUnsignedRawReviewAid", "version": VERSION,
              "status": "pending_visual_review", "numeric_status": "pass" if not failures else "fail",
              "acceptance_granted": False, "visual_inspection_claimed": False, "historical_acceptance_changed": False,
              "production_coordinates_used_for_extraction": False, "smoothing_or_interpolation_used": False,
              "public_event_contract": contract, "public_event_contract_sha256": digest(contract),
              "contact_policy": contact_policy, "contact_selection_sha256": digest(contact_selection),
              "position_travel_definition": "Maximum per-reference-record diagonal of independent raw x/y ranges, per selected object ID.",
              "records": measurement_bindings, "positions": positions, "events": event_results,
              "sheets": sheets, "unsupported": unsupported, "failures": failures,
              "methods": [{"name": "raw_panel_review_aid.raw_anchors", "status": "unsigned_aid", "input": "raw image only"},
                          {"name": "visual.raw_event_review.candidates", "status": "unsigned_aid", "input": "independent raw points and public contract"},
                          {"name": "visual.raw_event_review.relative_timing", "status": "unsigned_aid", "input": "independent raw event intervals and observed raw cadence"},
                          {"name": contact_policy, "status": "unsigned_aid", "input": "raw points/events, public input timeline, declared object lifetimes"}],
              "dependencies": {"opencv": cv2.__version__, "numpy": np.__version__},
              "limitations": ["HSV identities cover the declared natural panel artwork only; visual inspection is required.",
                              "SVG and game raw review are unsupported by this version.",
                              "Hashes bind this experiment and do not provide a digital signature."]}
    write_json(output / "result.json", result)
    generated = {p.relative_to(output).as_posix(): file_sha256(p) for p in output.rglob("*") if p.is_file()}
    receipt = {"kind": "UnsignedRawReviewEvidenceReceipt", "status": "unsigned", "result_sha256": digest(result),
               "source_files": inventory, "code_sources": code_inventory, "generated_files": generated,
               "measurement_bindings_sha256": digest(measurement_bindings), "public_event_contract_sha256": digest(contract),
               "contact_policy": contact_policy, "contact_selection_sha256": digest(contact_selection)}
    write_json(output / "evidence-receipt.json", receipt)
    return {**result, "output": str(output), "receipt_path": str(output / "evidence-receipt.json")}


def attest_review(output, *, reviewer, inspected_sheets, accepted, images_inspected=False):
    """Persist the caller's explicit inspection; never infer it from numerical pass.

    The human/root reviewer must actually open every recorded contact sheet,
    then provide their exact relative paths. This function records that claim;
    it cannot establish that somebody looked at the images by itself.
    """
    output = _ordinary(Path(output))
    if not isinstance(reviewer, str) or not reviewer.strip() or images_inspected is not True or type(accepted) is not bool:
        raise VisualError("invalid_review", "An explicit named reviewer, actual image inspection, and boolean decision are required.")
    if (output / "attestation.json").exists():
        raise VisualError("output_exists", "Preserve an earlier review attestation.")
    result = read_json(output / "result.json")
    receipt = read_json(output / "evidence-receipt.json")
    if digest(result) != receipt.get("result_sha256"):
        raise VisualError("stale_review", "The pending raw aid changed.")
    for path, expected in {**receipt["source_files"], **receipt["code_sources"]}.items():
        if file_sha256(_ordinary(Path(path))) != expected:
            raise VisualError("stale_review", "A source image, measurement, or code source changed.")
    for path, expected in receipt["generated_files"].items():
        if file_sha256(safe_path(output, path)) != expected:
            raise VisualError("stale_review", "A raw proposal or contact sheet changed.")
    if not isinstance(inspected_sheets, (list, tuple)) or len(set(inspected_sheets)) != len(inspected_sheets) or set(inspected_sheets) != {s["path"] for s in result["sheets"]}:
        raise VisualError("incomplete_visual_review", "Explicitly inspect every bound raw contact sheet.")
    if accepted and (result["numeric_status"] != "pass" or result["unsupported"] or not result["sheets"]):
        raise VisualError("review_not_qualified", "Inspection cannot override missing points, numerical failure, unsupported domains, or unresolved uncertainty.")
    attestation = {"kind": "ProspectiveRawVisualReviewAttestation", "status": "pass" if accepted else "fail",
                   "reviewer": reviewer.strip(), "images_inspected": True, "accepted": accepted,
                   "method": "explicit_visual_inspection_of_unsigned_raw_pixel_aids",
                   "result_sha256": digest(result), "evidence_receipt_sha256": file_sha256(output / "evidence-receipt.json"),
                   "inspected_sheets": result["sheets"], "historical_acceptance_changed": False,
                   "digital_signature": False}
    # Exclusive creation avoids replacing another reviewer's concurrent decision.
    with (output / "attestation.json").open("x", encoding="utf-8") as stream:
        import json
        stream.write(json.dumps(attestation, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return attestation
