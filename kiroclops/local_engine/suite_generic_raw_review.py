"""Unsigned game/SVG pixel admission before a model prompt.

Independent glyph, card and stroke estimators see raw images only. Production
positions and events are opened after the complete pixel extraction pass, for
comparison. A numerical pass still needs actual inspection and attestation.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
import importlib.util
from itertools import combinations
import math
from pathlib import Path
import statistics
import sys

import cv2
import numpy as np

from kiroclops.visual import raw_event_review
from kiroclops.visual.package import frame_paths, load_package, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, finite, read_json, safe_path, write_json
from . import suite_raw_review as common
from .session import _ordinary, source_hashes
from scripts.prepare_game_raw_review_aid import anchor as game_anchor
from scripts.raw_panel_centroid_aid import mass
from scripts.draft_panel_scope_review import pair

VERSION = "prospective-game-svg-raw-review-v1"
ROOT = Path(__file__).resolve().parents[2]
MIN_DISTINCT_IMAGES = 20
MAX_UNCERTAINTY_S = .1
SUPPORTED_ROLES = {"reference", "original", "before"}


@lru_cache(maxsize=1)
def _svg_helpers():
    """Load exactly the existing independent SVG sources, never a tracker."""
    directory = ROOT / "artifacts/phase3a/tools"
    modules = []
    for name in ("raw_svg_sequence_aid", "raw_svg_geometry_aid_v4", "raw_motion_event_proposals_v4"):
        path = directory / (name + ".py")
        existing = sys.modules.get(name)
        if existing is not None and Path(existing.__file__).resolve() != path.resolve():
            raise VisualError("helper_source_mismatch", "Independent SVG helper name resolves to another source.")
        if existing is None:
            spec = importlib.util.spec_from_file_location(name, path)
            existing = importlib.util.module_from_spec(spec)
            sys.modules[name] = existing
            spec.loader.exec_module(existing)
        modules.append(existing)
    # Same raw six-node artwork colors declared by the frozen SVG holdout aid.
    modules[0].COLORS.update(e=(72, 9), f=(9, 7))
    return tuple(modules)


def _public_contract(value, domain="game"):
    options = deepcopy((value or {}).get("event_options", value or {}))
    if not isinstance(options, dict):
        raise VisualError("invalid_event_definition", "An explicit public event policy is required.")
    rest = .25 if domain == "svg" else .2
    result = {"motion_policy": options.get("motion_policy", raw_event_review.CONFIRMED),
              "rest_duration_s": finite(options.get("rest_duration_s", rest), "rest duration", minimum=0),
              "max_gap_s": finite(options.get("max_gap_s", .12), "maximum gap", minimum=0),
              "rest_radius_px": finite(options.get("rest_radius_px", 1.5), "rest radius", minimum=0),
              "event_tolerance_s": finite((value or {}).get("event_tolerance_s", .05), "event tolerance", minimum=0),
              "max_event_uncertainty_s": options.get("max_event_uncertainty_s", MAX_UNCERTAINTY_S),
              "geometry_tolerance_px": 2., "minimum_distinct_images": MIN_DISTINCT_IMAGES,
              "game_event_tolerance_frames": 2, "game_position_tolerance": "max(1px, 0.05*declared_body_height)"}
    if (result["motion_policy"] != raw_event_review.CONFIRMED or abs(result["rest_duration_s"] - rest) > 1e-9
            or result["max_gap_s"] != .12 or result["rest_radius_px"] != 1.5
            or result["event_tolerance_s"] != .05 or result["max_event_uncertainty_s"] != MAX_UNCERTAINTY_S):
        raise VisualError("unsupported_event_definition", "Raw admission requires the frozen v3, 50ms/100ms and 2px policy.")
    return result


def _raw_image(image, domain):
    """The complete marking boundary: no production value is an argument."""
    if domain == "game":
        found = game_anchor(image)
        point = found[0] if len(found) == 1 else {"status": "ambiguous" if found else "not_detected", "count": len(found)}
        return {"points": {"character": point}, "objects": {}, "connections": {}}
    anchors, geometry, _ = _svg_helpers()
    bounds, points = anchors.raw_anchors(image), {}
    for oid, value in bounds.items():
        if "x" not in value:
            points[oid] = deepcopy(value)
            continue
        try:
            x, y = mass(image, value)
            points[oid] = {"x": x, "y": y, "color_bounds": value["color_bounds"], "status": "unsigned"}
        except ValueError as error:
            points[oid] = {"status": "ambiguous", "reason": str(error)}
    selected = geometry.proposals(image)
    for oid, obj in selected["objects"].items():
        if "x" in points.get(oid, {}):
            obj["anchor"] = [points[oid]["x"], points[oid]["y"]]
    return {"points": points, **selected}


def _extract(record, output, inventory):
    manifest = load_package(record["package"], verify="hashes")
    inventory[str(record["package"] / "manifest.json")] = file_sha256(record["package"] / "manifest.json")
    if record.get("package_sha256") not in (None, manifest["package_sha256"]):
        raise VisualError("wrong_result", "Raw package differs from the bound recording.")
    rows = []
    for frame, path in zip(manifest["frames"], frame_paths(record["package"], manifest)):
        raw = _raw_image(read_image(path), record["domain"])
        actual = file_sha256(path)
        if actual != frame["sha256"]:
            raise VisualError("source_changed", "Raw source changed during independent extraction.")
        inventory[str(path)] = actual
        rows.append({"index": frame["index"], "time_s": frame["time_s"], "image_sha256": actual, **raw})
    write_json(output / "raw" / record["id"] / "points.json", {"rows": rows,
        "package_sha256": manifest["package_sha256"], "production_coordinates_used": False,
        "production_events_used": False, "target_code_or_dom_read": False, "status": "unsigned"})
    return {"record": record, "manifest": manifest, "raw": {"rows": rows}, "sheets": []}


def _producer_inventory(record, inventory):
    """Seal the already-verified native bridge and its bounded producer files."""
    if not record.get("raw_run"):
        return
    raw = _ordinary(record["raw_run"])
    manifest = read_json(raw / "manifest.json")
    for name in ("manifest.json", "recording.json", "capture_config.json"):
        path = raw / name
        if path.is_file():
            inventory[str(path)] = file_sha256(path)
    for frame in manifest["frames"]:
        index = frame["frame_index"]
        path = _ordinary(raw / "frames" / f"{index:06d}.png")
        inventory[str(path)] = file_sha256(path)
    provenance = manifest.get("provenance", {})
    for key in ("runtime_receipt", "process_receipt"):
        value = provenance.get(key)
        if not value:
            continue
        path = _ordinary(value)
        inventory[str(path)] = file_sha256(path)
        receipt = read_json(path)
        for relative, expected in receipt.get("evidence_hashes", {}).items():
            evidence_path = _ordinary(safe_path(path.parent, relative))
            sha = file_sha256(evidence_path)
            if sha != expected:
                raise VisualError("source_changed", "Native runtime evidence changed during raw review.")
            inventory[str(evidence_path)] = sha
        # Process evidence is enumerated by its already-verified owned receipt.
        for relative, entry in receipt.get("evidence", {}).items():
            evidence_path = _ordinary(safe_path(path.parent, relative))
            sha = file_sha256(evidence_path)
            if not isinstance(entry, dict) or entry.get("sha256") != sha:
                raise VisualError("source_changed", "Owned native process evidence changed during raw review.")
            inventory[str(evidence_path)] = sha
        for relative in ("process-start.json", "process-identity.json", "process/process.json"):
            evidence_path = path.parent / relative
            if evidence_path.is_file():
                inventory[str(_ordinary(evidence_path))] = file_sha256(evidence_path)


def _scope(bundle, inventory, contract):
    from . import evidence
    record, manifest = bundle["record"], bundle["manifest"]
    # The extraction pass is already complete for every recording.
    observations, events = evidence.observations(record), evidence.event_set(record)
    if not isinstance(observations, dict) or not isinstance(events, dict):
        raise VisualError("invalid_review", "Actual observations and EventSet are required after raw extraction.")
    for key in ("analysis", "observations_path"):
        if record.get(key):
            path = _ordinary(record[key]); inventory[str(path)] = file_sha256(path)
    if record.get("analysis"):
        directory = Path(record["analysis"]).parent
        for path in (*directory.glob("*.json"), *directory.joinpath("common-analysis").glob("*.json")):
            inventory[str(path.resolve())] = file_sha256(path)
    if (observations.get("package_sha256") != manifest["package_sha256"] or observations.get("inputs") != manifest["inputs"]
            or observations.get("image_size") != manifest["image_size"] or events.get("package_sha256") != manifest["package_sha256"]
            or events.get("observation_sha256") != digest(observations)):
        raise VisualError("wrong_result", "Produced measurement belongs to different pixels, inputs or observations.")
    selection = record.get("selection", observations.get("selection"))
    if isinstance(selection, (str, Path)):
        path = _ordinary(selection); inventory[str(path)] = file_sha256(path); selection = read_json(path)
    if selection != observations.get("selection"):
        raise VisualError("wrong_result", "Public selection differs from the measured selection.")
    ids = {obj["id"] for obj in observations["objects"]}
    mapping = record.get("object_map") or record.get("intake_binding", {}).get("object_map") or {oid: oid for oid in ids}
    allowed = {"character"} if record["domain"] == "game" else set("abcdef")
    if (not isinstance(mapping, dict) or set(mapping) != ids or len(set(mapping.values())) != len(mapping)
            or not set(mapping.values()) <= allowed):
        raise VisualError("invalid_review", "Map each selected object to a distinct supported raw glyph.")
    times = [frame["time_s"] for frame in manifest["frames"]]
    active = {oid: common._active(selection, oid, times) for oid in mapping}
    rows = [{**row, "points": {oid: deepcopy(row["points"].get(glyph, {"status": "not_detected"}))
                              for oid, glyph in mapping.items()},
             "objects": {oid: deepcopy(row["objects"][glyph]) for oid, glyph in mapping.items() if glyph in row["objects"]}}
            for row in bundle["raw"]["rows"]]
    failures = []
    measured_options = events.get("options", {})
    if (events.get("motion_start_policy", {}).get("version") != contract["motion_policy"] or any(
            measured_options.get(key) != contract[key] for key in ("rest_duration_s", "rest_radius_px", "max_gap_s", "max_event_uncertainty_s"))):
        failures.append({"code": "event_definition_mismatch", "record_id": record["id"]})
    for obj in observations["objects"]:
        frames = obj.get("frames", [])
        if len(frames) != len(times) or any(f.get("index") != index or f.get("time_s") != times[index] for index, f in enumerate(frames)):
            raise VisualError("wrong_result", "Produced frame identity differs from raw images.")
    # Optional producer expectations come only from the explicit frozen suite,
    # never from a candidate's self-reported source.
    if record.get("expected_source") is not None:
        expected = record["expected_source"]
        required_kind = "native" if record["domain"] == "game" else "web"
        if expected.get("kind") != required_kind or record.get("source_sha256") != digest(expected.get("session_sources", expected.get("sources"))):
            raise VisualError("wrong_baseline", "Producer expectations must bind the explicit original or authoring-parent source.")
        # The review normalizer intentionally uses Path for safe local reads;
        # the immutable evidence ABI requires the producer package as a string.
        # Convert only the view passed to that boundary, never the declarations.
        analysis_record = {**record, "package": str(record["package"])}
        if record["domain"] == "game":
            analysis_record["analysis"] = str(Path(record["analysis"]).parent / "common-analysis/analysis.json")
        evidence._record_observation(analysis_record, record["scenario"], "web" if record["domain"] == "svg" else record["scenario"]["mechanic"],
            measured=True, expected_source=expected)
        if record.get("source_project"):
            project = _ordinary(record["source_project"])
            sources = expected.get("session_sources", expected["sources"])
            if source_hashes(project) != sources:
                raise VisualError("source_changed", "Explicit comparison source changed during raw review.")
            for relative, sha in sources.items():
                inventory[str(_ordinary(safe_path(project, relative)))] = sha
    elif record.get("reference"):
        if record["domain"] == "game":
            from .suite_game import verify_reference_record
            verify_reference_record(record, record["scenario"]["mechanic"])
            checked = {**record, "package": str(record["package"]),
                       "analysis": str(Path(record["analysis"]).parent / "common-analysis/analysis.json")}
            evidence._record_observation(checked, record["scenario"], "media", measured=True,
                reference_package=manifest["package_sha256"], expected_source={"kind": "uploaded"})
            if record.get("raw_run"):
                projection = read_json(record["analysis"])
                if (projection.get("candidate_sha256") is not None or projection.get("scenario") != record["scenario"]
                        or projection.get("provenance", {}).get("observation_sha256") != digest(observations)
                        or record.get("motion_events") != projection.get("events")):
                    raise VisualError("measurement_binding_changed", "Frozen game projection differs from its actual common worker pixels.")
        elif record.get("intake_binding"):
            from .suite_intake_binding import verify_reference_record
            verify_reference_record(record)
    else:
        raise VisualError("needs_source_binding", "Original and authoring-parent roles require explicit producer source expectations.")
    _producer_inventory(record, inventory)
    bundle.update(observations=observations, event_set=events, mapping=mapping, rows=rows,
                  active=active, selection=selection)
    return failures


def _distance(point, bounds):
    l, t, r, b = bounds; x, y = point
    return min(x-l, r-x, y-t, b-y) if l <= x <= r and t <= y <= b else math.hypot(max(l-x, 0, x-r), max(t-y, 0, y-b))


def _raw_events(bundle, contract):
    _, _, helper = _svg_helpers()
    result = []
    groups = [(oid,) for oid in bundle["mapping"]]
    groups += [(r["object_id"], r["relative_to"]) for r in bundle["selection"].get("relative_motion", [])]
    for group in groups:
        result.extend(helper.candidates(bundle["rows"], list(group), contract["rest_duration_s"],
            active_intervals_s=bundle["active"], max_gap_s=contract["max_gap_s"]))
    relations, previous_pairs, previous_edges = [], {}, {}
    if bundle["record"]["domain"] == "svg":
        for row in bundle["rows"]:
            active_objects = {oid: obj for oid, obj in row["objects"].items() if any(a <= row["time_s"] <= b for a, b in bundle["active"][oid])}
            for left, right in combinations(sorted(active_objects), 2):
                a, b = active_objects[left], active_objects[right]
                boxes = [{"anchor": obj["anchor"], "box": [obj["bounds"][0], obj["bounds"][1],
                         obj["bounds"][2]-obj["bounds"][0], obj["bounds"][3]-obj["bounds"][1]]} for obj in (a, b)]
                geometry = pair(*boxes)
                relations.append({"type": "object_pair_geometry", "object_ids": [left, right], "index": row["index"], **geometry})
                prior = previous_pairs.get((left, right))
                if prior is not None and prior["index"] + 1 == row["index"] and prior["overlap"] != geometry["visually_overlapping"]:
                    result.append({"type": "overlap_enter" if geometry["visually_overlapping"] else "overlap_leave",
                                   "object_ids": [left, right], "indices": [prior["index"], row["index"]]})
                previous_pairs[left, right] = {"index": row["index"], "overlap": geometry["visually_overlapping"]}
            for edge in bundle["selection"].get("connections", []):
                a, b = edge["from_object"], edge["to_object"]
                if a not in active_objects or b not in active_objects:
                    previous_edges.pop(edge["id"], None)
                    continue
                first, second = bundle["mapping"][a], bundle["mapping"][b]
                observed = row["connections"].get(first+"-"+second)
                reversed_edge = False
                if observed is None:
                    observed = row["connections"].get(second+"-"+first); reversed_edge = True
                if observed is None:
                    relations.append({"type": "connection_attachment", "connection_id": edge["id"], "index": row["index"], "status": "unobservable"})
                    previous_edges.pop(edge["id"], None)
                    continue
                ends = [observed["end"], observed["start"]] if reversed_edge else [observed["start"], observed["end"]]
                distances = [_distance(endpoint, active_objects[oid]["bounds"]) for endpoint, oid in zip(ends, (a, b))]
                attached = max(distances) <= 2.
                relations.append({"type": "connection_attachment", "connection_id": edge["id"], "index": row["index"],
                                  "endpoint_distances_px": distances, "visually_attached": attached})
                prior = previous_edges.get(edge["id"])
                if (not attached and (prior is None or prior["attached"])) or (attached and prior is not None and not prior["attached"]):
                    result.append({"type": "connection_attached" if attached else "connection_detached", "object_ids": [a, b],
                                   "connection_id": edge["id"], "indices": [prior["index"] if prior else row["index"], row["index"]]})
                previous_edges[edge["id"]] = {"index": row["index"], "attached": attached}
    frames = bundle["manifest"]["frames"]
    for event in result:
        a, b = [frames[index] for index in event["indices"]]
        event["observed_interval_s"] = [a.get("time_interval_s", [a["time_s"], a["time_s"]])[0],
                                         b.get("time_interval_s", [b["time_s"], b["time_s"]])[1]]
        event["uncertainty_s"] = event["observed_interval_s"][1] - event["observed_interval_s"][0]
        event["timing_status"] = "resolved" if event["uncertainty_s"] <= MAX_UNCERTAINTY_S + 1e-9 else "unresolved"
    return sorted(result, key=lambda e: (e["observed_interval_s"], e["type"], e["object_ids"])), relations


def _event_key(event):
    ids = list(event.get("object_ids", []))
    if event.get("relative_to_object_id") and len(ids) == 1:
        ids.append(event["relative_to_object_id"])
    return event["type"], tuple(ids), event.get("connection_id"), event.get("boundary_id")


def _compare_events(bundle, contract):
    raw, raw_relations = _raw_events(bundle, contract)
    produced = bundle["event_set"]
    pools, expected = defaultdict(list), defaultdict(list)
    checks, failures = [], []
    for event in raw:
        pools[_event_key(event)].append(event)
        if event["timing_status"] != "resolved":
            failures.append({"code": "raw_event_uncertainty_exceeds_limit", "record_id": bundle["record"]["id"], "event_type": event["type"]})
    for event in produced.get("events", []):
        expected[_event_key(event)].append(event)
    for key in sorted(set(pools) | set(expected), key=repr):
        source = sorted(expected[key], key=lambda e: e["time_interval_s"])
        actual = pools[key]
        if len(source) != len(actual):
            failures.append({"code": "event_occurrence_mismatch", "record_id": bundle["record"]["id"],
                             "event_type": key[0], "objects": list(key[1]), "produced": len(source), "raw": len(actual)})
        for occurrence, (value, independent) in enumerate(zip(source, actual)):
            interval = common._interval(value["time_interval_s"], "produced event")
            difference = max(abs(a-b) for a, b in zip(interval, independent["observed_interval_s"]))
            width = max(interval[1]-interval[0], value.get("uncertainty_s", 0), independent["uncertainty_s"])
            passed = difference <= .05+1e-9 and width <= .1+1e-9 and value.get("timing_status") == "resolved"
            checks.append({"type": key[0], "objects": list(key[1]), "occurrence": occurrence, "produced_interval_s": interval,
                           "raw_interval_s": independent["observed_interval_s"], "difference_s": difference,
                           "max_uncertainty_s": width, "pass": passed})
            if not passed:
                failures.append({"code": "event_interval_or_uncertainty_disagreement", "record_id": bundle["record"]["id"], "event_type": key[0]})
    lookup = {(r["type"], tuple(r.get("object_ids", [])), r.get("connection_id"), r.get("index")): r for r in raw_relations}
    relation_checks = []
    cadence = [b["time_s"]-a["time_s"] for a, b in zip(bundle["rows"], bundle["rows"][1:])]
    for relation in produced.get("relations", []):
        kind = relation["type"]
        if kind == "relative_event_timing":
            parent = relation.get("relative_to_object_id")
            values = [pools[(relation["event_type"], tuple([oid]+([parent] if parent else [])), None, None)]
                      for oid in relation["object_ids"]]
            occurrence = relation.get("occurrence", 0)
            actual = raw_event_review.relative_timing(*(v[occurrence]["observed_interval_s"] for v in values), cadence) if all(len(v) > occurrence for v in values) else None
            passed = actual is not None and actual["order"] == relation.get("order") and actual["timing_status"] == relation.get("timing_status") == "resolved"
            passed = passed and actual["uncertainty_s"] <= .1 and relation.get("uncertainty_s", float("inf")) <= .1
            passed = passed and max(abs(a-b) for a, b in zip(actual["lag_interval_s"], relation["lag_interval_s"])) <= .05+1e-9
        else:
            actual = lookup.get((kind, tuple(relation.get("object_ids", [])), relation.get("connection_id"), relation.get("index")))
            passed = actual is not None and actual.get("status") != "unobservable"
            numeric = {"object_pair_geometry": ("anchor_distance_px", "edge_distance_px", "overlap_area_px2", "intersection_over_union"),
                       "connection_attachment": ("endpoint_distances_px",)}.get(kind)
            if numeric is None:
                passed = False
            elif passed:
                for field in numeric:
                    wanted, observed = relation[field], actual[field]
                    differences = [abs(a-b) for a, b in zip(wanted, observed)] if isinstance(wanted, list) else [abs(wanted-observed)]
                    limit = .02 if field == "intersection_over_union" else 2.
                    passed = passed and max(differences) <= limit+1e-9
                flag = "visually_attached" if kind == "connection_attachment" else "visually_overlapping"
                passed = passed and actual.get(flag) == relation.get(flag)
        relation_checks.append({"type": kind, "index": relation.get("index"), "objects": relation.get("object_ids"),
                                "connection_id": relation.get("connection_id"), "raw": actual, "pass": bool(passed)})
        if not passed:
            failures.append({"code": "relation_unobservable_or_disagreement", "record_id": bundle["record"]["id"], "relation_type": kind})
    return {"record_id": bundle["record"]["id"], "raw_events": raw, "event_checks": checks,
            "relation_checks": relation_checks, "raw_relations": raw_relations}, failures


def _jump(bundle):
    """Image-only baseline, sustained takeoff, plateau apex and stable return."""
    if bundle["record"]["domain"] != "game" or bundle["record"]["scenario"].get("mechanic") != "jump":
        return None, []
    rows = bundle["rows"]; oid = next(iter(bundle["mapping"]))
    inputs = bundle["manifest"]["inputs"]["events"]
    starts = [e["time_s"] for e in inputs if e.get("action") == "jump" and e.get("pressed") is True]
    points = [r["points"][oid].get("y") for r in rows]
    failure = lambda code: (None, [{"code": code, "record_id": bundle["record"]["id"]}])
    if len(starts) != 1 or any(type(v) not in (int, float) or not math.isfinite(v) for v in points):
        return failure("jump_raw_unobservable")
    pre = [p for r, p in zip(rows, points) if r["time_s"] < starts[0]]
    if len(pre) < 3 or max(pre)-min(pre) > 2.:
        return failure("jump_raw_ground_uncertain")
    low, high = min(pre), max(pre)
    takeoff = next((i-1 for i in range(1, len(rows)-1) if rows[i]["time_s"] >= starts[0]
                    and points[i] < low-.5 and points[i+1] < low-.5), None)
    if takeoff is None:
        return failure("jump_raw_takeoff_unobservable")
    landing = next((i for i in range(takeoff+3, len(rows)-2) if all(low-.5 <= y <= high+.5 for y in points[i:i+3])), None)
    if landing is None:
        return failure("jump_raw_landing_unobservable")
    peak = min(points[takeoff+1:landing])
    plateau = [i for i in range(takeoff+1, landing) if points[i]-peak <= .25]
    apex = plateau[len(plateau)//2]
    raw = {"takeoff_frame": takeoff, "apex_frame": apex, "landing_frame": landing}
    produced = bundle["record"].get("motion_events")
    if not isinstance(produced, dict) and bundle["record"].get("analysis"):
        produced = read_json(bundle["record"]["analysis"]).get("events")
    checks = {}
    frames = bundle["manifest"]["frames"]
    for name, index in raw.items():
        wanted = (produced or {}).get(name)
        checks[name] = {"raw_frame": index, "produced_frame": wanted, "pass": type(wanted) is int and 0 <= wanted < len(rows)
            and abs(index-wanted) <= 2
            and max(frames[index].get("uncertainty_s", 0), frames[wanted].get("uncertainty_s", 0)) <= .1}
    failures = [] if all(c["pass"] for c in checks.values()) and takeoff < apex < landing else [{"code": "jump_raw_event_disagreement", "record_id": bundle["record"]["id"]}]
    return {"record_id": bundle["record"]["id"], "raw": raw, "checks": checks}, failures


def _positions(bundles):
    positions, failures = common._compare_positions(bundles)
    if bundles[0]["record"]["domain"] != "game":
        return positions, failures
    # Preserve the established native review's body-scaled pixel gate. The
    # browser's 1.5px/travel threshold must not loosen native game controls.
    for group in positions:
        heights = {obj.get("body_height_px", b["record"].get("body_height_px"))
                   for b in bundles if b["record"]["role"] == group["role"]
                   for obj in b["selection"]["objects"] if obj["id"] == group["object_id"]}
        valid = len(heights) == 1 and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in heights)
        tolerance = max(1., .05*next(iter(heights))) if valid else None
        passed = (valid and group["raw_distinct_images"] >= 20 and group["paired_distinct_images"] >= 20
                  and not group["missing_or_ambiguous"] and group["median_error_px"] is not None
                  and group["median_error_px"] <= tolerance+1e-9)
        group.update(tolerance_px=tolerance, pass_=bool(passed), tolerance_policy="max(1px, 0.05*declared_body_height)")
        group["pass"] = group.pop("pass_")
        if not passed:
            failures.append({"code": "game_position_review_failed", "role": group["role"], "object_id": group["object_id"],
                             "median_error_px": group["median_error_px"], "tolerance_px": tolerance})
    return positions, failures


def _tile(image, frame, name, row, domain):
    tile = np.full((230, 600, 3), 24, np.uint8)
    cv2.putText(tile, f"{name} f{frame['index']} {frame['time_s']:.4f}s", (5, 16), cv2.FONT_HERSHEY_SIMPLEX, .4, (240, 240, 240), 1)
    overlay = image.copy()
    for value in row["points"].values():
        if "x" in value:
            cv2.drawMarker(overlay, (round(value["x"]), round(value["y"])), (50, 255, 50), cv2.MARKER_CROSS, 7, 1)
    for obj in row["objects"].values():
        l, t, r, b = obj["bounds"]; cv2.rectangle(overlay, (l, t), (r-1, b-1), (50, 255, 50), 1)
    for edge in row["connections"].values():
        for point in (edge["start"], edge["end"]):
            cv2.circle(overlay, tuple(np.rint(point).astype(int)), 2, (0, 0, 255), 1)
    for number, raw in enumerate((image, overlay)):
        scale = min(290/raw.shape[1], 195/raw.shape[0])
        resized = cv2.resize(raw, (round(raw.shape[1]*scale), round(raw.shape[0]*scale)), interpolation=cv2.INTER_NEAREST if domain == "game" else cv2.INTER_AREA)
        tile[27:27+resized.shape[0], 5+number*300:5+number*300+resized.shape[1]] = resized
    return tile


def _sheets(bundles, event_results, jump_results, output):
    selected, reasons, pool = defaultdict(set), defaultdict(set), defaultdict(dict)
    controls = []
    by_id = {b["record"]["id"]: b for b in bundles}
    def near(name, indices, reason):
        count = len(by_id[name]["rows"])
        for index in indices:
            for value in (index-1, index, index+1):
                if 0 <= value < count:
                    selected[name].add(value); reasons[name, value].add(reason)
    for bundle in bundles:
        name = bundle["record"]["id"]
        for row in bundle["rows"]:
            for oid in bundle["mapping"]:
                if not any(a <= row["time_s"] <= b for a, b in bundle["active"][oid]):
                    continue
                if "x" in row["points"][oid]:
                    pool[bundle["record"]["role"], oid].setdefault(row["image_sha256"], {"record_id": name,
                        "role": bundle["record"]["role"], "object_id": oid, "index": row["index"], "image_sha256": row["image_sha256"]})
                else:
                    near(name, [row["index"]], "raw_ambiguity")
        near(name, [0, len(bundle["rows"])-1], "clip_boundary")
        times = [row["time_s"] for row in bundle["rows"]]
        for event in bundle["manifest"]["inputs"]["events"]:
            near(name, [min(range(len(times)), key=lambda i: abs(times[i]-event["time_s"]))], "public_input")
    for result in event_results:
        for event in result["raw_events"]:
            near(result["record_id"], event["indices"]+event.get("support_indices", []), "independent_raw_event")
    for result in jump_results:
        if result:
            near(result["record_id"], list(result["raw"].values()), "independent_jump_event")
    for entries in pool.values():
        values = list(entries.values())
        for index in sorted({round(i) for i in np.linspace(0, len(values)-1, min(20, len(values)))}):
            row = values[index]; controls.append(row); near(row["record_id"], [row["index"]], "distinct_raw_control")
    sheets, identities = [], []
    for bundle in bundles:
        name = bundle["record"]["id"]; paths = frame_paths(bundle["record"]["package"], bundle["manifest"])
        tiles = []
        for index in sorted(selected[name]):
            frame = bundle["manifest"]["frames"][index]
            image = read_image(paths[index])
            if file_sha256(paths[index]) != frame["sha256"]:
                raise VisualError("source_changed", "Raw sheet source changed.")
            tiles.append(_tile(image, frame, name, bundle["rows"][index], bundle["record"]["domain"]))
            identities.append({"record_id": name, "role": bundle["record"]["role"], "index": index,
                               "image_sha256": frame["sha256"], "reasons": sorted(reasons[name, index])})
        for page, start in enumerate(range(0, len(tiles), 12)):
            batch = tiles[start:start+12]; batch.extend(np.full((230, 600, 3), 24, np.uint8) for _ in range((-len(batch)) % 3))
            relative = f"sheets/{name}-{page:03}.png"
            common._save_image(output / relative, np.vstack([np.hstack(batch[i:i+3]) for i in range(0, len(batch), 3)]))
            sheets.append({"path": relative, "sha256": file_sha256(output / relative)})
    selection = {"kind": "FrozenRawContactSelection", "policy": "control_and_event_v1", "controls": controls,
                 "selected_images": identities, "minimum_distinct_control_images_per_object_role": 20,
                 "production_coordinates_or_events_used": False, "all_frames_extracted_hashed_and_numerically_checked": True}
    write_json(output / "contact-selection.json", selection)
    return sheets, selection


def _code_sources():
    from kiroclops.visual import package, png, schema
    from . import session, evidence, suite_evidence, suite_game, suite_intake_binding, media_session, capture_modes
    from scripts import prepare_game_raw_review_aid, raw_panel_centroid_aid, draft_panel_scope_review
    modules = (package, png, schema, session, evidence, suite_evidence, suite_game, suite_intake_binding, media_session, capture_modes,
               raw_event_review, common, prepare_game_raw_review_aid,
               raw_panel_centroid_aid, draft_panel_scope_review, *_svg_helpers())
    return {str(Path(m.__file__).resolve()): file_sha256(m.__file__) for m in modules} | {str(Path(__file__).resolve()): file_sha256(__file__)}


def review_records(records, output, event_contract=None, *, contact_policy="control_and_event_v1"):
    output = _ordinary(output)
    if output.exists():
        raise VisualError("output_exists", "Preserve prior raw reviews; use a fresh directory.")
    if not records or contact_policy != "control_and_event_v1":
        raise VisualError("invalid_review", "Supply actual records and the frozen control/event contact policy.")
    normalized = [common._record(row, i) for i, row in enumerate(records)]
    domains = {r["domain"] for r in normalized}
    if len(domains) != 1 or not domains <= {"game", "svg"} or len({r["id"] for r in normalized}) != len(normalized):
        raise VisualError("unsupported_domain", "Review one game or SVG domain with unique recording identities.")
    if {r["role"] for r in normalized} != SUPPORTED_ROLES:
        raise VisualError("incomplete_review", "Review calibration references, original and authoring-parent recordings together.")
    if any(output.is_relative_to(r["package"]) or r["package"].is_relative_to(output) for r in normalized):
        raise VisualError("unsafe_path", "Review output must remain separate from source images.")
    contract = _public_contract(event_contract, next(iter(domains)))
    code_sources = _code_sources(); inventory = {}
    output.mkdir(parents=True)
    bundles = [_extract(r, output, inventory) for r in normalized]
    write_json(output / "extraction-complete.json", {"all_pixel_extraction_finished_before_measurement_comparison": True,
        "raw_hashes": {b["record"]["id"]: file_sha256(output / "raw" / b["record"]["id"] / "points.json") for b in bundles}})
    bindings, failures, event_results, jump_results, common_mapping = [], [], [], [], {}
    for bundle in bundles:
        failures.extend(_scope(bundle, inventory, contract))
        for oid, glyph in bundle["mapping"].items():
            if oid in common_mapping and common_mapping[oid] != glyph:
                raise VisualError("invalid_review", "One public object changed raw glyph identity between source roles.")
            common_mapping[oid] = glyph
        write_json(output / "measurements" / bundle["record"]["id"] / "measurement.json",
                   {"observations": bundle["observations"], "events": bundle["event_set"]})
        events, errors = _compare_events(bundle, contract); event_results.append(events); failures.extend(errors)
        jump, errors = _jump(bundle); jump_results.append(jump); failures.extend(errors)
        bindings.append({"record_id": bundle["record"]["id"], "role": bundle["record"]["role"],
            "package_sha256": bundle["manifest"]["package_sha256"], "observations_sha256": digest(bundle["observations"]),
            "events_sha256": digest(bundle["event_set"]), "source_sha256": bundle["record"].get("source_sha256"),
            "object_map": bundle["mapping"], "public_active_intervals_s": bundle["active"]})
    for bundle in bundles:
        if set(bundle["mapping"]) != set(common_mapping):
            failures.append({"code": "missing_selected_objects", "record_id": bundle["record"]["id"]})
    positions, errors = _positions(bundles); failures.extend(errors)
    sheets, contact = _sheets(bundles, event_results, jump_results, output)
    result = {"kind": "ProspectiveUnsignedGenericRawReviewAid", "schema_version": 1, "version": VERSION,
              "domain": next(iter(domains)), "status": "pending_visual_review", "numeric_status": "pass" if not failures else "fail",
              "acceptance_granted": False, "visual_inspection_claimed": False, "historical_acceptance_changed": False,
              "production_coordinates_used_for_extraction": False, "smoothing_or_interpolation_used": False,
              "public_event_contract": contract, "public_event_contract_sha256": digest(contract),
              "contact_policy": contact_policy, "contact_selection_sha256": digest(contact),
              "records": bindings, "positions": positions, "events": event_results,
              "jump_events": [v for v in jump_results if v], "sheets": sheets, "unsupported": [], "failures": failures,
              "limitations": ["Artwork-specific unsigned helpers require actual contact-sheet inspection.",
                              "No application, model, production tracker or production event extraction is run by this aid.",
                              "Hashes bind local evidence; they are not a digital signature or final behavior acceptance."]}
    write_json(output / "result.json", result)
    receipt = {"kind": "UnsignedRawReviewEvidenceReceipt", "status": "unsigned", "result_sha256": digest(result),
               "source_files": inventory, "code_sources": code_sources,
               "generated_files": {p.relative_to(output).as_posix(): file_sha256(p) for p in output.rglob("*") if p.is_file()},
               "measurement_bindings_sha256": digest(bindings), "public_event_contract_sha256": digest(contract),
               "contact_policy": contact_policy, "contact_selection_sha256": digest(contact)}
    write_json(output / "evidence-receipt.json", receipt)
    return {**result, "output": str(output), "receipt_path": str(output / "evidence-receipt.json")}


def review_suite(suite_directory, output, *, authoring_parent):
    """Bind exactly every frozen calibration case and both initial source roles."""
    from .media_suite import load_suite, record_from_suite
    from .suite_evidence import _source_expectation
    suite_directory, authoring_parent = _ordinary(suite_directory), _ordinary(authoring_parent)
    suite = load_suite(suite_directory)
    calibration = [c for c in suite["cases"] if c["role"] == "calibration"]
    expected = {(role, c["case_id"]) for c in calibration for role in ("original", "before")}
    initial = suite.get("initial_records", [])
    if {(c["role"], c["case_id"]) for c in initial} != expected or len(initial) != len(expected):
        raise VisualError("incomplete_review", "All original and authoring-parent calibration captures are required.")
    original = safe_path(suite_directory, suite["original"], must_exist=False)
    expectations = {"original": _source_expectation(original, suite["original_source_hashes"], suite["domain"]),
                    "before": _source_expectation(authoring_parent, suite["authoring_parent_source_hashes"], suite["domain"])}
    rows = [{"id": "reference-"+c["case_id"], "case_id": c["case_id"], "domain": suite["domain"], "role": "reference",
             "reference": True, "object_map": c.get("object_map"), "record": record_from_suite(suite_directory, c)} for c in calibration]
    for c in initial:
        source = record_from_suite(suite_directory, c)
        case = next(case for case in calibration if case["case_id"] == c["case_id"])
        if source.get("source_sha256") != digest(suite["original_source_hashes"] if c["role"] == "original" else suite["authoring_parent_source_hashes"]):
            raise VisualError("wrong_baseline", "Initial source role changed before raw review.")
        rows.append({"id": c["role"]+"-"+c["case_id"], "case_id": c["case_id"], "domain": suite["domain"], "role": c["role"],
                     "reference": False, "object_map": case.get("object_map"), "expected_source": expectations[c["role"]],
                     "source_project": str(original if c["role"] == "original" else authoring_parent), "record": source})
    return review_records(rows, output, suite.get("event_contract"))


def attest_review(output, *, reviewer, inspected_sheets, accepted, images_inspected=False):
    """Record a real reviewer's explicit inspection; numeric failure cannot pass."""
    return common.attest_review(output, reviewer=reviewer, inspected_sheets=inspected_sheets,
                               accepted=accepted, images_inspected=images_inspected)


