"""Actual-time reference support: no synthetic geometry in the time-only gate."""
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.local_engine.web_reference import (
    reference_time_coverage, require_reference_time_coverage, check_reference_comparison_coverage)
from kiroclops.visual.schema import VisualError, read_json


def recording(times, commands):
    events = [{'action': 'click', 'selector': selector, 'time_s': time} for time, selector in commands]
    scenario = {'id': 'temporal-only-unit', 'events': events}
    return {'frames': [{'index': i, 'time_s': t} for i, t in enumerate(times)],
        'inputs': {'provenance': 'recorder_verified', 'events': events}, 'metadata': {'scenario': scenario}}


class ReferenceTemporalCoverage(unittest.TestCase):
    def test_dense_real_timeline_passes_without_coordinates(self):
        manifest = recording([i / 100 for i in range(151)], [(.4, '#open-panel'), (1., '#close-panel')])
        result = require_reference_time_coverage(manifest)
        self.assertEqual(result['status'], 'pass')
        self.assertFalse(result['pixel_geometry_or_lifecycle_checked'])
        self.assertFalse(result['behavior_transfer_success_claimed'])

    def test_preinput_idle_gap_does_not_invent_missing_motion(self):
        times = [0., .25, .38] + [i / 100 for i in range(39, 101)]
        self.assertEqual(require_reference_time_coverage(recording(times, [(.4, '#open-panel')]))['status'], 'pass')

    def test_gap_crossing_short_input_window_rejects_perfect_target_upper_bound(self):
        times = [0., .32025694847106934, .4471321105957031] + [.4471321105957031 + i / 100 for i in range(1, 81)]
        manifest = recording(times, [(.4316999912261963, '#open-panel'), (.644899845123291, '#close-panel')])
        result = reference_time_coverage(manifest)
        self.assertLess(result['windows'][0]['reference_time_coverage_upper_bound'], .927617)
        with self.assertRaises(VisualError) as raised:
            require_reference_time_coverage(manifest)
        self.assertEqual(raised.exception.code, 'reference_window_coverage_failure')

    def test_postcommand_frame_cannot_repair_interrupted_tail(self):
        # All frames are valid timestamps, but15ms of a200ms response cannot be
        # interpolated using the frame rendered after its terminating command.
        times = [i / 100 for i in range(40)] + [.4, .44, .48, .52, .56, .585, .61, .65, .7, .75, .8, .85, .9, .95, 1.]
        result = reference_time_coverage(recording(times, [(.4, '#open-panel'), (.6, '#close-panel')]))
        self.assertAlmostEqual(result['windows'][0]['reference_time_coverage_upper_bound'], .925)
        self.assertFalse(result['windows'][0]['pass'])

    def test_exact120ms_gap_is_allowed_but_larger_gap_is_not(self):
        times = [0., .12, .24, .36, .48, .6, .72, .84, .96]
        # Use exact binary-representable gaps to test the bound without decimal
        # rounding deciding the result; the greater-than bound stays unchanged.
        result = reference_time_coverage(recording([0., .125, .25, .375, .5], [(0., '#open-panel')]))
        self.assertEqual(result['windows'][0]['covered_time_s'], 0.)
        result = reference_time_coverage(recording([0., .12], [(0., '#open-panel')]))
        self.assertEqual(result['windows'][0]['reference_time_coverage_upper_bound'], 1.)

    def test_unverified_or_wrong_action_receipt_rejected(self):
        manifest = recording([0., .01, .02], [(0., '#open-panel')])
        manifest['inputs']['provenance'] = 'unknown'
        with self.assertRaises(VisualError): reference_time_coverage(manifest)
        manifest['inputs']['provenance'] = 'recorder_verified'
        with self.assertRaises(VisualError):
            reference_time_coverage(manifest, expected_scenario={'events': []})

    def test_observation_helper_labels_no_transfer_and_keeps_exact_comparator_result(self):
        record = {'scenario': {'id': 'unit'}, 'observations': {'package_sha256': 'a' * 64}, 'functional': {'status': 'pass'}}
        result = {'pass': False, 'pixel_comparison': {'object_windows': [{'reference_id': 'p', 'window_id': 'motion-0',
            'valid_time_ratio': .85, 'gates': {'coverage': False}}]}, 'dynamics': [], 'relative_timing': []}
        with patch('kiroclops.local_engine.evaluation._compare_web', return_value=result) as current:
            checked = check_reference_comparison_coverage(record)
        current.assert_called_once_with(record, record, record, improvement=False)
        self.assertEqual(checked['status'], 'fail')
        self.assertFalse(checked['behavior_transfer_success_claimed'])
        self.assertFalse(checked['new_pixel_measurement_executed'])

    def test_preserved_actual_reverse_record_is_unqualified(self):
        path = Path(__file__).resolve().parents[1] / 'artifacts/phase3a/shared-web-references-01/heldout/panel/reverse-w1280-r1/package/manifest.json'
        if not path.exists(): self.skipTest('Historical real reference evidence is not distributed with unit fixtures')
        manifest = read_json(path)
        result = reference_time_coverage(manifest)
        self.assertAlmostEqual(result['windows'][0]['reference_time_coverage_upper_bound'], .8531242137046046)
        with self.assertRaises(VisualError): require_reference_time_coverage(manifest)

    def test_actual_observations_fail_self_coverage_without_new_pixel_measurement(self):
        root = Path(__file__).resolve().parents[1]
        directory = root / 'artifacts/phase3a/shared-web-reference-observability-01/panel/validation/reverse-w1280-r1'
        if not (directory / 'analysis.json').exists():
            self.skipTest('Historical measured reference observations are not distributed with unit fixtures')
        analysis = read_json(directory / 'analysis.json')
        observations = analysis['observations']
        record = {'scenario': {'id': 'reverse-w1280-r1'}, 'observations': observations,
            'selection': read_json(directory / 'selection.json'),
            'measurement_status': 'ok', 'functional': {'status': 'pass'}}
        result = check_reference_comparison_coverage(record)
        self.assertEqual(result['status'], 'fail')
        first = [w for w in result['coverage_windows'] if w['window_id'] == 'motion-0']
        self.assertEqual(len(first), 3)
        self.assertTrue(all(not w['gates']['coverage'] for w in first))
        self.assertAlmostEqual(first[0]['valid_time_ratio'], .8531242137046046)
        self.assertFalse(result['behavior_transfer_success_claimed'])


if __name__ == '__main__':
    unittest.main()
