"""Read and bind the complete immutable reference-suite review matrix.

This performs no capture or tracking. Frozen reference pixels, the original
improvement denominator, the authoring parent and the selected candidate are
four distinct roles, all of which remain required during review.
"""
from __future__ import annotations

from pathlib import Path

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path
from kiroclops.visual.package import load_package
from . import evidence

ROLES = ("reference", "original", "before", "candidate", "final")


def _need(value, message):
    if not value:
        raise VisualError("incomplete_review", message)


def _inside(path, root, label):
    from .session import _ordinary
    _need(isinstance(path, (str, Path)) and str(path), label + " is missing")
    resolved = _ordinary(path)
    _need(resolved.is_relative_to(root.resolve()), label + " belongs to another job, role or case")
    return resolved


def _matrix(suite, state):
    calibration = [c for c in suite["cases"] if c["role"] == "calibration"]
    final = [c for c in suite["cases"] if c["role"] in ("regression", "validation")]
    selected = state.get("selected_candidate_id")
    candidates = [c for c in state.get("candidates", []) if c.get("candidate_id") == selected]
    chosen = candidates[0] if selected and len(candidates) == 1 else None
    evaluation_id = chosen.get("job_id") if chosen else None
    layout = [(state.get("prepare_job_id"), role, calibration) for role in ("reference", "original", "before")]
    layout += [(evaluation_id, "candidate", calibration)]
    layout += [(state.get("final_job_id"), role, final) for role in ("reference", "original", "before", "final")]
    return chosen, layout


def _records(path, cases, *, complete):
    if not path.is_file() and not complete:
        return []
    rows = read_json(path)
    _need(isinstance(rows, list), "Required suite role index is malformed: " + str(path))
    expected = {c["case_id"]: c for c in cases}
    found = []
    result = []
    for record in rows:
        _need(isinstance(record, dict) and isinstance(record.get("scenario"), dict), "Suite recording scenario is missing")
        scenario = record["scenario"]
        name = scenario.get("case_id", scenario.get("id"))
        _need(name in expected, "Recording belongs to an unexpected suite case")
        _need(digest(scenario) == digest(expected[name]["scenario"]), "Exact suite scenario/input recipe differs")
        _need(record.get("case_id", name) == name, "Recording case identity differs")
        found.append(name)
        result.append((record, expected[name]))
    _need(len(set(found)) == len(found), "Suite role contains duplicate cases")
    _need(not complete or set(found) == set(expected), "Required suite cases are missing")
    return result


def _source_expectation(project, full_hashes, domain):
    from .session import source_hashes
    _need(source_hashes(project) == full_hashes, "Required immutable producer source changed")
    if domain == "game":
        from kiroclops.portable_v2.runtime import source_hashes as native_hashes
        sources = native_hashes(project)
        # Renderer receipts use the runtime projection; model/job/worker
        # candidate identities retain every full session sidecar.
        return {"kind": "native", "sources": sources, "session_sources": full_hashes}
    from kiroclops.web_local.capture import project_hashes
    return {"kind": "web", "sources": project_hashes(project)}


def _web_events(record, suite):
    from .suite_web import event_contract
    from kiroclops.visual.events import extract_events
    policy = event_contract("panel" if suite["domain"] == "web_panel" else suite["domain"])
    _need(suite.get("event_contract") == policy, "Suite production/review event contract differs")
    _need(record.get("suite_event_contract") == record.get("independent_review_contract") == policy
          and record.get("event_contract_sha256") == digest(policy), "Required web event policy metadata differs")
    observed = record.get("observations")
    if isinstance(observed, str):
        observed = read_json(observed)
    if observed is None:
        observed = evidence.observations(record)
    selection = record.get("selection")
    if isinstance(selection, str):
        selection = read_json(selection)
    actual = evidence.event_set(record)
    _need(actual is not None and actual == extract_events(observed, selection, policy),
          "Stored web events differ from the actual shared production/review definition")


