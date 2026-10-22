"""Tiny fixture tests: these do not claim actual screenshot inspection."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.local_engine import realistic_review as review
from kiroclops.local_engine import realistic_policy, suite_web_v2, media_suite, suite_readiness
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from tests.evidence import requires_evidence


def bundle(role="reference", count=30, errors=None, ids=("bookmark",)):
    errors = errors or [0.] * count
    frames = [{"index": i, "time_s": i / 60, "sha256": digest([i, role])} for i in range(count)]
    raw = [{"index": i, "time_s": i / 60, "image_sha256": f["sha256"],
            "points": {oid: {"x": float(i), "y": 10., "geometry_bounds": [i-2., 8., 4., 4.]} for oid in ids}}
           for i, f in enumerate(frames)]
    objects = [{"id": oid, "frames": [{"index": i, "time_s": i/60, "x": i+errors[i], "y": 10.,
                "left": i-2., "top": 8., "width": 4., "height": 4., "valid": True} for i in range(count)]} for oid in ids]
    return {"record": {"id": role, "role": role, "split_role": "calibration", "domain": "panel",
                       "selection": {"objects": [{"id": oid} for oid in ids]}},
            "manifest": {"frames": frames}, "raw": {"rows": raw}, "observations": {"objects": objects},
            "mapping": {oid: oid for oid in ids}, "active": {oid: [[0., count/60]] for oid in ids}}


class PixelChecks(unittest.TestCase):
    def test_control_count_and_original_role(self):
        points, failures = review._controls([bundle("reference"), bundle("original"), bundle("before")])
        self.assertEqual(len(points), 60)
        self.assertFalse(failures)
        self.assertEqual({point["role"] for point in points}, {"reference", "original", "before"})

    def test_repeated_static_png_is_not_twenty_images(self):
        value = bundle()
        for row in value["raw"]["rows"]:
            row["image_sha256"] = "a" * 64
        points, failures = review._controls([value])
        self.assertEqual(len(points), 1)
        self.assertEqual(failures[0]["code"], "insufficient_distinct_control_images")

    def test_invalid_production_control_is_failure_not_replaced(self):
        value = bundle()
        value["observations"]["objects"][0]["frames"][0]["valid"] = False
        points, failures = review._controls([value])
        self.assertEqual(len(points), 19)
        self.assertEqual(failures[0]["code"], "control_point_not_valid")

    def test_coordinate_error_requires_real_pixel_accuracy(self):
        checks, errors = review._position_checks([bundle(errors=[2.] * 30)])
        self.assertFalse(checks[0]["pass"])
        self.assertEqual(errors[0]["code"], "raw_position_unqualified")

    def test_low_validity_is_not_filled(self):
        value = bundle()
        for row in value["raw"]["rows"][:3]:
            row["points"]["bookmark"] = {"status": "ambiguous"}
        checks, _ = review._position_checks([value])
        self.assertEqual(checks[0]["valid_ratio"], .9)
        self.assertFalse(checks[0]["pass"])

    def test_short_bad_motion_window_cannot_hide_in_tail(self):
        value = bundle(count=60)
        value["manifest"]["inputs"] = {"events": [{"time_s": 0.}, {"time_s": .1}]}
        value["raw"]["rows"][2]["points"]["bookmark"] = {"status": "ambiguous"}
        checks, failures = review._position_checks([value])
        self.assertTrue(checks[0]["pass"])
        self.assertFalse(checks[1]["pass"])
        self.assertTrue(any(row["code"] == "raw_motion_window_unqualified" for row in failures))

    def test_removed_object_window_is_not_observable(self):
        value = bundle(count=60)
        value["active"]["bookmark"] = [[0., .4084]]
        value["manifest"]["inputs"] = {"events": [{"time_s": .4118}]}
        for frame in value["observations"]["objects"][0]["frames"]:
            if frame["time_s"] > .4084:frame["valid"] = False
        for row in value["raw"]["rows"]:
            if row["time_s"] > .4084:row["points"]["bookmark"] = {"status": "not_detected"}
        checks, failures = review._position_checks([value])
        self.assertTrue(checks[0]["pass"])
        self.assertEqual(checks[1]["status"], "not_observable")
        self.assertFalse(checks[1]["observable"])
        self.assertIsNone(checks[1]["pass"])
        self.assertFalse(failures)

    def test_public_active_but_zero_raw_tracking_remains_failure(self):
        value = bundle(count=60)
        value["manifest"]["inputs"] = {"events": [{"time_s": .45}]}
        for row in value["raw"]["rows"]:
            if row["time_s"] >= .45:row["points"]["bookmark"] = {"status": "ambiguous"}
        checks, failures = review._position_checks([value])
        self.assertEqual(checks[1]["status"], "fail")
        self.assertTrue(checks[1]["observable"])
        self.assertEqual(checks[1]["valid_ratio"], 0.)
        self.assertTrue(any(row["code"] == "raw_motion_window_unqualified" for row in failures))

    def test_active_interval_between_samples_is_measurement_failure(self):
        value = bundle(count=60)
        value["active"]["bookmark"] = [[0., .4], [.4501, .4502]]
        value["manifest"]["inputs"] = {"events": [{"time_s": .45}]}
        checks, failures = review._position_checks([value])
        self.assertTrue(checks[0]["pass"])
        self.assertEqual(checks[1]["status"], "fail")
        self.assertEqual(checks[1]["reason"], "expected_active_interval_unsampled")
        self.assertTrue(failures)

    def test_inactive_middle_window_does_not_skip_later_lifetime(self):
        value = bundle(count=60)
        value["active"]["bookmark"] = [[0., .09], [.21, 1.]]
        value["manifest"]["inputs"] = {"events": [{"time_s": .10}, {"time_s": .20}]}
        checks, failures = review._position_checks([value])
        self.assertEqual(checks[1]["status"], "not_observable")
        self.assertTrue(checks[2]["pass"])
        self.assertFalse(failures)

    def test_geometry_cannot_reuse_small_glyph(self):
        value = bundle()
        for row in value["raw"]["rows"]:
            row["points"]["bookmark"].pop("geometry_bounds")
        checks, errors = review._geometry_checks([value])
        self.assertFalse(checks[0]["pass"])
        self.assertEqual(errors[0]["code"], "raw_body_geometry_unqualified")

    def test_relative_anchor_checked_separately(self):
        value = bundle(ids=("bookmark", "handle"))
        value["record"]["selection"]["relative_motion"] = [{"object_id": "bookmark", "relative_to": "handle"}]
        for frame in value["observations"]["objects"][0]["frames"]:
            frame["x"] += 3
        checks, failures = review._geometry_checks([value])
        self.assertFalse(checks[-1]["pass"])
        self.assertEqual(failures[-1]["code"], "raw_relative_anchor_unqualified")

    def test_calibration_cannot_relabel_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            value = {"package": directory, "source_sha256": "a"*64, "selection": {}, "role": "reference",
                     "split_role": "calibration", "scenario": {"split": "validation"}}
            with self.assertRaises(VisualError):
                review._records([value], "panel", "calibration")

    def test_calibration_selected_role_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            value = {"package": directory, "source_sha256": "a"*64, "selection": {}, "role": "selected",
                     "split_role": "calibration"}
            with self.assertRaises(VisualError):
                review._records([value], "panel", "calibration")

    def test_duplicate_physical_mapping_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            selection = {"objects": [{"id": "a"}, {"id": "b"}]}
            sources = {"app.js": "b" * 64}
            manifest = {"package_sha256": "a" * 64, "inputs": {}, "image_size": [10, 10],
                        "metadata": {"source_hashes": sources}, "frames": [{"index": 0, "time_s": 0.}]}
            observed = {"package_sha256": manifest["package_sha256"], "inputs": {}, "image_size": [10, 10], "selection": selection,
                        "objects": [{"id": oid, "frames": [{"index": 0, "time_s": 0.}]} for oid in ("a", "b")]}
            value = {"record": {"selection": selection, "source_sha256": digest(sources), "object_map": {"a": "bookmark", "b": "bookmark"},
                               "observations": observed}, "manifest": manifest}
            with self.assertRaises(VisualError):
                review._measurement(value, {})

    def test_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(VisualError) as caught:
                review.prepare_calibration_review([], directory)
            self.assertEqual(caught.exception.code, "output_exists")

    def test_stored_event_guard_rejects_all_read_methods(self):
        value = review._EventReadGuard(events={"events": []}, observations={})
        for fn in (lambda: value["events"], lambda: value.get("events"), lambda: value.setdefault("events")):
            with self.assertRaises(VisualError):fn()
        self.assertEqual(value["observations"], {})

    def test_static_six_node_qualification_keeps_actual_split(self):
        with tempfile.TemporaryDirectory() as directory:
            row={"package": directory, "source_sha256": "a"*64, "selection": {}, "role": "reference",
                "split_role": "calibration", "purpose": "measurement_qualification", "qualification_kind": "static_geometry",
                "behavior_fit_eligible": False, "scenario": {"split": "qualification", "suite_split": "qualification",
                    "recipe": "six-node-measurement", "initial_node_count": 6, "expected_final_node_count": 6}}
            self.assertEqual(review._records([row], "svg", "calibration")[0]["scenario"]["split"], "qualification")
            row["scenario"]["expected_final_node_count"]=5
            with self.assertRaises(VisualError):review._records([row], "svg", "calibration")


class AttestationChecks(unittest.TestCase):
    def fixture(self, directory, numeric="pass", phase="calibration"):
        directory = Path(directory)
        (directory/"image.png").write_bytes(b"tiny fixture, not a real viewed image")
        sheet = {"path": "image.png", "sha256": file_sha256(directory/"image.png")}
        result = {"kind": "RealisticIndependentPixelProposal", "schema_version": 2, "version": review.VERSION,
                  "phase": phase, "numeric_status": numeric, "sheets": [sheet], "points": [], "status": "pending_visual_review"}
        write_json(directory/"result.json", result)
        receipt = {"result_sha256": digest(result), "source_files": {}, "code_sources": {},
                   "generated_files": {"image.png": sheet["sha256"], "result.json": file_sha256(directory/"result.json")},
                   "runtime": {"opencv": review.cv2.__version__, "numpy": review.np.__version__,
                               "opencv_build_sha256": digest(review.cv2.getBuildInformation())}}
        write_json(directory/"evidence-receipt.json", receipt)
        return result

    def test_unsigned_review_does_not_supply_floors(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(review, "_code_sources", return_value={}):
            self.fixture(directory)
            with self.assertRaises((VisualError, FileNotFoundError)):
                review.calibration_review(directory)

    def test_partial_sheet_list_cannot_attest(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(review, "_code_sources", return_value={}):
            self.fixture(directory)
            with self.assertRaises(VisualError):
                review.attest_review(directory, reviewer="unit fixture", inspected_sheets=[], accepted=True, images_inspected=True)

    def test_unviewed_attestation_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(review, "_code_sources", return_value={}):
            value=self.fixture(directory)
            with self.assertRaises(VisualError):
                review.attest_review(directory, reviewer="unit fixture", inspected_sheets=value["sheets"], accepted=True, images_inspected=False)

    def test_numeric_failure_can_only_be_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(review, "_code_sources", return_value={}):
            value=self.fixture(directory, numeric="fail")
            with self.assertRaises(VisualError):
                review.attest_review(directory, reviewer="unit fixture", inspected_sheets=value["sheets"], accepted=True, images_inspected=True)
            receipt=review.attest_review(directory, reviewer="unit fixture", inspected_sheets=value["sheets"], accepted=False, images_inspected=True)
            self.assertEqual(receipt["status"], "fail")

    def test_modified_sheet_invalidates_proposal(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(review, "_code_sources", return_value={}):
            self.fixture(directory)
            (Path(directory)/"image.png").write_bytes(b"changed")
            with self.assertRaises(VisualError):
                review.validate_review(directory, require_attested=False)

    def test_changed_current_consumer_source_invalidates_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(review,"_code_sources",return_value={}):self.fixture(directory)
            with patch.object(review,"_code_sources",return_value={"new-consumer.py":digest("changed")}),self.assertRaises(VisualError) as caught:
                review.validate_review(directory,require_attested=False)
            self.assertEqual(caught.exception.code,"source_changed")

    def test_historical_result_kind_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(review, "_code_sources", return_value={}):
            self.fixture(directory)
            value=read_json(Path(directory)/"result.json")
            value["kind"]="FinalOnlyUnsignedPanelReviewAid"
            write_json(Path(directory)/"result.json", value)
            with self.assertRaises(VisualError):
                review.validate_review(directory, require_attested=False)

    def test_final_pixels_cannot_produce_calibration_floor(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(review, "_code_sources", return_value={}):
            value=self.fixture(directory, phase="final")
            review.attest_review(directory, reviewer="unit fixture", inspected_sheets=value["sheets"], accepted=True, images_inspected=True)
            with self.assertRaises(VisualError) as caught:
                review.calibration_review(directory)
            self.assertEqual(caught.exception.code, "calibration_unqualified")

    def test_second_attestation_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(review, "_code_sources", return_value={}):
            value=self.fixture(directory)
            review.attest_review(directory, reviewer="unit fixture", inspected_sheets=value["sheets"], accepted=True, images_inspected=True)
            with self.assertRaises(VisualError) as caught:
                review.attest_review(directory, reviewer="unit fixture", inspected_sheets=value["sheets"], accepted=False, images_inspected=True)
            self.assertEqual(caught.exception.code, "output_exists")


def math_record(scale, scenario):
    objects=[]
    for oid, offset, delay, travel in (("handle", 0., 0., 0.), ("bookmark", 30., .08, 24.), ("star", 60., .18, 40.)):
        frames=[]
        for index in range(181):
            stamp=index/60
            x=100*scale*max(0., min(1., (stamp-.4)/.6))+offset
            y=30+travel*scale*max(0., min(1., (stamp-.4-delay)/.3))
            frames.append({"index":index,"time_s":stamp,"x":x,"y":y,"left":x-5.,"top":y-5.,
                "width":10.,"height":10.,"valid":True,"present":True,"expected_active":True})
        objects.append({"id":oid,"frames":frames,"active_intervals_s":[[0.,3.]],"valid_ratio":1.})
    inputs=[{**event,"client_x":100.,"client_y":30.} for event in scenario["events"]]
    return {"scenario":deepcopy(scenario),"observations":{"schema_version":1,"kind":"ObservationSet","status":"ok",
        "measurement_status":"pass","package_sha256":"fixture","image_size":[960,640],"objects":objects,"connections":[],
        "inputs":{"provenance":"recorder_verified","events":inputs}},
        "measurement_status":"ok","functional":{"status":"pass"},"suite_event_contract":suite_web_v2.event_contract("panel"),
        "events":{"events":[],"relations":[]},"selection":{"objects":[{"id":obj["id"]} for obj in objects],"relative_motion":[
        {"object_id":"bookmark","relative_to":"handle"},{"object_id":"star","relative_to":"handle"}]}}


def svg_math_record(scale, scenario):
    objects=[]
    for position,oid in enumerate("abcdef"):
        frames=[]
        for index in range(181):
            stamp=index/60
            x=30.+position*100.+30*scale*max(0.,min(1.,(stamp-.4)/.6))
            frames.append({"index":index,"time_s":stamp,"x":x,"y":60.,"left":x-10.,"top":50.,
                "width":20.,"height":20.,"valid":True,"present":True,"expected_active":True,"geometry_method":"contour"})
        objects.append({"id":oid,"method":"contour","frames":frames,"active_intervals_s":[[0.,3.]],"valid_ratio":1.})
    selection={"objects":[{"id":obj["id"],"method":"contour"} for obj in objects],
        "connections":[{"id":a+"-"+b,"from_object":a,"to_object":b,"method":"line"} for a,b in zip("abcde","bcdef")]}
    lookup={obj["id"]:obj for obj in objects}
    connections=[]
    for edge in selection["connections"]:
        a,b=lookup[edge["from_object"]],lookup[edge["to_object"]]
        frames=[{"index":aa["index"],"time_s":aa["time_s"],"valid":True,
            "start":[aa["x"]+10.,aa["y"]],"end":[bb["x"]-10.,bb["y"]]} for aa,bb in zip(a["frames"],b["frames"])]
        connections.append({**edge,"frames":frames})
    inputs=[{**event,"client_x":30.,"client_y":60.} for event in scenario["events"]]
    return {"scenario":deepcopy(scenario),"observations":{"schema_version":1,"kind":"ObservationSet","status":"ok",
        "measurement_status":"pass","package_sha256":"fixture","image_size":[960,640],"objects":objects,
        "connections":connections,"inputs":{"provenance":"recorder_verified","events":inputs},"selection":selection},
        "selection":selection,"measurement_status":"ok","functional":{"status":"pass"},
        "suite_event_contract":suite_web_v2.event_contract("svg"),"events":{"events":[],"relations":[]}}


def svg_bundle(role="reference", *, scale=1., scenario=None):
    scenario=scenario or suite_web_v2.scenarios("svg","calibration")[0]
    record=svg_math_record(scale,scenario)
    record.update(id=role+"-"+scenario["case_id"],case_id=scenario["case_id"],role=role,domain="svg")
    rows=[]
    for index in range(181):
        rawobjects={}
        points={}
        for obj in record["observations"]["objects"]:
            f=obj["frames"][index]
            points[obj["id"]]={"x":f["x"],"y":f["y"]}
            rawobjects[obj["id"]]={"anchor":[f["x"],f["y"]],"bounds":[f["left"],f["top"],f["left"]+f["width"],f["top"]+f["height"]]}
        rows.append({"index":index,"time_s":index/60,"image_sha256":digest([role,index]),"points":points,"objects":rawobjects,
            "connections":{edge["id"]:{"start":deepcopy(edge["frames"][index]["start"]),"end":deepcopy(edge["frames"][index]["end"])}
                           for edge in record["observations"]["connections"]}})
    frames=[{"index":row["index"],"time_s":row["time_s"],"sha256":row["image_sha256"]} for row in rows]
    return {"record":record,"observations":record["observations"],"raw":{"rows":rows},
        "manifest":{"frames":frames,"inputs":deepcopy(record["observations"]["inputs"]),"image_size":[960,640],"package_sha256":"fixture"},
        "mapping":{oid:oid for oid in "abcdef"},"active":{oid:[[0.,3.]] for oid in "abcdef"}}


class SvgIndependentGeometry(unittest.TestCase):
    def test_v5_production_occlusion_flag_is_checked_against_the_raw_reader(self):
        # V5: the ring-aware production reader may flag an end as covered by an
        # occluder; that production decision must agree with the raw reader's.
        def detached():
            value=svg_bundle()
            row=value["raw"]["rows"][50];edge=row["connections"]["a-b"]
            measured=next(e for e in value["observations"]["connections"] if e["id"]=="a-b")["frames"][50]
            for point in (edge["start"],measured["start"]):
                point[:]=[s+.3*(e-s) for s,e in zip(point,edge["end"])]
            return value,edge,measured

        def outcome(value):
            checks,failures=review._svg_geometry_checks(value)
            row=next(r for r in checks if r.get("type")=="connection_attachment"
                     and r["connection_id"]=="a-b" and r["index"]==50)
            return row,[f for f in failures if f["code"]=="raw_svg_connection_attachment_unqualified"]

        value,_,_=detached()
        row,failed=outcome(value)
        self.assertEqual((False,False,[]),(row["raw_attached"],row["measured_attached"],failed))
        value,_,measured=detached();measured["start_occluded"]=True
        row,failed=outcome(value)
        self.assertEqual([True,False],row["measured_occluded_ends"])
        self.assertTrue(row["measured_attached"]);self.assertFalse(row["raw_attached"]);self.assertEqual(1,len(failed))
        value,edge,measured=detached();measured["start_occluded"]=True;edge["start_occluded"]=True
        row,failed=outcome(value)
        self.assertEqual((True,True,[]),(row["raw_attached"],row["measured_attached"],failed))

    def test_visible_outer_cards_pairs_and_all_stroke_ends(self):
        value=svg_bundle()
        checks,failures=review._svg_geometry_checks(value)
        self.assertFalse(failures)
        self.assertEqual(len([row for row in checks if row.get("type")=="connection_attachment"]),181*5)
        self.assertEqual(len([row for row in checks if row.get("type")=="object_pair_geometry"]),181*15)

    def test_calibration_floor_does_not_claim_graph_behavior(self):
        value=svg_bundle();value["raw"]["rows"][0]["connections"]={}
        checks,failures=review._geometry_checks([value])
        self.assertFalse(failures)
        self.assertTrue(all("type" not in row for row in checks))
        _,failures=review._geometry_checks([value],final=True)
        self.assertTrue(any(row["code"]=="raw_svg_connection_attachment_unqualified" for row in failures))

    def test_glyph_accuracy_cannot_hide_wrong_outer_card(self):
        value=svg_bundle()
        for row in value["raw"]["rows"]:row["objects"]["a"]["bounds"][2]+=4.
        _,failures=review._svg_geometry_checks(value)
        self.assertTrue(any(row["code"]=="raw_svg_outer_geometry_unqualified" for row in failures))

    def test_stroke_endpoint_error_is_reported_and_decision_disagreement_fails(self):
        # V3 (user decision 2026-10-09): endpoint coordinates are reported; the
        # frame gate is whether independent and produced attachment agree.
        value=svg_bundle()
        value["raw"]["rows"][50]["connections"]["a-b"]["start"][1]+=5.
        checks,failures=review._svg_geometry_checks(value)
        row=next(r for r in checks if r.get("type")=="connection_attachment"
                 and r["connection_id"]=="a-b" and r["index"]==50)
        self.assertFalse(row["coordinate_agreement_2px"])
        self.assertEqual("attachment_decision_v5",row["decision_rule"])
        self.assertEqual(row["raw_attached"],row["measured_attached"])
        self.assertTrue(row["pass"])
        self.assertFalse([f for f in failures if f["code"]=="raw_svg_connection_attachment_unqualified"])
        value=svg_bundle()
        connection=value["raw"]["rows"][50]["connections"]["a-b"]
        connection["start"]=[s+.3*(e-s) for s,e in zip(connection["start"],connection["end"])]
        _,failures=review._svg_geometry_checks(value)
        failed=[f for f in failures if f["code"]=="raw_svg_connection_attachment_unqualified"]
        self.assertEqual(1,len(failed))
        self.assertEqual(50,failed[0]["index"])
        self.assertFalse(failed[0]["raw_attached"])
        self.assertTrue(failed[0]["measured_attached"])

    def test_occluded_raw_end_is_not_a_detachment(self):
        # V3 (user decision 2026-10-09): a painted occluder covering the end is
        # unobservable, not evidence that the connection detached.
        value=svg_bundle()
        connection=value["raw"]["rows"][50]["connections"]["a-b"]
        connection["start"]=[s+.3*(e-s) for s,e in zip(connection["start"],connection["end"])]
        connection["start_occluded"]=True
        checks,failures=review._svg_geometry_checks(value)
        row=next(r for r in checks if r.get("type")=="connection_attachment"
                 and r["connection_id"]=="a-b" and r["index"]==50)
        self.assertEqual([True,False],row["raw_occluded_ends"])
        self.assertTrue(row["raw_attached"])
        self.assertTrue(row["pass"])
        self.assertFalse([f for f in failures if f["code"]=="raw_svg_connection_attachment_unqualified"])
        raw=review._svg_raw_record(value)["observations"]
        sample=next(edge for edge in raw["connections"] if edge["id"]=="a-b")["frames"][50]
        self.assertTrue(sample["start_occluded"])
        self.assertFalse(sample["end_occluded"])

    def test_original_unused_strokes_do_not_hide_card_errors(self):
        value=svg_bundle("original")
        for row in value["raw"]["rows"]:row["connections"]={}
        checks,failures=review._svg_geometry_checks(value)
        self.assertFalse(failures)
        self.assertTrue(checks[-1]["requires_complete_consumer_exclusion_proof"])
        for row in value["raw"]["rows"]:row["objects"]["a"]["bounds"][0]-=4.
        _,failures=review._svg_geometry_checks(value)
        self.assertTrue(failures)

    @requires_evidence('artifacts/phase3a/tools')
    def test_consumed_dynamics_and_final_relations_use_raw_geometry(self):
        values=[svg_bundle(role) for role in review.ROLES]
        points=[{"object_id":oid,"frame_sha256":digest([oid,i]),"error_px":1.,"role":"reference",
                 "split":"calibration","independent":True} for oid in "abcdef" for i in range(20)]
        policy=realistic_policy.build_policy("svg",{"status":"pass","basis":"independent_raw_pixels","points":points})
        context={"stage":"svg","cases":[{"case_id":values[0]["record"]["case_id"]}],"acceptance_policy":policy}
        checks,events,relevance,failures=review._final_svg_events(values,suite_web_v2.event_contract("svg"),context)
        self.assertFalse(failures)
        self.assertTrue(any(row["type"]=="consumed_coordinate_dynamics" for row in checks))
        self.assertEqual(len([row for row in checks if row["type"]=="consumed_svg_relationships"]),2)
        self.assertTrue(events)
        selected=next(value for value in values if value["record"]["role"]=="selected")
        selected["raw"]["rows"][-1]["connections"]["a-b"]["start"][0]+=5.
        _,_,_,failures=review._final_svg_events(values,suite_web_v2.event_contract("svg"),context)
        self.assertTrue(any(row["code"]=="raw_svg_consumed_relationship_disagreement" for row in failures))


class CompleteConsumerProof(unittest.TestCase):
    def fixture(self, root, stage="panel"):
        points=[{"object_id":oid,"frame_sha256":digest([oid,i]),"error_px":1.,"role":"reference",
                 "split":"calibration","independent":True} for oid in (("handle","bookmark","star") if stage=="panel" else tuple("abcdef")) for i in range(20)]
        policy=realistic_policy.build_policy(stage,{"status":"pass","basis":"independent_raw_pixels","points":points})
        scenarios=[suite_web_v2.scenarios(stage,"calibration")[0],suite_web_v2.scenarios(stage,"validation")[0]]
        cases=[{"case_id":scenario["case_id"],"role":role,"purpose":"primary"} for scenario,role in zip(scenarios,("calibration","validation"))]
        rows=[]
        for case,scenario,job in zip(cases,scenarios,("evaluation","final")):
            maker=math_record if stage=="panel" else svg_math_record
            records={role:maker(scale,scenario) for role,scale in (("reference",1.),("original",.75),("before",1.),("selected",1.))}
            result=media_suite._compare(records["reference"],records["original"],records["selected"],stage,require_improvement=True,acceptance_policy=policy)
            result.update(case_id=case["case_id"],scenario_id=case["case_id"],role=case["role"],purpose="primary")
            parent=media_suite._compare(records["reference"],records["original"],records["before"],stage,require_improvement=False,acceptance_policy=policy)
            preserved=deepcopy(result)
            preserved.update(preservation_reference_basis="primary_reference_quality",purpose="authoring_parent_preservation",
                parent_reference_acceptance=parent,previously_accepted_case=parent["pass"],retained_reference_behavior=True)
            write_json(root/"measurements"/job/"comparison.json",[result])
            write_json(root/"measurements"/job/"parent-preservation.json",[preserved])
            for name in ("job.json","binding.json","result.json"):write_json(root/"jobs"/job/name,{"fixture":True})
            rows.extend({**record,"case_id":case["case_id"],"role":role,"domain":stage} for role,record in records.items())
        write_json(root/"suite/suite.json",{"schema_version":2,"acceptance_policy":policy,"stage":stage,
            "domain":"web_panel" if stage=="panel" else "svg"})
        return {"session_directory":str(root),"suite_directory":str(root/"suite"),"acceptance_policy":policy,
                "cases":cases,"evaluation_job_id":"evaluation","final_job_id":"final","stage":stage},rows

    def test_real_math_complete_roles_and_saved_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            context,rows=self.fixture(Path(directory))
            proof=review._prove_original_events(context,rows)
            self.assertEqual(proof["status"],"pass")
            self.assertEqual(len(proof["cases"]),2)
            self.assertEqual(len(proof["interventions"]),6)
            self.assertTrue(all(set(row["provider_roles"])=={"reference","before","selected"} for row in proof["provider_traces"]))
            self.assertFalse(proof["original_stored_events_read"])

    def test_missing_saved_case_is_closed_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context,rows=self.fixture(root)
            write_json(root/"measurements/final/comparison.json",[])
            with self.assertRaises(VisualError):review._prove_original_events(context,rows)

    def test_changed_saved_decision_is_not_diagnostic_stripped(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context,rows=self.fixture(root)
            path=root/"measurements/final/comparison.json"
            result=read_json(path);result[0]["original_pixel_comparison"]["extra_decision_field"]=True;write_json(path,result)
            with self.assertRaises(VisualError) as caught:review._prove_original_events(context,rows)
            self.assertEqual(caught.exception.code,"wrong_result")

    def test_direct_original_event_read_is_defect(self):
        with tempfile.TemporaryDirectory() as directory:
            context,rows=self.fixture(Path(directory))
            actual=media_suite._compare
            def bad(reference,original,*args,**kwargs):
                original.get("events")
                return actual(reference,original,*args,**kwargs)
            with patch.object(media_suite,"_compare",bad),self.assertRaises(VisualError) as caught:
                review._prove_original_events(context,rows)
            self.assertEqual(caught.exception.code,"event_dependency_unclosed")

    def test_svg_complete_live_geometry_consumer_and_interventions(self):
        with tempfile.TemporaryDirectory() as directory:
            context,rows=self.fixture(Path(directory),stage="svg")
            proof=review._prove_original_events(context,rows)
            self.assertEqual(proof["stage"],"svg")
            self.assertEqual(len(proof["cases"]),2)
            self.assertEqual(len(proof["interventions"]),6)
            self.assertTrue(all(row["saved_output_equal"] and set(row["provider_roles"])=={"reference","before","selected"}
                                for row in proof["provider_traces"]))

    def test_svg_matrix_cannot_be_declared_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            context,rows=self.fixture(Path(directory),stage="svg")
            rows[0]["domain"]="panel"
            with self.assertRaises(VisualError):review._prove_original_events(context,rows)

    def test_changed_svg_relationship_saved_gate_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context,rows=self.fixture(root,stage="svg")
            path=root/"measurements/final/comparison.json"
            value=read_json(path);value[0]["svg_geometry_preservation"]["pass"]=False;write_json(path,value)
            with self.assertRaises(VisualError) as caught:review._prove_original_events(context,rows)
            self.assertEqual(caught.exception.code,"wrong_result")


class CanonicalSvgContext(unittest.TestCase):
    def fixture(self,root):
        hashes={"app.js":digest("verified source")}
        case={"case_id":"case","role":"calibration","purpose":"primary"}
        suite={"schema_version":2,"domain":"svg","stage":"svg","cases":[case],"acceptance_policy":{"domain":"svg"},
            "original":"original","original_source_hashes":hashes,"authoring_parent_source_hashes":hashes}
        state={"domain":"svg","reference":str(root/"suite"),"baseline":str(root/"baseline"),"sealed_at":"sealed",
            "experiment":{"stage":"svg","reference_mode":"media_suite","suite_sha256":digest(suite)},
            "final":{"status":"succeeded"},"final_job_id":"final"}
        selected={"candidate_id":"candidate-01","job_id":"evaluation","source_sha256":digest(hashes)}
        layout=[("prepare",role,[case]) for role in ("reference","original","before")]+[("evaluation","candidate",[case])]
        record={"selection":{"objects":[{"id":"a"},{"id":"b"}]},"observations":{},"source_sha256":digest(hashes)}
        return suite,state,selected,layout,record,hashes

    def test_canonical_svg_context_keeps_all_four_roles(self):
        from kiroclops.local_engine import suite_evidence
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);suite,state,selected,layout,record,hashes=self.fixture(root)
            with patch.object(review.Session,"_load",return_value=state),patch.object(media_suite,"load_suite",return_value=suite),\
                 patch.object(suite_evidence,"_matrix",return_value=(selected,layout)),patch.object(suite_evidence,"_records",return_value=[(record,suite["cases"][0])]),\
                 patch.object(review,"source_hashes",return_value=hashes):
                context,rows=review._session_context(root)
                self.assertEqual(context["stage"],"svg")
                self.assertEqual(context["domain"],"svg")
                self.assertEqual({row["role"] for row in rows},set(review.ROLES))
                self.assertTrue(all(row["domain"]=="svg" for row in rows))

    def test_changed_selected_source_cannot_be_reviewed(self):
        from kiroclops.local_engine import suite_evidence
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);suite,state,selected,layout,record,hashes=self.fixture(root)
            with patch.object(review.Session,"_load",return_value=state),patch.object(media_suite,"load_suite",return_value=suite),\
                 patch.object(suite_evidence,"_matrix",return_value=(selected,layout)),patch.object(review,"source_hashes",return_value={"app.js":digest("changed")}),\
                 self.assertRaises(VisualError) as caught:review._session_context(root)
            self.assertEqual(caught.exception.code,"source_changed")

    def test_svg_session_cannot_use_panel_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);suite,state,*_=self.fixture(root);suite["stage"]="panel"
            with patch.object(review.Session,"_load",return_value=state),patch.object(media_suite,"load_suite",return_value=suite),\
                 self.assertRaises(VisualError):review._session_context(root)

    def test_final_dispatch_uses_authoritative_svg_stage(self):
        context={"stage":"svg","event_contract":{"options":"fixture"}}
        with patch.object(review,"_session_context",return_value=(context,[])),patch.object(review,"_prepare",return_value={}) as prepared:
            review.prepare_final_review("session","output")
        self.assertEqual(prepared.call_args.kwargs["domain"],"svg")
        self.assertEqual(prepared.call_args.kwargs["context"],context)


class ProspectiveReadiness(unittest.TestCase):
    def fixture(self, root):
        observed={"objects":[{"id":"bookmark"}]}
        records={role:{"observations":deepcopy(observed),"source_sha256":digest(role),"package":"fixture"}
                 for role in ("reference","original","before")}
        case={"case_id":"case","role":"calibration","package_sha256":digest("reference-pixels")}
        suite={"schema_version":2,"stage":"panel","domain":"web_panel","cases":[case],"acceptance_policy":{"fixture":True},
               "initial_records":[{"case_id":"case","role":role} for role in ("original","before")]}
        result={"phase":"calibration","records":[{"record_id":role,"role":role,"case_id":"case", "package_sha256":digest("reference-pixels") if role=="reference" else "initial-pixels",
                  "observations_sha256":digest(observed),"source_sha256":records[role]["source_sha256"]} for role in records]}
        (root/"attestation.json").write_text("fixture",encoding="utf8")
        return suite,result,records

    def test_v2_complete_three_role_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);suite,result,records=self.fixture(root)
            with patch.object(review,"validate_review",return_value=result),patch.object(review,"calibration_review",return_value={"fixture":True}),\
                 patch.object(realistic_policy,"build_policy",return_value={"fixture":True,"floors_px":{"bookmark":2.},"policy_sha256":digest("policy")}),\
                 patch.object(suite_readiness,"record_from_suite",side_effect=lambda _,case:records["reference" if case.get("role")=="calibration" else case.get("role","reference")]),\
                 patch("kiroclops.visual.package.load_package",return_value={"package_sha256":"initial-pixels"}):
                suite["acceptance_policy"]={"fixture":True,"floors_px":{"bookmark":2.},"policy_sha256":digest("policy")}
                value=suite_readiness._raw_review(root,"suite",suite)
                self.assertEqual(value["required_record_count"],3)

    def test_wrong_floor_receipt_cannot_admit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);suite,result,_=self.fixture(root)
            with patch.object(review,"validate_review",return_value=result),patch.object(review,"calibration_review",return_value={}),\
                 patch.object(realistic_policy,"build_policy",return_value={"wrong":True}),self.assertRaises(VisualError):
                suite_readiness._raw_review(root,"suite",suite)


if __name__ == "__main__":unittest.main()
