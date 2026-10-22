import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np

from kiroclops.local_engine.intake import prepare_intake
from kiroclops.local_engine.intake_ui_server import IntakeWorkflow, declared_form_inputs, make_server
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, read_json, write_json
from kiroclops.web_local.specs import fixture_project


class IntakeUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kiroclops UI café naïve ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        images = []
        for index in range(3):
            array = np.zeros((320, 320, 3), dtype=np.uint8)
            array[130:150, 70+index*20:90+index*20] = [180, 220, 60]
            path = self.root / f'frame-{index}.png'
            path.write_bytes(cv2.imencode('.png', array)[1].tobytes())
            images.append(path)
        create_package(self.root / 'video', images, [0., .5, 1.])
        self.directory = self.root / 'intake'
        # Object selection is explicit in these workflow tests; tracking itself
        # has its own image-based tests and is not replaced in production.
        with patch('kiroclops.local_engine.intake._qualify_automatic', return_value=(None, {'status': 'needs_selection'})):
            prepare_intake(self.root / 'video', fixture_project('panel'), 'Approximate the motion.', self.directory)
        manifest = load_package(self.directory / 'package')
        self.selection = {'schema_version': 1, 'kind': 'ObjectSelection', 'package_sha256': manifest['package_sha256'],
            'objects': [{'id': 'object-01', 'roi': [70, 130, 20, 20], 'anchor': [10, 10],
                'body_height_px': 20, 'frame_index': 0, 'template_frame_sha256': manifest['frames'][0]['sha256'], 'method': 'template'}],
            'background_rois': [[0, 0, 30, 30]]}

    def test_declarations_cannot_upgrade_certainty(self):
        events = [{'time_s': .1, 'action': 'click', 'selector': '#open-panel'}]
        value = declared_form_inputs(events)
        self.assertEqual(value['provenance'], 'user_declared')
        with self.assertRaises(VisualError):
            declared_form_inputs([{**events[0], 'source': 'recorder_verified'}])

    def test_fresh_form_creates_real_session_without_a_model_or_download(self):
        workflow = IntakeWorkflow(self.directory)
        original = read_json(self.directory / 'intake.json')
        with patch('kiroclops.local_engine.intake._qualify_automatic', return_value=(None, {'status': 'needs_selection'})), patch('kiroclops.local_engine.launcher.run_media') as model:
            job = workflow.submit({'selection': self.selection, 'object_map': {'object-01': 'handle'},
                'inputs': [{'time_s': .1, 'action': 'click', 'selector': '#open-panel'}], 'launch': False})
            deadline = time.monotonic() + 15
            while (state := workflow.status(job['job_id']))['status'] in ('queued', 'running') and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(state['status'], 'completed', state)
            model.assert_not_called()
        self.assertEqual(read_json(self.directory / 'intake.json'), original)
        session = read_json(Path(state['session']) / 'session.json')
        self.assertEqual(session['experiment']['input_provenance'], 'user_declared')
        self.assertEqual(session['experiment']['heldout_status'], 'not_provided')
        self.assertTrue((Path(state['session']) / 'target/.kiro/agents/kiroclops-local-tools.json').exists())

    def test_restart_marks_interrupted_and_keeps_request(self):
        workflow = IntakeWorkflow(self.directory)
        identifier = 'attempt-' + 'a'*24
        path = workflow.output / identifier
        path.mkdir()
        write_json(path / 'status.json', {'job_id': identifier, 'status': 'running'})
        write_json(path / 'request.json', {'kept': True})
        restarted = IntakeWorkflow(self.directory)
        self.assertEqual(restarted.status(identifier)['status'], 'interrupted')
        self.assertEqual(restarted.metadata()['current_job'], identifier)
        self.assertEqual(restarted.metadata()['last_form'], {'kept': True})
        self.assertEqual(read_json(path / 'request.json'), {'kept': True})

    def test_embedded_close_keeps_lease_until_work_finishes(self):
        server = make_server(self.directory)
        release = threading.Event()
        def hold(*args):
            release.wait(3)
        with patch.object(server.workflow, '_execute', side_effect=hold):
            server.workflow.submit({'selection': self.selection})
            server.server_close()
            try:
                with self.assertRaises(VisualError) as duplicate:
                    make_server(self.directory)
                self.assertEqual(duplicate.exception.code, 'ui_already_running')
                with self.assertRaises(VisualError) as closed:
                    server.workflow.submit({'selection': self.selection})
                self.assertEqual(closed.exception.code, 'ui_closed')
            finally:
                release.set()
                self.assertTrue(server.workflow.finished.wait(2))
        # The closer releases asynchronously after the worker's terminal signal.
        deadline = time.monotonic()+2
        while True:
            try:
                reopened = make_server(self.directory)
                reopened.server_close()
                break
            except VisualError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.01)

    def test_native_failure_is_not_workflow_completion(self):
        workflow = IntakeWorkflow(self.directory)
        with patch('kiroclops.local_engine.intake._qualify_automatic', return_value=(None, {'status': 'needs_selection'})), patch('kiroclops.local_engine.launcher.run_media', return_value={'status': 'tool_denied', 'error': 'Native tool denied', 'prompt_sent': False}) as model:
            job = workflow.submit({'selection': self.selection, 'object_map': {'object-01': 'handle'},
                'inputs': [{'time_s': .1, 'action': 'click', 'selector': '#open-panel'}], 'launch': True})
            deadline = time.monotonic() + 15
            while (state := workflow.status(job['job_id']))['status'] in ('queued', 'running') and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(state['status'], 'failed', state)
            self.assertEqual(state['error']['code'], 'tool_denied')
            self.assertFalse(state['launch_result']['prompt_sent'])
            model.assert_called_once_with(state['session'])

    def test_cannot_submit_over_active_work(self):
        workflow = IntakeWorkflow(self.directory)
        release = threading.Event()
        finished = threading.Event()
        def hold(*args):
            release.wait(3)
            finished.set()
        with patch.object(workflow, '_execute', side_effect=hold):
            workflow.submit({'selection': self.selection})
            try:
                with self.assertRaises(VisualError) as busy:
                    workflow.submit({'selection': self.selection})
                self.assertEqual(busy.exception.code, 'job_running')
            finally:
                release.set()
                self.assertTrue(finished.wait(2))

    def test_original_request_mutation_is_rejected(self):
        workflow = IntakeWorkflow(self.directory)
        altered = read_json(self.directory / 'intake.json')
        altered['request'] = 'Another request'
        write_json(self.directory / 'intake.json', altered)
        with self.assertRaises(VisualError) as mismatch:
            workflow._verify_intake()
        self.assertEqual(mismatch.exception.code, 'intake_binding_changed')

    def test_foreign_job_id_and_payload_rejected(self):
        workflow = IntakeWorkflow(self.directory)
        for identifier in ('../../secret', 'attempt-'+'z'*24, 'attempt-123'):
            with self.assertRaises(VisualError):
                workflow.status(identifier)
        with self.assertRaises(VisualError):
            workflow.submit({'selection': self.selection, 'session_path': 'C:/outside'})
        with self.assertRaises(VisualError):
            workflow.submit({'selection': self.selection, 'launch': 'yes'})

    def test_http_serves_only_bound_images_and_current_ui(self):
        server = make_server(self.directory)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with urlopen(server.url) as response:
                html = response.read().decode('utf-8')
            self.assertIn('Save and continue', html)
            base = server.url.removesuffix('annotation.html')
            with urlopen(base + 'metadata') as response:
                self.assertEqual(json.load(response)['domain'], 'web_panel')
            manifest = load_package(self.directory / 'package')
            with urlopen(base + 'package/' + manifest['frames'][0]['path']) as response:
                self.assertEqual(response.headers['Content-Type'], 'image/png')
            for path in ('intake.json', 'package/manifest.json', '../intake.json', 'package/../../intake.json'):
                with self.assertRaises(HTTPError):
                    urlopen(base + path)
            request = Request(base + 'metadata', headers={'Origin': 'https://external.example'})
            with self.assertRaises(HTTPError):
                urlopen(request)
            with self.assertRaises(VisualError) as duplicate:
                make_server(self.directory)
            self.assertEqual(duplicate.exception.code, 'ui_already_running')
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