def _binding(record, case, suite, *, expected_source=None, reference_package=None, expected_job_id=None):
    _need(record.get("measurement_status") == "ok", "Required suite recording is unqualified")
    stage = case["stage"]
    kind = stage if stage in ("jump", "ground") else "web"
    if expected_source is None:
        # Supplied frozen references can use a documented historical split
        # label. Their immutable exact package/observation binding is checked
        # against the suite, independently of their old producer folder.
        kind = "media"
    checked = record
    if stage in ("jump", "ground"):
        projection_path = Path(record["analysis"])
        projection = read_json(projection_path)
        observed = record.get("observations")
        if isinstance(observed, str):
            observed = read_json(observed)
        if observed is None:
            observed = evidence.observations(record)
        _need(isinstance(observed, dict), "Native suite common observations are missing")
        _need(projection.get("provenance", {}).get("package_sha256") == observed.get("package_sha256")
              and projection.get("provenance", {}).get("observation_sha256") == digest(observed),
              "Native metric projection belongs to different measured pixels")
        _need(projection.get("scenario") == case["scenario"], "Native metric projection scenario differs")
        if expected_source is not None:
            raw=Path(record['raw_run'])
            if (raw.parent/'prepared-target.json').is_file() or record.get('prepared_target'):
                from .suite_prepared_target import verify_record
                verify_record(record, expected_job_id=expected_job_id,
                              expected_full_sources=expected_source['session_sources'])
            _need(projection.get("candidate_sha256") == digest(expected_source["session_sources"]),
                  "Native metric projection belongs to another full session source")
            if record.get("native_renderer_source_sha256") is not None:
                _need(record["native_renderer_source_sha256"] == digest(expected_source["sources"]),
                      "Native renderer source projection differs")
            # Both the metric projection and common worker result are bound
            # to the same full source; renderer sidecars remain separate.
            checked = {**record, "analysis": str(projection_path.parent / "common-analysis/analysis.json")}
        elif reference_package is not None:
            _reference_native(record, case, observed, projection)
    actual_scenario=case['scenario']
    if suite.get('schema_version')==2 and stage in ('panel','svg') and record.get('execution_scenario') is not None:
        from .suite_web_v2 import _binding as verify_replay_binding
        verify_replay_binding(record)
        actual_scenario=record['execution_scenario']
        checked={**checked,'scenario':actual_scenario}
    value = evidence._record_observation(checked, actual_scenario, kind, measured=True,
        reference_package=reference_package, expected_source=expected_source)
    if stage not in ("jump", "ground"):
        if record.get('intake_binding'):
            from .suite_intake_binding import verify_reference_record
            if expected_source is None:verify_reference_record(record)
        _web_events(record, suite)
    return value


def _reference_native(record, case, observed, projection):
    """Recheck the actual pinned producer behind immutable Tiny pixels."""
    from . import suite_game
    _need(isinstance(record.get("raw_run"), str), "Frozen native producer path is missing")
    raw = Path(record["raw_run"]).resolve()
    manifest = load_package(record["package"])
    suite_game.verify_reference_record(record, case["stage"])
    evidence._record_source(record, manifest, {"kind": "frozen_reference", "raw_run": str(raw)})
    _need(record.get("source_sha256") == digest({"source_commit": suite_game.TINY_COMMIT,
          "controller_hashes": suite_game.TINY_CONTROLLERS}), "Frozen reference source is not pinned Tiny")
    _need(projection.get("provenance", {}).get("manifest_sha256") == file_sha256(raw / "manifest.json"),
          "Frozen native metric projection producer differs")
    _need(projection.get("candidate_sha256") is None, "Frozen reference was mislabeled as a candidate source")
    common = read_json(Path(record["analysis"]).parent / "common-analysis/analysis.json")
    _need("candidate_sha256" in common and common["candidate_sha256"] is None
          and common.get("package_sha256") == manifest["package_sha256"]
          and common.get("observations") == observed, "Frozen reference common worker result differs")


