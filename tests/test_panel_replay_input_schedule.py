"""Prospective panel replay scheduler, with virtual time and no browser.

The late-image/RAF durations reproduce the recorder seam, not a movement
model. Actual scheduler waits, deadline checks and input-history gates run.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

from kiroclops.local_engine.realistic_policy import input_history_check
from kiroclops.web_local import capture, natural_capture


class VirtualClock:
    def __init__(self, now):
        self.now = now

    async def sleep(self, duration):
        self.now += duration


def observation(pointer_up):
    # Preserve the affected recording's frame cadence and physical point.
    return {"objects": [{"frames": [{"time_s": i * .016746998} for i in range(5)]}],
            "inputs": {"provenance": "recorder_verified", "events": [
                {"action": "drag_move", "fraction": 1., "time_s": 0., "client_x": 654., "client_y": 352.},
                {"action": "drag_end", "time_s": pointer_up, "client_x": 654., "client_y": 352.}]}}


def scenario(domain="panel", *, replay=True, version="reference-suite-web-v2"):
    value = {"domain": domain, "frame_driver": "natural_browser_raf_v1", "suite_version": version}
    event = {"time_s": .0684, "action": "drag_end"}
    if replay:event["replay_client_point"] = [654., 352.]
    return value, event


class PanelReplayInputSchedule(unittest.IsolatedAsyncioTestCase):
    async def run_schedule(self, *, replay=True, image_duration=.027, boundary_duration=.018,
                           latest_image=-1., action="drag_end", domain="panel", checks=lambda: None):
        recipe, event = scenario(domain, replay=replay)
        event["action"] = action
        stage = natural_capture.input_boundary_stage(recipe, event)
        due = event["time_s"]
        clock = VirtualClock(due - .040)
        trace = []
        png = {"data": "unchanged-real-png", "timestamp_source": "cdp_screenshot_bracket"}
        raf = {"driver": "browser", "frameTimeTicks_supplied": False}

        async def boundary():
            trace.append(("raf-start", clock.now))
            raf["host_before_s"] = clock.now
            clock.now += boundary_duration
            raf["host_after_s"] = clock.now
            return raf

        async def checkpoint(*, eligible):
            self.assertTrue(eligible())
            trace.append(("png-start", clock.now))
            before = clock.now
            clock.now += image_duration
            png.update(epoch_s=(before + clock.now) / 2, interval_epoch_s=[before, clock.now])
            return png

        def dispatch_ready():
            trace.append(("due-ready", clock.now))
            capture.require_input_deadline(due, clock.now)

        before_recipe, before_event = deepcopy(recipe), deepcopy(event)
        with patch.object(capture, "utc_seconds", side_effect=lambda: clock.now), \
                patch.object(capture.asyncio, "sleep", side_effect=clock.sleep):
            frame, receipt = await natural_capture.schedule_input_observation(due,
                checkpoint=checkpoint, latest_image_time=lambda: latest_image, check=checks,
                policy=natural_capture.input_pixel_policy(domain), boundary=boundary,
                boundary_stage=stage, dispatch_ready=dispatch_ready)
            # This is the actual time at which the caller can issue pointer-up,
            # rather than its scheduled deadline or the screenshot midpoint.
            trace.append(("trusted-up", clock.now))
            capture.require_input_deadline(due, clock.now)
        self.assertEqual((before_recipe, before_event), (recipe, event))
        self.assertIs(frame, png if latest_image < due - .020 else None)
        self.assertIs(receipt, raf)
        return clock.now, trace, png, raf

    async def test_replay_does_not_pay_observed_reference_boundary_again(self):
        actual, trace, frame, receipt = await self.run_schedule()
        self.assertEqual(["raf-start", "png-start", "due-ready", "trusted-up"], [row[0] for row in trace])
        self.assertLess(receipt["host_after_s"], .0684)
        self.assertGreater(actual, .0684)  # Slow real PNG remains honestly late.
        self.assertAlmostEqual(.0854, actual)
        self.assertAlmostEqual(.017, actual - .0684)
        self.assertEqual([.0584, .0854], [round(t, 4) for t in frame["interval_epoch_s"]])
        result = input_history_check(observation(.0684), observation(actual))
        self.assertTrue(result["pass"], result)
        self.assertAlmostEqual(.025120497, result["tolerance_s"])
        self.assertFalse(result["duration_rescaled"])

    async def test_canonical_recording_retains_existing_post_due_raf(self):
        actual, trace, _, _ = await self.run_schedule(replay=False)
        self.assertEqual(["png-start", "due-ready", "raf-start", "trusted-up"], [row[0] for row in trace])
        self.assertAlmostEqual(.1034, actual)
        # Reapplying this canonical scheduling cost to an actual-time replay
        # produces the specific adjacent gap defect, under the unchanged gate.
        result = input_history_check(observation(.0684), observation(actual))
        self.assertFalse(result["pass"])
        self.assertGreater(result["maximum_adjacent_difference_s"], .030)

    async def test_fresh_primary_pixels_do_not_force_new_screenshot(self):
        actual, trace, _, _ = await self.run_schedule(latest_image=.0584)
        self.assertEqual(["raf-start", "due-ready", "trusted-up"], [row[0] for row in trace])
        self.assertAlmostEqual(.0684, actual)

    async def test_slow_pixels_can_still_fail_input_comparability(self):
        actual, _, _, _ = await self.run_schedule(image_duration=.055)
        result = input_history_check(observation(.0684), observation(actual))
        self.assertFalse(result["pass"])
        self.assertGreater(result["maximum_adjacent_difference_s"], .030)

    async def test_true_deadline_overrun_is_not_hidden_or_timewarped(self):
        with self.assertRaises(capture.CaptureError) as caught:
            await self.run_schedule(image_duration=.150)
        self.assertEqual("capture_timing_failure", caught.exception.code)

    async def test_cancellation_survives_waiting_and_preparation(self):
        def cancelled():
            raise capture.CaptureError("cancelled", "stop")
        with self.assertRaises(capture.CaptureError) as caught:
            await self.run_schedule(checks=cancelled)
        self.assertEqual("cancelled", caught.exception.code)

    def test_only_bound_panel_v2_replay_changes_boundary_phase(self):
        for domain, replay, version, expected in [
                ("panel", True, "reference-suite-web-v2", "preparation"),
                ("panel", False, "reference-suite-web-v2", "dispatch"),
                ("panel", True, "reference-suite-web-v1", "dispatch"),
                ("svg", True, "reference-suite-web-v2", "dispatch")]:
            value, event = scenario(domain, replay=replay, version=version)
            self.assertEqual(expected, natural_capture.input_boundary_stage(value, event))
        value, event = scenario()
        event["action"] = "drag_move"
        self.assertIsNone(natural_capture.input_boundary_stage(value, event))
        value["frame_driver"] = "natural_browser_v1"
        event["action"] = "drag_end"
        self.assertIsNone(natural_capture.input_boundary_stage(value, event))

    def test_click_and_drag_start_replay_times_also_include_the_reference_wait(self):
        value, event = scenario()
        for action in ("click", "drag_start", "drag_end"):
            event["action"] = action
            self.assertEqual("preparation", natural_capture.input_boundary_stage(value, event))

    async def test_unrecognised_or_missing_boundary_policy_is_rejected(self):
        for boundary, stage in ((None, "preparation"), (lambda: None, None), (None, "unknown")):
            with self.assertRaises(ValueError):
                await natural_capture.schedule_input_observation(1., boundary=boundary, boundary_stage=stage)


if __name__ == "__main__":unittest.main()
