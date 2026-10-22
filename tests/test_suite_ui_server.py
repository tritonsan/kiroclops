"""Real PNG intake/HTTP binding; no browser, model or application capture."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np

from kiroclops.local_engine import suite_ui_server as ui
from kiroclops.local_engine import intake, suite_intake
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, read_json


class SuiteUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='suite UI café naïve ')
        self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.target = self.root / 'target'; (self.target / 'src').mkdir(parents=True)
        (self.target / 'index.html').write_text('<button id="open-panel">Go</button><script src="src/app.js" type="module"></script>')
        (self.target / 'src/app.js').write_text("import { advanceMotion } from './panel-motion.js'; advanceMotion();")
        (self.target / 'src/panel-motion.js').write_text('export function advanceMotion() { return 1; }')
        self.goal = 'Transfer the panel opening and interruption movement.'
        self.counter = 0
        self.auto = patch('kiroclops.local_engine.intake._qualify_automatic', return_value=(None, {'status': 'needs_confirmation'}))
        self.auto.start(); self.addCleanup(self.auto.stop)
        self.first_package = self.package(1)
        self.intake = self.root / 'first-intake'
        intake.prepare_intake(self.first_package, self.target, self.goal, self.intake)
        self.draft = self.root / 'draft'

    def package(self, seed):
        self.counter += 1; source = self.root / f'images-{self.counter}'; source.mkdir()
        body = np.random.default_rng(seed).integers(40, 230, (12, 12, 3), dtype=np.uint8)
        paths = []
        for index in range(5):
            image = np.zeros((64, 64, 3), dtype=np.uint8); image[20:32, 10 + index:22 + index] = body
            path = source / f'{index}.png'; path.write_bytes(cv2.imencode('.png', image)[1].tobytes()); paths.append(path)
        package = self.root / f'package-{self.counter}'
        create_package(package, paths, [i * .1 for i in range(5)])
        return package

    def selection(self, workflow):
        manifest = workflow.package(workflow.current)
        return {'schema_version': 1, 'kind': 'ObjectSelection', 'package_sha256': manifest['package_sha256'],
            'objects': [{'id': 'object-01', 'roi': [10, 20, 12, 12], 'anchor': [6, 6], 'body_height_px': 12,
                'frame_index': 0, 'template_frame_sha256': manifest['frames'][0]['sha256'], 'method': 'template'}],
            'background_rois': []}

    def form(self, workflow, scenario='open-01', behaviors=None, inputs=True):
        return {'selection': self.selection(workflow), 'object_map': {'object-01': 'handle'},
            'scenario_id': scenario, 'behaviors': behaviors or ['open'], 'required_behaviors': ['open', 'interrupt'],
            'inputs': [{'time_s': .1, 'action': 'click', 'selector': '#open-panel'}] if inputs else None}

    def start_server(self):
        server = ui.make_server(self.intake, self.draft)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close(): server.shutdown(); server.server_close(); thread.join(2)
        self.addCleanup(close)
        return server

    def get(self, server, path, headers=None):
        url = server.url.rsplit('/', 1)[0] + '/' + path
        return urlopen(Request(url, headers=headers or {}), timeout=10)

    def post(self, server, path, value, headers=None):
        origin = f'http://127.0.0.1:{server.server_port}'
        return self.get(server, path, {}) if value is None else urlopen(Request(
            server.url.rsplit('/', 1)[0] + '/' + path,
            data=json.dumps(value).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Origin': origin, **(headers or {})}, method='POST'), timeout=10)

    def test_metadata_and_extra_clip_refresh_keep_original_intake_and_target(self):
        workflow = ui.SuiteIntakeWorkflow(self.intake, self.draft)
        original, target = source_hashes(self.intake), source_hashes(self.target)
        meta = workflow.metadata()
        self.assertEqual(meta['domain'], 'web_panel'); self.assertEqual(meta['required_behaviors'], ['open', 'close', 'interrupt', 'reopen'])
        first = workflow.current
        value = workflow.new_clip({'video': str(self.package(2)), 'target': str(self.target), 'request': self.goal})
        self.assertNotEqual(first, value['clip_id'])
        html = workflow.annotation_html().decode('utf-8')
        self.assertIn('frame/' + value['clip_id'] + '/0', html)
        self.assertNotIn('frame/' + first + '/0', html)
        self.assertEqual(source_hashes(self.intake), original); self.assertEqual(source_hashes(self.target), target)
        self.assertFalse(meta['execution_ready']); self.assertFalse(meta['model_started'])

    def test_unknown_inputs_require_explicit_declaration_and_no_model_can_start(self):
        workflow = ui.SuiteIntakeWorkflow(self.intake, self.draft)
        original = source_hashes(self.intake)
        with patch('kiroclops.local_engine.launcher.run_media') as model, \
             patch('kiroclops.local_engine.launcher.run_suite') as suite_model:
            result = workflow.attach(self.form(workflow, inputs=False))
            self.assertEqual(result['status'], 'needs_input_binding')
            self.assertEqual(suite_intake.coverage(self.draft)['clips'], [])
            result = workflow.attach(self.form(workflow))
            self.assertEqual(result['status'], 'clip_added')
            model.assert_not_called(); suite_model.assert_not_called()
        self.assertEqual(result['clip']['input_provenance'], 'user_declared')
        self.assertIn('closed early', result['coverage']['requests'][0])
        self.assertFalse(result['execution_ready']); self.assertEqual(source_hashes(self.intake), original)
        selection = read_json(self.draft / 'clips/open-01/selection.json')
        self.assertEqual(selection['objects'][0]['template_frame_sha256'], self.selection(workflow)['objects'][0]['template_frame_sha256'])

    def test_same_screen_extra_clip_freezes_intake_coverage_without_fake_validation(self):
        workflow = ui.SuiteIntakeWorkflow(self.intake, self.draft)
        workflow.attach(self.form(workflow))
        workflow.new_clip({'video': str(self.package(9)), 'target': str(self.target), 'request': self.goal})
        workflow.attach(self.form(workflow, 'interrupt-01', ['interrupt']))
        result = workflow.freeze()
        self.assertEqual(result['status'], 'suite_ready_intake'); self.assertTrue(result['frozen'])
        self.assertFalse(result['execution_ready']); self.assertFalse(result['behavior_accepted'])
        self.assertFalse(result['benchmark_qualified']); self.assertFalse(result['model_started'])
        self.assertEqual(len(result['clips']), 2)
        with self.assertRaises(VisualError): workflow.new_clip({'video': str(self.first_package), 'target': str(self.target), 'request': self.goal})
        self.assertEqual(workflow.freeze(), result)

    def test_http_routes_serve_bound_png_only_and_reject_wrong_host_origin_token(self):
        server = self.start_server()
        with self.get(server, 'metadata') as response: meta = json.load(response)
        identifier = meta['clip_id']
        with self.get(server, f'frame/{identifier}/0') as response:
            self.assertEqual(response.headers['Content-Type'], 'image/png')
            self.assertTrue(response.read().startswith(b'\x89PNG'))
        with self.get(server, 'index.html') as response:
            page = response.read().decode('utf-8')
            self.assertIn('Prepare this video', page); self.assertNotIn('kiroclopsSelection=()=>', page)
        with self.get(server, 'annotation.html') as response:
            self.assertIn('kiroclopsSelection=()=>', response.read().decode('utf-8'))
        for path, headers in ((f'frame/{identifier}/../intake.json', {}), ('target/src/app.js', {}),
                              ('metadata', {'Host': 'attacker.local'}), ('metadata', {'Origin': 'https://attacker.local'})):
            with self.assertRaises(HTTPError): self.get(server, path, headers)
        with self.assertRaises(HTTPError): urlopen(server.url.replace(server.url.split('/')[3], 'bad-token'), timeout=10)
        with self.assertRaises(HTTPError): self.post(server, 'freeze', {}, {'Origin': 'https://attacker.local'})
        with self.assertRaises(HTTPError): self.post(server, 'freeze', {}, {'Sec-Fetch-Site': 'cross-site'})
        request = Request(server.url.rsplit('/', 1)[0] + '/freeze', data=b'{}',
                          headers={'Content-Type': 'application/json'}, method='POST')
        with self.assertRaises(HTTPError): urlopen(request, timeout=10)

    def test_http_selection_hash_tamper_and_oversized_requests_fail_closed(self):
        server = self.start_server(); form = self.form(server.workflow)
        bad = deepcopy(form); bad['selection']['objects'][0]['template_frame_sha256'] = 'a' * 64
        with self.assertRaises(HTTPError): self.post(server, 'attach', bad)
        with patch.object(ui, 'MAX_REQUEST_BYTES', 16):
            with self.assertRaises(HTTPError): self.post(server, 'attach', form)
        with self.post(server, 'attach', form) as response:
            self.assertEqual(json.load(response)['status'], 'clip_added')
        identifier = server.workflow.current
        frame_path = self.intake / 'package' / server.workflow.package(identifier)['frames'][0]['path']
        frame_path.write_bytes(b'changed')
        with self.assertRaises(HTTPError): self.get(server, f'frame/{identifier}/0')

    def test_server_lifetime_lease_and_declared_provenance_are_explicit(self):
        server = ui.make_server(self.intake, self.draft)
        try:
            with self.assertRaises(VisualError) as caught: ui.make_server(self.intake, self.draft)
            self.assertEqual(caught.exception.code, 'ui_already_running')
        finally: server.server_close()
        reopened = ui.make_server(self.intake, self.draft); reopened.server_close()
        with self.assertRaises(VisualError): reopened.workflow.attach(self.form(reopened.workflow))
        events = [{'time_s': .1, 'action': 'click', 'selector': '#open-panel', 'source': 'recorder_verified'}]
        with self.assertRaises(VisualError): ui.declared_form_inputs(events)


if __name__ == '__main__': unittest.main()
