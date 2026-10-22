"""Append-only diagnosis of the panel reader's repeated relative-parent key.

Only an externally pinned, complete, unsigned failed final proposal can enter.
Previously extracted image points are byte-verified and reused; no PNG decoding
or tracking is performed.  The sole event-method change is the wanted-key
normalization.  All other failures remain, and no acceptance is published.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import inspect
import json
from pathlib import Path
import re
import sys
import textwrap
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import (evidence, media_suite, prepared_evidence,
                                    suite_evidence, realistic_review as review)
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual import package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts import phase3d_v2_rollup_report as report
from scripts import phase3d_v2_web_final_raw as predecessor_reader

VERSION = "panel-relative-event-key-diagnostic-v1"
PREDECESSOR_PRODUCER_SHA256 = "72a3b518e1c494438f8c1e289b7916451faa5f775f7250ee14011b30d91452b5"
ORIGINAL_LOAD_PACKAGE = package.load_package
EVENT_FAILURE_CODES = {"raw_consumed_event_count_mismatch", "raw_consumed_event_disagreement",
                       "raw_consumed_relative_order_disagreement"}


def _need(value, message, code="invalid_event_identity_diagnostic"):
    if not value:
        raise VisualError(code, message)


def canonical_event_key(event, selection):
    """Normalize either public relative representation without deduplicating IDs."""
    ids = event.get("object_ids")
    _need(isinstance(ids, list) and 1 <= len(ids) <= 2
          and all(isinstance(value, str) and value for value in ids) and len(set(ids)) == len(ids),
          "Motion event IDs must be one/two distinct public identities.", "invalid_relative_event_identity")
    selected = {obj["id"] for obj in selection.get("objects", [])}
    _need(set(ids) <= selected, "Event contains an unselected identity.", "invalid_relative_event_identity")
    parent = event.get("relative_to_object_id")
    if parent is None:
        _need(len(ids) == 1, "A two-object motion event requires its public relative parent.", "invalid_relative_event_identity")
        return event.get("type"), tuple(ids)
    _need(isinstance(parent, str) and parent in selected and parent != ids[0],
          "Relative parent must be a distinct selected identity.", "invalid_relative_event_identity")
    matches = [spec for spec in selection.get("relative_motion", []) if spec.get("object_id") == ids[0]]
    _need(len(matches) == 1 and matches[0].get("relative_to") == parent,
          "Relative parent differs from the unique public child/parent contract.", "invalid_relative_event_identity")
    _need(len(ids) == 1 or ids[1] == parent,
          "The second explicit event identity is not its declared parent.", "invalid_relative_event_identity")
    return event.get("type"), tuple(ids + [parent] if len(ids) == 1 else ids)


def corrected_event_method():
    """Copy the frozen method with exactly its wanted-key block replaced.

    The source AST must contain the literal old block once.  Nothing is patched
    in its module, so candidates, ordering, tolerances and relative timing stay
    those of the existing independent reader.
    """
    source = textwrap.dedent(inspect.getsource(review._final_events))
    original = ast.parse(source)
    function = original.body[0]
    _need(isinstance(function, ast.FunctionDef) and function.name == "_final_events",
          "Unexpected frozen event function.", "unsupported_event_reader")
    expected = ast.parse('''
ids = list(event.get("object_ids", []))
if event.get("relative_to_object_id"):
    ids.append(event["relative_to_object_id"])
wanted[event["type"], tuple(ids)].append(event)
''').body
    changed = deepcopy(original)
    replacements = 0
    for node in ast.walk(changed):
        if isinstance(node, ast.If) and ast.dump(ast.Module(body=node.body, type_ignores=[]), include_attributes=False) == ast.dump(ast.Module(body=expected, type_ignores=[]), include_attributes=False):
            node.body = ast.parse("wanted[canonical_event_key(event, public)].append(event)").body
            replacements += 1
    _need(replacements == 1, "Only the exact known repeated-parent block may be replaced.", "unsupported_event_reader")
    changed.body[0].name = "_isolated_final_events"
    ast.fix_missing_locations(changed)
    environment = dict(review.__dict__)
    environment["canonical_event_key"] = canonical_event_key
    exec(compile(changed, "<isolated-relative-event-key>", "exec"), environment)
    transform = {"kind": "ExactRelativeParentKeyASTTransform", "schema_version": 1,
        "source_function": "realistic_review._final_events", "source_file": str(Path(review.__file__).resolve()),
        "source_ast_sha256": digest(ast.dump(original, include_attributes=False)),
        "corrected_ast_sha256": digest(ast.dump(changed, include_attributes=False)),
        "replacement_count": replacements, "changed_scope": "wanted_event_identity_key_only",
        "prediction_and_count_timing_order_method_changed": False, "frozen_module_mutated": False}
    return environment["_isolated_final_events"], transform


@contextmanager
def no_png_decode():
    """Use the official hash/header verifier on unchanged, decoded-before PNGs."""
    def hash_only(path, verify=True):
        _need(verify is True or verify == "hashes", "Unchecked package loading is forbidden.", "package_integrity_failure")
        return ORIGINAL_LOAD_PACKAGE(path, verify="hashes")

    def forbidden(*args, **kwargs):
        raise VisualError("png_decode_forbidden", "This diagnostic may only reuse existing independently extracted points.")

    modules = (package, evidence, media_suite, prepared_evidence, suite_evidence,
               review, review.legacy, review.painted, review.generic)
    with ExitStack() as stack:
        for module in dict.fromkeys(modules):
            if hasattr(module, "load_package"):
                _need(module.load_package is ORIGINAL_LOAD_PACKAGE, "Unexpected package-loader callback.", "unsupported_event_reader")
                stack.enter_context(patch.object(module, "load_package", hash_only))
        stack.enter_context(patch.object(package, "read_image", forbidden))
        stack.enter_context(patch.object(package.cv2, "imdecode", forbidden))
        yield


def _raw_bundle(record, directory, extraction, inputs):
    manifest = package.load_package(record["package"], verify="hashes")
    manifest_path = _ordinary(record["package"]) / "manifest.json"
    inputs.bind(manifest_path)
    path = safe_path(directory, "raw/" + record["id"] + "/points.json")
    inputs.bind(path, extraction["raw_points_sha256"].get(record["id"]))
    value = read_json(path)
    _need(value.get("kind") == "UnsignedIndependentPaintedPanelProposals" and value.get("status") == "unsigned"
          and value.get("package_sha256") == manifest["package_sha256"]
          and value.get("first_image_sha256") == manifest["frames"][0]["sha256"]
          and value.get("public_selection_sha256") == digest(record["selection"])
          and all(value.get(key) is False for key in ("production_coordinates_used", "production_bounds_used",
              "production_events_used", "smoothing_or_rounding_used", "target_code_or_dom_read")),
          "Only the exact earlier independent painted-panel extraction may be reused.", "wrong_raw_points")
    rows = value.get("rows")
    _need(isinstance(rows, list) and len(rows) == len(manifest["frames"])
          and all(row.get("index") == frame["index"] and row.get("time_s") == frame["time_s"]
                  and row.get("image_sha256") == frame["sha256"] and isinstance(row.get("points"), dict)
                  for row, frame in zip(rows, manifest["frames"])),
          "Raw point rows, image identities or recorder times changed.", "wrong_raw_points")
    return {"record": record, "manifest": manifest, "raw": value, "sheets": value.get("sheets", [])}


def compare_same_raw(bundles, contract):
    old = review._final_events(bundles, contract)
    method, transform = corrected_event_method()
    corrected = method(bundles, contract)
    _need(old[1] == corrected[1] and old[2] == corrected[2],
          "Independent predictions or role relevance changed beyond event-key normalization.", "wrong_event_transform")
    return old, corrected, transform


def _derive(session, directory, receipt_pin, inputs):
    _need(isinstance(receipt_pin, str) and re.fullmatch(r"[0-9a-f]{64}", receipt_pin),
          "An externally observed predecessor receipt pin is required.", "untrusted_predecessor")
    inputs.bind(directory / "evidence-receipt.json", receipt_pin)
    _need(not (directory / "attestation.json").exists(), "Only the unchanged unsigned failed proposal is supported.")
    inputs.code(predecessor_reader.__file__, PREDECESSOR_PRODUCER_SHA256)
    with no_png_decode():
        # Complete canonical/source/generated/code/runtime validation, while
        # hash/header checking the exact PNGs already decoded by the old aid.
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
        bundles = [_raw_bundle(record, directory, extraction, inputs) for record in records]
        measurement_inputs = {}
        for bundle in bundles:
            review._measurement(bundle, measurement_inputs)
        inputs.declared(measurement_inputs)
        old, corrected, transform = compare_same_raw(bundles, context.get("event_contract") or {})
    old_checks, _, old_relevance, old_failures = old
    new_checks, _, _, new_failures = corrected
    _need(old_checks == old_result.get("event_checks") and old_relevance == old_result.get("event_relevance")
          and old_failures == [row for row in old_result["failures"] if row.get("code") in EVENT_FAILURE_CODES],
          "The frozen event reader no longer reproduces the saved failed proposal.", "stale_predecessor")
    other_failures = [row for row in old_result["failures"] if row.get("code") not in EVENT_FAILURE_CODES]
    _need(other_failures and all(row.get("code") == "raw_motion_window_unqualified"
              and row.get("reason") == "expected_active_interval_unsampled" for row in other_failures),
          "Unrelated predecessor failures cannot be silently changed.", "unsupported_predecessor_failure")
    remaining = deepcopy(other_failures) + deepcopy(new_failures)
    return {"kind": "PanelRelativeEventIdentityDiagnostic", "schema_version": 1, "version": VERSION,
        "status": "diagnostic_verified", "numeric_status": "fail", "domain": "panel",
        "session_directory": str(session), "predecessor_directory": str(directory),
        "predecessor_receipt_sha256": receipt_pin,
        "candidate_sha256": proof["candidate_sha256"], "suite_sha256": proof["suite_sha256"],
        "selected_candidate_id": proof["selected_candidate_id"], "evaluation_job_id": proof["evaluation_job_id"],
        "final_job_id": proof["final_job_id"], "record_count": proof["record_count"],
        "required_observations": proof["required_observations"],
        "old_event_failure_count": len(old_failures), "old_event_count_mismatch_count": sum(row["code"] == "raw_consumed_event_count_mismatch" for row in old_failures),
        "corrected_event_failure_count": len(new_failures), "corrected_event_count_mismatch_count": sum(row["code"] == "raw_consumed_event_count_mismatch" for row in new_failures),
        "old_matched_event_check_count": len(old_checks), "corrected_matched_event_check_count": len(new_checks),
        "unchanged_other_failure_count": len(other_failures), "unchanged_other_failures": deepcopy(other_failures),
        "unchanged_other_failures_sha256": digest(other_failures), "remaining_failure_count": len(remaining),
        "remaining_failures": remaining, "corrected_event_checks": new_checks, "event_transform": transform,
        "independent_raw_predictions_sha256": digest(old[1]), "independent_raw_predictions_unchanged": True,
        "positions_geometry_controls_and_contact_sheets_changed": False,
        "png_verification": "same_previously_decoded_bytes_sha256_and_png_header_dimensions",
        "png_decoding_performed": False, "new_pixel_extraction_performed": False,
        "new_recording_or_model_started": False, "frozen_module_mutated": False,
        "native_acceptance_granted": False, "source_acceptance_granted": False, "raw_acceptance_granted": False,
        "human_inspection_claimed": False, "full_behavior_acceptance": False,
        "ordinary_reader_compatibility_claimed": False, "historical_outcomes_rewritten": False,
        "scope": "Event identity diagnosis only. Every unsampled-window failure is retained; no raw/native/source/domain success is granted."}


def _summary(value):
    return {key: value[key] for key in ("kind", "version", "status", "numeric_status", "record_count",
        "old_event_failure_count", "old_event_count_mismatch_count", "corrected_event_failure_count",
        "corrected_event_count_mismatch_count", "unchanged_other_failure_count", "remaining_failure_count",
        "png_decoding_performed", "raw_acceptance_granted", "full_behavior_acceptance")}


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
    write_json(output / "receipt.json", {"kind": "PanelRelativeEventIdentityDiagnosticReceipt", "schema_version": 1,
        "version": VERSION, "result_sha256": digest(value), "predecessor_receipt_sha256": predecessor_receipt_sha256,
        "consumed_files_sha256": value["consumed_files_sha256"], "output_file_hashes": written,
        "raw_acceptance_granted": False, "full_behavior_acceptance": False})
    return _summary(value)


def validate(output):
    output = _ordinary(output)
    value, receipt = read_json(output / "result.json"), read_json(output / "receipt.json")
    _need(value.get("kind") == "PanelRelativeEventIdentityDiagnostic" and value.get("version") == VERSION
          and receipt.get("kind") == "PanelRelativeEventIdentityDiagnosticReceipt" and receipt.get("schema_version") == 1
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
          "Fresh exact source/point/event replay disagrees with this diagnostic.", "stale_diagnostic")
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