def _origin(directory, job_id, role, record, case):
    folder = "candidate" if role in ("candidate", "final") else role
    root = directory / "measurements" / job_id / folder / case["case_id"]
    _inside(record.get("package"), root, "Recording package")
    _inside(record.get("analysis"), root, "Recording analysis")
    for key in ("observations", "selection"):
        if isinstance(record.get(key), str):
            _inside(record[key], root, "Recording " + key)
    # Native raw_run deliberately lives in the short runtime root. Its actual
    # producer receipt, frame bridge and source hashes are checked by evidence.


def _result_matrix(result, cases):
    rows = result.get("comparisons")
    _need(isinstance(rows, list) and len(rows) == len(cases), "Completed suite comparison matrix is missing")
    expected = {case["case_id"]: case for case in cases}
    keys = [row.get("case_id") for row in rows if isinstance(row, dict)]
    _need(len(keys) == len(rows) and len(set(keys)) == len(keys) and set(keys) == set(expected),
          "Completed suite comparison cases differ")
    _need(all(row.get("role") == expected[row["case_id"]]["role"]
              and row.get("purpose") == expected[row["case_id"]]["purpose"] for row in rows),
          "Completed suite comparison roles or purposes differ")


def required_bindings(directory, state, require_complete=True):
    """Return exact (package SHA, observation SHA) pairs for independent review.

    The partial path returns available selected-role evidence for display. It
    never establishes completed acceptance, sealing or a heldout pass.
    """
    from .media_suite import load_suite, record_from_suite
    from .session import source_hashes
    from .prepared_evidence import load_prepared

    directory = Path(directory).resolve()
    try:
        suite = load_suite(state["reference"])
        experiment = state.get("experiment", {})
        _need(experiment.get("reference_mode") == "media_suite"
              and digest(suite) == experiment.get("suite_sha256")
              and suite["domain"] == state.get("domain")
              and suite["stage"] == experiment.get("stage"), "Session references a different suite revision or stage")
        candidate, layout = _matrix(suite, state)
        if require_complete:
            _need(state.get("mode") in ("tools", "baseline"), "Session review mode is missing")
            _need(candidate is not None and candidate.get("status") == "succeeded"
                  and candidate.get("eligible") is True, "One eligible selected candidate evaluation is required")
            _need(Path(candidate["candidate_id"]).name == candidate["candidate_id"]
                  and candidate["candidate_id"] not in (".", ".."), "Selected candidate identity is unsafe")
            final = state.get("final") or {}
            _need(state.get("sealed_at") is not None and final.get("status") == "succeeded"
                  and final.get("job_id") == state.get("final_job_id"), "A completed sealed suite final measurement is required")
            prepare_id, chosen_id, final_id = state.get("prepare_job_id"), candidate["job_id"], state["final_job_id"]
            _need(len({prepare_id, chosen_id, final_id}) == 3, "Suite prepare, evaluate and final jobs must be distinct")
            results = {
                prepare_id: evidence._completed_job(directory, state, prepare_id, "prepare"),
                chosen_id: evidence._completed_job(directory, state, chosen_id, "evaluate", candidate),
                final_id: evidence._completed_job(directory, state, final_id, "finalize", candidate),
            }
            _need(all(result.get("suite_sha256") == digest(suite) for result in results.values()),
                  "Completed job belongs to another suite revision")
            _need(results[chosen_id].get("eligible") is True, "Selected completed result is not eligible")
            _need(results[final_id].get("pass") is final.get("pass"), "Final suite result and session summary differ")
            _need(results[final_id].get("heldout_status") == suite["heldout_status"], "Final heldout provenance differs")
            _need(suite["heldout_status"] == "provided" or results[final_id].get("pass") is False,
                  "A suite without fresh validation cannot claim a final pass")
            _result_matrix(results[chosen_id], layout[3][2])
            _result_matrix(results[final_id], layout[-1][2])
            _need(source_hashes(state["reference"]) == state["reference_hashes"], "Session frozen reference files changed")
            _need(state["baseline_hashes"] == suite["authoring_parent_source_hashes"], "Authoring parent and original denominator roles were exchanged")
            prepared = load_prepared(directory, state)
            _need(set(prepared) == {"reference", "original", "before"}, "Preparation has missing or unexpected baseline roles")
            for role, path in prepared.items():
                _need(Path(path).resolve() == directory / "measurements" / prepare_id / (role + ".json"),
                      "Prepared suite role points outside its completed prepare job")
            expected_paths = {directory / "measurements" / job / (role + ".json") for job, role, _ in layout}
            for job in (prepare_id, chosen_id, final_id):
                found = {path for role in ROLES for path in (directory / "measurements" / job).rglob(role + ".json")}
                _need(found == {path for path in expected_paths if path.parent.name == job},
                      "Suite measurement role index is missing or unexpected")
        producers = {}
        producers["original"] = _source_expectation(safe_path(Path(state["reference"]), suite["original"], must_exist=False), suite["original_source_hashes"], suite["domain"])
        producers["before"] = _source_expectation(Path(state["baseline"]), state["baseline_hashes"], suite["domain"])
        if candidate is not None:
            chosen_project = directory / "candidates" / candidate["candidate_id"] / "source"
            full = source_hashes(chosen_project)
            _need(digest(full) == candidate.get("source_sha256"), "Selected immutable candidate source changed")
            producers["candidate"] = producers["final"] = _source_expectation(chosen_project, full, suite["domain"])
        required = set()
        reference_cache = {}
        for job, role, cases in layout:
            if not job or candidate is None and role in ("candidate", "final"):
                continue
            _need(isinstance(job, str) and Path(job).name == job and job not in (".", ".."), "Measurement job identity is unsafe")
            for record, case in _records(directory / "measurements" / job / (role + ".json"), cases, complete=require_complete):
                _origin(directory, job, role, record, case)
                if role == "reference":
                    if case["case_id"] not in reference_cache:
                        frozen = record_from_suite(state["reference"], case)
                        reference_cache[case["case_id"]] = _binding(frozen, case, suite, reference_package=case["package_sha256"])
                    _need(record.get("source_sha256") == case.get("source_sha256"), "Frozen reference producer identity differs")
                    if case["stage"] in ("jump", "ground"):
                        _need(record.get("raw_run") == record_from_suite(state["reference"], case).get("raw_run"),
                              "Frozen reference raw producer pointer differs")
                    value = _binding(record, case, suite, reference_package=case["package_sha256"])
                    _need(value == reference_cache[case["case_id"]], "Copied reference observations differ from the frozen suite")
                else:
                    value = _binding(record, case, suite, expected_source=producers.get(role),
                                     expected_job_id=job if role in ('candidate','final') else None)
                required.add(value)
        if require_complete and suite['domain']=='game':
            from .suite_boundary import bindings as boundary_bindings
            required.update(boundary_bindings(directory/'measurements'/state['final_job_id']/'visual-boundary.json',
                candidate_sha256=candidate['source_sha256'],candidate_id=candidate['candidate_id']))
            from .suite_game_demo import verify as verify_normal_level
            normal=verify_normal_level(directory/'measurements'/state['final_job_id']/'normal-level',
                                       source_sha256=candidate['source_sha256'])
            _need(normal['status']=='pass','The selected character must pass the original-level combined route.')
        _need(not require_complete or bool(required), "No complete suite observation evidence")
        return required
    except (OSError, KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        if isinstance(exc, VisualError) and exc.code == "incomplete_review":
            raise
        raise VisualError("incomplete_review", "Missing or changed suite review evidence: " + str(exc)) from exc
