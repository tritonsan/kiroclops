"""Explicit, fixed pixel templates for horizontal movement."""
from __future__ import annotations

import json
from pathlib import Path

from kiroclops.annotation import MANUAL_SOURCES, _numbers
from kiroclops.annotation import validate_annotation as _legacy_validate


def validate_annotation(annotation, viewport=None):
    if not isinstance(annotation, dict):
        raise ValueError("annotation must be an object")
    # Reuse shape/texture-coordinate validation only, not jump event semantics.
    checked = dict(annotation, schema_version=1, control_events=[], template_rotations=[0])
    if annotation.get("schema_version") not in (1, 2):
        raise ValueError("ground annotation schema_version must be 1 or 2")
    if not isinstance(annotation.get("control_points", []), list) or any(not isinstance(p, dict) for p in annotation.get("control_points", [])):
        raise ValueError("control_points must be a list of point objects")
    if annotation.get("template_rotations", [0]) != [0]:
        raise ValueError("ground templates support horizontal mirroring only")
    _legacy_validate(checked, viewport)
    mirror = annotation.get("horizontal_mirror", True)
    if type(mirror) is not bool:
        raise ValueError("horizontal_mirror must be boolean")
    if annotation.get("control_events"):
        raise ValueError("ground annotations use pixel controls, not jump events")
    for point in annotation.get("control_points", []):
        if point["frame_index"] >= 240:
            raise ValueError("control frame_index must be within the native 240-frame recording")
        _numbers([point.get("x"), point.get("y")], 2, "control point")
        if viewport and not (0 <= point["x"] < viewport[0] and 0 <= point["y"] < viewport[1]):
            raise ValueError("control point lies outside native viewport")
    return annotation


def load_annotation(path, viewport=None):
    return validate_annotation(json.loads(Path(path).read_text(encoding="utf-8")), viewport)
