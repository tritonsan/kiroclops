"""Native positive/negative contact-v2 qualification; explicit operator run only.

Creates disposable copies, never changes a session/controller original. All
engines use private runtime-v3. Do not run concurrently with another engine.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.integration import normal_gameplay_v2 as probe
from kiroclops.integration import isolated_runtime_v3 as runtime
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, utc_now, write_json

PLAYER = "source/characters/player/player.gd"
FAULT_VARIABLE = "var _contact_probe_fault_applied: bool = false\n"
FAULT = """\t# Deliberately broken disposable qualification control, never a model patch.
\tif not _contact_probe_fault_applied and self.state_callback and str(self.state_callback.get_object().name) == "Fall" and not self.is_on_floor() and self.velocity.y >= 0.0 and Input.is_action_pressed("ui_right") and not Input.is_action_pressed("ui_up"):
\t\tself.velocity.y = -300.0
\t\t_contact_probe_fault_applied = true
"""


def inject_negative(project, expected):
    """A one-shot unrequested upward impulse in an external copied controller."""
    project = probe.background._ordinary(project)
    allowed = (ROOT / "artifacts/model-trials/preflight").resolve()
    if not project.is_relative_to(allowed) or project_hashes(project) != expected:
        raise ValueError("Negative control must be an exact disposable preflight source copy")
    path = project / PLAYER
    original = path.read_bytes()
    text = original.decode("utf-8").replace("\r\n", "\n")
    anchor = "func _physics_process(delta: float) -> void:\n"
    if text.count(anchor) != 1 or "_contact_probe_fault_applied" in text:
        raise ValueError("Unexpected controller structure for the deliberate negative control")
    changed = text.replace(anchor, FAULT_VARIABLE + "\n" + anchor + FAULT, 1)
    newline = "\r\n" if b"\r\n" in original else "\n"
    path.write_bytes(changed.replace("\n", newline).encode("utf-8"))
    after = project_hashes(project)
    if set(after) != set(expected) or {name for name in after if after[name] != expected[name]} != {PLAYER}:
        raise ValueError("Negative control changed more than its one declared controller file")
    return after, {"kind": "deliberate_one_shot_unrequested_airborne_ascent",
        "source_file": PLAYER, "before_sha256": hashlib.sha256(original).hexdigest(), "after_sha256": sha256(path),
        "fault_sha256": hashlib.sha256(FAULT.encode()).hexdigest(),
        "condition": "existing Fall while airborne, nonnegative vertical velocity, right held and jump released",
        "scope": "Only a disposable intentionally bad test control; never applied to a real model candidate"}


def qualify(output):
    output = probe.background._ordinary(output)
    allowed = (ROOT / "artifacts/model-trials/preflight").resolve()
    if not output.is_relative_to(allowed) or output == allowed or output.exists():
        raise ValueError("Use a fresh named output beneath artifacts/model-trials/preflight")
    original = read_json(ROOT / "sessions/cli-ground-01/session.json")
    known = read_json(ROOT / "sessions/cli-repair-v2-01/candidates/candidate-01/candidate.json")
    parent = ROOT / "sessions/cli-ground-01/baseline"
    wall = ROOT / "sessions/cli-repair-v2-01/candidates/candidate-01/project"
    cases = [("parent", parent, original["baseline_hashes"], "pass", False),
             ("known-wall-slide", wall, known["source_hashes"], "pass", False),
             ("unrequested-ascent", wall, known["source_hashes"], "fail", True)]
    result = {"schema_version": 1, "kind": "normal-gameplay-contact-v2-qualification", "status": "running",
        "started_at": utc_now(), "protocol": probe.protocol_record(), "script_sha256": sha256(__file__),
        "runtime_source_sha256": sha256(runtime.__file__), "cases": [], "canonical_experiment_acceptance": False}
    output.mkdir(parents=True)
    write_json(output / "preregistered-controls.json", {**result, "cases": [
        {"id": name, "source": str(source), "source_hashes": hashes, "expected_status": expected, "negative": negative}
        for name, source, hashes, expected, negative in cases]})
    try:
        with runtime.isolated_runtime(project_roots=[output]):
            for name, source, hashes, expected_status, negative in cases:
                if project_hashes(source) != hashes:
                    raise ValueError("Original control source changed: " + name)
                folder = output / name
                folder.mkdir()
                project = copy_project(source, folder / "project")
                copied = project_hashes(project)
                if copied != hashes:
                    raise ValueError("Control source copy differs")
                mutation = None
                if negative:
                    copied, mutation = inject_negative(project, hashes)
                run = probe.run_probe(project, folder / "capture", disposable_root=folder,
                    expected_source_hashes=copied, video=True)
                observed = probe.verify_probe(project, run, expected_source_hashes=copied)
                receipt = read_json(run / "probe.json")
                private = runtime.verify_runtime_receipt(run / "engine.isolated-runtime-v3/bridge.json",
                    project=project, expected_source_hashes=receipt["full_source_hashes_after"])
                preserved = project_hashes(source) == hashes
                special = (not negative or observed.get("checks", {}).get("new_ascent_requires_requested_contact") is False)
                # The positive regression must actually exercise wall contact;
                # a route that never touches a wall cannot qualify this repair.
                wall_observed = name != "known-wall-slide" or bool(observed.get("contact_supported_wall_slide_frames"))
                passed = (observed["status"] == expected_status and private["status"] == "pass"
                    and preserved and special and wall_observed)
                result["cases"].append({"id": name, "status": "pass" if passed else "fail",
                    "expected_status": expected_status, "actual": observed, "runtime": private,
                    "source": str(source), "source_hashes": hashes, "source_preserved": preserved,
                    "project": str(project), "output": str(run), "input_source_hashes": copied,
                    "negative_mutation": mutation, "required_semantic_event_observed": special and wall_observed})
                write_json(output / "progress.json", result)
                print(json.dumps({"case": name, "expected": expected_status, "actual": observed["status"],
                                  "qualification": "pass" if passed else "fail"}), flush=True)
        stable = (result["protocol"] == probe.protocol_record() and result["script_sha256"] == sha256(__file__)
                  and result["runtime_source_sha256"] == sha256(runtime.__file__))
        result.update(status="pass" if stable and all(c["status"] == "pass" for c in result["cases"]) else "fail",
                      implementation_unchanged=stable, finished_at=utc_now())
    except BaseException as exc:
        result.update(status="error", error_type=type(exc).__name__, error=str(exc), finished_at=utc_now())
        write_json(output / "result.json", result)
        raise
    write_json(output / "result.json", result)
    return 0 if result["status"] == "pass" else 2


def verify_qualification(output):
    """Read-only native replay of all three controls, never outer-status trust."""
    output = probe.background._ordinary(output)
    path = output / "result.json"
    if not path.is_file():
        return {"status": "pending", "reason": "Native contact-v2 qualification has not completed"}
    try:
        result = read_json(path)
        expected = {"parent": "pass", "known-wall-slide": "pass", "unrequested-ascent": "fail"}
        if (result.get("kind") != "normal-gameplay-contact-v2-qualification"
                or result.get("protocol") != probe.protocol_record()
                or result.get("script_sha256") != sha256(__file__)
                or result.get("runtime_source_sha256") != sha256(runtime.__file__)
                or result.get("canonical_experiment_acceptance") is not False
                or len(result.get("cases", [])) != 3 or {c.get("id") for c in result["cases"]} != set(expected)):
            raise ValueError("Qualification implementation, protocol or three controls differ")
        checks = []
        for case in result["cases"]:
            name, project, capture = case["id"], output / case["id"] / "project", output / case["id"] / "capture"
            if (Path(case["project"]).resolve() != project or Path(case["output"]).resolve() != capture
                    or case["expected_status"] != expected[name]
                    or project_hashes(case["source"]) != case["source_hashes"]):
                raise ValueError("Qualification control source or path identity changed")
            inputs = case["source_hashes"]
            if name == "unrequested-ascent":
                original_bytes = (Path(case["source"]) / PLAYER).read_bytes()
                text = original_bytes.decode("utf-8").replace("\r\n", "\n")
                anchor = "func _physics_process(delta: float) -> void:\n"
                if text.count(anchor) != 1 or "_contact_probe_fault_applied" in text:
                    raise ValueError("Negative control's source no longer has the expected insertion point")
                changed = text.replace(anchor, FAULT_VARIABLE + "\n" + anchor + FAULT, 1)
                data = changed.replace("\n", "\r\n" if b"\r\n" in original_bytes else "\n").encode("utf-8")
                inputs = {**inputs, PLAYER: hashlib.sha256(data).hexdigest()}
            if case["input_source_hashes"] != inputs:
                raise ValueError("Negative source transformation or positive input bytes differ")
            actual = probe.verify_probe(project, capture, expected_source_hashes=inputs)
            receipt = read_json(capture / "probe.json")
            private = runtime.verify_runtime_receipt(capture / "engine.isolated-runtime-v3/bridge.json",
                project=project, expected_source_hashes=receipt["full_source_hashes_after"])
            semantic = (name != "known-wall-slide" or bool(actual.get("contact_supported_wall_slide_frames"))) and (
                name != "unrequested-ascent" or actual.get("checks", {}).get("new_ascent_requires_requested_contact") is False)
            passed = actual["status"] == expected[name] and private["status"] == "pass" and semantic
            checks.append({"id": name, "status": "pass" if passed else "fail", "actual": actual, "runtime": private})
        passed = all(c["status"] == "pass" for c in checks)
        if result.get("status") != ("pass" if passed else "fail"):
            raise ValueError("Saved qualification outcome does not match the fresh checks")
        return {"status": "pass" if passed else "fail", "path": str(path), "sha256": sha256(path),
                "finished_at": result["finished_at"], "checks": checks}
    except (OSError, KeyError, ValueError, TypeError, RuntimeError) as error:
        return {"status": "fail", "reason": str(error)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(qualify(parser.parse_args().output))
