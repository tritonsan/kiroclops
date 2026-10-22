"""V5 feedback: per-window calibration windows reach the author, inspections stay below Kiro's offload limit."""
import json
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine.job_feedback import (compact_inspection, compact_job_feedback, INSPECT_MAX_CHARS,
                                                 MAX_RESPONSE_CHARS)
from kiroclops.visual.schema import VisualError, read_json, write_json


def size(value):
    return len(json.dumps(value, ensure_ascii=True, indent=2))


def suite_comparison(case_id, windows):
    """A suite-shaped comparison: production gates plus measured windows (current and starting-version error)."""
    gates, measured = [], []
    for obj, window, after, before, limit in windows:
        gates.append({'object_id': obj, 'window_id': window, 'segment_index': 0, 'after_rmse': after,
                      'before_rmse': before, 'pass': after <= limit, 'classification': 'x'})
        measured.append({'reference_id': obj, 'window_id': window, 'segment_index': 0, 'position_rmse': after,
                         'size_rmse': 0., 'endpoint_error': 0., 'scale_px': 25., 'relative_to_reference': 'handle',
                         'time_window_s': [0., .9], 'limits': {'rmse_limit': limit, 'endpoint_limit': .03,
                                                               'size_rmse_limit': .03},
                         'gates': {'position': after <= limit, 'size': True, 'endpoint': True, 'coverage': True}})
    return {'case_id': case_id, 'role': 'calibration', 'purpose': 'primary', 'pass': all(g['pass'] for g in gates),
            'score': .02, 'per_window_gates': gates, 'pixel_comparison': {'object_windows': measured},
            'dynamics': [], 'filler': ['é' * 200] * 30}


class SuiteWindowFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='window feedback ')
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / 'target'

    def public(self, comparisons, **extra):
        return dict(job_id='e' * 32, operation='evaluate', status='succeeded', candidate_id='candidate-01',
                    source_sha256='f' * 64, result=dict(eligible=True, score=.03, comparisons=comparisons, **extra))

    def test_failing_regressed_and_near_limit_windows_are_listed_with_their_case(self):
        rows = [suite_comparison('cal-interrupt-w768-r1', [('star', 'motion-1', .09, .07, .0844),
                                                           ('handle', 'motion-0', .0482, .0111, .05),
                                                           ('bookmark', 'motion-0', .01, .0098, .0863)])]
        response = compact_job_feedback(self.public(rows), self.target)
        item = response['result']['comparisons'][0]
        self.assertEqual('cal-interrupt-w768-r1', item['case_id'])
        self.assertEqual(['star', 'handle'], [w['object'] for w in item['windows']])
        star, handle = item['windows']
        self.assertFalse(star['pass'])
        self.assertEqual(['position'], star['failed_gates'])
        self.assertTrue(handle['worse_than_before'])
        self.assertTrue(handle['near_limit'])
        self.assertEqual(.0111, handle['position_error_before_change'])
        self.assertEqual((3, 1), (item['windows_total'], item['windows_not_listed']))
        # The star window fails and also got worse than before (.09 against .07).
        self.assertEqual({'failing': 1, 'worse_than_before': 2, 'near_limit': 2},
                         {k: v for k, v in response['result']['window_feedback'].items() if k != 'meaning'})
        self.assertNotIn('object_windows', item)

    def test_failing_windows_survive_the_size_cascade_longest(self):
        rows = [suite_comparison(f'cal-{i:02}', [('star', 'motion-1', .09, .07, .0844)]
                                 + [(f'obj{j}', 'motion-0', .03, .01, .05) for j in range(8)]) for i in range(30)]
        media = [{'scenario_id': f'case-{i}', 'package': 'C:/' + 'p' * 120} for i in range(40)]
        response = compact_job_feedback(self.public(rows, candidate_media=media, reference_media=media), self.target)
        self.assertLessEqual(size(response), MAX_RESPONSE_CHARS)
        result = response['result']
        # V6: the candidate's frames stay reachable through one compact root entry.
        self.assertEqual(result['candidate_media']['count'], 40)
        self.assertEqual(result['candidate_media']['root'], 'C:/')
        self.assertEqual(len(result['candidate_media']['scenario_ids']), 40)
        listed = [w for item in result['comparisons'] for w in item['windows']]
        self.assertEqual(30, sum(w['object'] == 'star' for w in listed))
        self.assertTrue(all(item['windows_total'] == 9 for item in result['comparisons']))
        saved = read_json(Path(result['full_feedback']['path']))
        self.assertEqual(rows, saved['result']['comparisons'])

    def test_the_bounded_response_stays_below_kiros_offload_limit(self):
        self.assertLess(MAX_RESPONSE_CHARS, 30000)
        self.assertLess(INSPECT_MAX_CHARS, 30000)


class InspectionCompactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='inspection ')
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / 'target'

    def scenario(self, i):
        name = f'realistic-suite-panel-calibration-case-{i:02}'
        return {'id': name, 'case_id': name, 'recipe': 'interrupt', 'split': 'calibration', 'repeat': 1,
                'viewport': [768, 640], 'duration_s': 3., 'events': [{'action': 'click', 'selector': '#open-panel',
                                                                       'time_s': .4}],
                'timing_limits': {'clock_uncertainty_s': .02}, 'frame_driver': 'natural_browser_raf_v1',
                'suite_version': 'reference-suite-web-v2', 'v4_origin': 'v3-calibration', 'padding': 'x' * 700}

    def inspection(self, count):
        scenarios = [self.scenario(i) for i in range(count)]
        return {'session_id': 's', 'target': str(self.target), 'experiment': {'calibration': scenarios},
                'calibration_planning': {'cases': [{'scenario_id': s['id'], 'scenario': s, 'behavior_tags': ['panel'],
                                                    'status': 'not_measured'} for s in scenarios],
                                         'next_case': {'scenario_id': scenarios[0]['id'], 'scenario': scenarios[0]}}}

    def test_small_inspections_are_unchanged_and_not_exported(self):
        value = self.inspection(2)
        self.assertEqual(value, compact_inspection(value, self.target))
        self.assertFalse(self.target.exists())

    def test_long_inspection_is_shortened_below_the_offload_limit_and_kept_whole(self):
        value = self.inspection(24)
        self.assertGreater(size(value), INSPECT_MAX_CHARS)
        compact = compact_inspection(value, self.target)
        self.assertLessEqual(size(compact), INSPECT_MAX_CHARS)
        self.assertTrue(compact['inspection_is_summary'])
        self.assertEqual(value, read_json(Path(compact['full_inspection']['path'])))
        first = compact['experiment']['calibration'][0]
        self.assertEqual({'id', 'recipe', 'split', 'repeat', 'viewport', 'duration_s', 'events'}, set(first))
        self.assertEqual(value['experiment']['calibration'][0]['events'], first['events'])
        self.assertEqual(24, len(compact['calibration_planning']['cases']))

    def test_very_long_inspections_fall_back_to_case_identities(self):
        value = self.inspection(80)
        compact = compact_inspection(value, self.target)
        self.assertLessEqual(size(compact), INSPECT_MAX_CHARS)
        self.assertEqual([s['id'] for s in value['experiment']['calibration']], compact['experiment']['calibration'])
        self.assertEqual(80, compact['calibration_planning']['case_count'])

    def test_errors_and_changed_exports(self):
        error = {'status': 'error', 'error': {'code': 'x', 'message': 'y' * 40000}}
        self.assertEqual(error, compact_inspection(error, self.target))
        value = self.inspection(40)
        path = Path(compact_inspection(value, self.target)['full_inspection']['path'])
        write_json(path, {'tampered': True})
        with self.assertRaises(VisualError):
            compact_inspection(value, self.target)


if __name__ == '__main__':
    unittest.main()
