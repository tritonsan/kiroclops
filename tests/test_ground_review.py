"""Operator-review transactions with synthetic files and mocked CV reanalysis."""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import evaluation, ground_review as review, ground_worker, storage

REAL_CONTEXT=review._context


class GroundReviewTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.directory=self.root/"sessions/review"
        self.candidate_root=self.directory/"candidates/candidate-01"
        for project,text in ((self.directory/"baseline","before"),(self.candidate_root/"project","after")):
            project.mkdir(parents=True)
            (project/"player.gd").write_text(text)
        self.baseline_hashes=storage.project_hashes(self.directory/"baseline")
        self.candidate_hashes=storage.project_hashes(self.candidate_root/"project")
        self.groups={m:{r:[] for r in ("reference","baseline","candidate")} for m in ("ground","jump")}
        self.annotations={}
        self.source_annotations={}
        for game in ("tiny","slay"):
            source=self.root/"source"/game
            (source/"frames").mkdir(parents=True)
            storage.write_json(source/"manifest.json",{"game":game,"status":"ok","frame_count":240,"fps":60,"viewport":[200,100]})
            for index in range(240):
                (source/"frames"/f"{index:06d}.png").write_bytes(f"native-{game}-{index}".encode())
            ann={"schema_version":1,"template_run":str(source),"template_frame":0,"roi":[10,10,8,8],
                 "anchor_offset":[3,3],"body_height_px":20,"background_rois":[[30,30,8,8]],"horizontal_mirror":True,
                 "control_points":[],"control_events":[]}
            storage.write_json(self.root/f"config/phase2b/{game}-annotation.json",ann)
            self.source_annotations[game]=ann
        for mechanic in self.groups:
            for role in self.groups[mechanic]:
                run=self.directory/"captures"/mechanic/role
                (run/"frames").mkdir(parents=True)
                game="tiny" if role=="reference" else "slay"
                variant={"reference":"reference","baseline":"before","candidate":"after"}[role]
                source=self.root/"source"/game
                for index in range(240):
                    (run/"frames"/f"{index:06d}.png").write_bytes((source/"frames"/f"{index:06d}.png").read_bytes())
                manifest={"game":game,"variant":variant,"status":"ok","fps":60,"frame_count":240,"viewport":[200,100],
                          "scenario":{"split":"validation"}}
                storage.write_json(run/"manifest.json",manifest)
                annotation=(self.directory/"annotations/ground"/f"{game}-annotation.json" if mechanic=="ground"
                            else evaluation.controls_path(run,self.directory/"annotations/jump"))
                storage.write_json(annotation,self.source_annotations[game])
                storage.write_json(run/"analysis.json",{"game":game,"variant":variant,"fps":60,"body_height_px":20,
                    "metrics":{"value":1},"frames":[{"frame_index":30,"x":12,"y":13}],"status":"ok",
                    "annotation_validation":{"status":"pending"},"provenance":{"manifest_sha256":storage.sha256(run/"manifest.json"),
                    "annotation_sha256":storage.sha256(annotation)}})
                self.groups[mechanic][role].append(run)
                self.annotations[run]=annotation
        self.runs=[r for groups in self.groups.values() for runs in groups.values() for r in runs]
        self.candidate={"candidate_id":"candidate-01","status":"finalized","source_hashes":self.candidate_hashes}
        storage.write_json(self.candidate_root/"candidate.json",self.candidate)
        self.comparison=self.make_comparison("pending")
        self.comparison_path=self.directory/"final/comparison.json"
        storage.write_json(self.comparison_path,self.comparison)
        self.state={"session_id":"review","experiment_id":review.GROUND_ID,"status":"finished","sealed_at":"fixed",
                    "selected_candidate_id":"candidate-01","active_job_id":None,"baseline":str(self.directory/"baseline"),
                    "baseline_hashes":self.baseline_hashes,"candidates":[self.candidate],
                    "final":{"candidate_id":"candidate-01","comparison_path":str(self.comparison_path),
                    "numerical_status":"pass","manual_validation_status":"pending","measurement_status":"pending","overall_status":"pending"}}
        storage.write_json(self.directory/"session.json",self.state)
        for target,value in ((storage,"ROOT"),(ground_worker,"ROOT")):
            p=patch.object(target,value,self.root);p.start();self.addCleanup(p.stop)
        p=patch.object(review,"_context",side_effect=self.context);p.start();self.addCleanup(p.stop)
        p=patch.object(review.ground_analysis,"analyze_run",side_effect=self.reanalyze);self.analyze=p.start();self.addCleanup(p.stop)
        p=patch.object(evaluation,"reanalyze_controls",side_effect=lambda run,directory:self.reanalyze(run,self.annotations[run]));p.start();self.addCleanup(p.stop)
        p=patch.object(review,"_compare",side_effect=lambda context:self.make_comparison("pass"));self.compare=p.start();self.addCleanup(p.stop)

    def context(self,directory):
        session=review.Session(directory)
        review._require_source(self.directory/"baseline",self.baseline_hashes)
        review._require_source(self.candidate_root/"project",self.candidate_hashes)
        return {"session":session,"state":session.read(),"candidate":storage.read_json(self.candidate_root/"candidate.json"),
                "candidate_root":self.candidate_root,"runs":self.runs,"groups":self.groups,
                "annotations":self.annotations,"comparison_path":self.comparison_path}

    @staticmethod
    def make_comparison(manual):
        section={"numerical_status":"pass","gates":{"max_rmse":.15},"validation_scenarios":[{"after_rmse_body":.07}],
                 "coverage":{"complete_triplets":18},"mean_rmse_body":.07,"manual_validation_status":manual}
        return ground_worker.combine_comparisons(deepcopy(section),deepcopy(section),{"status":"diagnostic"})

    def reanalyze(self,run,annotation):
        data=storage.read_json(run/"analysis.json")
        data["annotation_validation"]={"status":"pass"}
        data["provenance"]["annotation_sha256"]=storage.sha256(annotation)
        storage.write_json(run/"analysis.json",data)
        return data

    def control(self,role="candidate"):
        run=self.groups["ground"][role][0]
        return {"run":str(run),"points":[{"frame_index":30,"x":12,"y":13,
                "frame_sha256":storage.sha256(run/"frames/000030.png"),"evidence":"synthetic explicit visual observation"}]}

    def snapshot(self):
        paths={self.directory/"session.json",self.comparison_path,*self.annotations.values(),*(r/"analysis.json" for r in self.runs)}
        return {p:p.read_bytes() for p in paths}

    def test_explicit_all_role_controls_and_bound_read_only_receipt(self):
        controls=[self.control(r) for r in ("reference","baseline","candidate")]
        result=review.review_session(self.directory,controls,source="assistant_visual")
        self.assertEqual(result["status"],"pass")
        record=storage.read_json(result["review_path"])
        self.assertEqual(len(record["runs"]),6)
        self.assertTrue(record["measurements_unchanged"])
        for role in ("reference","baseline","candidate"):
            run=self.groups["ground"][role][0]
            points=storage.read_json(self.annotations[run])["control_points"]
            self.assertTrue(any(p["run_id"]==str(run) and p["source"]=="assistant_visual" for p in points))
        before=self.snapshot()
        checked=review.verify_review_receipt(self.directory)
        self.assertEqual(checked["status"],"pass",checked)
        self.assertEqual(checked["manual_validation_status"],"pass")
        self.assertEqual(self.snapshot(),before)

    def test_missing_receipt_unverified(self):
        self.assertEqual(review.verify_review_receipt(self.directory)["status"],"unverified")

    def test_unused_session_with_explicit_null_final_is_read_only_unverified(self):
        from kiroclops.integration.source_review import verify_source_review
        directory=self.root/"sessions/unused-ide"
        path=directory/"session.json"
        storage.write_json(path,{"schema_version":3,"experiment_id":review.GROUND_ID,
            "session_id":"unused-ide","status":"new","candidates":[],"selected_candidate_id":None,
            "active_job_id":None,"final":None})
        original=path.read_bytes()
        with patch.object(review,"_context",side_effect=AssertionError("Absent receipt must not validate captures")):
            checked=review.verify_review_receipt(directory)
        self.assertEqual(checked["status"],"unverified")
        self.assertEqual(verify_source_review(directory)["status"],"pending")
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual([p.relative_to(directory).as_posix() for p in directory.rglob('*')],["session.json"])
        self.assertEqual(self.analyze.call_count,0)

    def test_malformed_final_is_failed_rather_than_missing_or_passed(self):
        for final in ([],"invalid",True):
            storage.write_json(self.directory/"session.json",{**self.state,"final":final})
            with self.subTest(final=final):
                self.assertEqual(review.verify_review_receipt(self.directory)["status"],"fail")

    def test_receipt_detects_raw_annotation_analysis_and_comparison_changes(self):
        review.review_session(self.directory,[self.control()])
        paths=[self.runs[0]/"frames/000100.png",self.annotations[self.runs[0]],self.runs[0]/"analysis.json",self.comparison_path,
               self.candidate_root/"candidate.json",self.candidate_root/"project/player.gd"]
        for path in paths:
            original=path.read_bytes()
            with self.subTest(path=str(path)):
                path.write_bytes(original+b" ")
                self.assertEqual(review.verify_review_receipt(self.directory)["status"],"fail")
                path.write_bytes(original)
        self.assertEqual(review.verify_review_receipt(self.directory)["status"],"pass")

    def test_measurement_change_rolls_back_all_review_outputs(self):
        before=self.snapshot()
        def changed(run,annotation):
            value=self.reanalyze(run,annotation)
            value["metrics"]["value"]=2
            storage.write_json(run/"analysis.json",value)
        self.analyze.side_effect=changed
        with self.assertRaisesRegex(storage.IntegrationError,"image-derived"):
            review.review_session(self.directory,[self.control()])
        self.assertEqual(before,self.snapshot())
        self.assertEqual(list((self.directory/"final").glob("ground-review-*.json")),[])

    def test_keyboard_interrupt_rolls_back_partial_review(self):
        before=self.snapshot()
        def interrupted(run,annotation):
            self.reanalyze(run,annotation)
            raise KeyboardInterrupt()
        self.analyze.side_effect=interrupted
        with self.assertRaises(KeyboardInterrupt):review.review_session(self.directory,[self.control()])
        self.assertEqual(before,self.snapshot())

    def test_linked_analysis_or_annotation_cannot_write_outside_session(self):
        for path in (self.runs[0]/"analysis.json",self.annotations[self.runs[0]]):
            original=path.read_bytes()
            outside=self.root/f"external-{path.name}"
            outside.write_bytes(original)
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError as exc:
                path.write_bytes(original)
                self.skipTest(f"Native symlink creation unavailable: {exc}")
            try:
                with self.assertRaises(storage.IntegrationError):review.review_session(self.directory,[self.control()])
                self.assertEqual(outside.read_bytes(),original)
            finally:
                path.unlink()
                path.write_bytes(original)
        self.assertEqual(self.analyze.call_count,0)

    def test_windows_reparse_component_rejected_even_when_target_stays_inside(self):
        path=self.annotations[self.runs[0]]
        original_lstat=Path.lstat
        class ReparseStat:
            st_file_attributes=0x400
            st_mode=original_lstat(path.parent).st_mode
        def flagged(item,*args,**kwargs):
            if item==path.parent:return ReparseStat()
            return original_lstat(item,*args,**kwargs)
        with patch.object(Path,"lstat",flagged):
            with self.assertRaisesRegex(storage.IntegrationError,"reparse"):
                review._local_file(path,self.directory)

    def test_hardlinked_analysis_or_annotation_rejected_before_review_mutations(self):
        for path in (self.runs[0]/"analysis.json",self.annotations[self.runs[0]]):
            with self.subTest(path=str(path)):
                before=self.snapshot()
                original=path.read_bytes()
                outside=self.root/f"external-hardlink-{path.name}"
                outside.write_bytes(original)
                path.unlink()
                path.hardlink_to(outside)
                try:
                    self.assertEqual(path.stat().st_nlink,2)
                    with self.assertRaisesRegex(storage.IntegrationError,"Hardlinked"):
                        review.review_session(self.directory,[self.control()])
                    self.assertEqual(outside.read_bytes(),original)
                    self.assertEqual(self.snapshot(),before)
                    self.assertEqual(list((self.directory/"final").glob("ground-review-*.json")),[])
                finally:
                    path.unlink()
                    path.write_bytes(original)
        self.assertEqual(self.analyze.call_count,0)

    def test_predictable_jump_staging_file_is_never_overwritten(self):
        run=self.groups["jump"]["candidate"][0]
        staged=run/"analysis.manual-review.json.tmp"
        staged.write_bytes(b"preexisting review state")
        with self.assertRaisesRegex(storage.IntegrationError,"staging"):
            review.review_session(self.directory,[self.control()])
        self.assertEqual(staged.read_bytes(),b"preexisting review state")
        self.assertEqual(self.analyze.call_count,0)

    def test_changed_numeric_gate_rolls_back(self):
        before=self.snapshot()
        changed=self.make_comparison("pass")
        changed["ground"]["gates"]["max_rmse"]=.2
        self.compare.side_effect=lambda context:changed
        with self.assertRaisesRegex(storage.IntegrationError,"numerical"):
            review.review_session(self.directory,[self.control()])
        self.assertEqual(before,self.snapshot())

    def test_explicit_controls_reject_nonfinite_stale_generated_and_outside(self):
        for field,value in (("x",float("nan")),("frame_sha256","0"*64),("frame_index",True)):
            control=self.control();control["points"][0][field]=value
            with self.assertRaises(storage.IntegrationError):review.review_session(self.directory,[control])
        with self.assertRaises(storage.IntegrationError):review.review_session(self.directory,[self.control()],source="tracker")
        control=self.control();control["run"]=str(self.root/"source/slay")
        with self.assertRaises(storage.IntegrationError):review.review_session(self.directory,[control])
        self.assertEqual(self.analyze.call_count,0)

    def test_reuse_exact_pixels_preserves_direct_origin_and_detects_source_mutation(self):
        annotation=deepcopy(self.source_annotations["slay"])
        source=self.root/"source/slay"
        annotation["control_points"]=[{"run_id":str(source),"frame_index":30,"x":12,"y":13,"source":"assistant_visual",
                                        "frame_sha256":storage.sha256(source/"frames/000030.png")}]
        path=self.root/"observations/slay.json"
        storage.write_json(path,annotation)
        result=review.review_session(self.directory,reuse_ground_controls_from=path)
        self.assertEqual(result["reused_ground_controls"]["points"],2)
        candidate=self.groups["ground"]["candidate"][0]
        p=next(p for p in storage.read_json(self.annotations[candidate])["control_points"] if p["run_id"]==str(candidate))
        self.assertEqual(p["reuse_origin"]["run_id"],str(source))
        self.assertEqual(review.verify_review_receipt(self.directory)["status"],"pass")
        path.write_bytes(path.read_bytes()+b" ")
        self.assertEqual(review.verify_review_receipt(self.directory)["status"],"fail")

    def test_reuse_pixel_mismatch_never_remaps_or_changes_coordinates(self):
        annotation=deepcopy(self.source_annotations["slay"])
        source=self.root/"source/slay"
        annotation["control_points"]=[{"run_id":str(source),"frame_index":30,"x":12,"y":13,"source":"human",
                                        "frame_sha256":storage.sha256(source/"frames/000030.png")}]
        path=self.root/"observations/slay.json";storage.write_json(path,annotation)
        for role in ("baseline","candidate"):
            (self.groups["ground"][role][0]/"frames/000030.png").write_bytes(b"different pixels")
        result=review.review_session(self.directory,reuse_ground_controls_from=path)
        self.assertEqual(result["reused_ground_controls"]["points"],0)

    def test_failed_transaction_restores_even_after_jump_reuse(self):
        before=self.snapshot()
        def broken(*args):
            storage.write_json(self.annotations[self.groups["jump"]["candidate"][0]],{"modified":True})
            raise ValueError("source observation changed")
        with patch.object(review.control_reuse,"reuse_controls",side_effect=broken):
            with self.assertRaises(ValueError):review.review_session(self.directory,reuse_jump_controls_from=self.root/"other")
        self.assertEqual(before,self.snapshot())

    def test_existing_receipts_are_preserved_on_subsequent_review(self):
        first=review.review_session(self.directory,[self.control()])
        path=Path(first["review_path"]);raw=path.read_bytes()
        second=review.review_session(self.directory)
        self.assertNotEqual(first["review_path"],second["review_path"])
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(review.verify_review_receipt(self.directory)["status"],"pass")

    def test_valid_receipt_can_honestly_remain_manual_pending(self):
        self.compare.side_effect=lambda context:self.make_comparison("pending")
        result=review.review_session(self.directory)
        self.assertEqual(result["status"],"pending")
        checked=review.verify_review_receipt(self.directory)
        self.assertEqual(checked["status"],"pass")
        self.assertEqual(checked["manual_validation_status"],"pending")

    def test_copied_origin_requires_exact_canonical_whole_png_sequence(self):
        run=self.groups["ground"]["reference"][0]
        original=self.root/"artifacts/phase2b/reference"/run.name
        shutil.copytree(run,original)
        files={"manifest.json":storage.sha256(original/"manifest.json"),
               **{f"frames/{i:06d}.png":storage.sha256(original/"frames"/f"{i:06d}.png") for i in range(240)}}
        storage.write_json(run/"copy-origin.json",{"source":str(original),"hashes":files})
        review._verify_origin(run,"ground")
        (run/"frames/000199.png").write_bytes(b"wrong later frame")
        with self.assertRaisesRegex(storage.IntegrationError,"native frames"):
            review._verify_origin(run,"ground")

    def test_real_context_requires_complete_matrix_before_any_review(self):
        state=storage.read_json(self.directory/"session.json")
        state["reference"]={"calibration":[],"validation":[],"jump":{}}
        state["baseline_runs"]={"calibration":[],"validation":[],"jump":{}}
        storage.write_json(self.directory/"session.json",state)
        with patch.object(ground_worker,"verify_session",return_value=state):
            with self.assertRaisesRegex(storage.IntegrationError,"Incomplete ground reference calibration"):
                REAL_CONTEXT(self.directory)

    def test_real_context_rejects_wrong_candidate_snapshot(self):
        state=storage.read_json(self.directory/"session.json")
        candidate=storage.read_json(self.candidate_root/"candidate.json")
        candidate["status"]="evaluated"
        storage.write_json(self.candidate_root/"candidate.json",candidate)
        with patch.object(ground_worker,"verify_session",return_value=state):
            with self.assertRaisesRegex(storage.IntegrationError,"metadata differs"):
                REAL_CONTEXT(self.directory)


if __name__=="__main__":
    unittest.main()
