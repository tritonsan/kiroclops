"""Natural panel captures must compare observable support, without hiding gaps."""
from copy import deepcopy
import unittest

from tests import test_observed_response as response_fixtures
from tests.test_local_evaluation import record
from kiroclops.visual.comparison import compare_observations, window_timing
from kiroclops.local_engine.evaluation import _contract
from kiroclops.local_engine.web_reference import reference_time_coverage


class PanelObservedBoundaryTests(unittest.TestCase):
    def sequences(self):
        helper = response_fixtures.ObservedResponseTests()
        config = helper.cfg()
        config['sampling_policy'] = 'observed_pre_input_v1'
        return helper.sequence(), helper.sequence(.008), config

    def test_independent_camera_phase_preserves_input_origin_and_visible_end(self):
        a, b, cfg = self.sequences()
        timing = window_timing(a, b, cfg, cfg['windows'][0])
        self.assertEqual(timing['time_window_s'][1], min(timing['last_pre_input_sample_s']))
        self.assertFalse(timing['duration_rescaled'])
        self.assertTrue(compare_observations(a, b, cfg)['success'])

    def test_missing_interior_observations_still_fail_95_percent_gate(self):
        a, b, cfg = self.sequences()
        for frame in b['objects'][0]['frames']:
            if .15 < frame['time_s'] < .24:
                frame['valid'] = False
        result = compare_observations(a, b, cfg)
        self.assertFalse(result['success'])
        self.assertFalse(result['object_windows'][0]['gates']['coverage'])

    def test_wrong_trajectory_still_fails(self):
        a, b, cfg = self.sequences()
        for frame in b['objects'][0]['frames']:
            frame['x'] += 50
        self.assertFalse(compare_observations(a, b, cfg)['success'])

    def test_contract_changes_only_boundary_policy_for_natural_panel(self):
        ref = record()
        legacy = _contract(ref, ref)
        ref['scenario'].update(domain='panel', frame_driver='natural_browser_raf_v1')
        fixed = _contract(ref, ref)
        self.assertEqual(fixed.pop('sampling_policy'), 'observed_pre_input_v1')
        self.assertEqual(legacy, fixed)

    def test_reference_gate_and_comparator_share_last_observed_boundary(self):
        events = [{'time_s': .2, 'action': 'click', 'selector': '#open-panel'},
                  {'time_s': .4, 'action': 'click', 'selector': '#close-panel'}]
        manifest = {'inputs': {'provenance': 'recorder_verified', 'events': events},
                    'frames': [{'time_s': t} for t in (0, .08, .16, .24, .32, .37, .41, .49, .57, .65)],
                    'metadata': {'scenario': {'domain': 'panel', 'events': deepcopy(events)}}}
        self.assertEqual(reference_time_coverage(manifest)['status'], 'fail')
        manifest['metadata']['scenario']['frame_driver'] = 'natural_browser_raf_v1'
        fixed = reference_time_coverage(manifest)
        self.assertEqual(fixed['status'], 'pass')
        self.assertEqual(fixed['windows'][0]['end_s'], .37)
        self.assertEqual(fixed['windows'][0]['next_input_s'], .4)
        manifest['frames'] = [f for f in manifest['frames'] if f['time_s'] not in (.24, .32)]
        self.assertEqual(reference_time_coverage(manifest)['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
