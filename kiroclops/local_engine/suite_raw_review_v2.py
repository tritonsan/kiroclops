"""Independent prospective panel refinements; legacy evidence is unchanged.

This module never calls the production tracker or event detector. Soft glyph
mass is measured independently in each raw image, without time smoothing,
rounding to tracker coordinates, or reading a target implementation.
"""
from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
import math
from pathlib import Path
import statistics

from kiroclops.visual.raw_event_review import CONFIRMED, relative_timing
from kiroclops.visual.package import load_package, frame_paths, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, finite, write_json
from . import suite_raw_review as legacy
from scripts.raw_panel_centroid_aid import mass
from scripts.raw_panel_review_aid import raw_anchors

VERSION = "prospective-panel-subpixel-raw-review-v2"


def subpixel_anchors(image, origin=None):
    """Return float raw anchors and immutable first-image physical-point offsets.

    The first point is the raw HSV midpoint, requiring explicit visual review.
    Subsequent points retain its offset from independently measured glyph mass.
    Neither a produced coordinate nor a stored trajectory is accepted as input.
    """
    legacy = raw_anchors(image)
    origin = deepcopy(origin or {})
    points = {}
    for oid, point in legacy.items():
        if "x" not in point:
            points[oid] = deepcopy(point)
            continue
        try:
            center = mass(image, point)
        except ValueError as exc:
            points[oid] = {"status": "ambiguous", "reason": "independent_color_mass_unobservable", "detail": str(exc)}
            continue
        if oid not in origin:
            origin[oid] = {"anchor": [point["x"], point["y"]], "centroid": center,
                           "method": "first_raw_HSV_midpoint_with_independent_color_mass_offset"}
        first = origin[oid]
        points[oid] = {"x": first["anchor"][0] + center[0] - first["centroid"][0],
                       "y": first["anchor"][1] + center[1] - first["centroid"][1],
                       "color_bounds": point["color_bounds"], "legacy_HSV_center": [point["x"], point["y"]],
                       "independent_mass_centroid": center, "subpixel_method": "local_background_color_contrast_mass",
                       "quantization": "none", "status": "unsigned"}
    return points, origin


def _intervals(value):
    intervals = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise VisualError("invalid_review", "Object lifetime must contain finite intervals.")
        a, b = [finite(v, "active interval") for v in item]
        if a > b:
            raise VisualError("invalid_review", "Object lifetime interval is reversed.")
        intervals.append((a, b))
    return intervals


def _commands(inputs):
    states, result = {}, []
    for event in inputs or []:
        stamp = finite(event.get("time_s"), "public input timestamp")
        if not isinstance(event.get("action"), str):
            raise VisualError("invalid_input_timeline", "Public input action is required.")
        pressed = event.get("pressed")
        if type(pressed) is bool:
            key = tuple(event.get(k) for k in ("action", "selector", "key", "button"))
            if key in states and states[key] is pressed:
                continue
            states[key] = pressed
        result.append({**event, "time_s": stamp})
    return result