def validate_review(output, suite_directory, suite):
    """Read-only readiness hook; verify exact calibration and initial matrices."""
    from .evidence import observations
    from .media_suite import record_from_suite
    output = _ordinary(output)
    try:
        result, receipt, attestation = (read_json(output / name) for name in ("result.json", "evidence-receipt.json", "attestation.json"))
    except OSError as error:
        raise VisualError("environment_not_ready", "A complete inspected game/SVG raw review is required.") from error
    if (result.get("kind") != "ProspectiveUnsignedGenericRawReviewAid" or result.get("version") != VERSION
            or result.get("domain") != suite["domain"] or result.get("numeric_status") != "pass" or result.get("failures")
            or result.get("public_event_contract") != _public_contract(suite.get("event_contract"), suite["domain"])
            or attestation.get("kind") != "ProspectiveRawVisualReviewAttestation" or attestation.get("status") != "pass"
            or attestation.get("images_inspected") is not True or attestation.get("accepted") is not True
            or attestation.get("result_sha256") != receipt.get("result_sha256") or receipt.get("result_sha256") != digest(result)
            or attestation.get("evidence_receipt_sha256") != file_sha256(output / "evidence-receipt.json")
            or attestation.get("inspected_sheets") != result.get("sheets") or not result.get("sheets")):
        raise VisualError("environment_not_ready", "Game/SVG raw review is pending, failed or uses a different frozen policy.")
    for name, expected in {**receipt["source_files"], **receipt["code_sources"]}.items():
        if file_sha256(_ordinary(name)) != expected:
            raise VisualError("environment_not_ready", "Raw source pixels, measurements or helpers changed.")
    for name, expected in receipt["generated_files"].items():
        if file_sha256(safe_path(output, name)) != expected:
            raise VisualError("environment_not_ready", "Independent marks or contact sheets changed.")
    calibration = [c for c in suite["cases"] if c["role"] == "calibration"]
    expected_initial = {(role, c["case_id"]) for c in calibration for role in ("original", "before")}
    initial = suite.get("initial_records", [])
    if {(c["role"], c["case_id"]) for c in initial} != expected_initial or len(initial) != len(expected_initial):
        raise VisualError("environment_not_ready", "Initial calibration source matrix is incomplete.")
    required = []
    for role, cases in (("reference", calibration), (None, initial)):
        for case in cases:
            record = record_from_suite(suite_directory, case)
            required.append((role or case["role"], load_package(record["package"], verify="hashes")["package_sha256"],
                             digest(observations(record)), record.get("source_sha256")))
    actual = [(r["role"], r["package_sha256"], r["observations_sha256"], r.get("source_sha256")) for r in result["records"]]
    if len(actual) != len(required) or sorted(actual, key=repr) != sorted(required, key=repr):
        raise VisualError("environment_not_ready", "Raw review omits or replaces a required calibration recording or source role.")
    return {"directory": str(output), "attestation_sha256": file_sha256(output / "attestation.json"),
            "result_sha256": digest(result), "required_record_count": len(required)}
