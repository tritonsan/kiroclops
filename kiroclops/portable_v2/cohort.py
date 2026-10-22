"""Preregister three immutable jump→ground chains; never replace failed runs."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading

from kiroclops.common import ROOT
from kiroclops.integration.storage import IntegrationError, project_hashes, read_json, sha256, utc_now, write_json
from .session import Session, create_session, digest, ordinary

QUALIFICATION_GATES = frozenset({"runtime_determinism", "tracking_calibration", "manual_calibration",
    "integrity_baseline", "integrity_negative_controls", "wall_coyote_controls", "software_tests", "legacy_sources"})


def _qualified(proof):
    gates = proof.get("gates", {}) if isinstance(proof, dict) else {}
    if (not isinstance(proof, dict) or proof.get("status") != "pass" or not isinstance(gates, dict)
            or not QUALIFICATION_GATES.issubset(gates)
            or any(not isinstance(gates[name], dict) or gates[name].get("status") != "pass"
                   for name in QUALIFICATION_GATES)
            or any(not isinstance(proof.get(key), dict) for key in ("files", "evidence_files", "reference_files"))):
        raise IntegrationError("not_qualified", "All eight explicit qualification gates and their file maps are required")
    return proof


def _exclusive(path, value):
    path = ordinary(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return path


def _file_map(root, values):
    output = {}
    for value in values:
        path = ordinary(value)
        if not path.is_relative_to(root) or not path.is_file():
            raise IntegrationError("qualification_binding", "Qualified inputs must be local regular files")
        output[path.relative_to(root).as_posix()] = sha256(path)
    return output


def register_cohort(cohort_id, baseline_project, *, qualification=None, root=ROOT):
    from . import specs
    if not isinstance(cohort_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", cohort_id):
        raise IntegrationError("invalid_cohort", "Use a short lowercase cohort identifier")
    root = ordinary(root)
    qualification = ordinary(qualification or specs.CONFIG / "qualification.json")
    proof = _qualified(read_json(qualification))
    protocol_sources = specs.protocol_sources()
    if proof.get("protocol_sources") != protocol_sources:
        raise IntegrationError("qualification_binding", "Qualification was produced by different protocol sources")
    legacy = specs.verify_legacy()
    specs.verify_source()
    source = ordinary(baseline_project)
    hashes = project_hashes(source)
    if not hashes or "kiroclops_heart/capture.gd" not in hashes:
        raise IntegrationError("unprepared_baseline", "Register an immutable adapter-prepared Heart baseline")
    config = root / "config/phase2c-v2"
    # Bind all setup controls, annotations and declared reference-image receipts.
    inputs = [p for p in config.rglob("*") if p.is_file() and "cohorts" not in p.relative_to(config).parts]
    inputs.append(qualification)
    for mapping in (proof.get("files", {}), proof.get("evidence_files", {}), proof.get("reference_files", {})):
        if not isinstance(mapping, dict):
            raise IntegrationError("qualification_binding", "Qualification file maps must be dictionaries")
        for rel, expected in mapping.items():
            path = ordinary(root / rel)
            if not path.is_relative_to(root) or not path.is_file() or sha256(path) != expected:
                raise IntegrationError("qualification_binding", "Qualified file changed before cohort registration")
            inputs.append(path)
    registry = {"schema_version": 2, "protocol_id": "heart-transfer-chain-v2", "cohort_id": cohort_id,
                "registered_at": utc_now(), "root": str(root), "model": "gpt-5.6-sol", "effort": "max",
                "limits_per_stage": {"max_candidates": 3, "max_seconds": 1800},
                "target_spec": specs.get_target("heart").record(),
                "experiments": {stage: specs.experiment(stage) for stage in ("jump", "ground")},
                "protocol_sources": protocol_sources, "legacy_sources": legacy,
                "qualified_inputs": _file_map(root, inputs), "qualification_path": str(qualification),
                "qualification_sha256": sha256(qualification),
                "baseline": {"project": str(source), "source_hashes": hashes},
                "chains": [{"chain_id": f"{cohort_id}-{i:02}", "jump_session": f"heart-{cohort_id}-{i:02}-jump",
                            "ground_session": f"heart-{cohort_id}-{i:02}-ground"} for i in (1, 2, 3)],
                "replacement_policy": "none", "manual_review": "after all frozen model runs; pending is not full acceptance"}
    return _exclusive(config / "cohorts" / f"{cohort_id}.json", registry)


def verify_registry(path):
    from . import specs
    path = ordinary(path)
    registry = read_json(path)
    root = ordinary(registry["root"])
    if (registry.get("schema_version") != 2 or registry.get("protocol_id") != "heart-transfer-chain-v2" or registry.get("model") != "gpt-5.6-sol"
            or registry.get("effort") != "max" or registry.get("replacement_policy") != "none"
            or registry.get("limits_per_stage") != {"max_candidates": 3, "max_seconds": 1800}
            or len(registry.get("chains", [])) != 3
            or registry.get("protocol_sources") != specs.protocol_sources()
            or registry.get("legacy_sources") != specs.verify_legacy()
            or registry.get("target_spec") != specs.get_target("heart").record()
            or registry.get("experiments") != {s: specs.experiment(s) for s in ("jump", "ground")}):
        raise IntegrationError("cohort_changed", "Cohort controls or frozen source changed")
    expected_chains = [{"chain_id": f'{registry["cohort_id"]}-{i:02}',
                       "jump_session": f'heart-{registry["cohort_id"]}-{i:02}-jump',
                       "ground_session": f'heart-{registry["cohort_id"]}-{i:02}-ground'} for i in (1, 2, 3)]
    if registry["chains"] != expected_chains:
        raise IntegrationError("cohort_changed", "The exact three registered chains must remain")
    for rel, expected in registry["qualified_inputs"].items():
        item = ordinary(root / rel)
        if not item.is_relative_to(root) or not item.is_file() or sha256(item) != expected:
            raise IntegrationError("qualification_changed", f"Qualified input changed: {rel}")
    q = ordinary(registry["qualification_path"])
    if sha256(q) != registry["qualification_sha256"]:
        raise IntegrationError("qualification_changed", "Qualification receipt changed")
    proof = _qualified(read_json(q))
    if proof.get("protocol_sources") != registry["protocol_sources"]:
        raise IntegrationError("qualification_binding", "Qualification and registered protocol sources differ")
    if project_hashes(registry["baseline"]["project"]) != registry["baseline"]["source_hashes"]:
        raise IntegrationError("baseline_changed", "Clean prepared Heart baseline changed")
    specs.verify_source()
    return registry


def verify_launch_registration(directory):
    session = Session(directory)
    state = session.verify()
    protocol = state.get("protocol", {})
    path = ordinary(protocol.get("cohort_path", ""))
    if not path.is_file() or sha256(path) != protocol.get("cohort_sha256"):
        raise IntegrationError("unregistered_launch", "No immutable preregistered cohort authorizes this stage")
    registry = verify_registry(path)
    chain = next((c for c in registry["chains"] if c["chain_id"] == protocol.get("chain_id")), None)
    stage = protocol.get("stage")
    if (not chain or stage not in {"jump", "ground"} or chain[stage + "_session"] != state["session_id"]
            or state["target_spec"] != registry["target_spec"] or state["experiment_spec"] != registry["experiments"][stage]
            or state.get("original_baseline") != registry["baseline"]):
        raise IntegrationError("unregistered_launch", "Stage identity is not one of the fixed cohort members")
    if datetime.fromisoformat(registry["registered_at"]) >= datetime.fromisoformat(state["created_at"]):
        raise IntegrationError("late_registration", "Cohort must precede session creation")
    if stage == "jump":
        if state.get("parent") or state["baseline_hashes"] != registry["baseline"]["source_hashes"]:
            raise IntegrationError("baseline_changed", "Jump must begin at the registered clean source")
    else:
        parent_dir = Path(registry["root"]) / "sessions" / chain["jump_session"]
        promoted = promotion_receipt(parent_dir)
        if state.get("parent") != promoted or state["baseline_hashes"] != promoted["source_hashes"]:
            raise IntegrationError("parent_changed", "Ground must inherit exactly its own sealed jump candidate")
    return registry


def promotion_receipt(parent_directory):
    """Private backend receipt; never export parent held-out feedback to the child."""
    session = Session(parent_directory)
    state = session.verify()
    launch_path = session.directory / "kiro/launch.json"
    launch = read_json(launch_path)
    final = state.get("final") or {}
    stream = launch.get("stream_configuration", {})
    if (state.get("experiment_id") != "heart-jump-v2" or state.get("status") != "finished"
            or state.get("active_job_id") or not state.get("sealed_at")
            or final.get("numerical_status") != "pass" or final.get("automatic_integrity_status") != "pass"
            or final.get("measurement_status") not in {"pending", "pass"}
            or launch.get("status") != "completed" or launch.get("process_returncode_after_cleanup") is None
            or launch.get("model_requested") != "gpt-5.6-sol" or launch.get("effort_requested") != "max"
            or not stream.get("generation_configuration_verified") or launch.get("elapsed_seconds", 1801) > 1800):
        raise IntegrationError("parent_unverified", "Parent needs a terminal native run and sealed numerical/integrity acceptance")
    cid = state["selected_candidate_id"]
    candidate = session.verify_candidate(cid)
    jobs = [read_json(p) for p in (session.directory / "jobs").glob("job-*.json")]
    finals = [j for j in jobs if j.get("action") == "finalize" and j.get("candidate_id") == cid
              and j.get("status") == "succeeded" and j.get("session_id") == state["session_id"]
              and j.get("candidate_source_sha256") == candidate["source_sha256"] and j.get("result") == final]
    if len(finals) != 1:
        raise IntegrationError("parent_unverified", "Parent finalization job/source binding is missing")
    return {"session": str(session.directory), "candidate_id": cid, "source_hashes": candidate["source_hashes"],
            "source_sha256": candidate["source_sha256"], "candidate_record_sha256": sha256(session.directory / "candidates" / cid / "candidate.json"),
            "finalization_job_sha256": sha256(session.directory / "jobs" / (finals[0]["job_id"] + ".json")),
            "launch_sha256": sha256(launch_path), "review_status_at_promotion": final["measurement_status"]}


def run_cohort(registry_path, *, executable=None, cancel=None, progress=None):
    """One bounded operator call, up to six fresh native chats, no follow-up coaching."""
    from .launcher import configure_model_cli, run_model_cli
    registry_path = ordinary(registry_path)
    registry = verify_registry(registry_path)
    root = Path(registry["root"])
    output = root / "artifacts/phase2c-v2/cohorts" / registry["cohort_id"] / "run.json"
    record = {"schema_version": 1, "cohort_id": registry["cohort_id"], "registry_sha256": sha256(registry_path),
              "started_at": utc_now(), "status": "running", "chains": []}
    _exclusive(output, record)
    for chain in registry["chains"]:
        item = {"chain_id": chain["chain_id"], "status": "running", "stages": {}}
        record["chains"].append(item)
        write_json(output, record)
        parent = None
        for stage in ("jump", "ground"):
            if cancel is not None and cancel.is_set():
                item["stages"][stage] = {"status": "not_run", "reason": "cohort_cancelled"}
                item["status"] = "cancelled"
                continue
            if stage == "ground" and parent is None:
                item["stages"][stage] = {"status": "not_run", "reason": "jump_not_promotable"}
                item["status"] = "fail"
                continue
            try:
                verify_registry(registry_path)
                source = (Path(parent["session"]) / "candidates" / parent["candidate_id"] / "project"
                          if parent else Path(registry["baseline"]["project"]))
                protocol = {"cohort_path": str(registry_path), "cohort_sha256": sha256(registry_path),
                            "chain_id": chain["chain_id"], "stage": stage}
                directory = create_session(chain[stage + "_session"], source, registry["target_spec"],
                    registry["experiments"][stage], parent=parent, original_baseline=registry["baseline"],
                    protocol=protocol, root=root)
                configure_model_cli(directory)
                item["stages"][stage] = {"session": str(directory), "status": "running"}
                write_json(output, record)
                if progress:
                    progress(chain["chain_id"], stage, "running")
                launch = run_model_cli(directory, executable=executable, cancel=cancel)
                state = Session(directory).verify()
                final = state.get("final") or {}
                numerical = (launch.get("status") == "completed" and launch.get("process_returncode_after_cleanup") is not None
                             and final.get("numerical_status") == "pass" and final.get("automatic_integrity_status") == "pass")
                item["stages"][stage].update(status="numerical_pass_review_pending" if numerical else "fail",
                    candidate_count=len(state["candidates"]), selected_candidate_id=state.get("selected_candidate_id"),
                    launch_status=launch.get("status"), elapsed_seconds=launch.get("elapsed_seconds"),
                    numerical_status=final.get("numerical_status", "not_run"),
                    automatic_integrity_status=final.get("automatic_integrity_status", "not_run"),
                    measurement_status=final.get("measurement_status", "pending"))
                if stage == "jump" and numerical:
                    parent = promotion_receipt(directory)
                    _exclusive(Path(directory) / "promotion.json", parent)
                if stage == "ground":
                    item["status"] = "numerical_pass_review_pending" if numerical else "fail"
            except (Exception, KeyboardInterrupt) as exc:
                item["stages"][stage] = {"status": "fail", "code": getattr(exc, "code", type(exc).__name__), "error": str(exc)}
                item["status"] = "fail"
                if isinstance(exc, KeyboardInterrupt):
                    if cancel is None:
                        cancel = threading.Event()
                    cancel.set()
            write_json(output, record)
            if progress:
                progress(chain["chain_id"], stage, item["stages"][stage]["status"])
    numeric = sum(c["status"] == "numerical_pass_review_pending" for c in record["chains"])
    record.update(status="pending_independent_review" if numeric == 3 else "fail",
                  numerical_passed_chains=numeric, total_chains=3, finished_at=utc_now())
    write_json(output, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("register", "run", "verify"))
    parser.add_argument("--cohort")
    parser.add_argument("--baseline")
    parser.add_argument("--registry")
    args = parser.parse_args()
    if args.action == "register":
        result = str(register_cohort(args.cohort, args.baseline))
    elif args.action == "verify":
        result = verify_registry(args.registry)
    else:
        result = run_cohort(args.registry)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