def candidates_v3(rows, objects, rest_duration=.2, *, active_intervals_s=None,
                  max_gap_s=.12, rest_radius_px=1.5, speed_threshold_px_s=5.,
                  overshoot_min_px=1.5, axis="auto", inputs=None, policy=CONFIRMED):
    """Independently implement the declared streaming v3 rest/departure policy.

    Rest includes the departure image when its following support is bounded.
    Public commands censor endpoint excursions of interrupted episodes. No
    production coordinates, validity flags, event intervals or functions enter.
    """
    rest_duration = finite(rest_duration, "public rest duration", minimum=0)
    if policy != CONFIRMED or abs(rest_duration - .2) > 1e-9:
        raise VisualError("unsupported_event_definition", "This aid requires v3 and .20s rest support.")
    if not 1 <= len(objects) <= 2 or len(set(objects)) != len(objects) or axis not in ("auto", "x", "y"):
        raise VisualError("invalid_event_definition", "Select one/two distinct raw IDs and a public axis.")
    for value in (max_gap_s, rest_radius_px, speed_threshold_px_s, overshoot_min_px):
        if finite(value, "event limit", minimum=0) <= 0:
            raise VisualError("invalid_event_definition", "Positive finite public event limits are required.")
    active = {oid: _intervals(intervals) for oid, intervals in (active_intervals_s or {}).items()}
    prepared, prior_time = [], None
    for row in rows:
        stamp = finite(row["time_s"], "raw frame timestamp")
        if prior_time is not None and stamp <= prior_time:
            raise VisualError("invalid_frame_time", "Raw frame times must strictly increase.")
        prior_time = stamp
        points = [row.get("points", {}).get(oid, {}) for oid in objects]
        valid = all(all(type(p.get(k)) in (int, float) and math.isfinite(p[k]) for k in ("x", "y")) for p in points)
        valid &= all(oid not in active or any(a <= stamp <= b for a, b in active[oid]) for oid in objects)
        value = None if not valid else [points[0][k] - (points[1][k] if len(points) == 2 else 0) for k in ("x", "y")]
        prepared.append({"index": row["index"], "time_s": stamp, "point": value})
    if axis == "auto":
        known = [r["point"] for r in prepared if r["point"] is not None]
        ranges = [max((p[i] for p in known), default=0) - min((p[i] for p in known), default=0) for i in (0, 1)]
        axis = "y" if ranges[1] > ranges[0] else "x"
    coordinate = 0 if axis == "x" else 1
    commands = _commands(inputs)
    events, previous, window, episode = [], None, [], []
    direction, extreme, first_turn, moving = 0, None, None, False

    def emit(kind, a, b, **extra):
        events.append({"type": kind, "object_ids": list(objects), "indices": [a["index"], b["index"]],
                       "observed_interval_s": [a["time_s"], b["time_s"]], "predicate_version": CONFIRMED,
                       "axis": axis, "status": "unsigned", "physical_motion_inferred": False, **extra})

    def extent(values):
        return math.hypot(*(max(r["point"][i] for r in values) - min(r["point"][i] for r in values) for i in (0, 1)))

    def excursion(tail):
        endpoint = [statistics.median(r["point"][i] for r in tail) for i in (0, 1)]
        vector = [endpoint[i] - episode[0]["point"][i] for i in (0, 1)]
        length = math.hypot(*vector)
        projection = [v / length for v in vector] if length > rest_radius_px else ([1., 0.] if
            max(r["point"][0] for r in episode) - min(r["point"][0] for r in episode) >=
            max(r["point"][1] for r in episode) - min(r["point"][1] for r in episode) else [0., 1.])
        amounts = [sum((r["point"][i] - endpoint[i]) * projection[i] for i in (0, 1)) for r in episode]
        support = {"support_indices": [r["index"] for r in tail], "endpoint_px": endpoint,
                   "endpoint_source": "independently_observed_stable_tail", "projection": projection}
        if length > rest_radius_px and max(amounts) > overshoot_min_px:
            onset = next(i for i, value in enumerate(amounts) if value > overshoot_min_px)
            peak = max(range(len(episode)), key=lambda i: amounts[i])
            last_peak = peak
            while last_peak + 1 < len(episode) and abs(amounts[last_peak + 1] - amounts[peak]) <= 1e-9:
                last_peak += 1
            a, b = episode[max(0, peak - 1)], episode[min(len(episode) - 1, last_peak + 1)]
            peak_support = {**support, "support_indices": sorted(set(support["support_indices"] + [a["index"], b["index"]])),
                "endpoint_support_indices": support["support_indices"], "peak_indices": [a["index"], b["index"]],
                "peak_observed_interval_s": [a["time_s"], b["time_s"]], "peak_uncertainty_s": b["time_s"] - a["time_s"]}
            emit("overshoot", episode[max(0, onset - 1)], episode[onset], **peak_support)
        signs = []
        for i, value in enumerate(amounts):
            sign = 1 if value > overshoot_min_px else -1 if value < -overshoot_min_px else 0
            if not sign or not signs and sign < 0 and length > rest_radius_px:
                continue
            if not signs or signs[-1][0] != sign:
                signs.append((sign, i))
        if len(signs) >= 3:
            onset = signs[0][1]
            emit("oscillation", episode[max(0, onset - 1)], episode[onset], **support)

    for frame in prepared:
        if frame["point"] is None or previous is not None and frame["time_s"] - previous["time_s"] > max_gap_s:
            previous, window, episode, direction, extreme, first_turn, moving = None, [], [], 0, None, None, False
            if frame["point"] is None:
                continue
        if previous is None:
            previous, window = frame, [frame]
            continue
        dt = frame["time_s"] - previous["time_s"]
        signed_speed = (frame["point"][coordinate] - previous["point"][coordinate]) / dt
        current_direction = 1 if signed_speed > speed_threshold_px_s else -1 if signed_speed < -speed_threshold_px_s else 0
        if not moving:
            departure = math.dist(frame["point"], window[0]["point"])
            if departure > rest_radius_px:
                emit("motion_start", previous, frame, rest_anchor_index=window[0]["index"],
                     rest_anchor_px=window[0]["point"], departure_distance_px=departure)
                moving, episode, window, direction, extreme, first_turn = True, [previous, frame], [], current_direction, frame, None
            else:
                window.append(frame)
        else:
            episode.append(frame)
            if current_direction:
                if not direction:
                    direction, extreme = current_direction, frame
                elif current_direction == direction:
                    if direction * (frame["point"][coordinate] - extreme["point"][coordinate]) >= 0:
                        extreme = frame
                    first_turn = None
                else:
                    if first_turn is None:
                        first_turn = (previous, frame)
                    if direction * (extreme["point"][coordinate] - frame["point"][coordinate]) > rest_radius_px:
                        emit("reversal", *first_turn, confirmed_at_index=frame["index"])
                        direction, extreme, first_turn = current_direction, frame, None
            if not window:
                window = [previous]
            window.append(frame)
            while len(window) > 1 and extent(window) > rest_radius_px:
                window.pop(0)
            if len(window) > 1 and window[-1]["time_s"] - window[0]["time_s"] >= rest_duration - 1e-9:
                for kind in ("stop", "settled"):
                    emit(kind, window[0], window[1], support_indices=[r["index"] for r in window],
                         confirmed_at_index=frame["index"])
                interrupted = any(episode[1]["time_s"] < e["time_s"] < window[0]["time_s"] for e in commands)
                if not interrupted:
                    excursion(window)
                moving, episode, direction, extreme, first_turn, window = False, [], 0, None, None, [frame]
        previous = frame
    return events


