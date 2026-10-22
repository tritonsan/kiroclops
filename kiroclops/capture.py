"""Run isolated Godot captures and preserve provenance without game coordinates."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .common import ROOT, read_json, sha256, write_json


def engine_path(game: str) -> Path:
    if game == "tiny":
        candidates = sorted((ROOT / ".tools/godot3").glob("*console.exe"))
        candidates += sorted((ROOT / ".tools/godot3").glob("*.exe"))
    else:
        candidates = sorted((ROOT / ".tools/godot4").glob("*console.exe"))
    if not candidates:
        raise FileNotFoundError(f"Godot executable missing for {game}; run scripts/bootstrap.py")
    return candidates[0]


def process_options() -> dict:
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def project_path(game: str, variant: str) -> Path:
    if game not in {"tiny", "slay"}:
        raise ValueError("game must be tiny or slay")
    if (game, variant) not in {("tiny", "reference"), ("slay", "before"), ("slay", "after")}:
        raise ValueError("Use tiny/reference, slay/before, or slay/after")
    return ROOT / "work" / ("tiny" if game == "tiny" else f"slay-{variant}")


def isolated_environment(variant: str) -> dict:
    env = dict(os.environ)
    for key, name in (("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata")):
        path = ROOT / ".runtime" / variant / name
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    return env


def import_project(game: str, variant: str, timeout: int = 180) -> None:
    project = project_path(game, variant)
    engine = engine_path(game)
    args = [str(engine), "--path", str(project), "--editor"]
    if game == "tiny":
        # Godot 3's --quit exits before asynchronous imports finish. Wait for
        # every declared import product, then for a stable cache, and terminate
        # only this child editor process. No arbitrary sleep guesses success.
        args += ["--no-window"]
        expected = set()
        for sidecar in project.rglob("*.import"):
            if sidecar.is_file():
                expected.update(re.findall(r'"res://(\.import/[^"\r\n]+)"', sidecar.read_text(encoding="utf-8")))
        if not expected:
            raise RuntimeError("No Godot 3 import sidecars found")
        log_path = project / "kiroclops-import.log"
        started, last_snapshot, stable_since = time.monotonic(), None, None
        with log_path.open("wb") as log:
            proc = subprocess.Popen(args, env=isolated_environment(variant), cwd=project,
                                    stdout=log, stderr=subprocess.STDOUT, **process_options())
            try:
                while time.monotonic() - started < timeout:
                    if proc.poll() is not None:
                        raise RuntimeError(f"Godot 3 import exited early; see {log_path}")
                    products = [project / rel for rel in expected]
                    if all(p.is_file() and p.stat().st_size for p in products):
                        snapshot = tuple((p.stat().st_size, p.stat().st_mtime_ns) for p in products)
                        if snapshot == last_snapshot:
                            stable_since = stable_since or time.monotonic()
                            if time.monotonic() - stable_since >= 2:
                                return
                        else:
                            stable_since = None
                        last_snapshot = snapshot
                    time.sleep(.25)
                raise RuntimeError(f"Godot 3 asset import timed out; see {log_path}")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
    args += ["--headless", "--import"]
    result = subprocess.run(args, env=isolated_environment(variant), cwd=project,
                            capture_output=True, timeout=timeout, **process_options())
    text = (result.stdout + result.stderr).decode("utf-8", "replace")
    (project / "kiroclops-import.log").write_text(text, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Godot import failed: {text[-4000:]}")


def make_video(run_dir: str | Path) -> Path:
    run_dir = Path(run_dir).resolve()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("FFmpeg not found; PNG measurements remain available")
    output = run_dir / "video.mp4"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "60",
           "-i", str(run_dir / "frames/%06d.png"), "-c:v", "libx264", "-crf", "16",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120, **process_options())
    return output


def validate_recording(recording: dict, config: dict, game: str) -> None:
    """Reject timing/input mistakes before exposing a capture to analysis."""
    expected_viewport = [228, 128] if game == "tiny" else [640, 360]
    if recording.get("status") != "ok" or recording.get("fps") != 60:
        raise RuntimeError("Recording did not complete at 60 Hz")
    if recording.get("viewport") != expected_viewport:
        raise RuntimeError("Recording viewport differs from the native measurement viewport")
    frames = recording.get("frames", [])
    if len(frames) != config["frame_count"] or recording.get("frame_count") != len(frames):
        raise RuntimeError("Recording frame count mismatch")
    if not recording.get("settled"):
        raise RuntimeError("Character did not settle before the capture")
    action = "jump" if game == "tiny" else "ui_up"
    for index, frame in enumerate(frames):
        if frame.get("frame_index") != index or frame.get("physics_tick") != config["warmup_ticks"] + index:
            raise RuntimeError("Missing or duplicated physics/frame tick")
        if abs(frame.get("time_s", -1) - index / 60) > 1e-5:
            raise RuntimeError("Frame timestamp differs from the fixed-step clock")
        expected_pressed = config["start_tick"] <= index < config["start_tick"] + config["hold_ticks"]
        if not config.get("extra_inputs") and (action in frame.get("actions", [])) != expected_pressed:
            raise RuntimeError("Recorded action phase differs from the requested input schedule")


def capture_run(game: str, variant: str, hold_ticks: int, *, repeat: int = 1,
                spawn_offset: int = 0, output_dir: str | Path | None = None,
                normal_level: bool = False, timeout: int = 120,
                video: bool = True, extra_config: dict | None = None) -> Path:
    if hold_ticks not in {3, 24, 8, 14}:
        raise ValueError("Phase 1 holds must be 3, 24, 8, or 14 ticks")
    if repeat not in {1, 2, 3}:
        raise ValueError("repeat must be 1, 2, or 3")
    project = project_path(game, variant)
    engine = engine_path(game)
    sources = read_json(ROOT / "config/sources.json")
    split = "calibration" if hold_ticks in {3, 24} else "validation"
    suffix = "-normal" if normal_level else ""
    run_id = f"{game}-{variant}-h{hold_ticks:02d}-x{spawn_offset}-r{repeat}{suffix}"
    run = Path(output_dir).resolve() if output_dir else ROOT / "artifacts/runs" / run_id
    if run.exists() and any(run.iterdir()):
        raise FileExistsError(f"Refusing to overwrite an existing capture: {run}")
    (run / "frames").mkdir(parents=True, exist_ok=True)
    adapter = ROOT / "adapters" / ("godot3" if game == "tiny" else "godot4")
    shutil.copytree(adapter, project / "kiroclops_capture", dirs_exist_ok=True)
    config = {"output_dir": str(run), "hold_ticks": hold_ticks, "start_tick": 30,
              "warmup_ticks": 60, "frame_count": 240, "spawn_offset": spawn_offset,
              "normal_level": normal_level}
    if extra_config:
        protected = set(config) & set(extra_config)
        if protected:
            raise ValueError(f"extra_config cannot override {sorted(protected)}")
        config.update(extra_config)
    write_json(run / "capture_config.json", config)
    env = isolated_environment(run_id)
    env["KIROCLOPS_CONFIG"] = str(run / "capture_config.json")
    args = [str(engine), "--path", str(project), "--fixed-fps", "60", "--resolution",
            "228x128" if game == "tiny" else "640x360", "--windowed",
            "res://kiroclops_capture/Capture.tscn"]
    if game == "tiny":
        args.extend(["--video-driver", "GLES2"])
    else:
        args.extend(["--rendering-method", "gl_compatibility"])
    started = time.perf_counter()
    try:
        proc = subprocess.run(args, cwd=project, env=env, capture_output=True,
                              timeout=timeout, **process_options())
    except subprocess.TimeoutExpired as exc:
        log = (exc.stdout or b"") + (exc.stderr or b"")
        (run / "engine.log").write_bytes(log)
        write_json(run / "failure.json", {"status": "runtime_error", "error": "capture_timeout"})
        raise RuntimeError(f"Capture timed out; see {run / 'engine.log'}") from exc
    (run / "engine.log").write_bytes(proc.stdout + proc.stderr)
    recording_path = run / "recording.json"
    if not recording_path.exists():
        raise RuntimeError(f"Godot did not produce recording.json; see {run / 'engine.log'}")
    recording = read_json(recording_path)
    if proc.returncode or recording.get("status") != "ok":
        write_json(run / "failure.json", {"status": "runtime_error", "recording": recording,
                                          "returncode": proc.returncode})
        raise RuntimeError(f"Recording failed: {recording}; see {run / 'engine.log'}")
    validate_recording(recording, config, game)
    frames = recording.get("frames", [])
    if len(frames) != 240 or any(f["frame_index"] != i for i, f in enumerate(frames)):
        raise RuntimeError("Invalid frame count/order")
    ticks = [f["physics_tick"] for f in frames]
    if any(b != a + 1 for a, b in zip(ticks, ticks[1:])):
        raise RuntimeError("Skipped or duplicated physics tick")
    for i in range(240):
        if not (run / f"frames/{i:06d}.png").is_file():
            raise RuntimeError(f"Missing frame {i}")
    controller_paths = [Path(sources[game]["controller"])]
    if game == "tiny":
        controller_paths.append(Path("src/actor/Actor.gd"))
    else:
        controller_paths += [p.relative_to(project) for p in
                             (project / "source/characters/player/state_machine/states").glob("*.gd")]
    controller_hashes = {p.as_posix(): sha256(project / p) for p in sorted(controller_paths)}
    fixture_paths = ["project.godot", "kiroclops_capture/Capture.tscn", "kiroclops_capture/capture.gd"]
    fixture_paths += (["src/stage/SolidTileMap.tscn", "src/actor/Player.tscn"] if game == "tiny" else
                      ["source/maps/map_scenes/map_demo.tscn", "source/characters/player/player.tscn"])
    fixture_hashes = {p: sha256(project / p) for p in fixture_paths}
    version = subprocess.run([str(engine), "--version"], capture_output=True, timeout=15,
                             **process_options()).stdout.decode("utf-8", "replace").strip()
    manifest = {"schema_version": 1, "run_id": run_id, "status": "ok", "game": game,
                "variant": variant, "fps": 60, "viewport": recording["viewport"],
                "frame_count": 240, "normal_level": normal_level,
                "scenario": {"hold_ticks": hold_ticks, "start_tick": 30, "split": split,
                             "repeat": repeat, "spawn_offset": spawn_offset},
                "frames": frames, "settled": recording.get("settled", False),
                "provenance": {"source_commit": sources[game]["commit"], "engine": version,
                               "controller_hashes": controller_hashes,
                               "fixture_hashes": fixture_hashes,
                               "capture_script_sha256": sha256(adapter / "capture.gd"),
                               "wall_seconds": round(time.perf_counter() - started, 3)}}
    write_json(run / "manifest.json", manifest)
    if video:
        make_video(run)
    return run
