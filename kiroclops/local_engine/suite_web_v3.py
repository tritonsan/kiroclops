"""V3 panel reference suite: recipes, retest-aware window gates and capture.

Panel recipes add the narrow-viewport interrupted close and reversal to
calibration, re-label every V2-scored recipe as known regression, and add new
held-out recipes at viewport widths and timings never recorded before.

SVG is not served here. It keeps the unchanged V2 recipe objects and the V1
window rule (``suite_web_v2``); its V3 change is the endpoint measurement and
the review rules, which the V3b source epoch carries by itself.

The scenario ``suite_version`` stays ``reference-suite-web-v2`` on purpose: the
natural recorder enables its panel replay-boundary fix only for that value. The
V3 identity is carried by ``v3_suite_version``, the case-id prefix and the
acceptance profile.
"""
from __future__ import annotations

from copy import deepcopy

from kiroclops.visual.schema import VisualError
from kiroclops.web_local.specs import canonical_domain, validate_scenario

from . import realistic_policy_v3 as policy_v3
from . import suite_web as legacy
from . import suite_web_v2 as v2

V3_VERSION = "reference-suite-web-v3"
NARROW_CALIBRATION_WIDTH = (768, 640)
NEW_VALIDATION_SIZES = ((640, 640), (1120, 720))


def _panel_recipes(split):
    first, second = "#open-panel", "#close-panel"
    if split == "calibration":
        return [("interrupt", [legacy._click(.4, first), legacy._click(.70, second)], 3.),
                ("reverse", [legacy._click(.4, first), legacy._click(.70, second), legacy._click(.92, first)], 3.8)]
    return [("interrupt-v3", [legacy._click(.4, first), legacy._click(.54, second)], 3.),
            ("reverse-v3", [legacy._click(.4, first), legacy._click(.54, second), legacy._click(.76, first)], 3.8),
            ("drag-release-v3", [legacy._click(.4, first),
                                 *legacy._drag("panel", .95, duration=.24, release=1.25, travel=.45)], 3.8)]


def _new_case(name, events, duration, split, width, height, repeat):
    case_id = f"realistic-v3-suite-panel-{split}-{name}-w{width}-r{repeat}"
    value = {
        "id": case_id, "case_id": case_id, "recipe": name, "domain": "panel",
        "split": split, "suite_split": split, "repeat": repeat,
        "viewport": [width, height], "device_scale_factor": 1,
        "duration_s": duration, "events": deepcopy(events),
        "timing_limits": dict(legacy.TIMING_LIMITS), "version": V3_VERSION,
        "suite_version": v2.VERSION, "v3_suite_version": V3_VERSION,
        "blind_validation": split == "validation", "frame_driver": "natural_browser_raf_v1",
        "acceptance_profile_id": policy_v3.POLICY_ID,
    }
    validate_scenario(value)
    return value


def _mark_v3(scenario, *, suite_split, origin):
    scenario["v3_suite_version"] = V3_VERSION
    scenario["acceptance_profile_id"] = policy_v3.POLICY_ID
    scenario["suite_split"] = suite_split
    scenario["v3_origin"] = origin
    if suite_split == "regression":
        scenario["blind_validation"] = False
    validate_scenario(scenario)
    return scenario


def scenarios(domain, split):
    domain = canonical_domain(domain)
    split = {"known": "regression", "known_regression": "regression"}.get(split, split)
    if split not in {"calibration", "regression", "validation"}:
        raise VisualError("invalid_suite_split", "Expected calibration, regression or validation")
    if domain != "panel":
        raise VisualError("unsupported_domain", "SVG keeps the unchanged V2 recipes and window rule; use suite_web_v2.")
    if split == "calibration":
        result = [_mark_v3(s, suite_split="calibration", origin="v2-calibration")
                  for s in v2.scenarios("panel", "calibration")]
        width, height = NARROW_CALIBRATION_WIDTH
        for name, events, duration in _panel_recipes("calibration"):
            for repeat in (1, 2):
                result.append(_new_case(name, events, duration, "calibration", width, height, repeat))
        return result
    if split == "regression":
        return ([_mark_v3(s, suite_split="regression", origin="v2-known-regression")
                 for s in v2.scenarios("panel", "known_regression")]
                + [_mark_v3(s, suite_split="regression", origin="v2-validation-scored")
                   for s in v2.scenarios("panel", "validation")])
    result = []
    for name, events, duration in _panel_recipes("validation"):
        for width, height in NEW_VALIDATION_SIZES:
            for repeat in (1, 2):
                result.append(_new_case(name, events, duration, "validation", width, height, repeat))
    return result


def retest_key(case_id, row):
    return policy_v3.window_key(case_id, row["reference_id"], row.get("relative_to_reference"),
                                row["window_id"], row.get("segment_index", 0))


