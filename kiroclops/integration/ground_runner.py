"""Phase 2B action-schedule captures, isolated from frozen jump experiments.

Game controllers are never patched here. The original character is instantiated
by a separate fixture; only rendered pixels, inputs and optional state names are
recorded. All engine launches share the legacy cross-process Godot lock.
"""
from __future__ import annotations

from copy import deepcopy
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from ..capture import engine_path, process_options
from ..common import ROOT, read_json, sha256, write_json
from . import runner as legacy

RunnerError = legacy.RunnerError
source_hashes = legacy.source_hashes
FPS, FRAME_COUNT, WARMUP = 60, 240, 60
ACTIONS = {"left", "right", "jump"}
_REQUIRED = {
    "tiny": ("project.godot", "src/actor/Player.gd", "src/actor/Actor.gd", "src/actor/Player.tscn", "src/map/1-1.tscn"),
    "slay": legacy._REQUIRED_FILES,
}


def _game(game):
    if not isinstance(game, str) or game not in _REQUIRED:
        raise RunnerError("invalid_config", "game must be tiny or slay")
    return game


def _project_path(project, game):
    _game(game)
    project = Path(project).resolve()
    if not project.is_dir() or any(not (project / name).is_file() for name in _REQUIRED[game]):
        raise RunnerError("invalid_project", f"Expected a complete {game} project snapshot")
    for protected in (ROOT / "vendor", ROOT / "work"):
        if project.is_relative_to(protected.resolve()):
            raise RunnerError("protected_project", "Use a fresh experiment copy, never vendor/work")
    return project


def _adapter(game):
    return ROOT / "adapters" / ("ground-godot3" if game == "tiny" else "ground-godot4")


def _custom_dir(game):
    # Slay's existing jump fixture remains usable on this same immutable tree.
    return legacy._CUSTOM_DIR if game == "slay" else 'config/custom_user_dir_name="KiroClops/phase2b-tiny"'


def prepare_project(project: Path, game="slay") -> None:
    """Prepare a fresh copy once, before recording its baseline source hashes."""
    project = _project_path(project, game)
    source_hashes(project)
    if (project.parent / "candidate.json").exists():
        raise RunnerError("protected_project", "Never prepare an already recorded candidate snapshot")
    for parent in project.parents:
        state_path = parent / "session.json"
        if state_path.is_file():
            state = read_json(state_path)
            if state.get("sealed_at") or state.get("baseline_hashes"):
                raise RunnerError("protected_project", "Prepare only before session source hashes are frozen")
            break
        if parent == ROOT:
            break
    adapter = _adapter(game)
    for name in ("Capture.tscn", "capture.gd"):
        original, target = adapter / name, project / "kiroclops_ground" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or sha256(target) != sha256(original):
            shutil.copy2(original, target)
    path = project / "project.godot"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if "[application]" not in lines:
        raise RunnerError("invalid_project", "project.godot has no application section")
    custom = _custom_dir(game)
    if "config/use_custom_user_dir=true" not in lines or custom not in lines:
        lines = [line for line in lines if not line.startswith(("config/use_custom_user_dir=", "config/custom_user_dir_name="))]
        index = lines.index("[application]") + 1
        if game == "tiny":
            # Godot 3 serializes these built-in settings after main_scene.
            # Use that order before import so the editor does not rewrite it.
            main_scene = next((i for i, line in enumerate(lines) if line.startswith("run/main_scene=")), None)
            if main_scene is not None:
                index = main_scene + 1
        lines[index:index] = ["config/use_custom_user_dir=true", custom]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _assert_prepared(project, game)


def _assert_prepared(project, game):
    lines = (project / "project.godot").read_text(encoding="utf-8-sig").splitlines()
    if "config/use_custom_user_dir=true" not in lines or _custom_dir(game) not in lines:
        raise RunnerError("not_prepared", "Prepare the project before freezing its source hashes")
    expected = {name: sha256(_adapter(game) / name) for name in ("Capture.tscn", "capture.gd")}
    if source_hashes(project / "kiroclops_ground") != expected:
        raise RunnerError("fixture_changed", "Ground fixture differs from the frozen experiment adapter")


