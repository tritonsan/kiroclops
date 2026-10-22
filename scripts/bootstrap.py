"""Install project-local prerequisites and pinned game working copies.

Run with the existing Python 3.10: python scripts/bootstrap.py
Never overwrites an existing game working copy or changes the system PATH.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.common import read_json, sha256, write_json


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def download(url: str, path: Path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".partial")
        urllib.request.urlretrieve(url, partial)
        partial.replace(path)


def extract_archive(archive: Path, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            path = (target / item.filename).resolve()
            if not path.is_relative_to(target.resolve()):
                raise ValueError(f"Unsafe archive member {item.filename}")
        source.extractall(target)


def configure_project(path: Path, variant: str):
    project = path / "project.godot"
    text = project.read_text(encoding="utf-8")
    expected_name = f'config/custom_user_dir_name="KiroClops/{variant}"'
    if "config/use_custom_user_dir=true" in text.splitlines() and expected_name in text.splitlines():
        return
    lines = [line for line in text.splitlines()
             if not line.startswith(("config/use_custom_user_dir=", "config/custom_user_dir_name="))]
    index = lines.index("[application]") + 1
    lines[index:index] = ["config/use_custom_user_dir=true",
                        expected_name]
    project.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    if sys.version_info[:2] != (3, 10):
        raise RuntimeError("Use CPython 3.10 for the pinned Phase 1 environment")
    python = ROOT / ".venv/Scripts/python.exe"
    if not python.exists():
        run(sys.executable, "-m", "venv", ROOT / ".venv")
    run(python, "-m", "pip", "install", "--only-binary=:all:", "-r", ROOT / "requirements.txt")
    run(python, "-c", "import cv2,numpy; assert cv2.__version__.startswith('5.0.0'); "
        "assert numpy.__version__=='2.2.6'; print('OpenCV',cv2.__version__,'NumPy',numpy.__version__)")
    sources = read_json(ROOT / "config/sources.json")
    for name in ("tiny", "slay"):
        spec, target = sources[name], ROOT / "vendor" / name
        if not target.exists():
            run("git", "clone", "--no-checkout", spec["url"], target)
            run("git", "-C", target, "checkout", "--detach", spec["commit"])
        # Only these explicitly downloaded repositories are trusted for this
        # process; do not change the user's global Git configuration.
        git = ("git", "-c", f"safe.directory={target.as_posix()}", "-C", target)
        commit = run(*git, "rev-parse", "HEAD", capture_output=True,
                     text=True).stdout.strip()
        if commit != spec["commit"]:
            raise RuntimeError(f"Pinned source mismatch: {target}")
        dirty = run(*git, "status", "--porcelain", capture_output=True,
                    text=True).stdout.strip()
        if dirty:
            raise RuntimeError(f"Original source changed: {target}")
    tools = ROOT / ".tools"
    tools.mkdir(exist_ok=True)
    godot3_zip = tools / "godot3.zip"
    download("https://github.com/godotengine/godot/releases/download/3.6-stable/"
             "Godot_v3.6-stable_win64.exe.zip", godot3_zip)
    if not list((tools / "godot3").glob("*.exe")):
        extract_archive(godot3_zip, tools / "godot3")
    local_godot4 = Path.home() / "Downloads/Godot_v4.5.1-stable_win64.exe.zip"
    godot4_zip = local_godot4 if local_godot4.exists() else tools / "godot4.zip"
    download("https://github.com/godotengine/godot/releases/download/4.5.1-stable/"
             "Godot_v4.5.1-stable_win64.exe.zip", godot4_zip)
    if not list((tools / "godot4").glob("*.exe")):
        extract_archive(godot4_zip, tools / "godot4")
    for variant, source in (("tiny", "tiny"), ("slay-before", "slay"), ("slay-after", "slay")):
        path = ROOT / "work" / variant
        if not path.exists():
            shutil.copytree(ROOT / "vendor" / source, path, ignore=shutil.ignore_patterns(".git", ".godot", ".import"))
        configure_project(path, variant)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("FFmpeg is required for report videos")
    write_json(tools / "environment-lock.json", {
        "python": sys.version, "opencv_python": "5.0.0.93", "numpy": "2.2.6",
        "godot3_archive_sha256": sha256(godot3_zip),
        "godot4_archive_sha256": sha256(godot4_zip), "ffmpeg": ffmpeg,
        "sources": sources})
    print("Prerequisites ready. Next: import game assets and run calibration captures.")


if __name__ == "__main__":
    main()