def _regate_windows(comparison, acceptance_policy, case_id):
    """Apply max(B, 0.05D, k*R) per window; every other gate is unchanged."""
    regated = deepcopy(comparison)
    rows = []
    for row in regated["object_windows"]:
        limit = policy_v3.window_rmse_limit(acceptance_policy, case_id, row)
        gates = dict(row["gates"])
        rmse = row.get("position_rmse")
        gates["position"] = rmse is not None and rmse <= limit + 1e-12
        row["gates"] = gates
        row["pass"] = all(gates.values())
        row["retest_limit"] = limit
        row["retest_floor_px"] = policy_v3.retest_px(acceptance_policy, case_id, row)
        rows.append(row)
    regated["object_windows"] = rows
    measurement_ok = bool(rows) and all(r["gates"]["coverage"] for r in rows) and not any(
        e.get("code") == "missing_object" for e in regated.get("errors", []))
    passed = (measurement_ok and not regated.get("errors") and all(r["pass"] for r in rows)
              and all(e["pass"] for e in regated.get("event_checks", []) + regated.get("preservation_checks", [])
                      + regated.get("lifecycle_checks", [])))
    regated.update(status="pass" if passed else "fail", numerical_status="pass" if passed else "fail",
                   measurement_status="pass" if measurement_ok else "fail", success=passed)
    return regated


# Failure classes that V2 assigns before it looks at any per-window pixel gate.
_PRIOR_FAILURES = {"input_history_mismatch", "required_order_unobservable", "comparison_measurement_failure",
                   "application_integrity_failure", "required_order_mismatch"}


def compare_records(reference, original, candidate, require_improvement=True, *, acceptance_policy):
    """V2 comparison under the frozen base policy; only the window position gate is re-evaluated."""
    policy_v3.validate_policy(acceptance_policy)
    for record in (reference, original, candidate):
        stamp = record.get("v3_acceptance_policy_sha256", acceptance_policy["policy_sha256"])
        if stamp != acceptance_policy["policy_sha256"]:
            raise VisualError("invalid_acceptance_policy", "Record belongs to a different frozen V3 acceptance policy.")
    case_id = reference["scenario"].get("case_id", reference["scenario"]["id"])
    base = acceptance_policy["base_policy"]
    result = v2.compare_records(reference, original, candidate, require_improvement, acceptance_policy=base)
    new = _regate_windows(result["pixel_comparison"], acceptance_policy, case_id)
    by_key = {(r["reference_id"], r["window_id"], r.get("segment_index", 0)): r for r in new["object_windows"]}
    gates = []
    for row in result["per_window_gates"]:
        window = by_key[(row["object_id"], row["window_id"], row["segment_index"])]
        absolute_pass = all(window["gates"].get(name) is True for name in ("coverage", "position", "size", "endpoint"))
        diagnostic = policy_v3.base_tools.gain_diagnostic(row["before_rmse"], row["after_rmse"],
                                                          absolute_pass=absolute_pass, limit=window["retest_limit"])
        gates.append({**row, **diagnostic, "absolute_pass": absolute_pass, "pass": absolute_pass,
                      "reason": diagnostic["classification"], "retest_limit": window["retest_limit"],
                      "retest_floor_px": window["retest_floor_px"]})
    geometry = result.get("svg_geometry_preservation")
    failure = result.get("failure_class")
    if failure in _PRIOR_FAILURES or (geometry is not None and not geometry.get("pass")):
        passed = False
    else:
        pixel_failure = any(not row["absolute_pass"] for row in gates)
        dynamics_failure = any(not row["pass"] for row in result["dynamics"] if row["observable"])
        passed = bool(gates and new.get("success") and not pixel_failure and not dynamics_failure)
        failure = ("per_window_pixel_failure" if pixel_failure
                   else "observable_dynamics_failure" if dynamics_failure else None)
    return {**result, "pass": passed, "pixel_comparison": new, "per_window_gates": gates, "improvement": gates,
            "failure_class": failure, "base_acceptance_profile_id": base["id"],
            "acceptance_profile_id": policy_v3.POLICY_ID, "acceptance_policy_sha256": acceptance_policy["policy_sha256"]}


def capture_record(project, output, scenario, check, progress, cancel, variant, reference_selection=None,
                   reference_record=None, *, acceptance_policy):
    policy_v3.validate_policy(acceptance_policy)
    record = v2.capture_record(project, output, scenario, check, progress, cancel, variant,
                               reference_selection=reference_selection, reference_record=reference_record,
                               acceptance_policy=acceptance_policy["base_policy"])
    # V2 stamps the embedded base policy, which its comparator re-checks; the V3
    # identity is carried separately so neither check can be satisfied by the other.
    record["v3_acceptance_policy_sha256"] = acceptance_policy["policy_sha256"]
    record["acceptance_profile_id"] = policy_v3.POLICY_ID
    return record