def canonical_scenario(scenario):
    """Validate and copy a logical schedule; all releases precede same-tick presses."""
    if not isinstance(scenario, dict):
        raise RunnerError("invalid_config", "scenario must be an object")
    required = {"id", "split", "repeat", "spawn_offset", "events"}
    if not required <= scenario.keys() or set(scenario) - required - {"frame_count", "warmup_ticks"}:
        raise RunnerError("invalid_config", "Unexpected scenario fields")
    value = deepcopy(scenario)
    value.setdefault("frame_count", FRAME_COUNT)
    value.setdefault("warmup_ticks", WARMUP)
    if (not isinstance(value["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["id"])
            or not isinstance(value["split"], str) or value["split"] not in {"calibration", "validation", "smoke", "regression"}
            or type(value["repeat"]) is not int or value["repeat"] not in {1, 2, 3}
            or type(value["frame_count"]) is not int or value["frame_count"] != FRAME_COUNT
            or type(value["warmup_ticks"]) is not int or value["warmup_ticks"] != WARMUP
            or type(value["spawn_offset"]) not in {int, float} or not math.isfinite(value["spawn_offset"])
            or abs(value["spawn_offset"]) > 48 or not isinstance(value["events"], list)
            or len(value["events"]) > 120):
        raise RunnerError("invalid_config", "Invalid deterministic scenario")
    events = value["events"]
    for event in events:
        if (not isinstance(event, dict) or set(event) != {"tick", "action", "pressed"}
                or type(event["tick"]) is not int or not 0 <= event["tick"] < FRAME_COUNT
                or not isinstance(event["action"], str) or event["action"] not in ACTIONS or type(event["pressed"]) is not bool):
            raise RunnerError("invalid_config", "Malformed logical input event")
    events.sort(key=lambda event: (event["tick"], event["pressed"], event["action"]))
    active, seen = set(), set()
    for event in events:
        tick, action, pressed = event["tick"], event["action"], event["pressed"]
        key = tick, action, pressed
        if key in seen or pressed == (action in active):
            raise RunnerError("invalid_config", "Input schedule contains duplicate or unmatched action transitions")
        seen.add(key)
        (active.add if pressed else active.remove)(action)
        if {"left", "right"} <= active:
            raise RunnerError("invalid_config", "Left and right may never be held simultaneously")
    if active:
        raise RunnerError("invalid_config", "Every held action must be released before recording ends")
    return value


def validate_recording(recording, config, game):
    """Reject missing frames, invented telemetry, timing drift and input disagreement."""
    viewport = [228, 128] if game == "tiny" else [640, 360]
    if (recording.get("status") != "ok" or recording.get("fps") != FPS
            or recording.get("viewport") != viewport or recording.get("settled") is not True
            or recording.get("frame_count") != FRAME_COUNT):
        raise RunnerError("recording_error", "Recording protocol or settling check failed")
    frames = recording.get("frames")
    if not isinstance(frames, list) or len(frames) != FRAME_COUNT:
        raise RunnerError("recording_error", "Expected exactly 240 frames")
    active = set()
    for index, frame in enumerate(frames):
        for event in config["events"]:
            if event["tick"] == index:
                (active.add if event["pressed"] else active.remove)(event["action"])
        if (not isinstance(frame, dict) or set(frame) != {"frame_index", "physics_tick", "time_s", "actions"}
                or type(frame["frame_index"]) is not int or frame["frame_index"] != index
                or type(frame["physics_tick"]) is not int or frame["physics_tick"] != WARMUP + index
                or type(frame["time_s"]) not in {int, float} or not math.isfinite(frame["time_s"])
                or abs(frame["time_s"] - index / FPS) > 1e-5
                or frame["actions"] != sorted(active)):
            raise RunnerError("recording_error", "Rendered frame timing or action schedule differs", {"frame": index})


def _engine(game):
    try:
        return engine_path(game)
    except FileNotFoundError as exc:
        raise RunnerError("engine_missing", str(exc)) from exc


def _runtime(runtime_dir=None):
    return Path(runtime_dir).resolve() if runtime_dir is not None else ROOT / ".runtime/phase2b" / uuid.uuid4().hex


def _import_tiny(project, env, log_path, cancel, timeout):
    # Fresh pinned sources intentionally contain no generated *.import files.
    # Require sidecars for every importable asset (except .gdignore subtrees),
    # then require all their declared products to become nonempty and stable.
    asset_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".wav", ".ogg", ".mp3"}
    sidecars = set()
    for asset in project.rglob("*"):
        relative = asset.relative_to(project)
        if any(part in legacy._CACHE_DIRS for part in relative.parts) or not asset.is_file():
            continue
        if any((parent / ".gdignore").exists() for parent in (asset.parent, *asset.parents)
               if parent == project or parent.is_relative_to(project)):
            continue
        if asset.suffix.lower() in asset_suffixes:
            sidecars.add(Path(str(asset) + ".import"))
        elif asset.suffix == ".import":
            sidecars.add(asset)
    if not sidecars:
        raise RunnerError("import_error", "Godot 3 project has no importable assets")
    options = process_options()
    if os.name != "nt":
        options["start_new_session"] = True
    with log_path.open("wb") as log:
        try:
            proc = subprocess.Popen([str(_engine("tiny")), "--path", str(project), "--editor", "--no-window"],
                cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT, **options)
        except OSError as exc:
            raise RunnerError("launch_error", "Could not start the Godot 3 importer") from exc
        started, snapshot, stable_since = time.monotonic(), None, None
        try:
            while time.monotonic() - started < timeout:
                legacy._check_cancel(cancel)
                if proc.poll() is not None:
                    raise RunnerError("import_error", "Godot 3 importer exited before products stabilized")
                # A first Godot 3 scan may load autoload scenes before their
                # assets have been imported. Preserve that log; after all
                # declared products stabilize, the actual scene capture must
                # pass the strict runtime check. A stand-alone --script check
                # does not initialize this game's circular autoload graph.
                products, declared = set(), True
                for sidecar in sidecars:
                    if not sidecar.is_file():
                        declared = False
                        continue
                    paths = re.findall(r'"res://(\.import/[^"\r\n]+)"', sidecar.read_text(encoding="utf-8"))
                    if not paths:
                        declared = False
                    for path in paths:
                        product = (project / path).resolve()
                        if not product.is_relative_to(project / ".import"):
                            raise RunnerError("import_error", "Import product escapes the isolated cache")
                        products.add(product)
                products = sorted(products)
                if declared and products and all(p.is_file() and p.stat().st_size for p in products):
                    current = tuple((p.stat().st_size, p.stat().st_mtime_ns) for p in products)
                    if current == snapshot:
                        stable_since = time.monotonic() if stable_since is None else stable_since
                        if time.monotonic() - stable_since >= 2:
                            return
                    else:
                        stable_since = None
                    snapshot = current
                else:
                    snapshot, stable_since = None, None
                if cancel is not None:
                    cancel.wait(.25)
                else:
                    time.sleep(.25)
            raise RunnerError("timeout", "Godot 3 import products did not stabilize")
        finally:
            legacy._terminate_owned_tree(proc)


def import_project(project: Path, game="slay", *, runtime_dir=None, cancel=None, timeout=180) -> None:
    legacy._check_cancel(cancel)
    project = _project_path(project, game)
    _assert_prepared(project, game)
    runtime = _runtime(runtime_dir)
    legacy._assert_outside(runtime, project, "Runtime directory")
    env = legacy._runtime_environment(runtime)
    log_path = runtime / "import.log"
    with legacy._godot_lock(cancel=cancel, timeout=timeout):
        before = source_hashes(project)
        try:
            if game == "tiny":
                _import_tiny(project, env, log_path, cancel, timeout)
                write_json(runtime / "import-readiness.json", {"status": "assets_ready", "game": "tiny",
                    "validation": "Every declared product is nonempty and stable; full scene runtime is checked by capture_project"})
            else:
                result = legacy._run_process([_engine(game), "--path", project, "--editor", "--headless", "--import"],
                    cwd=project, env=env, log_path=log_path, cancel=cancel, timeout=timeout)
                legacy._engine_failure(result, log_path, "import_error")
        finally:
            legacy._ensure_unchanged(project, before)


def capture_project(project, output, *, game, scenario, variant="after", cancel=None,
                    video=True, normal_level=False, extra_config=None, timeout=120) -> Path:
    legacy._check_cancel(cancel)
    project = _project_path(project, game)
    _assert_prepared(project, game)
    scenario = canonical_scenario(scenario)
    if (game, variant) not in {("tiny", "reference"), ("slay", "before"), ("slay", "after")}:
        raise RunnerError("invalid_config", "Expected tiny/reference or slay/before/after")
    extras = deepcopy(extra_config) if isinstance(extra_config, dict) else {}
    if (extra_config is not None and not isinstance(extra_config, dict)
            or set(extras) - {"smoke_wall"} or type(normal_level) is not bool or type(video) is not bool
            or "smoke_wall" in extras and type(extras["smoke_wall"]) is not bool
            or extras.get("smoke_wall") and (game != "slay" or normal_level)):
        raise RunnerError("invalid_config", "Only a Slay fixture may enable the smoke wall")
    if game == "tiny" and scenario["spawn_offset"] != int(scenario["spawn_offset"]):
        raise RunnerError("invalid_config", "Tiny spawn offsets must be integer pixels")
    run = Path(output).resolve()
    legacy._assert_outside(run, project, "Capture output")
    if run.exists() and (not run.is_dir() or any(run.iterdir())):
        raise RunnerError("output_exists", "Existing captures are never overwritten")
    (run / "frames").mkdir(parents=True, exist_ok=True)
    config = dict(scenario, output_dir=str(run), game=game, normal_level=normal_level, **extras)
    config_path = write_json(run / "capture_config.json", config)
    runtime = _runtime()
    legacy._assert_outside(runtime, project, "Runtime directory")
    env = legacy._runtime_environment(runtime)
    env["KIROCLOPS_CONFIG"] = str(config_path)
    before, after = {}, {}
    started = time.perf_counter()
    try:
        with legacy._godot_lock(cancel=cancel, timeout=timeout):
            _assert_prepared(project, game)
            before = source_hashes(project)
            args = [_engine(game), "--path", project, "--fixed-fps", "60", "--resolution",
                    "228x128" if game == "tiny" else "640x360", "--windowed", "res://kiroclops_ground/Capture.tscn"]
            args += ["--video-driver", "GLES2"] if game == "tiny" else ["--rendering-method", "gl_compatibility"]
            try:
                result = legacy._run_process(args, cwd=project, env=env, log_path=run / "engine.log", cancel=cancel, timeout=timeout)
            finally:
                after = source_hashes(project)
                legacy._ensure_unchanged(project, before)
            legacy._engine_failure(result, run / "engine.log", "engine_error")
            try:
                recording = read_json(run / "recording.json")
                validate_recording(recording, config, game)
                if any(not (run / "frames" / f"{i:06d}.png").is_file() or not (run / "frames" / f"{i:06d}.png").stat().st_size for i in range(FRAME_COUNT)):
                    raise ValueError("Missing rendered PNG")
            except RunnerError:
                raise
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                raise RunnerError("recording_error", str(exc)) from exc
            version_path = runtime / "version.log"
            result = legacy._run_process([_engine(game), "--version"], cwd=project, env=env, log_path=version_path, cancel=cancel, timeout=15)
            if result:
                raise RunnerError("engine_error", "Could not read engine version")
            version = version_path.read_text(encoding="utf-8", errors="replace").strip()
        if video:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RunnerError("video_error", "FFmpeg missing; raw PNGs are retained")
            args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "60", "-i", str(run / "frames/%06d.png"),
                    "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(run / "video.mp4")]
            result = legacy._run_process(args, cwd=run, env=env, log_path=run / "video.log", cancel=cancel, timeout=timeout)
            if result or not (run / "video.mp4").is_file() or not (run / "video.mp4").stat().st_size:
                raise RunnerError("video_error", "Video encoding failed; raw PNGs are retained")
        legacy._check_cancel(cancel)
        after = legacy._ensure_unchanged(project, before)
        prefix = "src/actor/" if game == "tiny" else "source/characters/player/"
        provenance = {"source_commit": read_json(ROOT / "config/sources.json")[game]["commit"], "engine": version,
            "project": str(project), "runtime_dir": str(runtime), "wall_seconds": round(time.perf_counter() - started, 3),
            "full_source_hashes_before": before, "full_source_hashes_after": after,
            "source_digest_before": legacy._digest(before), "source_digest_after": legacy._digest(after),
            "controller_hashes": {k:v for k,v in before.items() if k.startswith(prefix) and k.endswith(".gd")},
            "fixture_hashes": {k:v for k,v in before.items() if k.startswith("kiroclops_ground/") or k == "project.godot"},
            "capture_script_sha256": sha256(_adapter(game) / "capture.gd")}
        manifest = {"schema_version": 2, "mechanic": "ground_horizontal", "run_id": run.name, "status": "ok", "game": game,
            "variant": variant, "fps": FPS, "viewport": recording["viewport"], "frame_count": FRAME_COUNT,
            "warmup_ticks": WARMUP, "normal_level": normal_level, "scenario": scenario,
            "frames": recording["frames"], "settled": recording["settled"], "provenance": provenance}
        write_json(run / "manifest.json", manifest)
        return run
    except Exception as exc:
        if not isinstance(exc, RunnerError):
            exc = RunnerError("io_error", str(exc))
        write_json(run / "failure.json", {"status": "error", "code": exc.code, "error": str(exc), "details": exc.details,
            "full_source_hashes_before": before, "full_source_hashes_after": source_hashes(project)})
        raise exc


def _smoke_recipes():
    def event(tick, action, pressed): return {"tick": tick, "action": action, "pressed": pressed}
    jump = [event(30, "jump", True), event(33, "jump", False)]
    return {
        "normal": ([event(5,"right",True),event(80,"right",False),event(30,"jump",True),event(54,"jump",False)], True, False),
        "ground": (jump, False, False),
        "walk-double": (jump + [event(0,"right",True),event(20,"right",False),event(40,"jump",True),event(43,"jump",False)],False,False),
        "wall": (jump + [event(5,"right",True),event(80,"right",False),event(55,"jump",True),event(58,"jump",False)],False,True),
    }


def _smoke_run_evidence(project, run, name, recipe, expected_source_hashes):
    """Replay a fixed probe and bind its raw evidence; never infer motion from states."""
    from .gameplay_review import wall_transition_checks
    paths = [run / filename for filename in ("capture_config.json", "recording.json", "manifest.json", "engine.log")]
    if name in {"normal", "wall"}:
        paths.append(run / "video.mp4")
    frames = [run / "frames" / f"{index:06d}.png" for index in range(FRAME_COUNT)]
    for path in [*paths, *frames]:
        if not path.resolve().is_relative_to(run) or not path.is_file() or not path.stat().st_size:
            raise RunnerError("invalid_evidence", "Gameplay smoke evidence is missing, empty or outside its fixed run")
    events, normal, wall = recipe
    scenario = canonical_scenario(dict(id=f"smoke-{name}", split="smoke", repeat=1, spawn_offset=0, events=events))
    expected_config = dict(scenario, output_dir=str(run), game="slay", normal_level=normal)
    if wall:
        expected_config["smoke_wall"] = True
    config = read_json(run / "capture_config.json")
    manifest = read_json(run / "manifest.json")
    recording = read_json(run / "recording.json")
    if config != expected_config:
        raise RunnerError("smoke_protocol_changed", "Gameplay smoke input/config differs from its fixed recipe")
    provenance = manifest.get("provenance", {})
    if (manifest.get("schema_version") != 2 or manifest.get("mechanic") != "ground_horizontal"
            or manifest.get("game") != "slay" or manifest.get("variant") != "after"
            or manifest.get("scenario") != scenario or manifest.get("normal_level") is not normal
            or manifest.get("warmup_ticks") != WARMUP or manifest.get("run_id") != name
            or provenance.get("project") != str(project)
            or any(provenance.get(key) != expected_source_hashes for key in
                   ("full_source_hashes_before", "full_source_hashes_after"))
            or any(provenance.get(key) != legacy._digest(expected_source_hashes) for key in
                   ("source_digest_before", "source_digest_after"))):
        raise RunnerError("smoke_protocol_changed", "Gameplay smoke source or manifest does not match the supplied project")
    validate_recording(recording, config, "slay")
    validate_recording(manifest, config, "slay")
    if any(recording.get(key) != manifest.get(key) for key in
           ("status", "fps", "viewport", "settled", "frame_count", "frames")):
        raise RunnerError("recording_error", "Gameplay manifest disagrees with the raw recording")
    legacy._engine_failure(0, run / "engine.log", "engine_error")
    if set((run / "frames").glob("*.png")) != set(frames):
        raise RunnerError("recording_error", "Gameplay smoke must contain exactly the expected 240 PNG frames")
    trace = legacy._state_trace(run)
    ordered = bool(trace) and all(index == 0 or item["physics_tick"] > trace[index - 1]["physics_tick"]
                                  for index, item in enumerate(trace))
    if name == "wall":
        checks, transition = wall_transition_checks(trace)
    else:
        expected = {"normal": ["Run", "Jump"], "ground": ["Jump", "Fall", "Idle"],
                    "walk-double": ["Run", "Jump", "DoubleJump", "Fall", "Idle"]}[name]
        checks = {"ordered_trace": ordered, "runtime_state_sequence": legacy._state_sequence(trace, expected)}
        transition = None
    return {"path": str(run), "manifest_sha256": sha256(run / "manifest.json"),
            "engine_log_sha256": sha256(run / "engine.log"),
            "evidence_sha256": {path.name: sha256(path) for path in paths},
            "raw_frames_sha256": legacy._digest([sha256(path) for path in frames]),
            "state_trace": trace, "checks": checks, "transition": transition}


def smoke_project(project, output_root, *, cancel=None):
    """Original map, walking/double jump, ground return and direct wall launch."""
    legacy._check_cancel(cancel)
    project = _project_path(project, "slay")
    output = Path(output_root).resolve()
    legacy._assert_outside(output, project, "Smoke output")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RunnerError("output_exists", "Existing smoke evidence is never overwritten")
    before = source_hashes(project)
    result = {"schema_version": 1, "status": "passed", "checks": {}, "runs": {}, "full_source_hashes_before": before}
    try:
        for name, recipe in _smoke_recipes().items():
            events, normal, wall = recipe
            scenario = dict(id=f"smoke-{name}", split="smoke", repeat=1, spawn_offset=0, events=events)
            run = capture_project(project, output / name, game="slay", scenario=scenario, normal_level=normal,
                                  extra_config={"smoke_wall": True} if wall else None, video=name in {"normal","wall"}, cancel=cancel)
            evidence = _smoke_run_evidence(project, run, name, recipe, before)
            result["checks"][name] = all(evidence["checks"].values())
            result["runs"][name] = evidence
        if not all(result["checks"].values()):
            result["status"] = "failed"
    except Exception as exc:
        result.update(status="error", error={"code": getattr(exc,"code",type(exc).__name__), "message":str(exc)})
        if getattr(exc, "code", None) == "cancelled":
            result["cancelled"] = True
    after = source_hashes(project)
    result["full_source_hashes_after"] = after
    if before != after:
        result.update(status="error", error={"code":"source_changed", "message":"Source changed during gameplay smoke"})
    result["scope"] = "Runtime states on unchanged source; does not establish original trajectories or full-game completion. Wall checks direct adjacent WallJump -> Jump at physics ticks115..117."
    write_json(output / "smoke.json", result)
    if result.get("cancelled"):
        raise RunnerError("cancelled", "Ground smoke cancelled")
    return result


def verify_smoke(project, output_root, expected_source_hashes):
    """Read-only replay of all four fixed probes and source/raw-evidence hashes."""
    output = Path(output_root).resolve()
    receipt_path = output / "smoke.json"
    if not receipt_path.exists():
        return {"status": "unverified", "reason": "No gameplay smoke receipt is recorded"}
    try:
        project = _project_path(project, "slay")
        legacy._assert_outside(output, project, "Smoke output")
        if not receipt_path.resolve().is_relative_to(output):
            raise RunnerError("invalid_evidence", "Gameplay receipt escapes the smoke directory")
        if not expected_source_hashes or source_hashes(project) != expected_source_hashes:
            raise RunnerError("source_changed", "Current project differs from the expected sealed source")
        _assert_prepared(project, "slay")
        receipt = read_json(receipt_path)
        recipes = _smoke_recipes()
        if (receipt.get("schema_version") != 1 or any(receipt.get(key) != expected_source_hashes for key in
                ("full_source_hashes_before", "full_source_hashes_after"))):
            raise RunnerError("source_changed", "Gameplay smoke receipt belongs to different source bytes")
        if receipt.get("status") not in {"passed", "failed"}:
            return {"status": "fail", "reason": "Gameplay smoke did not finish", "error": receipt.get("error")}
        if set(receipt.get("runs", {})) != set(recipes) or set(receipt.get("checks", {})) != set(recipes):
            raise RunnerError("invalid_evidence", "Gameplay smoke must contain all four fixed probes")
        checks, verified = {}, {}
        for name, recipe in recipes.items():
            run = output / name
            if not run.resolve().is_relative_to(output):
                raise RunnerError("invalid_evidence", "Gameplay run escapes its fixed smoke directory")
            evidence = _smoke_run_evidence(project, run, name, recipe, expected_source_hashes)
            if receipt["runs"][name] != evidence:
                raise RunnerError("evidence_changed", f"Gameplay {name} source, log, input or rendered evidence changed")
            verified[name] = evidence
            checks[name] = all(evidence["checks"].values())
        passed = all(checks.values())
        if receipt["checks"] != checks or receipt["status"] != ("passed" if passed else "failed"):
            raise RunnerError("evidence_changed", "Gameplay smoke conclusions do not replay")
        legacy._ensure_unchanged(project, expected_source_hashes)
        return {"status": "pass" if passed else "fail", "checks": checks,
                "reason": "All four gameplay probes replay" if passed else "At least one gameplay probe failed",
                "receipt_path": str(receipt_path), "receipt_sha256": sha256(receipt_path),
                "wall_transition": verified["wall"]["transition"]}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {"status": "fail", "reason": str(exc), "error_code": getattr(exc, "code", type(exc).__name__)}
