import copy
import unittest

from kiroclops.capture import validate_recording


class CaptureContractTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(frame_count=240, warmup_ticks=60, start_tick=30, hold_ticks=3)
        self.recording = dict(status="ok", fps=60, viewport=[228,128], frame_count=240, settled=True,
                              frames=[dict(frame_index=i, physics_tick=60+i, time_s=round(i/60,6),
                                           actions=["jump"] if 30 <= i < 33 else []) for i in range(240)])

    def test_complete_capture_matches_physics_and_input(self):
        validate_recording(self.recording, self.config, "tiny")

    def test_input_shift_cannot_silently_become_a_valid_capture(self):
        self.recording["frames"][30]["actions"] = []
        with self.assertRaisesRegex(RuntimeError, "action phase"):
            validate_recording(self.recording, self.config, "tiny")

    def test_skipped_tick_wrong_clock_or_unsettled_capture_rejected(self):
        for field, value in [("physics_tick", 100), ("time_s", .5)]:
            changed = copy.deepcopy(self.recording)
            changed["frames"][10][field] = value
            with self.assertRaises(RuntimeError):
                validate_recording(changed, self.config, "tiny")
        self.recording["settled"] = False
        with self.assertRaisesRegex(RuntimeError, "settle"):
            validate_recording(self.recording, self.config, "tiny")


if __name__ == "__main__":
    unittest.main()
