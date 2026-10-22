"""OpenCV 5 measurements from rendered frames, independent of engine telemetry."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np

from .annotation import MANUAL_SOURCES, load_annotation


def _read_frame(run_dir, index, viewport):
    image = cv2.imread(str(Path(run_dir) / "frames" / f"{index:06d}.png"), cv2.IMREAD_COLOR)
    if image is None or [image.shape[1], image.shape[0]] != list(viewport):
        raise ValueError(f"frame {index} missing, unreadable, or wrong viewport")
    return image


def _match(image, template, center, radius):
    """Bounded search; raw correlation and spatially distinct runner-up."""
    th, tw = template.shape[:2]
    ih, iw = image.shape[:2]
    x, y = center
    left, top = max(0, int(x - radius)), max(0, int(y - radius))
    right, bottom = min(iw, int(x + tw + radius + 1)), min(ih, int(y + th + radius + 1))
    crop = image[top:bottom, left:right]
    if crop.shape[0] < th or crop.shape[1] < tw:
        return None, 0., 0.
    response = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
    response = np.nan_to_num(response, nan=-1., posinf=-1., neginf=-1.)
    _, score, _, location = cv2.minMaxLoc(response)
    competitors = response.copy()
    px, py = location
    # Adjacent pixels are the same match, not independent alternatives.
    suppression = max(1, min(tw, th) // 3)
    competitors[max(0, py-suppression):py+suppression+1,
                max(0, px-suppression):px+suppression+1] = -1.
    second = float(competitors.max())
    return (left + px, top + py), float(score), float(score - second)


def _textured(image):
    # Across spatial dimensions; a flat non-gray color is still textureless.
    return float(np.max(np.std(image.astype(float), axis=(0, 1)))) >= 2.


def _template_bank(template, anchor, rotations):
    """Fixed quarter-turn variants, with the same physical anchor transformed."""
    height, width = template.shape[:2]
    ax, ay = anchor
    transformed = {0: (ax, ay), 90: (ay, width-1-ax),
                   180: (width-1-ax, height-1-ay), 270: (height-1-ay, ax)}
    return [(np.ascontiguousarray(np.rot90(template, angle//90)), transformed[angle], angle)
            for angle in dict.fromkeys(rotations)]


def _match_bank(image, bank, predicted_anchor, radius):
    candidates = []
    for template, offset, angle in bank:
        origin = (predicted_anchor[0]-offset[0], predicted_anchor[1]-offset[1])
        location, score, margin = _match(image, template, origin, radius)
        if location is not None:
            anchor = (location[0]+offset[0], location[1]+offset[1])
            candidates.append((anchor, score, margin, angle, min(template.shape[:2])))
    if not candidates:
        return None, 0., 0., 0
    best = max(candidates, key=lambda candidate: candidate[1])
    runner_up = best[1]-best[2]
    suppression = max(1, best[4]//3)
    for candidate in candidates:
        # Equivalent poses at the same anchor are not independent locations.
        if math.hypot(candidate[0][0]-best[0][0], candidate[0][1]-best[0][1]) > suppression:
            runner_up = max(runner_up, candidate[1])
    return best[0], best[1], best[1]-runner_up, best[3]


def _matches_run(record, run_dir, manifest):
    return record.get("run_id") in {str(Path(run_dir).resolve()), Path(run_dir).name, manifest.get("run_id")}


def validate_controls(frames, events, annotation, run_dir, manifest, body_height):
    """Validate only explicitly independent manual observations of this run."""
    by_index = {f["frame_index"]: f for f in frames}
    points, ignored = [], 0
    for control in annotation.get("control_points", []):
        if not _matches_run(control, run_dir, manifest):
            continue
        source = control.get("source", annotation.get("control_point_source"))
        if source not in MANUAL_SOURCES:
            ignored += 1
            continue
        frame = by_index.get(control["frame_index"])
        error = None
        if frame and frame["valid"]:
            error = math.hypot(frame["x"] - control["x"], frame["y"] - control["y"])
        points.append({"run_id": str(Path(run_dir).resolve()), "frame_index": control["frame_index"],
                       "error_px": error, "source": source})
    errors = [p["error_px"] for p in points if p["error_px"] is not None]
    median = float(np.median(errors)) if errors else None
    tolerance = max(1., .05 * body_height)
    point_status = "pending" if not points else (
        "pass" if len(errors) == len(points) and median <= tolerance else "fail")
    event_checks = []
    for control in annotation.get("control_events", []):
        if not _matches_run(control, run_dir, manifest):
            continue
        source = control.get("source", annotation.get("control_event_source"))
        if source not in MANUAL_SOURCES:
            continue
        differences = {name: abs(events[name] - control[name]) if events.get(name) is not None else None
                       for name in ("takeoff_frame", "apex_frame", "landing_frame")}
        event_checks.append({"source": source, "errors_frames": differences,
                             "pass": all(v is not None and v <= 2 for v in differences.values())})
    event_status = "pending" if not event_checks else ("pass" if all(e["pass"] for e in event_checks) else "fail")
    status = "fail" if "fail" in (point_status, event_status) else (
        "pass" if point_status == event_status == "pass" else "pending")
    return {"status": status, "point_status": point_status, "event_status": event_status,
            "control_point_count": len(points), "ignored_nonmanual_points": ignored,
            "median_error_px": median, "tolerance_px": tolerance,
            "points": points, "event_checks": event_checks}


def measure_jump(frames, scenario, fps, body_height):
    """Events use observed positions. Takeoff is the last grounded frame."""
    events = {name: None for name in ("takeoff_frame", "apex_frame", "landing_frame")}
    metrics = {name: None for name in ("height_body", "rise_s", "fall_s", "input_latency_s")}
    start_tick = scenario.get("start_tick", 30)
    # Scenario ticks are capture-relative; physics_tick includes engine warmup.
    pre = [f["y"] for f in frames if f["valid"] and f["frame_index"] < start_tick]
    if len(pre) < 3:
        return events, metrics, ["insufficient pre-input baseline frames"]
    baseline = float(np.median(pre))
    # A stable tracked facial patch may still bob in the grounded animation.
    # Learn this image-only envelope before input; do not mistake it for flight.
    ground_min, ground_max = float(min(pre)), float(max(pre))
    if ground_max-ground_min > max(2., .10*body_height):
        return events, metrics, ["pre-input anchor motion exceeds the stationary-ground envelope"]
    for frame in frames:
        frame["height_body"] = (baseline - frame["y"]) / body_height if frame["valid"] else None
    tolerance_px = max(.5, .025*body_height)
    first_air = None
    for index in range(1, len(frames)-1):
        a, b = frames[index:index+2]
        if a["frame_index"] < start_tick:
            continue
        if a["valid"] and b["valid"] and a["y"] < ground_min-tolerance_px and b["y"] < ground_min-tolerance_px:
            first_air = index
            break
    if first_air is None or not frames[first_air - 1]["valid"]:
        return events, metrics, ["takeoff not observable"]
    takeoff = first_air - 1
    # First stable return to the original ground; do not incorporate another jump.
    landing = None
    for index in range(first_air + 2, len(frames)-2):
        group = frames[index:index+3]
        if all(f["valid"] and ground_min-tolerance_px <= f["y"] <= ground_max+tolerance_px for f in group):
            landing = index
            break
    if landing is None:
        return events, metrics, ["same-level landing not observable"]
    flight = [f for f in frames[first_air:landing] if f["valid"]]
    if not flight:
        return events, metrics, ["no reliable airborne observations"]
    peak = max(f["height_body"] for f in flight)
    plateau = [f["frame_index"] for f in flight if peak - f["height_body"] <= .25 / body_height]
    apex_index = plateau[len(plateau)//2]
    index_lookup = {f["frame_index"]: i for i, f in enumerate(frames)}
    apex = index_lookup[apex_index]
    if not takeoff < apex < landing:
        return events, metrics, ["invalid event order"]
    for name, index in [("takeoff_frame", takeoff), ("apex_frame", apex), ("landing_frame", landing)]:
        events[name] = frames[index]["frame_index"]
    input_time = next((f["time_s"] for f in frames if f["frame_index"] == start_tick), start_tick / fps)
    metrics.update(height_body=float(peak),
                   rise_s=frames[apex]["time_s"] - frames[takeoff]["time_s"],
                   fall_s=frames[landing]["time_s"] - frames[apex]["time_s"],
                   input_latency_s=frames[first_air]["time_s"] - input_time)
    return events, metrics, []


def analyze_run(run_dir, annotation_path, output_path=None):
    if cv2.__version__.split(".")[0] != "5":
        raise RuntimeError(f"OpenCV 5 is required, found {cv2.__version__}")
    run_dir = Path(run_dir).resolve()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("fps") != 60:
        raise ValueError("phase 1 requires schema 1 and 60 Hz capture")
    viewport, metadata = manifest["viewport"], manifest["frames"]
    if len(metadata) != manifest["frame_count"] or len(metadata) < 4:
        raise ValueError("manifest frame_count does not match frame records")
    for index, frame in enumerate(metadata):
        if frame["frame_index"] != index or not math.isfinite(frame["time_s"]):
            raise ValueError("frame indices must be contiguous from zero and timestamps finite")
        if index and abs(frame["time_s"] - metadata[index-1]["time_s"] - 1/60) > 1e-5:
            raise ValueError("timestamps must represent contiguous 60 Hz frames")
    annotation = load_annotation(annotation_path, viewport)
    template_dir = Path(annotation["template_run"])
    template_manifest = json.loads((template_dir / "manifest.json").read_text(encoding="utf-8"))
    if template_manifest.get("game") != manifest.get("game"):
        raise ValueError("annotation template belongs to a different game")
    template_image = _read_frame(template_dir, annotation["template_frame"], viewport)
    x, y, w, h = map(int, annotation["roi"])
    template = template_image[y:y+h, x:x+w].copy()
    if not _textured(template):
        raise ValueError("body template lacks spatial texture")
    first = _read_frame(run_dir, 0, viewport)
    backgrounds = []
    for box in annotation["background_rois"]:
        bx, by, bw, bh = map(int, box)
        patch = first[by:by+bh, bx:bx+bw].copy()
        if not _textured(patch):
            raise ValueError("background ROI lacks spatial texture")
        backgrounds.append((patch, (bx, by)))
    height = float(annotation["body_height_px"])
    # The first local search accommodates explicitly recorded spawn displacement.
    spawn = float(manifest["scenario"].get("spawn_offset", 0))
    anchor_offset = annotation["anchor_offset"]
    bank = _template_bank(template, anchor_offset, annotation.get("template_rotations", [0]))
    position = (x + spawn + anchor_offset[0], y + anchor_offset[1])
    radius = max(8, int(math.ceil(height * .75)))
    frames, errors = [], []
    all_camera_valid = True
    camera_threshold = float(annotation.get("camera_max_shift_px", 1.))
    for record in metadata:
        image = _read_frame(run_dir, record["frame_index"], viewport)
        camera_valid, max_shift = True, 0.
        for patch, origin in backgrounds:
            found, score, _ = _match(image, patch, origin, 4)
            shift = math.hypot(found[0]-origin[0], found[1]-origin[1]) if found else float("inf")
            max_shift = max(max_shift, shift)
            if score < .85 or shift > camera_threshold:
                camera_valid = False
        all_camera_valid &= camera_valid
        found, score, margin, rotation = _match_bank(image, bank, position, radius)
        valid = bool(found and camera_valid and score >= annotation.get("min_score", .75)
                     and margin >= annotation.get("min_margin", .03))
        if valid:
            position = found
        frames.append({"frame_index": record["frame_index"], "physics_tick": record.get("physics_tick", record["frame_index"]),
                       "time_s": record["time_s"],
                       "x": found[0] if found else None,
                       "y": found[1] if found else None,
                       "score": score, "margin": margin, "valid": valid,
                       "template_rotation": rotation,
                       "camera_valid": camera_valid, "camera_shift_px": max_shift if math.isfinite(max_shift) else None,
                       "height_body": None})
    events, metrics, event_errors = measure_jump(frames, manifest["scenario"], 60, height)
    errors.extend(event_errors)
    valid_ratio = sum(f["valid"] for f in frames) / len(frames)
    if not all_camera_valid:
        errors.append("fixed-camera condition failed")
    if valid_ratio < .95:
        errors.append("valid frame ratio below 0.95")
    validation = validate_controls(frames, events, annotation, run_dir, manifest, height)
    if validation["status"] == "fail":
        errors.append("independent manual annotation validation failed")
    result = {"schema_version": 1, "status": "rejected" if errors else "ok",
              "run_id": str(run_dir), "game": manifest["game"], "variant": manifest["variant"],
              "scenario": manifest["scenario"], "fps": 60, "body_height_px": height,
              "valid_ratio": valid_ratio, "camera_valid": bool(all_camera_valid),
              "events": events, "metrics": metrics, "frames": frames,
              "annotation_validation": validation, "errors": errors,
              "provenance": {"manifest_path": str(manifest_path),
                             "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                             "annotation_path": str(Path(annotation_path).resolve()),
                             "annotation_sha256": hashlib.sha256(Path(annotation_path).read_bytes()).hexdigest(),
                             "opencv_version": cv2.__version__, "measurement": "rendered-pixels"}}
    destination = Path(output_path) if output_path else run_dir / "analysis.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result
