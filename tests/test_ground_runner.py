"""Ground schedule contracts with fake engine output; no Godot or model runs."""
from copy import deepcopy
import itertools
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kiroclops.common import read_json, write_json
from kiroclops.integration import ground_runner as ground, runner as legacy


def scenario(events=None):
    return dict(id="right18", split="validation", repeat=1, spawn_offset=0, events=events if events is not None else [
        dict(tick=30, action="right", pressed=True), dict(tick=48, action="right", pressed=False)],
        frame_count=240, warmup_ticks=60)


class ScenarioTests(unittest.TestCase):
    def test_reversal_releases_before_press_without_mutating_caller(self):
        original = scenario([dict(tick=30,action="right",pressed=True), dict(tick=54,action="left",pressed=True),
                             dict(tick=54,action="right",pressed=False),dict(tick=78,action="left",pressed=False)])
        before = deepcopy(original)
        result = ground.canonical_scenario(original)
        self.assertEqual(original, before)
        self.assertEqual([(e["tick"],e["pressed"]) for e in result["events"]], [(30,True),(54,False),(54,True),(78,False)])

    def test_simultaneous_direction_duplicate_edge_missing_release_and_bad_types_rejected(self):
        schedules = [
            [dict(tick=30,action="right",pressed=True),dict(tick=31,action="left",pressed=True)],
            [dict(tick=30,action="right",pressed=True)] * 2,
            [dict(tick=30,action="right",pressed=False)],
            [dict(tick=30,action="right",pressed=True)],
            [dict(tick=True,action="right",pressed=True)],
            [dict(tick=30,action="ui_right",pressed=True)],
            [dict(tick=30,action="right",pressed=1)],
        ]
        for events in schedules:
            with self.subTest(events=events), self.assertRaises(ground.RunnerError):
                ground.canonical_scenario(scenario(events))

    def test_fixed_clock_and_scenario_shape_enforced(self):
        for update in ({"frame_count":239},{"warmup_ticks":0},{"repeat":True},{"spawn_offset":float("nan")},
                       {"id":"../../other"},{"events":{}},{"hold_ticks":18}):
            with self.subTest(update=update),self.assertRaises(ground.RunnerError):
                ground.canonical_scenario(dict(scenario(),**update))


class GroundRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Horizontal café"
        for module in (ground, legacy):
            p = patch.object(module,"ROOT",self.root); p.start(); self.addCleanup(p.stop)
        for game in ("tiny","slay"):
            for name in ("capture.gd","Capture.tscn"):
                path=ground._adapter(game)/name; path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text("fixture-"+game+name,encoding="utf-8")
        write_json(self.root/"config/sources.json",{"tiny":{"commit":"tiny-pinned"},"slay":{"commit":"slay-pinned"}})
        self.projects = {}
        for game in ("tiny","slay"):
            project=self.root/"sessions/experiment"/game
            for name in ground._REQUIRED[game]:
                path=project/name;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('[application]\nconfig/name="game"\n' if name=="project.godot" else "original",encoding="utf-8")
            ground.prepare_project(project,game)
            self.projects[game]=project
        p=patch.object(ground,"engine_path",side_effect=lambda game:self.root/f"godot-{game}.exe")
        p.start();self.addCleanup(p.stop)
        self.output=self.root/"runs/candidate"

    def fake_process(self,args,*,cwd,env,log_path,cancel=None,timeout=120):
        log_path=Path(log_path);log_path.parent.mkdir(parents=True,exist_ok=True)
        if "--version" in args:
            log_path.write_text("engine-version",encoding="utf-8");return 0
        if "--import" in args:
            (Path(cwd)/"generated.gd.uid").write_text("generated")
            log_path.write_text("imported");return 0
        if "--check-only" in args:
            log_path.write_text("clean editor validation");return 0
        if "-framerate" in args:
            Path(args[-1]).write_bytes(b"video");log_path.write_text("");return 0
        config=read_json(env["KIROCLOPS_CONFIG"]);run=Path(config["output_dir"])
        active=set();frames=[]
        for index in range(240):
            for event in config["events"]:
                if event["tick"]==index:
                    (active.add if event["pressed"] else active.remove)(event["action"])
            frames.append(dict(frame_index=index,physics_tick=60+index,time_s=index/60,actions=sorted(active)))
            (run/"frames"/f"{index:06d}.png").write_bytes(b"raw PNG")
        write_json(run/"recording.json",dict(status="ok",fps=60,viewport=[228,128] if config["game"]=="tiny" else [640,360],
                                           frame_count=240,frames=frames,settled=True))
        log_path.write_text("KIROCLOPS_USER_DIR="+env["APPDATA"],encoding="utf-8")
        return 0

    def capture(self,game="slay",**kwargs):
        return ground.capture_project(self.projects[game],self.output,game=game,scenario=scenario(),
            variant="reference" if game=="tiny" else "after",video=False,**kwargs)

    def test_both_native_game_captures_use_logical_schedule_and_full_source_provenance(self):
        for game in ("tiny","slay"):
            self.output=self.root/"runs"/game
            with self.subTest(game=game),patch.object(legacy,"_run_process",side_effect=self.fake_process) as process:
                run=self.capture(game)
            manifest=read_json(run/"manifest.json")
            self.assertEqual(manifest["scenario"],scenario())
            self.assertEqual(manifest["frames"][30]["actions"],["right"])
            self.assertEqual(manifest["frames"][48]["actions"],[])
            self.assertFalse(any("jump" in frame["actions"] for frame in manifest["frames"]))
            provenance=manifest["provenance"]
            self.assertEqual(provenance["full_source_hashes_before"],provenance["full_source_hashes_after"])
            self.assertIn("res://kiroclops_ground/Capture.tscn",process.call_args_list[0].args[0])
            self.assertTrue(Path(process.call_args_list[0].kwargs["env"]["APPDATA"]).is_relative_to(self.root/".runtime/phase2b"))

    def test_prepare_is_idempotent_preserves_original_controller_and_legacy_custom_dir(self):
        for game,project in self.projects.items():
            before=ground.source_hashes(project)
            ground.prepare_project(project,game)
            self.assertEqual(before,ground.source_hashes(project))
        self.assertIn(legacy._CUSTOM_DIR,(self.projects["slay"]/"project.godot").read_text())
        self.assertEqual((self.projects["tiny"]/"src/actor/Player.gd").read_text(),"original")

    def test_prepare_refuses_recorded_candidates_and_sealed_sessions(self):
        project=self.projects["slay"]
        write_json(project.parent/"candidate.json",{"status":"finalized"})
        with self.assertRaises(ground.RunnerError):ground.prepare_project(project)
        (project.parent/"candidate.json").unlink()
        write_json(project.parent/"session.json",{"baseline_hashes":{"frozen":"bytes"}})
        with self.assertRaises(ground.RunnerError):ground.prepare_project(project)

    def test_adapter_mutation_or_protected_vendor_copy_rejected_without_engine(self):
        (self.projects["slay"]/"kiroclops_ground/capture.gd").write_text("changed")
        with patch.object(legacy,"_run_process") as process,self.assertRaises(ground.RunnerError) as error:
            self.capture()
        self.assertEqual(error.exception.code,"fixture_changed");process.assert_not_called()

    def test_capture_mutation_rejected_and_failure_preserves_hash_difference(self):
        def mutate(*args,**kwargs):
            result=self.fake_process(*args,**kwargs)
            (self.projects["slay"]/"new_helper.gd").write_text("changed")
            return result
        with patch.object(legacy,"_run_process",side_effect=mutate),self.assertRaises(ground.RunnerError) as error:
            self.capture()
        self.assertEqual(error.exception.code,"source_changed")
        failure=read_json(self.output/"failure.json")
        self.assertNotEqual(failure["full_source_hashes_before"],failure["full_source_hashes_after"])
        self.assertFalse((self.output/"manifest.json").exists())

    def test_missing_png_wrong_phase_nonfinite_time_and_telemetry_fail(self):
        for kind in ("png","phase","time","telemetry"):
            self.output=self.root/"runs"/kind
            def broken(*args,**kwargs):
                result=self.fake_process(*args,**kwargs)
                r=read_json(self.output/"recording.json")
                if kind=="png":(self.output/"frames/000036.png").unlink()
                if kind=="phase":r["frames"][30]["actions"]=[]
                if kind=="time":r["frames"][30]["time_s"]=float("nan")
                if kind=="telemetry":r["frames"][30]["position"]=[1,2]
                (self.output/"recording.json").write_text(json.dumps(r),encoding="utf-8");return result
            with self.subTest(kind=kind),patch.object(legacy,"_run_process",side_effect=broken),self.assertRaises(ground.RunnerError) as error:
                self.capture()
            self.assertEqual(error.exception.code,"recording_error")

    def test_precancelled_never_launches_or_writes_output(self):
        cancel=threading.Event();cancel.set()
        with patch.object(legacy,"_run_process") as process:
            for operation in (lambda:self.capture(cancel=cancel),lambda:ground.import_project(self.projects["slay"],cancel=cancel)):
                with self.assertRaises(ground.RunnerError) as error:operation()
                self.assertEqual(error.exception.code,"cancelled")
            process.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_import_uses_shared_lock_and_preserves_sources(self):
        project=self.projects["slay"];before=ground.source_hashes(project)
        with patch.object(legacy,"_run_process",side_effect=self.fake_process),patch.object(legacy,"_godot_lock") as lock:
            ground.import_project(project,runtime_dir=self.root/"runtime/import")
        lock.assert_called_once();self.assertEqual(before,ground.source_hashes(project))

    def test_tiny_import_ready_products_do_not_bypass_editor_and_cleanup(self):
        project=self.projects["tiny"]
        (project/"asset.png.import").write_text('[remap]\npath="res://.import/asset.stex"')
        (project/".import").mkdir();(project/".import/asset.stex").write_bytes(b"imported")
        runtime=self.root/"runtime/tiny"
        proc=Mock();proc.poll.return_value=None
        with patch.object(ground.subprocess,"Popen",return_value=proc) as process, \
             patch.object(ground.time,"monotonic",side_effect=itertools.count(0,.5)), \
             patch.object(ground.time,"sleep"),patch.object(legacy,"_terminate_owned_tree") as terminate, \
             patch.object(legacy,"_run_process",side_effect=self.fake_process) as validate:
            ground.import_project(project,"tiny",runtime_dir=runtime)
        self.assertIn("--no-window",process.call_args.args[0]);terminate.assert_called_once_with(proc)
        validate.assert_not_called()
        self.assertEqual(read_json(runtime/"import-readiness.json")["status"],"assets_ready")

    def fake_smoke(self, *, old_wall=False):
        def process(args, **kwargs):
            result = self.fake_process(args, **kwargs)
            if Path(kwargs["log_path"]).name != "engine.log":
                return result
            run = Path(kwargs["log_path"]).parent
            trace = [(0,"Fall"),(4,"Idle"),(61,"Run"),(91,"Jump"),(101,"DoubleJump"),(131,"Fall"),(155,"Idle")]
            if run.name == "wall":
                self.assertIn(dict(tick=55,action="jump",pressed=True), read_json(run/"capture_config.json")["events"])
                trace = [(0,"Fall"),(4,"Idle"),(66,"Run"),(91,"Jump"),(101,"Fall"),(102,"WallJump"),(116,"Jump"),(141,"Fall"),(151,"Idle")]
                if old_wall:
                    trace = trace[:6] + [(125,"Idle"),(126,"Run"),(131,"Jump"),(141,"Fall"),(151,"Idle")]
            with Path(kwargs["log_path"]).open("a", encoding="utf-8") as log:
                log.write('\n'+'\n'.join(f'KIROCLOPS_STATE physics_tick={tick} state={state}' for tick,state in trace))
            return result
        with patch.object(legacy,"_run_process",side_effect=process), patch.object(ground.shutil,"which",return_value="ffmpeg"):
            return ground.smoke_project(self.projects["slay"], self.root/"smoke")

    def test_smoke_rejects_the_old_grounded_jump_wall_false_positive(self):
        result = self.fake_smoke(old_wall=True)
        self.assertEqual(result["status"],"failed")
        self.assertFalse(result["checks"]["wall"])
        self.assertEqual(result["full_source_hashes_before"],result["full_source_hashes_after"])
        self.assertEqual(ground.verify_smoke(self.projects["slay"],self.root/"smoke",ground.source_hashes(self.projects["slay"]))["status"],"fail")

    def test_smoke_verifier_replays_sources_actions_frames_and_states_without_engine_or_writes(self):
        result = self.fake_smoke()
        self.assertEqual(result["status"], "passed")
        project, output = self.projects["slay"], self.root/"smoke"
        sources = ground.source_hashes(project)
        before = {str(path): (path.stat().st_mtime_ns, path.read_bytes()) for path in output.rglob("*") if path.is_file()}
        with patch.object(legacy,"_run_process") as process, patch.object(ground,"write_json") as write:
            verified = ground.verify_smoke(project, output, sources)
        process.assert_not_called(); write.assert_not_called()
        self.assertEqual(verified["status"], "pass")
        self.assertEqual(verified["wall_transition"]["jump_tick"], 116)
        self.assertEqual(before, {str(path): (path.stat().st_mtime_ns, path.read_bytes()) for path in output.rglob("*") if path.is_file()})

    def test_smoke_verifier_missing_receipt_unverified_but_changed_evidence_fails(self):
        project, output = self.projects["slay"], self.root/"smoke"
        sources = ground.source_hashes(project)
        self.assertEqual(ground.verify_smoke(project,output,sources)["status"], "unverified")
        self.fake_smoke()
        mutations = {
            "frame": output/"wall/frames/000055.png",
            "config": output/"normal/capture_config.json",
            "manifest": output/"ground/manifest.json",
            "recording": output/"ground/recording.json",
            "log": output/"wall/engine.log",
            "video": output/"normal/video.mp4",
            "source": project/"source/characters/player/player.gd",
        }
        for name, path in mutations.items():
            with self.subTest(name=name):
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assertEqual(ground.verify_smoke(project,output,sources)["status"], "fail")
                path.write_bytes(original)
        self.assertEqual(ground.verify_smoke(project,output,sources)["status"], "pass")

    def test_smoke_verifier_rejects_changed_recipe_even_if_summary_is_rehashed(self):
        self.fake_smoke()
        project, output = self.projects["slay"], self.root/"smoke"
        path = output/"wall/capture_config.json"
        config = read_json(path)
        next(event for event in config["events"] if event["tick"] == 55)["tick"] = 50
        write_json(path, config)
        receipt = read_json(output/"smoke.json")
        receipt["runs"]["wall"]["evidence_sha256"]["capture_config.json"] = ground.sha256(path)
        write_json(output/"smoke.json", receipt)
        result = ground.verify_smoke(project,output,ground.source_hashes(project))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["error_code"], "smoke_protocol_changed")


if __name__=="__main__":unittest.main()
