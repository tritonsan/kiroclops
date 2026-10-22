"""Portable frame packages and timestamp-preserving video imports."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import tempfile

import cv2
import numpy as np

from .schema import VisualError, digest, file_sha256, finite, read_json, safe_path, validate_inputs, write_json

MAX_FRAMES = 6000
MAX_PIXELS = 1920 * 1080
MAX_DURATION_S = 60


def read_image(path):
    try:
        data = Path(path).read_bytes()
        if Path(path).suffix.lower() == '.png' or data.startswith(b'\x89PNG'):
            from .png import dimensions
            dimensions(data[:33], max_pixels=MAX_PIXELS)
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except OSError as exc:
        raise VisualError("missing_frame", str(exc)) from exc
    if image is None:
        raise VisualError("invalid_frame", f"Unreadable image: {Path(path).name}")
    return image


def _size(image):
    height, width = image.shape[:2]
    if height * width > MAX_PIXELS:
        raise VisualError("unsupported_input", "Frame exceeds the supported 1920x1080 pixel budget")
    return [width, height]


def _timestamps(timestamps):
    values = [finite(t, "frame time_s", minimum=0) for t in timestamps]
    if not 2 <= len(values) <= MAX_FRAMES:
        raise VisualError("unsupported_input", "Expected 2..6000 frames")
    if any(b <= a for a, b in zip(values, values[1:])):
        raise VisualError("capture_timing_failure", "Frame timestamps must be strictly increasing")
    if values[-1] - values[0] > MAX_DURATION_S:
        raise VisualError("unsupported_input", "Clips longer than 60 seconds are unsupported")
    return values


def _frame_metadata(values, times):
    if values is None:
        return [{} for _ in times]
    if not isinstance(values, list) or len(values) != len(times):
        raise VisualError("capture_timing_failure", "Time metadata must bind every frame")
    result = []
    for value, timestamp in zip(values, times):
        if not isinstance(value, dict) or set(value) - {"timestamp_source", "uncertainty_s", "time_interval_s"}:
            raise VisualError("invalid_document", "Unexpected per-frame timing field")
        if not isinstance(value.get("timestamp_source"), str) or not value["timestamp_source"]:
            raise VisualError("capture_timing_failure", "Explicit timestamp source required")
        uncertainty = finite(value.get("uncertainty_s"), "uncertainty_s", minimum=0)
        interval = value.get("time_interval_s")
        if not isinstance(interval, list) or len(interval) != 2:
            raise VisualError("capture_timing_failure", "Time interval requires lower and upper bounds")
        low, high = [finite(x, "time bound") for x in interval]
        if not low <= timestamp <= high or max(timestamp-low, high-timestamp) > uncertainty + 1e-6:
            raise VisualError("capture_timing_failure", "Timestamp uncertainty does not enclose observation interval")
        result.append(deepcopy(value))
    return result


def create_package(output, frame_sources, timestamps, inputs=None, metadata=None, frame_metadata=None):
    output = Path(output).resolve()
    if (output / "manifest.json").exists():
        raise VisualError("output_exists", "Never overwrite an existing package")
    sources = [Path(p).resolve() for p in frame_sources]
    times = _timestamps(timestamps)
    if len(sources) != len(times):
        raise VisualError("capture_timing_failure", "Exactly one timestamp per frame is required")
    time_metadata = _frame_metadata(frame_metadata, times)
    inputs = validate_inputs(deepcopy(inputs or {"provenance": "unknown", "events": []}))
    image_size = _size(read_image(sources[0]))
    output.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, (source, timestamp) in enumerate(zip(sources, times)):
        if _size(read_image(source)) != image_size:
            raise VisualError("invalid_frame", "Image size changed inside the recording")
        relative = f"frames/{index:06d}.png"
        target = safe_path(output, relative, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target:
            if target.exists():
                raise VisualError("output_exists", f"Frame already exists: {relative}")
            if source.suffix.lower() == ".png":
                shutil.copyfile(source, target)
            else:
                ok, data = cv2.imencode(".png", read_image(source))
                if not ok:
                    raise VisualError("invalid_frame", "PNG encoding failed")
                target.write_bytes(data.tobytes())
        frames.append({"index": index, "path": relative, "time_s": timestamp, "sha256": file_sha256(target), **time_metadata[index]})
    manifest = {"kind": "VideoPackage", "schema_version": 1, "producer": "kiroclops.visual/1.0.0",
                "image_size": image_size, "frames": frames, "inputs": inputs, "metadata": deepcopy(metadata or {})}
    manifest["package_sha256"] = digest(manifest)
    write_json(output / "manifest.json", manifest)
    return manifest


def load_package(path, verify=True):
    if verify not in (True, False, 'hashes'):
        raise VisualError('invalid_request', 'Unknown package verification mode')
    root = Path(path).resolve()
    manifest = read_json(safe_path(root, "manifest.json"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("kind") != "VideoPackage":
        raise VisualError("unsupported_schema", "Expected VideoPackage v1")
    expected = manifest.get("package_sha256")
    if digest({k: v for k, v in manifest.items() if k != "package_sha256"}) != expected:
        raise VisualError("package_integrity_failure", "Manifest fingerprint differs")
    frames = manifest.get("frames")
    if not isinstance(frames, list) or any(not isinstance(f, dict) for f in frames):
        raise VisualError("invalid_document", "frames must be objects")
    _timestamps([f.get("time_s") for f in frames])
    if any("timestamp_source" in f for f in frames):
        _frame_metadata([{k: f[k] for k in ("timestamp_source", "uncertainty_s", "time_interval_s") if k in f}
                         for f in frames], [f["time_s"] for f in frames])
    validate_inputs(manifest.get("inputs"))
    size = manifest.get("image_size")
    if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
        raise VisualError("invalid_document", "image_size must contain positive integer width/height")
    if size[0] * size[1] > MAX_PIXELS:
        raise VisualError("unsupported_input", "Frame pixel budget exceeded")
    paths = set()
    for index, frame in enumerate(frames):
        if frame.get("index") != index or frame.get("path") in paths:
            raise VisualError("capture_timing_failure", "Missing or repeated frame indices/assets")
        asset = safe_path(root, frame.get("path"))
        paths.add(frame["path"])
        if verify:
            if file_sha256(asset) != frame.get("sha256"):
                raise VisualError("package_integrity_failure", f"Frame {index} content changed")
            if verify == 'hashes':
                from .png import dimensions
                with asset.open('rb') as stream:
                    actual_size = dimensions(stream.read(33), max_pixels=MAX_PIXELS)
            else:
                actual_size = _size(read_image(asset))
            if actual_size != size:
                raise VisualError("invalid_frame", "Frame dimensions differ from manifest")
    return manifest


def frame_paths(path, manifest=None):
    manifest = manifest or load_package(path)
    return [safe_path(path, row["path"]) for row in manifest["frames"]]


def copy_package(source, destination):
    manifest = load_package(source)
    result = create_package(destination, frame_paths(source, manifest), [f["time_s"] for f in manifest["frames"]],
                            manifest["inputs"], manifest["metadata"],
                            [{k: f[k] for k in ("timestamp_source", "uncertainty_s", "time_interval_s") if k in f}
                             for f in manifest["frames"]] if "timestamp_source" in manifest["frames"][0] else None)
    if result["package_sha256"] != manifest["package_sha256"]:
        raise VisualError("package_integrity_failure", "Copied package differs")
    return result


def timing_quality(manifest, *, uncertainty_s=0., maximum_gap_s=.12):
    intervals = np.diff([f["time_s"] for f in manifest["frames"]])
    uncertainty = finite(uncertainty_s, "clock uncertainty", minimum=0)
    values = {"median_interval_s": float(np.median(intervals)), "p95_interval_s": float(np.percentile(intervals, 95)),
              "maximum_gap_s": float(np.max(intervals)), "clock_uncertainty_s": uncertainty}
    values["pass"] = (values["median_interval_s"] <= .05 and values["p95_interval_s"] <= .08
                      and values["maximum_gap_s"] <= maximum_gap_s and uncertainty <= .02)
    return values


def import_video(source, output, *, inputs=None, ffmpeg=None, ffprobe=None):
    source = Path(source).resolve()
    ffmpeg, ffprobe = ffmpeg or shutil.which("ffmpeg"), ffprobe or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VisualError("missing_dependency", "FFmpeg and ffprobe are required")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries",
               "frame=best_effort_timestamp_time,width,height", "-of", "json", str(source)]
    result = subprocess.run(command, capture_output=True, timeout=120, **flags)
    if result.returncode:
        raise VisualError("unsupported_input", result.stderr.decode("utf-8", "replace")[-1500:])
    import json
    try:
        frames = json.loads(result.stdout)["frames"]
        raw_times = [finite(float(f["best_effort_timestamp_time"]), "media PTS") for f in frames]
        if not raw_times:
            raise ValueError("No frames")
        for frame in frames:
            if int(frame["width"]) * int(frame["height"]) > MAX_PIXELS:
                raise VisualError("unsupported_input", "Video exceeds pixel budget")
        times = _timestamps([t - raw_times[0] for t in raw_times])
    except (KeyError, ValueError, TypeError) as exc:
        if isinstance(exc, VisualError):
            raise
        raise VisualError("capture_timing_failure", "Video must have usable per-frame timestamps") from exc
    with tempfile.TemporaryDirectory(prefix="kiroclops-video-") as temporary:
        command = [ffmpeg, "-v", "error", "-i", str(source), "-map", "0:v:0", "-fps_mode", "passthrough",
                   "-start_number", "0", str(Path(temporary) / "%06d.png")]
        result = subprocess.run(command, capture_output=True, timeout=180, **flags)
        if result.returncode:
            raise VisualError("unsupported_input", result.stderr.decode("utf-8", "replace")[-1500:])
        paths = sorted(Path(temporary).glob("*.png"))
        return create_package(output, paths, times, inputs=inputs,
                              metadata={"source_sha256": file_sha256(source), "media_origin_pts_s": raw_times[0],
                                        "timestamp_source": "ffprobe.best_effort_timestamp_time"})
