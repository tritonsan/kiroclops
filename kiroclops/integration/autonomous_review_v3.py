"""Additive operator review for the registered CLI repair protocol.

Manual metadata changes a full analysis-file digest without changing measured
motion. Exclude only that digest at four exact speed-comparison locations from
the numerical invariant. Full analysis hashes remain in saved comparisons and
the existing per-run review receipt. This does not change any acceptance gate.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import threading

from kiroclops.common import ROOT
from . import ground_review, autonomous_workflow_v3 as workflow
from .storage import IntegrationError, read_json, sha256, utc_now

CORRECTION_ID = "autonomous-manual-analysis-provenance-v3"
EXCLUDED_PATHS = tuple(
    f"ground.{collection}[*].speed_comparison.{role}.analysis_content_sha256"
    for collection in ("validation_scenarios", "scenario_checks")
    for role in ("reference", "target")
)
OPERATOR_SOURCES = (
    "kiroclops/integration/autonomous_review_v3.py",
    "kiroclops/integration/ground_review.py",
    "kiroclops/integration/autonomous_workflow_v3.py",
    "kiroclops/integration/ground_comparison_v2.py",
    "kiroclops/integration/ground_speed_v2.py",
)
_LEGACY_NUMERICAL = ground_review._numerical
_CONTEXT_LOCK = threading.Lock()


def numerical_fingerprint(comparison):
    """Use the legacy fingerprint with only the documented leaf exclusions."""
    copied = deepcopy(comparison)
    ground = copied.get("ground")
    if isinstance(ground, dict):
        for collection in ("validation_scenarios", "scenario_checks"):
            rows = ground.get(collection)
            if not isinstance(rows, list):
                continue
            for row in rows:
                speed = row.get("speed_comparison") if isinstance(row, dict) else None
                if not isinstance(speed, dict):
                    continue
                for role in ("reference", "target"):
                    estimate = speed.get(role)
                    if isinstance(estimate, dict):
                        estimate.pop("analysis_content_sha256", None)
    return _LEGACY_NUMERICAL(copied)


@contextmanager
def operator_context(directory):
    """One verified, process-local context; do not nest with workflow context."""
    if not _CONTEXT_LOCK.acquire(blocking=False):
        raise IntegrationError("operator_context_active", "An operator context is already active")
    original = ground_review._numerical
    try:
        if original is not _LEGACY_NUMERICAL:
            raise IntegrationError("operator_context_active", "The numerical verifier is already modified")
        with workflow.operator_context(directory):
            ground_review._numerical = numerical_fingerprint
            try:
                yield
            finally:
                ground_review._numerical = original
    finally:
        _CONTEXT_LOCK.release()


def _source_hashes():
    return {name: sha256(ROOT / name) for name in OPERATOR_SOURCES}


def _binding_path(directory, review_path):
    directory = Path(directory).resolve()
    path = ground_review._local_file(Path(review_path), directory)
    if path.parent != directory / "final" or not path.name.startswith("ground-review-") or path.suffix != ".json":
        raise IntegrationError("invalid_operator_binding", "Expected a canonical ground review receipt")
    return ground_review._local_file(path.with_suffix(".repair-operator.json"), directory)


def _binding_content(directory, result):
    directory = Path(directory).resolve()
    review_path = ground_review._local_file(Path(result["review_path"]), directory)
    comparison_path = ground_review._local_file(Path(result["comparison_path"]), directory)
    protocol_path = ground_review._local_file(directory / "execution-protocol.json", directory)
    receipt = read_json(review_path)
    comparison = read_json(comparison_path)
    if (receipt.get("candidate_id") != result["candidate_id"]
            or receipt.get("comparison_sha256") != sha256(comparison_path)
            or receipt.get("numerical_sha256") != numerical_fingerprint(comparison)):
        raise IntegrationError("invalid_operator_binding", "Canonical review changed before operator binding")
    return {
        "schema_version": 1,
        "correction_id": CORRECTION_ID,
        "scope": "operator_provenance_only",
        "grants_new_acceptance": False,
        "session_id": directory.name,
        "candidate_id": result["candidate_id"],
        "protocol_id": workflow.PROTOCOL_ID,
        "execution_protocol_sha256": sha256(protocol_path),
        "operator_source_hashes": _source_hashes(),
        "excluded_numerical_paths": list(EXCLUDED_PATHS),
        "review_path": review_path.relative_to(directory).as_posix(),
        "review_sha256": sha256(review_path),
        "comparison_path": comparison_path.relative_to(directory).as_posix(),
        "comparison_sha256": sha256(comparison_path),
        "numerical_sha256": receipt["numerical_sha256"],
        "numerical_status": comparison["numerical_status"],
        "manual_validation_status": comparison["manual_validation_status"],
        "measurement_status": comparison["measurement_status"],
        "overall_status": comparison["status"],
        "full_analysis_hashes_preserved": True,
        "per_run_measurement_raw_and_source_guards_unchanged": True,
    }


def _attach_binding(directory, result, expected_sources):
    if _source_hashes() != expected_sources:
        raise IntegrationError("operator_source_changed", "Operator code changed during review")
    path = _binding_path(directory, result["review_path"])
    content = {**_binding_content(directory, result), "created_at": utc_now()}
    # Additive and exclusive: an existing review or companion is never replaced.
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(content, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return {**result, "operator_binding_path": str(path), "operator_binding_sha256": sha256(path),
            "operator_correction_id": CORRECTION_ID}


def verify_operator_binding(directory, result):
    """Read-only companion verification, also usable inside operator_context.

    ``result`` must come from ground_review.verify_review_receipt in that same
    context. This function does not replace its complete raw/source validation.
    """
    if result.get("status") != "pass":
        return result
    path = _binding_path(directory, result["review_path"])
    if not path.is_file():
        return {**result, "status": "unverified", "errors": ["Missing additive operator binding"]}
    try:
        binding = read_json(path)
        expected = _binding_content(directory, result)
        if (set(binding) != set(expected) | {"created_at"}
                or not isinstance(binding.get("created_at"), str)
                or any(binding.get(key) != value for key, value in expected.items())):
            raise IntegrationError("invalid_operator_binding", "Operator code, correction or evidence binding changed")
        return {**result, "operator_binding_path": str(path), "operator_binding_sha256": sha256(path),
                "operator_correction_id": CORRECTION_ID}
    except (OSError, ValueError, KeyError, TypeError, IntegrationError) as error:
        return {**result, "status": "fail", "errors": [str(error)]}


def review_session(directory, controls=None, *, source="human", reuse_ground_controls_from=None,
                   reuse_jump_controls_from=None):
    with operator_context(directory):
        sources = _source_hashes()
        result = ground_review.review_session(directory, controls, source=source,
            reuse_ground_controls_from=reuse_ground_controls_from,
            reuse_jump_controls_from=reuse_jump_controls_from)
        # The original transaction owns canonical state. If this additive write
        # fails, verification remains unverified; no acceptance is manufactured.
        return _attach_binding(directory, result, sources)


def verify_review_receipt(directory):
    """Verify the protocol, all legacy evidence checks, then operator binding."""
    try:
        with operator_context(directory):
            return verify_operator_binding(directory, ground_review.verify_review_receipt(directory))
    except (OSError, ValueError, KeyError, TypeError, IntegrationError) as error:
        return {"status": "fail", "errors": [str(error)]}


def build_report(directory):
    from .ground_reporting import generate_ground_report
    # Generating or verifying comparisons in nested v2 contexts would apply the
    # comparator twice. Keep the entire operation in this single context.
    with operator_context(directory):
        result = verify_operator_binding(directory, ground_review.verify_review_receipt(directory))
        if result["status"] == "fail" or (result["status"] == "unverified" and result.get("review_path")):
            raise IntegrationError("invalid_operator_binding", "; ".join(result.get("errors", [])))
        path = generate_ground_report(directory)
        return workflow._label_report(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["review", "verify-review", "report"])
    parser.add_argument("--session", required=True)
    parser.add_argument("--controls", type=Path)
    parser.add_argument("--source", choices=["human", "assistant_visual"], default="human")
    parser.add_argument("--reuse-ground-controls", type=Path, nargs="*")
    parser.add_argument("--reuse-jump-controls", type=Path)
    args = parser.parse_args(argv)
    directory = ROOT / "sessions" / args.session if Path(args.session).parent == Path(".") else Path(args.session)
    if args.action == "report":
        print(build_report(directory))
        return 0
    result = (verify_review_receipt(directory) if args.action == "verify-review" else
              review_session(directory, args.controls, source=args.source,
                  reuse_ground_controls_from=args.reuse_ground_controls,
                  reuse_jump_controls_from=args.reuse_jump_controls))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # A review that records a numerical failure still completed successfully.
    return int(args.action == "verify-review" and result["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