def _extract(record, output, inventory, contact_policy):
    manifest = load_package(record["package"])
    inventory[str(record["package"] / "manifest.json")] = file_sha256(record["package"] / "manifest.json")
    if record.get("package_sha256") not in (None, manifest["package_sha256"]):
        raise VisualError("wrong_result", "Recording binding differs from actual raw pixels.")
    rows, tiles, sheets, origin = [], [], [], None

    def page():
        relative = f"sheets/{record['id']}-{len(sheets):03}.png"
        tiles.extend(np.full((210, 400, 3), 24, np.uint8) for _ in range((-len(tiles)) % 4))
        legacy._save_image(output / relative, np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)]))
        sheets.append(relative)
        tiles.clear()

    import numpy as np
    for frame, path in zip(manifest["frames"], frame_paths(record["package"], manifest)):
        image = read_image(path)
        points, origin = subpixel_anchors(image, origin)
        if file_sha256(path) != frame["sha256"]:
            raise VisualError("source_changed", "A raw image changed during independent soft-mass extraction.")
        inventory[str(path)] = frame["sha256"]
        rows.append({"index": frame["index"], "time_s": frame["time_s"], "image_sha256": frame["sha256"], "points": points})
        if contact_policy == "all_frames":
            tiles.append(legacy._tile(image, frame, record["id"], points))
            if len(tiles) == 20:
                page()
    if tiles:
        page()
    raw = {"kind": "UnsignedIndependentSubpixelPanelProposals", "status": "unsigned", "version": VERSION,
           "package_sha256": manifest["package_sha256"], "rows": rows, "first_image_origins": origin,
           "method": "Each raw glyph's local-background color-contrast mass; unchanged first-image raw physical point offset.",
           "production_coordinates_used": False, "production_events_used": False,
           "smoothing_or_rounding_used": False, "target_code_or_dom_read": False, "sheets": sheets}
    write_json(output / "raw" / record["id"] / "points.json", raw)
    return {"record": record, "manifest": manifest, "raw": raw, "sheets": sheets}


