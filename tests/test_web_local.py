"""Local adapter contracts, fail-closed clocks and separate private oracle."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from kiroclops.web_local import specs
from kiroclops.web_local.capture import CaptureError, capture_run, project_hashes, timing_summary, merge_frame_receipts, local_server, exclude_redundant_checkpoints
from kiroclops.web_local.oracle import validate_oracle
from kiroclops.web_local.selection import select_from_pixels


class ScenarioTests(unittest.TestCase):
    def test_matrix_counts(self):
        for domain in ('panel','svg'):
            self.assertEqual(len(specs.scenarios(domain,'calibration')),4)
            values=specs.scenarios(domain,'validation')
            self.assertEqual(len(values),12)
            self.assertEqual(len({v['id'] for v in values}),12)
            self.assertEqual({tuple(v['viewport']) for v in values},{(768,640),(1280,720)})
            for value in values:specs.validate_scenario(value)

    def test_svg_diverse_counts(self):
        values=specs.scenarios('svg','validation')
        self.assertEqual({(s['initial_node_count'],s['expected_final_node_count']) for s in values},{(3,5),(5,6),(6,4)})
        self.assertTrue(all(s['expected_selected_id']=='b' for s in values))

    def test_drag_recipe_has_intermediate_events_and_hold(self):
        # v3 is a new challenge, not a replay of v2's 15 moves at 60Hz.
        # Both reference and target arms consume the same versioned scenario.
        self.assertEqual(specs.VERSION,'local-web-svg-v3')
        self.assertEqual(specs.TIMING_LIMITS,{'median_interval_s':.050,
            'p95_interval_s':.080,'maximum_motion_gap_s':.120,'clock_uncertainty_s':.020})
        for domain,start in (('panel',.9),('svg',.65)):
            for value in specs.scenarios(domain,'validation'):
                if not any(e['action']=='drag_move' for e in value['events']):continue
                self.assertEqual(value['version'],'local-web-svg-v3')
                events=value['events'];moves=[e for e in events if e['action']=='drag_move']
                self.assertEqual(len(moves),8)
                self.assertEqual([e['fraction'] for e in moves],[i/8 for i in range(1,9)])
                for i,move in enumerate(moves,1):
                    self.assertAlmostEqual(move['time_s'],start+.25*i/8)
                self.assertAlmostEqual(moves[-1]['time_s'],start+.25)
                self.assertAlmostEqual(events[-1]['time_s']-moves[-1]['time_s'],.05)
                self.assertAlmostEqual(events[-1]['time_s'],start+.3)

    def test_mutating_one_recipe_does_not_modify_next(self):
        values=specs.scenarios('panel','calibration');values[0]['events'][0]['time_s']=9
        self.assertEqual(values[1]['events'][0]['time_s'],.4)

    def test_bad_order_and_unreleased_pointer_rejected(self):
        value=specs.scenarios('panel','validation')[4]
        for change in ('order','release'):
            candidate=copy.deepcopy(value)
            if change=='order':candidate['events'][1]['time_s']=5
            else:candidate['events'].pop()
            with self.assertRaises(ValueError):specs.validate_scenario(candidate)

    def test_fixture_sources_separate_real_motion_entrypoint(self):
        for domain in ('panel','svg'):
            reference=specs.fixture_project(domain,'reference');target=specs.fixture_project(domain,'target')
            self.assertNotEqual(reference,target)
            name=specs.editable_files(domain)[0]
            self.assertNotEqual((reference/name).read_text(),(target/name).read_text())
            app=(target/'src/app.js').read_text()
            self.assertIn(Path(name).name,app)
            self.assertNotIn('scenario',app)
            self.assertNotIn('__kcPrivate',app)
            self.assertIn('requestAnimationFrame',app)

    def test_panel_children_are_real_rendered_groups_owned_by_editable_controller(self):
        for variant in ('reference','target'):
            root=specs.fixture_project('panel',variant)
            html=(root/'index.html').read_text();app=(root/'src/app.js').read_text()
            motion=(root/'src/panel-motion.js').read_text()
            self.assertIn('class="panel-title"',html);self.assertIn('class="panel-content"',html)
            self.assertIn('title.style.transform',app);self.assertIn('content.style.transform',app)
            self.assertIn('titleY',motion);self.assertIn('contentY',motion)
            self.assertIn('{open,dragging:!!drag}',app)


class TimingTests(unittest.TestCase):
    def good(self):
        return [i/50 for i in range(160)],[{'uncertainty_s':.001,'epoch_in_host_bracket':True}],[{'time_s':.4,'action':'click'}]

    def test_variable_fps_passes_without_60_assumption(self):
        timestamps,clocks,events=self.good()
        result=timing_summary(timestamps,clocks,events)
        self.assertEqual(result['status'],'pass')
        self.assertFalse(result['nominal_fps_assumed'])

    def test_duplicate_or_reverse_timestamp_fails(self):
        for delta in (0,-.01):
            timestamps,clocks,events=self.good();timestamps[8]=timestamps[7]+delta
            self.assertIn('non_monotonic_frame_timestamp',timing_summary(timestamps,clocks,events)['errors'])

    def test_gap_straddling_input_is_checked(self):
        timestamps,clocks,events=self.good();timestamps=[t for t in timestamps if not .38<t<.6]
        self.assertIn('motion_frame_gap_exceeded',timing_summary(timestamps,clocks,events)['errors'])

    def test_bad_clock_mapping_and_checkpoint_fail(self):
        timestamps,clocks,events=self.good();clocks[0]['epoch_in_host_bracket']=False
        result=timing_summary(timestamps,clocks,events,frame_metadata=[{'uncertainty_s':.021}])
        self.assertIn('clock_epoch_mismatch',result['errors'])
        self.assertIn('clock_mapping_uncertainty_exceeded',result['errors'])

    def test_empty_clock_is_failure(self):
        timestamps,_,events=self.good()
        self.assertEqual(timing_summary(timestamps,[],events)['status'],'fail')

    def test_clock_merge_does_not_repair_reversed_source_order(self):
        frames=[{'timestamp_source':'cdp_compositor','epoch_s':t,'interval_epoch_s':[t,t]} for t in (2.,1.)]
        with self.assertRaises(CaptureError):merge_frame_receipts(frames)

    def test_clock_merge_rejects_overlapping_uncertainty(self):
        frames=[{'timestamp_source':'cdp_compositor','epoch_s':1.,'interval_epoch_s':[1.,1.]},
                {'timestamp_source':'cdp_screenshot_bracket','epoch_s':1.01,'interval_epoch_s':[.99,1.03]}]
        with self.assertRaises(CaptureError):merge_frame_receipts(frames)

    def test_clock_merge_preserves_real_disjoint_intervals(self):
        frames=[{'timestamp_source':'cdp_compositor','epoch_s':1.,'interval_epoch_s':[1.,1.]},
                {'timestamp_source':'cdp_screenshot_bracket','epoch_s':1.04,'interval_epoch_s':[1.03,1.05]}]
        self.assertEqual(merge_frame_receipts(frames),frames)

    def test_redundant_checkpoint_is_explicitly_excluded_without_changing_times(self):
        frames=[{'timestamp_source':'cdp_compositor','epoch_s':1.,'interval_epoch_s':[1.,1.]},
                {'timestamp_source':'cdp_screenshot_bracket','epoch_s':1.01,'interval_epoch_s':[.99,1.03],'data':'eA=='}]
        kept,excluded=exclude_redundant_checkpoints(frames)
        self.assertEqual(kept,frames[:1]);self.assertEqual(excluded[0]['epoch_s'],1.01)
        self.assertEqual(excluded[0]['overlapping_compositor_times'],[1.])
        self.assertEqual(len(excluded[0]['png_sha256']),64)


class PrivateOracleTests(unittest.TestCase):
    @staticmethod
    def record():
        objects={n:{'x':i*150,'y':50,'width':100,'height':50} for i,n in enumerate('abc')}
        edges=[{'id':'a-'+n,'source':'a','target':n,'start':{'x':50,'y':75},'end':{'x':objects[n]['x']+50,'y':75}} for n in 'bc']
        row={'objects':objects,'edges':edges,'selected':['b']}
        return {'samples':[copy.deepcopy(row) for _ in range(12)],'scenario':{'initial_node_count':3,'expected_final_node_count':3,'expected_selected_id':'b'}}

    def test_valid_private_structural_contract(self):
        result=validate_oracle(self.record(),'svg')
        self.assertEqual(result['status'],'pass');self.assertEqual(result['checks_source'],'private_dom_oracle')

    def test_disconnected_edge_rejected(self):
        record=self.record();record['samples'][5]['edges'][0]['end']['x']+=10
        self.assertIn('edge_detached',validate_oracle(record,'svg')['failures'])

    def test_wrong_final_count_rejected(self):
        record=self.record();record['scenario']['expected_final_node_count']=4
        self.assertIn('final_node_count_mismatch',validate_oracle(record,'svg')['failures'])

    def test_selection_cannot_disappear_and_return(self):
        record=self.record();record['samples'][5]['selected']=[]
        self.assertIn('selection_lost_during_structure_change',validate_oracle(record,'svg')['failures'])

    def test_orphan_or_duplicated_edge_rejected(self):
        record=self.record();record['samples'][3]['edges'].append(copy.deepcopy(record['samples'][3]['edges'][0]))
        self.assertIn('edge_identity_or_count',validate_oracle(record,'svg')['failures'])


class CaptureBoundaryTests(unittest.TestCase):
    def test_idle_checkpoint_cannot_race_inflight_or_just_completed_trusted_input(self):
        from kiroclops.web_local.capture import idle_checkpoint_due
        # Actual preserved failure: planned input already elapsed, command was
        # still in flight, so an upcoming-input-only guard allowed a screenshot.
        state={'since_frame_s':.061,'upcoming_input_s':None,'input_in_flight':True,'since_input_s':None}
        self.assertFalse(idle_checkpoint_due(**state))
        state.update(input_in_flight=False,since_input_s=.020)
        self.assertFalse(idle_checkpoint_due(**state))
        state.update(since_input_s=.051,since_frame_s=.006)
        self.assertFalse(idle_checkpoint_due(**state))
        state.update(since_frame_s=.066)
        self.assertTrue(idle_checkpoint_due(**state))
        state.update(upcoming_input_s=.020)
        self.assertFalse(idle_checkpoint_due(**state))

    def test_panel_geometry_tracks_real_container_not_handle_glyph(self):
        import cv2
        import numpy as np
        from kiroclops.web_local.selection import _panel_geometry
        from kiroclops.visual.tracking import _reference_geometry_options,_contours
        source=Path(__file__).resolve().parents[1]/'artifacts/phase3a/web-corpus-03/references/panel/open-w960-r1/package/frames'
        if not source.exists():self.skipTest('Real panel reference images unavailable')
        initial=cv2.imdecode(np.fromfile(str(source/'000000.png'),dtype=np.uint8),cv2.IMREAD_COLOR)
        first=next(o for o in select_from_pixels(source/'000000.png','panel')['objects'] if o['id']=='handle')
        geometry=_panel_geometry(initial,first['roi']);roi=geometry['roi']
        options=_reference_geometry_options(initial,roi,geometry)
        self.assertGreater(roi[2],10*first['roi'][2]);self.assertGreater(roi[3],8*first['roi'][3])
        for number in (0,30,50,70):
            path=source/f'{number:06d}.png';image=cv2.imdecode(np.fromfile(str(path),dtype=np.uint8),cv2.IMREAD_COLOR)
            current=next(o for o in select_from_pixels(path,'panel')['objects'] if o['id']=='handle')
            origin=[roi[i]+current['roi'][i]-first['roi'][i] for i in (0,1)]
            boxes=_contours(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),origin,roi[2:],20,options,image)
            self.assertTrue(boxes)
            left,top,width,height=boxes[0][:4]
            self.assertLessEqual(abs(width-260),1);self.assertLessEqual(abs(height-270),1)
            self.assertLessEqual(abs(top-327),1)
            self.assertAlmostEqual(left+1,origin[0],delta=1)

    def test_multifill_outline_does_not_extend_into_shadow_or_other_background(self):
        import cv2
        import numpy as np
        from kiroclops.visual.tracking import _reference_geometry_options,_contours
        image=np.full((200,300,3),(15,10,8),dtype=np.uint8)
        image[90:]=(55,35,22)
        cv2.rectangle(image,(38,28),(202,172),(7,5,3),-1)
        cv2.rectangle(image,(39,29),(200,170),(156,132,104),-1)
        image[30:70,40:200]=(99,67,52);image[70:170,40:200]=(75,52,36)
        roi=(40,30,160,140)
        options=_reference_geometry_options(image,roi,{'segmentation':'reference_fill','fill_palette_count':2,
            'outline_mode':'reference_color','min_area_ratio':.7})
        boxes=_contours(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),roi[:2],roi[2:],20,options,image)
        self.assertEqual(boxes[0][:4],(39,29,162,142))
        # Painted sides removed while shadow remains: no imaginary padding.
        erased=image.copy();erased[29,39:201]=(7,5,3);erased[170,39:201]=(7,5,3)
        erased[29:171,39]=(7,5,3);erased[29:171,200]=(7,5,3)
        boxes=_contours(cv2.cvtColor(erased,cv2.COLOR_BGR2GRAY),roi[:2],roi[2:],20,options,erased)
        self.assertEqual(boxes[0][:4],roi)

    def test_normalized_shape_match_cannot_invent_absent_icon_of_another_color(self):
        import cv2
        import numpy as np
        from kiroclops.visual.tracking import _ncc
        patch=np.full((30,26,3),(79,57,38),dtype=np.uint8)
        patch[2:28,2:24]=(255,165,191);patch[5:11,5:11]=(79,57,38)
        impostor=patch.copy();impostor[2:28,2:24]=(196,139,230);impostor[5:11,5:11]=(79,57,38)
        image=np.full((100,150,3),(14,10,8),dtype=np.uint8);image[30:60,40:66]=impostor
        self.assertGreater(_ncc(image,patch,(40,30),5)[2],.75)
        self.assertLess(_ncc(image,patch,(40,30),5,12)[2],.45)
        image[30:60,40:66]=patch
        self.assertGreater(_ncc(image,patch,(40,30),5,12)[2],.99)

    def test_private_control_directories_are_neither_hashed_nor_served(self):
        from urllib.request import urlopen
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'index.html').write_text('ordinary app')
            for name in ('.kiroclops-scratch','.kiroclops-evidence','.kiro'):
                (root/name).mkdir();(root/name/'private.json').write_text('private')
            self.assertEqual(set(project_hashes(root)),{'index.html'})
            with local_server(root) as url:
                self.assertEqual(urlopen(url).read(),b'ordinary app')
                for name in ('.kiroclops-scratch','.kiroclops-evidence','.kiro','%2ekiro'):
                    with self.assertRaises(HTTPError) as raised:urlopen(url+name+'/private.json')
                    self.assertEqual(raised.exception.code,403)

    def test_reference_fill_preserves_boundary_when_connector_touches_outline(self):
        import cv2
        import numpy as np
        from kiroclops.visual.tracking import _reference_geometry_options, _contours
        image=np.full((160,300,3),(20,10,5),dtype=np.uint8)
        cv2.line(image,(20,80),(270,80),(130,120,100),3)
        cv2.rectangle(image,(80,45),(200,115),(77,61,35),-1)
        cv2.rectangle(image,(95,65),(115,85),(230,200,80),-1)
        roi=(80,45,121,71)
        options=_reference_geometry_options(image,roi,{'segmentation':'reference_fill','min_area_ratio':.7})
        found=_contours(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),roi[:2],roi[2:],20,options,image)
        self.assertEqual(found[0][:4],roi)
        self.assertGreater(found[0][4],.95)
        missing=image.copy();missing[45:116,80:201]=(20,10,5)
        self.assertEqual(_contours(cv2.cvtColor(missing,cv2.COLOR_BGR2GRAY),roi[:2],roi[2:],20,options,missing),[])

    def test_outline_geometry_requires_painted_side_pixels_not_fixed_padding(self):
        import cv2
        import numpy as np
        from kiroclops.visual.tracking import _visible_outline_bounds
        image=np.full((120,220,3),(24,16,10),dtype=np.uint8)
        cv2.rectangle(image,(50,30),(170,90),(130,120,100),3)
        cv2.rectangle(image,(52,32),(168,88),(79,57,38),-1)
        bounds=_visible_outline_bounds(image,(52,32,117,57),np.array((24,16,10)))
        self.assertEqual(bounds,(48,28,125,65))
        # Removing the actual stroke must remove its measured extent.
        clean=np.full_like(image,(24,16,10));clean[32:89,52:169]=image[32:89,52:169]
        self.assertEqual(_visible_outline_bounds(clean,(52,32,117,57),np.array((24,16,10))),(52,32,117,57))

    def test_real_svg_contours_and_continuous_connector_support(self):
        from kiroclops.visual.package import load_package, frame_paths, create_package
        from kiroclops.visual.tracking import analyze_package
        from kiroclops.visual.events import extract_events
        from kiroclops.web_local.selection import select_package_from_pixels
        source=Path(__file__).resolve().parents[1]/'artifacts/phase3a/web-qualification-svg-three-to-five-04/package'
        if not source.exists():self.skipTest('Locally captured real SVG regression pixels are unavailable')
        manifest=load_package(source);paths=frame_paths(source,manifest)
        # Includes actual frames which previously yielded partial Hough lines
        # and false detachments, plus both new nodes. No DOM oracle is read.
        indexes=list(range(76))+[100,180,229]
        with tempfile.TemporaryDirectory() as tmp:
            package=Path(tmp)/'pixels'
            create_package(package,[paths[i] for i in indexes],[manifest['frames'][i]['time_s'] for i in indexes])
            selection=select_package_from_pixels(package,'svg')
            observed=analyze_package(package,selection,{'require_static_camera':True})
            self.assertEqual(observed['status'],'ok',observed['errors'])
            self.assertTrue(all(o['valid_ratio']==1 for o in observed['objects']))
            events=extract_events(observed,contract={'event_options':{'connection_tolerance_px':2.}})
            detached=[r for r in events['relations'] if r.get('type')=='connection_attachment' and not r['visually_attached']]
            self.assertEqual(detached,[])

    def test_real_svg_painted_outline_prevents_false_three_pixel_detachment(self):
        from kiroclops.visual.package import load_package,frame_paths,create_package
        from kiroclops.visual.tracking import analyze_package
        from kiroclops.visual.events import extract_events
        from kiroclops.web_local.selection import select_package_from_pixels
        source=Path(__file__).resolve().parents[1]/'artifacts/phase3a/web-corpus-01/cases/svg-correct-first/capture/package'
        if not source.exists():self.skipTest('Qualified real subpixel stroke frames unavailable')
        manifest=load_package(source);paths=frame_paths(source,manifest);indices=[43,44]
        with tempfile.TemporaryDirectory() as tmp:
            package=Path(tmp)/'pixels'
            create_package(package,[paths[i] for i in indices],[manifest['frames'][i]['time_s'] for i in indices])
            observed=analyze_package(package,select_package_from_pixels(package,'svg'),{'require_static_camera':True})
            self.assertEqual(observed['status'],'ok',observed['errors'])
            relations=extract_events(observed,contract={'event_options':{'connection_tolerance_px':2.}})['relations']
            self.assertTrue(relations)
            self.assertFalse([r for r in relations if r.get('type')=='connection_attachment' and not r['visually_attached']])

    def test_search_corridor_cut_is_not_a_visible_line_endpoint(self):
        import cv2
        import numpy as np
        from kiroclops.visual.tracking import _line_connection
        frames={'a':{'valid':True,'left':20,'top':60,'width':30,'height':30},
                'b':{'valid':True,'left':240,'top':60,'width':30,'height':30}}
        spec={'from_object':'a','to_object':'b','corridor_px':8}
        gray=np.zeros((150,300),np.uint8)
        # This continuous stroke leaves the selected center corridor; its
        # retained fragment must not acquire an invented endpoint at the cut.
        cv2.line(gray,(30,20),(255,75),220,3)
        result=_line_connection(gray,spec,frames)
        self.assertFalse(result['valid'])
        self.assertEqual(result['reason'],'endpoint_clipped_by_search_region')
        self.assertIsNone(result['start'])
        # An actual gap inside the corridor still has an observable endpoint.
        gray[:]=0;cv2.line(gray,(70,75),(255,75),220,3)
        result=_line_connection(gray,spec,frames)
        self.assertTrue(result['valid'],result)
        self.assertGreater(result['start'][0],60)

    def test_real_svg_continuing_stroke_does_not_claim_eighty_pixel_gap(self):
        from kiroclops.visual.package import load_package,frame_paths,read_image
        from kiroclops.visual.tracking import _line_connection
        import cv2
        root=Path(__file__).resolve().parents[1]
        package=root/'artifacts/phase3a/web-corpus-03/cases/svg-detached-edge/capture/package'
        raw=root/'artifacts/phase3a/raw-svg-geometry-proposals-01/svg-detached-edge/proposals.json'
        if not package.exists() or not raw.exists():self.skipTest('Independently checked raw regression frame unavailable')
        # Node outlines are independent raw-color measurements, not DOM state.
        marked=json.loads(raw.read_text())['rows'][26]['objects']
        frames={oid:{'valid':True,'left':o['bounds'][0],'top':o['bounds'][1],
                    'width':o['bounds'][2]-o['bounds'][0],'height':o['bounds'][3]-o['bounds'][1]}
                for oid,o in marked.items()}
        image=read_image(frame_paths(package,load_package(package))[26])
        result=_line_connection(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),
                                {'from_object':'a','to_object':'d','corridor_px':8},frames)
        self.assertFalse(result['valid'])
        self.assertEqual(result['reason'],'endpoint_clipped_by_search_region')
        self.assertIsNone(result['end'])

    def test_existing_destination_refused_without_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CaptureError) as raised:
                capture_run(specs.fixture_project('panel'),tmp,specs.scenarios('panel','calibration')[0])
            self.assertEqual(raised.exception.code,'output_exists')

    def test_project_hash_includes_motion_source(self):
        files=project_hashes(specs.fixture_project('panel'))
        self.assertIn('src/panel-motion.js',files)
        self.assertNotIn('private/oracle.json',files)

    def test_pixel_selector_rejects_plain_or_wrong_image(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'blank.png';cv2.imencode('.png',np.zeros((200,300,3),dtype=np.uint8))[1].tofile(str(path))
            with self.assertRaises(ValueError):select_from_pixels(path,'panel')
            self.assertEqual(select_from_pixels(path,'svg',required=[])['objects'],[])

    def test_pixel_selector_does_not_need_dom(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            image=np.zeros((200,300,3),dtype=np.uint8)
            for offset,rgb in enumerate(((191,165,255),(77,208,225),(255,209,102))):
                cv2.rectangle(image,(20+60*offset,50),(40+60*offset,74),rgb[::-1],-1)
                cv2.rectangle(image,(24+60*offset,54),(27+60*offset,59),(0,0,0),-1)
            path=Path(tmp)/'art.png';cv2.imencode('.png',image)[1].tofile(str(path))
            result=select_from_pixels(path,'panel')
            self.assertEqual({o['id'] for o in result['objects']},{'handle','bookmark','star'})
            self.assertTrue(all(o['method']=='template' for o in result['objects']))

    @unittest.skipUnless(os.environ.get('KIROCLOPS_BROWSER_TESTS')=='1','Explicit headless browser integration opt-in')
    def test_headless_real_frame_clock_and_private_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=capture_run(specs.fixture_project('panel','reference'),Path(tmp)/'capture',specs.scenarios('panel','calibration')[0],variant='reference')
            self.assertEqual(result['status'],'ok',result)
            manifest=json.loads((Path(result['package_dir'])/'manifest.json').read_text())
            # Source provenance may name oracle.py; the private runtime samples
            # and DOM geometry must stay outside the pixel analysis package.
            self.assertNotIn('private_oracle',manifest)
            self.assertNotIn('oracle',manifest['metadata'])
            self.assertTrue(all('objects' not in frame for frame in manifest['frames']))
            self.assertEqual(manifest['inputs']['provenance'],'recorder_verified')
            self.assertTrue(any(f.get('timestamp_source')=='cdp_begin_frame_bracket' for f in manifest['frames']))


if __name__=='__main__':unittest.main()
