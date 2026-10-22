"""Calibration-only motion diagnostics; never edits code or acceptance gates.

Use inside a newly registered worker, after its measurement-policy context.
Rendered trajectories and the reference-only fitted profile are the complete
inputs. No controller source, state logs, telemetry, or held-out observations
are read. Delays are diagnostic hypotheses, never acceptance realignment.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import threading

import numpy as np

from kiroclops.ground import comparison, fitting
from kiroclops.ground.analysis import FPS, FRAME_COUNT
from . import ground_worker
from .storage import IntegrationError, read_json, sha256

POLICY = {
    "id": "calibration-motion-feedback-v3",
    "input_split": "calibration",
    "complete_slots": [[sid, 0, repeat] for sid in ("r06", "r48") for repeat in (1, 2, 3)],
    "weighting": "equal phase MSE within each run; equal run MSE; no idle tail",
    "uniform_output_lag_ticks": list(range(-4, 5)),
    "press_delay_release_fixed_ticks": list(range(5)),
    "target_rounding_margin_body": "1/target_body_height_px: two +/-0.5px rounding errors (frame and origin)",
    "advisory_rule": "target_profile_rmse > reference_profile_rmse + target_rounding_margin_body",
    "lag_uncertainty": "per-sample +/-rounding margin; retain hypotheses whose RMSE lower bound overlaps the minimum upper bound",
    "acceptance_modified": False,
}
_LOCK = threading.Lock()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def _reports(values, variant):
    if not isinstance(values, (list, tuple)) or len(values) != 6:
        raise ValueError("Diagnostics require the complete six-run calibration matrix")
    result = {}
    for report in values:
        if not isinstance(report, dict) or report.get("scenario", {}).get("split") != "calibration":
            raise ValueError("Diagnostics cannot consume held-out observations")
        scenario = report["scenario"]
        key = comparison._key(report)
        if list(key) not in POLICY["complete_slots"] or key in result or report.get("variant") not in variant:
            raise ValueError("Wrong, duplicate or unexpected calibration slot/role")
        phases = comparison._usable(report)
        frames, height = report["frames"], report["body_height_px"]
        if not all(frames[i]["valid"] for i in range(20, 30)):
            raise ValueError("Pre-input origin must be completely visible")
        if any(f["valid"] and not _finite(f.get("x")) for f in frames):
            raise ValueError("Native pixel positions are required")
        origin = float(np.median([frames[i]["x"] for i in range(20, 30)]))
        if np.ptp([frames[i]["x"] for i in range(20, 30)]) > max(1., .05 * height):
            raise ValueError("Pre-input origin is not stationary")
        if any(f["valid"] and abs(f["x_body"] - (f["x"] - origin) / height) > 1e-8 for f in frames):
            raise ValueError("The input-aligned native origin differs from measured displacement")
        result[key] = {"report": report, "phases": phases, "height": height, "origin": origin,
                       "x": np.array([f["x_body"] if f["valid"] else np.nan for f in frames]),
                       "valid": np.array([f["valid"] for f in frames], dtype=bool)}
    if (set(result) != {tuple(slot) for slot in POLICY["complete_slots"]}
            or len({item["height"] for item in result.values()}) != 1
            or len({item["report"].get("game") for item in result.values()}) != 1
            or len({item["report"]["variant"] for item in result.values()}) != 1):
        raise ValueError("Calibration roles and scales must be consistent and complete")
    return result


def _statistics(observed, predicted, phases, valid, margin):
    rows = []
    for phase in phases:
        indices = np.arange(phase["start"], phase["end"])
        indices = indices[valid[indices]]
        if len(indices) / (phase["end"] - phase["start"]) < .95:
            raise ValueError("Common observations cover less than 95% of an input phase")
        residual = observed[indices] - predicted[indices]
        rows.append({"phase": phase["name"], "start_inclusive": phase["start"], "end_exclusive": phase["end"],
            "valid_samples": len(indices), "mean_signed_error_body": float(np.mean(residual)),
            "rmse_body": float(np.sqrt(np.mean(residual ** 2))),
            "max_absolute_error_body": float(np.max(np.abs(residual))),
            "rounding_rmse_lower_body": float(np.sqrt(np.mean(np.maximum(np.abs(residual) - margin, 0.) ** 2))),
            "rounding_rmse_upper_body": float(np.sqrt(np.mean((np.abs(residual) + margin) ** 2)))})
    endpoint = phases[-1]["end"] - 1
    return {"phase_weighted_rmse_body": float(np.sqrt(np.mean([r["rmse_body"] ** 2 for r in rows]))),
            "rounding_rmse_interval_body": [float(np.sqrt(np.mean([r[key] ** 2 for r in rows])))
                for key in ("rounding_rmse_lower_body", "rounding_rmse_upper_body")],
            "endpoint_frame": endpoint,
            "endpoint_signed_error_body": float(observed[endpoint] - predicted[endpoint]) if valid[endpoint] else None,
            "phases": rows}


def _press_delay_prediction(profile, events, delay, frame_count=FRAME_COUNT):
    """A generic delayed-press hypothesis with immediate releases, either direction.

    Every press is deferred by the same fixed number of ticks. Its release
    cancels a pending press; release precedes press on a simultaneous reversal.
    Parameters/integration are the existing fitted model, never refitted here.
    """
    if type(delay) is not int or delay not in POLICY["press_delay_release_fixed_ticks"]:
        raise ValueError("Delay must belong to the preregistered diagnostic grid")
    by_tick = {}
    for event in events:
        if (type(event.get("tick")) is not int or not 0 <= event["tick"] < frame_count
                or event.get("action") not in {"left", "right"} or type(event.get("pressed")) is not bool):
            raise ValueError("Expected bounded directional input events")
        by_tick.setdefault(event["tick"], []).append(event)
    speed, acceleration = profile["max_speed_body_s"], profile["accel_body_s2"]
    active, pending, velocity, position, result = set(), {}, 0., 0., []
    for tick in range(frame_count):
        for event in sorted(by_tick.get(tick, []), key=lambda event: event["pressed"]):
            action = event["action"]
            if event["pressed"]:
                pending[action] = tick + delay
            else:
                pending.pop(action, None)
                active.discard(action)
        for action, ready in list(pending.items()):
            if ready <= tick:
                active.add(action)
                del pending[action]
        if len(active) > 1:
            raise ValueError("Opposing simultaneous inputs are not a diagnostic hypothesis")
        direction = int("right" in active) - int("left" in active)
        if direction:
            velocity += float(np.clip(direction * speed - velocity, -acceleration / FPS, acceleration / FPS))
        elif profile["family"] == fitting.RETENTION:
            velocity *= profile["retention_per_tick"]
        elif profile["family"] == fitting.LINEAR:
            brake = profile["brake_body_s2"] / FPS
            velocity += float(np.clip(-velocity, -brake, brake))
        else:
            raise ValueError("Unsupported fitted profile family")
        position += velocity / FPS
        result.append(position)
    return np.asarray(result)


def _visible_onset(item):
    start = item["phases"][0]["start"]
    origin = item["origin"]
    frames = item["report"]["frames"]
    pre = [frames[i]["x"] for i in range(20, 30)]
    extent = max(abs(x - origin) for x in pre)
    visible = next((i for i in range(start, item["phases"][-1]["end"])
                    if item["valid"][i] and abs(frames[i]["x"] - origin) > extent), None)
    return {"first_input_frame": start, "first_visible_displacement_frame": visible,
            "visible_delay_ticks": visible - start if visible is not None else None,
            "unobserved_frames_before_first_visible": (sum(not item["valid"][i] for i in range(start, visible))
                                                        if visible is not None else None),
            "not_a_physics_callback_timestamp": True}


def _aggregate_hypotheses(rows):
    result = []
    for family, values in (("uniform_output_lag", POLICY["uniform_output_lag_ticks"]),
                           ("press_delay_release_fixed", POLICY["press_delay_release_fixed_ticks"])):
        for ticks in values:
            observations = [row[family][ticks] for row in rows]
            result.append({"hypothesis": family, "ticks": ticks,
                "rmse_body": float(np.sqrt(np.mean([r["phase_weighted_rmse_body"] ** 2 for r in observations]))),
                "rounding_rmse_interval_body": [float(np.sqrt(np.mean([r["rounding_rmse_interval_body"][index] ** 2
                                                              for r in observations]))) for index in (0, 1)]})
    minimum_upper = min(row["rounding_rmse_interval_body"][1] for row in result)
    best = min(row["rmse_body"] for row in result)
    for row in result:
        row["nominal_minimum"] = abs(row["rmse_body"] - best) < 1e-12
        row["rounding_compatible_with_minimum"] = row["rounding_rmse_interval_body"][0] <= minimum_upper + 1e-12
    return result


def calibration_diagnostics(reference_reports, target_reports, profile):
    """Return an advisory supplement without changing inputs or existing gates."""
    refs = _reports(reference_reports, {"reference"})
    targets = _reports(target_reports, {"before", "after"})
    if (not isinstance(profile, dict) or profile.get("fps") != FPS or profile.get("mechanic") != "ground"
            or profile.get("status") not in {"ok", "mismatch"}):
        raise ValueError("A reference-only 60Hz ground profile is required")
    provenance = profile.get("provenance", {})
    if provenance and (provenance.get("source") != "reference calibration pixels only"
                       or any(slot[0] not in {"r06", "r48"} for slot in provenance.get("calibration_slots", []))):
        raise ValueError("Profile provenance must be calibration-only")
    runs, hypotheses = [], []
    for key in sorted(refs):
        reference, target = refs[key], targets[key]
        scenario, phases = reference["report"]["scenario"], reference["phases"]
        nominal = fitting.simulate_profile(profile, scenario)
        if not np.all(np.isfinite(nominal)):
            raise ValueError("Fitted profile did not produce finite native positions")
        margin = 1. / target["height"]
        ref_fit = _statistics(reference["x"], nominal, phases, reference["valid"], 1. / reference["height"])
        target_fit = _statistics(target["x"], nominal, phases, target["valid"], margin)
        direct = _statistics(target["x"], reference["x"], phases, target["valid"] & reference["valid"],
                             margin + 1. / reference["height"])
        scores = {"uniform_output_lag": {}, "press_delay_release_fixed": {}}
        for lag in POLICY["uniform_output_lag_ticks"]:
            predicted = nominal[np.clip(np.arange(FRAME_COUNT) - lag, 0, FRAME_COUNT - 1)]
            scores["uniform_output_lag"][lag] = _statistics(target["x"], predicted, phases, target["valid"], margin)
        for delay in POLICY["press_delay_release_fixed_ticks"]:
            predicted = _press_delay_prediction(profile, scenario["events"], delay)
            scores["press_delay_release_fixed"][delay] = _statistics(target["x"], predicted, phases, target["valid"], margin)
        hypotheses.append(scores)
        release = phases[-1]["start"]
        sample_indices = sorted(set(range(29, 37)) | {release - 1, release, release + 1, release + 2})
        runs.append({"scenario": deepcopy(scenario), "reference_body_height_px": reference["height"],
            "target_body_height_px": target["height"], "reference_vs_profile": ref_fit,
            "target_vs_profile": target_fit, "target_minus_reference": direct,
            "visible_onset": {"reference": _visible_onset(reference), "target": _visible_onset(target)},
            "input_edge_samples": [{"frame_index": i, "reference_x_body": float(reference["x"][i]) if reference["valid"][i] else None,
                "target_x_body": float(target["x"][i]) if target["valid"][i] else None,
                "profile_x_body": float(nominal[i])} for i in sample_indices]})
    mean = lambda field: float(np.sqrt(np.mean([r[field]["phase_weighted_rmse_body"] ** 2 for r in runs])))
    reference_rmse, target_rmse, margin = mean("reference_vs_profile"), mean("target_vs_profile"), 1. / runs[0]["target_body_height_px"]
    fit = profile.get("fit_diagnostics", {})
    recorded_rmse = fit.get("rmse_body")
    fit_consistent = _finite(recorded_rmse) and abs(recorded_rmse - reference_rmse) <= 1e-6
    confidence_reasons = []
    if not fit_consistent: confidence_reasons.append("Recorded reference fit does not match these calibration observations")
    if profile["status"] != "ok" or fit.get("model_mismatch") is not False:
        confidence_reasons.append("The supported model family does not establish a usable reference fit")
    if fit.get("ambiguous") is not False: confidence_reasons.append("Reference model-family selection is ambiguous or unreported")
    advisory = ("indeterminate" if confidence_reasons else "refinement_recommended"
                if target_rmse > reference_rmse + margin else "within_reference_fit_plus_target_rounding")
    grid = _aggregate_hypotheses(hypotheses)
    return {"schema_version": 1, "status": "diagnostic", "policy": deepcopy(POLICY),
        "scope": "reference and actual target calibration pixels plus unchanged reference-fitted profile",
        "acceptance_modified": False, "profile_refitted": False, "source_or_state_log_read": False,
        "profile_sha256": _digest(profile),
        "input_analysis_content_sha256": {role: [_digest(item) for item in reports]
            for role, reports in (("reference", reference_reports), ("target", target_reports))},
        "summary": {"input_aligned_target_reference_rmse_body": mean("target_minus_reference"),
            "reference_profile_rmse_body": reference_rmse, "target_profile_rmse_body": target_rmse,
            "target_rounding_margin_body": margin, "refinement_advisory": advisory,
            "advisory_boundary_body": reference_rmse + margin,
            "target_model_discrepancy_above_boundary_body": target_rmse - reference_rmse - margin,
            "recorded_reference_fit_rmse_body": recorded_rmse, "reference_fit_reproduced": fit_consistent,
            "confidence_limits": confidence_reasons},
        "lag_hypotheses": grid,
        "lag_interpretation": {"positive_uniform_lag": "rendered curve later than unchanged input-aligned model",
            "press_delay_release_fixed": "all directional press activations delayed equally; releases remain at commanded ticks",
            "ambiguous": sum(r["rounding_compatible_with_minimum"] for r in grid) > 1,
            "causal_conclusion": "unidentified; position residuals alone do not prove callback scheduling or any code location"},
        "runs": runs,
        "guidance": "Compare both hold durations and their press/release samples. If refinement is recommended, inspect input-to-motion timing, update cadence, scale conversion and release behavior in your own implementation. A matching parameter value does not establish matching rendered motion. Eligible is a minimum gate, not evidence that the calibration fit has been reproduced. Use remaining calibration attempts according to the trial budget; preserve all air/jump behavior.",
        "limitations": ["Hypothesis search is diagnostic only; acceptance stays on the original input-aligned curve.",
            "Pixel intervals cover rounding of position/origin, not tracking, landmark, scale or model error; they are not statistical confidence intervals.",
            "Deterministic repeated frames are not independent observations; no confidence gain is claimed from repeats.",
            "Visible movement can lag physical motion by subpixel quantization; it is not a callback timestamp.",
            "Only calibration r06/r48 are used. No held-out results, controller code, engine positions or velocities are inputs."]}


def _load_calibration(directory, paths, role):
    """Reject held-out/outside paths before opening analysis contents."""
    directory = Path(directory).resolve()
    if not isinstance(paths, (list, tuple)) or len(paths) != 6:
        raise ValueError("Expected six explicit calibration paths")
    loaded, hashes, run_roots = [], {}, set()
    for raw in paths:
        path = Path(raw).resolve()
        run = path.parent if path.name == "analysis.json" else path
        if role == "reference": expected = directory / "reference" / "calibration" / run.name
        elif role == "before": expected = directory / "baseline-runs" / "calibration" / run.name
        else:
            if not run.is_relative_to(directory / "candidates") or len(run.relative_to(directory / "candidates").parts) != 4:
                raise ValueError("Target evidence is not a candidate calibration copy")
            cid = run.relative_to(directory / "candidates").parts[0]
            expected = directory / "candidates" / cid / "runs" / "calibration" / run.name
        if (run != expected or run.name not in {f"{sid}-x0-r{r}" for sid in ("r06", "r48") for r in (1, 2, 3)}
                or not run.is_relative_to(directory)):
            raise ValueError("Only owned exact calibration run paths can be read")
        run_roots.add(run.parent)
        for name in ("manifest.json", "analysis.json"):
            if (run / name).resolve().parent != run:
                raise ValueError("Linked calibration evidence is not allowed")
        manifest_hash = sha256(run / "manifest.json")
        manifest = read_json(run / "manifest.json")
        scenario = manifest.get("scenario", {})
        if scenario.get("split") != "calibration" or scenario.get("id") not in {"r06", "r48"}:
            raise ValueError("Manifest is not calibration evidence")
        analysis_hash = sha256(run / "analysis.json")
        report = read_json(run / "analysis.json")
        if (report.get("scenario") != scenario or report.get("variant") != role or manifest.get("variant") != role
                or manifest.get("status") != "ok" or manifest.get("fps") != FPS or manifest.get("frame_count") != FRAME_COUNT
                or report.get("provenance", {}).get("manifest_sha256") != manifest_hash
                or manifest_hash != sha256(run / "manifest.json") or analysis_hash != sha256(run / "analysis.json")):
            raise ValueError("Calibration analysis is not bound to its native capture")
        hashes[str(run / "manifest.json")] = manifest_hash
        hashes[str(run / "analysis.json")] = analysis_hash
        loaded.append(report)
    if len(run_roots) != 1:
        raise ValueError("Cannot mix candidate snapshots in calibration feedback")
    _reports(loaded, {role})
    return loaded, hashes


def _supplement(directory, reference_paths, target_paths, profile, target_role):
    refs, refs_hash = _load_calibration(directory, reference_paths, "reference")
    targets, target_hash = _load_calibration(directory, target_paths, target_role)
    result = calibration_diagnostics(refs, targets, profile)
    inputs = {**refs_hash, **target_hash}
    if any(sha256(Path(path)) != expected for path, expected in inputs.items()):
        raise ValueError("Calibration evidence changed while building diagnostic feedback")
    result["input_file_sha256"] = inputs
    return result


@contextmanager
def enhanced_calibration_feedback(directory):
    """Append diagnostics to prepare/evaluate feedback in this process only.

    The caller owns protocol/source validation and must opt in explicitly after
    repaired_measurements(). This context does not change legacy eligibility.
    """
    directory = Path(directory).resolve()
    if not _LOCK.acquire(blocking=False):
        raise IntegrationError("feedback_context_active", "Calibration feedback context is already active")
    original_prepare, original_feedback = ground_worker._prepare_feedback, comparison.calibration_feedback
    try:
        def prepare(session, reference, before, check):
            if session.directory.resolve() != directory:
                raise ValueError("Calibration feedback belongs to a different session")
            supplement = _supplement(directory, reference["runs"], before, reference["profile"], "before")
            result = original_prepare(session, reference, before, check)
            return {**result, "motion_diagnostics": supplement}

        def evaluate(refpaths, beforepaths, afterpaths):
            # Validate all roles before the legacy reader can open any paths.
            _load_calibration(directory, beforepaths, "before")
            profile = read_json(directory / "session.json")["reference"]["profile"]
            supplement = _supplement(directory, refpaths, afterpaths, profile, "after")
            result = original_feedback(refpaths, beforepaths, afterpaths)
            return {**result, "motion_diagnostics": supplement}

        ground_worker._prepare_feedback, comparison.calibration_feedback = prepare, evaluate
        yield
    finally:
        ground_worker._prepare_feedback, comparison.calibration_feedback = original_prepare, original_feedback
        _LOCK.release()
