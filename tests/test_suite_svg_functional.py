"""Tiny synthetic owned-receipt fixtures; no process/browser/capture/attestation.

The fixture process identities and executable bytes are explicitly invented
for parser/gate tests. They do not qualify a real execution or SVG behavior.
"""
from copy import deepcopy
import asyncio
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiroclops.local_engine import suite_svg_functional as functional
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


def observed_trace():
    rows = []
    stamp = time.time() - 8
    def action(name, args=(), kwargs=None, value=None):
        nonlocal stamp
        row = {'action': name, 'args': list(args), 'kwargs': kwargs or {},
               'started_at': stamp, 'finished_at': stamp + .001, 'value': value}
        stamp += .01
        rows.append(row)
    def snapshot(count, selected, capture=False):
        action('snapshot', [functional._snapshot_script()], value={'finite': True, 'count': count,
            'selected': selected, 'pointer_capture': capture, 'value': None, 'max': None})
        rows[-1]['geometry'] = [{'id': chr(97 + i), 'node_id': chr(97 + i), 'box': [i * 10, 0, 4, 4]} for i in range(count)]
    action('browser.new_page', kwargs={'viewport': {'width': 960, 'height': 640}, 'device_scale_factor': 1})
    action('page.add_init_script', ["document.addEventListener('pointerdown',e=>window.__kcSuitePointerId=e.pointerId,true)"])
    action('page.goto', ['http://127.0.0.1:8888/'], {'wait_until': 'networkidle'},
           {'url': 'http://127.0.0.1:8888/', 'status': 200, 'ok': True})
    action('locator:#reset.click')
    action('locator:[data-node-id="b"].click')
    action('locator:#add-node.click')
    action('page.wait_for_timeout', [600])
    snapshot(4, ['b'])
    action('locator:#remove-node.click')
    action('page.wait_for_timeout', [600])
    snapshot(3, ['b'])
    action('page.set_viewport_size', [{'width': 1280, 'height': 720}])
    action('page.wait_for_timeout', [600])
    snapshot(3, ['b'])
    action('locator:[data-node-id="b"].bounding_box', value={'x': 10, 'y': 20, 'width': 8, 'height': 6})
    action('mouse.move', [14., 23.])
    action('mouse.down')
    snapshot(3, ['b'], True)
    action('evaluate', ['window.__kcSuitePointerId||1'], value=7)
    action('locator:[data-node-id="b"].dispatch_event', ['pointercancel', {'pointerId': 7, 'bubbles': True}])
    action('mouse.up')
    action('page.wait_for_timeout', [600])
    snapshot(3, ['b'])
    for selector in ('#add-node', '#remove-node', '#add-node'):
        action('locator:' + selector + '.click')
        action('page.wait_for_timeout', [40])
    action('page.wait_for_timeout', [800])
    snapshot(4, ['b'])
    action('locator:#reset.click')
    action('page.wait_for_timeout', [600])
    snapshot(3, [])
    action('browser.close')
    return {'kind': 'ObservedSVGFunctionalActions', 'schema_version': 1, 'actions': rows, 'page_errors': [],
            'browser': {'version': 'fixture-version', 'launch_options': {'headless': True, 'args': []}}}


