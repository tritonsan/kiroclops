"""Open the sealed Kiro candidate's normal game with isolated local saves."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.capture import process_options
from kiroclops.integration.runner import _engine, _godot_lock, _terminate_owned_tree
from kiroclops.integration.session import Session
from kiroclops.integration.storage import IntegrationError, project_hashes, read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true", help="Verify the candidate without opening Godot")
    args = parser.parse_args(argv)
    try:
        session = Session(args.session)
        state = session.read()
        selected = state.get("selected_candidate_id")
        if state.get("status") != "finished" or not selected or state.get("active_job_id"):
            raise IntegrationError("not_playable", "Finish and seal the experiment before opening its candidate")
        candidate = read_json(session.directory / "candidates" / selected / "candidate.json")
        project = session.directory / "candidates" / selected / "project"
        expected = candidate["source_hashes"]
        if project_hashes(project) != expected:
            raise IntegrationError("experiment_integrity", "The sealed candidate sources have changed")
        if args.check_only:
            print(json.dumps({"candidate": selected, "sources_verified": True, "project": str(project)}))
            return 0
        runtime = session.directory / "play" / uuid.uuid4().hex[:12]
        env = dict(os.environ)
        for key, child in (("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata")):
            folder = runtime / child
            folder.mkdir(parents=True)
            env[key] = str(folder)
        env.pop("KIROCLOPS_CONFIG", None)
        # Interactive normal game; no injected experiment inputs or capture scene.
        with _godot_lock(timeout=5), (runtime / "game.log").open("wb") as log:
            process = subprocess.Popen([str(_engine()), "--path", str(project),
                                        "--rendering-method", "gl_compatibility"],
                                       cwd=project, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, **process_options())
            try:
                result = process.wait()
            finally:
                if process.poll() is None:
                    _terminate_owned_tree(process)
                if project_hashes(project) != expected:
                    raise IntegrationError("experiment_integrity", "Game sources changed during play")
        return result
    except (IntegrationError, OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