def _proposals(bundle, contract):
    ids = list(bundle["mapping"])
    selection = bundle["observations"].get("selection", {})
    groups = {(oid,): contract["axis"] for oid in ids}
    for spec in selection.get("relative_motion", []):
        group = (spec["object_id"], spec["relative_to"])
        if not set(group).issubset(ids) or len(set(group)) != 2:
            raise VisualError("invalid_review", "Public relative-motion objects are not selected raw identities.")
        groups[group] = spec.get("axis", contract["axis"])
    events = []
    for group, axis in groups.items():
        events.extend(candidates_v3(bundle["rows"], list(group), contract["rest_duration_s"],
            active_intervals_s=bundle["active"], max_gap_s=contract["max_gap_s"], rest_radius_px=contract["rest_radius_px"],
            speed_threshold_px_s=contract["speed_threshold_px_s"], overshoot_min_px=contract["overshoot_min_px"],
            axis=axis, inputs=bundle["manifest"]["inputs"]["events"]))
    frames = bundle["manifest"]["frames"]
    for event in events:
        a, b = [frames[index] for index in event["indices"]]
        event["observed_interval_s"] = [a.get("time_interval_s", [a["time_s"], a["time_s"]])[0],
                                        b.get("time_interval_s", [b["time_s"], b["time_s"]])[1]]
        event["uncertainty_s"] = event["observed_interval_s"][1] - event["observed_interval_s"][0]
        event["timing_status"] = "resolved" if event["uncertainty_s"] <= .1 + 1e-9 else "unresolved"
        if "peak_indices" in event:
            a, b = [frames[index] for index in event["peak_indices"]]
            event["peak_observed_interval_s"] = [a.get("time_interval_s", [a["time_s"], a["time_s"]])[0],
                                                 b.get("time_interval_s", [b["time_s"], b["time_s"]])[1]]
            event["peak_uncertainty_s"] = event["peak_observed_interval_s"][1] - event["peak_observed_interval_s"][0]
            event["peak_timing_status"] = "resolved" if event["peak_uncertainty_s"] <= .1 + 1e-9 else "unresolved"
    return sorted(events, key=lambda e: (e["observed_interval_s"][0], e["object_ids"], e["type"]))


