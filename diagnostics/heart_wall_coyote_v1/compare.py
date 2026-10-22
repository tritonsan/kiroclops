"""Replay separate diagnostic receipts and compare observed wall responses."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from kiroclops.integration.storage import sha256, utc_now
from kiroclops.portable import runtime
from diagnostics.heart_wall_coyote_v1.run import read, write, verify_recording


def main():
    base = Path(sys.argv[1]).resolve()
    all_runs = {}
    evidence = {}
    for name in ("original", "chain01", "chain02", "chain03"):
        directory = base/name
        declaration = read(directory/"declaration.json")
        result = read(directory/"result.json")
        config = read(directory/"capture/capture_config.json")
        actual = verify_recording(read(directory/"capture/recording.json"), config)
        assert result["status"] == actual["status"] == "pass"
        assert result["cases"] == actual["cases"]
        assert sha256(directory/"declaration.json") == result["declared_protocol_sha256"]
        assert sha256(directory/"capture/recording.json") == result["recording_sha256"]
        assert sha256(directory/"capture/capture_config.json") == declaration["config_sha256"]
        assert runtime.source_hashes(declaration["source_path"]) == declaration["source_hashes"]
        project_sources = runtime.source_hashes(directory/"project")
        assert {n:h for n,h in project_sources.items() if not n.startswith("kiroclops_heart_integrity/")} == declaration["source_hashes"]
        for filename, digest in declaration["diagnostic_sources"].items():
            assert sha256(Path(__file__).parent/filename) == digest
        assert runtime.verify_runtime_receipt(result["runtime_receipt"], project=directory/"project", expected_source_hashes=project_sources)["status"] == "pass"
        for filename, digest in result["png_hashes"].items():
            assert sha256(directory/"capture/frames"/filename) == digest
        all_runs[name] = (declaration, config, actual)
        evidence[str(directory/"result.json")] = sha256(directory/"result.json")
        evidence[str(directory/"declaration.json")] = sha256(directory/"declaration.json")
    baseline = all_runs["original"]
    comparisons = []
    for name in ("chain01", "chain02", "chain03"):
        declaration, config, result = all_runs[name]
        assert config["cases"] == baseline[1]["cases"]
        cases = []
        for expected, actual in zip(baseline[2]["cases"], result["cases"]):
            assert actual["id"] == expected["id"]
            # Baseline and jump candidates have identical ground and air-x laws.
            # Compare raw engine float observations exactly, not a new acceptance tolerance.
            pre_equal = actual["pre_jump"] == expected["pre_jump"]
            steps = []
            for before, after in zip(expected["response_steps"], actual["response_steps"]):
                assert before["step"] == after["step"]
                steps.append({"step":before["step"],
                              "baseline_velocity":before["post"]["velocity"],
                              "candidate_velocity":after["post"]["velocity"],
                              "velocity_equal":before["post"]["velocity"] == after["post"]["velocity"],
                              "position_equal":before["post"]["position"] == after["post"]["position"]})
            cases.append({"id":actual["id"], "same_pre_jump_state":pre_equal,
                          "wall_response_preserved":pre_equal and all(s["velocity_equal"] and s["position_equal"] for s in steps),
                          "steps":steps})
        comparisons.append({"label":name, "candidate_source_sha256":declaration["candidate_source_sha256"],
                            "player_sha256":declaration["source_hashes"]["player.gd"],
                            "wall_response_preserved":all(c["wall_response_preserved"] for c in cases), "cases":cases})
    summary = {"schema_version":1, "diagnostic":"posthoc-wall-coyote-v1", "verified_at":utc_now(),
               "proof_status":"pass", "scope":"Input-only reachability in this static controlled fixture. Four cases, one replay per original/selected jump controller. Not a registered acceptance replacement or normal-map/all-map guarantee.",
               "motor_observations_used_for_fit_or_model":False,
               "controller_or_registered_protocol_modified":False,
               "comparison":"Exact equal pre-jump state and five native post-jump velocity/position samples, launch plus four following physics steps.",
               "comparisons":comparisons, "evidence_files":evidence,
               "comparison_source_sha256":sha256(Path(__file__))}
    path = base/"summary.json"
    write(path, summary)
    print(json.dumps({"path":str(path),"sha256":sha256(path),"proof_status":"pass", "controllers":[{"label":r["label"],"preserved":r["wall_response_preserved"],"cases":[{"id":c["id"],"preserved":c["wall_response_preserved"]} for c in r["cases"]]} for r in comparisons]}))


if __name__ == "__main__":
    main()
