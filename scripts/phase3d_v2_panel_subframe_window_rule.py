"""Prospective sub-frame input-window rule for the realistic position reader.

The frozen reader splits every recording at each input time and fails an
active input window that holds no image.  When two inputs arrive inside one
compositor interval (pointerdown, then the first pointermove 5-12 ms later),
that window fails although no recorder could image it.  This rule records such
a window as not observable only when it is shorter than the recording's median
image interval, lies between two consecutive images no further apart than the
recorder's motion-gap limit (input clock and image time uncertainty included),
and both images show the active object validly in both methods within the
window tolerance.  The response is then checked in the following input window.
Every other empty active window still fails.

The corrected reader is an isolated AST copy; the frozen module is never
mutated.  Applied to an existing result this is a labelled post-hoc
diagnostic: the canonical result, its numeric FAIL and every acceptance axis
stay unchanged.  A future run may use the rule only by naming this file's
SHA-256 before the run starts.
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
import inspect
import json
import math
from pathlib import Path
import re
import statistics
import sys
import textwrap

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import realistic_review as review
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from kiroclops.web_local.specs import TIMING_LIMITS
from scripts import phase3d_v2_panel_event_identity_diagnostic as event_key
from scripts import phase3d_v2_rollup_report as report
from scripts import phase3d_v2_web_final_raw as predecessor_reader

VERSION = "panel-subframe-input-window-rule-v1"
SUBFRAME_REASON = "input_window_between_consecutive_images"
UNSAMPLED_REASON = "expected_active_interval_unsampled"
WINDOW_CODE = "raw_motion_window_unqualified"
POSITION_FAILURE_CODES = {"raw_position_unqualified", WINDOW_CODE}
PREDECESSOR_PRODUCER_SHA256 = "72a3b518e1c494438f8c1e289b7916451faa5f775f7250ee14011b30d91452b5"
EVENT_KEY_SHA256 = "aae4cfa38abdf27cedb165dbb062be11f327173a2a28c1cebff7221586e69f4e"
IDENTITY = ("record_id", "object_id", "role", "window_id", "window_s", "tolerance_px")
CONDITIONS = (
    "The frozen reader found no valid sample: the active input window holds no image.",
    "The recording was captured under the frozen web timing limits.",
    "No image time interval meets the window widened by both input clock uncertainties.",
    "The window is shorter than the recording's median image interval.",
    "The two consecutive bracketing images are at most maximum_motion_gap_s apart.",
    "In both bracketing images the object is active, the produced sample is valid, the independent "
    "point is finite and their distance is within the window tolerance.",
)

# The exact frozen branch for an empty input window, comments removed.
FROZEN_EMPTY_WINDOW_BLOCK = '''
active_overlap = any(min(right, b) > max(left, a) for a, b in bundle["active"][oid])
if not active_overlap:
    checks.append({"record_id": record["id"], "object_id": oid, "role": record["role"],
        "window_id": f"input-{occurrence}", "window_s": [left, right],
        "status": "not_observable", "observable": False,
        "reason": "object_not_active_in_input_window", "valid_ratio": None,
        "median_error_px": None, "tolerance_px": tolerance, "pass": None})
    continue
'''
# Appended to that branch; an unqualified window falls through to the frozen failure.
SUBFRAME_BLOCK = '''
subframe = subframe_window(bundle, oid, obj["frames"], left, right, tolerance)
if subframe is not None:
    checks.append({"record_id": record["id"], "object_id": oid, "role": record["role"],
        "window_id": f"input-{occurrence}", "window_s": [left, right],
        "status": "not_observable", "observable": False,
        "reason": SUBFRAME_REASON, "valid_ratio": None,
        "median_error_px": None, "tolerance_px": tolerance, "pass": None,
        "subframe_evidence": subframe})
    continue
'''


def _need(value, message, code="invalid_subframe_window_diagnostic"):
    if not value:
        raise VisualError(code, message)


def _finite(*values):
    return all(type(value) in (int, float) and math.isfinite(value) for value in values)


def _image_interval(frame):
    value = frame.get("time_interval_s", [frame["time_s"], frame["time_s"]])
    if not (isinstance(value, (list, tuple)) and len(value) == 2 and _finite(*value)
            and value[0] <= frame["time_s"] <= value[1]):
        return None
    return float(value[0]), float(value[1])


def _input_uncertainty(events, stamp):
    values = [event.get("clock_uncertainty_s") for event in events if event.get("time_s") == stamp]
    if not values or not all(_finite(value) and value >= 0 for value in values):
        return None
    return max(values)


def subframe_window(bundle, object_id, produced, left, right, tolerance):
    """Evidence that an empty active input window lies between two consecutive images, else None."""
    manifest = bundle["manifest"]
    frames, events = manifest["frames"], manifest.get("inputs", {}).get("events", [])
    if manifest.get("metadata", {}).get("timing", {}).get("limits") != TIMING_LIMITS:
        return None
    if len(frames) < 3 or len(produced) != len(frames):
        return None
    times = [frame["time_s"] for frame in frames]
    intervals = [value for value in map(_image_interval, frames) if value is not None]
    lead, trail = _input_uncertainty(events, left), _input_uncertainty(events, right)
    if len(intervals) != len(frames) or lead is None or trail is None or not _finite(left, right, tolerance):
        return None
    low, high = left - lead, right + trail
    if any(a <= high and b >= low for a, b in intervals):
        return None
    prior = max((index for index, (_, b) in enumerate(intervals) if b < low), default=None)
    following = min((index for index, (a, _) in enumerate(intervals) if a > high), default=None)
    if prior is None or following is None or following != prior + 1:
        return None
    median = statistics.median(b - a for a, b in zip(times, times[1:]))
    duration, bracket = right - left, times[following] - times[prior]
    limit = TIMING_LIMITS["maximum_motion_gap_s"]
    if not (0 <= duration < median and bracket <= limit):
        return None
    images = []
    for index in (prior, following):
        frame, sample = frames[index], produced[index]
        point = review._point(bundle, object_id, frame["index"])
        if (sample.get("index") != frame["index"] or sample.get("time_s") != frame["time_s"]
                or sample.get("valid") is not True
                or not _finite(point.get("x"), point.get("y"), sample.get("x"), sample.get("y"))
                or not any(a <= frame["time_s"] <= b for a, b in bundle["active"][object_id])):
            return None
        error = math.hypot(point["x"] - sample["x"], point["y"] - sample["y"])
        if error > tolerance + 1e-9:
            return None
        images.append({"index": frame["index"], "time_s": frame["time_s"], "image_sha256": frame.get("sha256"),
                       "raw": [point["x"], point["y"]], "produced": [sample["x"], sample["y"]],
                       "error_px": error})
    return {"rule": VERSION, "window_duration_s": duration, "recording_median_image_interval_s": median,
            "bracketing_interval_s": bracket, "maximum_bracket_s": limit,
            "input_clock_uncertainty_s": [lead, trail], "bracketing_images": images,
            "response_checked_in_following_input_window": True}


def _dump(nodes):
    return ast.dump(ast.Module(body=list(nodes), type_ignores=[]), include_attributes=False)


def corrected_position_method():
    """Copy the frozen position reader with the sub-frame branch appended once."""
    source = textwrap.dedent(inspect.getsource(review._position_checks))
    original = ast.parse(source)
    function = original.body[0] if original.body else None
    _need(isinstance(function, ast.FunctionDef) and function.name == "_position_checks",
          "Unexpected frozen position function.", "unsupported_position_reader")
    test = ast.dump(ast.parse("not validity", mode="eval").body, include_attributes=False)
    frozen = _dump(ast.parse(FROZEN_EMPTY_WINDOW_BLOCK).body)
    addition = ast.parse(SUBFRAME_BLOCK).body
    changed = deepcopy(original)
    replacements = 0
    for node in ast.walk(changed):
        if (isinstance(node, ast.If) and ast.dump(node.test, include_attributes=False) == test
                and _dump(node.body) == frozen):
            node.body = node.body + deepcopy(addition)
            replacements += 1
    _need(replacements == 1, "Only the exact known empty-window branch may be extended.", "unsupported_position_reader")
    renamed = changed.body[0]
    if not isinstance(renamed, ast.FunctionDef):
        raise VisualError("unsupported_position_reader", "Unexpected frozen position function.")
    renamed.name = "_subframe_position_checks"
    ast.fix_missing_locations(changed)
    environment = dict(review.__dict__)
    environment.update(subframe_window=subframe_window, SUBFRAME_REASON=SUBFRAME_REASON)
    exec(compile(changed, "<isolated-subframe-input-window>", "exec"), environment)
    transform = {"kind": "ExactSubframeInputWindowASTTransform", "schema_version": 1, "version": VERSION,
        "source_function": "realistic_review._position_checks", "source_file": str(Path(review.__file__).resolve()),
        "source_ast_sha256": digest(ast.dump(original, include_attributes=False)),
        "corrected_ast_sha256": digest(ast.dump(changed, include_attributes=False)),
        "replacement_count": replacements, "changed_scope": "empty_active_input_window_branch_only",
        "conditions": list(CONDITIONS), "timing_limits": dict(TIMING_LIMITS),
        "tolerances_ratios_and_other_checks_changed": False, "frozen_module_mutated": False}
    return environment["_subframe_position_checks"], transform


def compare_same_raw(bundles):
    """Run the frozen and corrected readers on the same points; only empty windows may differ."""
    old = review._position_checks(bundles)
    method, transform = corrected_position_method()
    new = method(bundles)
    (old_checks, old_failures), (new_checks, new_failures) = old, new
    _need(len(old_checks) == len(new_checks), "The rule cannot add or remove checks.", "wrong_window_transform")
    changed = []
    for before, after in zip(old_checks, new_checks):
        if before == after:
            continue
        _need(before.get("reason") == UNSAMPLED_REASON and before.get("pass") is False
              and before.get("valid_ratio") == 0 and before.get("median_error_px") is None
              and after.get("reason") == SUBFRAME_REASON and after.get("status") == "not_observable"
              and after.get("pass") is None and isinstance(after.get("subframe_evidence"), dict)
              and all(before.get(key) == after.get(key) for key in IDENTITY),
              "Only empty active input windows may be reclassified.", "wrong_window_transform")
        changed.append({"before": before, "after": after})
    removed = [{"code": WINDOW_CODE, **row["before"]} for row in changed]
    retained = [row for row in old_failures if row not in removed]
    _need(len(retained) == len(old_failures) - len(removed) and new_failures == retained,
          "Failures may change only by the reclassified windows.", "wrong_window_transform")
    return old, new, transform, changed


def _derive(session, directory, receipt_pin, inputs):
    _need(isinstance(receipt_pin, str) and re.fullmatch(r"[0-9a-f]{64}", receipt_pin),
          "An externally observed predecessor receipt pin is required.", "untrusted_predecessor")
    inputs.bind(directory / "evidence-receipt.json", receipt_pin)
    _need(not (directory / "attestation.json").exists(), "Only the unchanged unsigned failed proposal is supported.")
    inputs.code(predecessor_reader.__file__, PREDECESSOR_PRODUCER_SHA256)
    inputs.code(event_key.__file__, EVENT_KEY_SHA256)
    with event_key.no_png_decode():
        proof = predecessor_reader.validate(directory, session_directory=session, require_attested=False)
        _need(proof.get("kind") == "IndependentFinalWebEnvelopeRawVerification"
              and proof.get("status") == "proposal_verified" and proof.get("numeric_status") == "fail"
              and proof.get("domain") == "panel" and proof.get("record_count") == 128
              and proof.get("verified_scopes") == {} and proof.get("full_behavior_acceptance") is False,
              "The predecessor must be the complete unsigned failed panel matrix.", "wrong_predecessor")
        inputs.declared(proof["consumed_files"])
        old_result = inputs.read(directory / "result.json")
        snapshots = inputs.read(directory / "records.json")
        extraction = inputs.read(directory / "extraction-complete.json")
        _need(extraction.get("all_pixel_extraction_finished_before_measurement_read") is True,
              "The original pixel-extraction ordering is unproven.", "wrong_raw_points")
        with predecessor_reader._controller():
            context, values = review._session_context(session)
            records = review._records(values, "panel", "final")
        _need(context == old_result.get("context") and snapshots == [{**row, "package": str(row["package"])} for row in records]
              and len(records) == 128 and len({row["id"] for row in records}) == 128
              and set(extraction.get("raw_points_sha256", {})) == {row["id"] for row in records},
              "Only the complete canonical predecessor row matrix can be reconstructed.", "wrong_raw_points")
        bundles = [event_key._raw_bundle(record, directory, extraction, inputs) for record in records]
        measurement_inputs = {}
        for bundle in bundles:
            review._measurement(bundle, measurement_inputs)
        inputs.declared(measurement_inputs)
        old, new, transform, changed = compare_same_raw(bundles)
        old_events, corrected_events, event_transform = event_key.compare_same_raw(bundles, context.get("event_contract") or {})
    (old_checks, old_position_failures), (new_checks, new_position_failures) = old, new
    known = POSITION_FAILURE_CODES | event_key.EVENT_FAILURE_CODES
    saved = old_result.get("failures", [])
    _need(old_checks == old_result.get("positions")
          and old_position_failures == [row for row in saved if row.get("code") in POSITION_FAILURE_CODES],
          "The frozen position reader no longer reproduces the saved failed proposal.", "stale_predecessor")
    _need(old_events[0] == old_result.get("event_checks") and old_events[2] == old_result.get("event_relevance")
          and old_events[3] == [row for row in saved if row.get("code") in event_key.EVENT_FAILURE_CODES],
          "The frozen event reader no longer reproduces the saved failed proposal.", "stale_predecessor")
    other = [row for row in saved if row.get("code") not in known]
    remaining = deepcopy(new_position_failures) + deepcopy(other) + deepcopy(corrected_events[3])
    unsampled = [row for row in old_position_failures if row.get("reason") == UNSAMPLED_REASON]
    retained = [row for row in new_position_failures if row.get("reason") == UNSAMPLED_REASON]
    evidence = [row["after"]["subframe_evidence"] for row in changed]
    errors = [image["error_px"] for item in evidence for image in item["bracketing_images"]]

    def span(key):
        return [min(item[key] for item in evidence), max(item[key] for item in evidence)] if evidence else None

    return {"kind": "PanelSubframeInputWindowDiagnostic", "schema_version": 1, "version": VERSION,
        "status": "diagnostic_verified", "domain": "panel",
        "canonical_numeric_status": old_result.get("numeric_status"),
        "post_hoc_numeric_status_with_event_key_and_subframe_rules": "pass" if not remaining else "fail",
        "session_directory": str(session), "predecessor_directory": str(directory),
        "predecessor_receipt_sha256": receipt_pin,
        "candidate_sha256": proof["candidate_sha256"], "suite_sha256": proof["suite_sha256"],
        "selected_candidate_id": proof["selected_candidate_id"], "evaluation_job_id": proof["evaluation_job_id"],
        "final_job_id": proof["final_job_id"], "record_count": proof["record_count"],
        "required_observations": proof["required_observations"],
        "position_transform": transform, "event_key_transform": event_transform,
        "old_position_check_count": len(old_checks), "corrected_position_checks_sha256": digest(new_checks),
        "old_unsampled_window_failure_count": len(unsampled), "reclassified_window_count": len(changed),
        "retained_unsampled_window_failure_count": len(retained), "retained_unsampled_window_failures": retained,
        "reclassified_windows": changed, "reclassified_duration_range_s": span("window_duration_s"),
        "reclassified_bracket_range_s": span("bracketing_interval_s"),
        "maximum_bracketing_image_error_px": max(errors) if errors else None,
        "old_event_failure_count": len(old_events[3]), "corrected_event_failure_count": len(corrected_events[3]),
        "other_failures_unchanged": other, "remaining_failure_count": len(remaining), "remaining_failures": remaining,
        "independent_raw_predictions_sha256": digest(old_events[1]),
        "png_decoding_performed": False, "new_pixel_extraction_performed": False,
        "new_recording_or_model_started": False, "frozen_module_mutated": False,
        "canonical_result_changed": False, "historical_outcomes_rewritten": False,
        "applies_prospectively_only": True, "contact_sheets_inspected": False,
        "behavior_failure_outside_raw_review": "realistic-suite-known-panel-interrupt-w768-r1: closing-star window 2.74537 px > 2 px remains FAIL.",
        "native_acceptance_granted": False, "source_acceptance_granted": False, "raw_acceptance_granted": False,
        "human_inspection_claimed": False, "full_behavior_acceptance": False,
        "scope": "Post-hoc diagnostic of the existing failed panel raw proposal under the two prospective reader "
                 "repairs. It changes no canonical result and grants no raw/native/source/domain success."}


SUMMARY_KEYS = ("kind", "version", "status", "canonical_numeric_status",
    "post_hoc_numeric_status_with_event_key_and_subframe_rules", "record_count",
    "old_unsampled_window_failure_count", "reclassified_window_count", "retained_unsampled_window_failure_count",
    "reclassified_duration_range_s", "reclassified_bracket_range_s", "maximum_bracketing_image_error_px",
    "old_event_failure_count", "corrected_event_failure_count", "remaining_failure_count",
    "png_decoding_performed", "raw_acceptance_granted", "full_behavior_acceptance")


def _summary(value):
    return {key: value[key] for key in SUMMARY_KEYS}


def run(session_directory, predecessor, predecessor_receipt_sha256, output):
    session, directory, output = map(_ordinary, (session_directory, predecessor, output))
    _need(not output.exists(), "Use a fresh append-only diagnostic directory.", "output_exists")
    for protected in (session, directory):
        _need(not output.is_relative_to(protected) and not protected.is_relative_to(output),
              "Diagnostic output must be separate from the Session and predecessor.", "unsafe_path")
    inputs = report.Inputs()
    for module in (sys.modules[__name__], report):
        inputs.code(module.__file__)
    value = _derive(session, directory, predecessor_receipt_sha256, inputs)
    inputs.stable()
    output.mkdir(parents=True)
    value["code_snapshot"], written = report._archive_code(inputs, output)
    inputs.stable()
    value["consumed_files"] = dict(inputs.files)
    value["consumed_files_sha256"] = digest(inputs.files)
    write_json(output / "result.json", value)
    write_json(output / "summary.json", _summary(value))
    written.update({name: file_sha256(output / name) for name in ("result.json", "summary.json")})
    write_json(output / "receipt.json", {"kind": "PanelSubframeInputWindowDiagnosticReceipt", "schema_version": 1,
        "version": VERSION, "result_sha256": digest(value), "predecessor_receipt_sha256": predecessor_receipt_sha256,
        "consumed_files_sha256": value["consumed_files_sha256"], "output_file_hashes": written,
        "raw_acceptance_granted": False, "full_behavior_acceptance": False})
    return _summary(value)


def validate(output):
    output = _ordinary(output)
    value, receipt = read_json(output / "result.json"), read_json(output / "receipt.json")
    _need(value.get("kind") == "PanelSubframeInputWindowDiagnostic" and value.get("version") == VERSION
          and receipt.get("kind") == "PanelSubframeInputWindowDiagnosticReceipt" and receipt.get("schema_version") == 1
          and receipt.get("version") == VERSION and receipt.get("result_sha256") == digest(value)
          and receipt.get("predecessor_receipt_sha256") == value.get("predecessor_receipt_sha256")
          and receipt.get("consumed_files_sha256") == value.get("consumed_files_sha256") == digest(value.get("consumed_files"))
          and receipt.get("raw_acceptance_granted") is receipt.get("full_behavior_acceptance") is False,
          "Diagnostic receipt changed.", "stale_diagnostic")
    inputs = report.Inputs()
    inputs.declared(value["consumed_files"])
    archives = value.get("code_snapshot")
    _need(isinstance(archives, dict) and set(archives) == set(inputs.code_files),
          "Every consumed implementation requires its exact archive.", "invalid_code_snapshot")
    paths = []
    for origin, entry in archives.items():
        _need(isinstance(entry, dict) and set(entry) == {"origin_path", "relative_path", "sha256"}
              and entry["origin_path"] == origin and entry["sha256"] == inputs.files[origin]
              and entry["relative_path"].startswith("snapshot/code/"), "Implementation archive changed.", "invalid_code_snapshot")
        inputs.bind(safe_path(output, entry["relative_path"]), entry["sha256"])
        paths.append(entry["relative_path"])
    _need(len(paths) == len(set(paths)) and set(receipt["output_file_hashes"]) == {"result.json", "summary.json"} | set(paths),
          "Output archive closure differs.", "stale_diagnostic")
    for name, sha in receipt["output_file_hashes"].items():
        inputs.bind(safe_path(output, name), sha)
    current = _derive(_ordinary(value["session_directory"]), _ordinary(value["predecessor_directory"]),
                      value["predecessor_receipt_sha256"], inputs)
    _need(current == {key: item for key, item in value.items() if key not in ("code_snapshot", "consumed_files", "consumed_files_sha256")}
          and read_json(output / "summary.json") == _summary(value),
          "Fresh exact source/point/window replay disagrees with this diagnostic.", "stale_diagnostic")
    inputs.stable()
    return _summary(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("run")
    for key in ("session", "predecessor", "output"):
        prepare.add_argument("--" + key, required=True, type=Path)
    prepare.add_argument("--predecessor-receipt-sha256", required=True)
    check = commands.add_parser("validate")
    check.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = (run(args.session, args.predecessor, args.predecessor_receipt_sha256, args.output)
              if args.command == "run" else validate(args.output))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
