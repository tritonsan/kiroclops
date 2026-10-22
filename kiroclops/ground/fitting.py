"""Deterministic NumPy-only horizontal model fitting from calibration pixels."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .analysis import FRAME_COUNT, FPS, _write, scenario_phases
from .comparison import _manual, _usable

LINEAR = "accel_cap_linear_brake"
RETENTION = "accel_cap_retention"


def _directions(scenario, frame_count=FRAME_COUNT):
    phases = scenario_phases(scenario)
    directions = np.zeros(frame_count)
    for phase in phases:
        directions[phase["start"]:phase["end"]] = phase["direction"]
    return directions


def _basis(scenario, acceleration_ticks, braking, family, frame_count=FRAME_COUNT):
    acceleration_ticks, braking = np.broadcast_arrays(np.asarray(acceleration_ticks, float), np.asarray(braking, float))
    a, b = acceleration_ticks.ravel(), braking.ravel()
    v, x = np.zeros(len(a)), np.zeros(len(a))
    result = np.zeros((len(a), frame_count))
    for tick, direction in enumerate(_directions(scenario, frame_count)):
        if direction:
            v += np.clip(direction-v, -1/a, 1/a)
        elif family == LINEAR:
            v += np.clip(-v, -1/b, 1/b)
        elif family == RETENTION:
            v *= b
        else:
            raise ValueError("unsupported ground model family")
        x += v/FPS
        result[:, tick] = x
    return result


def simulate_profile(profile, scenario, frame_count=FRAME_COUNT):
    family = profile["family"]
    speed, acceleration = profile["max_speed_body_s"], profile["accel_body_s2"]
    values = [speed, acceleration]
    if family == LINEAR:
        values.append(profile["brake_body_s2"])
    elif family == RETENTION:
        values.append(profile["retention_per_tick"])
    else:
        raise ValueError("unsupported ground model family")
    if any(type(v) not in (int, float) or not np.isfinite(v) for v in values) or speed <= 0 or acceleration <= 0:
        raise ValueError("model parameters must be finite and positive")
    if family == LINEAR:
        if profile["brake_body_s2"] <= 0:
            raise ValueError("braking acceleration must be positive")
        brake = speed*FPS/profile["brake_body_s2"]
    else:
        brake = profile["retention_per_tick"]
        if not 0 <= brake < 1:
            raise ValueError("retention must lie in [0,1)")
    return speed*_basis(scenario, [speed*FPS/acceleration], [brake], family, frame_count)[0]


def _observations(value):
    path = None
    if isinstance(value, dict):
        report = value
    else:
        path = Path(value)
        if path.is_dir():
            path = path / "analysis.json"
        report = json.loads(path.read_text(encoding="utf-8"))
    if (report.get("status") != "ok" or report.get("fps") != FPS
            or report.get("variant") != "reference" or report.get("scenario", {}).get("split") != "calibration"
            or report.get("camera_valid") is not True or report.get("valid_ratio", 0) < .95):
        raise ValueError("fit requires reliable reference calibration observations only")
    phases = scenario_phases(report["scenario"])
    _usable(report)
    frames = report.get("frames", [])
    if len(frames) != FRAME_COUNT or any(f.get("frame_index") != i for i, f in enumerate(frames)):
        raise ValueError("fit requires contiguous native frame observations")
    y = np.zeros(FRAME_COUNT)
    weights = np.zeros(FRAME_COUNT)
    for phase in phases:
        indices = [i for i in range(phase["start"], phase["end"]) if frames[i].get("valid") is True]
        if len(indices)/(phase["end"]-phase["start"]) < .95:
            raise ValueError("fit phase coverage is below 95%")
        for i in indices:
            value = frames[i].get("x_body")
            if type(value) not in (int, float) or not np.isfinite(value):
                raise ValueError("valid fitting observations must be finite")
            y[i] = value
        weights[indices] = 1/(len(phases)*len(indices))
    height = report.get("body_height_px")
    if type(height) not in (int, float) or not np.isfinite(height) or height <= 0:
        raise ValueError("body scale must be fixed and finite")
    return {"report": report, "y": y, "weights": weights, "path": path,
            "count": int(np.count_nonzero(weights))}


def _score(observations, family, acceleration, braking):
    a, b = np.broadcast_arrays(acceleration, braking)
    numerator, denominator = np.zeros(a.size), np.zeros(a.size)
    squared = 0.
    for obs in observations:
        basis = _basis(obs["report"]["scenario"], a.ravel(), b.ravel(), family)
        weight = obs["weights"]/len(observations)
        numerator += basis @ (weight*obs["y"])
        denominator += (basis*basis) @ weight
        squared += float(np.dot(weight, obs["y"]**2))
    speed = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 1e-15)
    mse = squared-2*speed*numerator+speed*speed*denominator
    mse = np.maximum(mse, 0.)
    mse[(speed <= 0) | (denominator <= 1e-15)] = np.inf
    index = int(np.argmin(mse))
    return {"mse": float(mse[index]), "speed": float(speed[index]),
            "acceleration_ticks": float(a.ravel()[index]), "braking": float(b.ravel()[index])}


def _fit_family(observations, family):
    accelerations = np.unique(np.r_[np.geomspace(.25, 120, 45), np.arange(1., 13.)])
    brakes = (np.unique(np.r_[np.geomspace(.25, 180, 50), np.arange(1., 13.)]) if family == LINEAR
              else np.unique(np.r_[np.linspace(0, .9, 25), 1-np.geomspace(.0005, .1, 20)]))
    a, b = np.meshgrid(accelerations, brakes, indexing="ij")
    best = _score(observations, family, a, b)
    for iteration in range(9):
        factor = .35/(1.8**iteration)
        aa = np.clip(best["acceleration_ticks"] * (1 + np.linspace(-factor, factor, 7)), .05, 240)
        if family == LINEAR:
            bb = np.clip(best["braking"] * (1 + np.linspace(-factor, factor, 7)), .05, 360)
        else:
            bb = np.clip(best["braking"] + np.linspace(-factor, factor, 9)*max(.01, 1-best["braking"]), 0, .9999)
        a, b = np.meshgrid(aa, bb, indexing="ij")
        candidate = _score(observations, family, a, b)
        if candidate["mse"] < best["mse"]:
            best = candidate
    count = sum(obs["count"] for obs in observations)
    rmse = float(np.sqrt(best["mse"]))
    return {"family": family, "max_speed_body_s": best["speed"],
            "accel_body_s2": best["speed"]*FPS/best["acceleration_ticks"],
            **({"brake_body_s2": best["speed"]*FPS/best["braking"]} if family == LINEAR
               else {"retention_per_tick": best["braking"]}),
            "rmse_body": rmse, "bic": float(count*np.log(max(best["mse"], 1e-16))+3*np.log(count)),
            "parameter_count": 3, "sample_count": count,
            "acceleration_ticks_to_cap": best["acceleration_ticks"],
            "braking_parameter": best["braking"]}


def fit_profile(analysis_paths, output_path):
    values = [analysis_paths] if isinstance(analysis_paths, (str, Path, dict)) else list(analysis_paths)
    observations = [_observations(v) for v in values]
    reports = [o["report"] for o in observations]
    if {r["scenario"]["id"] for r in reports} != {"r06", "r48"}:
        raise ValueError("fit requires both r06 and r48 calibration scenarios")
    if len({r["game"] for r in reports}) != 1 or len({r["body_height_px"] for r in reports}) != 1:
        raise ValueError("one game and a constant body scale are required")
    keys = [(r["scenario"]["id"], r["scenario"]["repeat"], r["scenario"].get("spawn_offset",0)) for r in reports]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate calibration run slot")
    families = [_fit_family(observations, family) for family in (LINEAR, RETENTION)]
    if not all(np.isfinite(f["bic"]) for f in families):
        raise ValueError("no positive identifiable horizontal model")
    chosen = min(families, key=lambda item: item["bic"])
    delta = abs(families[0]["bic"]-families[1]["bic"])
    mismatch = all(f["rmse_body"] > .15 for f in families)
    result = {"schema_version": 1, "mechanic": "ground", "status": "mismatch" if mismatch else "ok",
              **{k:v for k,v in chosen.items() if k in {"family","max_speed_body_s","accel_body_s2","brake_body_s2","retention_per_tick"}},
              "fps": FPS, "validation_status": _manual(reports, False)["status"],
              "fit_diagnostics": {"rmse_body": chosen["rmse_body"], "bic": chosen["bic"], "families": families,
                  "model_mismatch": mismatch, "ambiguous": delta < 2., "bic_delta": delta,
                  "cap_not_observed_in_calibration": chosen["acceleration_ticks_to_cap"] > 48,
                  "subtick_acceleration_unidentified": chosen["acceleration_ticks_to_cap"] <= 1,
                  "weighting": "equal MSE per input phase within each run; equal weight per run",
                  "search": "deterministic coarse shape grid plus nine local refinements; analytical least-squares speed scale; 3-parameter BIC",
                  "integration": "60Hz input-aligned: approach signed speed cap by acceleration*dt; no-input linear brake or per-tick retention; then move",
                  "limits": "Only two supported families; no original-code constants or held-out pixels used."},
              "provenance": {"source": "reference calibration pixels only", "analysis_files": [
                  {"path": str(o["path"].resolve()), "sha256": hashlib.sha256(o["path"].read_bytes()).hexdigest()}
                  for o in observations if o["path"] is not None], "calibration_slots": keys}}
    _write(output_path, result)
    return result
