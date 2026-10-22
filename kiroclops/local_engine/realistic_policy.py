"""Frozen, prospective tolerances derived from independently reviewed pixels.

This policy does not change legacy suite limits. A review receipt is a trusted
input, not a claim that this module independently inspected its source images.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
import statistics

from kiroclops.visual.schema import VisualError, digest

POLICY_ID = "realistic-behavior-v1"
RULES = {
    "minimum_valid_ratio": .95,
    "minimum_control_points": 20,
    "minimum_pixel_floor": 2.,
    "maximum_calibration_p95_px": 3.,
    "curve_fraction": .05,
    "endpoint_fraction": .03,
    "size_fraction": .03,
    "overshoot_fraction": .05,
    "settle_band_fraction": .02,
    "minimum_time_tolerance_s": .15,
    "time_fraction": .15,
    "relative_lag_tolerance_s": .15,
    "meaningful_gain_fraction": .25,
    "strong_gain_fraction": .50,
    "gain_required": False,
    "maximum_input_history_difference_s": .030,
    "minimum_input_history_difference_s": .020,
    "input_history_frame_factor": 1.5,
    "capture_retry_count": 1,
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _domain(value):
    from kiroclops.web_local.specs import canonical_domain
    try:
        return canonical_domain(value)
    except ValueError as error:
        raise VisualError("invalid_acceptance_policy", str(error)) from error


def percentile95(values):
    """Deterministic linear percentile; identical to NumPy's default method."""
    ordered = sorted(values)
    if not ordered:
        raise VisualError("calibration_unqualified", "No independent position errors supplied.")
    index = .95 * (len(ordered) - 1)
    low = math.floor(index)
    high = math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def build_policy(domain, calibration_review):
    """Freeze one floor per object, using calibration-only independent points."""
    domain = _domain(domain)
    if (not isinstance(calibration_review, dict)
            or calibration_review.get("status") != "pass"
            or calibration_review.get("basis") != "independent_raw_pixels"
            or not isinstance(calibration_review.get("points"), list)):
        raise VisualError("calibration_unqualified", "A passing independent raw-pixel calibration receipt is required.")
    groups = {}
    identities = set()
    for point in calibration_review["points"]:
        if (not isinstance(point, dict) or not isinstance(point.get("object_id"), str)
                or not point["object_id"] or not _sha(point.get("frame_sha256"))
                or not _number(point.get("error_px")) or point["error_px"] < 0
                or point.get("role") not in ("reference", "original")
                or point.get("split") != "calibration" or point.get("independent") is not True):
            raise VisualError("calibration_unqualified", "Control points must bind independent calibration frames and finite pixel errors.")
        identity = (point["object_id"], point["frame_sha256"])
        if identity in identities:
            raise VisualError("calibration_unqualified", "An object cannot reuse a control frame to meet the point minimum.")
        identities.add(identity)
        groups.setdefault(point["object_id"], []).append(point["error_px"])
    if not groups:
        raise VisualError("calibration_unqualified", "At least one reviewed object is required.")
    floors, summaries = {}, {}
    for object_id, values in sorted(groups.items()):
        p95 = percentile95(values)
        if len(values) < RULES["minimum_control_points"] or p95 > RULES["maximum_calibration_p95_px"] + 1e-9:
            raise VisualError("calibration_unqualified", "Every object needs 20 distinct points and a p95 error no greater than 3 natural pixels.")
        floors[object_id] = max(RULES["minimum_pixel_floor"], p95)
        summaries[object_id] = {"point_count": len(values), "p95_error_px": p95}
    policy = {
        "kind": "AcceptancePolicy", "schema_version": 1, "id": POLICY_ID,
        "domain": domain, "rules": deepcopy(RULES), "floors_px": floors,
        "calibration": {"review_sha256": digest(calibration_review), "objects": summaries},
        "frozen_before_model": True,
    }
    policy["policy_sha256"] = digest(policy)
    validate_policy(policy, domain=domain)
    return policy


