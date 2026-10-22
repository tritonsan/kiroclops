"""Append-only web domain closure with an explicitly separate raw-only reader.

This consumer does not publish an ordinary independent/source review or change
Session acceptance.  Its domain verdict combines the existing canonical stage
gates, historical/current source gates and a fresh custom final-pixel proof.
It cannot claim full phase success or a new SVG model generation.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import suite_evidence
from kiroclops.local_engine.session import Session, _ordinary
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts import phase3d_v2_rollup_report as base
from scripts import phase3d_v2_web_final_raw as raw

VERSION = "standalone-web-domain-closure-v1"
SPEC_PATHS = {"session", "suite", "calibration_preparation", "calibration_review",
              "initial_quality", "unchanged_result", "source_axis", "source_review"}
COUNTS = {"panel": 128, "svg": 112}
MANDATORY = tuple(key for key in base.AXES if key != "improvement")


def _need(value, message, code="invalid_web_closure"):
    if not value:
        raise VisualError(code, message)


def _config(value):
    _need(isinstance(value, dict) and set(value) == {"kind", "schema_version", "stage", "raw_review", "raw_proof"}
          and value["kind"] == "Phase3DV2WebFinalClosureInputs" and value["schema_version"] == 1,
          "Supply the explicit standalone web closure input schema.")
    spec = value["stage"]
    _need(isinstance(spec, dict) and set(spec) <= {"stage"} | SPEC_PATHS
          and spec.get("stage") in COUNTS and "session" in spec,
          "Only a panel/SVG canonical stage and named evidence paths are supported.")
    _need(all(isinstance(path, str) and path for key, path in spec.items() if key != "stage")
          and all(isinstance(value[key], str) and value[key] for key in ("raw_review", "raw_proof")),
          "Every evidence path must be explicit and nonempty.")
    _need(not (spec.get("source_axis") and spec.get("source_review")),
          "Choose historical unchanged origin or current source review, never both.")
    if spec["stage"] == "svg":
        _need(spec.get("source_axis") and spec.get("unchanged_result") and not spec.get("source_review"),
              "This SVG closure reviews the unchanged historical source; it cannot claim new authorship.")
    return deepcopy(value)


def _pairs(value):
    _need(isinstance(value, list) and value
          and all(isinstance(pair, list) and len(pair) == 2
                  and all(isinstance(item, str) and len(item) == 64 for item in pair) for pair in value),
          "Complete package/observation identities are required.", "incomplete_review")
    pairs = [tuple(pair) for pair in value]
    _need(len(pairs) == len(set(pairs)), "Repeated identities cannot enlarge proof coverage.", "incomplete_review")
    return set(pairs)


def _derive(inputs, config):
    spec = config["stage"]
    directory = _ordinary(spec["session"])
    # Reuse the canonical comparator, function and source/native origin gates.
    # The custom proof is intentionally absent from the ordinary reader's spec.
    row = base._stage(inputs, spec)
    _need(row.get("status") != "invalid" and row.get("complete_matrix_verified") is True,
          "The canonical stage must have a complete, valid terminal matrix.", "invalid_canonical_stage")
    state = Session(directory)._load()
    _need(state.get("status") == "finalized" and state.get("active_job_id") is None,
          "Wait for terminal recording and model work.", "stage_not_terminal")
    selected = [item for item in state.get("candidates", [])
                if item.get("candidate_id") == state.get("selected_candidate_id")]
    _need(len(selected) == 1, "One exact selected candidate is required.", "wrong_candidate")
    candidate = selected[0]
    _need(row.get("selected_candidate", {}).get("candidate_id") == candidate["candidate_id"]
          and row["selected_candidate"].get("source_sha256") == candidate["source_sha256"],
          "Canonical source changed during closure.", "wrong_candidate")
    suite = inputs.read(_ordinary(state["reference"]) / "suite.json")
    if spec["stage"] == "svg":
        _need(state.get("clock_origin") == "unchanged_initial_measurement"
              and row["axes"]["source"].get("new_code_authored") is False
              and row["axes"]["native_origin"].get("historical_only") is True,
              "SVG generation must stay explicitly historical.", "wrong_origin")
    proof = inputs.read(config["raw_proof"])
    fresh = raw.validate(config["raw_review"], session_directory=directory, require_attested=True)
    _need(proof == fresh, "Saved FULL custom proof differs from its fresh validator.", "stale_raw_proof")
    _need(proof.get("kind") == "IndependentFinalWebEnvelopeRawVerification"
          and proof.get("schema_version") == 1 and proof.get("version") == raw.VERSION
          and proof.get("status") == proof.get("numeric_status") == "pass"
          and proof.get("raw_only") is True
          and proof.get("ordinary_reader_compatibility_claimed") is False
          and proof.get("full_behavior_acceptance") is False and proof.get("new_model_generation") is False
          and proof.get("source_code_review_status") == proof.get("native_model_generation_review_status") == "pending"
          and proof.get("verified_scopes") == {"position": "pass", "events": "pass", "relationships": "pass"}
          and isinstance(proof.get("attestation_sha256"), str) and len(proof["attestation_sha256"]) == 64,
          "An attested passing raw-only proof is required; it cannot transfer source/native acceptance.", "incomplete_review")
    expected = {"candidate_sha256": candidate["source_sha256"], "suite_sha256": digest(suite),
                "selected_candidate_id": candidate["candidate_id"], "evaluation_job_id": candidate["job_id"],
                "final_job_id": state["final_job_id"], "record_count": COUNTS[spec["stage"]]}
    _need(all(proof.get(key) == value for key, value in expected.items())
          and _ordinary(proof.get("session_directory", "")) == directory
          and _ordinary(proof.get("raw_review_directory", "")) == _ordinary(config["raw_review"])
          and proof.get("domain") == ("panel" if spec["stage"] == "panel" else "svg"),
          "Custom proof belongs to another source, suite, job, domain or complete matrix.", "wrong_raw_binding")
    required = suite_evidence.required_bindings(directory, state, require_complete=True)
    _need(_pairs(proof.get("required_observations")) == required,
          "Raw review does not cover the exact complete canonical observations.", "incomplete_review")
    consumed = proof.get("consumed_files")
    _need(isinstance(consumed, dict) and consumed and digest(consumed) == proof.get("consumed_files_sha256"),
          "The FULL standalone proof's byte closure is required.", "incomplete_review")
    inputs.declared(consumed)
    # Only this axis changes.  Known failed/pending source/native/function gates
    # are never supplied by the raw-only proof and are never promoted here.
    row["axes"]["independent_raw_review"] = base.axis("pass", "Fresh custom web final-pixel proof and actual attestation replayed.",
        consumer_kind=proof["kind"], consumer_version=proof["version"], record_count=proof["record_count"],
        ordinary_reader_compatibility_claimed=False)
    required_statuses = [row["axes"][name]["status"] for name in MANDATORY]
    row["status"] = ("pass" if all(status == "pass" for status in required_statuses)
                     else "fail" if "fail" in required_statuses else "pending")
    accepted = row["status"] == "pass"
    return {"kind": "StandaloneWebDomainClosure", "schema_version": 1, "version": VERSION,
            "domain": spec["stage"], "status": row["status"], "domain_behavior_acceptance": accepted,
            "stage": row, "raw_proof": {**expected, "kind": proof["kind"], "version": proof["version"],
                "path": str(_ordinary(config["raw_proof"])), "file_sha256": inputs.files[str(_ordinary(config["raw_proof"]))],
                "consumed_files_sha256": proof["consumed_files_sha256"]},
            "source_epoch": suite["source_epoch"], "acceptance_profile": suite["acceptance_profile"],
            "historical_native_only": spec["stage"] == "svg", "new_model_generation": False,
            "completed_current_authorship_verified": base._verified_current_authorship(row),
            "ordinary_reader_compatibility_claimed": False, "canonical_session_acceptance_changed": False,
            "full_behavior_acceptance": False, "full_phase_acceptance": False,
            "recording_or_model_started": False, "comparison_thresholds_recomputed": False,
            "scope": "Only this web domain under the named standalone reader; no overall phase or ordinary-reader acceptance."}


def _paths(config):
    return [config["raw_review"], config["raw_proof"],
            *(value for key, value in config["stage"].items() if key != "stage")]


def _summary(value):
    return {key: value[key] for key in ("kind", "version", "domain", "status", "domain_behavior_acceptance",
            "historical_native_only", "new_model_generation", "ordinary_reader_compatibility_claimed",
            "full_behavior_acceptance", "full_phase_acceptance")}


def build(config_path, output):
    config_path, output = map(_ordinary, (config_path, output))
    _need(not output.exists(), "Preserve previous closures; use a fresh external output.", "output_exists")
    inputs = base.Inputs()
    config = _config(inputs.read(config_path))
    for named in [str(config_path), *_paths(config)]:
        protected = _ordinary(named)
        _need(not output.is_relative_to(protected) and not protected.is_relative_to(output),
              "Output must be separate from all input evidence.", "unsafe_path")
    # Check both named proof files before canonical processing can do any work.
    _need(_ordinary(config["raw_proof"]).is_file() and (_ordinary(config["raw_review"]) / "attestation.json").is_file(),
          "Supply the full proof and actual final attestation first.", "incomplete_review")
    for module in (sys.modules[__name__], base, raw, suite_evidence):
        inputs.code(module.__file__)
    value = _derive(inputs, config)
    inputs.stable()
    output.mkdir(parents=True)
    value["code_snapshot"], written = base._archive_code(inputs, output)
    inputs.stable()
    value["input_config"] = config
    value["consumed_files"] = dict(inputs.files)
    value["consumed_files_sha256"] = digest(inputs.files)
    write_json(output / "result.json", value)
    write_json(output / "summary.json", _summary(value))
    written.update({name: file_sha256(output / name) for name in ("result.json", "summary.json")})
    write_json(output / "receipt.json", {"kind": "StandaloneWebDomainClosureReceipt", "schema_version": 1,
        "version": VERSION, "result_sha256": digest(value), "consumed_files_sha256": digest(inputs.files),
        "output_file_hashes": written, "domain_behavior_acceptance": value["domain_behavior_acceptance"],
        "full_phase_acceptance": False, "ordinary_reader_compatibility_claimed": False})
    return _summary(value)


def validate(output):
    output = _ordinary(output)
    value, receipt = read_json(output / "result.json"), read_json(output / "receipt.json")
    _need(value.get("kind") == "StandaloneWebDomainClosure" and value.get("version") == VERSION
          and receipt.get("kind") == "StandaloneWebDomainClosureReceipt" and receipt.get("version") == VERSION
          and receipt.get("schema_version") == 1 and digest(value) == receipt.get("result_sha256")
          and receipt.get("consumed_files_sha256") == value.get("consumed_files_sha256") == digest(value.get("consumed_files")),
          "Closure receipt or input inventory changed.", "stale_closure")
    inputs = base.Inputs()
    inputs.declared(value["consumed_files"])
    archives = value.get("code_snapshot")
    _need(isinstance(archives, dict) and set(archives) == set(inputs.code_files),
          "Every consumed implementation origin requires its exact archive.", "invalid_code_snapshot")
    paths = []
    for origin, entry in archives.items():
        _need(isinstance(entry, dict) and set(entry) == {"origin_path", "relative_path", "sha256"}
              and entry["origin_path"] == origin and entry["sha256"] == inputs.files[origin]
              and entry["relative_path"].startswith("snapshot/code/"), "Source archive identity changed.", "invalid_code_snapshot")
        inputs.bind(safe_path(output, entry["relative_path"]), entry["sha256"])
        paths.append(entry["relative_path"])
    _need(len(paths) == len(set(paths)) and set(receipt.get("output_file_hashes", {})) == {"result.json", "summary.json"} | set(paths),
          "The exact output archive closure is required.", "stale_closure")
    for path, sha in receipt["output_file_hashes"].items():
        inputs.bind(safe_path(output, path), sha)
    current = _derive(inputs, _config(value["input_config"]))
    expected = {key: item for key, item in value.items()
                if key not in ("code_snapshot", "input_config", "consumed_files", "consumed_files_sha256")}
    _need(current == expected and read_json(output / "summary.json") == _summary(value)
          and receipt.get("domain_behavior_acceptance") is value["domain_behavior_acceptance"]
          and receipt.get("full_phase_acceptance") is False and receipt.get("ordinary_reader_compatibility_claimed") is False,
          "A fresh canonical/raw replay disagrees with the published domain verdict.", "stale_closure")
    inputs.stable()
    return _summary(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("build")
    prepare.add_argument("--config", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    check = commands.add_parser("validate")
    check.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = build(args.config, args.output) if args.command == "build" else validate(args.output)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
