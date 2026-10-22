"""Reproduce the local Phase 1 experiment without overwriting captured evidence.

Run with the workspace Python: ``python scripts/run_suite.py --help``.
Capture, annotation, fitting and controller editing remain separate operations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kiroclops.capture import capture_run
from kiroclops.common import read_json, sha256, write_json
from kiroclops.report import generate_report

ROLES = (("tiny", "reference"), ("slay", "before"), ("slay", "after"))
CONTROL_FIELDS = {"control_points", "control_events"}
MEASUREMENT_FIELDS = ("game", "variant", "scenario", "fps", "body_height_px",
                      "valid_ratio", "camera_valid", "events", "metrics", "frames")
SUPPORTED_PLAN = {"schema_version": 1, "fps": 60, "warmup_ticks": 60,
                  "start_tick": 30, "frame_count": 240,
                  "calibration_hold_ticks": [3, 24], "validation_hold_ticks": [8, 14],
                  "repeats": 3, "validation_spawn_offsets": [0, 24]}
SUPPORTED_ACCEPTANCE = {"valid_ratio": .95, "control_points_per_game": 20,
                        "position_median_px_floor": 1, "position_median_body_fraction": .05,
                        "event_error_frames": 2, "curve_rmse_body": .15,
                        "minimum_improvement": .5, "height_relative_error": .1,
                        "duration_relative_error": .1}


class SuiteError(RuntimeError):
    """An experiment prerequisite was not established."""


def analyze_run(*args, **kwargs):
    from kiroclops.analysis import analyze_run as analyze
    return analyze(*args, **kwargs)


def compare_runs(*args):
    from kiroclops.comparison import compare_runs as compare
    return compare(*args)


def digest(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def annotation_measurement(annotation):
    """Only independent control records may be extended after freezing."""
    return {key: value for key, value in annotation.items() if key not in CONTROL_FIELDS}


def analysis_measurement(analysis):
    # Rechecking independent points updates annotation_validation and can change
    # status/errors. It must not change any actual image-derived measurement.
    return {key: analysis.get(key) for key in MEASUREMENT_FIELDS}


class Suite:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.runs = self.root / "artifacts/runs"
        self.profile = self.root / "config/measured-profile.json"
        self.frozen = self.root / "artifacts/frozen_experiment.json"

    def config(self):
        config = read_json(self.root / "config/experiment.json")
        if any(config.get(key) != value for key, value in SUPPORTED_PLAN.items()):
            raise SuiteError("config/experiment.json differs from the supported Phase 1 plan.")
        if config.get("acceptance") != SUPPORTED_ACCEPTANCE:
            raise SuiteError("Acceptance thresholds do not match the fixed thresholds of the comparison module.")
        return config

    def scenarios(self, split, roles=ROLES):
        config = self.config()
        holds = config[f"{split}_hold_ticks"]
        offsets = config["validation_spawn_offsets"] if split == "validation" else [0]
        return [(game, variant, hold, offset, repeat)
                for game, variant in roles for hold in holds for offset in offsets
                for repeat in range(1, config["repeats"] + 1)]

    def run_path(self, scenario):
        game, variant, hold, offset, repeat = scenario
        return self.runs / f"{game}-{variant}-h{hold:02d}-x{offset}-r{repeat}"

    def project(self, game, variant):
        return self.root / "work" / ("tiny" if game == "tiny" else f"slay-{variant}")

    def controller_files(self, game, variant):
        project = self.project(game, variant)
        sources = read_json(self.root / "config/sources.json")
        files = [Path(sources[game]["controller"])]
        if game == "tiny":
            files.append(Path("src/actor/Actor.gd"))
        else:
            files += [path.relative_to(project) for path in
                      (project / "source/characters/player/state_machine/states").glob("*.gd")]
        return {path.as_posix(): project / path for path in files}

    def fixture_files(self, game, variant):
        project = self.project(game, variant)
        adapter = self.root / "adapters" / ("godot3" if game == "tiny" else "godot4")
        files = {"project.godot": project / "project.godot",
                 "kiroclops_capture/Capture.tscn": adapter / "Capture.tscn",
                 "kiroclops_capture/capture.gd": adapter / "capture.gd"}
        paths = (["src/stage/SolidTileMap.tscn", "src/actor/Player.tscn"] if game == "tiny" else
                 ["source/maps/map_scenes/map_demo.tscn", "source/characters/player/player.tscn"])
        files.update({path: project / path for path in paths})
        return files

    def verify_capture(self, scenario):
        """A reusable capture must match the current fixture and controller."""
        run = self.run_path(scenario)
        manifest_path = run / "manifest.json"
        if not manifest_path.is_file():
            raise SuiteError(f"Missing/invalid recording is preserved; it will not be overwritten: {run}")
        manifest = read_json(manifest_path)
        game, variant, hold, offset, repeat = scenario
        split = "calibration" if hold in (3, 24) else "validation"
        expected = {"hold_ticks": hold, "start_tick": 30, "split": split,
                    "repeat": repeat, "spawn_offset": offset}
        if (manifest.get("status") != "ok" or manifest.get("schema_version") != 1
                or manifest.get("game") != game or manifest.get("variant") != variant
                or manifest.get("scenario") != expected or manifest.get("normal_level") is True
                or manifest.get("fps") != 60 or manifest.get("frame_count") != 240):
            raise SuiteError(f"Recording status or scenario does not match: {run}")
        provenance = manifest.get("provenance", {})
        controllers = {name: sha256(path) for name, path in self.controller_files(game, variant).items()}
        sources = read_json(self.root / "config/sources.json")
        if (provenance.get("controller_hashes") != controllers
                or provenance.get("source_commit") != sources[game]["commit"]):
            raise SuiteError(f"Recording belongs to an old controller/source version: {run}")
        fixtures = self.fixture_files(game, variant)
        if provenance.get("capture_script_sha256") != sha256(fixtures["kiroclops_capture/capture.gd"]):
            raise SuiteError(f"Capture script has changed: {run}")
        stored = provenance.get("fixture_hashes", {})
        if not isinstance(stored, dict) or not set(fixtures).issubset(stored):
            raise SuiteError(f"Fixture hash evidence is missing; the recording cannot be reused automatically: {run}")
        for name, expected_hash in stored.items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise SuiteError(f"Invalid fixture path: {name}")
            path = fixtures.get(name, self.project(game, variant) / relative)
            if not path.is_file() or sha256(path) != expected_hash:
                raise SuiteError(f"Fixture has changed ({name}): {run}")
        metadata = manifest.get("frames", [])
        if len(metadata) != 240 or any(frame.get("frame_index") != index for index, frame in enumerate(metadata)):
            raise SuiteError(f"Frame manifest is missing/out of order: {run}")
        if not (run / "video.mp4").is_file() or any(not (run / "frames" / f"{index:06d}.png").is_file() for index in range(240)):
            raise SuiteError(f"Video or PNG evidence is missing; the recording was not deleted: {run}")
        return manifest

    def annotation(self, game, example=None):
        path = self.root / "config" / f"{game}-annotation.json"
        if not path.is_file():
            example = example or self.run_path((game, "reference" if game == "tiny" else "before", 3, 0, 1))
            raise SuiteError(
                f"Visual annotation required: {path}\n"
                f'  .venv\\Scripts\\python.exe -m kiroclops annotate "{example}" --output "{path}" '
                '--anchor AX AY --body-height PX\n'
                "From the opened image, select the fixed body and background areas; measure the AX/AY and PX values "
                "from the image. Independent control points are not generated automatically.")
        from kiroclops.annotation import load_annotation
        return path, load_annotation(path)

    def capture_stage(self, split, roles):
        if split == "validation":
            self.verify_frozen()
        elif self.frozen.exists():
            raise SuiteError("Experiment is frozen. Calibration is not re-run; use report for the control marks.")
        scenarios = self.scenarios(split, roles)
        # Reject stale evidence before beginning any new captures.
        for scenario in scenarios:
            run = self.run_path(scenario)
            if run.exists() and any(run.iterdir()):
                self.verify_capture(scenario)
        for scenario in scenarios:
            game, variant, hold, offset, repeat = scenario
            run = self.run_path(scenario)
            if run.exists() and any(run.iterdir()):
                print(f"Reusing: {run.name}", flush=True)
            else:
                print(f"Recording: {run.name}", flush=True)
                capture_run(game, variant, hold, repeat=repeat, spawn_offset=offset, output_dir=run)
                self.verify_capture(scenario)
            annotation_path, _ = self.annotation(game, run)
            result = analyze_run(run, annotation_path)
            if result.get("status") != "ok":
                raise SuiteError(f"Analysis invalid: {run}; {result.get('errors', [])}. The recording is preserved.")
        return scenarios

    def calibration(self):
        result = self.capture_stage("calibration", ROLES[:2])
        refs = " ".join(f'"{self.run_path(s) / "analysis.json"}"' for s in result if s[1] == "reference")
        print(f'Calibration ready. Fit from the references: .venv\\Scripts\\python.exe -m kiroclops fit {refs} --output "{self.profile}"')

    def after_calibration(self):
        self.capture_stage("calibration", ROLES[2:])

    def _calibration_evidence(self):
        evidence = {}
        for scenario in self.scenarios("calibration"):
            run = self.run_path(scenario)
            self.verify_capture(scenario)
            analysis_path = run / "analysis.json"
            analysis = read_json(analysis_path)
            annotation_path, _ = self.annotation(scenario[0], run)
            provenance = analysis.get("provenance", {})
            if (analysis.get("status") != "ok" or analysis.get("scenario", {}).get("split") != "calibration"
                    or provenance.get("manifest_sha256") != sha256(run / "manifest.json")
                    or provenance.get("annotation_sha256") != sha256(annotation_path)):
                raise SuiteError(f"A successful and current calibration analysis is required: {analysis_path}")
            evidence[run.relative_to(self.root).as_posix()] = {
                "manifest_sha256": sha256(run / "manifest.json"),
                "analysis_sha256": sha256(analysis_path),
                "measurement_sha256": digest(analysis_measurement(analysis)),
            }
        return evidence

    def _profile_evidence(self):
        if not self.profile.is_file():
            raise SuiteError(f"First produce a fit using only the reference/calibration recordings: {self.profile}")
        profile = read_json(self.profile)
        files = profile.get("provenance", {}).get("analysis_files", [])
        if not files:
            raise SuiteError("The profile's calibration sources and SHA256 evidence are missing.")
        holds, paths = set(), set()
        expected_refs = {self.run_path(s).resolve() for s in self.scenarios("calibration", ROLES[:1])}
        for item in files:
            path = Path(item["path"])
            if not path.is_absolute():
                path = self.root / path
            report = read_json(path)
            if (path.resolve().parent not in expected_refs or report.get("game") != "tiny"
                    or report.get("variant") != "reference" or report.get("status") != "ok"
                    or report.get("scenario", {}).get("split") != "calibration"
                    or item.get("sha256") != sha256(path)):
                raise SuiteError(f"Profile sources must be only the current canonical reference calibration: {path}")
            holds.add(report["scenario"]["hold_ticks"])
            paths.add(path.resolve())
        if holds != {3, 24} or len(paths) < 2:
            raise SuiteError("The profile must rely on both short and long reference presses.")
        return profile

    def _frozen_files(self):
        paths = {"config/experiment.json", "config/sources.json", "config/measured-profile.json"}
        for game, variant in ROLES:
            paths.update(path.relative_to(self.root).as_posix() for path in self.controller_files(game, variant).values())
            paths.update(path.relative_to(self.root).as_posix() for path in self.fixture_files(game, variant).values())
        for name in ("analysis.py", "annotation.py", "comparison.py", "fitting.py", "capture.py"):
            paths.add(f"kiroclops/{name}")
        return {path: sha256(self.root / path) for path in sorted(paths)}

    def _frozen_annotations(self):
        records = {}
        for game in ("tiny", "slay"):
            path, annotation = self.annotation(game)
            template = Path(annotation["template_run"]) / "frames" / f'{annotation["template_frame"]:06d}.png'
            records[game] = {"path": path.relative_to(self.root).as_posix(),
                             "measurement_sha256": digest(annotation_measurement(annotation)),
                             "template_frame": str(template), "template_sha256": sha256(template)}
        return records

    def freeze(self):
        self.config()
        if self.frozen.exists():
            self.verify_frozen()
            print(f"Existing freeze verified: {self.frozen}")
            return
        for scenario in self.scenarios("validation"):
            run = self.run_path(scenario)
            if run.exists() and any(run.iterdir()):
                raise SuiteError(f"Validation data exists before freezing: {run}. History is not deleted automatically.")
        calibration = self._calibration_evidence()
        self._profile_evidence()
        record = {"schema_version": 1, "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
                  "files": self._frozen_files(), "annotations": self._frozen_annotations(),
                  "calibration_runs": calibration, "thresholds": self.config()["acceptance"],
                  "annotation_extension_policy": "Only control_points and control_events may change; use per-record source labels."}
        # Exclusive creation: never silently replace the preregistered snapshot.
        self.frozen.parent.mkdir(parents=True, exist_ok=True)
        with self.frozen.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        print(f"Experiment frozen: {self.frozen}")

    def verify_frozen(self):
        if not self.frozen.is_file():
            raise SuiteError("Run freeze first; validation does not start without a freeze record.")
        record = read_json(self.frozen)
        if record.get("schema_version") != 1 or not record.get("files") or not record.get("calibration_runs"):
            raise SuiteError("Freeze record is missing or invalid.")
        if self._frozen_files() != record["files"]:
            raise SuiteError("Frozen profile, controller, fixture, threshold or analysis code has changed. Validation stopped.")
        if self._frozen_annotations() != record.get("annotations"):
            raise SuiteError("The annotation's measurement settings or template image have changed. Only control_points/control_events may be added.")
        expected = {self.run_path(s).relative_to(self.root).as_posix() for s in self.scenarios("calibration")}
        if set(record["calibration_runs"]) != expected:
            raise SuiteError("Freeze record does not contain all 18 calibration runs.")
        for relative, evidence in record["calibration_runs"].items():
            run = self.root / relative
            if sha256(run / "manifest.json") != evidence["manifest_sha256"]:
                raise SuiteError(f"Frozen calibration manifest has changed: {run}")
            if digest(analysis_measurement(read_json(run / "analysis.json"))) != evidence["measurement_sha256"]:
                raise SuiteError(f"Frozen calibration measurement has changed: {run}")
        return record

    def validation(self):
        self.capture_stage("validation", ROLES)

    def report(self):
        frozen = self.frozen.is_file()
        if frozen:
            self.verify_frozen()
        self.config()
        runs, grouped, problems = [], {role: [] for _, role in ROLES}, []
        for scenario in self.scenarios("calibration") + self.scenarios("validation"):
            run = self.run_path(scenario)
            if not run.exists():
                continue
            runs.append(run)
            try:
                self.verify_capture(scenario)
                annotation_path, _ = self.annotation(scenario[0], run)
                analyze_run(run, annotation_path)
                grouped[scenario[1]].append(run / "analysis.json")
            except (OSError, ValueError, RuntimeError) as exc:
                problems.append(str(exc))
        if frozen:
            self.verify_frozen()
        if any(grouped.values()):
            result = compare_runs(grouped["reference"], grouped["before"], grouped["after"])
        else:
            result = {"schema_version": 1, "status": "pending", "success": False,
                      "pending": ["No comparable analysis yet."], "errors": []}
        if problems:
            result.setdefault("errors", []).extend(problems)
            result.update(status="fail", success=False)
        if not frozen:
            result.setdefault("pending", []).append("Experiment is not frozen; a final success decision cannot be made.")
            result["success"] = False
            if result.get("status") != "fail":
                result["status"] = "pending"
        result["experiment_frozen"] = frozen
        comparison = write_json(self.root / "artifacts/comparison.json", result)
        # Normal-level records are viewable but never enter the controlled gates.
        for path in sorted(self.runs.glob("*-normal")) if self.runs.exists() else []:
            if (path / "manifest.json").is_file():
                runs.append(path)
        page = generate_report(runs, self.root / "artifacts/report",
                               profile_path=self.profile if self.profile.is_file() else None,
                               comparison_paths=[comparison])
        print(f"Report: {page}\nComparison: {result['status']}")
        return page


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["calibration", "after-calibration", "freeze", "validation", "report"],
                        help="Stages are run separately; validation requires freeze beforehand.")
    args = parser.parse_args(argv)
    suite = Suite()
    try:
        getattr(suite, args.stage.replace("-", "_"))()
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"KiroClops suite: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