def _compare_events(bundle, contract):
    proposed = _proposals(bundle, contract)
    pools, expected = defaultdict(list), defaultdict(list)
    failures, checks, relations = [], [], []
    name = bundle["record"]["id"]

    def fail(code, **extra):
        failures.append({"code": code, "record_id": name, **extra})

    for event in proposed:
        pools[legacy._event_key(event)].append(event)
        if event["timing_status"] != "resolved":
            fail("raw_event_uncertainty_exceeds_limit", event_type=event["type"], uncertainty_s=event["uncertainty_s"])
        if event.get("peak_timing_status", "resolved") != "resolved":
            fail("raw_peak_uncertainty_exceeds_limit", event_type=event["type"], uncertainty_s=event["peak_uncertainty_s"])
    for event in bundle["event_set"].get("events", []):
        interval = legacy._interval(event.get("time_interval_s"), "produced event")
        width = max(interval[1] - interval[0], finite(event.get("uncertainty_s", interval[1] - interval[0]), "event uncertainty", minimum=0))
        if width > .1 + 1e-9:
            fail("produced_event_uncertainty_exceeds_limit", event_type=event["type"], uncertainty_s=width)
        elif event.get("timing_status", "resolved") != "resolved":
            fail("unresolved_produced_event", event_type=event["type"])
        if "peak_time_interval_s" in event:
            peak = legacy._interval(event["peak_time_interval_s"], "produced peak")
            peak_width = max(peak[1] - peak[0], finite(event.get("peak_uncertainty_s", peak[1] - peak[0]), "peak uncertainty", minimum=0))
            if peak_width > .1 + 1e-9:
                fail("produced_peak_uncertainty_exceeds_limit", event_type=event["type"], uncertainty_s=peak_width)
            elif event.get("peak_timing_status", "resolved") != "resolved":
                fail("unresolved_produced_peak", event_type=event["type"])
        if event["type"] not in legacy.EVENT_TYPES | {"oscillation"}:
            fail("unsupported_event_type", event_type=event["type"])
        else:
            expected[legacy._event_key(event)].append(event)
    for key in sorted(set(pools) | set(expected)):
        produced = sorted(expected[key], key=lambda e: e["time_interval_s"])
        actual = pools[key]
        if len(produced) != len(actual):
            fail("event_occurrence_mismatch", event_type=key[0], objects=list(key[1]), produced=len(produced), raw=len(actual))
        for occurrence, (value, raw) in enumerate(zip(produced, actual)):
            wanted = legacy._interval(value["time_interval_s"], "produced event")
            difference = max(abs(a - b) for a, b in zip(wanted, raw["observed_interval_s"]))
            within = difference <= contract["event_tolerance_s"] + 1e-9
            resolved = raw["timing_status"] == "resolved" and wanted[1] - wanted[0] <= .1 + 1e-9 and value.get("timing_status", "resolved") == "resolved"
            check = {"type": key[0], "objects": list(key[1]), "occurrence": occurrence,
                "produced_interval_s": wanted, "raw_interval_s": raw["observed_interval_s"], "difference_s": difference,
                "within_frozen_tolerance": within, "resolved": resolved}
            if "peak_time_interval_s" in value:
                peak = legacy._interval(value["peak_time_interval_s"], "produced peak")
                observed_peak = raw.get("peak_observed_interval_s")
                peak_difference = max(abs(a - b) for a, b in zip(peak, observed_peak)) if observed_peak else None
                check["peak"] = {"produced_interval_s": peak, "raw_interval_s": observed_peak,
                    "difference_s": peak_difference, "within_frozen_tolerance": peak_difference is not None and peak_difference <= contract["event_tolerance_s"] + 1e-9}
                if not check["peak"]["within_frozen_tolerance"]:
                    fail("peak_interval_disagreement", event_type=key[0], objects=list(key[1]), occurrence=occurrence)
            checks.append(check)
            if not within:
                fail("event_interval_disagreement", event_type=key[0], objects=list(key[1]), occurrence=occurrence, difference_s=difference)
    cadence = [b["time_s"] - a["time_s"] for a, b in zip(bundle["rows"], bundle["rows"][1:])]
    for value in bundle["event_set"].get("relations", []):
        if value["type"] in ("relative_anchor", "object_pair_geometry"):
            continue  # The separate independent geometry gate checks every row.
        if value["type"] != "relative_event_timing":
            fail("unsupported_relation_type", relation_type=value["type"])
            continue
        occurrence = value.get("occurrence", 0)
        ids = value.get("object_ids", [])
        if len(ids) != 2 or type(occurrence) is not int or occurrence < 0:
            raise VisualError("invalid_review", "Relative timing needs two raw IDs and a finite occurrence.")
        parent = value.get("relative_to_object_id")
        choices = [pools[(value["event_type"], tuple([oid] + ([parent] if parent else [])))] for oid in ids]
        available = all(len(pool) > occurrence for pool in choices)
        raw = relative_timing(*(pool[occurrence]["observed_interval_s"] for pool in choices), cadence) if available else None
        wanted = legacy._interval(value["lag_interval_s"], "produced lag")
        width = max(wanted[1] - wanted[0], finite(value.get("uncertainty_s", wanted[1] - wanted[0]), "relation uncertainty", minimum=0),
                    raw["uncertainty_s"] if raw else 0)
        within = raw is not None and raw["order"] == value["order"] and max(abs(a - b) for a, b in zip(wanted, raw["lag_interval_s"])) <= contract["event_tolerance_s"] + 1e-9
        resolved = raw is not None and width <= .1 + 1e-9 and value.get("timing_status") == "resolved" and raw["timing_status"] == "resolved"
        relations.append({"objects": ids, "event_type": value["event_type"], "occurrence": occurrence,
            "produced_lag_interval_s": wanted, "produced_order": value["order"], "raw": raw,
            "within_frozen_tolerance": within, "resolved": resolved, "max_uncertainty_s": width})
        if raw is None:
            fail("missing_raw_relation_support", event_type=value["event_type"], objects=ids, occurrence=occurrence)
        elif width > .1 + 1e-9:
            fail("relation_uncertainty_exceeds_limit", uncertainty_s=width, event_type=value["event_type"])
        elif not resolved:
            fail("unresolved_timing_relation", event_type=value["event_type"])
        if not within:
            fail("relation_disagreement", event_type=value["event_type"], objects=ids, occurrence=occurrence)
    return {"record_id": name, "raw_events": proposed, "event_checks": checks, "relation_checks": relations}, failures


