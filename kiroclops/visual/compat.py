"""Read-only import of frozen Godot frame evidence into the common contracts."""
from __future__ import annotations

from pathlib import Path
import math
import cv2
import numpy as np

from .package import create_package, read_image
from .schema import VisualError, file_sha256, read_json


def import_legacy_run(source, output):
    source = Path(source).resolve()
    record = read_json(source / "manifest.json")
    frames = record.get("frames", [])
    if record.get("status") != "ok" or record.get("fps") != 60 or len(frames) != 240:
        raise VisualError("capture_timing_failure", "Legacy importer requires a complete qualified 60Hz/240 frame run")
    previous = set()
    events = []
    for index, frame in enumerate(frames):
        if (frame.get("frame_index") != index or frame.get("physics_tick") != index + 60
                or not isinstance(frame.get("time_s"), (int, float))
                or not math.isfinite(frame["time_s"]) or abs(frame["time_s"] - index / 60) > 1e-5):
            raise VisualError("capture_timing_failure", "Legacy frame/physics mapping differs")
        current = set(frame.get("actions", []))
        if any(not isinstance(action, str) for action in current):
            raise VisualError("invalid_input_timeline", "Legacy action list must contain names")
        for action in sorted(previous ^ current):
            events.append({"time_s": frame["time_s"], "action": action, "pressed": action in current})
        previous = current
    return create_package(output, [source / "frames" / f"{i:06d}.png" for i in range(240)],
                          [f["time_s"] for f in frames],
                          {"provenance": "recorder_verified", "events": events},
                          {"adapter": "legacy-godot-v1", "source_manifest_sha256": file_sha256(source / "manifest.json"),
                           "scenario": record.get("scenario", {}), "source_id": source.name,
                           "domain": "game", "game": record.get("game"), "variant": record.get("variant")})


def selection_from_legacy(source, annotation_path):
    """Locate a frozen appearance in the new initial frame; never read native position."""
    annotation = read_json(annotation_path)
    x, y, w, h = map(int, annotation["roi"])
    template_source = Path(annotation["template_run"]) / "frames" / f"{annotation.get('template_frame', 0):06d}.png"
    template = read_image(template_source)[y:y+h, x:x+w]
    frame = read_image(Path(source) / "frames/000000.png")
    if template.shape[:2] != (h, w) or np.max(np.std(template.astype(float), axis=(0, 1))) < 2:
        raise VisualError("tracking_failure", "Frozen template is not usable")
    response = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
    response = np.nan_to_num(response, nan=-1, posinf=-1, neginf=-1)
    _, score, _, location = cv2.minMaxLoc(response)
    if score < annotation.get("min_score", .75):
        raise VisualError("tracking_failure", "Initial appearance cannot be located")
    return {"schema_version": 1, "objects": [{"id": "character", "roi": [*location, w, h],
             "anchor": annotation["anchor_offset"], "frame_index": 0, "method": "template",
             "min_score": annotation.get("min_score", .75), "min_margin": annotation.get("min_margin", .03),
             "body_height_px": annotation["body_height_px"], "horizontal_mirror": annotation.get("horizontal_mirror", False),
             "template_rotations": annotation.get("template_rotations", [0])}],
            "background_rois": annotation["background_rois"],
            "camera_max_shift_px": annotation.get("camera_max_shift_px", 1),
            "boundaries": [], "connections": []}
