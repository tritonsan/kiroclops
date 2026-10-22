"""Post-hoc controlled contact diagnostic, separate from registered acceptance.

Run only after heart-02 terminal. Uses the existing private desktop/runtime lock.
All motor observations are private diagnostic evidence, never fitter/model input.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from kiroclops.integration.storage import copy_project, sha256, utc_now
from kiroclops.portable import runtime
from kiroclops.portable.session import Session

HERE = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write("\n")


def sources(label):
    if label == "original":
        return ROOT/"artifacts/phase2c/setup/baseline-final", None
    session = Session(ROOT/"sessions"/f"heart-heart-02-{label}-jump")
    state = session.verify()
    launch = read(session.directory/"kiro/launch.json")
    assert state["sealed_at"] and launch["status"] == "completed"
    candidate = session.verify_candidate(state["selected_candidate_id"])
    return session.directory/"candidates"/candidate["candidate_id"]/"project", candidate


def verify_recording(recording, config):
    assert recording["status"] == "ok" and recording["fps"] == 60
    assert recording["frame_count"] == config["frame_count"]
    assert len(recording["frames"]) == config["frame_count"]
    assert len(recording["cases"]) == len(config["cases"])
    for n, frame in enumerate(recording["frames"]):
        spec = config["cases"][n//60]
        step = n % 60
        actions = []
        if spec["press"] <= step < spec["press"]+4:
            actions.append("jump")
        if spec["direction_start"] <= step < spec["direction_end"]:
            actions.append("move_"+spec["side"])
        assert frame == {"frame": n, "tick": n+60, "case_id": spec["id"], "step": step, "actions": actions}
    results = []
    for case, spec in zip(recording["cases"], config["cases"]):
        assert case["id"] == spec["id"]
        assert [v["step"] for v in case["steps"]] == list(range(1,60))
        pre = case["steps"][spec["press"]-1]["pre"]
        post = case["steps"][spec["press"]-1]["post"]
        earlier = case["steps"][:spec["press"]-1]
        floor_loss = [v["step"] for v in earlier if v["pre"]["floor"] and not v["post"]["floor"]]
        valid = not pre["floor"] and pre["wall_only"] and pre["jump_edge"]
        if spec["kind"] == "overlap":
            valid = valid and pre["coyote_seconds"] > 0 and bool(floor_loss)
        else:
            valid = valid and pre["coyote_seconds"] == 0 and not any(v["post"]["floor"] for v in earlier)
        results.append({"id": spec["id"], "fixture_valid": bool(valid), "press": spec["press"],
                        "floor_loss_steps": floor_loss, "pre_jump": pre, "post_jump": post,
                        "response_steps": case["steps"][spec["press"]-1:spec["press"]+4]})
    return {"status": "pass" if all(v["fixture_valid"] for v in results) else "invalid_fixture", "cases": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", choices=["original","01","02","03"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--press", type=int, default=35)
    args = parser.parse_args()
    cohort_path = ROOT/"artifacts/phase2c/cohorts/heart-02/run.json"
    cohort = read(cohort_path)
    assert cohort.get("finished_at") and cohort["status"] != "running"
    for n in ("01", "02", "03"):
        for stage in ("jump", "ground"):
            session_dir = ROOT/"sessions"/f"heart-heart-02-{n}-{stage}"
            assert read(session_dir/"kiro/launch.json")["status"] == "completed"
            assert read(session_dir/"session.json")["active_job_id"] is None
    output = Path(args.output).resolve()
    assert output.is_relative_to(ROOT/"artifacts/phase2c/diagnostics")
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    source, candidate = sources(args.label)
    source_before = runtime.source_hashes(source)
    project = copy_project(source, output/"project")
    overlay = project/"kiroclops_heart_integrity"
    overlay.mkdir(exist_ok=False)
    for name in ("Integrity.tscn", "overlap.gd"):
        shutil.copy2(HERE/name, overlay/name)
    copied = runtime.source_hashes(project)
    assert {n:h for n,h in copied.items() if not n.startswith("kiroclops_heart_integrity/")} == source_before
    cases = []
    for kind in ("overlap", "wall_only"):
        for side in ("right", "left"):
            cases.append({"id": kind+"_"+side, "kind": kind, "side": side,
                          "start": len(cases)*60, "duration": 60, "press": args.press,
                          "direction_start": 10 if kind == "overlap" else 1,
                          "direction_end": args.press+4})
    capture = output/"capture"
    (capture/"frames").mkdir(parents=True)
    config = {"protocol":"posthoc-wall-coyote-v1", "frame_count":240,
              "output_dir":str(capture), "cases":cases}
    write(capture/"capture_config.json", config)
    declaration = {"schema_version":1, "declared_at":utc_now(), "engineering_only":True,
                   "label":args.label, "source_path":str(source), "source_hashes":source_before,
                   "candidate_source_sha256":candidate["source_sha256"] if candidate else None,
                   "cohort_terminal_path":str(cohort_path), "cohort_terminal_sha256":sha256(cohort_path),
                   "diagnostic_sources":{n:sha256(HERE/n) for n in ("run.py","Integrity.tscn","overlap.gd")},
                   "config_sha256":sha256(capture/"capture_config.json"),
                   "scope":"Static controlled ledge and wall, initial spawn, action inputs only. No timer, velocity, flag, or post-spawn position seeding. Does not assert reachability in the original normal map or all maps. Private motor evidence is not motion-fit/model input."}
    write(output/"declaration.json", declaration)
    try:
        imported = runtime.import_project(project, output/"import")
        native = runtime.run_adapter(project, capture, scene="res://kiroclops_heart_integrity/Integrity.tscn",
                                     config_path=capture/"capture_config.json", timeout=120)
        assert runtime.source_hashes(source) == source_before and runtime.source_hashes(project) == copied
        proof = runtime.verify_runtime_receipt(native["receipt_path"], project=project, expected_source_hashes=copied)
        assert proof["status"] == "pass", proof
        result = verify_recording(read(capture/"recording.json"), config)
        result.update(label=args.label, declared_protocol_sha256=sha256(output/"declaration.json"),
                      original_source_unchanged=True, diagnostic_copy_unchanged=True,
                      import_receipt=imported["receipt_path"], runtime_receipt=native["receipt_path"],
                      runtime_receipt_sha256=sha256(native["receipt_path"]), runtime_proof=proof,
                      recording_sha256=sha256(capture/"recording.json"),
                      png_hashes={p.name:sha256(p) for p in sorted((capture/"frames").glob("*.png"))},
                      elapsed_seconds=time.monotonic()-started, finished_at=utc_now())
    except Exception as error:
        result = {"status":"error", "error":str(error), "error_type":type(error).__name__,
                  "elapsed_seconds":time.monotonic()-started, "finished_at":utc_now()}
    write(output/"result.json", result)
    print(json.dumps({"output":str(output), "status":result["status"], "elapsed_seconds":result["elapsed_seconds"],
                      "cases":[{"id":c["id"], "valid":c["fixture_valid"], "vy":c["post_jump"]["velocity"][1]} for c in result.get("cases",[])], "error":result.get("error")}), flush=True)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
