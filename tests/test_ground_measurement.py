"""Synthetic pixel/model tests; never reads original game motion constants."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from kiroclops.analysis import _match_bank
from kiroclops.ground.analysis import (
    SCENARIOS, analyze_run, measure_ground, mirror_bank, scenario_phases, validate_controls,
)
from kiroclops.ground.annotation import validate_annotation
from kiroclops.ground.comparison import calibration_feedback, compare_runs, curve_rmse
from kiroclops.ground.fitting import LINEAR, RETENTION, fit_profile, simulate_profile


LINEAR_MODEL = {"family": LINEAR, "max_speed_body_s": 4., "accel_body_s2": 30., "brake_body_s2": 15.}
RETENTION_MODEL = {"family": RETENTION, "max_speed_body_s": 4., "accel_body_s2": 30., "retention_per_tick": .84}


def scenario(sid, repeat=1, offset=0):
    return {"id": sid, "split": "calibration" if sid in {"r06", "r48"} else "validation",
            "repeat": repeat, "spawn_offset": offset, "warmup_ticks": 60, "frame_count": 240,
            "events": [{"tick": t, "action": a, "pressed": p} for t,a,p in SCENARIOS[sid]]}


def report(sid="r48", role="reference", *, model=None, repeat=1, offset=0, height=20., quantize=False):
    s = scenario(sid, repeat, offset)
    values = simulate_profile(model or LINEAR_MODEL, s)
    if quantize:
        values = np.round(values*height)/height
    frames = [{"frame_index": i, "time_s": i/60, "x": float(x*height+100+offset), "y": 50.,
               "valid": True, "mirrored": sid == "l30" and i >= 30 or sid == "reverse24" and i >= 54,
               "x_body": float(x)} for i,x in enumerate(values)]
    _, phases, metrics, errors = measure_ground(frames, s, height)
    assert not errors, errors
    return {"schema_version": 1, "mechanic": "ground", "run_id": f"{role}-{sid}-x{offset}-r{repeat}",
            "status": "ok", "game": "tiny" if role == "reference" else "slay", "variant": role,
            "scenario": s, "fps": 60, "body_height_px": height, "camera_valid": True,
            "valid_ratio": 1., "frames": frames, "phases": phases, "metrics": metrics,
            "errors": [], "annotation_validation": {"status": "pending", "points": []}}


def matrix(split="validation", *, after_model=None):
    ids = ("r06", "r48") if split == "calibration" else ("r18", "l30", "reverse24")
    offsets = (0,) if split == "calibration" else (0,24)
    bad = dict(LINEAR_MODEL, max_speed_body_s=6.)
    return [[report(sid, role, repeat=r, offset=o, model=model)
             for sid in ids for o in offsets for r in (1,2,3)]
            for role,model in (("reference", LINEAR_MODEL), ("before", bad), ("after", after_model or LINEAR_MODEL))]


def add_manual(groups, *, outlier=False):
    for reports in groups:
        points = [{"source": "assistant_visual", "hash_bound": True,
                   "frame_sha256": hashlib.sha256(f"synthetic-{i}".encode()).hexdigest(),
                   "x": i, "y": 20., "error_px": 8. if outlier and i == 0 else .2,
                   "categories": ["flip", "brake", "reverse"] if i == 0 else []}
                  for i in range(20)]
        reports[0]["annotation_validation"] = {"status": "pass", "points": points}


class GroundPixelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, *, sid="reverse24", camera=False, occluded=()):
        run = self.root / "raw"
        (run / "frames").mkdir(parents=True)
        rng = np.random.default_rng(982)
        background = rng.integers(10, 70, (100, 180, 3), dtype=np.uint8)
        sprite = rng.integers(130, 250, (10, 8, 3), dtype=np.uint8)
        # Deliberately off-centre anchor: transformed along with the fixed patch.
        anchor = (2, 5)
        s, frames, truth = scenario(sid), [], []
        position = simulate_profile(dict(LINEAR_MODEL, max_speed_body_s=3.), s)
        active = set()
        for i, value in enumerate(position):
            for event in s["events"]:
                if event["tick"] == i:
                    (active.add if event["pressed"] else active.discard)(event["action"])
            mirrored = sid == "l30" and i >= 30 or sid == "reverse24" and i >= 54
            image = background.copy()
            px = 65+int(round(value*10))
            ax = 7-anchor[0] if mirrored else anchor[0]
            left = px-ax
            if i not in occluded:
                image[65:75, left:left+8] = sprite[:, ::-1] if mirrored else sprite
            if camera and i >= 50:
                image = np.roll(image, 3, axis=1)
            cv2.imwrite(str(run / "frames" / f"{i:06d}.png"), image)
            frames.append({"frame_index": i, "time_s": i/60, "physics_tick": 60+i, "actions": sorted(active)})
            truth.append((px, 70))
        manifest = {"schema_version": 2, "status": "ok", "game": "tiny", "variant": "reference",
                    "fps": 60, "frame_count": 240, "warmup_ticks": 60, "viewport": [180,100],
                    "scenario": s, "frames": frames}
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        ann = {"schema_version": 1, "template_run": str(run), "template_frame": 0,
               "roi": [63.,65.,8.,10.], "anchor_offset": list(anchor), "body_height_px": 10.,
               "background_rois": [[5.,5.,15.,15.]], "horizontal_mirror": True,
               "control_points": [], "control_events": []}
        path = self.root / "annotation.json"
        path.write_text(json.dumps(ann), encoding="utf-8")
        return run,path,ann,truth

    def test_pixels_mirror_without_anchor_jump_or_manual_claim(self):
        run,path,_,truth = self.fixture()
        result = analyze_run(run,path)
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertEqual(result["annotation_validation"]["status"], "pending")
        self.assertEqual(result["valid_ratio"], 1.)
        self.assertTrue(result["frames"][54]["mirrored"])
        self.assertEqual([(f["x"],f["y"]) for f in result["frames"]], truth)
        self.assertEqual(result["frames"][30]["physics_tick"], 90)
        self.assertEqual(result["events"]["reversal_ticks"], [54])
        self.assertAlmostEqual(result["metrics"]["end_position_body"], (truth[108][0]-65)/10)
        self.assertEqual(json.loads((run/"analysis.json").read_text())["frames"][29]["x_body"], 0.)

    def test_mirror_bank_retains_subpixel_anchor_and_fixed_template(self):
        rng = np.random.default_rng(1)
        patch = rng.integers(0,255,(9,8,3), dtype=np.uint8)
        original = patch.copy()
        bank = mirror_bank(patch, (1.5,4))
        self.assertEqual(bank[1][1], (5.5,4))
        image = np.zeros((40,40,3), np.uint8)
        image[15:24,10:18] = patch[:,::-1]
        found,score,_,mirrored = _match_bank(image,bank,(15.5,19),8)
        self.assertTrue(mirrored)
        self.assertEqual(found,(15.5,19))
        self.assertGreater(score,.99)
        np.testing.assert_array_equal(original,patch)

    def test_camera_shift_rejects(self):
        run,path,_,_ = self.fixture(camera=True)
        result = analyze_run(run,path)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("fixed-camera condition failed", result["errors"])

    def test_phase_occlusion_rejects_even_when_total_exceeds_95_percent(self):
        run,path,_,_ = self.fixture(occluded=(38,39))
        result = analyze_run(run,path)
        self.assertGreaterEqual(result["valid_ratio"],.95)
        self.assertTrue(any("held phase" in e for e in result["errors"]))
        self.assertIsNone(result["frames"][38]["x_body"])

    def test_bad_timing_input_and_native_bounds_reject(self):
        run,path,_,_ = self.fixture()
        manifest = json.loads((run/"manifest.json").read_text())
        for key,value in (("time_s",float("nan")),("frame_index",31),("physics_tick",30),("actions",[])):
            with self.subTest(key=key):
                changed = copy.deepcopy(manifest)
                changed["frames"][30][key] = value
                (run/"manifest.json").write_text(json.dumps(changed))
                with self.assertRaises(ValueError): analyze_run(run,path)
        (run/"manifest.json").write_text(json.dumps(manifest))
        cv2.imwrite(str(run/"frames/000239.png"), np.zeros((99,180,3),np.uint8))
        with self.assertRaisesRegex(ValueError,"viewport"): analyze_run(run,path)

    def test_controls_require_exact_png_and_run_identity(self):
        run,path,ann,truth = self.fixture()
        points = []
        for i in (53,54,78):
            points.append({"run_id": str(run), "frame_index": i, "x": truth[i][0], "y": truth[i][1],
                           "source": "assistant_visual", "frame_sha256": hashlib.sha256((run/"frames"/f"{i:06d}.png").read_bytes()).hexdigest()})
        ann["control_points"] = points
        path.write_text(json.dumps(ann))
        result = analyze_run(run,path)
        self.assertEqual(result["annotation_validation"]["status"], "pass")
        self.assertIn("flip", result["annotation_validation"]["points"][0]["categories"])
        self.assertIn("reverse",result["annotation_validation"]["points"][1]["categories"])
        self.assertIn("brake", result["annotation_validation"]["points"][2]["categories"])
        ann["control_points"][0]["frame_sha256"] = "0"*64
        path.write_text(json.dumps(ann))
        self.assertEqual(analyze_run(run,path)["annotation_validation"]["status"], "fail")
        for p in ann["control_points"]: p["run_id"] = "different-run"
        path.write_text(json.dumps(ann))
        self.assertEqual(analyze_run(run,path)["annotation_validation"]["status"], "pending")

    def test_annotation_rejects_nonfinite_unbounded_and_jump_controls(self):
        _,_,ann,_ = self.fixture()
        for field,value in (("body_height_px",float("nan")),("roi",[175,60,8,10]),
                            ("horizontal_mirror","yes"),("control_events",[{"takeoff_frame":3}])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_annotation(dict(ann,**{field:value}),[180,100])


class GroundMeasurementTests(unittest.TestCase):
    def test_input_aligned_origin_and_reverse_speed_uses_second_leg(self):
        value = report("reverse24")
        frames = value["frames"]
        self.assertEqual(frames[29]["x_body"],0.)
        self.assertGreater(frames[30]["x_body"],0.)
        self.assertEqual(value["metrics"]["speed_window"],list(range(70,78)))
        self.assertAlmostEqual(value["metrics"]["speed_body_s"],-4.)
        self.assertAlmostEqual(value["metrics"]["stop_distance_body"],frames[108]["x_body"]-frames[77]["x_body"])
        self.assertFalse(report("r06")["metrics"]["speed_eligible"])

    def test_missing_origin_and_endpoint_are_not_interpolated(self):
        for i in (25,108):
            value = report("reverse24")
            value["frames"][i]["valid"] = False
            _,_,_,errors = measure_ground(value["frames"],value["scenario"],20)
            self.assertTrue(errors)

    def test_quantized_last_eight_ols_keeps_real_rounding_error(self):
        exact,quantized = report("l30"),report("l30",height=7,quantize=True)
        # Small sprites can exceed a 10% speed error; never smooth it into a pass.
        self.assertAlmostEqual(quantized["metrics"]["speed_body_s"],-25/7)
        self.assertGreater(abs(quantized["metrics"]["speed_body_s"]/exact["metrics"]["speed_body_s"]-1),.1)

    def test_stop_reversal_windows_are_observed_diagnostics_only(self):
        value=report("reverse24",quantize=True)
        diagnostic=value["metrics"]["diagnostics"]
        self.assertTrue(diagnostic["diagnostic_only"])
        self.assertGreaterEqual(diagnostic["stop_pixel_rest_window"][0],78)
        self.assertGreater(diagnostic["reversal_motion_window"][0],54)
        self.assertLess(diagnostic["reversal_motion_window"][-1],78)

    def test_scenario_rejects_reorder_wrong_split_and_shifted_inputs(self):
        for change in ({"split":"calibration"},{"repeat":True},{"spawn_offset":1},{"warmup_ticks":0}):
            with self.assertRaises(ValueError): scenario_phases(dict(scenario("reverse24"),**change))
        s = scenario("reverse24")
        s["events"][1:3] = s["events"][1:3][::-1]
        with self.assertRaises(ValueError): scenario_phases(s)


class GroundComparisonTests(unittest.TestCase):
    def test_complete_numerical_matrix_remains_pending_manual(self):
        groups = matrix()
        result = compare_runs(*groups)
        self.assertEqual(result["numerical_status"],"pass")
        self.assertEqual(result["status"],"pending")
        self.assertFalse(result["success"])
        self.assertEqual(result["coverage"]["complete_triplets"],18)
        add_manual(groups)
        self.assertEqual(compare_runs(*groups)["status"],"pass")

    def test_manual_uses_median_not_worst_point_and_requires_binding(self):
        groups = matrix()
        add_manual(groups,outlier=True)
        self.assertEqual(compare_runs(*groups)["status"],"pass")
        groups[0][0]["annotation_validation"]["points"][0]["hash_bound"] = False
        self.assertEqual(compare_runs(*groups)["manual_validation_status"],"fail")

    def test_role_median_is_not_replaced_by_small_run_median_veto(self):
        groups=matrix()
        add_manual(groups,outlier=True)
        for group in groups:
            points=group[0]["annotation_validation"]["points"]
            group[1]["annotation_validation"]={"status":"pass","points":points[1:]}
            group[0]["annotation_validation"]={"status":"fail","points":points[:1]}
            group[0].update(status="rejected",errors=["independent manual annotation validation failed"])
        self.assertEqual(compare_runs(*groups)["status"],"pass")

    def test_manual_repeated_png_observation_cannot_inflate_quota(self):
        groups = matrix()
        add_manual(groups)
        for group in groups:
            points = group[0]["annotation_validation"]["points"]
            group[0]["annotation_validation"]["points"] = [copy.deepcopy(points[0]) for _ in range(25)]
            for i,p in enumerate(group[0]["annotation_validation"]["points"]): p["x"] = i
        result = compare_runs(*groups)
        self.assertEqual(result["status"],"pending")
        self.assertEqual(result["manual_validation"]["reference"]["checked_point_count"],1)

    def test_manual_generated_and_missing_category_never_pass(self):
        groups = matrix()
        add_manual(groups)
        for p in groups[0][0]["annotation_validation"]["points"]: p["source"]="tracker"
        self.assertEqual(compare_runs(*groups)["manual_validation_status"],"pending")
        add_manual(groups)
        groups[0][0]["annotation_validation"]["points"][0]["categories"]=["flip","brake"]
        self.assertIn("reverse",compare_runs(*groups)["manual_validation"]["reference"]["missing_categories"])

    def test_missing_duplicate_scale_and_wrong_variant_matrix(self):
        groups = matrix()
        groups[2].pop()
        self.assertEqual(compare_runs(*groups)["numerical_status"],"pending")
        for mutation in (lambda g:g[2].append(copy.deepcopy(g[2][0])),
                         lambda g:g[2][0].update(body_height_px=21),
                         lambda g:g[2][0].update(variant="before")):
            groups = matrix()
            mutation(groups)
            self.assertEqual(compare_runs(*groups)["numerical_status"],"fail")

    def test_equal_phase_weight_does_not_favor_long_coast(self):
        reference,target = report("r06"),report("r06","after")
        for f in target["frames"][30:36]: f["x_body"] += 1
        self.assertAlmostEqual(curve_rmse(reference,target),np.sqrt(.5))

    def test_nonfinite_or_incomplete_frame_arrays_are_rejected(self):
        for key,value in (("x_body",float("nan")),("time_s",float("inf")),("frame_index",45)):
            groups=matrix()
            groups[2][0]["frames"][31][key]=value
            self.assertEqual(compare_runs(*groups)["numerical_status"],"fail")
        groups=matrix()
        for i in (31,32): groups[2][0]["frames"][i]["valid"]=False
        self.assertEqual(compare_runs(*groups)["numerical_status"],"fail")

    def test_every_slot_must_improve_and_pass_speed_and_coast(self):
        for field,value in (("speed_body_s",7.),("stop_distance_body",3.),("end_position_body",10.)):
            groups=matrix()
            groups[2][0]["metrics"][field]=value
            result=compare_runs(*groups)
            self.assertEqual(result["numerical_status"],"fail")
        result=compare_runs(*matrix(after_model=dict(LINEAR_MODEL,max_speed_body_s=5.8)))
        self.assertEqual(result["numerical_status"],"fail")

    def test_calibration_eligibility_is_diagnostic_and_never_uses_heldout(self):
        result=calibration_feedback(*matrix("calibration"))
        self.assertEqual(result["status"],"ok")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["manual_status"],"pending")
        groups=matrix("calibration")
        groups[0].append(report("r18"))
        with self.assertRaisesRegex(ValueError,"held-out"): calibration_feedback(*groups)


class GroundFittingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"profile.json"

    def tearDown(self): self.temp.cleanup()

    def test_known_linear_family_and_deterministic_replay(self):
        inputs=[report(sid) for sid in ("r06","r48")]
        profile=fit_profile(inputs,self.path)
        self.assertEqual(profile["family"],LINEAR)
        self.assertEqual(profile["status"],"ok")
        self.assertLess(profile["fit_diagnostics"]["rmse_body"],.005)
        self.assertAlmostEqual(profile["max_speed_body_s"],4.,delta=.015)
        np.testing.assert_allclose(simulate_profile(profile,scenario("reverse24")),
                                   simulate_profile(LINEAR_MODEL,scenario("reverse24")),atol=.02)
        self.assertEqual(profile,fit_profile(inputs,self.path))

    def test_known_retention_family_quantized_at_native_scale(self):
        inputs=[report(sid,model=RETENTION_MODEL,height=7,quantize=True) for sid in ("r06","r48")]
        profile=fit_profile(inputs,self.path)
        self.assertEqual(profile["family"],RETENTION)
        self.assertLess(profile["fit_diagnostics"]["rmse_body"],.05)
        self.assertAlmostEqual(profile["retention_per_tick"],.84,delta=.03)
        self.assertEqual(profile["validation_status"],"pending")

    def test_family_mismatch_is_exposed(self):
        inputs=[report(sid) for sid in ("r06","r48")]
        for r in inputs:
            for i in range(30,109): r["frames"][i]["x_body"] = float(r["frames"][i]["x_body"]+2*np.sin(i))
        profile=fit_profile(inputs,self.path)
        self.assertEqual(profile["status"],"mismatch")
        self.assertTrue(profile["fit_diagnostics"]["model_mismatch"])

    def test_fitting_rejects_heldout_duplicate_scale_nonfinite_and_missing_phase(self):
        inputs=[report(sid) for sid in ("r06","r48")]
        cases=[inputs+[report("r18")],inputs+[copy.deepcopy(inputs[0])],
               [inputs[0],dict(inputs[1],body_height_px=7)], [inputs[0]]]
        for values in cases:
            with self.assertRaises(ValueError): fit_profile(values,self.path)
        for key,value in (("x_body",float("nan")),("time_s",float("nan"))):
            changed=copy.deepcopy(inputs)
            changed[0]["frames"][31][key]=value
            with self.assertRaises(ValueError): fit_profile(changed,self.path)

    def test_source_provenance_hashes_exact_analysis_files(self):
        paths=[]
        for sid in ("r06","r48"):
            path=self.path.parent/f"{sid}.json"
            path.write_text(json.dumps(report(sid)),encoding="utf-8")
            paths.append(path)
        profile=fit_profile(paths,self.path)
        for item,path in zip(profile["provenance"]["analysis_files"],paths):
            self.assertEqual(item["sha256"],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertIn("calibration",profile["provenance"]["source"])

    def test_model_ambiguity_and_unidentified_subtick_acceleration_are_explicit(self):
        instantaneous=dict(LINEAR_MODEL,accel_body_s2=240.,brake_body_s2=240.)
        profile=fit_profile([report(sid,model=instantaneous) for sid in ("r06","r48")],self.path)
        self.assertTrue(profile["fit_diagnostics"]["ambiguous"])
        self.assertTrue(profile["fit_diagnostics"]["subtick_acceleration_unidentified"])


if __name__ == "__main__":
    unittest.main()
