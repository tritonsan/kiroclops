"""Explicit image-space annotations. No game physics or generated truth is used."""
from __future__ import annotations

import json
import math
from pathlib import Path

MANUAL_SOURCES = {"human", "assistant_visual"}


def _numbers(value, length, name):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} must contain {length} numbers")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise ValueError(f"{name} contains a non-finite/non-numeric value")
    return list(value)


def validate_annotation(annotation, viewport=None):
    """Validate shape/coordinates, without claiming annotations are accurate."""
    if annotation.get("schema_version") != 1:
        raise ValueError("annotation schema_version must be 1")
    if not Path(annotation.get("template_run", "")).is_absolute():
        raise ValueError("template_run must be an absolute run directory")
    frame = annotation.get("template_frame", 0)
    if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
        raise ValueError("template_frame must be a nonnegative integer")
    roi = _numbers(annotation.get("roi"), 4, "roi")
    anchor = _numbers(annotation.get("anchor_offset"), 2, "anchor_offset")
    height = annotation.get("body_height_px")
    if isinstance(height, bool) or not isinstance(height, (int, float)) or not math.isfinite(height) or height <= 0:
        raise ValueError("body_height_px must be a positive finite number")
    backgrounds = annotation.get("background_rois")
    if not isinstance(backgrounds, list) or not backgrounds:
        raise ValueError("at least one stationary background ROI is required")
    for name, box in [("roi", roi)] + [("background_roi", _numbers(b, 4, "background_roi")) for b in backgrounds]:
        x, y, w, h = box
        if any(int(v) != v for v in box) or x < 0 or y < 0 or w < 2 or h < 2:
            raise ValueError(f"{name} must be an integer rectangle with width/height >=2")
        if viewport and (x + w > viewport[0] or y + h > viewport[1]):
            raise ValueError(f"{name} extends beyond viewport")
    if not (0 <= anchor[0] < roi[2] and 0 <= anchor[1] < roi[3]):
        raise ValueError("anchor_offset must be inside the body ROI")
    for name, default in [("min_score", .75), ("min_margin", .03)]:
        value = annotation.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    rotations = annotation.get("template_rotations", [0])
    if not isinstance(rotations, list) or not rotations or any(angle not in (0, 90, 180, 270) for angle in rotations):
        raise ValueError("template_rotations must be a nonempty list of quarter turns")
    camera_shift = annotation.get("camera_max_shift_px", 1.)
    if not isinstance(camera_shift, (int, float)) or not math.isfinite(camera_shift) or not 0 <= camera_shift <= 1:
        raise ValueError("camera_max_shift_px must be between 0 and 1 for the fixed-camera experiment")
    seen = set()
    for point in annotation.get("control_points", []):
        if not isinstance(point.get("run_id"), str) or not point["run_id"]:
            raise ValueError("control point needs run_id")
        idx = point.get("frame_index")
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
            raise ValueError("control frame_index must be a nonnegative integer")
        _numbers([point.get("x"), point.get("y")], 2, "control point")
        key = (point["run_id"], idx)
        if key in seen:
            raise ValueError("duplicate control point for run/frame")
        seen.add(key)
    events = annotation.get("control_events", [])
    if not isinstance(events, list):
        raise ValueError("control_events must be a list of run_id/event records")
    for record in events:
        if not isinstance(record.get("run_id"), str):
            raise ValueError("control event needs run_id")
        for name in ("takeoff_frame", "apex_frame", "landing_frame"):
            value = record.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"control event {name} must be a nonnegative integer")
        if not record["takeoff_frame"] < record["apex_frame"] < record["landing_frame"]:
            raise ValueError("control events must be ordered takeoff < apex < landing")
    return annotation


def load_annotation(path, viewport=None):
    return validate_annotation(json.loads(Path(path).read_text(encoding="utf-8")), viewport)


def _select_point(image, title):
    """Click a pixel, Enter to confirm, Escape to cancel; no inferred point."""
    import cv2
    selected = []

    def clicked(event, x, y, flags, userdata):
        if event == cv2.EVENT_LBUTTONDOWN:
            selected[:] = [x, y]

    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(title, clicked)
    try:
        while True:
            preview = image.copy()
            if selected:
                cv2.drawMarker(preview, tuple(selected), (0, 255, 255), cv2.MARKER_CROSS, 9, 1)
            cv2.imshow(title, preview)
            key = cv2.waitKey(20) & 0xff
            if key in (10, 13) and selected:
                return list(selected)
            if key == 27 or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                raise ValueError("point annotation cancelled; nothing was saved")
    finally:
        cv2.destroyWindow(title)


