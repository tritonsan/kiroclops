"""Unsigned panel geometry calibration from raw paint and public first ROIs.

The panel's visible painted contour is segmented again in every image. Glyph
selection padding is calibrated once from public ROIs and raw pixels. Float
anchors use a declared raw photometric policy and strict v3/.20 event definition.
No production positions, boxes, events, target source or DOM enter extraction.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
from pathlib import Path

import cv2
import numpy as np

from kiroclops.visual.package import load_package, frame_paths, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, finite, read_json, safe_path, write_json
from . import suite_raw_review as legacy
from . import suite_raw_review_v2 as v2

VERSION = "prospective-panel-painted-geometry-raw-review-v3"
ANCHOR_POLICIES = {"local_background_v2", "first_palette_mixture_v1"}


def _anchor_definition(policy):
    definitions = {
        "local_background_v2": {"name": "local_background_v2", "method": "raw_panel_centroid_aid.mass",
            "background": "Per-image raw local border median", "color_residual_budget": 35.},
        "first_palette_mixture_v1": {"name": "first_palette_mixture_v1", "method": "suite_raw_review_v3.mixture_anchors",
            "foreground": "Public RGB with first raw-image opaque support", "background": "At most four raw first-selected-image paint layers",
            "mixture": "Independent per-pixel least-squares C=B+alpha(F-B), minimum Euclidean RGB residual",
            "mixture_residual_budget_rgb": 2., "first_palette_minimum_fraction": .01,
            "mass_change_limit": .08, "crop_pad_px": 4}}
    definition = definitions[policy] | {"calibration": "First actual raw image only", "quantization": "none",
        "temporal_smoothing": False, "production_coordinates_or_times_used": False,
        "selection_fixed_before_pixel_extraction": True}
    return definition


def _mixture_weights(pixels, foreground, backgrounds):
    """Independent per-pixel least-squares solution to C=B+alpha(F-B).

    First-image background colors are competing explanations of each pixel;
    the minimum Euclidean RGB residual chooses its observed paint layer.
    A 2-byte residual budget covers native 8-bit RGB quantization. This never
    chooses weights from motion/timing or an already produced coordinate.
    """
    values = pixels.astype(float)
    best, weights = np.full(pixels.shape[:2], np.inf), np.zeros(pixels.shape[:2], float)
    for background in backgrounds:
        background = np.asarray(background, float)
        vector = foreground-background
        if float(vector @ vector) < 32**2:
            continue
        alpha = np.clip(np.sum((values-background)*vector, axis=2)/(vector @ vector), 0., 1.)
        error = np.linalg.norm(values-background-alpha[..., None]*vector, axis=2)
        use = error < best
        best[use], weights[use] = error[use], alpha[use]
    return np.where(best <= 2., weights, 0.)


def _learn_mixture(image, spec):
    x, y, w, h = _roi(spec.get("roi"), image.shape)
    rgb = (spec.get("pixel_anchor") or {}).get("foreground_rgb")
    if not isinstance(rgb, (list, tuple)) or len(rgb) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in rgb):
        raise VisualError("needs_public_selection", "The independent mixture policy needs a public observed foreground RGB.")
    foreground = np.asarray(rgb[::-1], float)
    crop = image[y:y+h, x:x+w]
    if int((np.max(abs(crop.astype(float)-foreground), axis=2) <= 2).sum()) < 9:
        raise VisualError("ambiguous_raw_anchor", "The first raw image does not support the public foreground paint.")
    colors, counts = np.unique(crop.reshape(-1, 3), axis=0, return_counts=True)
    palette = []
    for index in np.argsort(-counts, kind="stable"):
        color = colors[index].astype(float)
        if counts[index] < max(2, math.ceil(w*h*.01)) or np.linalg.norm(color-foreground) < 32:
            continue
        if palette and _mixture_weights(color.reshape(1, 1, 3), foreground, palette)[0, 0] > 0:
            continue  # AA blends are not new physical background layers.
        # A pure existing background has alpha=0 and must also be excluded.
        if any(np.linalg.norm(color-np.asarray(background)) <= 2 for background in palette):
            continue
        palette.append(color.tolist())
        if len(palette) == 4:
            break
    if not palette:
        raise VisualError("ambiguous_raw_anchor", "No first-image separable observed background palette.")
    return {"foreground_bgr": foreground.tolist(), "background_palette_bgr": palette,
            "mixture_residual_budget_rgb": 2., "method": "first_image_observed_layer_mixture_least_squares"}


def _mixture_center(image, point, calibration):
    gx, gy, gw, gh = point["color_bounds"]
    left, top, right, bottom = max(0, gx-4), max(0, gy-4), min(image.shape[1], gx+gw+4), min(image.shape[0], gy+gh+4)
    weights = _mixture_weights(image[top:bottom, left:right], np.asarray(calibration["foreground_bgr"]), calibration["background_palette_bgr"])
    total = float(weights.sum())
    if total < 9 or min(left, top) == 0 or right == image.shape[1] or bottom == image.shape[0]:
        raise VisualError("ambiguous_raw_anchor", "Raw foreground mass is clipped or insufficient.")
    if float(weights[0].sum()+weights[-1].sum()+weights[1:-1, 0].sum()+weights[1:-1, -1].sum()) > .1:
        raise VisualError("ambiguous_raw_anchor", "The independent glyph crop has paint at its edge.")
    yy, xx = np.indices(weights.shape, dtype=float)
    return [float((xx*weights).sum()/total+left), float((yy*weights).sum()/total+top)], total


def mixture_anchors(image, public_selection, origin=None, *, object_map=None):
    """Unsmoothed float anchors from frozen first-image paint layers only."""
    points, origin = v2.raw_anchors(image), deepcopy(origin or {})
    specs = public_selection.get("objects", [])
    mapping = object_map or {spec["id"]: spec["id"] for spec in specs}
    for spec in specs:
        glyph = mapping.get(spec.get("id"))
        point = points[glyph]
        if "x" not in point:
            continue
        try:
            if glyph not in origin:
                if origin.get("_first_image_complete"):
                    raise VisualError("ambiguous_raw_anchor", "The first raw anchor calibration was missing.")
                paint = _learn_mixture(image, spec)
                center, total = _mixture_center(image, point, paint)
                origin[glyph] = {"anchor": [point["x"], point["y"]], "centroid": center,
                                 "foreground_mass": total, "paint": paint}
            first = origin[glyph]
            center, total = _mixture_center(image, point, first["paint"])
            if abs(total/first["foreground_mass"]-1.) > .08:
                raise VisualError("ambiguous_raw_anchor", "The observed fixed illustration mass changed.")
            points[glyph] = {"x": first["anchor"][0]+center[0]-first["centroid"][0],
                "y": first["anchor"][1]+center[1]-first["centroid"][1], "color_bounds": point["color_bounds"],
                "independent_mass_centroid": center, "foreground_mass": total,
                "subpixel_method": "first_image_observed_layer_mixture_least_squares", "quantization": "none", "status": "unsigned"}
        except VisualError as exc:
            points[glyph] = {"status": "ambiguous", "reason": exc.code, "detail": str(exc)}
    origin["_first_image_complete"] = True
    return points, origin


def _roi(value, shape):
    if not isinstance(value, (tuple, list)) or len(value) != 4 or any(type(v) is not int for v in value):
        raise VisualError("needs_public_selection", "Geometry requires a public integer first-image ROI.")
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > shape[1] or y + h > shape[0]:
        raise VisualError("needs_public_selection", "The public first-image ROI is clipped or empty.")
    return x, y, w, h


def _dominant_palette(pixels, *, count=2, minimum_fraction=.1):
    colors, counts = np.unique(pixels.reshape(-1, 3), axis=0, return_counts=True)
    order = np.argsort(-counts, kind="stable")
    selected = [colors[i].astype(float).tolist() for i in order[:count] if counts[i] >= len(pixels.reshape(-1, 3)) * minimum_fraction]
    if not selected:
        raise VisualError("ambiguous_raw_geometry", "No independently visible panel fill color.")
    return selected


def _learn_panel(image, roi):
    x, y, w, h = _roi(roi, image.shape)
    if min(x, y) < 1 or x + w >= image.shape[1] or y + h >= image.shape[0]:
        raise VisualError("ambiguous_raw_geometry", "Panel outline lacks an unclipped first-image annulus.")
    palette = _dominant_palette(image[y:y+h, x:x+w])
    # Central sides exclude rounded corners and all internal glyphs/text.
    xa, xb, ya, yb = x + w//4, x + 3*w//4, y + h//4, y + 3*h//4
    sides = np.concatenate((image[y-1, xa:xb], image[y+h, xa:xb], image[ya:yb, x-1], image[ya:yb, x+w]))
    colors, counts = np.unique(sides, axis=0, return_counts=True)
    winner = int(counts.argmax())
    if counts[winner] < .5 * len(sides):
        raise VisualError("ambiguous_raw_geometry", "The first-image outline has no common observed paint color.")
    return {"initial_fill_roi": list(roi), "fill_palette_bgr": palette,
            "outline_bgr": colors[winner].astype(float).tolist(),
            "method": "independent_connected_fill_and_observed_outline_scan",
            "fill_color_tolerance": 3., "outline_mixture_residual": 3.,
            "outline_min_visible_contrast": 10., "minimum_side_support_fraction": .75}


def panel_paint_bounds(image, raw_handle, calibration):
    """Segment the current filled/outlined card, without predicted body boxes.

    The raw handle selects a containing connected contour. Dimensions may
    change; no initial width/height is transported to produce the result.
    Native image coverage bounds use integer pixel edges, independently of the
    unsmoothed floating anchor. Missing/clipped/ambiguous paint stays unsigned.
    """
    if "x" not in raw_handle:
        raise VisualError("ambiguous_raw_geometry", "The panel identity glyph is not observable.")
    pixels = image.astype(np.int16)
    mask = np.zeros(image.shape[:2], np.uint8)
    for color in calibration["fill_palette_bgr"]:
        mask |= (np.max(abs(pixels - np.asarray(color, np.int16)), axis=2) <= calibration["fill_color_tolerance"]).astype(np.uint8)
    # Tiny internal ink/AA cuts do not disconnect the painted body. This closes
    # only 1px gaps; it neither fills exterior margins nor predicts motion.
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    initial = calibration["initial_fill_roi"]
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w*h < initial[2]*initial[3]*.25 or cv2.contourArea(contour) / max(1., (w-1)*(h-1)) < .85:
            continue
        if x <= raw_handle["x"] < x+w and y <= raw_handle["y"] < y+h:
            candidates.append([x, y, w, h])
    if len(candidates) != 1:
        raise VisualError("ambiguous_raw_geometry", "Panel raw identity does not select exactly one visible painted contour.")
    x, y, w, h = candidates[0]
    height, width = image.shape[:2]
    if min(x, y) <= 0 or x+w >= width or y+h >= height:
        raise VisualError("ambiguous_raw_geometry", "The current painted panel contour is clipped.")
    outline = np.asarray(calibration["outline_bgr"], float)
    xa, xb, ya, yb = x+w//4, x+3*w//4, y+h//4, y+3*h//4

    def painted(line, fill):
        delta, paint = outline - fill.astype(float), line.astype(float) - fill.astype(float)
        denominator = np.sum(delta*delta, axis=1)
        alpha = np.sum(paint*delta, axis=1) / np.maximum(1., denominator)
        residual = np.linalg.norm(paint - alpha[:, None]*delta, axis=1)
        visible = ((alpha > 0) & (alpha <= 1.25) & (residual <= calibration["outline_mixture_residual"]) &
                   (np.max(abs(paint), axis=1) >= calibration["outline_min_visible_contrast"]))
        return len(line) > 0 and np.mean(visible) >= calibration["minimum_side_support_fraction"]

    left, top, right, bottom = x, y, x+w, y+h
    for column in range(x-1, max(-1, x-9), -1):
        if not painted(image[ya:yb, column], image[ya:yb, x+2]):
            break
        left = column
    for column in range(x+w, min(width, x+w+8)):
        if not painted(image[ya:yb, column], image[ya:yb, x+w-3]):
            break
        right = column+1
    for row in range(y-1, max(-1, y-9), -1):
        if not painted(image[row, xa:xb], image[y+2, xa:xb]):
            break
        top = row
    for row in range(y+h, min(height, y+h+8)):
        if not painted(image[row, xa:xb], image[y+h-3, xa:xb]):
            break
        bottom = row+1
    return [left, top, right-left, bottom-top]


def raw_body_geometry(image, points, public_selection, origin=None, *, object_map=None):
    """Return geometry proposals using only raw glyphs and first public ROIs.

    Fixed glyph illustrations retain their selected first-image padding and
    move by independently observed float color mass. A changed native glyph
    silhouette of more than 2 AA pixels is an explicit unresolved shape gate;
    scaling illustrations need a separate qualified estimator. The card body
    is measured from its actual visible fill and outline in each image.
    """
    origin, result = deepcopy(origin or {}), deepcopy(points)
    if not isinstance(public_selection, dict) or not isinstance(public_selection.get("objects"), list):
        raise VisualError("needs_public_selection", "Pass the bound public selection separately before pixel extraction.")
    specs = public_selection["objects"]
    mapping = object_map or {s["id"]: s["id"] for s in specs}
    for spec in specs:
        glyph = mapping.get(spec.get("id"))
        if glyph not in legacy.PANEL_OBJECTS:
            raise VisualError("needs_public_selection", "Public selection needs a known raw glyph identity.")
        point = result[glyph]
        if "x" not in point:
            point.update(geometry_status="ambiguous", geometry_reason="raw_identity_unobservable")
            continue
        try:
            if glyph not in origin:
                if origin.get("_first_image_complete"):
                    raise VisualError("ambiguous_raw_geometry", "No body calibration was observable in the first image.")
                x, y, w, h = _roi(spec.get("roi"), image.shape)
                gx, gy, gw, gh = point["color_bounds"]
                if gx < x or gy < y or gx+gw > x+w or gy+gh > y+h:
                    raise VisualError("ambiguous_raw_geometry", "First raw glyph pixels escape their public selected extent.")
                learned = {"raw_anchor": [point["x"], point["y"]], "raw_color_bounds": [gx, gy, gw, gh],
                    "selected_roi": [x, y, w, h], "padding_px": [gx-x, gy-y, x+w-gx-gw, y+h-gy-gh],
                    "method": "first_raw_glyph_public_padding_and_independent_mass_translation"}
                if isinstance(spec.get("geometry"), dict):
                    if glyph != "handle":
                        raise VisualError("unsupported_geometry", "Only the independent handle/card body estimator is qualified here.")
                    learned["panel"] = _learn_panel(image, spec["geometry"].get("roi"))
                origin[glyph] = learned
            first = origin[glyph]
            if "panel" in first:
                bounds = panel_paint_bounds(image, point, first["panel"])
                method = first["panel"]["method"]
            else:
                if any(abs(point["color_bounds"][i] - first["raw_color_bounds"][i]) > 2 for i in (2, 3)):
                    raise VisualError("ambiguous_raw_geometry", "Fixed glyph silhouette changed beyond native AA coverage.")
                x, y, w, h = first["selected_roi"]
                bounds = [x + point["x"]-first["raw_anchor"][0], y + point["y"]-first["raw_anchor"][1], w, h]
                method = first["method"]
            point.update(geometry_bounds=bounds, geometry_status="unsigned", geometry_method=method)
        except VisualError as exc:
            point.update(geometry_status="ambiguous", geometry_reason=exc.code, geometry_detail=str(exc))
    origin["_first_image_complete"] = True
    return result, origin


def _tile(image, frame, record_id, raw):
    tile = legacy._tile(image, frame, record_id, raw)
    scale = min(270 / image.shape[1], 165 / image.shape[0])
    for glyph, point in raw.items():
        if "geometry_bounds" not in point:
            continue
        x, y, w, h = point["geometry_bounds"]
        cv2.rectangle(tile, (3+round(x*scale), 22+round(y*scale)),
                      (3+round((x+w)*scale), 22+round((y+h)*scale)), (150, 240, 190), 1)
    return tile


def _extract(record, output, inventory, contact_policy, anchor_policy):
    manifest = load_package(record["package"])
    inventory[str(record["package"] / "manifest.json")] = file_sha256(record["package"] / "manifest.json")
    if record.get("package_sha256") not in (None, manifest["package_sha256"]):
        raise VisualError("wrong_result", "Recording binding differs from actual raw pixels.")
    # Deliberately no fallback to observations.selection: extraction accepts
    # only the caller's public selection, before any measurement is read.
    selection = deepcopy(record.get("selection"))
    if not isinstance(selection, dict):
        raise VisualError("needs_public_selection", "Provide each bound public first-image selection explicitly.")
    rows, origin, geometry_origin, tiles, sheets = [], None, None, [], []

    def page():
        relative = f"sheets/{record['id']}-{len(sheets):03}.png"
        tiles.extend(np.full((210, 400, 3), 24, np.uint8) for _ in range((-len(tiles)) % 4))
        legacy._save_image(output / relative, np.vstack([np.hstack(tiles[i:i+4]) for i in range(0, len(tiles), 4)]))
        sheets.append(relative)
        tiles.clear()

    for frame, path in zip(manifest["frames"], frame_paths(record["package"], manifest)):
        image = read_image(path)
        if anchor_policy == "first_palette_mixture_v1":
            points, origin = mixture_anchors(image, selection, origin, object_map=record.get("object_map"))
        else:
            points, origin = v2.subpixel_anchors(image, origin)
        points, geometry_origin = raw_body_geometry(image, points, selection, geometry_origin, object_map=record.get("object_map"))
        if file_sha256(path) != frame["sha256"]:
            raise VisualError("source_changed", "A raw image changed during independent geometry extraction.")
        inventory[str(path)] = frame["sha256"]
        rows.append({"index": frame["index"], "time_s": frame["time_s"], "image_sha256": frame["sha256"], "points": points})
        if contact_policy == "all_frames":
            tiles.append(_tile(image, frame, record["id"], points))
            if len(tiles) == 20:
                page()
    if tiles:
        page()
    raw = {"kind": "UnsignedIndependentPaintedPanelProposals", "status": "unsigned", "version": VERSION,
        "package_sha256": manifest["package_sha256"], "rows": rows, "first_image_origins": origin,
        "anchor_policy": anchor_policy,
        "anchor_policy_definition_sha256": digest(_anchor_definition(anchor_policy)),
        "first_image_sha256": manifest["frames"][0]["sha256"],
        "first_image_geometry_calibration": geometry_origin, "public_selection_sha256": digest(selection),
        "method": "Independent float raw glyph anchors; raw painted panel contours; first-public-ROI glyph padding.",
        "production_coordinates_used": False, "production_bounds_used": False, "production_events_used": False,
        "smoothing_or_rounding_used": False, "target_code_or_dom_read": False, "sheets": sheets}
    write_json(output / "raw" / record["id"] / "points.json", raw)
    return {"record": record, "manifest": manifest, "raw": raw, "sheets": sheets}


def _compare_geometry(bundles):
    adapted, missing = deepcopy(bundles), []
    for bundle in adapted:
        for row in bundle["rows"]:
            for oid, point in row["points"].items():
                if "geometry_bounds" in point:
                    point["color_bounds"] = point["geometry_bounds"]
                else:
                    # Keep the separate anchor-vector gate; absent body geometry
                    # must not turn a valid anchor into a geometry pass.
                    if any(a <= row["time_s"] <= b for a, b in bundle.get("active", {}).get(oid, [[-math.inf, math.inf]])):
                        missing.append({"code": "raw_body_geometry_unobservable", "record_id": bundle["record"]["id"],
                            "object_id": oid, "index": row["index"], "reason": point.get("geometry_reason", "missing_raw_geometry")})
                    # A missing body cannot reuse the small glyph silhouette.
                    point.pop("x", None)
    # v2's compare routine reads color_bounds only for pair-body geometry. Feed
    # the independently scoped body boxes, never a production measured box.
    vectors = deepcopy(bundles)
    for bundle in vectors:
        bundle["event_set"]["relations"] = [value for value in bundle["event_set"].get("relations", []) if value["type"] == "relative_anchor"]
    for bundle in adapted:
        bundle["event_set"]["relations"] = [value for value in bundle["event_set"].get("relations", []) if value["type"] == "object_pair_geometry"]
    vector_groups, vector_errors = v2._compare_geometry(vectors)
    groups, errors = v2._compare_geometry(adapted)
    groups, errors = vector_groups + groups, vector_errors + errors
    if missing:
        for group in groups:
            if group["type"] == "object_pair_geometry":
                group["pass"] = False
        errors.extend(missing)
    return groups, errors


def _compact_sheets(bundles, event_results, output):
    # Stable v1 selection considers only independent raw events/public inputs.
    # Re-render selected sheets with v3's independently measured body overlay.
    contact = legacy._compact_sheets(bundles, event_results, output)
    identities = {(item["record_id"], item["index"]): item for item in contact["selected_images"]}
    for bundle in bundles:
        for row in bundle["rows"]:
            if not any(point.get("geometry_status") == "ambiguous" for point in row["points"].values()):
                continue
            for index in (row["index"]-1, row["index"], row["index"]+1):
                if not 0 <= index < len(bundle["manifest"]["frames"]):
                    continue
                frame = bundle["manifest"]["frames"][index]
                key = bundle["record"]["id"], index
                if key not in identities:
                    identities[key] = {"record_id": key[0], "role": bundle["record"]["role"], "index": index,
                        "time_s": frame["time_s"], "image_sha256": frame["sha256"],
                        "source_path": str(frame_paths(bundle["record"]["package"], bundle["manifest"])[index]), "reasons": []}
                identities[key]["reasons"] = sorted(set(identities[key]["reasons"]) | {"raw_body_geometry_ambiguity_neighborhood"})
    contact["selected_images"] = sorted(identities.values(), key=lambda item: (item["record_id"], item["index"]))
    selected = defaultdict(list)
    for item in contact["selected_images"]:
        selected[item["record_id"]].append(item["index"])
    for bundle in bundles:
        tiles, sheets = [], []
        manifest, record = bundle["manifest"], bundle["record"]
        paths = frame_paths(record["package"], manifest)
        for index in selected[record["id"]]:
            frame, path = manifest["frames"][index], paths[index]
            image = read_image(path)
            if file_sha256(path) != frame["sha256"]:
                raise VisualError("source_changed", "Selected raw geometry image changed.")
            tiles.append(_tile(image, frame, record["id"], bundle["raw"]["rows"][index]["points"]))
            if len(tiles) == 20:
                name = f"sheets/{record['id']}-{len(sheets):03}.png"
                legacy._save_image(output / name, np.vstack([np.hstack(tiles[i:i+4]) for i in range(0, 20, 4)]))
                sheets.append(name)
                tiles = []
        if tiles:
            tiles.extend(np.full((210, 400, 3), 24, np.uint8) for _ in range((-len(tiles)) % 4))
            name = f"sheets/{record['id']}-{len(sheets):03}.png"
            legacy._save_image(output / name, np.vstack([np.hstack(tiles[i:i+4]) for i in range(0, len(tiles), 4)]))
            sheets.append(name)
        bundle["sheets"] = sheets
    contact["raw_geometry_overlay"] = "Independent painted body and public first-image glyph extent; no produced box."
    return contact


def review_records(records, output, event_contract=None, *, contact_policy="all_frames", anchor_policy="local_background_v2"):
    """Create fresh v3 evidence, preserving numeric failures and the human gate."""
    output = legacy._ordinary(Path(output))
    if output.exists():
        raise VisualError("output_exists", "Preserve prior raw reviews; use a fresh v3 output.")
    if not isinstance(records, (list, tuple)) or not records or contact_policy not in legacy.CONTACT_POLICIES or anchor_policy not in ANCHOR_POLICIES:
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
    if any(r["domain"] == "panel" and not isinstance(r.get("selection"), dict) for r in normalized):
        raise VisualError("needs_public_selection", "Pass the bound public selection separately before extraction.")
    code = legacy._code_inventory() | {str(Path(module.__file__).resolve()): file_sha256(module.__file__) for module in (v2,)} | {
        str(Path(__file__).resolve()): file_sha256(__file__), str(Path(v2.mass.__code__.co_filename).resolve()): file_sha256(v2.mass.__code__.co_filename)}
    inventory, bundles, failures, unsupported = {}, [], [], []
    output.mkdir(parents=True)
    policy_receipt = {"kind": "FrozenProspectiveRawPolicy", "anchor_policy": anchor_policy,
        "anchor_definition": _anchor_definition(anchor_policy), "public_event_contract": contract,
        "geometry": "Independent per-image painted contour and fixed-public-first-ROI glyph padding",
        "selected_before_pixel_extraction": True, "production_coordinates_or_event_times_used_for_selection": False}
    write_json(output / "policy-selection.json", policy_receipt)
    for record in normalized:
        if record["domain"] != "panel":
            unsupported.append({"record_id": record["id"], "domain": record["domain"]})
            failures.append({"code": "unsupported_domain", "record_id": record["id"], "domain": record["domain"]})
        else:
            bundles.append(_extract(record, output, inventory, contact_policy, anchor_policy))
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
        result, errors = v2._compare_events(bundle, contract)
        event_results.append(result)
        failures.extend(errors)
        bindings.append({"record_id": bundle["record"]["id"], "role": bundle["record"]["role"],
            "case_id": bundle["record"].get("scenario", {}).get("case_id", bundle["record"].get("scenario", {}).get("id")),
            "source_sha256": bundle["record"].get("source_sha256"),
            "package_sha256": bundle["manifest"]["package_sha256"], "observations_sha256": digest(observations),
            "events_sha256": digest(events), "object_map": bundle["mapping"], "public_active_intervals_s": bundle["active"],
            "public_selection_sha256": bundle["raw"]["public_selection_sha256"]})
    for bundle in bundles:
        missing = sorted(set(mapping) - set(bundle["mapping"]))
        if missing:
            failures.append({"code": "missing_selected_objects", "record_id": bundle["record"]["id"], "objects": missing})
    positions, errors = legacy._compare_positions(bundles)
    failures.extend(errors)
    geometry, errors = _compare_geometry(bundles)
    failures.extend(errors)
    if contact_policy == "control_and_event_v1":
        contact = _compact_sheets(bundles, event_results, output)
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
        "anchor_policy": anchor_policy,
        "policy_selection_sha256": digest(policy_receipt),
        "contact_selection_sha256": digest(contact), "records": bindings, "positions": positions, "events": event_results,
        "geometry": geometry, "geometry_gate": "Independent painted card extent; first-public-ROI fixed-glyph padding; unchanged strict separate limits.",
        "sheets": [{"path": name, "sha256": file_sha256(output / name)} for b in bundles for name in b["sheets"]],
        "unsupported": unsupported, "failures": failures,
        "methods": [{"name": "suite_raw_review_v3.mixture_anchors" if anchor_policy == "first_palette_mixture_v1" else "suite_raw_review_v2.subpixel_anchors",
                     "status": "unsigned_aid", "input": "raw image and declared public first-image palette policy"},
                    {"name": "suite_raw_review_v3.raw_body_geometry", "status": "unsigned_aid", "input": "raw image, raw glyphs, bound public first ROIs"},
                    {"name": "suite_raw_review_v2.candidates_v3", "status": "unsigned_aid", "input": "independent float raw points and public input/event contract"}],
        "limitations": ["The unchanged float anchor/event estimator may disagree near the strict 1.5px rest threshold; every disagreement remains unqualified.",
                        "Synthetic fixtures qualify the helper, not every real glyph or contour; inspect every required sheet.",
                        "Glyph selected padding is fixed by the public first ROI; changed illustration scale/shape remains unresolved.",
                        "SVG/game are unsupported. No held-out observations were used to calibrate this aid."]}
    result["admission"] = _admission_proposal(result, bundles)
    write_json(output / "result.json", result)
    write_json(output / "evidence-receipt.json", {"kind": "UnsignedRawReviewEvidenceReceipt", "status": "unsigned", "result_sha256": digest(result),
        "source_files": inventory, "code_sources": code,
        "generated_files": {p.relative_to(output).as_posix(): file_sha256(p) for p in output.rglob("*") if p.is_file()},
        "measurement_bindings_sha256": digest(bindings), "public_event_contract_sha256": digest(contract),
        "contact_policy": contact_policy, "contact_selection_sha256": digest(contact),
        "anchor_policy": anchor_policy, "anchor_policy_definition_sha256": digest(_anchor_definition(anchor_policy)),
        "policy_selection_sha256": digest(policy_receipt), "runtime_libraries": {"opencv": cv2.__version__, "numpy": np.__version__}})
    return {**result, "output": str(output), "receipt_path": str(output / "evidence-receipt.json")}


attest_review = legacy.attest_review


ADMISSION_POLICY = "panel-premodel-scoring-scope-v2"
ORIGINAL_EVENT_DIAGNOSTICS = frozenset({
    "raw_event_uncertainty_exceeds_limit", "raw_peak_uncertainty_exceeds_limit",
    "produced_event_uncertainty_exceeds_limit", "unresolved_produced_event",
    "produced_peak_uncertainty_exceeds_limit", "unresolved_produced_peak",
    "unsupported_event_type", "event_occurrence_mismatch", "peak_interval_disagreement",
    "event_interval_disagreement", "missing_raw_relation_support",
    "relation_uncertainty_exceeds_limit", "unresolved_timing_relation", "relation_disagreement"})


def _metric_inventory():
    """Byte-exact closure of the comparator, input windows, dynamics and profile."""
    root = Path(__file__).resolve().parents[2]
    paths = {root / name for name in ("kiroclops/local_engine/suite_web.py", "kiroclops/local_engine/evaluation.py",
        "kiroclops/local_engine/feedback.py", "kiroclops/local_engine/geometry_acceptance.py")}
    # Comparison imports visual sampling, events, geometry and schema. Bind all
    # bounded module sources so a new dependency cannot escape the identity.
    paths.update((root / "kiroclops/visual").glob("*.py"))
    paths.update((root / "kiroclops/web_local").glob("*.py"))
    paths.add(root / "kiroclops/common.py")
    return {str(path.resolve()): file_sha256(path) for path in sorted(paths)}


def _scored_projection(value):
    """The scored/admission fields; keep baseline event diagnostics separate."""
    fields = ("pass", "score", "per_window_gates", "baseline_measurement_pass", "measurement_pass",
              "dynamics", "relative_timing", "required_order_status", "failure_class")
    return {field: deepcopy(value.get(field)) for field in fields}


def _baseline_event_diagnostics(value):
    old = value.get("original_pixel_comparison") or {}
    return {field: deepcopy(old.get(field)) for field in ("event_checks", "preservation_checks", "errors", "success", "status")}


def prove_original_events_unconsumed(reference, original, before):
    """Actual comparator dependency check over one calibration triple, no pixels.

    This is a bounded prospective proof for the current frozen comparator and
    these exact records. It is not a claim about future metrics or other data.
    Alter/delete stored original events and separately intercept derived events
    in visual.comparison ONLY for the exact original observation identity.
    Coordinates, inputs, reference/before events, source/status and policy stay
    fixed. The scored projection must remain identical. Original diagnostic
    event checks can change; both their differences and actual interception
    counts are retained explicitly, without accepting these measurements.
    """
    from .suite_web import compare_records
    from .evaluation import _profile
    from kiroclops.visual import comparison
    from unittest.mock import patch
    records = [deepcopy(record) for record in (reference, original, before)]
    expected = compare_records(*records)
    expected_score = _scored_projection(expected)
    expected_diagnostics = _baseline_event_diagnostics(expected)
    changes = [None, {}, {"kind": "UnqualifiedEventDiagnostic", "events": [{"type": "motion_start",
        "object_ids": ["bookmark"], "time_interval_s": [-10000., 10000.], "timing_status": "unresolved"}],
        "relations": [{"type": "relative_event_timing", "order": "after", "uncertainty_s": 20000.}]}]
    checks = []
    for index, changed in enumerate(changes):
        modified = deepcopy(records[1])
        if changed is None:
            modified.pop("events", None)
        else:
            modified["events"] = changed
        value = compare_records(records[0], modified, records[2])
        score, diagnostic = _scored_projection(value), _baseline_event_diagnostics(value)
        checks.append({"mutation": ("delete_original_events", "empty_original_events", "replace_with_unresolved_reversed_events")[index],
                       "scoring_sha256": digest(score), "identical": score == expected_score,
                       "whole_output_identical": value == expected,
                       "baseline_event_diagnostics_sha256": digest(diagnostic),
                       "baseline_event_diagnostics_changed": diagnostic != expected_diagnostics})
    original_observed = records[1]["observations"]
    original_identity = (original_observed.get("package_sha256"), digest(original_observed))
    if any((record["observations"].get("package_sha256"), digest(record["observations"])) == original_identity for record in (records[0], records[2])):
        raise VisualError("metric_dependency_unproven", "Original observations must have a distinct package/observation identity for a role-specific event intervention.")
    extract = comparison.extract_events
    derived_checks = []
    for mutation in ("absent_original_derived_events", "shifted_original_derived_events", "unresolved_original_derived_events"):
        intercepted, passed_through = [], []

        def intervene(observed, *args, **kwargs):
            identity = (observed.get("package_sha256"), digest(observed))
            events = extract(observed, *args, **kwargs)
            if identity != original_identity:
                passed_through.append({"package_sha256": identity[0], "observations_sha256": identity[1]})
                return events
            altered = deepcopy(events)
            if mutation == "absent_original_derived_events":
                altered.update(events=[], relations=[], unobservable=[])
            elif mutation == "shifted_original_derived_events":
                for event in altered.get("events", []):
                    for field in ("time_interval_s", "peak_time_interval_s"):
                        if field in event:
                            event[field] = [stamp+10. for stamp in event[field]]
                for relation in altered.get("relations", []):
                    if "lag_interval_s" in relation:
                        relation["lag_interval_s"] = [stamp+10. for stamp in relation["lag_interval_s"]]
            else:
                for event in altered.get("events", []):
                    event.update(timing_status="unresolved", uncertainty_s=20.)
                    if "time_interval_s" in event:
                        event["time_interval_s"] = [event["time_interval_s"][0]-10., event["time_interval_s"][1]+10.]
                for relation in altered.get("relations", []):
                    if relation.get("type") == "relative_event_timing":
                        relation.update(timing_status="unresolved", uncertainty_s=20.)
            intercepted.append({"package_sha256": identity[0], "observations_sha256": identity[1],
                "unaltered_events_sha256": digest(events), "intervened_events_sha256": digest(altered)})
            return altered

        with patch.object(comparison, "extract_events", side_effect=intervene):
            value = compare_records(*records)
        score, diagnostic = _scored_projection(value), _baseline_event_diagnostics(value)
        derived_checks.append({"mutation": mutation, "scoring_sha256": digest(score), "identical": score == expected_score,
            "whole_output_identical": value == expected, "original_extractor_interventions": intercepted,
            "original_extractor_intervention_count": len(intercepted), "other_observations_passed_through": passed_through,
            "baseline_event_diagnostics": diagnostic, "baseline_event_diagnostics_sha256": digest(diagnostic),
            "baseline_event_diagnostics_changed": diagnostic != expected_diagnostics,
            "observation_sha256_after": digest(records[1]["observations"]),
            "observations_and_inputs_unchanged": digest(records[1]["observations"]) == original_identity[1]})
    # Profile is intentionally reference-only; no original is passed to it.
    # Its stored events are mandatory raw-qualified reference measurements.
    profile = _profile([records[0]])
    def serializable(value):
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {key: serializable(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [serializable(item) for item in value]
        return value

    return {"kind": "ProspectiveMetricDependencyBinding", "schema_version": 2, "policy": ADMISSION_POLICY,
        "status": "pass" if all(check["identical"] for check in checks+derived_checks) else "fail",
        "metric_sources": _metric_inventory(), "scoring_sha256": digest(expected_score),
        "scored_projection": expected_score, "scored_projection_fields": list(expected_score),
        "baseline_event_diagnostics": expected_diagnostics, "baseline_event_diagnostics_sha256": digest(expected_diagnostics),
        "original_event_mutations": checks, "original_derived_event_mutations": derived_checks,
        "original_observation_identity": {"package_sha256": original_identity[0], "observations_sha256": original_identity[1]},
        "reference_profile_sha256": digest(profile), "profile_input_roles": ["reference"],
        "record_sha256": {role: digest(serializable(record)) for role, record in zip(("reference", "original", "before"), records)},
        "input_windows": "Recorder-verified input timeline and observed geometry; original event timing is independently intervened without changing inputs.",
        "gain": "Original observation position RMSE in the same input-aligned response windows.",
        "dynamics_and_order": "Reference and candidate/before observations only; stored and comparison-derived original events are intervened separately.",
        "scope": "Only the listed scored fields for these exact calibration triples under the bound comparator; baseline event diagnostics may change. Any source change invalidates admission."}


def _admission_proposal(result, bundles):
    originals = {r["record_id"] for r in result["records"] if r["role"] == "original"}
    diagnostics, mandatory = [], []
    for failure in result["failures"]:
        if failure.get("record_id") in originals and failure["code"] in ORIGINAL_EVENT_DIAGNOSTICS:
            diagnostics.append(deepcopy(failure))
        else:
            mandatory.append(deepcopy(failure))
    grouped = defaultdict(dict)
    for bundle in bundles:
        record = bundle["record"]
        scenario = record.get("scenario", {})
        case_id = scenario.get("case_id", scenario.get("id"))
        if not case_id or record["role"] not in ("reference", "before", "original") or record["role"] in grouped[case_id]:
            mandatory.append({"code": "admission_matrix_incomplete", "record_id": record["id"]})
            continue
        grouped[case_id][record["role"]] = bundle
        sources = bundle["manifest"].get("metadata", {}).get("source_hashes")
        if not isinstance(sources, dict) or not sources or record.get("source_sha256") != digest(sources):
            mandatory.append({"code": "admission_source_binding_missing", "record_id": record["id"]})
    proofs = []
    for case_id, roles in sorted(grouped.items()):
        if set(roles) != {"reference", "before", "original"}:
            mandatory.append({"code": "admission_matrix_incomplete", "case_id": case_id})
            continue
        try:
            records = [{**roles[role]["record"], "observations": roles[role]["observations"],
                        "events": roles[role]["event_set"]} for role in ("reference", "original", "before")]
            proof = prove_original_events_unconsumed(*records)
            proofs.append({"case_id": case_id, **proof})
            if proof["status"] != "pass":
                mandatory.append({"code": "original_events_affect_scoring", "case_id": case_id})
        except (VisualError, KeyError, TypeError, ValueError) as exc:
            mandatory.append({"code": "metric_dependency_unproven", "case_id": case_id, "detail": str(exc)})
    if not proofs or result["unsupported"] or not result["sheets"]:
        mandatory.append({"code": "admission_scope_unqualified"})
    return {"kind": "ProspectivePanelScoringAdmissionProposal", "policy": ADMISSION_POLICY,
        "status": "pass" if not mandatory else "fail", "images_inspected": False,
        "behavior_acceptance_granted": False, "whole_raw_review_accepted": False,
        "original_event_measurements_qualified": False,
        "mandatory_scope": ["All three roles: immutable pixels, public selections, source identity, coverage, positions and body geometry.",
                            "Reference and before: every event occurrence, interval, relation and 100ms uncertainty gate."],
        "diagnostic_scope": "Stored original events are independently checked and remain unqualified; they do not feed gain, dynamics, order or reference profile.",
        "mandatory_failures": mandatory, "original_event_diagnostics": diagnostics,
        "metric_dependency_bindings": proofs, "metric_sources": _metric_inventory(),
        "final_review_requirement": "Reference and final/candidate events and all scored measurements remain fully mandatory; this receipt grants pre-prompt admission only."}


def _verified_review(output):
    output = legacy._ordinary(Path(output))
    result, receipt = read_json(output / "result.json"), read_json(output / "evidence-receipt.json")
    if result.get("version") != VERSION or digest(result) != receipt.get("result_sha256"):
        raise VisualError("stale_review", "The separate v3 pending review changed.")
    policy = read_json(output / "policy-selection.json")
    if (policy.get("anchor_policy") != result.get("anchor_policy") or receipt.get("anchor_policy") != result.get("anchor_policy")
            or policy.get("anchor_definition") != _anchor_definition(result["anchor_policy"])
            or digest(policy) != result.get("policy_selection_sha256") or digest(policy) != receipt.get("policy_selection_sha256")
            or receipt.get("anchor_policy_definition_sha256") != digest(_anchor_definition(result["anchor_policy"]))
            or receipt.get("runtime_libraries") != {"opencv": cv2.__version__, "numpy": np.__version__}):
        raise VisualError("stale_review", "The prospective pixel policy or runtime library identity changed.")
    for name, expected in {**receipt["source_files"], **receipt["code_sources"]}.items():
        if file_sha256(legacy._ordinary(Path(name))) != expected:
            raise VisualError("stale_review", "A raw image, measurement, helper or code source changed.")
    for name, expected in receipt["generated_files"].items():
        if file_sha256(safe_path(output, name)) != expected:
            raise VisualError("stale_review", "A raw proposal or contact sheet changed.")
    proposal = result.get("admission", {})
    if proposal.get("policy") != ADMISSION_POLICY or proposal.get("metric_sources") != _metric_inventory():
        raise VisualError("stale_admission", "Scoring dependencies changed; original-event independence must be checked again.")
    for proof in proposal.get("metric_dependency_bindings", []):
        derived = proof.get("original_derived_event_mutations", [])
        if (proof.get("metric_sources") != proposal["metric_sources"] or proof.get("status") != "pass"
                or proof.get("schema_version") != 2 or len(derived) != 3
                or any(check.get("identical") is not True or check.get("observations_and_inputs_unchanged") is not True for check in derived)):
            raise VisualError("stale_admission", "A calibration metric dependency proof is missing or changed.")
    return output, result, receipt


def attest_admission(output, *, reviewer, inspected_sheets, admitted, images_inspected=False):
    """Record actual image inspection of the explicitly restricted admission.

    This distinct sidecar never marks the failed whole raw review accepted and
    never qualifies stored original events. The caller must inspect ALL sheets.
    """
    output, result, receipt = _verified_review(output)
    if not isinstance(reviewer, str) or not reviewer.strip() or images_inspected is not True or type(admitted) is not bool:
        raise VisualError("invalid_review", "Named reviewer, actual inspection and a boolean admission decision are required.")
    if (output / "admission-attestation.json").exists():
        raise VisualError("output_exists", "Preserve an earlier admission attestation.")
    if not isinstance(inspected_sheets, (list, tuple)) or len(set(inspected_sheets)) != len(inspected_sheets) or set(inspected_sheets) != {s["path"] for s in result["sheets"]}:
        raise VisualError("incomplete_visual_review", "Inspect every bound raw contact sheet before admission.")
    if admitted and (result["admission"].get("status") != "pass" or result["unsupported"] or not result["sheets"]):
        raise VisualError("admission_not_qualified", "Inspection cannot waive any mandatory admission gate.")
    value = {"kind": "ProspectivePanelScoringAdmissionAttestation", "policy": ADMISSION_POLICY,
        "status": "pass" if admitted else "fail", "reviewer": reviewer.strip(), "images_inspected": True, "admitted": admitted,
        "behavior_acceptance_granted": False, "whole_raw_review_accepted": False, "original_event_measurements_qualified": False,
        "result_sha256": digest(result), "admission_proposal_sha256": digest(result["admission"]),
        "evidence_receipt_sha256": file_sha256(output / "evidence-receipt.json"), "inspected_sheets": result["sheets"],
        "metric_dependency_sha256": digest(result["admission"]["metric_dependency_bindings"]), "digital_signature": False}
    with (output / "admission-attestation.json").open("x", encoding="utf-8") as stream:
        import json
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return value


def validate_admission(output, suite_directory=None, suite=None):
    """Read-only readiness hook; validate scope, inspection, every hash and suite."""
    output, result, receipt = _verified_review(output)
    value = read_json(output / "admission-attestation.json")
    proposal = result["admission"]
    if (proposal.get("status") != "pass" or proposal.get("mandatory_failures") or result["unsupported"]
            or value.get("kind") != "ProspectivePanelScoringAdmissionAttestation" or value.get("policy") != ADMISSION_POLICY
            or value.get("status") != "pass" or value.get("admitted") is not True or value.get("images_inspected") is not True
            or value.get("behavior_acceptance_granted") is not False or value.get("whole_raw_review_accepted") is not False
            or value.get("original_event_measurements_qualified") is not False
            or value.get("result_sha256") != digest(result) or value.get("admission_proposal_sha256") != digest(proposal)
            or value.get("evidence_receipt_sha256") != file_sha256(output / "evidence-receipt.json")
            or value.get("inspected_sheets") != result["sheets"] or not result["sheets"]
            or value.get("metric_dependency_sha256") != digest(proposal["metric_dependency_bindings"])):
        raise VisualError("environment_not_ready", "Restricted pre-prompt admission is pending, changed or unqualified.")
    if (suite_directory is None) != (suite is None):
        raise VisualError("invalid_review", "Pass both the suite directory and its frozen manifest.")
    if suite is not None:
        from .media_suite import load_suite, record_from_suite
        from .evidence import observations
        actual_suite = load_suite(suite_directory)
        if digest(actual_suite) != digest(suite) or suite.get("domain") != "web_panel":
            raise VisualError("environment_not_ready", "Admission belongs to another frozen panel suite.")
        contract = legacy._public_contract(suite.get("event_contract"))
        if any(result["public_event_contract"].get(key) != expected for key, expected in contract.items()):
            raise VisualError("environment_not_ready", "The admission event definition differs from the frozen suite.")
        cases = [case for case in suite["cases"] if case["role"] == "calibration"]
        initial = suite.get("initial_records", [])
        if {(case["role"], case["case_id"]) for case in initial} != {(role, case["case_id"]) for role in ("original", "before") for case in cases}:
            raise VisualError("environment_not_ready", "Both starting-source calibration matrices are required.")
        required = set()
        for item in cases + initial:
            record = record_from_suite(suite_directory, item)
            role = "reference" if item in cases else item["role"]
            observed = observations(record)
            required.add((role, item["case_id"], observed["package_sha256"], digest(observed), record.get("source_sha256")))
        checked = {(item["role"], item["case_id"], item["package_sha256"], item["observations_sha256"], item.get("source_sha256")) for item in result["records"]}
        if checked != required or len(checked) != len(result["records"]):
            raise VisualError("environment_not_ready", "Admission does not bind every calibration reference and starting-source observation.")
        if {proof["case_id"] for proof in proposal["metric_dependency_bindings"]} != {case["case_id"] for case in cases}:
            raise VisualError("environment_not_ready", "Metric independence is not bound to every calibration triple.")
    return {"directory": str(output), "admission_attestation_sha256": file_sha256(output / "admission-attestation.json"),
        "result_sha256": digest(result), "required_record_count": len(result["records"]), "policy": ADMISSION_POLICY,
        "metric_dependency_sha256": digest(proposal["metric_dependency_bindings"]),
        "whole_raw_review_accepted": False, "original_event_measurements_qualified": False}
