"""Public suite wrappers must bridge Session callbacks at real native polls.

Only Godot process entry points are stubbed. The wrapper, source-copy checks,
boundary overlay and the actual portable runtime cancellation predicate run.
No engine, browser, image capture, encoding or model is started by this suite.
"""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kiroclops.local_engine import suite_boundary, suite_game_demo
from kiroclops.portable_v2 import runtime
from kiroclops.visual.schema import VisualError


class PollingComplete(Exception):
    """Stop before real rendering or any analysis/receipt synthesis."""


class NativeCancellationSeamTests(unittest.TestCase):
    def _fixture(self, domain, cancel, *, expire_at=None):
        """Reach an actual runtime._cancel poll through the public wrapper."""
        with tempfile.TemporaryDirectory(prefix='kc-') as temp, ExitStack() as stack:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            (source / 'player.gd').write_text('extends CharacterBody2D\n', encoding='utf-8')
            (source / 'player.gd.uid').write_text('uid://immutable-sidecar\n', encoding='utf-8')
            # Path validation is real; only the allocation destination is a
            # small disposable test directory rather than the production root.
            stack.enter_context(patch('kiroclops.local_engine.suite_game._short_runtime', return_value=root / 'native'))
            trace = {'phase': None, 'polls': [], 'deadline_checks': []}

            def check():
                trace['deadline_checks'].append(trace['phase'])
                if trace['phase'] == expire_at and expire_at is not None:
                    raise VisualError('budget_exhausted', 'Frozen deadline expired during native polling.')

            def poll(phase, native_cancel):
                trace['phase'] = phase
                trace['polls'].append(phase)
                if callable(cancel):
                    cancel_phase[0] = phase
                runtime._cancel(native_cancel)  # The genuine Event-contract seam.
                trace['phase'] = None

            def importing(project, output, *, cancel=None, **kwargs):
                self.assertNotEqual(Path(project), source)
                self.assertEqual((Path(project) / 'player.gd').read_text(), (source / 'player.gd').read_text())
                poll('import', cancel)
                return {'status': 'fixture_import_polled_only'}

            def rendering(*args, **kwargs):
                native_cancel = args[3] if domain == 'boundary' else kwargs.get('cancel')
                if domain == 'normal':
                    self.assertTrue(kwargs.get('normal_level'))
                poll('render' if domain == 'boundary' else 'capture', native_cancel)
                raise PollingComplete('Native polls reached; no application was started.')

            cancel_phase = [None]
            # The callable can observe whether cancellation starts at import
            # or at the subsequent render/capture, like an asynchronous request.
            if callable(cancel) and getattr(cancel, '_phase_factory', False):
                cancel = cancel(cancel_phase)
            stack.enter_context(patch.object(runtime, 'import_project', side_effect=importing))
            if domain == 'boundary':
                stack.enter_context(patch('kiroclops.local_engine.visual_boundary_v4._render', side_effect=rendering))
                call = lambda: suite_boundary.capture(source, root / 'receipt.json', 'candidate-02', check, lambda *a: None, cancel)
            else:
                stack.enter_context(patch.object(runtime, 'capture_project', side_effect=rendering))
                stack.enter_context(patch('kiroclops.portable_v2.reporting.encode_video', side_effect=AssertionError('No encoding is permitted.')))
                call = lambda: suite_game_demo.capture(source, root / 'output', check, cancel)
            yield_call = (call, trace, source)
            # Tests execute while the controlled patches/temp paths are alive.
            self._exercise(*yield_call)
            self.assertEqual((source / 'player.gd').read_text(), 'extends CharacterBody2D\n')
            self.assertEqual((source / 'player.gd.uid').read_text(), 'uid://immutable-sidecar\n')

    def _false_callback(self, domain):
        def exercise(call, trace, source):
            with self.assertRaises(PollingComplete):
                call()
            expected = ['import', 'render' if domain == 'boundary' else 'capture']
            self.assertEqual(trace['polls'], expected)
            self.assertTrue(all(phase in trace['deadline_checks'] for phase in expected))
        self._exercise = exercise
        self._fixture(domain, lambda: False)

    def _true_callback(self, domain):
        def exercise(call, trace, source):
            with self.assertRaises(runtime.PortableRuntimeError) as error:
                call()
            self.assertEqual(error.exception.code, 'cancelled')
            self.assertEqual(trace['polls'], ['import'])
        self._exercise = exercise
        self._fixture(domain, lambda: True)

    def _later_callback(self, domain):
        phase_name = 'render' if domain == 'boundary' else 'capture'

        def factory(phase):
            return lambda: phase[0] == phase_name
        factory._phase_factory = True

        def exercise(call, trace, source):
            with self.assertRaises(runtime.PortableRuntimeError) as error:
                call()
            self.assertEqual(error.exception.code, 'cancelled')
            self.assertEqual(trace['polls'], ['import', phase_name])
        self._exercise = exercise
        self._fixture(domain, factory)

    def _event_and_none(self, domain):
        for kind in ('none', 'event_clear', 'event_set'):
            event = None if kind == 'none' else threading.Event()
            if kind == 'event_set':
                event.set()

            def exercise(call, trace, source):
                if kind == 'event_set':
                    with self.assertRaises(runtime.PortableRuntimeError) as error:
                        call()
                    self.assertEqual(error.exception.code, 'cancelled')
                else:
                    with self.assertRaises(PollingComplete):
                        call()
            self._exercise = exercise
            with self.subTest(kind=kind):
                self._fixture(domain, event)

    def _deadline(self, domain):
        for phase in ('import', 'render' if domain == 'boundary' else 'capture'):
            def exercise(call, trace, source):
                with self.assertRaises(VisualError) as error:
                    call()
                self.assertEqual(error.exception.code, 'budget_exhausted')
                self.assertEqual(trace['polls'][-1], phase)
            self._exercise = exercise
            with self.subTest(phase=phase):
                self._fixture(domain, None, expire_at=phase)

    def test_boundary_false_callback_reaches_import_and_render_native_polls(self):
        self._false_callback('boundary')

    def test_normal_false_callback_reaches_import_and_capture_native_polls(self):
        self._false_callback('normal')

    def test_boundary_true_callback_cancels_at_import(self):
        self._true_callback('boundary')

    def test_normal_true_callback_cancels_at_import(self):
        self._true_callback('normal')

    def test_boundary_cancellation_can_start_after_import(self):
        self._later_callback('boundary')

    def test_normal_cancellation_can_start_after_import(self):
        self._later_callback('normal')

    def test_boundary_existing_event_and_none_inputs_remain_supported(self):
        self._event_and_none('boundary')

    def test_normal_existing_event_and_none_inputs_remain_supported(self):
        self._event_and_none('normal')

    def test_boundary_deadline_propagates_from_each_native_poll(self):
        self._deadline('boundary')

    def test_normal_deadline_propagates_from_each_native_poll(self):
        self._deadline('normal')


if __name__ == '__main__':
    unittest.main()
