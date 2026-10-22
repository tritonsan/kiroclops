"""Prospective browser acceptance with independent pixel floors and real inputs.

The legacy suite remains unchanged. Gain is diagnostic; every observed response
must satisfy its absolute quality, geometry and application integrity gates.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import statistics

from kiroclops.visual.schema import VisualError, digest, write_json
from kiroclops.visual.comparison import compare_observations, relative_object, window_timing
from kiroclops.web_local.specs import canonical_domain, validate_scenario
from . import realistic_policy as policy_tools
from . import suite_web as legacy

VERSION = "reference-suite-web-v2"


def scenarios(domain, split):
    domain = canonical_domain(domain)
    split = {"known": "known_regression", "regression": "known_regression"}.get(split, split)
    result = legacy.scenarios(domain, split)
    for scenario in result:
        scenario["legacy_case_id"] = scenario["case_id"]
        scenario["id"] = scenario["case_id"] = "realistic-" + scenario["case_id"]
        scenario["suite_version"] = VERSION
        scenario["version"] = VERSION
        scenario["acceptance_profile_id"] = policy_tools.POLICY_ID
        if split == "validation":
            events = scenario["events"]
            if domain == "panel":
                if scenario["recipe"] == "interrupt-new":
                    events[1]["time_s"] = .61
                elif scenario["recipe"] == "reverse-new":
                    events[1]["time_s"], events[2]["time_s"] = .61, .88
                else:
                    scenario["events"] = [deepcopy(events[0]), *legacy._drag(domain, 1.10, duration=.28, release=1.43, travel=.35)]
            elif scenario["recipe"] == "three-to-five-new":
                events[-1]["time_s"] = .69
            elif scenario["recipe"] == "six-to-four-new":
                events[-1]["time_s"] = .71
            else:
                scenario["events"] = [deepcopy(events[0]), *legacy._drag(domain, .68, duration=.32, release=1.05)]
        validate_scenario(scenario)
    return result


def event_contract(domain):
    """Keep capture/event observability gates and the existing shared definition."""
    return legacy.event_contract(domain)


def _distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _reference_distance(observations, pair):
    objects = {obj["id"]: obj for obj in observations["objects"]}
    obj = objects[pair["reference_id"]]
    if pair.get("relative_to_reference"):
        obj = relative_object(obj, objects[pair["relative_to_reference"]])
    frames = [frame for frame in obj["frames"] if frame.get("valid") is True]
    if len(frames) < 2:
        raise VisualError("tracking_failure", "Reference object lacks two valid observations.")
    return math.hypot(max(f["x"] for f in frames) - min(f["x"] for f in frames),
                      max(f["y"] for f in frames) - min(f["y"] for f in frames))


def comparison_contract(reference, candidate, acceptance_policy, *, baseline_only=False):
    from . import evaluation
    domain = canonical_domain(reference["scenario"]["domain"])
    policy_tools.validate_policy(acceptance_policy, domain=domain)
    contract = evaluation._contract(reference, candidate)
    contract["acceptance_profile_id"] = policy_tools.POLICY_ID
    contract["acceptance_policy_sha256"] = acceptance_policy["policy_sha256"]
    contract["event_options"] = deepcopy(event_contract(domain)["event_options"])
    for pair in contract["object_pairs"]:
        distance = _reference_distance(reference["observations"], pair)
        limits = policy_tools.pixel_limits(acceptance_policy, pair["reference_id"], distance, pair["scale_px"])
        pair.update(limits)
        pair["static"] = distance <= limits["pixel_floor_px"]
    if baseline_only:
        # Original baseline supplies measured pixel error, not an event oracle.
        # Original EventSets never enter acceptance, recommendations or fitting.
        contract.update(required_event_types=[], require_connections=False)
        contract.pop("forbid_boundary_cross", None)
    return contract


def _settle_time(frames, endpoint, band, origin, rest_duration, max_gap):
    if len(frames) < 2:
        return None
    last_bad = -1
    for index, frame in enumerate(frames):
        if _distance((frame["x"], frame["y"]), endpoint) > band:
            last_bad = index
    first = last_bad + 1
    if first >= len(frames) or frames[-1]["time_s"] - frames[first]["time_s"] < rest_duration - 1e-9:
        return None
    if any(b["time_s"] - a["time_s"] > max_gap + 1e-9 for a, b in zip(frames[first:], frames[first + 1:])):
        return None
    return frames[first]["time_s"] - origin


def dynamic_checks(reference, candidate, contract):
    from . import evaluation
    refs, targets = reference["observations"], candidate["observations"]
    lookup = {obj["id"]: obj for obj in refs["objects"]}
    target_lookup = {obj["id"]: obj for obj in targets["objects"]}
    rest_duration = contract["event_options"]["rest_duration_s"]
    max_gap = contract.get("max_interpolation_gap_s", .12)
    checks = []
    for pair in contract["object_pairs"]:
        if pair["static"]:
            continue
        obj = lookup[pair["reference_id"]]
        target = target_lookup.get(pair["candidate_id"])
        if pair.get("relative_to_reference"):
            obj = relative_object(obj, lookup[pair["relative_to_reference"]])
            target = relative_object(target, target_lookup[pair["relative_to_candidate"]]) if target else None
        for window in contract["windows"]:
            if window.get("object_ids") is not None and obj["id"] not in window["object_ids"]:
                continue
            timing = window_timing(refs, targets, contract, window)
            ro, co = timing["reference_origin_s"], timing["candidate_origin_s"]
            start, end = [value + ro for value in timing["time_window_s"]]
            for interval in obj.get("active_intervals_s", [[start, end]]):
                left, right = max(start, interval[0]), min(end, interval[1])
                if right <= left:
                    continue
                row = {"object_id": obj["id"], "window_id": window["id"],
                       "reference_interval_s": [left, right], "observable": False, "pass": False,
                       "settle_band_px": pair["settle_band_px"],
                       "overshoot_limit_px": pair["overshoot_limit_px"], "input_timing": timing,
                       "rest_duration_s": rest_duration}
                if not timing["endpoint_required"]:
                    row["reason"] = "reference_interrupted_or_unsettled"
                    checks.append(row)
                    continue
                frames = evaluation._segment_samples(obj, left, right)
                if len(frames) < 2 or frames[-1]["time_s"] - frames[0]["time_s"] < rest_duration:
                    row["reason"] = "reference_interval_too_short"
                    checks.append(row)
                    continue
                tail = [f for f in frames if f["time_s"] >= frames[-1]["time_s"] - rest_duration - 1e-9]
                endpoint = (statistics.median(f["x"] for f in tail), statistics.median(f["y"] for f in tail))
                start_point = (frames[0]["x"], frames[0]["y"])
                if _distance(start_point, endpoint) <= row["settle_band_px"]:
                    row["reason"] = "no_directional_displacement_in_segment"
                    checks.append(row)
                    continue
                ref_settle = _settle_time(frames, endpoint, row["settle_band_px"], left, rest_duration, max_gap)
                if ref_settle is None:
                    row["reason"] = "reference_interrupted_or_unsettled"
                    checks.append(row)
                    continue
                row["observable"] = True
                candidate_start, candidate_end = co + left - ro, co + right - ro
                observed = evaluation._segment_samples(target, candidate_start, candidate_end) if target else []
                candidate_settle = _settle_time(observed, endpoint, row["settle_band_px"], candidate_start, rest_duration, max_gap)
                ref_overshoot = evaluation._overshoot(frames, start_point, endpoint)
                candidate_overshoot = evaluation._overshoot(observed, start_point, endpoint) if observed else None
                time_limit = max(.15, .15 * ref_settle)
                gates = {"settled_event_observed": candidate_settle is not None,
                         "settling_time": candidate_settle is not None and abs(candidate_settle - ref_settle) <= time_limit + 1e-9,
                         "overshoot": candidate_overshoot is not None and abs(candidate_overshoot - ref_overshoot) <= row["overshoot_limit_px"] + 1e-9}
                row.update(reference_settling_s=ref_settle, candidate_settling_s=candidate_settle,
                           reference_overshoot_px=ref_overshoot, candidate_overshoot_px=candidate_overshoot,
                           settling_time_limit_s=time_limit, endpoint_px=list(endpoint), gates=gates,
                           **{"pass": all(gates.values())})
                checks.append(row)
    return checks


def relative_timing_checks(reference, candidate, contract):
    """Keep observed order/count/uncertainty; allow 150ms lag difference."""
    from . import evaluation
    rows = evaluation._relative_timing_checks(reference, candidate, contract)
    for row in rows:
        row["lag_tolerance_s"] = .15
        if "gates" in row:
            row["gates"]["lag"] = row["lag_interval_distance_s"] <= .15 + 1e-9
            row["pass"] = all(row["gates"].values())
    return rows


def _binding(record):
    """A canonical recipe and its execution recipe must have separate digests."""
    if "input_replay" not in record:
        return
    proof = record["input_replay"]
    execution = record.get("execution_scenario")
    if (not isinstance(proof, dict) or not isinstance(execution, dict)
            or proof.get("kind") != "ActualInputReplay" or proof.get("schema_version") != 1
            or proof.get("canonical_scenario_sha256") != digest(record["scenario"])
            or proof.get("execution_scenario_sha256") != digest(execution)
            or proof.get("duration_rescaled") is not False):
        raise VisualError("suite_recipe_mismatch", "Canonical recipe and actual replay binding changed.")
    validate_scenario(execution)
    changed = deepcopy(execution)
    changed["events"] = deepcopy(record["scenario"]["events"])
    if changed != record["scenario"]:
        raise VisualError("suite_recipe_mismatch", "Replay may change only scheduled times and reference-bound pointer points.")


def compare_records(reference, original, candidate, require_improvement=True, *, acceptance_policy):
    """Absolute quality is mandatory; percent gain never masks a bad window."""
    from .geometry_acceptance import svg_preservation
    domain = canonical_domain(reference["scenario"]["domain"])
    policy_tools.validate_policy(acceptance_policy, domain=domain)
    for record in (reference, original, candidate):
        if digest(record["scenario"]) != digest(reference["scenario"]):
            raise VisualError("suite_recipe_mismatch", "All roles must retain one canonical recipe.")
        _binding(record)
        if record.get("input_replay") is not None and record["input_replay"].get("reference_inputs_sha256") != digest(reference["observations"]["inputs"]):
            raise VisualError("suite_recipe_mismatch", "Replay is not bound to this exact actual reference input history.")
        if record.get("input_replay") is not None:
            expected_execution, expected_proof = policy_tools.replay_scenario(record["scenario"], reference["observations"])
            if record["execution_scenario"] != expected_execution or record["input_replay"] != expected_proof:
                raise VisualError("suite_recipe_mismatch", "Replay schedule differs from the complete actual reference input history.")
        policy = record.get("suite_event_contract")
        if policy is not None and policy != event_contract(domain):
            raise VisualError("suite_policy_mismatch", "Production and independent review event definitions differ.")
        if record.get("acceptance_policy_sha256", acceptance_policy["policy_sha256"]) != acceptance_policy["policy_sha256"]:
            raise VisualError("invalid_acceptance_policy", "Record belongs to a different frozen acceptance policy.")
    histories = {"original": policy_tools.input_history_check(reference["observations"], original["observations"]),
                 "candidate": policy_tools.input_history_check(reference["observations"], candidate["observations"])}
    histories_pass = all(value["pass"] for value in histories.values())
    contract = comparison_contract(reference, candidate, acceptance_policy)
    old_contract = comparison_contract(reference, original, acceptance_policy, baseline_only=True)
    old = compare_observations(reference["observations"], original["observations"], old_contract)
    new = compare_observations(reference["observations"], candidate["observations"], contract)
    key = lambda row: (row["reference_id"], row["window_id"], row.get("segment_index", 0))
    prior = {key(row): row for row in old["object_windows"]}
    rows = []
    for row in new["object_windows"]:
        previous = prior.get(key(row), {})
        before, after = previous.get("position_rmse"), row.get("position_rmse")
        absolute_pass = all(row["gates"].get(name) is True for name in ("coverage", "position", "size", "endpoint"))
        diagnostic = policy_tools.gain_diagnostic(before, after, absolute_pass=absolute_pass, limit=row["limits"]["rmse_limit"])
        rows.append({"object_id": row["reference_id"], "window_id": row["window_id"],
                     "segment_index": row.get("segment_index", 0), "before_rmse": before, "after_rmse": after,
                     **diagnostic, "required_fraction": None, "absolute_pass": absolute_pass, "pass": absolute_pass,
                     "reason": diagnostic["classification"]})
    dynamics = dynamic_checks(reference, candidate, contract)
    order = relative_timing_checks(reference, candidate, contract)
    order_required = domain == "panel"
    unresolved = order_required and (not order or any((r.get("reference") or {}).get("timing_status") != "resolved"
                   or (r.get("candidate") or {}).get("timing_status") != "resolved" for r in order))
    order_pass = not order_required or bool(order) and all(row.get("pass") is True for row in order)
    geometry = svg_preservation(reference["observations"], candidate["observations"],
                reference.get("selection", {}).get("connections", [])) if domain == "svg" else None
    original_measured = original.get("measurement_status") == "ok" and old.get("measurement_status") == "pass"
    measured = (all(record.get("measurement_status") == "ok" for record in (reference, candidate))
                and new.get("measurement_status") == "pass" and original_measured and histories_pass and not unresolved
                and (geometry is None or geometry.get("measurement_status") == "ok"))
    functional = candidate.get("functional", {}).get("status") == "pass"
    passed = bool(rows and new.get("success") and measured and functional and order_pass
                  and all(row["pass"] for row in rows) and all(row["pass"] for row in dynamics if row["observable"])
                  and (geometry is None or geometry.get("pass")))
    failure = None
    if not histories_pass:
        failure = "input_history_mismatch"
    elif unresolved:
        failure = "required_order_unobservable"
    elif not measured:
        failure = "comparison_measurement_failure"
    elif not functional:
        failure = "application_integrity_failure"
    elif not order_pass:
        failure = "required_order_mismatch"
    elif geometry is not None and not geometry.get("pass"):
        failure = geometry.get("failure_class") or "geometry_preservation_failure"
    elif any(not row["absolute_pass"] for row in rows):
        failure = "per_window_pixel_failure"
    elif any(not row["pass"] for row in dynamics if row["observable"]):
        failure = "observable_dynamics_failure"
    values = [row["after_rmse"] for row in rows]
    finite = lambda value: type(value) in (int, float) and math.isfinite(value)
    return {"scenario_id": reference["scenario"]["id"], "case_id": reference["scenario"].get("case_id", reference["scenario"]["id"]),
            "pass": passed, "score": statistics.mean(values) if values and all(finite(v) for v in values) else None,
            "pixel_comparison": new, "original_pixel_comparison": old, "per_window_gates": rows, "improvement": rows,
            "contract": contract, "original_contract": old_contract,
            "dynamics": dynamics, "relative_timing": order, "required_order_status": "insufficient_observability" if unresolved else "pass" if order_pass else "fail",
            "functional": candidate.get("functional"), "svg_geometry_preservation": geometry,
            "measurement_pass": bool(measured), "baseline_measurement_pass": original_measured,
            "input_history": histories, "failure_class": failure,
            "acceptance_profile_id": policy_tools.POLICY_ID, "acceptance_policy_sha256": acceptance_policy["policy_sha256"],
            "suite_event_contract": event_contract(domain), "independent_review_contract": event_contract(domain),
            "improvement_requested": bool(require_improvement), "improvement_required": False,
            "decision_event_roles": ["reference", "candidate"], "original_event_role": "diagnostic_only_not_consumed",
            "acceptance_scope": "Every required absolute object/window gate plus measured inputs, events, geometry and application integrity; gain is diagnostic."}


def capture_record(project, output, scenario, check, progress, cancel, variant, reference_selection=None,
                   *, reference_record=None, acceptance_policy=None):
    """Capture at most twice, preserving a mismatched first real capture."""
    domain = canonical_domain(scenario["domain"])
    if acceptance_policy is not None:
        policy_tools.validate_policy(acceptance_policy, domain=domain)
    execution, proof = deepcopy(scenario), None
    if reference_record is not None:
        if reference_record["scenario"] != scenario:
            raise VisualError("suite_recipe_mismatch", "Replay reference belongs to another canonical recipe.")
        execution, proof = policy_tools.replay_scenario(scenario, reference_record["observations"])
    root = Path(output)
    if root.exists():
        raise VisualError("output_exists", "Keep prior captures unchanged.")
    attempts = []
    maximum = 2 if reference_record is not None else 1
    for index in range(maximum):
        check()
        if cancel and cancel():
            raise VisualError("job_cancelled", "Capture cancelled before actual replay.")
        destination = root / f"attempt-{index + 1:02d}"
        record = legacy.capture_record(project, destination, execution, check, progress, cancel, variant,
                                       reference_selection=reference_selection)
        record["execution_scenario"] = deepcopy(execution)
        record["scenario"] = deepcopy(scenario)
        if proof is not None:
            record["input_replay"] = deepcopy(proof)
        if acceptance_policy is not None:
            record["acceptance_policy_sha256"] = acceptance_policy["policy_sha256"]
        history = policy_tools.input_history_check(reference_record["observations"], record["observations"]) if reference_record else None
        receipt = {"attempt": index + 1, "directory": str(destination), "record": record,
                   "input_history": history, "status": "pass" if history is None or history["pass"] else "input_history_mismatch"}
        write_json(destination / "capture-attempt.json", receipt)
        attempts.append({"attempt": index + 1, "directory": str(destination),
                         "receipt_sha256": digest(receipt), "status": receipt["status"], "input_history": history})
        if history is None or history["pass"]:
            record["capture_attempts"] = deepcopy(attempts)
            record["input_history"] = history
            write_json(root / "capture-replay.json", {"status": "pass", "attempts": attempts,
                       "selected_attempt": index + 1, "duration_rescaled": False})
            return record
    write_json(root / "capture-replay.json", {"status": "input_history_mismatch", "attempts": attempts,
               "selected_attempt": None, "duration_rescaled": False})
    raise VisualError("input_history_mismatch", "Two real captures did not match the fixed actual reference input intervals.")