def validate_policy(policy, *, domain=None):
    if (not isinstance(policy, dict) or policy.get("kind") != "AcceptancePolicy"
            or policy.get("schema_version") != 1 or policy.get("id") != POLICY_ID
            or policy.get("rules") != RULES or policy.get("frozen_before_model") is not True
            or not isinstance(policy.get("floors_px"), dict) or not policy["floors_px"]
            or not isinstance(policy.get("calibration"), dict)):
        raise VisualError("invalid_acceptance_policy", "Expected an unchanged frozen realistic-behavior-v1 policy.")
    canonical = _domain(policy.get("domain"))
    if policy["domain"] != canonical or domain is not None and _domain(domain) != canonical:
        raise VisualError("invalid_acceptance_policy", "Policy belongs to a different browser domain.")
    calibration = policy["calibration"]
    summaries = calibration.get("objects")
    if not _sha(calibration.get("review_sha256")) or not isinstance(summaries, dict) or set(summaries) != set(policy["floors_px"]):
        raise VisualError("invalid_acceptance_policy", "Each floor must have its bound independent calibration summary.")
    for object_id, floor in policy["floors_px"].items():
        summary = summaries[object_id]
        if (not isinstance(object_id, str) or not object_id or not _number(floor) or not 2 <= floor <= 3
                or not isinstance(summary, dict) or type(summary.get("point_count")) is not int
                or summary["point_count"] < 20 or not _number(summary.get("p95_error_px"))
                or not 0 <= summary["p95_error_px"] <= 3
                or abs(floor - max(2., summary["p95_error_px"])) > 1e-9):
            raise VisualError("invalid_acceptance_policy", "Calibration floor or independent point count changed.")
    payload = {key: value for key, value in policy.items() if key != "policy_sha256"}
    if not _sha(policy.get("policy_sha256")) or digest(payload) != policy["policy_sha256"]:
        raise VisualError("invalid_acceptance_policy", "Frozen policy digest does not match its contents.")
    return policy


def policy_sha256(policy):
    validate_policy(policy)
    return policy["policy_sha256"]


def pixel_limits(policy, object_id, distance_px, scale_px=1.):
    validate_policy(policy)
    if object_id not in policy["floors_px"]:
        raise VisualError("calibration_unqualified", "The tracked object has no independent calibration floor: " + object_id)
    if not _number(distance_px) or distance_px < 0 or not _number(scale_px) or scale_px <= 0:
        raise VisualError("invalid_contract", "Reference distance and scale must be finite natural pixel measurements.")
    floor = policy["floors_px"][object_id]
    physical = {
        "curve_limit_px": max(floor, .05 * distance_px),
        "endpoint_limit_px": max(floor, .03 * distance_px),
        "size_limit_px": max(floor, .03 * distance_px),
        "overshoot_limit_px": max(floor, .05 * distance_px),
        "settle_band_px": max(floor, .02 * distance_px),
    }
    return {**physical, "pixel_floor_px": floor, "reference_distance_px": distance_px,
            "rmse_limit": physical["curve_limit_px"] / scale_px,
            "endpoint_limit": physical["endpoint_limit_px"] / scale_px,
            "size_rmse_limit": physical["size_limit_px"] / scale_px}


def gain_diagnostic(before, after, *, absolute_pass, limit):
    finite = lambda value: _number(value) and value >= 0
    gain = 1. - after / before if finite(before) and before > 1e-9 and finite(after) else None
    preserved = bool(absolute_pass and finite(before) and before <= limit + 1e-9)
    classification = ("preserved" if preserved else "strong_improvement" if gain is not None and gain >= .5
                      else "meaningful_improvement" if gain is not None and gain >= .25
                      else "no_demonstrable_gain" if gain is None else "below_meaningful_gain")
    return {"improvement_fraction": gain, "gain_required": False, "gain_pass": True,
            "gain_applicable": not preserved and finite(before) and before > 1e-9,
            "gain_applicability": "diagnostic_only", "gain_reason": classification,
            "classification": classification, "meaningful_gain_fraction": .25,
            "strong_gain_fraction": .5}


def input_identity(event):
    return {key: event[key] for key in ("action", "selector", "fraction", "destination") if key in event}


def _timeline(observation):
    timeline = observation.get("inputs", {})
    events = timeline.get("events", [])
    if (timeline.get("provenance") != "recorder_verified" or not isinstance(events, list)
            or not events or any(not isinstance(e, dict) or not _number(e.get("time_s")) for e in events)
            or any(b["time_s"] < a["time_s"] for a, b in zip(events, events[1:]))):
        raise VisualError("unverified_suite_inputs", "An ordered, recorder-verified actual input history is required.")
    return events


