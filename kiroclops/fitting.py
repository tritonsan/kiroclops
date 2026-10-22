"""Two transparent 60 Hz motion families, fitted only to calibration pixels."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

FPS = 60


def basis(steps, held_steps=0):
    """Positive-up semi-implicit integration, one row per elapsed physics tick.

    During held steps velocity is reset to v0; afterwards gravity is applied
    before movement. The first sample is the last grounded frame (step zero).
    """
    n = np.asarray(steps, dtype=float)
    free = np.maximum(0., n - held_steps)
    return np.column_stack((n / FPS, -free * (free + 1.) / (2. * FPS * FPS)))


def simulate_profile(profile, elapsed_ticks, input_hold_ticks, *, takeoff_tick=0, input_start_tick=0):
    """Reference discrete model for adapter tests; never a playback trajectory."""
    held = 0
    if profile["family"] == "held_velocity":
        release = input_start_tick + min(input_hold_ticks, profile["hold_ticks"])
        held = max(0, release - 1 - takeoff_tick)
    return basis(elapsed_ticks, held) @ np.asarray([profile["v0_body_s"], profile["gravity_body_s2"]])


def _observations(report):
    if report.get("fps") != FPS or report.get("status") != "ok":
        raise ValueError("fit requires successful 60 Hz analyses")
    if report.get("scenario", {}).get("split") != "calibration":
        raise ValueError("validation data must never be used for fitting")
    if report.get("variant") != "reference":
        raise ValueError("fit accepts reference analyses only")
    if report.get("valid_ratio", 0) < .95 or report.get("camera_valid") is not True:
        raise ValueError("fit requires reliable fixed-camera tracking")
    takeoff, landing = report["events"].get("takeoff_frame"), report["events"].get("landing_frame")
    if takeoff is None or landing is None:
        raise ValueError("fit requires observed takeoff and landing")
    by_index = {f["frame_index"]: f for f in report["frames"]}
    origin = by_index[takeoff]
    takeoff_tick = origin.get("physics_tick", takeoff)
    capture_tick_origin = report["frames"][0].get("physics_tick", 0)
    samples = [f for f in report["frames"] if takeoff < f["frame_index"] < landing
               and f["valid"] and f.get("height_body") is not None]
    if len(samples) < 5:
        raise ValueError("not enough airborne samples")
    steps = np.asarray([f.get("physics_tick", f["frame_index"]) - takeoff_tick for f in samples], dtype=float)
    values = np.asarray([f["height_body"] for f in samples], dtype=float)
    if not np.isfinite(values).all() or np.any(steps <= 0):
        raise ValueError("invalid fit observations")
    return {"steps": steps, "values": values, "takeoff_tick": takeoff_tick,
            "input_start_tick": capture_tick_origin + report["scenario"].get("start_tick", 30),
            "input_hold_ticks": report["scenario"]["hold_ticks"]}


def _candidate(observations, family, cap=0):
    matrices, values = [], []
    for observation in observations:
        held = 0
        if family == "held_velocity":
            absolute_release = observation["input_start_tick"] + min(observation["input_hold_ticks"], cap)
            held = max(0, absolute_release - 1 - observation["takeoff_tick"])
        matrices.append(basis(observation["steps"], held))
        values.append(observation["values"])
    matrix, observed = np.vstack(matrices), np.concatenate(values)
    parameters, _, rank, _ = np.linalg.lstsq(matrix, observed, rcond=None)
    if rank != 2 or not np.isfinite(parameters).all() or np.any(parameters <= 0):
        return None
    residual = matrix @ parameters - observed
    rss = float(residual @ residual)
    count, parameter_count = len(observed), 2 + int(family == "held_velocity")
    bic = count * math.log(max(rss / count, 1e-12)) + parameter_count * math.log(count)
    return {"family": family, "v0_body_s": float(parameters[0]),
            "gravity_body_s2": float(parameters[1]), "hold_ticks": int(cap),
            "bic": bic, "rmse_body": math.sqrt(rss/count), "sample_count": count,
            "parameter_count": parameter_count}


def fit_profile(analysis_paths, output_path):
    if isinstance(analysis_paths, (str, Path)):
        analysis_paths = [analysis_paths]
    paths = [Path(path).resolve() for path in analysis_paths]
    if len(paths) < 2:
        raise ValueError("at least two calibration runs with different hold lengths are required")
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if len({r.get("game") for r in reports}) != 1:
        raise ValueError("reference calibration runs must belong to one game")
    observations = [_observations(r) for r in reports]
    holds = {o["input_hold_ticks"] for o in observations}
    if len(holds) < 2 or any(isinstance(h, bool) or not isinstance(h, int) or h < 1 or h > 180 for h in holds):
        raise ValueError("need distinct positive input hold lengths of at most 180 ticks")
    ballistic = _candidate(observations, "ballistic")
    held_candidates = [_candidate(observations, "held_velocity", cap) for cap in range(1, max(holds)+1)]
    held_candidates = [candidate for candidate in held_candidates if candidate]
    held = min(held_candidates, key=lambda c: c["bic"]) if held_candidates else None
    candidates = [c for c in (ballistic, held) if c]
    if not candidates:
        raise ValueError("neither supported family has a positive identifiable fit")
    chosen = min(candidates, key=lambda c: c["bic"])
    validation_states = [r.get("annotation_validation", {}).get("status", "pending") for r in reports]
    validation_status = "fail" if "fail" in validation_states else (
        "pass" if all(s == "pass" for s in validation_states) else "pending")
    result = {"schema_version": 1, **{key: chosen[key] for key in
              ("family", "v0_body_s", "gravity_body_s2", "hold_ticks")},
              "validation_status": validation_status,
              "fit_diagnostics": {"bic": chosen["bic"], "rmse_body": chosen["rmse_body"],
                                  "sample_count": chosen["sample_count"], "families": candidates,
                                  "selection": "least-squares with BIC; 2 versus 3 parameters",
                                  "hold_cap_at_search_boundary": chosen["family"] == "held_velocity" and chosen["hold_ticks"] == max(holds),
                                  "integration": "60Hz: held steps set upward velocity to v0; free steps apply gravity then move",
                                  "hold_timing": "pressed ticks [start,start+min(input_hold,cap)); remaining steps = release-1-last_ground_tick",
                                  "limits": "Only these two families compared; no claim to recover original engine parameters."},
              "provenance": {"source": "calibration rendered-pixel observations only", "fps": FPS,
                             "calibration_runs": [r.get("run_id", str(p.parent)) for r, p in zip(reports, paths)],
                             "analysis_files": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]}}
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result
