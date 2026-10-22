"""Mocked native boundary: no Godot, desktop or model is launched by this suite."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from kiroclops.portable_v2 import runtime as rt


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def scenario(**changes):
    value = {"id": "h03", "split": "calibration", "repeat": 1, "spawn_offset": 0,
             "start_tick": 30, "hold_ticks": 3,
             "events": [{"tick": 30, "action": "jump", "pressed": True},
                        {"tick": 33, "action": "jump", "pressed": False}]}
    value.update(changes)
    return value


def recording(value=None):
    value = rt.canonical_scenario(value or scenario())
    active, frames = set(), []
    for index in range(240):
        for event in value["events"]:
            if event["tick"] == index:
                (active.add if event["pressed"] else active.remove)(event["action"])
        frames.append({"frame_index": index, "physics_tick": 60 + index,
                       "time_s": index / 60, "actions": sorted(active)})
    return {"status": "ok", "fps": 60, "viewport": [320, 180], "frame_count": 240,
            "settled": True, "normal_level": False, "active_warmup_ticks": 60, "frames": frames,
            "observations": [{"frame_index": index, "physics_tick": 60 + index, "on_floor": True,
                "on_wall": False, "moving_up": False, "moving_horizontal": False,
                "jump_edge": False, "hazard_entries": 0, "ground_incline": False,
                "ground_decline": False, "ground_rise": False, "ground_descent": False} for index in range(240)]}


def png_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 320, 180, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * 960) * 180)) + chunk(b"IEND", b""))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        for name in rt._REQUIRED:
            (self.project / name).write_text("# source\n", encoding="utf-8")
        (self.project / "project.godot").write_text('[application]\nconfig/name="Heart"\n', encoding="utf-8")
        rt.prepare_project(self.project)
        self.engine = patch.object(rt, "_engine", return_value=Path(rt.ROOT / rt.ENGINE_RELATIVE))
        self.engine.start()
        self.addCleanup(self.engine.stop)

    def fake_private(self, argv, *, cwd, env, output_dir, timeout, cancel=None):
        output_dir.mkdir()
        stamp = datetime.now(timezone.utc).isoformat()
        env_sha = hashlib.sha256(rt.private.environment_block(env).encode("utf-16-le")).hexdigest()
        start = {"schema_version": 1, "runner": rt.private.VERSION, "status": "starting",
            "argv": argv, "cwd": str(cwd), "executable_sha256": rt.ENGINE_SHA256,
            "policy": rt.PROCESS_POLICY, "desktop_name": "KiroClops_test", "started_at": stamp,
            "timeout_seconds": timeout, "environment_keys": sorted(env, key=str.upper), "environment_sha256": env_sha}
        dump(output_dir / "process-start.json", start)
        identity = {"runner": rt.private.VERSION, "status": "suspended_in_job",
            "process_identity": {"pid": 100, "primary_thread_id": 200, "creation_filetime_100ns": 300},
            "desktop_path": "WinSta0\\KiroClops_test", "recorded_at": stamp,
            "start_sha256": rt._sha(output_dir / "process-start.json")}
        dump(output_dir / "process-identity.json", identity)
        (output_dir / "stdout.log").write_text("Godot Engine v" + rt.ENGINE_VERSION + "\n", encoding="utf-8")
        (output_dir / "stderr.log").write_text("", encoding="utf-8")
        proof = dict(start, status="completed", exit_code=0, cleanup_errors=[],
            owned_process_termination_confirmed=True, process_identity=identity["process_identity"],
            desktop_path=identity["desktop_path"], resumed_at=stamp, finished_at=stamp,
            saw_owned_window="--headless" not in argv,
            desktop_samples=[{"name": "KiroClops_test", "receives_input": False,
                              "owned_window_count": 0 if "--headless" in argv else 1}])
        proof["evidence"] = {name: {"sha256": rt._sha(output_dir / name), "bytes": (output_dir / name).stat().st_size}
            for name in ("process-start.json", "process-identity.json", "stdout.log", "stderr.log")}
        dump(output_dir / "process.json", proof)
        if env.get("KIROCLOPS_CONFIG"):
            config = json.loads(Path(env["KIROCLOPS_CONFIG"]).read_text(encoding="utf-8"))
            output = Path(config["output_dir"])
            result = recording({key: value for key, value in config.items()
                                if key not in {"output_dir", "game", "normal_level", "protocol", "viewport", "fps"}})
            result["normal_level"] = config.get("normal_level", False)
            dump(output / "recording.json", result)
            (output / "frames").mkdir()
            for index in range(240):
                (output / "frames" / f"{index:06d}.png").write_bytes(png_bytes())
        return proof

    def config(self, output, **changes):
        output.mkdir()
        value = dict(rt.canonical_scenario(scenario()), output_dir=str(output), **changes)
        dump(output / "capture_config.json", value)

    def native(self, function=None):
        return patch.object(rt.private, "run_private_desktop", side_effect=function or self.fake_private)

    def test_prepare_preserves_controller_and_is_idempotent(self):
        old = (self.project / "player.gd").read_bytes()
        hashes = rt.source_hashes(self.project)
        rt.prepare_project(self.project)
        self.assertEqual(rt.source_hashes(self.project), hashes)
        self.assertEqual((self.project / "player.gd").read_bytes(), old)
        self.assertIn(rt.CUSTOM_DIR, (self.project / "project.godot").read_text())

    def test_prepare_rejects_changed_fixture_and_candidate(self):
        (self.project / "kiroclops_heart/capture.gd").write_text("tampered")
        with self.assertRaisesRegex(rt.PortableRuntimeError, "Existing fixture differs"):
            rt.prepare_project(self.project)
        dump(self.project.parent / "candidate.json", {})
        with self.assertRaisesRegex(rt.PortableRuntimeError, "Candidate snapshots"):
            rt.prepare_project(self.project)

    def test_jump_metadata_is_exactly_the_input_schedule(self):
        with self.assertRaises(rt.PortableRuntimeError):
            rt.canonical_scenario(scenario(hold_ticks=24))
        result = rt.canonical_scenario(scenario(variant="after"))
        self.assertEqual(result["warmup_ticks"], 60)
        self.assertEqual(result["variant"], "after")

    def test_reversal_releases_before_presses_without_mutating_input(self):
        value = scenario(events=[{"tick": 10, "action": "left", "pressed": True},
            {"tick": 5, "action": "right", "pressed": True},
            {"tick": 10, "action": "right", "pressed": False},
            {"tick": 20, "action": "left", "pressed": False}])
        del value["hold_ticks"], value["start_tick"]
        old = deepcopy(value)
        result = rt.canonical_scenario(value)
        self.assertEqual(value, old)
        self.assertFalse(result["events"][1]["pressed"])
        self.assertTrue(result["events"][2]["pressed"])

    def test_invalid_schedules(self):
        for change in ({"repeat": True}, {"spawn_offset": float("nan")}, {"warmup_ticks": 59},
                       {"frame_count": 239}, {"variant": "reference"}, {"split": []}):
            with self.subTest(change=change), self.assertRaises(rt.PortableRuntimeError):
                rt.canonical_scenario(scenario(**change))
        for events in ([{"tick": 0, "action": "jump", "pressed": False}],
                       [{"tick": 0, "action": "left", "pressed": True}, {"tick": 0, "action": "right", "pressed": True}],
                       [{"tick": 0, "action": "jump", "pressed": True}]):
            value = scenario(events=events)
            del value["hold_ticks"], value["start_tick"]
            with self.assertRaises(rt.PortableRuntimeError):
                rt.canonical_scenario(value)

    def test_recording_rejects_offset_drift_or_engine_coordinates(self):
        valid = recording()
        rt.validate_recording(valid, scenario())
        for mutate in (lambda x: x["frames"][0].update(physics_tick=0),
                       lambda x: x["frames"][30].update(actions=[]),
                       lambda x: x["frames"][20].update(x=1),
                       lambda x: x.update(active_warmup_ticks=59),
                       lambda x: x.update(viewport=[640, 360])):
            value = deepcopy(valid)
            mutate(value)
            with self.assertRaises(rt.PortableRuntimeError):
                rt.validate_recording(value, scenario())

    def test_private_import_no_window_required_and_fresh_source_bound(self):
        before = rt.source_hashes(self.project)
        with self.native() as process:
            result = rt.import_project(self.project, self.base / "import")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(process.call_count, 1)
        args = process.call_args.args[0]
        self.assertEqual(args[-3:], ["--editor", "--headless", "--import"])
        check = rt.verify_runtime_receipt(result["receipt_path"], project=self.project, expected_source_hashes=before)
        self.assertEqual(check["status"], "pass", check)

    def test_capture_schema_and_native_frame_hashes(self):
        run = self.base / "capture"
        with self.native() as process:
            result = rt.capture_project(self.project, run, scenario(variant="after"))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["variant"], "after")
        self.assertEqual(result["game"], "heart")
        self.assertEqual(len(result["raw_frame_hashes"]), 240)
        self.assertEqual(result["frames"][0]["physics_tick"], 60)
        self.assertEqual(result["frames"][30]["actions"], ["jump"])
        self.assertIn("--disable-vsync", process.call_args.args[0])
        self.assertIn("320x180", process.call_args.args[0])
        self.assertNotIn("--headless", process.call_args.args[0])
        self.assertEqual(rt.verify_runtime_receipt(result["provenance"]["runtime_receipt"])["status"], "pass")

    def test_cancel_before_start_never_calls_transport(self):
        cancel = threading.Event()
        cancel.set()
        with self.native() as process, self.assertRaisesRegex(rt.PortableRuntimeError, "cancelled"):
            rt.import_project(self.project, self.base / "cancelled", cancel=cancel)
        process.assert_not_called()

    def test_cancelled_process_retains_receipt_without_fallback(self):
        def cancelled(*args, **kwargs):
            proof = self.fake_private(*args, **kwargs)
            proof["status"] = "cancelled"
            dump(kwargs["output_dir"] / "process.json", proof)
            return proof
        with self.native(cancelled) as process, self.assertRaisesRegex(rt.PortableRuntimeError, "cancelled"):
            rt.import_project(self.project, self.base / "cancelled")
        self.assertEqual(process.call_count, 1)
        self.assertTrue((self.base / "cancelled" / ("engine." + rt.VERSION) / "bridge.json").is_file())

    def test_source_mutation_rejected_and_preserved_as_error(self):
        def altered(*args, **kwargs):
            proof = self.fake_private(*args, **kwargs)
            (self.project / "player.gd").write_text("changed")
            return proof
        with self.native(altered), self.assertRaisesRegex(rt.PortableRuntimeError, "Source/config/runtime changed"):
            rt.import_project(self.project, self.base / "changed")
        saved = rt._read(self.base / "changed" / ("engine." + rt.VERSION) / "bridge.json")
        self.assertEqual(saved["status"], "error")
        self.assertNotEqual(saved["source_hashes_before"], saved["source_hashes_after"])

    def test_private_input_or_cleanup_failure_cannot_pass(self):
        for field in ("input", "cleanup", "identity"):
            def altered(*args, **kwargs):
                proof = self.fake_private(*args, **kwargs)
                if field == "input":
                    proof["desktop_samples"][0]["receives_input"] = True
                elif field == "cleanup":
                    proof["owned_process_termination_confirmed"] = False
                else:
                    proof["process_identity"]["creation_filetime_100ns"] = 0
                dump(kwargs["output_dir"] / "process.json", proof)
                return proof
            with self.subTest(field=field), self.native(altered), self.assertRaises(rt.PortableRuntimeError):
                rt.import_project(self.project, self.base / field)

    def test_missing_owned_window_blocks_render(self):
        def altered(*args, **kwargs):
            proof = self.fake_private(*args, **kwargs)
            proof["saw_owned_window"] = False
            dump(kwargs["output_dir"] / "process.json", proof)
            return proof
        with self.native(altered), self.assertRaisesRegex(rt.PortableRuntimeError, "owned private window"):
            rt.capture_project(self.project, self.base / "nowindow", scenario())

    def test_no_output_inside_source_and_no_arbitrary_scene(self):
        with self.native() as process:
            with self.assertRaises(rt.PortableRuntimeError):
                rt.import_project(self.project, self.project / "output")
            output = self.base / "evil"
            self.config(output)
            with self.assertRaises(rt.PortableRuntimeError):
                rt.run_adapter(self.project, output, scene="res://player.tscn", config_path=output / "capture_config.json")
        process.assert_not_called()

    def test_reused_output_and_changed_evidence_rejected(self):
        output = self.base / "import"
        with self.native():
            result = rt.import_project(self.project, output)
            with self.assertRaises(rt.PortableRuntimeError):
                rt.import_project(self.project, output)
        (Path(result["receipt_path"]).parent / "process/process-identity.json").write_text("{}")
        self.assertEqual(rt.verify_runtime_receipt(result["receipt_path"])["status"], "fail")

    def test_environment_isolated_without_cloud_secrets(self):
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "must-not-leak", "KIROCLOPS_CONFIG": "old"}):
            env = rt._environment(self.base / "env")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("KIROCLOPS_CONFIG", env)
        self.assertTrue(Path(env["APPDATA"]).is_relative_to(self.base))

    def test_hardlinked_source_rejected(self):
        os.link(self.project / "player.gd", self.project / "second.gd")
        with self.assertRaises(rt.PortableRuntimeError):
            rt.source_hashes(self.project)

    def test_extra_or_wrong_size_png_rejected(self):
        run = self.base / "frameset"
        (run / "frames").mkdir(parents=True)
        for index in range(240):
            (run / "frames" / f"{index:06d}.png").write_bytes(png_bytes())
        self.assertEqual(len(rt._frames(run)), 240)
        (run / "frames/extra.png").write_bytes(png_bytes())
        with self.assertRaises(rt.PortableRuntimeError):
            rt._frames(run)

    def test_normal_gameplay_needs_observed_run_takeoff_land_brake(self):
        value = scenario(id="normal-combined", start_tick=36, hold_ticks=24,
            events=[{"tick": 30, "action": "right", "pressed": True},
            {"tick": 78, "action": "right", "pressed": False},
            {"tick": 36, "action": "jump", "pressed": True},
            {"tick": 60, "action": "jump", "pressed": False}])
        data = recording(value)
        data["normal_level"] = True
        self.assertEqual(rt.gameplay_checks(data, value)["status"], "fail")
        for index in range(30, 78):
            data["observations"][index]["moving_horizontal"] = True
        for index in range(36, 75):
            data["observations"][index]["on_floor"] = False
            data["observations"][index]["moving_up"] = index < 50
        self.assertEqual(rt.gameplay_checks(data, value)["status"], "pass")
        data["observations"][-1]["on_floor"] = False
        self.assertEqual(rt.gameplay_checks(data, value)["status"], "fail")

    def test_hazard_or_missing_contact_evidence_blocks_normal_pass(self):
        route = scenario(id="normal-combined", start_tick=36, hold_ticks=24,
            events=[{"tick": t, "action": a, "pressed": p} for t,a,p in rt.NORMAL_RECIPES["normal-combined"]])
        value = recording(route)
        value["normal_level"] = True
        for item in value["observations"]:
            item["hazard_entries"] = 1
        self.assertFalse(rt.gameplay_checks(value, route)["checks"]["no_hazard_reset"])
        del value["observations"][0]["on_wall"]
        with self.assertRaises(rt.PortableRuntimeError):
            rt.gameplay_checks(value, route)

    def test_slope_requires_actual_ground_contact_and_elevation_not_just_a_state_label(self):
        route = scenario(id="normal-slope", events=[{"tick":t,"action":a,"pressed":p}
                           for t,a,p in rt.NORMAL_RECIPES["normal-slope"]])
        del route["start_tick"], route["hold_ticks"]
        data = recording(route)
        data["normal_level"] = True
        for index in range(30, 78):
            data["observations"][index]["moving_horizontal"] = True
        self.assertEqual(rt.gameplay_checks(data, route)["status"], "fail")
        for index in range(40, 45):
            data["observations"][index].update(ground_decline=True, ground_descent=True)
        for index in range(60, 65):
            data["observations"][index].update(ground_incline=True, ground_rise=True)
        self.assertEqual(rt.gameplay_checks(data, route)["status"], "pass")
        data["observations"][50]["on_floor"] = False
        self.assertEqual(rt.gameplay_checks(data, route)["status"], "fail")

    def test_cold_theme_dependency_import_retries_once_with_original_error_bound(self):
        count = 0
        def cold(*args, **kwargs):
            nonlocal count
            count += 1
            proof = self.fake_private(*args, **kwargs)
            if count == 1:
                product = self.project / ".godot/imported/black_9slice.png-deadbeef.ctex"
                product.parent.mkdir(parents=True)
                product.write_bytes(b"imported")
                log = kwargs["output_dir"] / "stderr.log"
                log.write_text("ERROR: Unable to open file: res://.godot/imported/black_9slice.png-deadbeef.ctex.\n"
                    "ERROR: Failed loading resource: res://black_9slice.png.\n", encoding="utf-8")
                proof["evidence"]["stderr.log"] = {"sha256": rt._sha(log), "bytes": log.stat().st_size}
                dump(kwargs["output_dir"] / "process.json", proof)
            return proof
        with self.native(cold):
            result = rt.import_project(self.project, self.base / "cold")
        self.assertEqual(count, 2)
        self.assertEqual(len(result["import_attempts"]), 2)
        first = rt._read(result["import_attempts"][0]["path"])
        self.assertEqual(first["status"], "error")
        self.assertEqual(rt.verify_runtime_receipt(result["receipt_path"])["status"], "pass")
        self.assertTrue(Path(result["import_summary_path"]).is_file())

    def test_real_script_error_never_uses_dependency_retry(self):
        def broken(*args, **kwargs):
            proof = self.fake_private(*args, **kwargs)
            log = kwargs["output_dir"] / "stderr.log"
            log.write_text("SCRIPT ERROR: Parse Error: unexpected code\n", encoding="utf-8")
            proof["evidence"]["stderr.log"] = {"sha256": rt._sha(log), "bytes": log.stat().st_size}
            dump(kwargs["output_dir"] / "process.json", proof)
            return proof
        with self.native(broken) as process, self.assertRaises(rt.PortableRuntimeError):
            rt.import_project(self.project, self.base / "script-error")
        self.assertEqual(process.call_count, 1)


if __name__ == "__main__":
    unittest.main()