class FunctionalPredicateTests(unittest.TestCase):
    def test_real_predicate_replay_uses_states_and_actions(self):
        trace = observed_trace()
        result = functional.replay_trace(trace)
        self.assertEqual('pass', result['status'])
        self.assertEqual(functional.CHECKS, {r['id'] for r in result['checks']})
        for index, field, value, failed in ((7, 'count', 3, 'selection_add_remove'),
             (13, 'selected', [], 'resize_structure'), (22, 'pointer_capture', True, 'pointer_cancel'),
             (30, 'count', 3, 'rapid_mode_changes'), (33, 'selected', ['b'], 'reset')):
            changed = deepcopy(trace)
            changed['actions'][index]['value'][field] = value
            if field == 'count':
                changed['actions'][index]['geometry'] = changed['actions'][index]['geometry'][:value]
            result = functional.replay_trace(changed)
            self.assertEqual('fail', result['status'])
            self.assertFalse(next(r for r in result['checks'] if r['id'] == failed)['pass'])

    def test_missing_reordered_foreign_actions_deadline_and_pointer_are_closed(self):
        base = observed_trace()
        variants = []
        v = deepcopy(base); v['actions'].pop(5); variants.append(v)
        v = deepcopy(base); v['actions'][5], v['actions'][8] = v['actions'][8], v['actions'][5]; variants.append(v)
        v = deepcopy(base); v['actions'][2]['args'] = ['https://example.org']; variants.append(v)
        v = deepcopy(base); v['actions'][6]['args'] = [1]; variants.append(v)
        v = deepcopy(base); v['actions'][19]['args'][1]['pointerId'] = 8; variants.append(v)
        v = deepcopy(base); v['actions'][15]['args'] = [0, 0]; variants.append(v)
        v = deepcopy(base); v['actions'][7]['started_at'] = 0; variants.append(v)
        for variant in variants:
            with self.subTest(variant=variants.index(variant)), self.assertRaises(VisualError):
                functional.replay_trace(variant)

    def test_typed_finite_observation_gate_prevents_flag_only_pass(self):
        for field, value in (('finite', 1), ('count', True), ('selected', ['b', 'b']),
                             ('pointer_capture', 1), ('value', 1), ('max', 2)):
            variant = observed_trace(); variant['actions'][7]['value'][field] = value
            with self.subTest(field=field), self.assertRaises(VisualError):
                functional.replay_trace(variant)
        for geometry in (None, [], [{'id': 'x', 'node_id': 'x', 'box': [0, 0, float('nan'), 1]}] * 3):
            variant = observed_trace(); variant['actions'][7]['geometry'] = geometry
            with self.assertRaises(VisualError):
                functional.replay_trace(variant)
        variant = observed_trace(); variant['page_errors'] = ['actual page error fixture']
        self.assertEqual('fail', functional.replay_trace(variant)['status'])

    def test_actual_response_object_is_normalized_without_altering_checker_return(self):
        response = SimpleNamespace(url='http://127.0.0.1:8888/', status=200, ok=True)
        async def goto(*args, **kwargs): return response
        trace = functional._Trace()
        actual = asyncio.run(trace.call('page.goto', goto, response.url, wait_until='networkidle'))
        self.assertIs(response, actual)
        self.assertEqual({'url': response.url, 'status': 200, 'ok': True}, json.loads(json.dumps(trace.rows))[0]['value'])

    def test_numeric_boolean_aliases_and_geometry_identity_mismatches_rejected(self):
        variants = []
        v = observed_trace(); v['actions'][0]['kwargs']['device_scale_factor'] = True; variants.append(v)
        v = observed_trace(); v['actions'][19]['args'][1]['bubbles'] = 1; variants.append(v)
        v = observed_trace(); v['actions'][7]['geometry'].pop(); variants.append(v)
        v = observed_trace(); v['actions'][7]['geometry'][1]['node_id'] = 'x'; variants.append(v)
        v = observed_trace(); v['actions'][7]['geometry'][1]['node_id'] = 'a'; variants.append(v)
        v = observed_trace(); v['actions'][2]['value']['ok'] = 1; variants.append(v)
        for v in variants:
            with self.assertRaises(VisualError): functional.replay_trace(v)


class FunctionalReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'execution'; self.output.mkdir()
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'index.html').write_text('fixture only', encoding='utf-8')
        self.binary = self.root / 'fixture-shell.exe'; self.binary.write_bytes(b'not an executable; gate fixture')
        self.code = self.root / 'fixture-checker.py'; self.code.write_text('fixture code', encoding='utf-8')
        self.sources = {str(self.code): file_sha256(self.code)}
        self.request = {'kind': 'UnsignedUnchangedSVGFunctionalRequest', 'created_at': time.time() - 10,
            'initial_acceptance_directory': str(self.root / 'initial'), 'initial_result_sha256': digest('initial-fixture'),
            'source': str(self.source), 'source_hashes': {'index.html': file_sha256(self.source / 'index.html')},
            'source_sha256': digest({'index.html': file_sha256(self.source / 'index.html')}),
            'capture_lock': str(functional.COMMON_CAPTURE_LOCK.resolve()), 'environment': {'PLAYWRIGHT_NODEJS_PATH': 'fixture-node'},
            'browser_inventory': {str(self.binary): file_sha256(self.binary)}}
        request_patch = patch.object(functional, '_request', return_value=deepcopy(self.request)); request_patch.start()
        self.addCleanup(request_patch.stop)
        source_patch = patch.object(functional, '_sources', return_value=self.sources); source_patch.start()
        self.addCleanup(source_patch.stop)
        launch_patch = patch('kiroclops.web_local.natural_capture.browser_arguments', return_value=[]); launch_patch.start()
        self.addCleanup(launch_patch.stop)
        self.trace = observed_trace()
        write_json(self.output / 'request.json', self.request)
        self.worker = {'kind': 'OwnedSVGFunctionalWorkerStart', 'schema_version': 1,
            'request_sha256': digest(self.request), 'challenge_sha256': digest('fixture-challenge'),
            'process': {'pid': 52, 'identity': 'fixture-worker-create-time'}, 'parent_pid': 51,
            'started_at': time.time() - 9, 'source_hashes': self.request['source_hashes'],
            'functional_source_hashes': self.sources, 'capture_lock': self.request['capture_lock']}
        self.result = {'kind': 'OwnedSVGFunctionalWorkerResult', 'schema_version': 1, 'request_sha256': digest(self.request),
            'status': 'succeeded', 'result': functional.replay_trace(self.trace), 'observations_sha256': digest(self.trace),
            'challenge_sha256': self.worker['challenge_sha256'], 'process': self.worker['process'],
            'started_at': self.worker['started_at'], 'finished_at': time.time() - 5,
            'source_hashes_after': self.request['source_hashes'], 'functional_source_hashes_after': self.sources,
            'browser': self.trace['browser'], 'new_model_generation': False}
        self.proof = {'kind': 'OwnedSVGFunctionalExecution', 'schema_version': 1, 'status': 'succeeded',
            'request_sha256': digest(self.request), 'request_file_sha256': file_sha256(self.output / 'request.json'),
            'command': [sys.executable, '-X', 'utf8', '-m', 'scripts.phase3d_svg_functional', '_worker',
                        '--request', str(self.output / 'request.json'), '--output', str(self.output)],
            'cwd': str(Path(functional.__file__).resolve().parents[2]), 'environment': self.request['environment'],
            'parent': {'pid': 50, 'identity': 'fixture-parent-create-time'},
            'child_launcher': {'pid': 51, 'identity': 'fixture-launcher-create-time'},
            'challenge_sha256': self.worker['challenge_sha256'], 'capture_lock': self.request['capture_lock'],
            'max_seconds': functional.MAX_SECONDS, 'python_sha256': file_sha256(sys.executable),
            'functional_source_hashes': self.sources, 'prepared_at': time.time() - 9.8, 'lease_acquired_at': time.time() - 9.7,
            'launched_at': time.time() - 9.6, 'exited_at': time.time() - 4, 'lease_released_at': time.time() - 3,
            'common_lease_acquired': True, 'common_lease_held_until_exit': True, 'exit_code': 0,
            'owned_job': {'kind': 'WindowsKillOnCloseJob', 'assigned_before_resume': True},
            'actual_worker': self.worker['process'], 'owned_job_active_processes_at_exit': 0,
            'owned_job_confirmed_empty_at': time.time() - 4.5,
            'new_model_generation': False, 'functional_qualified': False}
        self.start = {k: v for k, v in self.proof.items() if k not in ('exit_code', 'exited_at',
            'common_lease_held_until_exit', 'lease_released_at', 'actual_worker', 'owned_job_active_processes_at_exit',
            'owned_job_confirmed_empty_at')} | {'status': 'running'}
        self.authorization = {'kind': 'OwnedSVGFunctionalWorkerAuthorization', 'schema_version': 1,
            'request_sha256': digest(self.request), 'worker_start_sha256': digest(self.worker),
            'execution_start_sha256': digest(self.start), 'challenge_sha256': digest('second-fixture-challenge'),
            'worker': self.worker['process'], 'worker_job_handle': 5, 'job_membership_verified': True,
            'authorized_at': time.time() - 8.5}
        self.proof['worker_authorization_sha256'] = digest(self.authorization)
        self.result.update(authorization_sha256=digest(self.authorization), actual_job_membership_verified=True,
                           temporary_job_handle_closed=True)
        stdout = {'worker_result_sha256': digest(self.result), 'challenge_sha256': self.worker['challenge_sha256']}
        (self.output / 'stdout.log').write_text(json.dumps(stdout), encoding='utf-8')
        stderr = 'pw:browser <launching> ' + str(self.binary) + ' --headless\npw:browser <launched> pid=53\n' \
                 'pw:browser [pid=53] <process did exit: exitCode=0, signal=null>\n'
        (self.output / 'stderr.log').write_text(stderr, encoding='utf-8')
        self.proof.update(stdout_sha256=file_sha256(self.output / 'stdout.log'), stderr_sha256=file_sha256(self.output / 'stderr.log'))
        for name, value in (('execution-start.json', self.start), ('execution.json', self.proof),
                            ('worker-start.json', self.worker), ('worker-result.json', self.result), ('observations.json', self.trace),
                            ('worker-authorization.json', self.authorization)):
            write_json(self.output / name, value)
        write_json(self.output / 'browser-launch.json', functional._launched_browser(stderr, self.request, self.trace))
        self.bind()
        proof = functional.inspect_run(self.output)
        self.witness = self.root / 'witness.json'
        self.receipt = {'kind': 'IndependentSVGFunctionalExecutionReview', 'schema_version': 1, 'status': 'pass',
            'method': 'independent_owned_functional_execution_inspection', 'reviewer': 'fixture parser test only',
            'reviewer_role': 'independent_functional_reviewer', 'authored_source': False, 'inspection_performed': True,
            'actual_owned_execution_inspected': True, 'accepted': True, 'verification_sha256': digest(proof),
            'initial_acceptance_directory': self.request['initial_acceptance_directory'], 'source_sha256': self.request['source_sha256'],
            'initial_result_sha256': self.request['initial_result_sha256'], 'new_model_generation': False,
            'full_behavior_acceptance': False, 'inspected_at': time.time() - 1, 'inspected_files': proof['input_files'],
            'checks': [{'id': c, 'verdict': 'pass', 'evidence': 'fixture does not attest an actual run'} for c in functional.CHECKS]}
        write_json(self.witness, self.receipt)

    def bind(self):
        (self.output / 'binding.json').unlink(missing_ok=True)
        write_json(self.output / 'binding.json', {'kind': 'SVGFunctionalExecutionBinding', 'schema_version': 1,
            'files': functional.source_hashes(self.output), 'functional_qualified': False, 'full_behavior_acceptance': False})

    def validate(self, **kwargs):
        return functional.validate_review(self.output, self.witness, directory=self.request['initial_acceptance_directory'],
            source_sha256=self.request['source_sha256'], initial_result_sha256=self.request['initial_result_sha256'],
            browser_versions=kwargs.get('browser_versions', ['fixture-version']))

    def test_tiny_parser_receipt_pass_remains_separate_and_unsigned_run(self):
        self.assertFalse(functional.inspect_run(self.output)['functional_qualified'])
        self.assertEqual('pass', self.validate()['status'])
        self.assertFalse(self.validate()['full_behavior_acceptance'])

    def test_missing_or_selfreported_witness_never_qualifies(self):
        for update in ({'reviewer': ''}, {'authored_source': True}, {'actual_owned_execution_inspected': False},
                       {'checks': []}, {'inspected_files': {}}, {'inspected_at': 0}, {'source_sha256': 'foreign'},
                       {'initial_result_sha256': 'foreign'}, {'new_model_generation': True}):
            write_json(self.witness, self.receipt | update)
            with self.subTest(update=update), self.assertRaises(VisualError):
                self.validate()
        self.witness.unlink()
        with self.assertRaises(OSError): self.validate()

    def test_wrong_browser_source_initial_epoch_and_binary_tamper_fail(self):
        with self.assertRaises(VisualError): self.validate(browser_versions=['foreign-version'])
        with self.assertRaises(VisualError): self.validate(browser_versions=['fixture-version', 'foreign-version'])
        self.binary.write_bytes(b'tampered')
        with self.assertRaises(VisualError): self.validate()

    def test_failed_stale_foreign_owned_execution_flags_rejected_even_if_rebound(self):
        base = deepcopy(self.proof)
        for update in ({'exit_code': 1}, {'status': 'failed'}, {'common_lease_acquired': False},
                       {'functional_source_hashes': {}}, {'request_sha256': 'foreign'}, {'max_seconds': 999},
                       {'parent': {'pid': 50, 'identity': ''}}, {'command': ['foreign-command']}):
            write_json(self.output / 'execution.json', base | update); self.bind()
            with self.subTest(update=update), self.assertRaises(VisualError): functional.inspect_run(self.output)

    def test_live_descendant_or_missing_exact_job_authorization_cannot_qualify(self):
        for name, update in (('execution.json', {'owned_job_active_processes_at_exit': 1}),
                             ('execution.json', {'owned_job': {}}),
                             ('worker-authorization.json', {'worker_job_handle': 0}),
                             ('worker-authorization.json', {'job_membership_verified': False}),
                             ('worker-result.json', {'actual_job_membership_verified': False}),
                             ('worker-result.json', {'temporary_job_handle_closed': False})):
            original = read_json(self.output / name); write_json(self.output / name, original | update); self.bind()
            with self.subTest(name=name), self.assertRaises(VisualError): functional.inspect_run(self.output)
            write_json(self.output / name, original); self.bind()

    def test_direct_worker_without_live_owned_start_never_enters_browser_checker(self):
        fresh = self.root / 'unowned'; fresh.mkdir(); write_json(fresh / 'request.json', self.request)
        with patch.object(functional.suite_web, '_functional_browser_checks', side_effect=AssertionError('no native work')):
            with self.assertRaises(OSError):
                functional.worker(fresh / 'request.json', fresh, 'a' * 64, read_authorization=lambda: {})
        self.assertFalse((fresh / 'worker-start.json').exists())

    def test_common_lease_live_owner_and_fresh_output_are_required_before_worker(self):
        owner = deepcopy(self.start)
        fresh = self.root / 'fresh-start'; fresh.mkdir(); write_json(fresh / 'request.json', self.request)
        owner['command'] = [sys.executable, '-X', 'utf8', '-m', 'scripts.phase3d_svg_functional', '_worker',
                            '--request', str(fresh / 'request.json'), '--output', str(fresh)]
        owner['request_file_sha256'] = file_sha256(fresh / 'request.json')
        owner['challenge_sha256'] = digest('a' * 64)
        owner['launched_at'] = time.time() - 1
        write_json(fresh / 'execution-start.json', owner)
        with patch.object(functional, '_live', return_value=False), patch.object(functional.os, 'getppid', return_value=51):
            with self.assertRaises(VisualError): functional._startup(self.request, fresh, 'a' * 64)
        @contextmanager
        def free_lock(*args, **kwargs): yield None
        with patch.object(functional, '_live', return_value=True), patch.object(functional.os, 'getppid', return_value=51), \
             patch.object(functional, '_lock', free_lock):
            with self.assertRaises(VisualError): functional._startup(self.request, fresh, 'a' * 64)
        with patch.object(functional, '_live', return_value=True), patch.object(functional.os, 'getppid', return_value=51), \
             patch.object(functional, '_lock', side_effect=TimeoutError('fixture owner lease')):
            self.assertEqual(owner, functional._startup(self.request, fresh, 'a' * 64))
            write_json(fresh / 'worker-start.json', {})
            with self.assertRaises(VisualError): functional._startup(self.request, fresh, 'a' * 64)

    def test_exact_owned_job_cleanup_waits_for_all_worker_browser_descendants(self):
        owner = object.__new__(functional._OwnedJob)
        calls = []
        owner.job = SimpleNamespace(handle='fixture exact job handle')
        owner.k = SimpleNamespace(TerminateJobObject=lambda handle, code: calls.append((handle, code)) or True)
        states = iter((3, 1, 0))
        owner.active = lambda: next(states)
        with patch.object(functional.time, 'sleep', return_value=None): owner.stop_and_confirm(self.root)
        self.assertEqual([('fixture exact job handle', 130)] * 3, calls)
        self.assertTrue(read_json(self.root / 'cleanup-pending.json')['capture_lease_retained'])

    def test_temporary_exact_job_handle_close_failure_blocks_native_authorization(self):
        import ctypes
        from ctypes import wintypes as w
        token = 'b' * 64
        value = self.authorization | {'challenge_sha256': digest(token)}
        write_json(self.output / 'worker-authorization.json', value)
        def member(process, handle, result):
            ctypes.cast(result, ctypes.POINTER(w.BOOL)).contents.value = True
            return True
        kernel = SimpleNamespace(GetCurrentProcess=lambda: 1, IsProcessInJob=member, CloseHandle=lambda handle: False)
        with patch.object(functional, '_live', return_value=True), patch.object(ctypes, 'WinDLL', return_value=kernel):
            with self.assertRaises(VisualError) as caught:
                functional._authorization(self.request, self.output, self.worker, self.start,
                    {'challenge': token, 'authorization_sha256': file_sha256(self.output / 'worker-authorization.json')})
        self.assertEqual('owned_job_handle_not_closed', caught.exception.code)

    def test_timeout_cleanup_retains_common_lease_until_exact_job_empty(self):
        destination = self.root / 'timeout'
        events, held = [], {'value': False}
        @contextmanager
        def lease(*args, **kwargs):
            held['value'] = True
            try: yield None
            finally: held['value'] = False; events.append('lease_released')
        class Input(io.BytesIO):
            def write(inner, value):
                if len(value.strip()) == 64:
                    proof = read_json(destination / 'execution-start.json')
                    events.append('startup_after_assignment')
                    write_json(destination / 'worker-start.json', {'request_sha256': digest(self.request),
                        'challenge_sha256': proof['challenge_sha256'], 'functional_source_hashes': self.sources,
                        'parent_pid': 52, 'process': {'pid': 53, 'identity': 'fixture child'}})
                return super().write(value)
        process = SimpleNamespace(pid=52, stdin=Input(), poll=lambda: None, wait=lambda **kw: 0)
        def communicate(**kwargs): raise __import__('subprocess').TimeoutExpired('fixture owned child', 1)
        process.communicate = communicate
        class Owner:
            def assign_and_resume(self, p): events.append('assigned_before_resume')
            def contains(self, p): return True
            def duplicate_for(self, p): return 5
            def stop_and_confirm(self, p):
                self_test.assertTrue(held['value']); events.append('job_empty')
            def close(self):
                self_test.assertTrue(held['value']); events.append('owner_closed')
        self_test = self
        plan = {'suite': str(self.root / 'suite'), 'authoring_parent': str(self.source)}
        with patch.object(functional.initial, 'load_plan', return_value=(plan, {})), \
             patch.object(functional, '_lock', lease), patch.object(functional, '_OwnedJob', Owner), \
             patch.object(functional.subprocess, 'Popen', return_value=process), \
             patch.object(functional, '_identity', return_value='fixture process identity'), \
             patch.object(functional, '_live', return_value=True):
            with self.assertRaises(__import__('subprocess').TimeoutExpired): functional.run(self.output / 'request.json', destination)
        self.assertEqual(['assigned_before_resume', 'startup_after_assignment', 'job_empty', 'owner_closed', 'lease_released'], events)
        self.assertEqual(0, read_json(destination / 'execution-failure.json')['owned_job_active_processes'])

    def test_changed_child_challenge_snapshots_or_stdout_are_closed(self):
        for name, update in (('worker-start.json', {'challenge_sha256': 'foreign'}),
                             ('worker-result.json', {'source_hashes_after': {}}),
                             ('worker-result.json', {'process': {'pid': 99, 'identity': 'foreign'}})):
            original = read_json(self.output / name); write_json(self.output / name, original | update); self.bind()
            with self.subTest(name=name), self.assertRaises(VisualError): functional.inspect_run(self.output)
            write_json(self.output / name, original); self.bind()
        (self.output / 'stdout.log').write_text('{}', encoding='utf-8'); self.bind()
        with self.assertRaises(VisualError): functional.inspect_run(self.output)

    def test_actual_launch_transcript_missing_ambiguous_or_foreign_rejected(self):
        for text in ('', 'pw:browser <launching> foreign --headless\npw:browser <launched> pid=5',
                     (self.output / 'stderr.log').read_text() * 2,
                     (self.output / 'stderr.log').read_text().replace('exitCode=0', 'exitCode=1')):
            with self.assertRaises(VisualError): functional._launched_browser(text, self.request, self.trace)


if __name__ == '__main__':
    unittest.main()