def _pair_geometry(a, b):
    al, at, aw, ah = a["color_bounds"]
    bl, bt, bw, bh = b["color_bounds"]
    ar, ab, br, bb = al + aw, at + ah, bl + bw, bt + bh
    area = max(0, min(ar, br) - max(al, bl)) * max(0, min(ab, bb) - max(at, bt))
    union = aw * ah + bw * bh - area
    return {"anchor_distance_px": math.hypot(a["x"] - b["x"], a["y"] - b["y"]),
            "edge_distance_px": math.hypot(max(al - br, bl - ar, 0), max(at - bb, bt - ab, 0)),
            "overlap_area_px2": float(area), "intersection_over_union": area / union,
            "visually_overlapping": area > 0}


def _compare_geometry(bundles):
    """Explicit raw anchor/vector/color-bound geometry gate, separate from events."""
    travel, groups, failures = defaultdict(list), {}, []
    for bundle in bundles:
        rows = bundle["rows"]
        for value in bundle["event_set"].get("relations", []):
            kind = value["type"]
            if kind not in ("relative_anchor", "object_pair_geometry"):
                continue
            ids = tuple(value.get("object_ids", []))
            if len(ids) != 2 or not set(ids).issubset(bundle["mapping"]):
                raise VisualError("invalid_review", "Geometry refers to an unselected raw identity.")
            key = bundle["record"]["role"], kind, ids
            group = groups.setdefault(key, {"role": key[0], "type": kind, "objects": list(ids), "images": set(),
                "errors": defaultdict(list), "missing": [], "boolean_disagreements": 0})
            index = value.get("index")
            if type(index) is not int or not 0 <= index < len(rows) or value.get("time_s") != rows[index]["time_s"]:
                raise VisualError("wrong_result", "Geometry is not bound to its actual raw image timestamp.")
            points = [rows[index]["points"].get(oid, {}) for oid in ids]
            if any("x" not in point for point in points):
                group["missing"].append({"record_id": bundle["record"]["id"], "index": index})
                continue
            group["images"].add(rows[index]["image_sha256"])
            if kind == "relative_anchor":
                raw = [points[0][k] - points[1][k] for k in ("x", "y")]
                wanted = [finite(value[k], "produced relative anchor") for k in ("dx_px", "dy_px")]
                group["errors"]["vector_px"].append(math.dist(raw, wanted))
            else:
                raw = _pair_geometry(*points)
                for field in ("anchor_distance_px", "edge_distance_px", "overlap_area_px2", "intersection_over_union"):
                    group["errors"][field].append(abs(raw[field] - finite(value[field], "produced pair geometry", minimum=0)))
                group["boolean_disagreements"] += raw["visually_overlapping"] is not value["visually_overlapping"]
    # Every role uses raw reference relative travel; role coordinates cannot
    # increase their own tolerance. Bounds additionally retain a strict 1.5px
    # median edge-distance gate and exact overlap classification.
    for bundle in bundles:
        if not bundle["record"]["reference"]:
            continue
        pairs = {tuple(v["object_ids"]) for v in bundle["event_set"].get("relations", []) if v["type"] in ("relative_anchor", "object_pair_geometry")}
        for ids in pairs:
            values = [[r["points"][ids[0]][k] - r["points"][ids[1]][k] for k in ("x", "y")]
                for r in bundle["rows"] if all("x" in r["points"].get(oid, {}) for oid in ids)]
            if values:
                travel[ids].append(math.hypot(*(max(v[i] for v in values) - min(v[i] for v in values) for i in (0, 1))))
    result = []
    for key, group in sorted(groups.items()):
        extent = max(travel[key[2]], default=None)
        limits = {"vector_px": max(1.5, extent * .005) if extent is not None else None,
                  "anchor_distance_px": max(1.5, extent * .005) if extent is not None else None,
                  "edge_distance_px": 1.5, "overlap_area_px2": 0., "intersection_over_union": 0.}
        medians = {field: statistics.median(values) for field, values in group.pop("errors").items()}
        count = len(group.pop("images"))
        passed = extent is not None and count >= 20 and not group["missing"] and not group["boolean_disagreements"] and all(
            limits[field] is not None and error <= limits[field] + 1e-9 for field, error in medians.items())
        group.update(raw_distinct_images=count, reference_relative_motion_travel_px=extent, median_errors=medians, limits=limits, **{"pass": passed})
        result.append(group)
        if not passed:
            failures.append({"code": "independent_geometry_review_failed", "role": key[0], "relation_type": key[1], "objects": list(key[2]),
                             "raw_distinct_images": count, "median_errors": medians, "limits": limits, "missing": len(group["missing"]),
                             "boolean_disagreements": group["boolean_disagreements"]})
    return result, failures


