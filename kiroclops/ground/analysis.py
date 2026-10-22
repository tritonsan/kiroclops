"""Input-aligned horizontal motion measured from native rendered pixels only."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np

from kiroclops.analysis import _read_frame, _match, _match_bank, _textured
from .annotation import MANUAL_SOURCES, load_annotation

FPS = 60
FRAME_COUNT = 240
SCENARIOS = {
    "r06": [(30, "right", True), (36, "right", False)],
    "r48": [(30, "right", True), (78, "right", False)],
    "r18": [(30, "right", True), (48, "right", False)],
    "l30": [(30, "left", True), (60, "left", False)],
    "reverse24": [(30, "right", True), (54, "right", False), (54, "left", True), (78, "left", False)],
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def scenario_phases(scenario):
    """Half-open input phases, with the endpoint 30 ticks after release included."""
    if not isinstance(scenario, dict) or scenario.get("id") not in SCENARIOS:
        raise ValueError("unsupported ground scenario")
    expected = SCENARIOS[scenario["id"]]
    events = scenario.get("events")
    if not isinstance(events, list) or len(events) != len(expected):
        raise ValueError("scenario input events are incomplete")
    for event, wanted in zip(events, expected):
        if (not isinstance(event, dict) or type(event.get("tick")) is not int
                or type(event.get("pressed")) is not bool
                or (event["tick"], event.get("action"), event["pressed"]) != wanted):
            raise ValueError("scenario input events differ from their frozen schedule")
    split = "calibration" if scenario["id"] in {"r06", "r48"} else "validation"
    if scenario.get("split") != split:
        raise ValueError("scenario belongs to a different split")
    if type(scenario.get("repeat")) is not int or scenario["repeat"] not in (1, 2, 3):
        raise ValueError("scenario repeat must be 1, 2 or 3")
    offset = scenario.get("spawn_offset", 0)
    if type(offset) not in (int, float) or not math.isfinite(offset) or offset not in (0, 24):
        raise ValueError("unsupported spawn offset")
    if split == "calibration" and offset != 0:
        raise ValueError("calibration spawn offset must be zero")
    if scenario.get("warmup_ticks", 60) != 60 or scenario.get("frame_count", 240) != 240:
        raise ValueError("scenario requires 60 warmup ticks and 240 recorded frames")
    release = expected[-1][0]
    if scenario["id"] == "reverse24":
        phases = [{"name": "held", "start": 30, "end": 54, "direction": 1},
                  {"name": "reversal", "start": 54, "end": 78, "direction": -1}]
    else:
        phases = [{"name": "held", "start": 30, "end": release,
                   "direction": -1 if scenario["id"] == "l30" else 1}]
    return phases + [{"name": "coast", "start": release, "end": release + 31, "direction": 0}]


def mirror_bank(template, anchor, horizontal_mirror=True):
    """Transform the same physical anchor along with the fixed image patch."""
    bank = [(template.copy(), tuple(anchor), False)]
    if horizontal_mirror:
        bank.append((np.ascontiguousarray(template[:, ::-1]),
                     (template.shape[1] - 1 - anchor[0], anchor[1]), True))
    return bank


def ground_diagnostics(frames, scenario, body_height):
    """Pixel-resolution event estimates, deliberately excluded from acceptance."""
    phases = scenario_phases(scenario)
    release = phases[-1]["start"]
    by_index = {frame["frame_index"]: frame for frame in frames}
    stop_window, reverse_window = None, None
    # Eight consecutive observations confined to one native pixel. Retention can
    # still have subpixel velocity: this is an observed rest window, not v == 0.
    for start in range(release, len(frames)-7):
        group = [by_index[i] for i in range(start,start+8)]
        if all(f["valid"] for f in group) and np.ptp([f["x"] for f in group]) <= 1.:
            stop_window = [start,start+7]
            break
    reversal = next((phase for phase in phases if phase["name"] == "reversal"), None)
    if reversal:
        for start in range(reversal["start"], reversal["end"]-4):
            group = [by_index[i] for i in range(start,start+5)]
            if all(f["valid"] for f in group):
                values = np.array([f["x"] for f in group])
                slope_px_tick = float(np.dot(np.arange(5)-2,values-values.mean())/10)
                if slope_px_tick*reversal["direction"] > .25:
                    reverse_window = [start,start+4]
                    break
    return {"diagnostic_only": True, "stop_pixel_rest_window": stop_window,
            "stop_time_after_release_s": (stop_window[0]-release)/FPS if stop_window else None,
            "reversal_motion_window": reverse_window,
            "reversal_time_after_input_s": (reverse_window[-1]-reversal["start"])/FPS if reverse_window else None,
            "speed_ols_rounding_bound_body_s": 80/(7*body_height),
            "method": "stop: first eight valid x pixels spanning <=1px; reversal: first five valid x pixels with signed OLS slope >0.25px/tick; no interpolation",
            "limitations": "Pixel rest need not mean zero physical velocity; reversal is a window estimate. Speed bound assumes independent +/-0.5px rounding, not other tracking errors."}


def measure_ground(frames, scenario, body_height):
    phases = scenario_phases(scenario)
    errors = []
    origin_frames = [f for f in frames if 20 <= f["frame_index"] <= 29]
    if len(origin_frames) != 10 or not all(f["valid"] for f in origin_frames):
        return None, phases, {}, ["origin frames 20..29 must all be valid"]
    origin = float(np.median([f["x"] for f in origin_frames]))
    if np.ptp([f["x"] for f in origin_frames]) > max(1., .05 * body_height):
        errors.append("pre-input horizontal origin is not stationary")
    for frame in frames:
        frame["x_body"] = (frame["x"] - origin) / body_height if frame["valid"] else None
    by_index = {f["frame_index"]: f for f in frames}
    for phase in phases:
        selected = [by_index[t] for t in range(phase["start"], phase["end"])]
        phase["valid_ratio"] = sum(f["valid"] for f in selected) / len(selected)
        if phase["valid_ratio"] < .95:
            errors.append(f"valid frame ratio below 0.95 in {phase['name']} phase")
    held = phases[-2]
    speed_window = list(range(held["end"] - 8, held["end"])) if held["end"] - held["start"] >= 18 else []
    speed = None
    if speed_window:
        if not all(by_index[t]["valid"] for t in speed_window):
            errors.append("last-eight-frame speed window is incomplete")
        else:
            times = np.array(speed_window, dtype=float) / FPS
            values = np.array([by_index[t]["x_body"] for t in speed_window])
            speed = float(np.dot(times-times.mean(), values-values.mean()) / np.dot(times-times.mean(), times-times.mean()))
    release = phases[-1]["start"]
    endpoint = phases[-1]["end"] - 1
    end = by_index[endpoint]["x_body"]
    at_release = by_index[release-1]["x_body"]
    if end is None or at_release is None:
        errors.append("release or terminal position is not observable")
    metrics = {"speed_body_s": speed, "speed_window": speed_window,
               "speed_direction": held["direction"], "speed_eligible": bool(speed_window),
               "stop_distance_body": end-at_release if end is not None and at_release is not None else None,
               "end_position_body": end, "origin_x_px": origin,
               "window_start": 30, "window_end": endpoint,
               "diagnostics": ground_diagnostics(frames,scenario,body_height)}
    return origin, phases, metrics, errors


def validate_controls(frames, phases, annotation, run_dir, manifest, body_height):
    by_index = {f["frame_index"]: f for f in frames}
    flips = {frames[i]["frame_index"] for i in range(1, len(frames))
             if frames[i]["valid"] and frames[i-1]["valid"]
             and frames[i]["mirrored"] != frames[i-1]["mirrored"]}
    points, ignored = [], 0
    for p in annotation.get("control_points", []):
        if p.get("run_id") not in {str(run_dir), run_dir.name, manifest.get("run_id")}:
            continue
        if p.get("source") not in MANUAL_SOURCES:
            ignored += 1
            continue
        index = p["frame_index"]
        f = by_index.get(index)
        path = run_dir / "frames" / f"{index:06d}.png"
        digest = _sha(path) if path.is_file() else None
        bound = bool(digest and p.get("frame_sha256") == digest)
        error = math.hypot(f["x"]-p["x"], f["y"]-p["y"]) if bound and f and f["valid"] else None
        categories = []
        if any(abs(index-t) <= 2 for t in flips):
            categories.append("flip")
        for phase in phases:
            if phase["start"] <= index < phase["end"] and phase["name"] in {"coast", "reversal"}:
                categories.append({"coast": "brake", "reversal": "reverse"}[phase["name"]])
        points.append({"run_id": str(run_dir), "frame_index": index, "source": p["source"],
                       "frame_sha256": digest, "hash_bound": bound, "x": p["x"], "y": p["y"],
                       "error_px": error, "categories": categories,
                       "observation_id": p.get("observation_id")})
    errors = [p["error_px"] for p in points if p["error_px"] is not None]
    median = float(np.median(errors)) if errors else None
    tolerance = max(1., .05 * body_height)
    status = "pending" if not points else ("pass" if len(errors) == len(points) and median <= tolerance else "fail")
    return {"status": status, "point_status": status, "control_point_count": len(points),
            "ignored_nonmanual_points": ignored, "median_error_px": median,
            "tolerance_px": tolerance, "points": points,
            "quota_note": "Per-role quota and flip/brake/reversal coverage are checked across distinct observations at comparison."}


def analyze_run(run_dir, annotation_path, output_path=None):
    if cv2.__version__.split(".")[0] != "5":
        raise RuntimeError("OpenCV 5 is required")
    run = Path(run_dir).resolve()
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in (1, 2) or manifest.get("fps") != FPS or manifest.get("frame_count") != FRAME_COUNT:
        raise ValueError("ground capture requires schema 1/2, 60 Hz and 240 native frames")
    phases = scenario_phases(manifest["scenario"])
    viewport, metadata = manifest["viewport"], manifest["frames"]
    if (len(viewport) != 2 or any(type(v) is not int or v <= 0 for v in viewport)
            or len(metadata) != FRAME_COUNT):
        raise ValueError("invalid viewport or frame count")
    if manifest.get("status", "ok") != "ok" or manifest.get("warmup_ticks", 60) != 60:
        raise ValueError("capture must be successful with 60 warmup ticks")
    active = set()
    for i, f in enumerate(metadata):
        if (type(f.get("frame_index")) is not int or f["frame_index"] != i
                or type(f.get("time_s")) not in (int, float) or not math.isfinite(f["time_s"])
                or abs(f["time_s"]-i/FPS) > 1e-5):
            raise ValueError("frames must be contiguous, finite, capture-relative 60 Hz observations")
        for event in manifest["scenario"]["events"]:
            if event["tick"] == i:
                (active.add if event["pressed"] else active.discard)(event["action"])
        if f.get("actions") != sorted(active) or f.get("physics_tick") != 60+i:
            raise ValueError("recorded logical inputs or physics ticks differ from the frozen schedule")
    annotation = load_annotation(annotation_path, viewport)
    template_run = Path(annotation["template_run"])
    template_manifest = json.loads((template_run / "manifest.json").read_text(encoding="utf-8"))
    if template_manifest.get("game") != manifest.get("game"):
        raise ValueError("template belongs to another game")
    template_image = _read_frame(template_run, annotation.get("template_frame", 0), viewport)
    x, y, w, h = map(int, annotation["roi"])
    template = template_image[y:y+h, x:x+w].copy()
    if not _textured(template):
        raise ValueError("body template lacks spatial texture")
    first = _read_frame(run, 0, viewport)
    backgrounds = []
    for box in annotation["background_rois"]:
        bx, by, bw, bh = map(int, box)
        patch = first[by:by+bh, bx:bx+bw].copy()
        if not _textured(patch):
            raise ValueError("background ROI lacks spatial texture")
        backgrounds.append((patch, (bx, by)))
    height = float(annotation["body_height_px"])
    anchor = annotation["anchor_offset"]
    bank = mirror_bank(template, anchor, annotation.get("horizontal_mirror", True))
    position = (x+anchor[0]+manifest["scenario"].get("spawn_offset", 0), y+anchor[1])
    radius = max(8, int(math.ceil(height*.75)))
    frames, all_camera_valid = [], True
    for f in metadata:
        image = _read_frame(run, f["frame_index"], viewport)
        camera_valid, shift_max = True, 0.
        for patch, origin in backgrounds:
            found, score, _ = _match(image, patch, origin, 4)
            shift = math.hypot(found[0]-origin[0], found[1]-origin[1]) if found else float("inf")
            shift_max = max(shift_max, shift)
            camera_valid &= score >= .85 and shift <= annotation.get("camera_max_shift_px", 1.)
        all_camera_valid &= camera_valid
        found, score, margin, mirrored = _match_bank(image, bank, position, radius)
        valid = bool(found and camera_valid and score >= annotation.get("min_score", .75)
                     and margin >= annotation.get("min_margin", .03))
        if valid:
            position = found
        frames.append({"frame_index": f["frame_index"], "time_s": f["time_s"],
                       "physics_tick": f["physics_tick"], "actions": f["actions"],
                       "x": found[0] if found else None, "y": found[1] if found else None,
                       "score": score, "margin": margin, "valid": valid, "mirrored": bool(mirrored),
                       "camera_valid": bool(camera_valid), "camera_shift_px": shift_max if math.isfinite(shift_max) else None,
                       "x_body": None})
    origin, phases, metrics, errors = measure_ground(frames, manifest["scenario"], height)
    ratio = sum(f["valid"] for f in frames)/FRAME_COUNT
    if ratio < .95:
        errors.append("valid frame ratio below 0.95")
    if not all_camera_valid:
        errors.append("fixed-camera condition failed")
    manual = validate_controls(frames, phases, annotation, run, manifest, height)
    if manual["status"] == "fail":
        errors.append("independent manual annotation validation failed")
    result = {"schema_version": 1, "mechanic": "ground", "status": "rejected" if errors else "ok",
              "run_id": str(run), "game": manifest["game"], "variant": manifest["variant"],
              "scenario": manifest["scenario"], "fps": FPS, "body_height_px": height,
              "valid_ratio": ratio, "camera_valid": bool(all_camera_valid), "phases": phases,
              "events": {"first_input_tick": 30, "final_release_tick": phases[-1]["start"],
                         "reversal_ticks": [p["start"] for p in phases if p["name"] == "reversal"]},
              "metrics": metrics, "frames": frames, "errors": errors, "annotation_validation": manual,
              "provenance": {"manifest_sha256": _sha(manifest_path), "annotation_sha256": _sha(annotation_path),
                             "template_frame_sha256": _sha(template_run / "frames" / f"{annotation.get('template_frame',0):06d}.png"),
                             "opencv_version": cv2.__version__, "measurement": "native rendered pixels; no interpolation or template updates"}}
    _write(output_path or run / "analysis.json", result)
    return result
