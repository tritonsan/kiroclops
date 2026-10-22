"""Immutable multi-clip reference intake before any model work.

Coverage is the user's declared scenario coverage, not a behavior acceptance
result. This module copies pixels and selection data only, never target code;
it neither records a capture nor starts a model. UI/CLI callers supply inputs.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from kiroclops.visual.package import copy_package, load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from .session import _lock, _ordinary


SCHEMA_VERSION = 1
SUPPORTED_BEHAVIORS = {
    "game": ("short_jump", "long_jump", "horizontal_movement", "preserved_jump"),
    "web_panel": ("open", "close", "interrupt", "reopen", "drag"),
    "svg": ("add_node", "remove_node", "drag", "select_node"),
}
MISSING_REQUESTS = {
    "short_jump": "Add a reference clip that shows a jump made with a short key press.",
    "long_jump": "Add a reference clip that shows a jump made with a long key press.",
    "horizontal_movement": "Add a reference clip that shows horizontal movement.",
    "preserved_jump": "Add a reference clip that shows the jump to be preserved after horizontal movement.",
    "open": "Add a reference clip in which the panel opens.",
    "close": "Add a reference clip in which the panel closes.",
    "interrupt": "Add a reference clip in which the panel movement is interrupted before completing or the panel is closed early.",
    "reopen": "Add a reference clip in which the panel is reopened after being closed.",
    "drag": "Add a reference clip in which the item is dragged and released.",
    "add_node": "Add a reference clip that shows a new node being added.",
    "remove_node": "Add a reference clip that shows a node being removed.",
    "select_node": "Add a reference clip that shows a node being selected.",
}
_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def _domain(value):
    value = {"panel": "web_panel", "web": "web_panel"}.get(value, value)
    if value not in SUPPORTED_BEHAVIORS:
        raise VisualError("unsupported_domain", "Game, panel and SVG references are supported.")
    return value


def _behaviors(values, domain):
    if not isinstance(values, (list, tuple)) or not values or any(not isinstance(v, str) for v in values):
        raise VisualError("invalid_behavior", "Select at least one supported behavior.")
    if len(values) != len(set(values)) or not set(values) <= set(SUPPORTED_BEHAVIORS[domain]):
        raise VisualError("unsupported_behavior", "Behaviors must be supported for this domain and unique.")
    return list(values)


def _exclusive_json(path, value):
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path = _ordinary(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except FileExistsError as error:
        raise VisualError("output_exists", "An earlier package or receipt cannot be changed.") from error


def _load(root):
    root = _ordinary(root)
    original = read_json(root / "draft.json")
    binding = read_json(root / "draft-binding.json")
    if (original.get("kind") != "MultiReferenceIntakeDraft" or original.get("schema_version") != SCHEMA_VERSION
            or digest(original) != binding.get("draft_sha256")):
        raise VisualError("suite_changed", "The original request of the reference package has changed.")
    _behaviors(original.get("required_behaviors"), _domain(original.get("domain")))
    state = read_json(root / "state.json")
    revision = state.get("revision")
    if type(revision) is not int or revision < 0 or state.get("draft_sha256") != digest(original):
        raise VisualError("suite_changed", "The revision binding of the reference package has changed.")
    previous = read_json(_ordinary(root / "revisions" / f"{revision:06}.json"))
    if state != previous:
        raise VisualError("suite_changed", "The versioned record of the reference package has changed.")
    if not isinstance(state.get("clips"), list):
        raise VisualError("suite_changed", "The reference clip record is invalid.")
    return root, original, state


def _can_edit(root):
    if (root / "model-started.json").exists():
        raise VisualError("model_already_started", "References cannot be added or re-sealed after the model has started.")
    if (root / "suite.json").exists():
        raise VisualError("suite_frozen", "A sealed reference package cannot be changed; open a new draft.")


def _pixels(manifest):
    # Changing only time origins, input declarations or a scenario name cannot
    # turn the same raw video frames into independent calibration/validation.
    return digest({"image_size": manifest["image_size"],
                   "frame_sha256": [frame["sha256"] for frame in manifest["frames"]]})


def _target(project):
    return {"domain": project["domain"], "entry": project["entry"], "source_sha256": project["source_sha256"]}


def _mapping(value, selection, domain):
    references = {obj["id"] for obj in selection["objects"]}
    allowed = {"character"} if domain == "game" else {"handle", "bookmark", "star"} if domain == "web_panel" else set("abcdef")
    if (not isinstance(value, dict) or set(value) != references
            or any(not isinstance(v, str) for v in value.values())
            or len(set(value.values())) != len(value) or not set(value.values()) <= allowed):
        raise VisualError("invalid_binding", "Map each selected object to a separate supported target object.")
    if domain == "game" and len(value) != 1:
        raise VisualError("invalid_binding", "A game reference requires a single character mapping.")
    return deepcopy(value)


def _verify_clip(root, row):
    name = row.get("scenario_id")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise VisualError("suite_changed", "The reference scenario name has changed.")
    directory = _ordinary(root / "clips" / name)
    record = read_json(directory / "clip.json")
    if digest(record) != row.get("clip_sha256") or record.get("scenario_id") != name:
        raise VisualError("clip_changed", "The immutable source binding of the clip has changed.")
    expected = record.get("file_hashes", {})
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    if not expected or actual != set(expected) | {"clip.json"}:
        raise VisualError("clip_changed", "Clip files are missing or an unexpected file was added.")
    for relative, sha in expected.items():
        if file_sha256(_ordinary(safe_path(directory, relative))) != sha:
            raise VisualError("clip_changed", "The reference footage or object selection has changed.")
    manifest = load_package(_ordinary(directory / "package"))
    selection = read_json(_ordinary(directory / "selection.json"))
    if (manifest["package_sha256"] != record.get("package_sha256")
            or _pixels(manifest) != record.get("pixels_sha256")
            or digest(selection) != record.get("selection_sha256")
            or digest(manifest["inputs"]) != record.get("inputs_sha256")):
        raise VisualError("clip_changed", "The clip identity or input timeline has changed.")
    return record


def _coverage(root, original, state):
    records = [_verify_clip(root, row) for row in state["clips"]]
    covered = {behavior for record in records for behavior in record["behaviors"]}
    required = original["required_behaviors"]
    missing = [behavior for behavior in required if behavior not in covered]
    return {"status": "needs_reference_clips" if missing else "coverage_ready",
            "domain": original["domain"], "goal": original["goal"],
            "required_behaviors": list(required), "covered_behaviors": sorted(covered),
            "missing_behaviors": missing, "requests": [MISSING_REQUESTS[value] for value in missing],
            "clips": [{"scenario_id": record["scenario_id"], "role": record["role"],
                       "behaviors": list(record["behaviors"]), "input_provenance": record["input_provenance"]}
                      for record in records],
            "model_started": (root / "model-started.json").exists(),
            "coverage_basis": "user_declared_behaviors_in_distinct_bound_pixel_clips",
            "behavior_accepted": False, "benchmark_qualified": False}


def draft(root, domain, goal, required_behaviors):
    """Create a fresh, versioned requirement draft from plain caller inputs."""
    root = _ordinary(root)
    domain = _domain(domain)
    if not isinstance(goal, str) or not goal.strip():
        raise VisualError("invalid_goal", "Describe the behavior you want to transfer.")
    required = _behaviors(required_behaviors, domain)
    if root.exists():
        raise VisualError("output_exists", "Choose a new folder for each reference draft.")
    root.mkdir(parents=True)
    original = {"kind": "MultiReferenceIntakeDraft", "schema_version": SCHEMA_VERSION,
                "suite_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                "domain": domain, "goal": goal.strip(), "required_behaviors": required,
                "model_started": False, "supported_behaviors": list(SUPPORTED_BEHAVIORS[domain])}
    state = {"schema_version": SCHEMA_VERSION, "revision": 0, "draft_sha256": digest(original),
             "clips": [], "object_map": None, "target_identity": None}
    _exclusive_json(root / "draft.json", original)
    _exclusive_json(root / "draft-binding.json", {"draft_sha256": digest(original)})
    _exclusive_json(root / "revisions/000000.json", state)
    _exclusive_json(root / "state.json", state)
    return {"status": "draft", "root": str(root), "draft": original, "coverage": _coverage(root, original, state)}


def add_clip(root, intake_dir, scenario_id, behaviors, object_map, role="calibration"):
    """Validate an existing selected intake and copy only its immutable media."""
    root, original, _ = _load(root)
    if not isinstance(scenario_id, str) or not _NAME.fullmatch(scenario_id):
        raise VisualError("unsafe_path", "The scenario name may contain only letters, digits, underscores and hyphens.")
    if role not in ("calibration", "validation"):
        raise VisualError("invalid_role", "The clip role must be calibration or held-out validation.")
    chosen = _behaviors(behaviors, original["domain"])
    intake_dir = _ordinary(intake_dir)
    if intake_dir.is_relative_to(root) or root.is_relative_to(intake_dir):
        raise VisualError("unsafe_path", "The original video intake and the reference package must be in separate folders.")
    with _lock(root / ".suite.lock", timeout=5):
        root, original, state = _load(root)
        _can_edit(root)
        _coverage(root, original, state)
        from .media_session import _intake
        intake, project, manifest, selection = _intake(intake_dir)
        if intake.get("model_started") is True:
            raise VisualError("model_already_started", "An intake that has already been used for a model cannot count as new reference preparation.")
        if project["domain"] != original["domain"]:
            raise VisualError("domain_mismatch", "The new clip must belong to the same domain as the draft.")
        mapping = _mapping(object_map, selection, original["domain"])
        if manifest["inputs"]["provenance"] == "unknown" or not manifest["inputs"]["events"]:
            return {"status": "needs_input_binding", "scenario_id": scenario_id, "model_started": False,
                    "inferred_events": False, "clip_added": False,
                    "request": "State the start and end times of the key press or interaction in the video, then prepare a new intake."}
        first, last = manifest["frames"][0]["time_s"], manifest["frames"][-1]["time_s"]
        if any(not first <= event["time_s"] < last for event in manifest["inputs"]["events"]):
            raise VisualError("invalid_input_timeline", "All interaction times must lie within the actual clip.")
        identity = _target(project)
        if state["object_map"] is not None and state["object_map"] != mapping:
            raise VisualError("mapping_changed", "Keep the same object IDs and target mapping in the new clip.")
        if state["target_identity"] is not None and state["target_identity"] != identity:
            raise VisualError("target_changed", "The new clip must be bound to the same unchanged target source version.")
        pixels_sha = _pixels(manifest)
        for row in state["clips"]:
            record = _verify_clip(root, row)
            if row["scenario_id"] == scenario_id:
                raise VisualError("duplicate_scenario", "The scenario name is already used in this package.")
            if record["pixels_sha256"] == pixels_sha:
                raise VisualError("duplicate_source", "The same image sequence cannot count as a new or held-out reference.")
        directory = _ordinary(root / "clips" / scenario_id)
        if directory.exists():
            raise VisualError("output_exists", "Earlier or incomplete clip evidence is preserved; choose a new scenario name.")
        copy_package(intake_dir / "package", directory / "package")
        _exclusive_json(directory / "selection.json", selection)
        # Revalidate the original after copying: source, selection or request
        # changes during preparation must not publish a completed clip.
        after = _intake(intake_dir)
        if digest([intake, project, manifest, selection]) != digest(list(after)):
            raise VisualError("source_changed", "The source or selection changed while the intake was being copied.")
        record = {"kind": "MultiReferenceClip", "schema_version": SCHEMA_VERSION,
                  "scenario_id": scenario_id, "role": role, "behaviors": chosen,
                  "package": "package", "selection": "selection.json",
                  "package_sha256": manifest["package_sha256"], "pixels_sha256": pixels_sha,
                  "selection_sha256": digest(selection), "inputs_sha256": digest(manifest["inputs"]),
                  "input_provenance": manifest["inputs"]["provenance"], "object_map": mapping,
                  "target_identity": identity, "intake_request_sha256": digest(intake["request"]),
                  "target_source_included": False, "model_started": False,
                  "file_hashes": {path.relative_to(directory).as_posix(): file_sha256(_ordinary(path))
                                  for path in sorted(directory.rglob("*")) if path.is_file()}}
        _exclusive_json(directory / "clip.json", record)
        _verify_clip(root, {"scenario_id": scenario_id, "clip_sha256": digest(record)})
        state["clips"].append({"scenario_id": scenario_id, "clip_sha256": digest(record)})
        state.update(revision=state["revision"] + 1, object_map=mapping, target_identity=identity)
        _exclusive_json(root / "revisions" / f"{state['revision']:06}.json", state)
        write_json(root / "state.json", state)
        return {"status": "clip_added", "clip": record, "coverage": _coverage(root, original, state)}


def coverage(root):
    """Report missing references in English without inferring unseen inputs."""
    root = _ordinary(root)
    with _lock(root / ".suite.lock", timeout=5):
        root, original, state = _load(root)
        return _coverage(root, original, state)


def _frozen(root):
    suite = read_json(_ordinary(root / "suite.json"))
    binding = read_json(_ordinary(root / "suite-binding.json"))
    if digest(suite) != binding.get("suite_sha256"):
        raise VisualError("suite_changed", "The sealed reference package has changed.")
    return suite


def freeze(root):
    """Seal intake coverage only; model execution still needs caller wiring."""
    root = _ordinary(root)
    with _lock(root / ".suite.lock", timeout=5):
        root, original, state = _load(root)
        if (root / "model-started.json").exists():
            raise VisualError("model_already_started", "The reference package cannot be sealed after the model has started.")
        current = _coverage(root, original, state)
        if current["missing_behaviors"]:
            return {**current, "frozen": False}
        if (root / "suite.json").exists():
            suite = _frozen(root)
            if suite["draft_sha256"] != digest(original) or suite["state_sha256"] != digest(state):
                raise VisualError("suite_changed", "The sealed package does not belong to the current draft.")
            return suite
        suite = {"kind": "MultiReferenceIntakeSuite", "schema_version": SCHEMA_VERSION,
                 "status": "suite_ready_intake", "suite_id": original["suite_id"], "domain": original["domain"],
                 "goal": original["goal"], "required_behaviors": original["required_behaviors"],
                 "draft_sha256": digest(original), "state_sha256": digest(state), "clips": deepcopy(state["clips"]),
                 "object_map": deepcopy(state["object_map"]), "target_identity": deepcopy(state["target_identity"]),
                 "coverage": current, "model_started": False, "frozen": True,
                 "execution_ready": False, "behavior_accepted": False, "benchmark_qualified": False}
        _exclusive_json(root / "suite.json", suite)
        _exclusive_json(root / "suite-binding.json", {"suite_sha256": digest(suite)})
        return suite


def mark_model_started(root):
    """Record a caller-reported one-way start; do not launch or alter a suite."""
    root = _ordinary(root)
    with _lock(root / ".suite.lock", timeout=5):
        root, original, state = _load(root)
        _coverage(root, original, state)
        path = root / "model-started.json"
        if path.exists():
            marker = read_json(_ordinary(path))
            if marker.get("draft_sha256") != digest(original) or marker.get("model_started") is not True:
                raise VisualError("suite_changed", "The model start record has changed.")
            return marker
        suite = _frozen(root) if (root / "suite.json").exists() else None
        marker = {"kind": "MultiReferenceModelStart", "schema_version": SCHEMA_VERSION,
                  "model_started": True, "caller_reported": True, "draft_sha256": digest(original),
                  "state_sha256": digest(state), "suite_sha256": digest(suite) if suite else None,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        _exclusive_json(path, marker)
        return marker
