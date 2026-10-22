"""Stage earlier real PNG sampling; keep the frozen cohort and gates unchanged.

The completed panel capture failed solely because eight identical-image static
gaps exceeded80ms. The earlier stale threshold leaves time for PNG acquisition
instead of spending40ms waiting before it. Native verification remains pending.
"""
from copy import deepcopy
from pathlib import Path
import difflib
import importlib.util
import io
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.web_local import natural_capture


class StaticSamplingTests(unittest.TestCase):
    def test_real_screenshot_is_requested_at_10ms_staleness(self):
        self.assertFalse(natural_capture.checkpoint_needed(.009, 0., None, False))
        self.assertTrue(natural_capture.checkpoint_needed(.010, 0., None, False))
        self.assertTrue(natural_capture.checkpoint_needed(.020, 0., 1., False))

    def test_input_and_fresh_image_guards_remain_active(self):
        for upcoming in (0., .01, .03):
            self.assertFalse(natural_capture.checkpoint_needed(1., 0., upcoming, False))
        self.assertFalse(natural_capture.checkpoint_needed(1., 0., None, True))
        self.assertFalse(natural_capture.checkpoint_needed(1., .995, None, False))
        self.assertTrue(natural_capture.checkpoint_needed(1., None, None, False))

    def test_per_domain_input_boundary_policy_is_unchanged(self):
        panel = natural_capture.input_pixel_policy('panel')
        svg = natural_capture.input_pixel_policy('svg')
        self.assertEqual(.010, panel['final_input_image_check_lead_s'])
        self.assertEqual(.020, panel['oldest_final_input_image_before_due_s'])
        self.assertEqual(.040, svg['final_input_image_check_lead_s'])
        self.assertEqual(.060, svg['oldest_final_input_image_before_due_s'])
        for policy in (panel, svg):
            self.assertFalse(policy['frameTimeTicks_supplied'])
            self.assertFalse(policy['routine_frame_advances'])
            self.assertEqual(.030, policy['input_image_quiet_s'])

    def test_bounded_static_screenshot_schedule_leaves_headroom(self):
        # Test the scheduling rule, not invented video or an acceptance claim.
        # Two real PNG command brackets of <=40ms satisfy the existing20ms
        # clock-uncertainty condition. The observer checks every10ms.
        for previous_duration in (.010, .025, .040):
            for next_duration in (.010, .025, .040):
                worst_midpoint_gap = (previous_duration/2
                    + natural_capture.POLICY['static_image_stale_s'] + .010
                    + next_duration/2)
                self.assertLessEqual(worst_midpoint_gap, .060000001)


def main():
    out = ROOT/'artifacts/phase3a/static-sampling-compatibility-01'
    assert not out.exists()
    evidence = ROOT/'artifacts/phase3a/completion-20260927/panel-tools-02-static-tail-diagnostic.json'
    saved = read_json(evidence)
    assert saved['all_long_gaps_same_png'] and len(saved['gaps_over_80ms']) == 8
    assert saved['registered_limit_s'] == .08 and saved['maximum_motion_gap_s'] < .035
    source = Path(natural_capture.__file__)
    assert file_sha256(source) == saved['source_sha256']
    original = source.read_text('utf-8')
    old = "'static_image_stale_s':.040,'input_image_quiet_s':.030,"
    assert original.count(old) == 1
    proposed = original.replace(old,
        "'static_image_stale_s':.010,'input_image_quiet_s':.030,\n"
        "          'static_pixel_cadence_policy':'earlier_real_png_v2',", 1)
    out.mkdir()
    path = out/'natural-capture-proposed.py'
    path.write_text(proposed, encoding='utf-8')
    (out/'natural-capture.patch').write_text(''.join(difflib.unified_diff(
        original.splitlines(True), proposed.splitlines(True),
        fromfile='kiroclops/web_local/natural_capture.py',
        tofile='kiroclops/web_local/natural_capture.py')), encoding='utf-8')
    spec = importlib.util.spec_from_file_location('kiroclops.web_local._static_proposal', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prior = deepcopy(natural_capture.POLICY)
    log = io.StringIO()
    with patch.object(natural_capture, 'POLICY', module.POLICY):
        tests = unittest.defaultTestLoader.loadTestsFromTestCase(StaticSamplingTests)
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(tests)
    assert natural_capture.POLICY == prior and file_sha256(source) == saved['source_sha256']
    (out/'tests.txt').write_text(log.getvalue(), encoding='utf-8')
    write_json(out/'result.json', {'status': 'staged_synthetic_pass_native_pending' if result.wasSuccessful() else 'fail',
        'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'production_source_sha256': file_sha256(source), 'proposed_source_sha256': file_sha256(path),
        'source_diagnosis_sha256': file_sha256(evidence), 'builder_sha256': file_sha256(__file__),
        'policy': module.POLICY, 'production_modified': False, 'acceptance_limits_changed': False,
        'native_validation': 'pending_until_frozen_cohort_and_heavy_review_preparation_end',
        'scope': 'Request real PNGs earlier during stale/static delivery. No frame, timestamp, verdict or trial substitution.'})
    print({'synthetic_tests': result.testsRun, 'pass': result.wasSuccessful(),
           'native_validation': 'pending', 'production_modified': False})
    assert result.wasSuccessful(), log.getvalue()


if __name__ == '__main__':
    main()
