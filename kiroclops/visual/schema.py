"""Portable JSON contracts and filesystem integrity primitives."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import time
import uuid

SCHEMA_VERSION = 1


class VisualError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def file_sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path):
    def invalid(value):
        raise VisualError("invalid_document", f"Nonfinite JSON number: {value}")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VisualError("invalid_document", str(exc)) from exc


def write_json(path, value):
    path = Path(path)
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8", newline="\n")
        for attempt in range(20):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.025)
    finally:
        temp.unlink(missing_ok=True)
    return path


def finite(value, name, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise VisualError("invalid_document", f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise VisualError("invalid_document", f"{name} must be >= {minimum}")
    return float(value)


def safe_path(root, relative, must_exist=True):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        raise VisualError("unsafe_path", "Expected a relative POSIX asset path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or PureWindowsPath(relative).drive or any(p in ("..", ".", "") for p in relative.split("/")):
        raise VisualError("unsafe_path", "Asset paths must stay inside the package")
    current = root
    for part in pure.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            attrs = getattr(current.lstat(), "st_file_attributes", 0)
            if current.is_symlink() or attrs & 0x400:
                raise VisualError("unsafe_path", "Package assets cannot use symlinks or reparse points")
    resolved = current.resolve()
    if not resolved.is_relative_to(root):
        raise VisualError("unsafe_path", "Asset escaped package")
    if must_exist and not resolved.is_file():
        raise VisualError("missing_frame", f"Missing asset: {relative}")
    return resolved


def validate_inputs(inputs):
    if not isinstance(inputs, dict) or inputs.get("provenance") not in ("recorder_verified", "user_declared", "unknown"):
        raise VisualError("invalid_input_timeline", "Explicit input provenance is required")
    events = inputs.get("events")
    if not isinstance(events, list):
        raise VisualError("invalid_input_timeline", "events must be a list")
    if inputs["provenance"] == "unknown" and events:
        raise VisualError("invalid_input_timeline", "Unknown inputs cannot claim verified events")
    previous = -float("inf")
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("action"), str):
            raise VisualError("invalid_input_timeline", "Each input needs an action")
        value = finite(event.get("time_s"), "input time_s", minimum=0)
        if value < previous:
            raise VisualError("capture_timing_failure", "Inputs must be ordered by actual time")
        previous = value
    return inputs


def semantic_digest(value):
    """Only documented non-semantic envelope fields are excluded."""
    ignored = {"created_at", "request_id", "output_path", "job_id", "worker_pid"}
    return digest({k: v for k, v in value.items() if k not in ignored})
