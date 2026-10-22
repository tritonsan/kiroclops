"""Local intake: real-entry discovery, honest provenance and bound annotation."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine.intake import detect_project, prepare_intake, resume_selection
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, read_json, write_json


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kiroclops intake café naïve ')
        self.root = Path(self.temporary.name)
        self.project = self.root/'project'
        (self.project/'src').mkdir(parents=True)
        (self.project/'index.html').write_text('<script type="module" src="src/app.js"></script>')
        (self.project/'src/app.js').write_text("import { advanceMotion } from './panel-motion.js'; advanceMotion();")
        (self.project/'src/panel-motion.js').write_text('export function advanceMotion() { return 1; }')
        images = self.root/'images'; images.mkdir()
        random = np.random.default_rng(18)
        body = random.integers(10,255,(12,12,3),dtype=np.uint8)
        background = random.integers(10,255,(16,16,3),dtype=np.uint8)
        paths = []
        for index in range(24):
            image = np.zeros((80,96,3),dtype=np.uint8)
            image[2:18,2:18] = background
            x = 30+min(index,12)
            image[35:47,x:x+12] = body
            path = images/f'{index:03}.png'
            path.write_bytes(cv2.imencode('.png',image)[1].tobytes())
            paths.append(path)
        self.package = self.root/'reference'
        self.manifest = create_package(self.package,paths,[i/60 for i in range(24)])
        self.output = self.root/'intake'

    def tearDown(self):
        self.temporary.cleanup()

    def assert_code(self, code, call):
        with self.assertRaises(VisualError) as caught:
            call()
        self.assertEqual(code,caught.exception.code)

    def prepare(self, **kwargs):
        return prepare_intake(kwargs.pop('video',self.package),kwargs.pop('project',self.project),
                              kwargs.pop('request','Apply this motion to the target'),kwargs.pop('output',self.output),**kwargs)

    def selection(self):
        manifest = load_package(self.output/'package')
        return {'schema_version':1,'kind':'ObjectSelection','package_sha256':manifest['package_sha256'],
                'objects':[{'id':'object-01','roi':[30,35,12,12],'anchor':[6,6], 'body_height_px':12,
                            'frame_index':0,'template_frame_sha256':manifest['frames'][0]['sha256'],'method':'template'}],
                'background_rois':[[2,2,12,12]]}

    def test_detect_actual_reachable_controller(self):
        result = detect_project(self.project)
        self.assertEqual('web_panel',result['domain'])
        self.assertEqual('src/panel-motion.js',result['entry'])

    def test_present_but_unreferenced_filename_is_not_detection(self):
        (self.project/'src/app.js').write_text("// import './panel-motion.js';\n console.log('no controller');")
        self.assert_code('unsupported_project',lambda:detect_project(self.project))

    def test_commented_html_script_is_not_entry(self):
        (self.project/'index.html').write_text('<!-- <script src="src/app.js"></script> -->')
        self.assert_code('unsupported_project',lambda:detect_project(self.project))

    def test_quoted_example_import_and_export_are_not_code_entries(self):
        (self.project/'src/app.js').write_text('const example="import \'./panel-motion.js\';";')
        self.assert_code('unsupported_project',lambda:detect_project(self.project))
        (self.project/'src/app.js').write_text("import { advanceMotion } from './panel-motion.js';")
        (self.project/'src/panel-motion.js').write_text('const example="export function advanceMotion() {}";')
        self.assert_code('unsupported_project',lambda:detect_project(self.project))

    def test_two_reachable_controllers_are_ambiguous(self):
        (self.project/'src/layout-motion.js').write_text('export function advanceLayout() {}')
        with (self.project/'src/app.js').open('a') as file:
            file.write("\nimport { advanceLayout } from './layout-motion.js';")
        self.assert_code('ambiguous_project',lambda:detect_project(self.project))

    def test_source_entry_cannot_escape_project(self):
        (self.project/'src/app.js').write_text("import '../../outside.js';")
        (self.root/'outside.js').write_text('export const bad=1')
        self.assert_code('unsafe_project_entry',lambda:detect_project(self.project))

    def test_godot_controller_must_be_reachable_from_main_scene(self):
        game = self.root/'game'; game.mkdir()
        (game/'project.godot').write_text('config_version=5\n[application]\nrun/main_scene="res://main.tscn"')
        (game/'main.tscn').write_text('[ext_resource type="Script" path="res://player.gd" id="1"]')
        (game/'player.gd').write_text('extends CharacterBody2D\n')
        self.assertEqual('game',detect_project(game)['domain'])
        (game/'main.tscn').write_text('[gd_scene format=3]')
        self.assert_code('unsupported_project',lambda:detect_project(game))

    def test_unknown_inputs_remain_observational_and_never_benchmark_pass(self):
        result = self.prepare()
        self.assertEqual('unknown',result['input_provenance'])
        self.assertEqual('observational_only',result['response_claim'])
        self.assertFalse(result['benchmark_qualified'])
        self.assertFalse(result['model_started'])
        self.assertEqual('not_started',result['transfer_status'])
        self.assertFalse(result['readiness']['autonomous_transfer']['ready'])
        self.assertIn('reference_inputs_unknown_no_interaction_rule_claim',result['readiness']['autonomous_transfer']['reasons'])
        self.assertEqual('not_accepted',read_json(result['contract_draft_path'])['acceptance_status'])
        self.assertEqual({'provenance':'unknown','events':[]},load_package(self.output/'package')['inputs'])

    def test_declared_inputs_do_not_become_verified(self):
        result = self.prepare(inputs={'events':[{'action':'click','time_s':.1}]})
        self.assertEqual('user_declared',result['input_provenance'])
        self.assertEqual('observational_only',result['response_claim'])
        self.assertEqual('unknown',load_package(self.package)['inputs']['provenance'])

    def test_separate_verified_claim_is_rejected(self):
        self.assert_code('unverified_input_claim',lambda:self.prepare(inputs={'provenance':'recorder_verified','events':[]}))
        self.assertFalse(self.output.exists())

    def test_verified_package_inputs_are_preserved_without_claiming_qualification(self):
        verified = self.root/'verified'
        manifest = create_package(verified,sorted((self.package/'frames').glob('*.png')),[i/60 for i in range(24)],
                                  {'provenance':'recorder_verified','events':[{'action':'click','time_s':.1}]})
        result = self.prepare(video=verified)
        self.assertEqual('recorder_verified',result['input_provenance'])
        self.assertEqual(manifest['package_sha256'],result['package_sha256'])
        self.assertFalse(result['benchmark_qualified'])

    def test_ambiguous_proposals_require_ui(self):
        ambiguous = {'schema_version':1,'proposals':[{'basis':'visible_outline','activity_pixels':30,'roi':[30,35,12,12]},
                    {'basis':'visible_outline','activity_pixels':20,'roi':[60,35,12,12]}]}
        with patch('kiroclops.local_engine.intake.propose_objects',return_value=ambiguous):
            result = self.prepare()
        self.assertEqual('needs_selection',result['status'])
        self.assertIsNone(result['selection_path'])
        self.assertTrue(Path(result['annotation_path']).is_file())

    def test_only_full_clip_qualified_single_outline_is_automatic(self):
        proposal = {'schema_version':1,'proposals':[{'basis':'visible_outline','activity_pixels':140,'roi':[30,35,12,12]},
                    {'basis':'motion_envelope','activity_pixels':280,'roi':[30,35,24,12]}]}
        with patch('kiroclops.local_engine.intake.propose_objects',return_value=proposal):
            result = self.prepare()
        self.assertEqual('ready_for_analysis',result['status'],result)
        self.assertEqual('qualified_single_object',result['selection_qualification']['status'])
        self.assertFalse(result['benchmark_qualified'])
        selection = read_json(result['selection_path'])
        self.assertEqual(result['package_sha256'],selection['package_sha256'])

    def test_ui_is_offline_and_request_cannot_inject_script(self):
        goal = '</script><script>alert("injected")</script>'
        result = self.prepare(request=goal)
        html = Path(result['annotation_path']).read_text(encoding='utf-8')
        self.assertNotIn(goal,html)
        self.assertEqual(1,html.count('</script>'))
        self.assertIn('download=',html)
        self.assertIn('body_height_px',html)
        self.assertIn('template_frame_sha256',html)
        self.assertNotIn('fetch(',html)
        self.assertNotIn('<script src=',html)

    def test_resume_bound_manual_selection(self):
        self.prepare()
        saved = self.root/'downloaded selection.json'
        write_json(saved,self.selection())
        result = resume_selection(self.output,saved)
        self.assertEqual('ready_for_analysis',result['status'])
        self.assertEqual('user_confirmed_not_measurement_qualified',result['selection_qualification']['status'])
        self.assertEqual('user_confirmed_local_annotation',read_json(result['selection_path'])['selection_source'])
        self.assertTrue(result['readiness']['analysis']['ready'])
        self.assertFalse(result['readiness']['autonomous_transfer']['ready'])
        self.assertEqual(['object-01'],read_json(result['contract_draft_path'])['reference_object_ids'])

    def test_recommended_actions_require_actual_entry_elements(self):
        self.assertEqual([],detect_project(self.project)['recommended_capture_actions'])
        with (self.project/'index.html').open('a') as file:
            file.write('<button id="open-panel">Open</button>')
        actions = detect_project(self.project)['recommended_capture_actions']
        self.assertEqual(['#open-panel'],[action['selector'] for action in actions])

    def test_resume_rejects_wrong_video_and_frame(self):
        self.prepare()
        selection = self.selection(); selection['package_sha256'] = 'wrong'
        self.assert_code('selection_binding_mismatch',lambda:resume_selection(self.output,selection))
        selection = self.selection(); selection['objects'][0]['template_frame_sha256'] = 'wrong'
        self.assert_code('selection_binding_mismatch',lambda:resume_selection(self.output,selection))

    def test_resume_rejects_package_image_tamper(self):
        self.prepare(); selection = self.selection()
        (self.output/'package/frames/000003.png').write_bytes(b'changed')
        self.assert_code('package_integrity_failure',lambda:resume_selection(self.output,selection))

    def test_resume_rejects_changed_target(self):
        self.prepare(); selection = self.selection()
        (self.project/'src/panel-motion.js').write_text('export function advanceMotion() {return 2}')
        self.assert_code('source_changed',lambda:resume_selection(self.output,selection))

    def test_resume_rejects_intake_domain_tamper(self):
        self.prepare(); selection = self.selection()
        state = read_json(self.output/'intake.json'); state['project']['domain'] = 'game'
        write_json(self.output/'intake.json',state)
        self.assert_code('intake_binding_changed',lambda:resume_selection(self.output,selection))

    def test_invalid_geometry_anchor_height_and_external_template_rejected(self):
        self.prepare()
        for field,value in [('roi',[90,70,20,20]),('anchor',[-1,0]),('body_height_px',float('nan')),('frame_index',1000),('template_package','elsewhere')]:
            selection = self.selection(); selection['objects'][0][field] = value
            self.assert_code('invalid_selection',lambda:resume_selection(self.output,selection))

    def test_output_never_overwrites_existing_work(self):
        self.prepare()
        self.assert_code('output_exists',self.prepare)

    def test_portable_package_and_ui_paths_survive_directory_move(self):
        self.prepare()
        selection = self.selection()
        moved = self.root/'moved intake'; self.output.rename(moved)
        result = resume_selection(moved,selection)
        self.assertEqual('ready_for_analysis',result['status'])
        self.assertEqual(self.manifest['package_sha256'],load_package(moved/'package')['package_sha256'])
        self.assertNotIn(str(self.root), (moved/'annotation.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