def review_records(records, output, event_contract=None, *, contact_policy="all_frames"):
    """Separate v2 evidence; every image and gate stays independent and prospective."""
    output = legacy._ordinary(Path(output))
    if output.exists():
        raise VisualError("output_exists", "Preserve prior raw reviews; use a fresh v2 output.")
    if not isinstance(records, (list, tuple)) or not records or contact_policy not in legacy.CONTACT_POLICIES:
        raise VisualError("invalid_review", "Actual recordings and a supported frozen contact policy are required.")
    contract = legacy._public_contract(event_contract)
    public = (event_contract or {}).get("event_options", event_contract or {})
    contract.update(speed_threshold_px_s=finite(public.get("speed_threshold_px_s", 5.), "public reversal speed", minimum=0),
                    overshoot_min_px=finite(public.get("overshoot_min_px", 1.5), "public excursion band", minimum=0),
                    axis=public.get("axis", "auto"))
    if contract["axis"] not in ("auto", "x", "y") or min(contract["speed_threshold_px_s"], contract["overshoot_min_px"]) <= 0:
        raise VisualError("invalid_event_definition", "Positive public limits and a valid axis are required.")
    normalized = [legacy._record(record, i) for i, record in enumerate(records)]
    if len({r["id"] for r in normalized}) != len(normalized):
        raise VisualError("invalid_review", "Unique raw recording IDs are required.")
    if any(output.is_relative_to(r["package"]) or r["package"].is_relative_to(output) for r in normalized):
        raise VisualError("unsafe_path", "Raw output and source packages cannot contain each other.")
    code = legacy._code_inventory() | {str(Path(__file__).resolve()): file_sha256(__file__),
                                     str(Path(mass.__code__.co_filename).resolve()): file_sha256(mass.__code__.co_filename)}
    inventory, bundles, failures, unsupported = {}, [], [], []
    output.mkdir(parents=True)
    for record in normalized:
        if record["domain"] != "panel":
            unsupported.append({"record_id": record["id"], "domain": record["domain"]})
            failures.append({"code": "unsupported_domain", "record_id": record["id"], "domain": record["domain"]})
        else:
            bundles.append(_extract(record, output, inventory, contact_policy))
    write_json(output / "extraction-complete.json", {"all_pixel_extraction_finished_before_measurement_read": True,
        "raw_hashes": {b["record"]["id"]: file_sha256(output / "raw" / b["record"]["id"] / "points.json") for b in bundles}})
    event_results, bindings, mapping = [], [], {}
    for bundle in bundles:
        observations, events = legacy._measurement(bundle["record"], inventory)
        failures.extend(legacy._scope(bundle, observations, events, contract))
        if any(abs(finite(events["options"].get(key), "measured public event option") - contract[key]) > 1e-9
               for key in ("speed_threshold_px_s", "overshoot_min_px")) or events["options"].get("axis", "auto") != contract["axis"]:
            failures.append({"code": "event_definition_mismatch", "record_id": bundle["record"]["id"]})
        for oid, glyph in bundle["mapping"].items():
            if oid in mapping and mapping[oid] != glyph:
                raise VisualError("invalid_review", "Physical glyph identity changed across recordings.")
            mapping[oid] = glyph
        write_json(output / "measurements" / bundle["record"]["id"] / "measurement.json", {"observations": observations, "events": events})
        result, errors = _compare_events(bundle, contract)
        event_results.append(result)
        failures.extend(errors)
        bindings.append({"record_id": bundle["record"]["id"], "role": bundle["record"]["role"],
            "package_sha256": bundle["manifest"]["package_sha256"], "observations_sha256": digest(observations),
            "events_sha256": digest(events), "object_map": bundle["mapping"], "public_active_intervals_s": bundle["active"]})
    for bundle in bundles:
        missing = sorted(set(mapping) - set(bundle["mapping"]))
        if missing:
            failures.append({"code": "missing_selected_objects", "record_id": bundle["record"]["id"], "objects": missing})
    positions, errors = legacy._compare_positions(bundles)
    failures.extend(errors)
    geometry, errors = _compare_geometry(bundles)
    failures.extend(errors)
    if contact_policy == "control_and_event_v1":
        contact = legacy._compact_sheets(bundles, event_results, output)
    else:
        contact = {"kind": "FrozenRawContactSelection", "policy": "all_frames", "controls": [], "selected_images": [
            {"record_id": b["record"]["id"], "role": b["record"]["role"], "index": f["index"], "time_s": f["time_s"],
             "image_sha256": f["sha256"], "source_path": str(path), "reasons": ["all_frames"]}
            for b in bundles for f, path in zip(b["manifest"]["frames"], frame_paths(b["record"]["package"], b["manifest"]))]}
    write_json(output / "contact-selection.json", contact)
    result = {"schema_version": 1, "kind": "ProspectiveUnsignedRawReviewAid", "version": VERSION,
        "status": "pending_visual_review", "numeric_status": "pass" if not failures else "fail", "acceptance_granted": False,
        "visual_inspection_claimed": False, "historical_acceptance_changed": False,
        "production_coordinates_used_for_extraction": False, "smoothing_or_interpolation_used": False,
        "public_event_contract": contract, "public_event_contract_sha256": digest(contract), "contact_policy": contact_policy,
        "contact_selection_sha256": digest(contact), "records": bindings, "positions": positions, "events": event_results,
        "geometry": geometry, "geometry_gate": "Independent soft anchors and raw color bounds; strict separate limits; unsupported relations fail.",
        "sheets": [{"path": name, "sha256": file_sha256(output / name)} for b in bundles for name in b["sheets"]],
        "unsupported": unsupported, "failures": failures,
        "methods": [{"name": "raw_panel_centroid_aid.mass", "status": "unsigned_aid", "input": "raw image, independent HSV color bounds and local background"},
                    {"name": "suite_raw_review_v2.candidates_v3", "status": "unsigned_aid", "input": "independent float raw points and public input/event contract"},
                    {"name": "visual.raw_event_review.relative_timing", "status": "unsigned_aid", "input": "independent event intervals and observed cadence"},
                    {"name": "suite_raw_review_v2._pair_geometry", "status": "unsigned_aid", "input": "independent raw coordinates and HSV bounds"}],
        "limitations": ["Synthetic alpha-translation precision is not proof of accuracy on every real glyph; inspect all required raw sheets.",
                        "First-image physical anchors are chosen from raw HSV midpoints and require explicit visual inspection.",
                        "Any residual event disagreement, missing support or uncertainty remains unqualified.", "SVG/game are unsupported."]}
    write_json(output / "result.json", result)
    write_json(output / "evidence-receipt.json", {"kind": "UnsignedRawReviewEvidenceReceipt", "status": "unsigned", "result_sha256": digest(result),
        "source_files": inventory, "code_sources": code,
        "generated_files": {p.relative_to(output).as_posix(): file_sha256(p) for p in output.rglob("*") if p.is_file()},
        "measurement_bindings_sha256": digest(bindings), "public_event_contract_sha256": digest(contract),
        "contact_policy": contact_policy, "contact_selection_sha256": digest(contact)})
    return {**result, "output": str(output), "receipt_path": str(output / "evidence-receipt.json")}


attest_review = legacy.attest_review
