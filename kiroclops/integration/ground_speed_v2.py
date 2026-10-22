"""Supplemental native-pixel speed diagnostic; never changes frozen acceptance.

The window is chosen from the input schedule before looking at positions. A
constant-velocity line must explain every selected position within +/-0.5 native
pixel. Its feasible slopes provide deterministic rounding bounds, not statistical
confidence intervals and not protection against tracking, scale or camera error.

A comparison passes only when EVERY feasible speed pair lies within the same
10% relative-error limit. A boundary-crossing interval is indeterminate. No
window search, interpolation, repeat averaging, profile fitting, or physics
constants are used here. In particular, a still-accelerating reversal can be
unmeasurable by this constant-speed diagnostic even when its curve is useful.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Mapping

import numpy as np

from kiroclops.ground.comparison import _usable


ALGORITHM = "native-pixel-speed-v2"
ROUNDING_HALF_WIDTH_PX = 0.5
RELATIVE_ERROR_LIMIT = 0.10
EPSILON = 1e-12  # numerical comparisons, not an added measurement tolerance


@dataclass(frozen=True)
class SpeedPolicy:
    """Window policy to record and freeze BEFORE evaluating future trials.

    The default keeps the last two-thirds of the final held leg, capped at
    32 observations. Different policies require a separately registered protocol;
    this module does not choose a policy from observed success/failure.
    """
    discard_numerator: int = 1
    discard_denominator: int = 3
    maximum_samples: int = 32
    minimum_samples: int = 12
    minimum_held_ticks: int = 18

    def __post_init__(self):
        if any(type(value) is not int for value in asdict(self).values()):
            raise ValueError("Window policy fields must be integers")
        if not 0 <= self.discard_numerator < self.discard_denominator:
            raise ValueError("Discard fraction must be in [0,1)")
        if not 6 <= self.minimum_samples <= self.maximum_samples <= 240:
            raise ValueError("Require 6 <= minimum_samples <= maximum_samples <= 240 for fixed-half diagnostics")
        if not 3 <= self.minimum_held_ticks <= 240:
            raise ValueError("minimum_held_ticks must be between 3 and 240")

    def record(self):
        record = {"algorithm": ALGORITHM, **asdict(self),
                  "rounding_half_width_px": ROUNDING_HALF_WIDTH_PX,
                  "relative_error_limit": RELATIVE_ERROR_LIMIT,
                  "selection": "last held leg; discard ceil(length*numerator/denominator); keep final maximum_samples; never shorten after inspecting pixels"}
        digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {**record, "sha256": digest}


DEFAULT_POLICY = SpeedPolicy()
# Explicit future protocol revision, never a replacement for historical gates.
# Eligibility depends only on input duration, not observed reference/target speed.
LONG_HOLD_POLICY = SpeedPolicy(minimum_held_ticks=30, minimum_samples=20)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _line(values_px, fps):
    """Exact feasible slope intersection for x_i = a + v*t_i +/- 0.5px.

    Pairwise constraints are sufficient: after fixing v, all intercept intervals
    must intersect. No distribution/independence assumption is made about rounding.
    """
    values = np.asarray(values_px, dtype=float)
    times = np.arange(len(values), dtype=float) / fps
    centered = times - times.mean()
    ols = float(np.dot(centered, values-values.mean()) / np.dot(centered, centered))
    ols_intercept = float(values.mean() - ols*times.mean())
    ols_residual = float(np.max(np.abs(values-(ols_intercept+ols*times))))
    rounding_bound = float(ROUNDING_HALF_WIDTH_PX * np.sum(np.abs(centered)) / np.dot(centered, centered))
    low, high = -math.inf, math.inf
    for i in range(len(values)-1):
        dt = times[i+1:]-times[i]
        dx = values[i+1:]-values[i]
        low = max(low, float(np.max((dx-2*ROUNDING_HALF_WIDTH_PX)/dt)))
        high = min(high, float(np.min((dx+2*ROUNDING_HALF_WIDTH_PX)/dt)))
    if not all(math.isfinite(value) for value in (ols, ols_intercept, ols_residual, rounding_bound, low, high)):
        raise ValueError("native line calculations are nonfinite")
    feasible = low <= high + EPSILON
    result = {"status": "compatible" if feasible else "model_mismatch",
              "sample_count": len(values), "ols_speed_px_s": ols,
              "ols_rounding_bound_px_s": rounding_bound,
              "ols_max_residual_px": ols_residual,
              "pairwise_lower_speed_px_s": low, "pairwise_upper_speed_px_s": high,
              "feasible_speed_interval_px_s": None, "speed_px_s": None,
              "feasible_intercept_px": None, "feasible_line_max_residual_px": None}
    if feasible:
        # Equal bounds can cross by floating-point noise. Collapse only at the
        # declared numerical epsilon, never by an extra pixel/noise allowance.
        if low > high:
            low = high = (low+high)/2
        speed = min(high, max(low, ols))
        intercept_low = float(np.max(values-ROUNDING_HALF_WIDTH_PX-speed*times))
        intercept_high = float(np.min(values+ROUNDING_HALF_WIDTH_PX-speed*times))
        intercept = (intercept_low+intercept_high)/2
        result.update(feasible_speed_interval_px_s=[low, high], speed_px_s=speed,
                      feasible_intercept_px=intercept,
                      feasible_line_max_residual_px=float(np.max(np.abs(values-(intercept+speed*times)))))
    return result


def _base(report, policy):
    try:
        content_sha = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        content_sha = None
    return {"schema_version": 1, "algorithm": ALGORITHM, "scope": "supplemental_v2_only",
            "grants_legacy_acceptance": False, "policy": policy.record(),
            "analysis_content_sha256": content_sha, "run_id": report.get("run_id"),
            "game": report.get("game"), "variant": report.get("variant"),
            "scenario": report.get("scenario"), "status": "invalid", "speed_eligible": None,
            "eligibility_basis": None,
            "window": None, "direction": None, "speed_body_s": None,
            "ols_speed_body_s": None, "feasible_speed_interval_body_s": None,
            "quantization": None, "stationarity": None, "errors": [],
            "limitations": [
                "Intervals assume only +/-0.5 native pixel rounding and a fixed physical anchor, camera and body scale; other errors are not covered.",
                "A feasible constant-speed line means compatible at this resolution, not proven constant physical velocity.",
                "An incompatible line can reflect acceleration, reversal, tracking error or model mismatch; this diagnostic does not identify the cause.",
                "The fixed final-two-thirds window may include continuing reversal acceleration. Do not search for a passing suffix; preregister a different measurement experiment if needed.",
                "Deterministic repeated images do not narrow these bounds; no repeated-run averaging is performed.",
                "Legacy numerical/manual/source/budget/coverage gates are unchanged and cannot be granted by this result."]}


def estimate_speed(report: Mapping, *, policy: SpeedPolicy = DEFAULT_POLICY) -> dict:
    """Consume one existing ground analysis dict without reading or writing files.

    status: ok | not_applicable | insufficient_data | model_mismatch | invalid.
    OLS and the rounding-feasible interval use the SAME predetermined window.
    Mirroring is already resolved in the tracker's physical x anchor; the mirror
    flag never multiplies x or speed and therefore cannot double-flip direction.
    """
    if not isinstance(report, dict) or not isinstance(policy, SpeedPolicy):
        raise TypeError("An analysis dict and SpeedPolicy are required")
    result = _base(report, policy)
    try:
        phases = _usable(report)  # read-only frozen timing/camera/coverage validation
        held = phases[-2]
        length = held["end"]-held["start"]
        result.update(direction=held["direction"], speed_eligible=length >= policy.minimum_held_ticks)
        result["eligibility_basis"] = {"source": "input schedule only", "last_held_ticks": length,
                                       "minimum_held_ticks": policy.minimum_held_ticks}
        if not result["speed_eligible"]:
            result.update(status="not_applicable", errors=[f"last held leg is {length} ticks, shorter than the registered {policy.minimum_held_ticks}-tick minimum"])
            return result
        discard = (length*policy.discard_numerator+policy.discard_denominator-1)//policy.discard_denominator
        start = max(held["start"]+discard, held["end"]-policy.maximum_samples)
        indices = list(range(start, held["end"]))
        result["window"] = {"selection_basis": "input schedule only", "held_start": held["start"],
                            "held_end_exclusive": held["end"], "start_frame": start,
                            "end_frame_exclusive": held["end"], "frame_indices": indices,
                            "sample_count": len(indices), "elapsed_s": (len(indices)-1)/report["fps"]}
        if len(indices) < policy.minimum_samples:
            result.update(status="insufficient_data", errors=["fixed window has fewer than the registered minimum samples"])
            return result
        frames = [report["frames"][i] for i in indices]
        if not all(frame["valid"] for frame in frames):
            result.update(status="insufficient_data", errors=["fixed window contains invalid frames; no interpolation or alternative suffix is allowed"])
            return result
        if any(not _finite(frame.get("x")) for frame in frames):
            raise ValueError("selected native x positions must be finite")
        height, fps = report["body_height_px"], report["fps"]
        values = [frame["x"] for frame in frames]
        # x_body must describe the same physical track, up to one constant origin.
        offsets = [frame["x"]-height*frame["x_body"] for frame in frames]
        if max(offsets)-min(offsets) > 1e-7:
            raise ValueError("raw x and x_body disagree about the physical track or scale")
        line = _line(values, fps)
        halves = [values[:len(values)//2], values[len(values)//2:]]
        result["stationarity"] = {"status": line["status"], "model": "one constant-velocity line in the entire fixed window",
            "full_window": line, "fixed_halves": [_line(part, fps) for part in halves],
            "window_adapted_after_observation": False}
        result["ols_speed_body_s"] = line["ols_speed_px_s"]/height
        result["quantization"] = {"native_half_width_px": ROUNDING_HALF_WIDTH_PX,
            "ols_worst_case_bound_body_s": line["ols_rounding_bound_px_s"]/height,
            "method": "all-pair +/-1px displacement constraints; common intercept; no independent-noise or repeat-count assumption"}
        if line["status"] != "compatible":
            result.update(status="model_mismatch", errors=["no constant-speed line explains every fixed-window pixel within +/-0.5px"])
            return result
        interval = [value/height for value in line["feasible_speed_interval_px_s"]]
        result.update(status="ok", speed_body_s=line["speed_px_s"]/height,
                      feasible_speed_interval_body_s=interval)
        return result
    except (KeyError, TypeError, ValueError, IndexError) as error:
        result.update(status="invalid", errors=[str(error)])
        return result


def compare_speeds(reference: Mapping, target: Mapping, *, policy: SpeedPolicy = DEFAULT_POLICY) -> dict:
    """Supplemental comparison: pass, fail, indeterminate, invalid or N/A.

    The 10% tolerance is constant. Uncertainty can prevent a decision; it never
    increases the tolerance or converts an ambiguous estimate into a pass.
    """
    estimates = [estimate_speed(value, policy=policy) for value in (reference, target)]
    result = {"schema_version": 1, "algorithm": ALGORITHM, "scope": "supplemental_v2_only",
              "grants_legacy_acceptance": False, "policy": policy.record(),
              "status": "indeterminate", "pass": None, "speed_eligible": None,
              "relative_error_limit": RELATIVE_ERROR_LIMIT, "speed_relative_error": None,
              "relative_error_interval": None, "reference": estimates[0], "target": estimates[1],
              "reference_eligibility": {"basis": "reference only; registered fixed window",
                  "status": "unverified", "nonstationarity_automatically_exempted": False}, "errors": []}
    def slot(value):
        scenario = value.get("scenario", {})
        return (scenario.get("id"), scenario.get("split"), scenario.get("spawn_offset", 0), scenario.get("repeat"))
    statuses = [value["status"] for value in estimates]
    if "invalid" in statuses:
        result.update(status="invalid", errors=["one or more analysis inputs are invalid"])
        return result
    if slot(reference) != slot(target):
        result.update(status="invalid", errors=["reference and target input-aligned scenario slots differ"])
        return result
    if statuses == ["not_applicable", "not_applicable"]:
        result["reference_eligibility"]["status"] = "not_applicable_short_press"
        result.update(status="not_applicable", speed_eligible=False)
        return result
    result["speed_eligible"] = True
    if statuses[0] != "ok":
        result["reference_eligibility"]["status"] = statuses[0]
        result["errors"] = ["fixed-window speed is not identifiable under the constant-speed rounding model"]
        return result
    direction = estimates[0]["direction"]
    ref = sorted(direction*value for value in estimates[0]["feasible_speed_interval_body_s"])
    if ref[0] <= EPSILON:
        result["reference_eligibility"]["status"] = "unresolved_reference_direction_or_zero_speed"
        result["errors"] = ["reference speed is not resolvably positive in the commanded direction"]
        return result
    result["reference_eligibility"]["status"] = "constant_speed_supported_at_native_resolution"
    if statuses[1] == "model_mismatch":
        result.update(status="fail", **{"pass": False},
                      errors=["reference supports a constant speed but the target does not; target nonstationarity cannot exempt its speed check"])
        return result
    if statuses[1] != "ok":
        result["errors"] = ["target fixed-window speed has insufficient data; no alternative window or exemption"]
        return result
    tgt = sorted(direction*value for value in estimates[1]["feasible_speed_interval_body_s"])
    ratios = [t/r for t in tgt for r in ref]
    ratio_low, ratio_high = min(ratios), max(ratios)
    minimum = 0. if ratio_low <= 1 <= ratio_high else min(abs(ratio_low-1), abs(ratio_high-1))
    maximum = max(abs(ratio_low-1), abs(ratio_high-1))
    result["relative_error_interval"] = [minimum, maximum]
    ref_point, target_point = (value["speed_body_s"] for value in estimates)
    result["speed_relative_error"] = abs(target_point-ref_point)/abs(ref_point)
    if maximum <= RELATIVE_ERROR_LIMIT+EPSILON:
        result.update(status="pass", **{"pass": True})
    elif minimum > RELATIVE_ERROR_LIMIT+EPSILON:
        result.update(status="fail", **{"pass": False})
    else:
        result["errors"] = ["rounding-feasible speed pairs lie on both sides of the unchanged 10% boundary"]
    return result