def _median_interval(observation):
    objects = observation.get("objects", [])
    times = [frame.get("time_s") for frame in objects[0].get("frames", [])] if objects else []
    if len(times) < 2 or any(not _number(t) for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise VisualError("capture_timing_failure", "Actual frame times are needed to freeze input-history comparability.")
    return statistics.median(b - a for a, b in zip(times, times[1:]))


def input_history_check(reference, other):
    ref, target = _timeline(reference), _timeline(other)
    if any(not _number(e.get(key))
           for events in (ref,target) for e in events for key in ('client_x','client_y')):
        raise VisualError('unverified_suite_inputs','Every trusted pointer event requires finite client coordinates.')
    median = max(_median_interval(reference), _median_interval(other))
    tolerance = min(.030, max(.020, 1.5 * median))
    if [input_identity(e) for e in ref] != [input_identity(e) for e in target]:
        return {"status": "input_history_mismatch", "pass": False,
                "reason": "input_identity_mismatch", "tolerance_s": tolerance,
                "duration_rescaled": False, "differences": []}
    rows = []
    for index, (a, b) in enumerate(zip(ref, target)):
        point_difference=None
        if any(key in a or key in b for key in ('client_x','client_y')):
            if not all(_number(e.get(key)) for e in (a,b) for key in ('client_x','client_y')):
                point_difference=math.inf
            else:
                point_difference=max(abs(a[key]-b[key]) for key in ('client_x','client_y'))
        interval_a, interval_b = a["time_s"] - ref[0]["time_s"], b["time_s"] - target[0]["time_s"]
        difference = abs(interval_a - interval_b)
        adjacent_a = a["time_s"] - ref[index - 1]["time_s"] if index else 0.
        adjacent_b = b["time_s"] - target[index - 1]["time_s"] if index else 0.
        adjacent_difference = abs(adjacent_a - adjacent_b)
        rows.append({"index": index, "identity": input_identity(a), "reference_after_first_s": interval_a,
                     "other_after_first_s": interval_b, "difference_s": difference,
                     "reference_after_previous_s": adjacent_a, "other_after_previous_s": adjacent_b,
                     "adjacent_difference_s": adjacent_difference,
                     "pointer_difference_px":point_difference if point_difference!=math.inf else None,
                     "pointer_match":point_difference is None or point_difference<=1.,
                     "pass": difference <= tolerance + 1e-9 and adjacent_difference <= tolerance + 1e-9
                         and (point_difference is None or point_difference<=1.)})
    passed = all(row["pass"] for row in rows)
    return {"status": "pass" if passed else "input_history_mismatch", "pass": passed,
            "tolerance_s": tolerance, "median_frame_interval_s": median,
            "maximum_difference_s": max(row["difference_s"] for row in rows),
            "maximum_adjacent_difference_s": max(row["adjacent_difference_s"] for row in rows),
            "duration_rescaled": False, "reference_inputs_sha256": digest(reference["inputs"]),
            "other_inputs_sha256": digest(other["inputs"]), "differences": rows}


def replay_scenario(canonical, reference):
    """Replay actual relative input intervals, retaining the canonical recipe."""
    from kiroclops.web_local.specs import validate_scenario
    validate_scenario(canonical)
    events = _timeline(reference)
    planned = canonical.get("events", [])
    if [input_identity(e) for e in planned] != [input_identity(e) for e in events]:
        raise VisualError("suite_recipe_mismatch", "Actual reference inputs differ from the canonical action sequence.")
    result = deepcopy(canonical)
    start = planned[0]["time_s"]
    for event, actual in zip(result["events"], events):
        event["time_s"] = start + actual["time_s"] - events[0]["time_s"]
        if not all(_number(actual.get(key)) for key in ('client_x','client_y')):
            raise VisualError('unverified_suite_inputs','Reference replay requires actual trusted pointer coordinates.')
        event['replay_client_point']=[actual['client_x'],actual['client_y']]
    validate_scenario(result)
    return result, {"kind": "ActualInputReplay", "schema_version": 1,
                    "canonical_scenario_sha256": digest(canonical),
                    "execution_scenario_sha256": digest(result),
                    "reference_inputs_sha256": digest(reference["inputs"]),
                    "reference_first_actual_s": events[0]["time_s"],
                    "execution_first_planned_s": start, "duration_rescaled": False}
