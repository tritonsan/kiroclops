"""The clip tail needs genuine pixels after 60Hz routine advances stop."""
import unittest

from kiroclops.local_engine.web_reference import (
    MAX_COMPARISON_GAP_S, MIN_COMPARISON_COVERAGE, reference_time_coverage,
)
from kiroclops.web_local.capture import (
    CHECKPOINT_POLICY, FRAME_CONTROL_POLICY, idle_checkpoint_due,
    routine_pump_can_stop, timing_summary,
)
from kiroclops.web_local.specs import scenarios


class TailCaptureTests(unittest.TestCase):
    @staticmethod
    def _reference(times):
        recipe = scenarios('panel', 'calibration')[0]
        return {'metadata': {'scenario': recipe},
                'inputs': {'provenance': 'recorder_verified', 'events': [
                    {'action': 'click', 'selector': '#open-panel', 'time_s': .418}]},
                'frames': [{'time_s': t} for t in times]}

    def test_single_final_png_does_not_cover_clip_end(self):
        active = [round(i * .02, 6) for i in range(112)]
        old = reference_time_coverage(self._reference(active + [3.0]))
        self.assertEqual(old['status'], 'fail')
        self.assertLess(old['windows'][0]['reference_time_coverage_upper_bound'], .75)
        self.assertEqual(MIN_COMPARISON_COVERAGE, .95)
        self.assertEqual(MAX_COMPARISON_GAP_S, .12)

    def test_actual_tail_pixels_restore_unchanged_temporal_gate(self):
        active = [round(i * .02, 6) for i in range(112)]
        tail = [round(2.24 + i * .07, 6) for i in range(11)]
        recorded = active + tail + [3.0]
        self.assertEqual(reference_time_coverage(self._reference(recorded))['status'], 'pass')
        clock = {'uncertainty_s': .001, 'epoch_in_host_bracket': True}
        timing = timing_summary(recorded, [clock],
                                [{'time_s': .418, 'action': 'click'}])
        self.assertEqual(timing['status'], 'pass', timing)

    def test_tail_mode_ends_routine_work_but_keeps_stale_pixel_capture(self):
        self.assertTrue(routine_pump_can_stop(next_input_index=1, input_count=1,
            response_end_epoch_s=2.238, now_epoch_s=2.239))
        self.assertFalse(idle_checkpoint_due(since_frame_s=.029,
            upcoming_input_s=None, input_in_flight=False, since_input_s=1.8))
        self.assertTrue(idle_checkpoint_due(since_frame_s=.031,
            upcoming_input_s=None, input_in_flight=False, since_input_s=1.8))
        self.assertEqual(CHECKPOINT_POLICY['recent_frame_s'], .030)
        # Leave room for one scheduled 60Hz tick and ordinary command jitter
        # before the unchanged 80ms p95 frame-interval gate.
        self.assertLess(CHECKPOINT_POLICY['tail_pixel_stale_s'] +
                        FRAME_CONTROL_POLICY['nominal_interval_s'] + .01, .08)
        self.assertIn('actual_png', FRAME_CONTROL_POLICY['tail_capture'])
        self.assertIn('no_routine_advance', FRAME_CONTROL_POLICY['tail_capture'])


if __name__ == '__main__':
    unittest.main()
