"""V4 panel reference suite recipes (PHASE3D_V4_PLAN.md).

- Calibration: the V3 calibration (V2's four w960 recipes plus the w768
  interrupt and reversal), and an open and an interrupt at 1280x720, so the
  wide layout is part of what Kiro sees.
- Regression: every recipe V3 scored, that is V3 regression and V3 new
  validation.
- Validation: unseen widths (1024 and 1440) and unseen timings. The interrupt
  comes 0.23 s after the open so the required order stays observable.

Acceptance is the frozen realistic-behavior-v1 window rule; V4 has no retest
allowance. Capture and comparison are the V2 functions. The scenario
``suite_version`` stays ``reference-suite-web-v2`` because the natural recorder
enables its panel replay-boundary fix only for that value.
"""
from __future__ import annotations

from copy import deepcopy

from kiroclops.visual.schema import VisualError
from kiroclops.web_local.specs import canonical_domain, validate_scenario

from . import realistic_policy
from . import suite_web as legacy
from . import suite_web_v2 as v2
from . import suite_web_v3 as v3

V4_VERSION = "reference-suite-web-v4"
WIDE_CALIBRATION_SIZE = (1280, 720)
NEW_VALIDATION_SIZES = ((1024, 640), (1440, 800))


def _recipes(split):
    first, second = "#open-panel", "#close-panel"
    if split == "calibration":
        return [("open-wide", [legacy._click(.4, first)], 3.),
                ("interrupt-wide", [legacy._click(.4, first), legacy._click(.70, second)], 3.)]
    return [("interrupt-v4", [legacy._click(.4, first), legacy._click(.63, second)], 3.),
            ("reverse-v4", [legacy._click(.4, first), legacy._click(.63, second), legacy._click(.86, first)], 3.8),
            ("drag-release-v4", [legacy._click(.4, first),
                                 *legacy._drag("panel", .98, duration=.27, release=1.31, travel=.50)], 3.8)]


def _new_case(name, events, duration, split, width, height, repeat):
    case_id = f"realistic-v4-suite-panel-{split}-{name}-w{width}-r{repeat}"
    value = {
        "id": case_id, "case_id": case_id, "recipe": name, "domain": "panel",
        "split": split, "suite_split": split, "repeat": repeat,
        "viewport": [width, height], "device_scale_factor": 1,
        "duration_s": duration, "events": deepcopy(events),
        "timing_limits": dict(legacy.TIMING_LIMITS), "version": V4_VERSION,
        "suite_version": v2.VERSION, "v4_suite_version": V4_VERSION,
        "blind_validation": split == "validation", "frame_driver": "natural_browser_raf_v1",
        "acceptance_profile_id": realistic_policy.POLICY_ID,
    }
    validate_scenario(value)
    return value


def _mark_v4(scenario, *, suite_split, origin):
    scenario["v4_suite_version"] = V4_VERSION
    scenario["acceptance_profile_id"] = realistic_policy.POLICY_ID
    scenario["suite_split"] = suite_split
    scenario["v4_origin"] = origin
    if suite_split == "regression":
        scenario["blind_validation"] = False
    validate_scenario(scenario)
    return scenario


def scenarios(domain, split):
    domain = canonical_domain(domain)
    split = {"known": "regression", "known_regression": "regression"}.get(split, split)
    if domain != "panel":
        raise VisualError("unsupported_domain", "V4 changes the panel recipes only.")
    if split not in {"calibration", "regression", "validation"}:
        raise VisualError("invalid_suite_split", "Expected calibration, regression or validation")
    if split == "calibration":
        result = [_mark_v4(s, suite_split="calibration", origin="v3-calibration")
                  for s in v3.scenarios("panel", "calibration")]
        width, height = WIDE_CALIBRATION_SIZE
        for name, events, duration in _recipes("calibration"):
            for repeat in (1, 2):
                result.append(_new_case(name, events, duration, "calibration", width, height, repeat))
        return result
    if split == "regression":
        return ([_mark_v4(s, suite_split="regression", origin="v3-regression")
                 for s in v3.scenarios("panel", "regression")]
                + [_mark_v4(s, suite_split="regression", origin="v3-validation-scored")
                   for s in v3.scenarios("panel", "validation")])
    result = []
    for name, events, duration in _recipes("validation"):
        for width, height in NEW_VALIDATION_SIZES:
            for repeat in (1, 2):
                result.append(_new_case(name, events, duration, "validation", width, height, repeat))
    return result
