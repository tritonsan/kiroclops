"""V5 panel reference suite recipes (PHASE3D_V5_PLAN.md).

- Calibration: the V4 calibration, plus an early interrupt (close 0.22 s after
  the open) at 768x640 and 1280x720, and a reversal at 1280x720. V4's failing
  regression interrupts came earlier than any calibration interrupt.
- Regression: every recipe V4 scored, except V3's ``interrupt-v3``. Its close
  comes 0.14 s after the open, so the reference itself cannot show the required
  order (``required_order_unobservable`` in V3 and V4).
- Validation: unseen widths (896 and 1360) and unseen timings. Every interrupt
  comes at least 0.22 s after the open, so the required order stays observable.

Acceptance is the frozen realistic-behavior-v1 window rule with the V1 window
limit; V5 has no retest allowance. Capture and comparison are the V2 functions.
The scenario ``suite_version`` stays ``reference-suite-web-v2`` because the
natural recorder enables its panel replay-boundary fix only for that value.
"""
from __future__ import annotations

from copy import deepcopy

from kiroclops.visual.schema import VisualError
from kiroclops.web_local.specs import canonical_domain, validate_scenario

from . import realistic_policy
from . import suite_web as legacy
from . import suite_web_v2 as v2
from . import suite_web_v4 as v4

V5_VERSION = "reference-suite-web-v5"
UNOBSERVABLE_RECIPES = ("interrupt-v3",)
NEW_VALIDATION_SIZES = ((896, 600), (1360, 768))


def _calibration_recipes():
    first, second = "#open-panel", "#close-panel"
    return [("interrupt-early", [legacy._click(.4, first), legacy._click(.62, second)], 3., ((768, 640), (1280, 720))),
            ("reverse-wide", [legacy._click(.4, first), legacy._click(.66, second), legacy._click(.92, first)], 3.8,
             ((1280, 720),))]


def _validation_recipes():
    first, second = "#open-panel", "#close-panel"
    return [("interrupt-v5", [legacy._click(.4, first), legacy._click(.66, second)], 3.),
            ("reverse-v5", [legacy._click(.4, first), legacy._click(.62, second), legacy._click(.90, first)], 3.8),
            ("drag-release-v5", [legacy._click(.4, first),
                                 *legacy._drag("panel", .95, duration=.30, release=1.35, travel=.45)], 3.8)]


def _new_case(name, events, duration, split, width, height, repeat):
    case_id = f"realistic-v5-suite-panel-{split}-{name}-w{width}-r{repeat}"
    value = {
        "id": case_id, "case_id": case_id, "recipe": name, "domain": "panel",
        "split": split, "suite_split": split, "repeat": repeat,
        "viewport": [width, height], "device_scale_factor": 1,
        "duration_s": duration, "events": deepcopy(events),
        "timing_limits": dict(legacy.TIMING_LIMITS), "version": V5_VERSION,
        "suite_version": v2.VERSION, "v5_suite_version": V5_VERSION,
        "blind_validation": split == "validation", "frame_driver": "natural_browser_raf_v1",
        "acceptance_profile_id": realistic_policy.POLICY_ID,
    }
    validate_scenario(value)
    return value


def _mark_v5(scenario, *, suite_split, origin):
    scenario["v5_suite_version"] = V5_VERSION
    scenario["acceptance_profile_id"] = realistic_policy.POLICY_ID
    scenario["suite_split"] = suite_split
    scenario["v5_origin"] = origin
    if suite_split == "regression":
        scenario["blind_validation"] = False
    validate_scenario(scenario)
    return scenario


def scenarios(domain, split):
    domain = canonical_domain(domain)
    split = {"known": "regression", "known_regression": "regression"}.get(split, split)
    if domain != "panel":
        raise VisualError("unsupported_domain", "V5 changes the panel recipes only.")
    if split not in {"calibration", "regression", "validation"}:
        raise VisualError("invalid_suite_split", "Expected calibration, regression or validation")
    if split == "calibration":
        result = [_mark_v5(s, suite_split="calibration", origin="v4-calibration")
                  for s in v4.scenarios("panel", "calibration")]
        for name, events, duration, sizes in _calibration_recipes():
            for width, height in sizes:
                for repeat in (1, 2):
                    result.append(_new_case(name, events, duration, "calibration", width, height, repeat))
        return result
    if split == "regression":
        return ([_mark_v5(s, suite_split="regression", origin="v4-regression")
                 for s in v4.scenarios("panel", "regression") if s.get("recipe") not in UNOBSERVABLE_RECIPES]
                + [_mark_v5(s, suite_split="regression", origin="v4-validation-scored")
                   for s in v4.scenarios("panel", "validation")])
    result = []
    for name, events, duration in _validation_recipes():
        for width, height in NEW_VALIDATION_SIZES:
            for repeat in (1, 2):
                result.append(_new_case(name, events, duration, "validation", width, height, repeat))
    return result
