"""Synthetic contract/identity tests. No Godot, GUI or model process is started."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from kiroclops.integration import airborne_regression_v1 as air


def recording():
    cases=[]
    for index,spec in enumerate(air.CASES):
        steps=[]
        for step in range(4):
            f=index*8+3+step
            snapshot={"state":spec["state"],"on_floor":False,"on_wall":False,
                "horizontal_rest":spec["state"]=="Idle","moving_up":spec["motion"]=="rising",
                "moving_down":spec["motion"]=="falling","actions":air._actions(f),"jump_just_pressed":False,
                "horizontal_response_sha256":"0"*64}
            steps.append({"step_index":step,"frame_index":f,"physics_tick":60+f,"pre":snapshot,
                "post":deepcopy(snapshot),"predicates":{k:False for k in air.PREDICATES}})
        cases.append({"case_id":spec["case_id"],"contact_refresh":[{"frame_index":index*8+n,"airborne":True,"no_wall":True} for n in (1,2)],"steps":steps})
    return {"schema_version":1,"protocol":air.VERSION,"status":"ok","fps":60,"viewport":[640,360],
        "frame_count":240,"warmup_ticks":60,"frames":[{"frame_index":i,"physics_tick":60+i,"time_s":i/60,"actions":air._actions(i)} for i in range(240)],"cases":cases}


class AirborneRecordingTests(unittest.TestCase):
    def test_all_eighteen_same_seed_cases_have_seventy_two_valid_steps(self):
        result=air.evaluate_recording(recording())
        self.assertEqual(result,{"status":"valid","invalid_preconditions":[],"case_count":18,"observed_steps":72})

    def test_raw_kinematics_are_rejected_in_any_snapshot(self):
        for key in ("velocity_x","x","acceleration"):
            value=recording();value["cases"][0]["steps"][0]["pre"][key]=1.0
            with self.subTest(key=key),self.assertRaises(air.SuiteError):air.evaluate_recording(value)

    def test_missing_case_duplicate_case_and_missing_step_cannot_pass(self):
        values=[]
        value=recording();value["cases"].pop();values.append(value)
        value=recording();value["cases"][1]["case_id"]=value["cases"][0]["case_id"];values.append(value)
        value=recording();value["cases"][0]["steps"].pop();values.append(value)
        for value in values:
            with self.assertRaises(air.SuiteError):air.evaluate_recording(value)

    def test_bad_contact_seed_input_and_refresh_are_probe_invalid(self):
        edits=[lambda v:v["cases"][0]["contact_refresh"][0].update(airborne=False),
               lambda v:v["cases"][1]["steps"][0]["pre"].update(on_floor=True),
               lambda v:v["cases"][1]["steps"][0]["pre"].update(moving_up=False),
               lambda v:v["cases"][1]["steps"][1]["post"].update(on_wall=True),
               lambda v:v["cases"][1]["steps"][0]["pre"].update(jump_just_pressed=True)]
        for edit in edits:
            value=recording();edit(value)
            with self.subTest(edit=edit):self.assertEqual(air.evaluate_recording(value)["status"],"probe_invalid")

    def test_frame_or_prepost_phase_mismatch_is_invalid(self):
        edits=[lambda v:v["frames"][3].update(time_s=float("nan")),
               lambda v:v["frames"][11].update(actions=[]),
               lambda v:v["cases"][0]["steps"][0].update(physics_tick=64),
               lambda v:v["cases"][0]["steps"][0]["predicates"].update(horizontal_velocity_unchanged=1)]
        for edit in edits:
            value=recording();edit(value)
            with self.subTest(edit=edit),self.assertRaises(air.SuiteError):air.evaluate_recording(value)


class AirborneSuiteTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.source=self.root/"source";self.source.mkdir()
        (self.source/"project.godot").write_text("[application]\n")
        (self.source/"player.gd").write_text("original controller")
        self.expected=air.project_hashes(self.source)
        self.native_calls=[];self.value=recording();self.mutate=None
        for name,value in [("_godot_lock",lambda **kw:nullcontext()),("_engine",lambda:self.root/"engine.exe")]:
            mock=patch.object(air.runner,name,value);mock.start();self.addCleanup(mock.stop)
        mock=patch.object(air.runtime,"isolated_runtime",lambda **kw:nullcontext());mock.start();self.addCleanup(mock.stop)
        mock=patch.object(air.runtime,"verify_runtime_receipt",return_value={"status":"pass"});mock.start();self.addCleanup(mock.stop)
        mock=patch.object(air.runner,"_run_process",side_effect=self.process);self.process_mock=mock.start();self.addCleanup(mock.stop)

    def process(self,args,*,cwd,env,log_path,**kwargs):
        log_path=Path(log_path);log_path.write_text("mock process")
        if "--import" in args:return 0
        self.native_calls.append(args)
        out=log_path.parent
        (out/"recording.json").write_text(json.dumps(self.value))
        for i in range(240):
            # Only a fixture header; no synthetic image is presented as native evidence.
            (out/"frames"/f"{i:06d}.png").write_bytes(b"\x89PNG\r\n\x1a\n"+b"\0"*8+struct.pack(">II",640,360))
        proof=out/("engine."+air.runtime.VERSION);proof.mkdir()
        (proof/"bridge.json").write_text("{}")
        if self.mutate:self.mutate(Path(cwd))
        return 0

    def run_suite(self,name="run"):
        return air.run_suite(self.source,self.root/name,expected_source_hashes=self.expected)

    def test_copy_and_single_private_route_preserve_original_and_verify(self):
        out=self.run_suite()
        self.assertEqual(air.verify_suite(out,expected_source_hashes=self.expected)["status"],"pass")
        self.assertEqual(len(self.native_calls),1)
        self.assertEqual(air.project_hashes(self.source),self.expected)
        prepared=air.project_hashes(out/"project")
        self.assertEqual(set(prepared)-set(self.expected),{air.ADAPTER_DIR+"/Capture.tscn",air.ADAPTER_DIR+"/capture.gd"})
        self.assertEqual(self.process_mock.call_count,2)

    def test_cancel_before_prepare_writes_nothing(self):
        cancel=threading.Event();cancel.set();out=self.root/"cancel"
        with self.assertRaises(air.SuiteError):air.run_suite(self.source,out,expected_source_hashes=self.expected,cancel=cancel)
        self.assertFalse(out.exists());self.process_mock.assert_not_called()

    def test_existing_output_and_wrong_source_are_refused(self):
        out=self.root/"existing";out.mkdir()
        for destination,source in [(out,self.expected),(self.root/"bad",{})]:
            with self.assertRaises(air.SuiteError):air.run_suite(self.source,destination,expected_source_hashes=source)
        self.process_mock.assert_not_called()

    def test_mutated_actor_or_adapter_never_publishes_completed_suite(self):
        self.mutate=lambda project:(project/"player.gd").write_text("changed")
        with self.assertRaises(air.SuiteError):self.run_suite()
        self.assertFalse((self.root/"run/suite.json").exists())
        self.assertTrue((self.root/"run/failure.json").exists())

    def test_missing_suite_unverified_and_stale_source_fails(self):
        self.assertEqual(air.verify_suite(self.root/"absent")["status"],"unverified")
        out=self.run_suite();(self.source/"player.gd").write_text("later source")
        self.assertEqual(air.verify_suite(out)["status"],"fail")

    def test_raw_recording_or_png_mutation_fails_fresh_replay(self):
        out=self.run_suite();(out/"capture/frames/000003.png").write_bytes(b"wrong")
        self.assertEqual(air.verify_suite(out)["status"],"fail")

    def test_differential_equal_and_extra_air_update_never_publish_values(self):
        baseline=self.run_suite("baseline");candidate=self.run_suite("candidate")
        self.assertEqual(air.compare_suites(baseline,candidate)["status"],"pass")
        self.value=recording();case=self.value["cases"][1];case["steps"][0]["post"]["horizontal_response_sha256"]="1"*64
        case["steps"][0]["predicates"]["horizontal_velocity_unchanged"]=True
        negative=self.run_suite("negative");result=air.compare_suites(baseline,negative)
        self.assertEqual(result["status"],"fail");self.assertEqual(result["summary"]["failed_cases"],1)
        self.assertIn("post.horizontal_response_sha256",result["mismatches"][0]["fields"])
        self.assertNotIn("1"*64,json.dumps(result));self.assertNotIn("velocity_x",json.dumps(result))

    def test_invalid_baseline_or_candidate_never_becomes_behavior_pass(self):
        baseline=self.run_suite("baseline")
        self.value=recording();self.value["cases"][0]["steps"][0]["pre"]["on_floor"]=True
        invalid=self.run_suite("invalid")
        self.assertEqual(air.verify_suite(invalid)["status"],"probe_invalid")
        for a,b in [(baseline,invalid),(invalid,baseline)]:self.assertEqual(air.compare_suites(a,b)["status"],"probe_invalid")
        self.assertEqual(air.compare_suites(baseline,self.root/"missing")["status"],"unverified")

    def test_changed_suite_policy_and_runtime_proof_fail(self):
        out=self.run_suite();receipt=json.loads((out/"suite.json").read_text())
        receipt["protocol"]["observed_steps_per_case"]=1;(out/"suite.json").write_text(json.dumps(receipt))
        self.assertEqual(air.verify_suite(out)["status"],"fail")

    def test_texture_seed_validates_original_before_adapter_and_import_still_runs(self):
        def seed(cache_dir,project,**kwargs):
            self.assertEqual(kwargs["expected_source_hashes"],self.expected)
            self.assertEqual(air.project_hashes(project),self.expected)
            self.assertFalse((project/air.ADAPTER_DIR).exists())
            return {"status":"seeded","project":str(project),"full_source_hashes":self.expected}
        with patch.object(air.asset_cache_module,"seed_import_cache",side_effect=seed) as mocked:
            out=air.run_suite(self.source,self.root/"seeded",expected_source_hashes=self.expected,
                asset_cache={"cache_dir":"explicit-cache","manifest_sha256":"a"*64})
        mocked.assert_called_once()
        self.assertEqual(self.process_mock.call_count,2)
        self.assertIn("--import",self.process_mock.call_args_list[0].args[0])
        self.assertIsNotNone(json.loads((out/"suite.json").read_text())["asset_seed"])

    def test_invalid_cache_does_not_fall_back_to_cold_import(self):
        with self.assertRaises(air.SuiteError):air.run_suite(self.source,self.root/"badcache",expected_source_hashes=self.expected,asset_cache={})
        self.process_mock.assert_not_called()

    def test_qualification_absent_or_wrong_protocol_cannot_pass(self):
        self.assertEqual(air.verify_qualification(self.root/"missing")["status"],"unverified")
        out=self.root/"qualification";out.mkdir();(out/"qualification.json").write_text(json.dumps({"status":"pass","protocol":{}}))
        self.assertEqual(air.verify_qualification(out)["status"],"fail")

if __name__=="__main__":unittest.main()
