"""V3 realistic acceptance: the frozen V1 window gates plus a per-window retest floor.

Per window, the position RMSE limit is ``max(B, 0.05 * D, k * R)``. ``B`` and
``D`` come from the unchanged ``realistic-behavior-v1`` base policy (independent
calibration pixels and the reference's own travel). ``R`` is the RMSE between
the two recordings (r1, r2) of the same unchanged reference in that window,
measured before any model run with the production comparator. ``R`` only
matters where the reference itself is unstable relative to the V1 limit.

A window without a retest value receives no allowance: its limit stays the V1
limit. Every other gate (size, endpoint, coverage, events, order, inputs,
function) is unchanged.
"""
from __future__ import annotations

from copy import deepcopy
import math

from kiroclops.visual.schema import VisualError, digest

from . import realistic_policy as base_tools

POLICY_ID = "realistic-behavior-v3"
RULES = {
    "retest_factor": 2.,
    "retest_scope": "position_rmse_px",
    "retest_pairing": "reference_r1_r2_max_of_both_directions",
    "missing_retest": "no_allowance_v1_limit",
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def window_key(case_id, object_id, relative_to, window_id, segment_index):
    return "|".join([str(case_id), str(object_id), str(relative_to or "-"), str(window_id), str(int(segment_index))])


def _recipe(case_id):
    if isinstance(case_id, str) and case_id.endswith(("-r1", "-r2")):
        return case_id[:-3]
    raise VisualError("invalid_acceptance_policy", "Retest floors need r1/r2 recordings of one recipe: " + str(case_id))


def retest_table(references, base_policy):
    """Measure R for every reference window from the suite's own r1/r2 recordings.

    ``references`` maps case_id to a record with scenario, observations and
    selection. Both directions are compared; the larger value is kept.
    """
    from kiroclops.visual.comparison import compare_observations
    from .suite_web_v2 import comparison_contract
    base_tools.validate_policy(base_policy)
    groups = {}
    for case_id in references:
        groups.setdefault(_recipe(case_id), set()).add(case_id)
    table, pairs = {}, []
    for recipe, ids in sorted(groups.items()):
        if ids != {recipe + "-r1", recipe + "-r2"}:
            raise VisualError("invalid_acceptance_policy", "Every recipe needs exactly one r1 and one r2 reference: " + recipe)
        values = {}
        for first, second in ((recipe + "-r1", recipe + "-r2"), (recipe + "-r2", recipe + "-r1")):
            a, b = references[first], references[second]
            contract = comparison_contract(a, b, base_policy)
            result = compare_observations(a["observations"], b["observations"], contract)
            for row in result["object_windows"]:
                rmse = row.get("position_rmse")
                if not _number(rmse) or not _number(row.get("scale_px")):
                    continue
                key = (row["reference_id"], row.get("relative_to_reference"), row["window_id"], row.get("segment_index", 0))
                values.setdefault(key, []).append(rmse * row["scale_px"])
        for key, measured in sorted(values.items(), key=lambda item: str(item[0])):
            floor = max(measured)
            for suffix in ("-r1", "-r2"):
                table[window_key(recipe + suffix, *key)] = floor
        pairs.append({"recipe": recipe, "windows": len(values),
                      "both_directions": sum(len(v) == 2 for v in values.values())})
    return table, pairs


def build_policy(domain, calibration_review, references):
    base = base_tools.build_policy(domain, calibration_review)
    if base["domain"] != "panel":
        raise VisualError("invalid_acceptance_policy", "The V3 retest floor is a panel rule; SVG keeps the V1 window rule.")
    table, pairs = retest_table(references, base)
    policy = {"kind": "AcceptancePolicy", "schema_version": 1, "id": POLICY_ID, "domain": base["domain"],
              "rules": deepcopy(RULES), "base_policy": base, "retest_floors_px": table, "retest_pairs": pairs,
              "frozen_before_model": True}
    policy["policy_sha256"] = digest(policy)
    validate_policy(policy, domain=domain)
    return policy


def validate_policy(policy, *, domain=None):
    if (not isinstance(policy, dict) or policy.get("kind") != "AcceptancePolicy" or policy.get("schema_version") != 1
            or policy.get("id") != POLICY_ID or policy.get("rules") != RULES
            or policy.get("frozen_before_model") is not True or not isinstance(policy.get("retest_floors_px"), dict)
            or not isinstance(policy.get("retest_pairs"), list)):
        raise VisualError("invalid_acceptance_policy", "Expected an unchanged frozen realistic-behavior-v3 policy.")
    base_tools.validate_policy(policy.get("base_policy"), domain=domain)
    if policy.get("domain") != policy["base_policy"]["domain"] or policy["domain"] != "panel":
        raise VisualError("invalid_acceptance_policy", "A V3 policy is a panel policy whose base shares its domain.")
    for key, value in policy["retest_floors_px"].items():
        if not isinstance(key, str) or not _number(value) or value < 0:
            raise VisualError("invalid_acceptance_policy", "Retest floors must be finite non-negative pixel values.")
    payload = {key: value for key, value in policy.items() if key != "policy_sha256"}
    if digest(payload) != policy.get("policy_sha256"):
        raise VisualError("invalid_acceptance_policy", "Frozen V3 policy digest does not match its contents.")
    return policy


def is_v3(policy):
    return isinstance(policy, dict) and policy.get("id") == POLICY_ID


def retest_px(policy, case_id, row):
    return policy["retest_floors_px"].get(window_key(case_id, row["reference_id"], row.get("relative_to_reference"),
                                                     row["window_id"], row.get("segment_index", 0)))


def window_rmse_limit(policy, case_id, row):
    """Normalized RMSE limit for one comparison window: max(V1 limit, k * R / scale)."""
    base = row["limits"]["rmse_limit"]
    floor = retest_px(policy, case_id, row)
    scale = row.get("scale_px")
    if floor is None or not _number(scale) or scale <= 0:
        return base
    return max(base, policy["rules"]["retest_factor"] * floor / scale)
