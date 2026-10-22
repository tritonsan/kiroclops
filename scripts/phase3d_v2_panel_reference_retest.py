"""Post-hoc diagnostic: same-source reference test-retest under the frozen v2p08 policy.

Each panel recipe in the v2p08 suite was recorded twice (r1, r2) from the same,
unchanged reference source. This script compares those two recordings with the
production pixel comparator and the frozen realistic-behavior-v1 policy, in both
directions. It measures how often a candidate that behaves exactly like the
reference would still fail the per-window gates.

Nothing is changed: no acceptance decision, threshold, model run or capture.
Inputs are only read and are rehashed after the analysis. The result is a
labelled post-hoc diagnostic, not a replacement for any canonical result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.local_engine import realistic_policy as policy_tools  # noqa: E402
from kiroclops.local_engine import suite_web_v2  # noqa: E402
from kiroclops.visual.comparison import compare_observations  # noqa: E402

KIND = "PanelReferenceRetestDiagnostic"
CODE_FILES = (
    "kiroclops/visual/comparison.py",
    "kiroclops/local_engine/suite_web_v2.py",
    "kiroclops/local_engine/realistic_policy.py",
    "kiroclops/local_engine/evaluation.py",
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load(path, inventory):
    path = Path(path)
    inventory[str(path)] = _sha(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _record(suite_dir, case, inventory):
    raw = case["record"]
    return {
        "scenario": raw["scenario"],
        "observations": _load(suite_dir / raw["observations"], inventory),
        "selection": _load(suite_dir / raw["selection"], inventory),
        "measurement_status": raw["measurement_status"],
    }


def _px(value, scale):
    return value * scale if isinstance(value, (int, float)) and math.isfinite(value) else None


def _compare(reference, other, policy):
    contract = suite_web_v2.comparison_contract(reference, other, policy)
    result = compare_observations(reference["observations"], other["observations"], contract)
    dynamics = suite_web_v2.dynamic_checks(reference, other, contract)
    order = suite_web_v2.relative_timing_checks(reference, other, contract)
    history = policy_tools.input_history_check(reference["observations"], other["observations"])
    windows = []
    for row in result["object_windows"]:
        scale = row["scale_px"]
        rmse_px = _px(row["position_rmse"], scale)
        limit_px = _px(row["limits"]["rmse_limit"], scale)
        windows.append({
            "object_id": row["reference_id"],
            "relative_to": row.get("relative_to_reference"),
            "window_id": row["window_id"],
            "segment_index": row.get("segment_index", 0),
            "position_rmse_px": rmse_px,
            "position_limit_px": limit_px,
            "rmse_over_limit": rmse_px / limit_px if rmse_px is not None and limit_px else None,
            "endpoint_error_px": _px(row["endpoint_error"], scale),
            "endpoint_limit_px": _px(row["limits"]["endpoint_limit"], scale),
            "endpoint_required": row["endpoint_required"],
            "gates": row["gates"],
            "pass": row["pass"],
        })
    dynamic_failures = [row for row in dynamics if row.get("observable") and not row.get("pass")]
    order_pass = bool(order) and all(row.get("pass") is True for row in order)
    pixel_pass = bool(windows) and all(row["pass"] for row in windows)
    return {
        "comparator_status": result["status"],
        "pixel_windows_pass": pixel_pass,
        "dynamics_pass": not dynamic_failures,
        "relative_order_pass": order_pass,
        "input_history_pass": history["pass"],
        "input_history_max_difference_s": history.get("maximum_difference_s"),
        "input_history_max_adjacent_difference_s": history.get("maximum_adjacent_difference_s"),
        "input_history_tolerance_s": history.get("tolerance_s"),
        "would_pass_behavior_gates": bool(result["status"] == "pass" and pixel_pass and not dynamic_failures
                                          and order_pass and history["pass"]),
        "failed_windows": [w for w in windows if not w["pass"]],
        "failed_dynamics": dynamic_failures,
        "windows": windows,
        "event_checks_failed": [e for e in result.get("event_checks", []) if not e.get("pass")],
        "lifecycle_checks_failed": [e for e in result.get("lifecycle_checks", []) if not e.get("pass")],
        "comparator_errors": result.get("errors", []),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", default="C:/kc3d/p/v2p08/suite")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    suite_dir = Path(args.suite)
    output = Path(args.output)
    if output.exists():
        raise SystemExit(f"Output folder already exists; choose a new one: {output}")

    inventory = {}
    suite = _load(suite_dir / "suite.json", inventory)
    policy = policy_tools.validate_policy(suite["acceptance_policy"], domain="panel")
    cases = {case["case_id"]: case for case in suite["cases"]}

    pairs = []
    for case_id in sorted(cases):
        if not case_id.endswith("-r1"):
            continue
        twin = case_id[:-1] + "2"
        if twin not in cases:
            continue
        first, second = cases[case_id], cases[twin]
        if first["source_sha256"] != second["source_sha256"]:
            raise SystemExit(f"Repeats do not share one reference source: {case_id}")
        a = _record(suite_dir, first, inventory)
        b = _record(suite_dir, second, inventory)
        recipe = case_id[:-3]
        pairs.append({
            "recipe": recipe,
            "role": first["role"],
            "source_sha256": first["source_sha256"],
            "r1_as_reference": _compare(a, b, policy),
            "r2_as_reference": _compare(b, a, policy),
        })

    directions = [(p["recipe"], p["role"], key, p[key]) for p in pairs for key in ("r1_as_reference", "r2_as_reference")]
    all_windows = [w for _, _, _, d in directions for w in d["windows"]]
    failing = [
        {"recipe": recipe, "role": role, "direction": key, **w}
        for recipe, role, key, d in directions for w in d["failed_windows"]
    ]
    ratios = sorted(w["rmse_over_limit"] for w in all_windows if w["rmse_over_limit"] is not None)

    def quantile(q):
        if not ratios:
            return None
        index = q * (len(ratios) - 1)
        low, high = math.floor(index), math.ceil(index)
        return ratios[low] + (ratios[high] - ratios[low]) * (index - low)

    summary = {
        "recipes": len(pairs),
        "comparisons": len(directions),
        "comparisons_failing_any_behavior_gate": sum(not d["would_pass_behavior_gates"] for _, _, _, d in directions),
        "comparisons_failing_pixel_windows": sum(not d["pixel_windows_pass"] for _, _, _, d in directions),
        "comparisons_failing_dynamics": sum(not d["dynamics_pass"] for _, _, _, d in directions),
        "comparisons_failing_relative_order": sum(not d["relative_order_pass"] for _, _, _, d in directions),
        "comparisons_failing_input_history": sum(not d["input_history_pass"] for _, _, _, d in directions),
        "object_windows": len(all_windows),
        "object_windows_failing": sum(not w["pass"] for w in all_windows),
        "rmse_over_limit_median": quantile(.5),
        "rmse_over_limit_p95": quantile(.95),
        "rmse_over_limit_max": ratios[-1] if ratios else None,
        "by_role": {
            role: {
                "comparisons": sum(r == role for _, r, _, _ in directions),
                "failing_any_behavior_gate": sum(r == role and not d["would_pass_behavior_gates"]
                                                 for _, r, _, d in directions),
            }
            for role in sorted({p["role"] for p in pairs})
        },
    }

    for path, digest in inventory.items():
        if _sha(path) != digest:
            raise SystemExit(f"Input changed during the analysis: {path}")

    result = {
        "kind": KIND,
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "acceptance_changed": False,
        "label": "post-hoc diagnostic; canonical v2p08 results are unchanged",
        "question": "How often do two recordings of the same unchanged reference fail the frozen per-window gates?",
        "suite": str(suite_dir),
        "acceptance_policy_sha256": policy["policy_sha256"],
        "policy_floors_px": policy["floors_px"],
        "producer_sha256": _sha(Path(__file__)),
        "code_sha256": {name: _sha(ROOT / name) for name in CODE_FILES},
        "python": platform.python_version(),
        "inputs_sha256": inventory,
        "inputs_rehashed_after_analysis": True,
        "summary": summary,
        "failing_windows": failing,
        "pairs": pairs,
    }
    output.mkdir(parents=True)
    (output / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"summary": summary, "failing_windows": [
        {k: f[k] for k in ("recipe", "role", "direction", "object_id", "relative_to", "window_id",
                           "position_rmse_px", "position_limit_px", "endpoint_error_px", "endpoint_limit_px", "gates")}
        for f in failing]}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
