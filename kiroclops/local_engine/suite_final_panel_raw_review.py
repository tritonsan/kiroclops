"""Final-only panel pixel review using the unchanged prospective v3 policy.

All reference, original, authoring-parent and selected records are required.
Only original event diagnostics may be excluded, after replaying the complete
current acceptance/preservation consumer with role-specific interventions.
The aid never certifies behavior, source authorship, or whole raw acceptance.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import statistics
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.visual import comparison
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from . import evidence, media_suite, suite_evidence, suite_final_raw_review as final
from . import suite_raw_review_v3 as v3
from .session import Session, _ordinary, source_hashes

VERSION = "final-panel-painted-geometry-raw-review-v1"
ANCHOR_POLICY = "first_palette_mixture_v1"
ROLES = ("reference", "original", "before", "selected")
CONTACT_POLICY = "control_and_event_v1"
PROOF_POLICY = "complete-final-panel-original-event-dependency-v1"
_need = final._need


def _code_sources():
    return final._code_sources() | v3._metric_inventory() | {
        str(Path(__file__).resolve()): file_sha256(__file__)}


def _contract(value):
    contract = v3.legacy._public_contract(value)
    public = (value or {}).get("event_options", value or {})
    for key, default in (("speed_threshold_px_s", 5.), ("overshoot_min_px", 1.5)):
        contract[key] = v3.finite(public.get(key, default), key, minimum=0)
        _need(contract[key] > 0, "Positive unchanged public event limits are required.")
    contract["axis"] = public.get("axis", "auto")
    _need(contract["axis"] in ("auto", "x", "y"), "Invalid public event axis.")
    return contract


def _matrix(context, rows):
    cases = context["cases"]
    _need(context.get("domain") == "panel" and context.get("kind") == "SealedSessionFinalRawMatrix",
          "A canonical sealed panel Session is required.")
    _need({case["role"] for case in cases} == {"calibration", "regression", "validation"},
          "All canonical calibration, regression and validation splits are mandatory.")
    _need(len({case["case_id"] for case in cases}) == len(cases), "Duplicate canonical case identity.")
    expected = {(role, case["case_id"]) for case in cases for role in ROLES}
    actual = {(row["role"], row["case_id"]) for row in rows}
    _need(len(rows) == len(expected) and actual == expected,
          "Review the exact complete four-role canonical matrix; no substituted or duplicate records.")
    by_id = {case["case_id"]: case for case in cases}
    for row in rows:
        case = by_id[row["case_id"]]
        _need(row.get("split_role") == case["role"] and row.get("purpose") == case.get("purpose", "primary")
              and row.get("stage") == case["stage"] and row.get("domain") == "panel",
              "Raw row split, stage or purpose differs from its canonical case.")


def _context_session(directory):
    directory = _ordinary(directory)
    state = Session(directory)._load()
    _need(state.get("domain") == "panel" and state.get("experiment", {}).get("reference_mode") == "media_suite",
          "Final panel review requires a canonical panel reference-suite Session.")
    suite_directory = _ordinary(state["reference"])
    suite = media_suite.load_suite(suite_directory)
    selected, layout = suite_evidence._matrix(suite, state)
    _need(selected is not None and selected.get("eligible") is True and selected.get("status") == "succeeded"
          and state.get("sealed_at") is not None and (state.get("final") or {}).get("status") == "succeeded",
          "Selected source and completed sealed final measurements are required.", "source_not_sealed")
    sealed = directory / "candidates" / media_suite._name(selected["candidate_id"]) / "source"
    hashes = source_hashes(sealed)
    _need(digest(hashes) == selected["source_sha256"] and digest(suite) == state["experiment"]["suite_sha256"],
          "Immutable selected source or canonical suite changed.", "source_changed")
    original = suite_directory / suite["original"]
    projects = {"original": original, "before": _ordinary(state["baseline"]), "selected": sealed}
    sources = {
        "original": suite_evidence._source_expectation(original, suite["original_source_hashes"], "panel"),
        "before": suite_evidence._source_expectation(projects["before"], suite["authoring_parent_source_hashes"], "panel"),
        "selected": suite_evidence._source_expectation(sealed, hashes, "panel")}
    rows = []
    for job_id, capture_role, cases in layout:
        role = "selected" if capture_role in ("candidate", "final") else capture_role
        for record, case in suite_evidence._records(directory / "measurements" / job_id / (capture_role + ".json"), cases, complete=True):
            row = {"id": final._label(role, case["case_id"], len(rows)), "case_id": case["case_id"],
                   "stage": case["stage"], "split_role": case["role"], "purpose": case.get("purpose", "primary"),
                   "domain": "panel", "role": role, "capture_role": capture_role, "reference": role == "reference",
                   "object_map": deepcopy(case.get("object_map") or record.get("intake_binding", {}).get("object_map") or {}),
                   "record": deepcopy(record)}
            # Public first-image selection is supplied separately, never learned
            # from produced coordinates, events, target code or DOM.
            selection = record.get("selection")
            if isinstance(selection, (str, Path)):
                selection = read_json(selection)
            _need(isinstance(selection, dict), "A separately bound public panel selection is required.")
            row["selection"] = deepcopy(selection)
            if not row["object_map"]:
                row["object_map"] = {spec["id"]: spec["id"] for spec in selection.get("objects", [])}
            if role in sources:
                row.update(expected_source=deepcopy(sources[role]), source_project=str(projects[role]))
            rows.append(row)
    context = {"kind": "SealedSessionFinalRawMatrix", "version": VERSION, "directory": str(directory),
               "suite": str(suite_directory), "suite_sha256": digest(suite), "domain": "panel", "stage": suite["stage"],
               "sealed_source": str(sealed), "sealed_source_hashes": hashes, "sealed_source_sha256": digest(hashes),
               "selected_candidate_id": selected["candidate_id"], "sealed_at": state["sealed_at"],
               "authoring_parent": str(projects["before"]), "authoring_parent_source_hashes": suite["authoring_parent_source_hashes"],
               "original": str(original), "original_source_hashes": suite["original_source_hashes"],
               "event_contract": deepcopy(suite.get("event_contract")), "cases": deepcopy(suite["cases"]),
               "expected_roles": list(ROLES), "boundary_receipt": None,
               "behavior_numerical_pass": (state.get("final") or {}).get("pass"), "heldout_status": suite["heldout_status"],
               "new_model_generation_claimed_by_aid": False}
    _matrix(context, rows)
    _contract(context["event_contract"])
    return context, rows


def _diagnostic_projection(value):
    """Retain every consumer field except explicit original-event diagnostics.

    The exclusion is structural and narrow. Parent-as-reference preservation,
    new pixel/event gates, all object/window values, inputs, functionality,
    order, dynamics and aggregate decisions remain in the equality check.
    """
    value = deepcopy(value)
    if isinstance(value, dict):
        original = value.get("original_pixel_comparison")
        if isinstance(original, dict):
            for key in ("event_checks", "preservation_checks", "errors", "success", "status"):
                original.pop(key, None)
        for key, item in list(value.items()):
            # Do not strip event diagnostics inside arbitrary before/candidate
            # observations: only outputs named original_pixel_comparison above.
            if key != "original_pixel_comparison":
                value[key] = _diagnostic_projection(item)
    elif isinstance(value, list):
        value = [_diagnostic_projection(item) for item in value]
    return value


def _case_metrics(records, case):
    reference, original, before, selected = (records[role] for role in ROLES)
    result = media_suite._compare(reference, original, selected, case["stage"], require_improvement=case.get("purpose", "primary") == "primary")
    result.update(case_id=case["case_id"], scenario_id=case["case_id"], role=case["role"], purpose=case.get("purpose", "primary"))
    preserved = media_suite._compare(before, before, selected, case["stage"], require_improvement=False)
    preserved.update(case_id=case["case_id"], role=case["role"], purpose="authoring_parent_preservation")
    prior = media_suite._parent_acceptance(reference, original, before, case["stage"])
    preserved["parent_reference_acceptance"] = prior
    preserved["previously_accepted_case"] = prior.get("pass") is True
    preserved["retained_reference_behavior"] = not preserved["previously_accepted_case"] or result.get("pass") is True
    return result, preserved


def _aggregate(context, cases, metrics, functional, *, final_job):
    comparisons = [metrics[case["case_id"]][0] for case in cases]
    preservation = [metrics[case["case_id"]][1] for case in cases]
    passed = all(row.get("pass") is True for row in comparisons)
    intact = (functional.get("status") == "pass"
              and all(row.get("retained_reference_behavior") is True for row in preservation)
              and all(row.get("pass") is True for row, case in zip(preservation, cases) if case.get("purpose") == "preservation"))
    scores = [row.get("score") for row in comparisons if row.get("score") is not None]
    result = {"eligible": passed and intact, "score": statistics.mean(scores) if len(scores) == len(comparisons) else None,
              "comparisons": comparisons, "preservation": functional, "parent_preservation": preservation,
              "suite_sha256": context["suite_sha256"], "independent_review_status": "pending", "source_review_status": "pending",
              "numerical_acceptance_only": True, "baseline_roles": {"improvement": "original", "preservation": "authoring_parent"}}
    if final_job:
        result.update(pass_=passed and intact and context["heldout_status"] == "provided",
                      groups={role: {"passed": sum(row.get("pass") is True for row in comparisons if row["role"] == role),
                                     "required": sum(row["role"] == role for row in comparisons)} for role in ("regression", "validation")},
                      heldout_status=context["heldout_status"])
        fresh = [row for row in comparisons if row["role"] == "validation" and row["purpose"] == "primary"]
        result.update(required=len(fresh), passed=sum(row.get("pass") is True for row in fresh))
        result["pass"] = result.pop("pass_")
    return result


def _record_identity(record):
    observed = record["observations"]
    return observed.get("package_sha256"), digest(observed)


def _intervention(records, mutation, audit):
    identities = {_record_identity(value["original"]) for value in records.values()}
    others = {_record_identity(value[role]) for value in records.values() for role in ("reference", "before", "selected")}
    _need(not identities & others, "Original and scored observations share an identity; role-specific intervention is unproven.", "metric_dependency_unproven")
    extract = comparison.extract_events

    def intervene(observed, *args, **kwargs):
        eventset = extract(observed, *args, **kwargs)
        identity = observed.get("package_sha256"), digest(observed)
        if identity not in identities:
            audit["passed_through"].add(identity)
            return eventset
        changed = deepcopy(eventset)
        if mutation == "absent":
            changed.update(events=[], relations=[], unobservable=[])
        elif mutation == "shifted":
            for event in changed.get("events", []):
                for key in ("time_interval_s", "peak_time_interval_s"):
                    if key in event:
                        event[key] = [stamp + 10. for stamp in event[key]]
            for relation in changed.get("relations", []):
                if "lag_interval_s" in relation:
                    relation["lag_interval_s"] = [stamp + 10. for stamp in relation["lag_interval_s"]]
        else:
            for event in changed.get("events", []):
                event.update(timing_status="unresolved", uncertainty_s=20.)
                if "time_interval_s" in event:
                    event["time_interval_s"] = [event["time_interval_s"][0] - 10., event["time_interval_s"][1] + 10.]
            for relation in changed.get("relations", []):
                if relation.get("type") == "relative_event_timing":
                    relation.update(timing_status="unresolved", uncertainty_s=20.)
        audit["intercepted"].add(identity)
        audit["count"] += 1
        return changed
    return intervene, identities


def prove_metric_dependencies(context, rows, *, inventory=None):
    """Replay the full actual selected calibration/final acceptance consumer.

    Every existing result field is reproduced except media exports. Stored and
    comparison-derived original events are separately deleted/shifted/made
    unresolved, while all observations, inputs and scored roles stay fixed.
    Before events remain mandatory because they feed parent preservation.
    """
    inventory = inventory if inventory is not None else {}
    directory = _ordinary(context["directory"])
    state = Session(directory)._load()
    selected = next(row for row in state["candidates"] if row["candidate_id"] == context["selected_candidate_id"])
    records = defaultdict(dict)
    for row in rows:
        record = v3.legacy._record(row, len(records))
        observed, events = v3.legacy._measurement(record, inventory)
        records[row["case_id"]][row["role"]] = {**record, "observations": observed, "events": events}
    _need(all(set(value) == set(ROLES) for value in records.values()), "Metric replay omits a source role.")
    jobs = []
    for job_id, operation, cases in (
        (selected["job_id"], "evaluate", [c for c in context["cases"] if c["role"] == "calibration"]),
        (state["final_job_id"], "finalize", [c for c in context["cases"] if c["role"] != "calibration"])):
        saved = evidence._completed_job(directory, state, job_id, operation, selected)
        functional_path = directory / "measurements" / job_id / "functional.json"
        functional = read_json(functional_path)
        final._file(functional_path, inventory)
        # Any added acceptance field must receive an explicit complete replay.
        metadata = {"candidate_media"} if operation == "evaluate" else set()
        expected = {key: value for key, value in saved.items() if key not in metadata}
        jobs.append((job_id, operation, cases, functional, expected))

    def replay(values):
        metrics = {case["case_id"]: _case_metrics(values[case["case_id"]], case) for case in context["cases"]}
        return {job_id: _aggregate(context, cases, metrics, functional, final_job=operation == "finalize")
                for job_id, operation, cases, functional, _ in jobs}

    expected = {job_id: saved for job_id, _, _, _, saved in jobs}
    baseline = replay(records)
    _need(_diagnostic_projection(baseline) == _diagnostic_projection(expected),
          "The complete current comparator/preservation/aggregate replay differs from the saved canonical jobs.", "metric_dependency_unproven")
    observations_before = digest({case: {role: value["observations"] for role, value in roles.items()} for case, roles in records.items()})
    trials = []
    for mode in ("delete_stored", "empty_stored", "unresolved_stored", "absent", "shifted", "unresolved"):
        values = deepcopy(records)
        audit = {"intercepted": set(), "passed_through": set(), "count": 0}
        if mode.endswith("stored"):
            for roles in values.values():
                if mode == "delete_stored":
                    roles["original"].pop("events", None)
                elif mode == "empty_stored":
                    roles["original"]["events"] = {}
                else:
                    roles["original"]["events"] = {"events": [{"type": "motion_start", "object_ids": ["bookmark"],
                        "time_interval_s": [-10000., 10000.], "timing_status": "unresolved"}], "relations": []}
            changed = replay(values)
        else:
            intervention, identities = _intervention(values, mode, audit)
            with patch.object(comparison, "extract_events", side_effect=intervention):
                changed = replay(values)
            _need(audit["intercepted"] == identities and audit["count"] > 0,
                  "Every actual original observation must be intercepted by each derived-event mutation.", "metric_dependency_unproven")
        unchanged = _diagnostic_projection(changed) == _diagnostic_projection(baseline)
        trials.append({"mutation": mode, "complete_scored_projection_identical": unchanged,
                       "scored_projection_sha256": digest(_diagnostic_projection(changed)),
                       "whole_output_identical": changed == baseline, "intervention_count": audit["count"],
                       "intercepted_identities": sorted(audit["intercepted"], key=str),
                       "other_observation_identities_passed_through": sorted(audit["passed_through"], key=str)})
    unchanged_observations = digest({case: {role: value["observations"] for role, value in roles.items()} for case, roles in records.items()}) == observations_before
    return {"kind": "CompleteFinalPanelMetricDependencyProof", "policy": PROOF_POLICY,
            "status": "pass" if unchanged_observations and all(trial["complete_scored_projection_identical"] for trial in trials) else "fail",
            "case_ids": sorted(records), "jobs": [job_id for job_id, *_ in jobs], "metric_sources": _code_sources(),
            "saved_job_scored_projection_sha256": digest(_diagnostic_projection(expected)),
            "replayed_job_scored_projection_sha256": digest(_diagnostic_projection(baseline)),
            "observations_and_inputs_unchanged": unchanged_observations, "trials": trials,
            "before_events_diagnostic_only": False, "before_events_scope": "Mandatory parent-as-reference preservation and prior reference acceptance.",
            "scope": "All fields from both canonical job results except presentation exports and explicitly isolated original event diagnostic fields; no future metric claim."}


def _partition_failures(failures, bindings, proof):
    originals = {row["record_id"] for row in bindings if row["role"] == "original"}
    mandatory, diagnostics = [], []
    qualified = proof.get("status") == "pass"
    for failure in failures:
        destination = (diagnostics if qualified and failure.get("record_id") in originals
                       and failure.get("code") in v3.ORIGINAL_EVENT_DIAGNOSTICS else mandatory)
        destination.append(deepcopy(failure))
    if not qualified:
        mandatory.append({"code": "complete_metric_dependency_unproven", "detail": proof.get("error", "Current full acceptance/preservation consumer was not proven independent.")})
    return mandatory, diagnostics


def _geometry_controls(bundles, contact):
    lookup = {bundle["record"]["id"]: bundle for bundle in bundles}
    identities = defaultdict(set)
    for item in contact["controls"]:
        bundle = lookup[item["record_id"]]
        glyph = bundle["mapping"][item["object_id"]]
        point = bundle["raw"]["rows"][item["index"]]["points"][glyph]
        if "geometry_bounds" in point:
            identities[item["role"], item["object_id"]].add(item["image_sha256"])
    expected = {(bundle["record"]["role"], oid) for bundle in bundles for oid in bundle["mapping"]}
    values = [{"role": role, "object_id": oid, "distinct_geometry_control_images": len(identities[role, oid]),
               "minimum_distinct_images": 20, "pass": len(identities[role, oid]) >= 20} for role, oid in sorted(expected)]
    return values, [{"code": "insufficient_raw_geometry_controls", **value} for value in values if not value["pass"]]


def _bindings(bundle):
    record = bundle["record"]
    return {"record_id": record["id"], "case_id": record["case_id"], "role": record["role"],
            "capture_role": record["capture_role"], "stage": record["stage"], "split_role": record["split_role"],
            "purpose": record["purpose"], "package_sha256": bundle["manifest"]["package_sha256"],
            "observations_sha256": digest(bundle["observations"]), "events_sha256": digest(bundle["event_set"]),
            "source_sha256": record.get("source_sha256"), "object_map": bundle["mapping"],
            "public_active_intervals_s": bundle["active"], "public_selection_sha256": bundle["raw"]["public_selection_sha256"]}


def _review(context, rows, output):
    output = _ordinary(output)
    _need(not output.exists(), "Preserve all earlier raw review evidence.", "output_exists")
    normalized = [v3.legacy._record(row, number) for number, row in enumerate(rows)]
    _matrix(context, normalized)
    _need(len({row["id"] for row in normalized}) == len(rows), "Duplicate recording identity.")
    for row in normalized:
        _need(isinstance(row.get("selection"), dict), "The separately declared public selection is mandatory.")
        _need(not output.is_relative_to(row["package"]) and not row["package"].is_relative_to(output),
              "Raw review output overlaps an immutable input package.", "unsafe_path")
    for key in ("suite", "sealed_source", "authoring_parent", "original", "directory"):
        _need(not output.is_relative_to(_ordinary(context[key])), "Raw output cannot be inside a canonical source/session.", "unsafe_path")
    contract = _contract(context["event_contract"])
    sources, inventory = _code_sources(), {}
    output.mkdir(parents=True)
    write_json(output / "matrix.json", context)
    policy = {"kind": "FrozenFinalPanelRawPolicy", "anchor_policy": ANCHOR_POLICY,
              "anchor_definition": v3._anchor_definition(ANCHOR_POLICY), "public_event_contract": contract,
              "selected_before_pixel_extraction": True, "production_coordinates_or_event_times_used_for_selection": False,
              "geometry": "Unchanged v3 painted body contour and fixed public first-image glyph padding."}
    write_json(output / "policy-selection.json", policy)
    # Exactly the v3 image-only extractor. Finish every pixel before opening
    # measurements, canonical consumer results or provenance checks.
    bundles = [v3._extract(row, output, inventory, CONTACT_POLICY, ANCHOR_POLICY) for row in normalized]
    write_json(output / "extraction-complete.json", {
        "all_pixel_extraction_finished_before_measurement_read": True,
        "production_coordinates_or_events_used": False,
        "raw_point_files": {bundle["record"]["id"]: file_sha256(output / "raw" / bundle["record"]["id"] / "points.json") for bundle in bundles}})
    producer = final._after_raw(context, rows, inventory)
    failures, event_results, bindings = [], [], []
    expected_objects = {}
    glyphs = {}
    for bundle in bundles:
        observed, events = v3.legacy._measurement(bundle["record"], inventory)
        failures.extend(v3.legacy._scope(bundle, observed, events, contract))
        options = events.get("options", {})
        if (any(abs(v3.finite(options.get(key), key) - contract[key]) > 1e-9 for key in ("speed_threshold_px_s", "overshoot_min_px"))
                or options.get("axis", "auto") != contract["axis"]):
            failures.append({"code": "event_definition_mismatch", "record_id": bundle["record"]["id"]})
        for oid, glyph in bundle["mapping"].items():
            _need(oid not in glyphs or glyphs[oid] == glyph, "Physical object/glyph identity changed across final sources.")
            glyphs[oid] = glyph
        if bundle["record"]["role"] == "reference":
            expected_objects[bundle["record"]["case_id"]] = set(bundle["mapping"])
        write_json(output / "measurements" / bundle["record"]["id"] / "measurement.json", {"observations": observed, "events": events})
        compared, errors = v3.v2._compare_events(bundle, contract)
        event_results.append(compared)
        failures.extend(errors)
        bindings.append(_bindings(bundle))
    for bundle in bundles:
        if set(bundle["mapping"]) != expected_objects.get(bundle["record"]["case_id"]):
            failures.append({"code": "case_object_set_mismatch", "record_id": bundle["record"]["id"]})
    positions, errors = v3.legacy._compare_positions(bundles)
    failures.extend(errors)
    geometry, errors = v3._compare_geometry(bundles)
    failures.extend(errors)
    contact = v3._compact_sheets(bundles, event_results, output)
    geometry_controls, errors = _geometry_controls(bundles, contact)
    failures.extend(errors)
    write_json(output / "contact-selection.json", contact)
    try:
        proof = prove_metric_dependencies(context, rows, inventory=inventory)
    except (OSError, VisualError, KeyError, TypeError, ValueError, StopIteration) as exc:
        proof = {"kind": "CompleteFinalPanelMetricDependencyProof", "policy": PROOF_POLICY, "status": "incomplete",
                 "error": str(exc), "metric_sources": sources, "before_events_diagnostic_only": False}
    mandatory, diagnostics = _partition_failures(failures, bindings, proof)
    _need(sources == _code_sources(), "Raw policy or metric sources changed during review.", "source_changed")
    for path, sha in inventory.items():
        _need(file_sha256(_ordinary(path)) == sha, "Bound final pixel/source/job evidence changed during review.", "source_changed")
    result = {"kind": "FinalOnlyUnsignedPanelReviewAid", "schema_version": 1, "version": VERSION,
              "domain": "panel", "seal_kind": context["kind"], "status": "incomplete_metric_dependency" if proof.get("status") == "incomplete" else "pending_visual_review",
              "numeric_status": "incomplete" if proof.get("status") == "incomplete" else "pass" if not mandatory else "fail", "whole_raw_numeric_status": "pass" if not failures else "fail",
              "matrix_sha256": digest(context), "suite_sha256": context["suite_sha256"],
              "selected_candidate_id": context["selected_candidate_id"], "sealed_source_sha256": context["sealed_source_sha256"],
              "records": bindings, "positions": positions, "geometry": geometry, "geometry_controls": geometry_controls,
              "events": event_results, "public_event_contract": contract, "public_event_contract_sha256": digest(contract),
              "anchor_policy": ANCHOR_POLICY, "anchor_policy_definition_sha256": digest(v3._anchor_definition(ANCHOR_POLICY)),
              "policy_selection_sha256": digest(policy), "contact_policy": CONTACT_POLICY, "contact_selection_sha256": digest(contact),
              "sheets": [{"path": name, "sha256": file_sha256(output / name)} for bundle in bundles for name in bundle["sheets"]],
              "failures": mandatory, "all_raw_failures": failures, "unqualified_original_event_diagnostics": diagnostics,
              "original_event_measurements_qualified": False, "before_events_diagnostic_only": False,
              "whole_raw_review_accepted": False, "metric_dependency_proof": proof, "producer_binding": producer,
              "source_code_review_status": "pending", "native_acceptance_review_status": "pending",
              "native_generation_provenance_status": "pass", "behavior_numerical_pass": context["behavior_numerical_pass"],
              "heldout_status": context["heldout_status"], "unsupported": [], "acceptance_granted": False,
              "full_behavior_acceptance": False, "visual_inspection_claimed": False,
              "production_coordinates_used_for_extraction": False, "smoothing_or_interpolation_used": False,
              "production_events_used_for_contact_selection": False, "new_model_generation_claimed_by_aid": False,
              "historical_acceptance_changed": False, "historical_original_event_failures_superseded": False,
              "limitations": ["Every bound contact image must be actually inspected before any attestation.",
                  "Every position/body geometry gate remains mandatory for all four roles; scored reference/selected and parent preservation events remain mandatory.",
                  "Original event diagnostics remain unqualified only after the exact complete current metric dependency proof passes.",
                  "Historical original-event failures, including the earlier 41 diagnostics, remain separate and unchanged.",
                  "Native provenance binding is separate from source/native acceptance review; this aid grants neither full behavior nor whole raw acceptance."]}
    write_json(output / "result.json", result)
    receipt = {"kind": "UnsignedRawReviewEvidenceReceipt", "result_sha256": digest(result), "matrix_sha256": digest(context),
               "source_files": inventory, "code_sources": sources,
               "generated_files": {path.relative_to(output).as_posix(): file_sha256(path) for path in output.rglob("*") if path.is_file()},
               "metric_dependency_proof_sha256": digest(proof), "measurement_bindings_sha256": digest(bindings),
               "policy_selection_sha256": digest(policy), "runtime_libraries": {"opencv": cv2.__version__, "numpy": np.__version__}}
    write_json(output / "evidence-receipt.json", receipt)
    return {**result, "output": str(output)}


def review_session(directory, output):
    """Create a new unsigned complete final panel aid; no automatic inspection."""
    return _review(*_context_session(directory), output)


def _verified_proposal(output):
    output = _ordinary(output)
    result, receipt = (read_json(output / name) for name in ("result.json", "evidence-receipt.json"))
    policy = read_json(output / "policy-selection.json")
    _need(result.get("kind") == "FinalOnlyUnsignedPanelReviewAid" and result.get("version") == VERSION
          and receipt.get("result_sha256") == digest(result) and receipt.get("matrix_sha256") == result.get("matrix_sha256")
          and receipt.get("measurement_bindings_sha256") == digest(result.get("records"))
          and receipt.get("metric_dependency_proof_sha256") == digest(result.get("metric_dependency_proof")),
          "Final panel proposal or its exact bindings changed.", "stale_review")
    _need(result.get("anchor_policy") == policy.get("anchor_policy") == ANCHOR_POLICY
          and policy.get("anchor_definition") == v3._anchor_definition(ANCHOR_POLICY)
          and result.get("anchor_policy_definition_sha256") == digest(v3._anchor_definition(ANCHOR_POLICY))
          and result.get("public_event_contract_sha256") == digest(result.get("public_event_contract"))
          and policy.get("public_event_contract") == result.get("public_event_contract")
          and result.get("policy_selection_sha256") == receipt.get("policy_selection_sha256") == digest(policy),
          "The frozen v3 anchor, geometry or public event policy changed.", "stale_review")
    _need(receipt.get("code_sources") == _code_sources(), "Current complete raw/metric code closure changed.", "stale_review")
    _need(receipt.get("runtime_libraries") == {"opencv": cv2.__version__, "numpy": np.__version__}, "Runtime pixel-library identity changed.", "stale_review")
    for path, sha in {**receipt["source_files"], **receipt["code_sources"]}.items():
        _need(file_sha256(_ordinary(path)) == sha, "Bound source, pixel, metric or provenance receipt changed.", "stale_review")
    for path, sha in receipt["generated_files"].items():
        _need(file_sha256(safe_path(output, path)) == sha, "Final raw proposals/contact sheets changed.", "stale_review")
    return output, result, receipt


def attest_review(output, *, reviewer, inspected_sheets, accepted, images_inspected=False):
    """Record the explicit real inspector of ALL sheets, never infer inspection."""
    _need(isinstance(reviewer, str) and reviewer.strip() and images_inspected is True and type(accepted) is bool,
          "A named actual inspector, real image inspection and boolean decision are required.", "invalid_review")
    output, result, _ = _verified_proposal(output)
    _need(not (output / "attestation.json").exists(), "Preserve every earlier inspection decision.", "output_exists")
    _need(isinstance(inspected_sheets, (list, tuple)) and len(set(inspected_sheets)) == len(inspected_sheets)
          and set(inspected_sheets) == {row["path"] for row in result["sheets"]}, "Actually inspect every bound contact sheet.", "incomplete_visual_review")
    if accepted:
        _need(result["numeric_status"] == "pass" and not result["failures"] and not result["unsupported"]
              and result["sheets"] and result["metric_dependency_proof"]["status"] == "pass",
              "Inspection cannot waive any scored measurement, preservation gate or incomplete dependency proof.", "review_not_qualified")
    value = {"kind": "FinalPanelRawVisualReviewAttestation", "status": "pass" if accepted else "fail",
             "reviewer": reviewer.strip(), "images_inspected": True, "accepted": accepted,
             "method": "explicit_all_final_panel_raw_contact_sheet_inspection",
             "result_sha256": digest(result), "evidence_receipt_sha256": file_sha256(output / "evidence-receipt.json"),
             "inspected_sheets": result["sheets"], "metric_dependency_proof_sha256": digest(result["metric_dependency_proof"]),
             "whole_raw_review_accepted": False, "original_event_measurements_qualified": False,
             "behavior_acceptance_granted": False, "digital_signature": False}
    import json
    with (output / "attestation.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return value


def validate_review(output, *, session_directory):
    """Replay the full final binding after a real independent sheet decision."""
    context, rows = _context_session(session_directory)
    output, result, receipt = _verified_proposal(output)
    attestation = read_json(output / "attestation.json")
    _need(result.get("matrix_sha256") == digest(context) and result.get("numeric_status") == "pass"
          and not result.get("failures") and result.get("sheets") and not result.get("unsupported")
          and result.get("public_event_contract") == _contract(context["event_contract"])
          and result.get("anchor_policy") == ANCHOR_POLICY and result.get("contact_policy") == CONTACT_POLICY
          and result.get("original_event_measurements_qualified") is False and result.get("before_events_diagnostic_only") is False
          and result.get("whole_raw_review_accepted") is False and result.get("full_behavior_acceptance") is False
          and attestation.get("kind") == "FinalPanelRawVisualReviewAttestation" and attestation.get("status") == "pass"
          and attestation.get("accepted") is True and attestation.get("images_inspected") is True
          and attestation.get("result_sha256") == digest(result) and attestation.get("inspected_sheets") == result["sheets"]
          and attestation.get("evidence_receipt_sha256") == file_sha256(output / "evidence-receipt.json")
          and attestation.get("metric_dependency_proof_sha256") == receipt["metric_dependency_proof_sha256"],
          "Final panel inspection is incomplete, changed or bound to another canonical Session.")
    producer = final._after_raw(context, rows, {})
    current = []
    for index, row in enumerate(rows):
        record = v3.legacy._record(row, index)
        manifest = load_package(record["package"], verify="hashes")
        observed, events = v3.legacy._measurement(record, {})
        raw = read_json(output / "raw" / record["id"] / "points.json")
        bundle = {"record": record, "manifest": manifest, "raw": raw}
        errors = v3.legacy._scope(bundle, observed, events, _contract(context["event_contract"]))
        _need(not errors, "Current public position/event scope changed.")
        current.append(_bindings(bundle))
    _need(current == result["records"], "Attested role/case/source/selection/event identities differ from the current complete seal.")
    proof = prove_metric_dependencies(context, rows)
    _need(proof == result["metric_dependency_proof"] and proof.get("status") == "pass", "Full current acceptance/preservation dependency proof changed.")
    actual = {(row["package_sha256"], row["observations_sha256"]) for row in current}
    _need(actual == {tuple(pair) for pair in producer["required_observations"]} and len(current) == len(rows), "Required current matrix observations are omitted.")
    return {"kind": "VerifiedFinalRawMeasurementInspection", "status": "pass", "directory": str(output),
            "attestation_sha256": file_sha256(output / "attestation.json"), "result_sha256": digest(result),
            "matrix_sha256": digest(context), "sealed_source_sha256": context["sealed_source_sha256"],
            "required_observations": producer["required_observations"], "record_count": len(rows),
            "source_code_review_status": "pending", "native_acceptance_review_status": "pending",
            "whole_raw_review_accepted": False, "original_event_measurements_qualified": False,
            "full_behavior_acceptance": False}
