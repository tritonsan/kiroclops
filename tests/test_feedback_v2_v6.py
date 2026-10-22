"""V6 plan WP3.1: signed offset, lag and trajectory per window, and the change table against the previous candidate."""
import json
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine.job_feedback import MAX_RESPONSE_CHARS, change_vs_previous, compact_job_feedback
from kiroclops.local_engine.session import Session
from kiroclops.visual import comparison
from kiroclops.visual.schema import write_json

LIMITS = {'rmse_limit': .5, 'size_rmse_limit': .1, 'endpoint_limit': .5}


def track(delay_s=0., speed=300.):
    times = [i/60 for i in range(61)]
    frames = [{'index': i, 'valid': True, 'x': 100+speed*max(0., t-delay_s), 'y': 50., 'width': 20., 'height': 20.}
              for i, t in enumerate(times)]
    return times, frames


def window(obj, name, error, before, limit, *, lag=.05, offset=(-.6, 0.)):
    trajectory = {'t_s': [round(i/15, 3) for i in range(16)], 'reference': [[100.+i, 50.] for i in range(16)],
                  'candidate': [[99.+i, 50.] for i in range(16)]}
    gate = {'object_id': obj, 'window_id': name, 'segment_index': 0, 'after_rmse': error, 'before_rmse': before,
            'pass': error <= limit, 'classification': 'x'}
    measured = {'reference_id': obj, 'window_id': name, 'segment_index': 0, 'position_rmse': error, 'size_rmse': 0.,
                'endpoint_error': 0., 'scale_px': 25., 'relative_to_reference': None, 'time_window_s': [0., .9],
                'limits': {'rmse_limit': limit, 'endpoint_limit': .03, 'size_rmse_limit': .03},
                'gates': {'position': error <= limit, 'size': True, 'endpoint': True, 'coverage': True},
                'mean_offset': list(offset), 'lag_s': lag, 'lag_position_rmse': .01, 'trajectory': trajectory}
    return gate, measured


def case(case_id, windows, filler=0):
    pairs = [window(*w) for w in windows]
    return {'case_id': case_id, 'role': 'calibration', 'purpose': 'primary', 'pass': all(g['pass'] for g, _ in pairs),
            'score': .02, 'per_window_gates': [g for g, _ in pairs],
            'pixel_comparison': {'object_windows': [m for _, m in pairs]}, 'dynamics': [], 'filler': ['é'*200]*filler}


def public(comparisons, candidate='candidate-02'):
    return dict(job_id='e'*32, operation='evaluate', status='succeeded', candidate_id=candidate, source_sha256='f'*64,
                result=dict(eligible=True, score=.03, comparisons=comparisons))


class WindowDiagnosticsTests(unittest.TestCase):
    def test_delayed_motion_reports_lag_signed_offset_and_trajectory_without_moving_a_gate(self):
        rt, rf = track()
        ct, cf = track(delay_s=.05)
        row = comparison._compare_window(rt, rf, ct, cf, .1, .9, .1, 25., LIMITS, .9)
        self.assertAlmostEqual(.6, row['position_rmse'], places=6)
        self.assertAlmostEqual(.6, row['endpoint_error'], places=6)
        self.assertEqual({'coverage': True, 'position': False, 'size': True, 'endpoint': False}, row['gates'])
        self.assertAlmostEqual(-.6, row['mean_offset'][0], places=6)
        self.assertAlmostEqual(0., row['mean_offset'][1], places=9)
        self.assertAlmostEqual(.05, row['lag_s'], places=6)
        self.assertLess(row['lag_position_rmse'], 1e-6)
        self.assertAlmostEqual(.6, row['zero_lag_position_rmse'], places=6)
        trajectory = row['trajectory']
        self.assertEqual(16, len(trajectory['t_s']))
        self.assertEqual([130., 50.], trajectory['reference'][0])
        self.assertEqual([115., 50.], trajectory['candidate'][0])

    def test_identical_motion_has_zero_lag_and_offset(self):
        rt, rf = track()
        row = comparison._compare_window(rt, rf, rt, rf, .1, .9, .1, 25., LIMITS, .9)
        self.assertEqual((0., [0., 0.]), (row['lag_s'], row['mean_offset']))
        self.assertTrue(row['pass'])


class FeedbackRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='feedback v2 ')
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name)/'target'

    def test_listed_windows_carry_offset_lag_and_trajectory(self):
        response = compact_job_feedback(public([case('cal-01', [('star', 'motion-1', .09, .07, .0844)], filler=120)]),
                                        self.target)
        row = response['result']['comparisons'][0]['windows'][0]
        self.assertEqual([-.6, 0.], row['mean_offset'])
        self.assertEqual(.05, row['lag_s'])
        self.assertEqual(16, len(row['trajectory']['candidate']))
        self.assertIn('lag_s', response['result']['window_feedback']['meaning'])

    def test_change_table_counts_and_lists_moved_windows(self):
        before = public([case('cal-01', [('star', 'm1', .09, .07, .1), ('handle', 'm0', .04, .01, .05),
                                         ('bookmark', 'm0', .02, .02, .05)])], 'candidate-01')
        now = public([case('cal-01', [('star', 'm1', .05, .07, .1), ('handle', 'm0', .06, .01, .05),
                                      ('bookmark', 'm0', .0205, .02, .05)])])
        table = change_vs_previous(now['result'], {'candidate_id': 'candidate-01', 'result': before['result']})
        self.assertEqual(('candidate-01', 3, 1, 1, 1), (table['previous_candidate_id'], table['windows_compared'],
                                                       table['worsened'], table['improved'], table['unchanged']))
        self.assertEqual(['handle', 'star'], [r['object'] for r in table['rows']])
        self.assertAlmostEqual(.02, table['rows'][0]['change'])
        small = compact_job_feedback(now, self.target, previous={'candidate_id': 'candidate-01', 'result': before['result']})
        self.assertEqual(table, small['change_vs_previous_candidate'])
        self.assertNotIn('change_vs_previous_candidate', compact_job_feedback(now, self.target))

    def test_large_feedback_stays_bounded_with_trajectories_and_changes(self):
        rows = [case(f'cal-{i:02}', [('star', 'm1', .09, .07, .0844)]+[(f'o{j}', 'm0', .03+j/1000, .01, .05) for j in range(8)],
                     filler=10) for i in range(30)]
        older = [case(f'cal-{i:02}', [('star', 'm1', .07, .07, .0844)]+[(f'o{j}', 'm0', .02, .01, .05) for j in range(8)])
                 for i in range(30)]
        response = compact_job_feedback(public(rows), self.target, previous={'candidate_id': 'candidate-01',
                                                                            'result': public(older)['result']})
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=True, indent=2)), MAX_RESPONSE_CHARS)
        table = response['change_vs_previous_candidate']
        self.assertEqual(270, table['windows_compared'])
        self.assertEqual(table['worsened']+table['improved'], len(table['rows'])+table['rows_not_listed'])


class PreviousCandidateTests(unittest.TestCase):
    class FakeSession:
        _previous_candidate_result = Session._previous_candidate_result

        def __init__(self, directory):
            self.directory = directory

        def _job_dir(self, job_id):
            return self.directory/'jobs'/job_id

    def test_nearest_earlier_succeeded_candidate_is_used(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.FakeSession(Path(temp))
            for job, score in (('a'*32, 1), ('c'*32, 3)):
                write_json(session._job_dir(job)/'result.json', {'score': score})
            state = {'candidates': [{'candidate_id': 'candidate-01', 'job_id': 'a'*32, 'status': 'succeeded'},
                                    {'candidate_id': 'candidate-02', 'job_id': 'b'*32, 'status': 'failed'},
                                    {'candidate_id': 'candidate-03', 'job_id': 'c'*32, 'status': 'succeeded'}]}
            job = {'operation': 'evaluate', 'status': 'succeeded', 'candidate_id': 'candidate-03'}
            self.assertEqual({'candidate_id': 'candidate-01', 'result': {'score': 1}},
                             session._previous_candidate_result(state, job))
            self.assertIsNone(session._previous_candidate_result(state, {**job, 'candidate_id': 'candidate-01'}))
            for other in ({'operation': 'finalize'}, {'status': 'failed'}, {'candidate_id': None}):
                self.assertIsNone(session._previous_candidate_result(state, {**job, **other}))


if __name__ == '__main__':
    unittest.main()
