"""Regression checks for the first-input browser command collision."""
import unittest

from kiroclops.web_local.capture import (
    CHECKPOINT_POLICY, FRAME_CONTROL_POLICY, DeliveryBackpressure,
    idle_checkpoint_due,
    pre_first_input_capture_action, pump_deadline,
)
from kiroclops.web_local.specs import TIMING_LIMITS


class FirstInputScheduleTests(unittest.TestCase):
    def test_static_baseline_has_real_png_outside_quiet_window(self):
        self.assertEqual(pre_first_input_capture_action(
            since_frame_s=.090, upcoming_input_s=.250, input_in_flight=False),
            'actual_png')
        self.assertEqual(FRAME_CONTROL_POLICY['pre_first_input'],
                         'routine_frames_continue_png_quiet_input_guard')

    def test_first_input_guard_rejects_stale_pixel_checkpoint(self):
        guard = CHECKPOINT_POLICY['first_input_quiet_s']
        self.assertGreaterEqual(guard,
                                .120 + FRAME_CONTROL_POLICY['input_preparation_lead_s'])
        for remaining in (guard, guard - .001, 0, -.001):
            with self.subTest(remaining=remaining):
                self.assertEqual(pre_first_input_capture_action(
                    since_frame_s=.5, upcoming_input_s=remaining,
                    input_in_flight=False), 'hold')
        self.assertEqual(pre_first_input_capture_action(
            since_frame_s=.5, upcoming_input_s=guard + .001,
            input_in_flight=False), 'actual_png')

    def test_no_png_during_input_or_when_actual_pixel_is_fresh(self):
        # The input may finish while a routine frame is awaited; a stale
        # first-input decision must not crash when no next input remains.
        self.assertEqual(pre_first_input_capture_action(
            since_frame_s=.5, upcoming_input_s=None,
            input_in_flight=False), 'hold')
        self.assertEqual(pre_first_input_capture_action(
            since_frame_s=.5, upcoming_input_s=.3,
            input_in_flight=True), 'hold')
        self.assertEqual(pre_first_input_capture_action(
            since_frame_s=.010, upcoming_input_s=.3,
            input_in_flight=False), 'hold')

    def test_actual_png_completion_resets_native_frame_cadence(self):
        interval = FRAME_CONTROL_POLICY['nominal_interval_s']
        self.assertEqual(pump_deadline(10.,10.010),10.+interval)
        self.assertEqual(pump_deadline(10.,10.010,after_png=True),10.010+interval)
        # A late native command can skip a scheduled slot, never burst-catch-up.
        self.assertEqual(pump_deadline(10.,10.050),10.050)

    def test_normal_checkpoint_policy_remains_available_after_first_input(self):
        self.assertTrue(idle_checkpoint_due(since_frame_s=.090,
            upcoming_input_s=.2,input_in_flight=False,since_input_s=.1))

    def test_routine_draw_waits_for_observation_or_bounded_quiet_period(self):
        pressure=DeliveryBackpressure()
        pressure.native_completed(now=10.)
        self.assertEqual(pressure.skip_reason(now=10.017),'unobserved_native_frame')
        pressure.received(0,100.,100.006,now=10.018)
        self.assertEqual(pressure.skip_reason(now=10.020),'screencast_ack_pending')
        # An input boundary is dispatched directly, without this routine guard.
        self.assertEqual(pressure.skip_reason(final_input_boundary=True,now=10.020),
                         'final_input_boundary')
        pressure.ack_finished(0)
        self.assertIsNone(pressure.skip_reason(now=10.021))
        pressure.native_completed(now=10.021)
        self.assertEqual(pressure.skip_reason(now=10.050),'unobserved_native_frame')
        self.assertIsNone(pressure.skip_reason(now=10.062))

    def test_late_screencast_delivery_and_pending_ack_suppress_optional_draws(self):
        pressure=DeliveryBackpressure()
        pressure.received(0,100.,100.090,now=10.)
        pressure.ack_finished(0)
        self.assertEqual(pressure.skip_reason(now=10.010),'late_delivery_recovery')
        self.assertIsNone(pressure.skip_reason(png=True,now=10.010))
        self.assertIsNone(pressure.skip_reason(now=10.041))
        self.assertGreaterEqual(pressure.snapshot()['maximum_delivery_lag_s'],.090)
        pressure.received(1,101.,101.005,now=11.)
        self.assertEqual(pressure.skip_reason(now=11.121),'screencast_ack_pending')
        self.assertIsNone(pressure.skip_reason(png=True,now=11.121))
        pressure.ack_finished(1,now=11.130)
        self.assertGreaterEqual(pressure.snapshot()['maximum_ack_age_s'],.130)

    def test_published_timing_limits_are_unchanged(self):
        self.assertEqual(FRAME_CONTROL_POLICY['maximum_command_lateness_s'],.120)
        self.assertEqual({k:TIMING_LIMITS[k] for k in (
            'median_interval_s','p95_interval_s','maximum_motion_gap_s','clock_uncertainty_s')},
            {'median_interval_s':.050,'p95_interval_s':.080,
             'maximum_motion_gap_s':.120,'clock_uncertainty_s':.020})


if __name__ == '__main__':
    unittest.main()
