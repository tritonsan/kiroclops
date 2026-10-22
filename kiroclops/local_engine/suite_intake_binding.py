"""Bind selected, frozen user clips to an executable browser reference suite.

The operator names both source roles. Only supplied input timelines become
capture recipes; uploaded pixels never acquire recorder or blind-test authority.
Creating this suite measures references, but starts no application or model.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import statistics

from kiroclops.visual.package import copy_package, load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from .session import _lock, _ordinary, source_hashes

VERSION = "user-intake-suite-v1"


def _load_intake(root):
    from . import suite_intake
    root = _ordinary(root)
    with _lock(root / ".suite.lock", timeout=5):
        root, draft, state = suite_intake._load(root)
        current = suite_intake._coverage(root, draft, state)
        if current["missing_behaviors"] or not (root / "suite.json").is_file():
            return root, None, current, []
        suite = suite_intake._frozen(root)
        expected = {"kind": "MultiReferenceIntakeSuite", "schema_version": 1,
                    "status": "suite_ready_intake", "frozen": True, "execution_ready": False,
                    "model_started": False, "behavior_accepted": False, "benchmark_qualified": False,
                    "suite_id": draft["suite_id"], "domain": draft["domain"], "goal": draft["goal"],
                    "required_behaviors": draft["required_behaviors"], "draft_sha256": digest(draft),
                    "state_sha256": digest(state), "clips": state["clips"],
                    "object_map": state["object_map"], "target_identity": state["target_identity"],
                    "coverage": current}
        if suite != expected or current["model_started"]:
            raise VisualError("suite_changed", "Frozen intake no longer matches its bound draft, clips or target.")
        clips = [suite_intake._verify_clip(root, row) for row in suite["clips"]]
        return root, suite, current, clips


def _binding(manifest, selection, mapping, project, case_id, role, options):
    """Use the existing single-video binding validator, with no inferred inputs."""
    from . import media_session
    from .capture_modes import LEGACY_DRIVER, reference_capture_policy
    from kiroclops.web_local.specs import TIMING_LIMITS, validate_scenario
    provenance = manifest["inputs"]["provenance"]
    if provenance == "unknown" or not manifest["inputs"]["events"]:
        raise VisualError("needs_input_binding", "State the interaction times in the video explicitly.")
    from .suite_intake import _mapping
    mapping = _mapping(mapping, selection, project["domain"])
    times = [f["time_s"] for f in manifest["frames"]]
    events = [{**{k: deepcopy(e[k]) for k in ("action", "selector", "destination", "fraction") if k in e},
               "time_s": e["time_s"] - times[0]} for e in manifest["inputs"]["events"]]
    if any(not times[0] <= e["time_s"] < times[-1] for e in manifest["inputs"]["events"]):
        raise VisualError("invalid_input_timeline", "Every input must lie inside the actual supplied clip.")
    scenario = {"id": case_id, "recipe": "user-bound-reference", "domain": "panel" if project["domain"] == "web_panel" else "svg",
                "split": "calibration", "repeat": 1, "viewport": list(manifest["image_size"]), "device_scale_factor": 1,
                "duration_s": times[-1] - times[0] + statistics.median(b-a for a, b in zip(times, times[1:])),
                "events": events, "timing_limits": dict(TIMING_LIMITS), "version": "uploaded-media-v1"}
    policy = reference_capture_policy(project["domain"], manifest)
    if policy["frame_driver"] != LEGACY_DRIVER:
        scenario.update(policy)
    if scenario["domain"] == "svg":
        count = options.get("initial_node_count")
        if type(count) is not int or not 3 <= count <= 6:
            raise VisualError("needs_input_binding", "Declare the initial SVG node count (3–6) for each clip.")
        selected = options.get("expected_selected_id")
        if selected is not None and selected not in set("abcdef"):
            raise VisualError("invalid_binding", "Expected selected SVG node must be an explicit adapter identity a–f.")
        scenario.update(initial_node_count=count, expected_final_node_count=count + sum(
            1 if e.get("selector") == "#add-node" else -1 if e.get("selector") == "#remove-node" else 0
            for e in events if e["action"] == "click"), expected_selected_id=selected)
    elif any(k in options for k in ("initial_node_count", "expected_selected_id")):
        raise VisualError("invalid_binding", "SVG state options do not apply to a panel clip.")
    inverse = {target: ref for ref, target in mapping.items()}
    relative = [{"object_id": inverse[child], "relative_to": inverse["handle"]}
                for child in ("bookmark", "star") if child in inverse and "handle" in inverse] if scenario["domain"] == "panel" else []
    binding = {"kind": "MediaCaptureBinding", "schema_version": 2, "preset": "recorded-timeline",
               "reference_package_sha256": manifest["package_sha256"], "selection_sha256": digest(selection),
               "object_map": mapping, "scenario": scenario, "relative_motion": relative,
               "input_provenance": provenance, "spatial_alignment": media_session._spatial_policy(
                   options.get("spatial_alignment", media_session.VIEWPORT_ALIGNMENT), set(mapping)),
               "claim": "conditional_on_user_declared_inputs" if provenance == "user_declared" else "recorded_input_response",
               "status": "bound", "heldout_status": "not_provided", "model_started": False}
    if scenario["domain"] == "svg":
        from .evaluation import _lifecycle_bindings
        binding.update(connections=deepcopy(selection.get("connections", [])), lifecycle_bindings=_lifecycle_bindings(selection, manifest))
    # The established validator checks selector support, complete graph mapping,
    # clock policy, spatial units and exact source input identities/times.
    media_session._validate_binding(binding, project, manifest, selection)
    scenario.update(case_id=case_id, split=role, suite_split=role, suite_version=VERSION,
                    blind_validation=False, validation_basis="user_designated_distinct_pixels" if role == "validation" else "not_provided")
    try:
        validate_scenario(scenario)
    except (ValueError, KeyError, TypeError) as error:
        raise VisualError("invalid_binding", str(error)) from error
    return binding


def _selection(selection, binding, manifest):
    from .evaluation import _lifecycle_bindings
    value = deepcopy(selection)
    value["relative_motion"] = deepcopy(binding["relative_motion"])
    value["lifecycle_bindings"] = deepcopy(binding.get("lifecycle_bindings", _lifecycle_bindings(selection, manifest)))
    value["input_origin_s"] = manifest["inputs"]["events"][0]["time_s"]
    return value


def _measured(record, selection, output, check, progress, *, candidate_sha256):
    from . import evaluation, suite_web
    contract = suite_web.event_contract(record["scenario"]["domain"])
    output = Path(output)
    write_json(output / "selection.json", selection)
    measured = evaluation._analysis(record["package"], selection, output, check, progress,
                                   contract=contract, candidate_sha256=candidate_sha256)
    return {**record, "selection": selection, "analysis": str(output / "analysis.json"),
            "measurement_status": measured["measurement_status"], "observations": measured["observations"], "events": measured["events"],
            "suite_event_contract": deepcopy(contract), "independent_review_contract": deepcopy(contract), "event_contract_sha256": digest(contract)}


def bind_intake(intake_suite, output, *, original, authoring_parent, case_options=None,
                check=lambda: None, progress=lambda *args: None):
    """Measure only the frozen supplied clips, then freeze explicit source roles.

    case_options maps existing scenario IDs to SVG state/spatial bindings. It
    cannot add cases, change roles, remap objects, or promote timeline authority.
    No target capture, session creation or model invocation occurs here.
    """
    from . import media_suite, suite_intake, suite_web
    from .intake import detect_project
    check()
    root, intake, current, clips = _load_intake(intake_suite)
    if intake is None:
        return {**current, "status": "needs_reference", "reason": "Complete and freeze the local intake first.",
                "execution_ready": False, "model_started": False, "behavior_accepted": False}
    if intake["domain"] not in ("web_panel", "svg"):
        raise VisualError("unsupported_media_adapter", "This bridge supports the panel and SVG browser adapters.")
    output = _ordinary(output)
    preparation = output.with_name(output.name + "-intake-preparation")
    original, authoring_parent = _ordinary(original), _ordinary(authoring_parent)
    for destination in (output, preparation):
        if destination.exists():
            raise VisualError("output_exists", "Use a fresh output; earlier immutable evidence is preserved.")
        for source in (root, original, authoring_parent):
            if destination.is_relative_to(source) or source.is_relative_to(destination):
                raise VisualError("unsafe_path", "Output must be separate from intake and both explicit source roles.")
    parent_info, original_info = detect_project(authoring_parent), detect_project(original)
    parent_hashes, original_hashes = source_hashes(authoring_parent), source_hashes(original)
    if suite_intake._target(parent_info) != intake["target_identity"]:
        raise VisualError("wrong_authoring_parent", "Authoring parent must be the unchanged target explicitly bound during intake.")
    if original_info["domain"] != intake["domain"] or original_info["entry"] != parent_info["entry"]:
        raise VisualError("wrong_baseline", "Original comparison source must use the same supported adapter.")
    options = deepcopy(case_options or {})
    ids = {c["scenario_id"] for c in clips}
    if not isinstance(options, dict) or not set(options) <= ids:
        raise VisualError("invalid_binding", "State options must name existing frozen clips only.")
    calibration = {b for c in clips if c["role"] == "calibration" for b in c["behaviors"]}
    missing = [b for b in intake["required_behaviors"] if b not in calibration]
    if missing:
        return {"status": "needs_reference", "missing_calibration_behaviors": missing,
                "requests": [suite_intake.MISSING_REQUESTS[b] for b in missing], "execution_ready": False,
                "model_started": False, "behavior_accepted": False, "inferred_inputs": False}
    planned = []
    for clip in clips:
        check()
        name = clip["scenario_id"]
        manifest = load_package(root / "clips" / name / "package", verify="hashes")
        selection = read_json(root / "clips" / name / "selection.json")
        if clip["object_map"] != intake["object_map"] or clip["target_identity"] != intake["target_identity"]:
            raise VisualError("binding_changed", "Frozen clips must keep the common object map and target source.")
        option = options.get(name, {})
        if not isinstance(option, dict) or set(option) - {"initial_node_count", "expected_selected_id", "spatial_alignment"}:
            raise VisualError("invalid_binding", "Only explicit SVG state and spatial alignment options are supported.")
        try:
            binding = _binding(manifest, selection, clip["object_map"], parent_info, name, clip["role"], option)
        except VisualError as error:
            if error.code in ("needs_input_binding", "needs_geometry_binding", "needs_connection_binding", "needs_spatial_binding"):
                return {"status": error.code, "case_id": name, "request": str(error), "execution_ready": False,
                        "model_started": False, "inferred_inputs": False, "behavior_accepted": False}
            raise
        planned.append((clip, manifest, selection, binding))
    records = []
    for clip, manifest, selection, binding in planned:
        check()
        name = clip["scenario_id"]
        directory = preparation / name
        copy_package(root / "clips" / name / "package", directory / "package")
        identity = {"version": VERSION, "intake_suite_sha256": digest(intake), "clip_sha256": digest(clip),
                    "package_sha256": manifest["package_sha256"], "pixels_sha256": clip["pixels_sha256"],
                    "selection_sha256": digest(selection), "inputs_sha256": digest(manifest["inputs"]),
                    "object_map": deepcopy(clip["object_map"]), "behaviors": list(clip["behaviors"]), "role": clip["role"],
                    "input_provenance": manifest["inputs"]["provenance"], "target_identity": deepcopy(intake["target_identity"]),
                    "adapter_actions": deepcopy(parent_info["recommended_capture_actions"]),
                    "original_source_sha256": digest(original_hashes), "authoring_parent_source_sha256": digest(parent_hashes)}
        record = {"scenario": deepcopy(binding["scenario"]), "package": str(directory / "package"),
                  "functional": {"status": "not_observed_reference_video"}, "reference_kind": "uploaded_pixels",
                  "timing": {"status": "source_timestamps", "input_provenance": manifest["inputs"]["provenance"]},
                  "source_sha256": digest({"kind": "uploaded_pixels", "package_sha256": manifest["package_sha256"], "pixels_sha256": clip["pixels_sha256"]}),
                  "intake_binding": deepcopy(binding), "intake_provenance": identity,
                  "intake_source_selection": deepcopy(selection), "model_started": False}
        record = _measured(record, _selection(selection, binding, manifest), directory / "measurement", check, progress, candidate_sha256=None)
        record["intake_observations_sha256"] = digest(record["observations"])
        verify_reference_record(record)
        records.append({"case_id": name, "role": clip["role"], "object_map": clip["object_map"], "record": record})
    # Copying/measurement must not publish a source or intake race as success.
    if source_hashes(original) != original_hashes or source_hashes(authoring_parent) != parent_hashes or _load_intake(root)[1] != intake:
        raise VisualError("source_changed", "Sources or frozen intake changed during reference preparation.")
    required = [{"case_id": c["scenario_id"], "role": c["role"]} for c in clips]
    declaration = {"adapter": VERSION, "intake_suite_sha256": digest(intake), "required_behaviors": intake["required_behaviors"],
                   "calibration_behaviors": sorted(calibration), "validation_basis": "user_designated_distinct_pixels",
                   "timeline_authority": sorted({c["input_provenance"] for c in clips}),
                   "full_behavior_acceptance": False, "benchmark_qualified": False, "model_started": False,
                   "source_roles": {"original": digest(original_hashes), "authoring_parent": digest(parent_hashes)}}
    suite = media_suite.create_suite(output, domain=intake["domain"], stage="panel" if intake["domain"] == "web_panel" else "svg",
        records=records, required_cases=required, original=original, authoring_parent=authoring_parent, goal=intake["goal"],
        event_contract=suite_web.event_contract("panel" if intake["domain"] == "web_panel" else "svg"), preservation={"intake_bridge": declaration})
    return {"status": "suite_bound", "output": str(output), "suite_sha256": digest(suite), "execution_ready": True,
            "heldout_status": suite["heldout_status"], "validation_basis": "user_designated_distinct_pixels" if suite["heldout_status"] == "provided" else "not_provided",
            "case_count": len(records), "timeline_authority": declaration["timeline_authority"],
            "behavior_accepted": False, "benchmark_qualified": False, "model_started": False}


def verify_reference_record(record):
    """Recheck imported pixels, binding, observations and shared worker identity."""
    from . import evidence, media_session, suite_web
    manifest = load_package(record["package"], verify="hashes")
    binding, identity = record["intake_binding"], record["intake_provenance"]
    selection = record["intake_source_selection"]
    from .suite_intake import _pixels
    if (identity.get("version") != VERSION or binding.get("scenario") != record.get("scenario")
            or manifest["package_sha256"] != identity.get("package_sha256")
            or _pixels(manifest) != identity.get("pixels_sha256")
            or digest(manifest["inputs"]) != identity.get("inputs_sha256")
            or digest(selection) != identity.get("selection_sha256")
            or binding.get("object_map") != identity.get("object_map")
            or manifest["inputs"]["provenance"] != identity.get("input_provenance")
            or identity.get("role") not in ("calibration", "validation")
            or record["scenario"].get("split") != identity.get("role")
            or record["scenario"].get("blind_validation") is not False
            or record.get("source_sha256") != digest({"kind": "uploaded_pixels", "package_sha256": manifest["package_sha256"], "pixels_sha256": identity.get("pixels_sha256")})):
        raise VisualError("binding_changed", "Imported reference identity, selection, mapping or timeline changed.")
    for obj in selection.get("objects", []):
        index = obj.get("frame_index", 0)
        if (type(index) is not int or not 0 <= index < len(manifest["frames"])
                or obj.get("template_frame_sha256") != manifest["frames"][index]["sha256"]
                or "template_package" in obj):
            raise VisualError("selection_binding_mismatch", "Imported template must remain bound to a supplied frame.")
    # Validate the original preset scope; suite role is an external declaration.
    preset = deepcopy(binding)
    scenario = preset["scenario"]
    scenario.update(split="calibration")
    for key in ("case_id", "suite_split", "suite_version", "blind_validation", "validation_basis"):
        scenario.pop(key, None)
    project = {**identity["target_identity"], "recommended_capture_actions": identity["adapter_actions"]}
    media_session._validate_binding(preset, project, manifest, selection)
    if record.get("selection") != _selection(selection, binding, manifest):
        raise VisualError("binding_changed", "Shared analysis selection differs from the frozen user selection.")
    policy = suite_web.event_contract(record["scenario"]["domain"])
    if record.get("suite_event_contract") != policy or record.get("independent_review_contract") != policy or record.get("event_contract_sha256") != digest(policy):
        raise VisualError("suite_policy_mismatch", "Imported analysis and independent review use different policies.")
    observed = evidence.observations(record)
    if not isinstance(observed, dict) or digest(observed) != record.get("intake_observations_sha256"):
        raise VisualError("measurement_binding_changed", "Imported observations changed.")
    evidence._record_observation(record, record["scenario"], "media", measured=True,
        reference_package=identity["package_sha256"], expected_source={"kind": "uploaded"})
    from kiroclops.visual.events import extract_events
    if evidence.event_set(record) != extract_events(observed, record["selection"], policy):
        raise VisualError("measurement_binding_changed", "Imported events differ from the shared pixel event policy.")
    return {"status": "verified", "input_provenance": identity["input_provenance"], "claim": binding["claim"],
            "package_sha256": manifest["package_sha256"], "observations_sha256": digest(observed), "object_map": deepcopy(binding["object_map"])}


def capture_record(project, output, scenario, check, progress, cancel, variant, *, reference):
    """Native target capture followed by explicit user-ID mapping and analysis."""
    from . import evaluation, media_session
    from kiroclops.web_local.selection import select_package_from_pixels
    verify_reference_record(reference)
    binding = reference["intake_binding"]
    if scenario != binding["scenario"]:
        raise VisualError("suite_recipe_mismatch", "Target capture must use the actual frozen user timeline recipe.")
    row = evaluation._capture_web(project, output, scenario, check, progress, cancel, variant, analyze=False)
    manifest = load_package(row["package"])
    detected = select_package_from_pixels(row["package"], scenario["domain"])
    lookup = {obj["id"]: obj for obj in detected["objects"]}
    source_objects = {obj["id"]: obj for obj in reference["selection"]["objects"]}
    objects = []
    for ref_id, target_id in binding["object_map"].items():
        if target_id not in lookup:
            raise VisualError("tracking_failure", "Explicitly mapped target object is missing: " + target_id)
        obj = media_session._preserve_point_semantics(source_objects[ref_id], lookup[target_id], manifest)
        obj["id"] = ref_id
        objects.append(obj)
    selection = {**detected, "objects": objects, "connections": deepcopy(binding.get("connections", [])),
                 "relative_motion": deepcopy(binding["relative_motion"]),
                 "lifecycle_bindings": deepcopy(reference["selection"]["lifecycle_bindings"]),
                 "input_origin_s": manifest["inputs"]["events"][0]["time_s"]}
    if scenario.get("analysis_policy") != "observed_response_v2":
        intervals = evaluation._map_lifecycle(selection["lifecycle_bindings"], manifest)
        for obj in objects:
            obj["active_intervals_s"] = intervals[obj["id"]]
    row.update(intake_binding=deepcopy(binding), intake_provenance=deepcopy(reference["intake_provenance"]))
    return _measured(row, selection, Path(output) / "measurement", check, progress, candidate_sha256=row["source_sha256"])


def compare_records(reference, original, candidate, require_improvement=True):
    """Keep declared timeline uncertainty; use existing bounded media metrics."""
    from . import evaluation, media_session, suite_web
    verify_reference_record(reference)
    binding = reference["intake_binding"]
    policy = suite_web.event_contract(reference["scenario"]["domain"])
    expected = [{k: e[k] for k in ("action", "selector", "destination", "fraction") if k in e}
                for e in reference["observations"]["inputs"]["events"]]
    for target in (original, candidate):
        if target.get("scenario") != reference["scenario"] or target.get("intake_binding") != binding:
            raise VisualError("suite_recipe_mismatch", "Comparison objects and recipes must keep the explicit imported binding.")
        inputs = target["observations"]["inputs"]
        actual = [{k: e[k] for k in ("action", "selector", "destination", "fraction") if k in e} for e in inputs["events"]]
        if inputs["provenance"] != "recorder_verified" or actual != expected:
            raise VisualError("capture_timing_failure", "Target input receipts must verify every supplied interaction identity.")
        if target.get("suite_event_contract") != policy:
            raise VisualError("suite_policy_mismatch", "Target analysis does not use the shared suite event policy.")
    result = media_session._compare(reference, original, candidate, binding, require_improvement=require_improvement)
    order = []
    if reference["scenario"]["domain"] == "panel" and len(binding["relative_motion"]) >= 2:
        contract = evaluation._contract(reference, candidate)
        contract["event_options"] = deepcopy(policy["event_options"])
        order = evaluation._relative_timing_checks(reference, candidate, contract)
        result["pass"] = bool(result["pass"] and order and all(r.get("pass") is True for r in order))
        if any((r.get("reference") or {}).get("timing_status") != "resolved" or
               (r.get("candidate") or {}).get("timing_status") != "resolved" for r in order):
            result.update(measurement_status="insufficient_observability", failure_class="relative_event_timing_unresolved")
    role = reference["intake_provenance"]["role"]
    result.update(relative_event_order=order, input_authority=binding["input_provenance"], claim=binding["claim"],
                  conditional_on_user_declared_inputs=binding["input_provenance"] == "user_declared",
                  reference_role=role, blind_validation=False, benchmark_qualified=False,
                  interpretation="Agreement with this supplied, explicitly mapped clip and its input timeline; validation is user-designated, not certified blind evidence.")
    return result


def _pairs(values, cast=str):
    result = {}
    for value in values:
        try:
            name, content = value.split("=", 1)
            if name in result or not name:
                raise ValueError("duplicate or empty case")
            result[name] = cast(content)
        except ValueError as error:
            raise VisualError("invalid_binding", "Options require distinct case_id=value pairs.") from error
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake", required=True, help="Frozen multi-reference intake directory")
    parser.add_argument("--output", required=True, help="New executable reference suite directory")
    parser.add_argument("--original", required=True, help="Explicit original comparison source")
    parser.add_argument("--authoring-parent", required=True, help="Explicit unchanged intake target source")
    parser.add_argument("--svg-initial", action="append", default=[], metavar="CASE=COUNT")
    parser.add_argument("--svg-selected", action="append", default=[], metavar="CASE=NODE")
    parser.add_argument("--visible-anchor", action="append", default=[], metavar="CASE=REFERENCE_OBJECT")
    args = parser.parse_args(argv)
    try:
        options = {}
        for field, values, cast in (("initial_node_count", args.svg_initial, int),
                                    ("expected_selected_id", args.svg_selected, str),
                                    ("spatial_alignment", args.visible_anchor, str)):
            for case, value in _pairs(values, cast).items():
                if field == "spatial_alignment":
                    from .media_session import VIEWPORT_ALIGNMENT
                    value = {**VIEWPORT_ALIGNMENT, "mode": "visible_anchor", "object_id": value}
                options.setdefault(case, {})[field] = value
        result = bind_intake(args.intake, args.output, original=args.original, authoring_parent=args.authoring_parent, case_options=options)
    except VisualError as error:
        print(json.dumps({"status": error.code, "message": str(error), "model_started": False}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("execution_ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
