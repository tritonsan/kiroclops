"""Operator-only, transactional visual review of a sealed ground experiment.

Coordinates are supplied explicitly or copied from an explicitly authorized
annotation only when native PNG bytes match. No tracker coordinate is promoted
to a manual observation. Existing jump review/reuse semantics stay unchanged.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import uuid

from kiroclops.ground import analysis as ground_analysis, comparison as ground_comparison
from kiroclops.ground.annotation import validate_annotation
from . import control_reuse, evaluation, ground_worker
from .experiments import GROUND_ID, get_experiment
from .review import _digest, _inside, _require_source, _restore
from .session import Session
from .storage import IntegrationError, file_lock, identifier, read_json, sha256, utc_now, write_json

MEASUREMENT_FIELDS = (*evaluation.MEASUREMENT_FIELDS, "phases")
NUMERICAL_FIELDS = ("gates", "validation_scenarios", "coverage", "alignment", "numerical_status",
                    "mean_rmse_body", "mean_before_rmse_body", "scenario_checks")


def _local_file(path, directory):
    """Reject links/junctions and shared file inodes before using review state."""
    path,directory=Path(path),Path(directory).resolve()
    if not path.is_absolute():
        path=directory/path
    resolved=path.resolve()
    if resolved==directory or not resolved.is_relative_to(directory):
        raise IntegrationError("invalid_review_path","Review output escapes its session directory")
    for component in (path,*path.parents):
        if component==directory:
            break
        if component.is_symlink() or (component.exists() and getattr(component.lstat(),"st_file_attributes",0)&0x400):
            raise IntegrationError("invalid_review_path","Linked/reparse review state is not allowed")
    if path.exists() and not path.is_file():
        raise IntegrationError("invalid_review_path","Review state must be a regular file")
    if path.exists() and path.stat().st_nlink != 1:
        raise IntegrationError("invalid_review_path","Hardlinked review state is not allowed")
    return path


def _measurement(run):
    value = read_json(Path(run) / "analysis.json")
    return _digest({key: value.get(key) for key in MEASUREMENT_FIELDS})


def _raw(run):
    manifest = read_json(run / "manifest.json")
    if manifest.get("frame_count") != 240 or manifest.get("fps") != 60:
        raise IntegrationError("invalid_evidence", "Review requires complete native 240-frame captures")
    files = {"manifest.json": sha256(run / "manifest.json")}
    files.update({f"frames/{index:06d}.png": sha256(run / "frames" / f"{index:06d}.png") for index in range(240)})
    if (run / "video.mp4").is_file():
        files["video.mp4"] = sha256(run / "video.mp4")
    return _digest(files)


def _numerical(comparison):
    return _digest({"ground": {key: comparison["ground"].get(key) for key in NUMERICAL_FIELDS},
                    "jump_regression": {key: comparison["jump_regression"].get(key) for key in NUMERICAL_FIELDS},
                    "parent_jump_drift": comparison.get("parent_jump_drift"),
                    "numerical_status": comparison.get("numerical_status")})


def _slot(manifest, mechanic):
    s = manifest["scenario"]
    if mechanic == "ground":
        ground_analysis.scenario_phases(s)
        return s["id"], s["spawn_offset"], s["repeat"]
    return s["hold_ticks"], s["spawn_offset"], s["repeat"], s["start_tick"]


def _verify_origin(run, mechanic):
    """Copied references/original-before must still match canonical raw bytes."""
    original=ground_worker.ROOT / ("artifacts/phase2b/reference" if mechanic=="ground" else "artifacts/runs") / run.name
    record=read_json(run/"copy-origin.json")
    source=record.get("source",record.get("source_run"))
    if not isinstance(source,str) or Path(source).resolve()!=original.resolve():
        raise IntegrationError("invalid_evidence","Copied capture origin differs from the canonical evidence slot")
    if sha256(run/"manifest.json")!=sha256(original/"manifest.json"):
        raise IntegrationError("invalid_evidence","Copied manifest differs from canonical evidence")
    target_sequence=[sha256(run/"frames"/f"{i:06d}.png") for i in range(240)]
    source_sequence=[sha256(original/"frames"/f"{i:06d}.png") for i in range(240)]
    if target_sequence!=source_sequence:
        raise IntegrationError("invalid_evidence","Copied native frames differ from canonical evidence")
    if "hashes" in record:
        expected={"manifest.json":sha256(original/"manifest.json"),
                  **{f"frames/{i:06d}.png":digest for i,digest in enumerate(source_sequence)}}
        if (original/"video.mp4").is_file():
            expected["video.mp4"]=sha256(original/"video.mp4")
        if record["hashes"]!=expected or any(sha256(run/name)!=digest for name,digest in expected.items()):
            raise IntegrationError("invalid_evidence","Copied capture file inventory changed")
    elif (record.get("manifest_sha256")!=sha256(original/"manifest.json")
            or record.get("raw_sequence_sha256")!=_digest(source_sequence)):
        raise IntegrationError("invalid_evidence","Copied jump reference origin changed")


def _context(directory):
    session = Session(directory)
    _local_file(session.path,session.directory)
    state = ground_worker.verify_session(session)
    directory = session.directory
    cid = state.get("selected_candidate_id")
    final = state.get("final")
    if (state.get("experiment_id") != GROUND_ID or state.get("status") != "finished"
            or not cid or not state.get("sealed_at") or state.get("active_job_id")
            or not isinstance(final, dict) or final.get("candidate_id") != cid):
        raise IntegrationError("review_not_ready", "Review requires a finished, sealed, idle ground candidate")
    root = directory / "candidates" / identifier(cid)
    _local_file(root/"candidate.json",directory)
    candidate = read_json(root / "candidate.json")
    saved = next((item for item in state.get("candidates", []) if item.get("candidate_id") == cid), None)
    if candidate != saved or candidate.get("status") != "finalized":
        raise IntegrationError("experiment_integrity", "Selected candidate metadata differs from session")
    baseline = directory / "baseline"
    if Path(state["baseline"]).resolve() != baseline:
        raise IntegrationError("invalid_review_path", "Baseline path differs from the fixed session location")
    _require_source(baseline, state["baseline_hashes"])
    _require_source(root / "project", candidate["source_hashes"])
    groups = {mechanic: {role: [] for role in ("reference", "baseline", "candidate")} for mechanic in ("ground", "jump")}
    annotations, all_runs = {}, []
    for mechanic in groups:
        for role in groups[mechanic]:
            game, variant = ("tiny", "reference") if role == "reference" else ("slay", "before" if role == "baseline" else "after")
            if mechanic == "ground":
                base = {"reference": directory/"reference", "baseline": directory/"baseline-runs", "candidate": root/"runs"}[role]
                saved_runs = state["reference" if role == "reference" else "baseline_runs"] if role != "candidate" else {
                    split: candidate.get(f"{split}_runs") for split in ("calibration", "validation")}
                annotation = directory / "annotations/ground" / f"{game}-annotation.json"
                _local_file(annotation,directory)
                locked = read_json(ground_worker.ROOT / f"config/phase2b/{game}-annotation.json")
                if evaluation._measurement(read_json(annotation)) != evaluation._measurement(locked):
                    raise IntegrationError("annotation_changed", "Ground measurement settings differ from their lock")
            else:
                base = {"reference": directory/"jump-regression/reference", "baseline": directory/"jump-regression/original-before", "candidate": root/"jump-runs"}[role]
                saved_runs = state["reference" if role == "reference" else "baseline_runs"]["jump"] if role != "candidate" else {
                    split: candidate.get(f"jump_{split}_runs") for split in ("calibration", "validation")}
            for split in ("calibration", "validation"):
                expected = ({(s["id"],s["spawn_offset"],s["repeat"]) for s in get_experiment(GROUND_ID).scenarios(split)}
                            if mechanic == "ground" else {(h,o,r,30) for h in ((3,24) if split == "calibration" else (8,14))
                                                          for o in ((0,) if split == "calibration" else (0,24)) for r in (1,2,3)})
                observed = set()
                paths = saved_runs.get(split)
                if not isinstance(paths,list):
                    raise IntegrationError("incomplete_experiment", "Capture group is missing")
                for raw in paths:
                    run = _inside(raw,base.resolve(),relative_base=directory)
                    _local_file(run/"manifest.json",directory)
                    _local_file(run/"analysis.json",directory)
                    manifest = read_json(run / "manifest.json")
                    if (manifest.get("game") != game or manifest.get("variant") != variant
                            or manifest.get("status") != "ok" or manifest.get("scenario", {}).get("split") != split):
                        raise IntegrationError("invalid_evidence", "Capture identity differs from selected experiment")
                    slot = _slot(manifest,mechanic)
                    if slot in observed or run in all_runs:
                        raise IntegrationError("invalid_evidence", "Duplicate capture or scenario slot")
                    observed.add(slot)
                    expected_source = (candidate["source_hashes"] if role == "candidate" else state["baseline_hashes"]
                                       if role == "baseline" and mechanic == "ground" else None)
                    if expected_source is not None and any(manifest.get("provenance", {}).get(key) != expected_source
                            for key in ("full_source_hashes_before", "full_source_hashes_after")):
                        raise IntegrationError("experiment_integrity", "Capture controller differs from sealed source")
                    if expected_source is None:
                        _verify_origin(run,mechanic)
                    if mechanic == "jump":
                        annotation_dir = directory / ("jump-regression/reference/annotations" if role == "reference" else "annotations/jump")
                        annotation = evaluation.controls_path(run,annotation_dir)
                        # controls_path resolves its directory; check the raw
                        # directory path too so an internal junction is rejected.
                        _local_file(annotation_dir/annotation.name,directory)
                        _local_file(annotation,directory)
                        if evaluation._measurement(read_json(annotation)) != evaluation._measurement(evaluation._locked_annotation(game)):
                            raise IntegrationError("annotation_changed", "Jump measurement settings differ from frozen Phase 1")
                    report = read_json(run / "analysis.json")
                    if report.get("provenance", {}).get("manifest_sha256") != sha256(run / "manifest.json"):
                        raise IntegrationError("invalid_evidence", "Analysis is not bound to its capture manifest")
                    if report.get("provenance", {}).get("annotation_sha256") != sha256(annotation):
                        raise IntegrationError("invalid_evidence", "Analysis is not bound to current annotation")
                    annotations[run] = annotation
                    groups[mechanic][role].append(run)
                    all_runs.append(run)
                if observed != expected:
                    raise IntegrationError("incomplete_experiment", f"Incomplete {mechanic} {role} {split} matrix")
    _local_file(state["final"]["comparison_path"],directory)
    comparison_path = _inside(state["final"]["comparison_path"], directory/"final", relative_base=directory)
    return {"session": session, "state": state, "candidate": candidate, "candidate_root": root,
            "groups": groups, "runs": all_runs, "annotations": annotations, "comparison_path": comparison_path}


def _point(point, run, source):
    manifest = read_json(run / "manifest.json")
    if (not isinstance(point,dict) or not {"frame_index", "x", "y", "frame_sha256"} <= point.keys()
            or point.keys()-{"frame_index", "x", "y", "frame_sha256", "evidence"}):
        raise IntegrationError("invalid_controls", "Point needs explicit frame_index, x, y, frame_sha256 and optional evidence")
    index = point["frame_index"]
    if type(index) is not int or not 0 <= index < manifest["frame_count"]:
        raise IntegrationError("invalid_controls", "Point frame lies outside native capture")
    for key,limit in zip(("x","y"),manifest["viewport"]):
        if type(point[key]) not in (int,float) or not math.isfinite(point[key]) or not 0 <= point[key] < limit:
            raise IntegrationError("invalid_controls", "Point coordinates must be finite native pixels")
    if sha256(run / "frames" / f"{index:06d}.png") != point["frame_sha256"]:
        raise IntegrationError("invalid_controls", "Observed frame hash is stale or belongs to another image")
    return {**deepcopy(point), "source": source, "run_id": str(run)}


def _controls(value, context, source):
    input_hash = None
    if value is None:
        return [],input_hash
    if isinstance(value,(str,Path)):
        input_hash = sha256(value)
        value = read_json(value)
    if not isinstance(value,list):
        raise IntegrationError("invalid_controls", "Controls input must be a list of run records")
    result,seen = [],set()
    for record in value:
        if not isinstance(record,dict) or record.keys()-{"run","points","event_frames"} or "run" not in record:
            raise IntegrationError("invalid_controls", "Control record needs run and points and/or jump event_frames")
        run = _inside(record["run"],context["session"].directory)
        if run not in context["runs"] or run in seen:
            raise IntegrationError("invalid_controls", "Control run is not a unique selected experiment capture")
        seen.add(run)
        raw_points = record.get("points",[])
        if not isinstance(raw_points,list):
            raise IntegrationError("invalid_controls", "points must be a list")
        points = [_point(p,run,source) for p in raw_points]
        if len({p["frame_index"] for p in points}) != len(points):
            raise IntegrationError("invalid_controls", "Duplicate point frame")
        events = deepcopy(record.get("event_frames"))
        is_jump = any(run in runs for runs in context["groups"]["jump"].values())
        if events is not None:
            names = ("takeoff_frame","apex_frame","landing_frame")
            if not is_jump or not isinstance(events,dict) or not set(names)<=events.keys() or events.keys()-set(names)-{"frame_sha256","evidence"}:
                raise IntegrationError("invalid_controls", "Only jump captures accept explicit event frames")
            count = read_json(run / "manifest.json")["frame_count"]
            if (any(type(events[n]) is not int or not 0<=events[n]<count for n in names)
                    or not events[names[0]]<events[names[1]]<events[names[2]]):
                raise IntegrationError("invalid_controls", "Jump events must be ordered within the capture")
            if any(events.get("frame_sha256",{}).get(n) != sha256(run / "frames" / f"{events[n]:06d}.png") for n in names):
                raise IntegrationError("invalid_controls", "Jump event frames require their exact observed PNG hashes")
        if not points and events is None:
            raise IntegrationError("invalid_controls", "Empty control record")
        result.append({"run":run,"points":points,"event_frames":events,"mechanic":"jump" if is_jump else "ground"})
    return result,input_hash


def _merge_ground(annotation_path, points):
    annotation = read_json(annotation_path)
    current = {(p["run_id"],p["frame_index"]):p for p in annotation.get("control_points",[])}
    for point in points:
        key = point["run_id"],point["frame_index"]
        previous = current.get(key)
        if previous and any(previous[axis] != point[axis] for axis in ("x","y")):
            raise IntegrationError("conflicting_controls", "Existing independent coordinates conflict for this raw frame")
        current.setdefault(key,point)
    annotation["control_points"] = sorted(current.values(),key=lambda p:(p["run_id"],p["frame_index"]))
    validate_annotation(annotation)
    write_json(annotation_path,annotation)


def _reuse_ground(sources, context):
    """Reuse explicitly authorized direct observations on identical whole PNGs."""
    if sources is None:
        return {"points":0,"evidence_sha256":{}}
    sources = [sources] if isinstance(sources,(str,Path)) else list(sources)
    evidence,points = {},[]
    for path in sources:
        path = Path(path).resolve()
        if path.is_relative_to(context["session"].directory):
            raise IntegrationError("invalid_reuse_source", "Use a separate immutable observation source")
        annotation = read_json(path)
        validate_annotation(annotation)
        template = Path(annotation["template_run"]).resolve()
        game = read_json(template / "manifest.json")["game"]
        locked = read_json(ground_worker.ROOT / f"config/phase2b/{game}-annotation.json")
        if evaluation._measurement(annotation) != evaluation._measurement(locked):
            raise IntegrationError("annotation_changed", "Reuse observations use different fixed measurement settings")
        evidence[str(path)] = sha256(path)
        for point in annotation.get("control_points",[]):
            if point.get("source") not in {"human","assistant_visual"} or point.get("reuse_origin"):
                continue
            original = Path(point["run_id"])
            if not original.is_absolute():
                raise IntegrationError("invalid_reuse_source", "Reuse observation needs its original absolute run ID")
            original = original.resolve()
            manifest = read_json(original / "manifest.json")
            if manifest.get("game") != game or manifest.get("status") != "ok":
                raise IntegrationError("invalid_reuse_source", "Reuse observation capture identity changed")
            clean = {key:point[key] for key in ("frame_index","x","y","frame_sha256","evidence") if key in point}
            checked = _point(clean,original,point["source"])
            source_frame = original / "frames" / f"{point['frame_index']:06d}.png"
            evidence[str(source_frame)] = sha256(source_frame)
            evidence[str(original/"manifest.json")] = sha256(original/"manifest.json")
            points.append((game,manifest["viewport"],checked,path))
    count=0
    for runs in context["groups"]["ground"].values():
        for run in runs:
            manifest = read_json(run / "manifest.json")
            relevant=[p for p in points if p[0]==manifest["game"] and p[1]==manifest["viewport"]]
            if not relevant:
                continue
            current=read_json(context["annotations"][run]).get("control_points",[])
            occupied={(p["run_id"],p["frame_index"]) for p in current}
            copied=[]
            for index in range(manifest["frame_count"]):
                digest=sha256(run / "frames" / f"{index:06d}.png")
                matches=[(point,path) for _,_,point,path in relevant if point["frame_sha256"]==digest]
                if not matches:
                    continue
                if len({(p["x"],p["y"]) for p,_ in matches}) != 1:
                    raise IntegrationError("conflicting_controls", "Authorized sources disagree on identical raw pixels")
                point,path=matches[0]
                copied.append({**point,"run_id":str(run),"frame_index":index,
                               "reuse_origin":{"method":"identical-native-PNG-authorized-annotation",
                                   "annotation_path":str(path),"annotation_sha256":evidence[str(path)],
                                   "run_id":point["run_id"],"frame_index":point["frame_index"],"frame_sha256":digest}})
                count += (str(run),index) not in occupied
            _merge_ground(context["annotations"][run],copied)
    _verify_external(evidence)
    return {"points":count,"evidence_sha256":evidence}


def _verify_external(evidence):
    if any(sha256(Path(path)) != expected for path,expected in evidence.items()):
        raise IntegrationError("reuse_source_changed", "Authorized observation source changed")


def _compare(context):
    groups=context["groups"]
    ground=ground_comparison.compare_runs(*(groups["ground"][role] for role in ("reference","baseline","candidate")))
    jump=evaluation.final_comparison(*(groups["jump"][role] for role in ("reference","baseline","candidate")))
    heldout=[r for r in groups["jump"]["candidate"] if read_json(r/"manifest.json")["scenario"]["split"]=="validation"]
    return ground_worker.combine_comparisons(ground,jump,ground_worker._jump_drift(heldout,"validation"))


def review_session(directory, controls=None, *, source="human", reuse_ground_controls_from=None, reuse_jump_controls_from=None):
    """Apply explicit controls, remeasure unchanged pixels, and bind a receipt.

    controls: [{run:absolute-or-session-relative, points:[{frame_index,x,y,
    frame_sha256,evidence?}], event_frames?:{takeoff_frame,apex_frame,
    landing_frame,frame_sha256:{same three keys}}}]. Events are jump-only.
    Ground reuse sources are separate annotation JSONs with direct controls;
    jump reuse is an explicitly approved legacy finished session directory.
    """
    if source not in {"human","assistant_visual"}:
        raise IntegrationError("invalid_controls", "Review source must be human or assistant_visual")
    session=Session(directory)
    _local_file(session.path,session.directory)
    _local_file(session.directory/".session.lock",session.directory)
    with file_lock(session.directory/".session.lock"):
        context=_context(session.directory)
        state,candidate=context["state"],context["candidate"]
        records,input_hash=_controls(controls,context,source)
        comparison_before=read_json(context["comparison_path"])
        numerical=_numerical(comparison_before)
        measured={run:_measurement(run) for run in context["runs"]}
        raw={run:_raw(run) for run in context["runs"]}
        paths={session.path,context["comparison_path"],*(r/"analysis.json" for r in context["runs"]),*context["annotations"].values()}
        for path in paths:
            _local_file(path,session.directory)
        # Legacy jump reanalysis owns these predictable temporary names. A
        # stale or linked file must not be overwritten, followed, or deleted.
        staging=set()
        for runs in context["groups"]["jump"].values():
            for run in runs:
                staging.update((run/"analysis.manual-review.json",run/"analysis.manual-review.json.tmp",
                                Path(str(context["annotations"][run])+".tmp")))
        for path in staging:
            _local_file(path,session.directory)
            if path.exists():
                raise IntegrationError("stale_review_state","Existing review staging file must be resolved before review")
        backups={path:path.read_bytes() if path.exists() else None for path in paths}
        receipt_path=session.directory/"final"/f"ground-review-{uuid.uuid4().hex[:12]}.json"
        _local_file(receipt_path,session.directory)
        try:
            reused_ground=_reuse_ground(reuse_ground_controls_from,context)
            reused_jump=None
            jump_dirs={run:context["annotations"][run].parent for runs in context["groups"]["jump"].values() for run in runs}
            if reuse_jump_controls_from is not None:
                reused_jump=control_reuse.reuse_controls(reuse_jump_controls_from,context["groups"]["jump"],jump_dirs)
            for record in records:
                if record["mechanic"]=="ground":
                    _merge_ground(context["annotations"][record["run"]],record["points"])
                else:
                    evaluation.record_manual_controls(record["run"],jump_dirs[record["run"]],record["points"],record["event_frames"],source=source)
            for mechanic,groups in context["groups"].items():
                for runs in groups.values():
                    for run in runs:
                        if mechanic=="ground":
                            ground_analysis.analyze_run(run,context["annotations"][run])
                        else:
                            evaluation.reanalyze_controls(run,jump_dirs[run])
                        if _measurement(run)!=measured[run]:
                            raise IntegrationError("measurement_changed", "Manual review changed an image-derived measurement")
            _require_source(session.directory/"baseline",state["baseline_hashes"])
            _require_source(context["candidate_root"]/"project",candidate["source_hashes"])
            if any(_raw(run)!=raw[run] for run in context["runs"]):
                raise IntegrationError("raw_evidence_changed", "Raw capture changed during visual review")
            _verify_external(reused_ground["evidence_sha256"])
            if reused_jump is not None:
                control_reuse.verify_source_unchanged(reused_jump)
            comparison=_compare(context)
            if _numerical(comparison)!=numerical:
                raise IntegrationError("numerical_result_changed", "Manual review changed frozen numerical results")
            write_json(context["comparison_path"],comparison)
            proofs={str(run.relative_to(session.directory)):{"measurement_sha256":measured[run],"raw_evidence_sha256":raw[run],
                    "analysis_sha256":sha256(run/"analysis.json"),
                    "annotation_path":str(context["annotations"][run].relative_to(session.directory)),
                    "annotation_sha256":sha256(context["annotations"][run])} for run in context["runs"]}
            receipt={"schema_version":1,"experiment_id":GROUND_ID,"reviewed_at":utc_now(),"candidate_id":candidate["candidate_id"],
                     "source":source,"controls_file_sha256":input_hash,
                     "supplied_controls":[{**r,"run":str(r["run"])} for r in records],
                     "reused_ground_controls":reused_ground,"reused_jump_controls":reused_jump,
                     "measurements_unchanged":True,"raw_evidence_unchanged":True,"sources_unchanged":True,
                     "baseline_source_hashes":state["baseline_hashes"],"candidate_source_hashes":candidate["source_hashes"],
                     "candidate_metadata_sha256":sha256(context["candidate_root"]/"candidate.json"),
                     "runs":proofs,"numerical_sha256":numerical,"comparison_sha256":sha256(context["comparison_path"]),
                     "numerical_status":comparison["numerical_status"],"measurement_status":comparison["measurement_status"],
                     "manual_validation_status":comparison["manual_validation_status"],"status":comparison["status"]}
            write_json(receipt_path,receipt)
            state["final"].update(numerical_status=comparison["numerical_status"],measurement_status=comparison["measurement_status"],
                manual_validation_status=comparison["manual_validation_status"],overall_status=comparison["status"],
                reviewed_at=receipt["reviewed_at"],manual_review_path=str(receipt_path),manual_review_sha256=sha256(receipt_path))
            write_json(session.path,state)
        except BaseException:
            _restore(backups)
            receipt_path.unlink(missing_ok=True)
            raise
        return {"status":comparison["status"],"candidate_id":candidate["candidate_id"],
                "numerical_status":comparison["numerical_status"],"manual_validation_status":comparison["manual_validation_status"],
                "measurement_status":comparison["measurement_status"],"comparison_path":str(context["comparison_path"]),
                "review_path":str(receipt_path),"reviewed_run_count":len(context["runs"]),
                "reused_ground_controls":reused_ground,"reused_jump_controls":reused_jump}


def verify_review_receipt(directory):
    """Read-only integrity status; manual pass is a separate returned field."""
    try:
        session=Session(directory)
        _local_file(session.path,session.directory)
        state=session.read()
        final = state.get("final")
        if final is not None and not isinstance(final, dict):
            raise IntegrationError("invalid_review_receipt", "Final review metadata must be an object or null")
        if not final or not final.get("manual_review_path"):
            return {"status":"unverified","errors":["No independent ground review receipt"]}
        context=_context(session.directory)
        state,candidate=context["state"],context["candidate"]
        _local_file(state["final"]["manual_review_path"],session.directory)
        path=_inside(state["final"]["manual_review_path"],session.directory/"final",relative_base=session.directory)
        receipt=read_json(path)
        if (receipt.get("schema_version")!=1 or receipt.get("experiment_id")!=GROUND_ID
                or receipt.get("candidate_id")!=candidate["candidate_id"]
                or state["final"].get("manual_review_sha256")!=sha256(path)
                or any(receipt.get(key) is not True for key in ("measurements_unchanged","raw_evidence_unchanged","sources_unchanged"))
                or receipt.get("baseline_source_hashes")!=state["baseline_hashes"]
                or receipt.get("candidate_source_hashes")!=candidate["source_hashes"]
                or receipt.get("candidate_metadata_sha256")!=sha256(context["candidate_root"]/"candidate.json")):
            raise IntegrationError("invalid_review_receipt", "Review identity, source or receipt hash changed")
        expected={str(run.relative_to(session.directory)) for run in context["runs"]}
        if set(receipt.get("runs",{}))!=expected:
            raise IntegrationError("invalid_review_receipt", "Receipt does not bind every ground and jump capture")
        for run in context["runs"]:
            proof=receipt["runs"][str(run.relative_to(session.directory))]
            annotation=context["annotations"][run]
            if (proof.get("measurement_sha256")!=_measurement(run) or proof.get("raw_evidence_sha256")!=_raw(run)
                    or proof.get("analysis_sha256")!=sha256(run/"analysis.json")
                    or proof.get("annotation_path")!=str(annotation.relative_to(session.directory))
                    or proof.get("annotation_sha256")!=sha256(annotation)):
                raise IntegrationError("invalid_review_receipt", "Raw pixels, analysis or annotation changed after review")
        _verify_external(receipt.get("reused_ground_controls",{}).get("evidence_sha256",{}))
        if receipt.get("reused_jump_controls") is not None:
            control_reuse.verify_source_unchanged(receipt["reused_jump_controls"])
        comparison=read_json(context["comparison_path"])
        if receipt.get("comparison_sha256")!=sha256(context["comparison_path"]) or receipt.get("numerical_sha256")!=_numerical(comparison):
            raise IntegrationError("invalid_review_receipt", "Reviewed comparison changed")
        for key in ("numerical_status","measurement_status","manual_validation_status"):
            if comparison.get(key)!=receipt.get(key) or comparison.get(key)!=state["final"].get(key):
                raise IntegrationError("invalid_review_receipt", "Receipt status differs from canonical comparison")
        if comparison.get("status")!=receipt.get("status") or comparison.get("status")!=state["final"].get("overall_status"):
            raise IntegrationError("invalid_review_receipt", "Overall review status differs")
        return {"status":"pass","candidate_id":candidate["candidate_id"],"numerical_status":receipt["numerical_status"],
                "manual_validation_status":receipt["manual_validation_status"],"measurement_status":receipt["measurement_status"],
                "comparison_path":str(context["comparison_path"]),"review_path":str(path),"errors":[],
                "note":"Receipt integrity verified; use manual_validation_status for the independent measurement decision."}
    except (OSError,ValueError,KeyError,TypeError,IntegrationError) as exc:
        return {"status":"fail","errors":[str(exc)]}
