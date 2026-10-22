"""Reference/candidate/difference strips and error plots for one object window (V6 plan WP3.2)."""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from kiroclops.local_engine.window_inspection import build_window_inspection
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, read_json
from kiroclops.visual.tracking import analyze_package


class WindowInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'window'
        self.root.mkdir()
        self.patch = np.random.default_rng(3).integers(30, 240, (14, 16, 3), dtype=np.uint8)

    def record(self, name, xs, origin=0.):
        directory = self.root/name
        directory.mkdir()
        paths = []
        for i, x in enumerate(xs):
            frame = np.zeros((120, 200, 3), np.uint8)
            frame[40:54, x:x+16] = self.patch
            path = directory/f'raw-{i}.png'
            path.write_bytes(cv2.imencode('.png', frame)[1].tobytes())
            paths.append(path)
        package = directory/'package'
        create_package(package, paths, [origin+i*.04 for i in range(len(xs))])
        selection = {'schema_version': 1, 'objects': [{'id': 'card', 'roi': [xs[0], 40, 16, 14], 'anchor': [8, 7], 'method': 'template',
                                                        'search_radius': 40}]}
        observations = analyze_package(package, selection)
        self.assertEqual(observations['status'], 'ok')
        return {'scenario': {'id': 'case-a', 'case_id': 'case-a'}, 'package': str(package), 'observations': observations,
                'selection': {**selection, 'input_origin_s': origin}}

    def record_with_anchor(self, name, panel_xs, card_xs):
        # A 'panel' anchor and a 'card' 40 px below it, tracked in the same frames.
        directory = self.root/name
        directory.mkdir()
        card = np.random.default_rng(5).integers(30, 240, (14, 16, 3), dtype=np.uint8)
        paths = []
        for i, (px, cx) in enumerate(zip(panel_xs, card_xs)):
            frame = np.zeros((120, 200, 3), np.uint8)
            frame[20:34, px:px+16] = self.patch
            frame[60:74, cx:cx+16] = card
            path = directory/f'raw-{i}.png'
            path.write_bytes(cv2.imencode('.png', frame)[1].tobytes())
            paths.append(path)
        package = directory/'package'
        create_package(package, paths, [i*.04 for i in range(len(paths))])
        selection = {'schema_version': 1, 'objects': [
            {'id': 'panel', 'roi': [panel_xs[0], 20, 16, 14], 'anchor': [8, 7], 'method': 'template', 'search_radius': 40},
            {'id': 'card', 'roi': [card_xs[0], 60, 16, 14], 'anchor': [8, 7], 'method': 'template', 'search_radius': 40}]}
        observations = analyze_package(package, selection)
        self.assertEqual(observations['status'], 'ok')
        return {'scenario': {'id': 'case-b', 'case_id': 'case-b'}, 'package': str(package), 'observations': observations,
                'selection': {**selection, 'input_origin_s': 0.}}

    def test_window_is_aligned_to_its_own_input(self):
        # Both recordings start with the same first input; the window's own input
        # comes at 0.40 s in the reference and at 0.48 s in the candidate, and each
        # moves only after it. The comparison aligned the window on that input.
        reference = self.record('r3', [20]*11+[24, 28, 32, 36, 40, 44, 48])
        candidate = self.record('c3', [20]*13+[24, 28, 32, 36, 40, 44, 48])
        timing = {'reference_origin_s': .4, 'candidate_origin_s': .48, 'time_window_s': [0., .28],
                  'next_input_after_origin_s': [.3, .3]}
        comparison = {'case_id': 'case-a', 'pixel_comparison': {'object_windows': [
            {'reference_id': 'card', 'window_id': 'motion-1', 'segment_index': 0, 'time_window_s': [0., .28],
             'scale_px': 100., 'limits': {'rmse_limit': .02}, 'input_timing': timing, 'position_rmse': .001}]}}
        summary = build_window_inspection(reference, candidate, comparison, 'card', 'motion-1', self.root/'o4')
        self.assertEqual(summary['alignment'], {'source': 'window_input_timing', 'reference_origin_s': .4, 'candidate_origin_s': .48})
        self.assertAlmostEqual(summary['scored_rmse_px'], .1)
        self.assertAlmostEqual(summary['rmse_px'], 0., delta=1e-6)
        self.assertAlmostEqual(summary['best_time_shift']['lag_s'], 0., delta=1e-9)
        self.assertGreater(summary['frames_compared'], 5)
        # Samples come from the motion after each recording's own input, not from the first input.
        self.assertTrue(all(s['reference_frame'] >= 10 and s['candidate_frame'] >= 12 for s in summary['samples']))

    def test_anchor_relative_window_is_plotted_as_scored(self):
        # Panel and card move together; the candidate's panel travels faster, so the
        # card's image position differs while its offset from the panel does not.
        reference = self.record_with_anchor('r5', [20+4*i for i in range(10)], [20+4*i for i in range(10)])
        candidate = self.record_with_anchor('c5', [20+6*i for i in range(10)], [20+6*i for i in range(10)])
        window = {'reference_id': 'card', 'window_id': 'motion-0', 'segment_index': 0, 'time_window_s': [0., .36],
                  'scale_px': 100., 'limits': {'rmse_limit': .02},
                  'input_timing': {'reference_origin_s': 0., 'candidate_origin_s': 0., 'time_window_s': [0., .36],
                                   'next_input_after_origin_s': [None, None]}}
        relative = {**window, 'relative_to_reference': 'panel', 'relative_to_candidate': 'panel'}
        summary = build_window_inspection(reference, candidate, {'pixel_comparison': {'object_windows': [relative]}},
                                          'card', 'motion-0', self.root/'o5')
        self.assertEqual(summary['coordinate_space'], 'relative_to_anchor')
        self.assertEqual(summary['relative_to_reference'], 'panel')
        self.assertAlmostEqual(summary['rmse_px'], 0., delta=1e-6)
        self.assertTrue((self.root/'o5'/'strip.png').is_file())
        absolute = build_window_inspection(reference, candidate, {'pixel_comparison': {'object_windows': [window]}},
                                           'card', 'motion-0', self.root/'o6')
        self.assertEqual(absolute['coordinate_space'], 'image')
        self.assertGreater(absolute['rmse_px'], 5.)
        missing = {**relative, 'relative_to_reference': 'ghost', 'relative_to_candidate': 'ghost'}
        with self.assertRaisesRegex(VisualError, 'Anchor ghost'):
            build_window_inspection(reference, candidate, {'pixel_comparison': {'object_windows': [missing]}},
                                    'card', 'motion-0', self.root/'o7')

    def test_strip_plot_and_summary_are_written(self):
        reference = self.record('reference', [20, 24, 28, 32, 36, 40, 44, 48, 52, 56])
        candidate = self.record('candidate', [20, 20, 24, 28, 32, 36, 40, 44, 48, 52])  # one frame late
        comparison = {'case_id': 'case-a', 'pixel_comparison': {'object_windows': [
            {'reference_id': 'card', 'window_id': 'motion-0', 'segment_index': 0, 'time_window_s': [0., .36],
             'scale_px': 100., 'limits': {'rmse_limit': .02}}]}}
        out = self.root/'out'
        summary = build_window_inspection(reference, candidate, comparison, 'card', 'motion-0', out, case_id='case-a', job_id='j1')
        self.assertTrue((out/'strip.png').is_file() and (out/'error.png').is_file())
        self.assertEqual(read_json(out/'summary.json')['rmse_px'], summary['rmse_px'])
        self.assertEqual(summary['window_id'], 'motion-0')
        self.assertEqual(summary['window_limit_px'], 2.)
        self.assertGreater(summary['frames_compared'], 5)
        # The candidate trails the reference by one 40 ms frame: dx is negative and the best shift says so.
        self.assertLess(summary['mean_dx_px'], -2.)
        self.assertAlmostEqual(summary['best_time_shift']['lag_s'], -.04, delta=.01)
        self.assertLess(summary['best_time_shift']['rmse_px_at_lag'], summary['rmse_px'])
        strip = cv2.imread(str(out/'strip.png'))
        self.assertEqual(strip.shape[2], 3)
        self.assertEqual(len(summary['samples']), 6)
        self.assertIn('strip.png', summary['reading'])
        self.assertEqual(summary['strip_png'], str(out/'strip.png'))

    def test_unknown_window_and_object_are_refused(self):
        reference = self.record('r2', [20, 24, 28, 32])
        candidate = self.record('c2', [20, 24, 28, 32])
        comparison = {'case_id': 'case-a', 'pixel_comparison': {'object_windows': [
            {'reference_id': 'card', 'window_id': 'motion-0', 'time_window_s': [0., .12]}]}}
        with self.assertRaisesRegex(VisualError, 'Window'):
            build_window_inspection(reference, candidate, comparison, 'card', 'motion-9', self.root/'o1')
        with self.assertRaisesRegex(VisualError, 'not measured'):
            build_window_inspection(reference, candidate, comparison, 'ghost', None, self.root/'o2')
        # Without a window id the whole overlapping range is inspected.
        summary = build_window_inspection(reference, candidate, None, 'card', None, self.root/'o3')
        self.assertIsNone(summary['window_id'])
        self.assertAlmostEqual(summary['rmse_px'], 0., delta=1e-6)


if __name__ == '__main__':
    unittest.main()