def annotate_run(run_dir, output_path, *, roi=None, anchor_offset=None,
                 body_height_px=None, background_rois=None, interactive=False,
                 template_frame=0):
    """Create a pending annotation; independent control points are added later.

    Interactive selection includes an explicit anchor and a separate standing
    body rectangle. Neither is inferred from an animated bounding box.
    """
    run_dir = Path(run_dir).resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if interactive:
        import cv2
        image = cv2.imread(str(run_dir / "frames" / f"{template_frame:06d}.png"))
        if image is None:
            raise ValueError("template frame cannot be read")
        try:
            if roi is None:
                roi = list(cv2.selectROI("Select stable torso (Enter confirms)", image, False))
            if roi[2] < 2 or roi[3] < 2:
                raise ValueError("body ROI annotation cancelled or too small")
            if anchor_offset is None:
                point = _select_point(image, "Click stable anchor INSIDE body patch; Enter confirms")
                anchor_offset = [point[0]-roi[0], point[1]-roi[1]]
            if body_height_px is None:
                body = cv2.selectROI("Select FULL standing body for scale; Enter confirms", image, False)
                if body[3] < 2:
                    raise ValueError("standing body annotation cancelled or too small")
                body_height_px = float(body[3])
            if background_rois is None:
                background_rois = [list(cv2.selectROI("Select stationary textured background", image, False))]
        finally:
            cv2.destroyAllWindows()
    if roi is None or body_height_px is None or anchor_offset is None or background_rois is None:
        raise ValueError("roi, anchor_offset, body_height_px and background_rois are required")
    annotation = {
        "schema_version": 1, "template_run": str(run_dir),
        "template_frame": template_frame, "roi": list(roi),
        "anchor_offset": list(anchor_offset), "body_height_px": body_height_px,
        "background_rois": background_rois, "min_score": .75, "min_margin": .03,
        "template_rotations": [0],
        "control_points": [], "control_events": [],
        "control_point_source": None, "control_event_source": None,
        "validation_status": "pending",
    }
    validate_annotation(annotation, manifest.get("viewport"))
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(annotation, indent=2) + "\n", encoding="utf-8")
    return annotation


create_annotation = annotate_run


def annotate_controls(run_dir, annotation_path, frame_indices, *, event_frames=None):
    """Collect independent manual anchor checks, with optional event frame IDs.

    event_frames can be a takeoff/apex/landing triple or a mapping with the
    corresponding ``*_frame`` keys. The caller supplies these observations;
    this function never reads analysis.json to create expected answers.
    """
    import cv2
    run_dir = Path(run_dir).resolve()
    annotation_path = Path(annotation_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    annotation = load_annotation(annotation_path, manifest["viewport"])
    template_manifest = json.loads((Path(annotation["template_run"]) / "manifest.json").read_text(encoding="utf-8"))
    if manifest["game"] != template_manifest["game"]:
        raise ValueError("control run belongs to a different game")
    indices = list(frame_indices)
    if any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < manifest["frame_count"] for i in indices):
        raise ValueError("control frame indices must exist in this run")
    if len(set(indices)) != len(indices):
        raise ValueError("control frame indices must be unique")
    new_points = []
    for index in indices:
        image = cv2.imread(str(run_dir / "frames" / f"{index:06d}.png"))
        if image is None:
            raise ValueError(f"cannot read control frame {index}")
        x, y = _select_point(image, f"Independent anchor: {run_dir.name} frame {index}; click then Enter")
        new_points.append({"run_id": str(run_dir), "frame_index": index, "x": x, "y": y, "source": "human"})
    replaced = {(str(run_dir), index) for index in indices} | {(run_dir.name, index) for index in indices}
    annotation["control_points"] = [point for point in annotation.get("control_points", [])
                                    if (point["run_id"], point["frame_index"]) not in replaced] + new_points
    if event_frames is not None:
        names = ("takeoff_frame", "apex_frame", "landing_frame")
        if isinstance(event_frames, dict):
            selected_events = {name: event_frames[name] for name in names}
        else:
            if len(event_frames) != 3:
                raise ValueError("event_frames requires takeoff, apex and landing")
            selected_events = dict(zip(names, event_frames))
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < manifest["frame_count"]
               for value in selected_events.values()):
            raise ValueError("event frame indices must exist in this run")
        annotation["control_events"] = [event for event in annotation.get("control_events", [])
                                        if event["run_id"] not in (str(run_dir), run_dir.name)]
        annotation["control_events"].append({"run_id": str(run_dir), "source": "human", **selected_events})
    annotation["validation_status"] = "pending"
    validate_annotation(annotation, manifest["viewport"])
    annotation_path.write_text(json.dumps(annotation, indent=2) + "\n", encoding="utf-8")
    return annotation
