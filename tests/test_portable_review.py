"""Independent observation contracts on small synthetic PNGs; no game or model."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from kiroclops.integration.storage import read_json, write_json, sha256, IntegrationError
from kiroclops.portable import review


class ReviewContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        (self.run / 'frames').mkdir(parents=True)
        for i in range(240):
            image = np.zeros((8, 8, 3), np.uint8)
            image[0, 0] = (i, 255-i, i//2)
            self.assertTrue(cv2.imwrite(str(self.run / 'frames' / f'{i:06d}.png'), image))
        self.analysis = self.run / 'analysis.json'
        self.report = {'run_dir': str(self.run), 'body_height_px': 20,
            'frames': [{'x': 10 + i/10, 'y': 20, 'valid': True, 'mirrored': i >= 50} for i in range(240)],
            'phases': [{'name': 'coast', 'start': 30, 'end': 50}, {'name': 'reversal', 'start': 50, 'end': 70}],
            'events': {'takeoff_frame': 10, 'apex_frame': 25, 'landing_frame': 40}}
        write_json(self.analysis, self.report)

    def points(self, indices=range(20), run=None):
        run = run or self.run
        return [{'run': str(run), 'source': 'assistant_visual', 'frame_index': i,
                 'frame_sha256': sha256(run / 'frames' / f'{i:06d}.png'),
                 'x': self.report['frames'][i]['x'], 'y': self.report['frames'][i]['y']}
                for i in indices]

    def event(self):
        result = {'run': str(self.run), 'source': 'assistant_visual', **self.report['events']}
        needed = {i for value in self.report['events'].values() for i in range(value-2, value+3)}
        result['frame_hashes'] = {str(i): sha256(self.run / 'frames' / f'{i:06d}.png') for i in needed}
        return result

    def check(self, points=None, events=None, **kwargs):
        return review.validate_observations([self.analysis],
            {'points': self.points() if points is None else points, 'events': events or []}, **kwargs)

    def test_twenty_distinct_hash_bound_manual_points_pass(self):
        result = self.check()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['distinct_pixel_points'], 20)
        self.assertEqual(result['median_error_px'], 0)
        self.assertEqual(result['analysis_files'], {str(self.analysis.resolve()): sha256(self.analysis)})

    def test_png_mutation_breaks_old_observation(self):
        points = self.points()
        cv2.imwrite(str(self.run/'frames/000003.png'), np.ones((8, 8, 3), np.uint8))
        result = self.check(points)
        self.assertEqual(result['status'], 'fail')
        self.assertIn('Duplicate point or changed PNG', result['errors'])

    def test_duplicate_run_frame_is_an_error_even_when_quota_is_met(self):
        points = self.points(); points.append(deepcopy(points[0]))
        self.assertEqual(self.check(points)['status'], 'fail')

    def test_duplicate_pixels_cannot_become_new_controls_by_coordinate_jitter(self):
        import shutil
        other = self.root / 'other'
        shutil.copytree(self.run, other)
        second = deepcopy(self.report); second['run_dir'] = str(other)
        write_json(other/'analysis.json', second)
        points = self.points()
        for point in self.points(run=other):
            point['x'] += .01
            points.append(point)
        result = review.validate_observations([self.analysis, other/'analysis.json'], {'points': points}, minimum=21)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['distinct_pixel_points'], 20)

    def test_nonmanual_source_is_rejected(self):
        points = self.points(); points[0]['source'] = 'tracker'
        self.assertEqual(self.check(points)['status'], 'fail')

    def test_nonfinite_manual_coordinates_are_rejected(self):
        points = self.points(); points[0]['x'] = float('nan')
        self.assertEqual(self.check(points)['status'], 'fail')

    def test_invalid_tracking_frame_cannot_validate_measurement(self):
        self.report['frames'][0]['valid'] = False
        write_json(self.analysis, self.report)
        self.assertEqual(self.check()['status'], 'fail')

    def test_error_threshold_uses_body_scale_and_median(self):
        points = self.points()
        for point in points: point['x'] += 1.1
        self.assertEqual(self.check(points)['status'], 'fail')
        self.report['body_height_px'] = 100
        write_json(self.analysis, self.report)
        result = self.check(points)
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['tolerance_px'], 5)

    def test_mixed_body_scales_are_rejected(self):
        other = self.root/'other-analysis.json'
        report = deepcopy(self.report); report['body_height_px'] = 30
        write_json(other, report)
        result = review.validate_observations([self.analysis, other], {'points': self.points()})
        self.assertEqual(result['status'], 'fail')

    def test_phase_and_mirror_derived_categories_pass(self):
        points = self.points([*range(18), 35, 60])
        points[-2]['categories'] = ['brake']
        points[-1]['categories'] = ['reverse', 'flip']
        result = self.check(points, required_categories=('brake', 'reverse', 'flip'))
        self.assertEqual(result['status'], 'pass')

    def test_manual_category_cannot_invent_a_reversal(self):
        points = self.points(); points[0]['categories'] = ['reverse']
        self.assertEqual(self.check(points)['status'], 'fail')

    def test_required_category_missing_fails(self):
        self.assertEqual(self.check(required_categories=('reverse',))['status'], 'fail')

    def test_event_requirement_needs_hash_bound_manual_event(self):
        self.assertEqual(self.check(require_events=True)['status'], 'fail')
        result = self.check(events=[self.event()], require_events=True)
        self.assertEqual(result['status'], 'pass')

    def test_event_error_greater_than_two_frames_fails(self):
        event = self.event()
        self.report['events']['apex_frame'] += 3
        write_json(self.analysis, self.report)
        result = self.check(events=[event], require_events=True)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['events'][0]['errors_frames']['apex_frame'], 3)

    def test_event_without_neighbor_png_bindings_fails(self):
        event = self.event(); event['frame_hashes'].pop('8')
        self.assertEqual(self.check(events=[event], require_events=True)['status'], 'fail')

    def test_raw_contact_sheet_has_hash_bound_provenance_without_analysis(self):
        self.analysis.unlink()
        output = review.contact_sheet(self.run, [0, 1], self.root/'sheet.png', crop=(0, 0, 8, 8), scale=2, columns=2)
        receipt = read_json(output.with_suffix('.json'))
        self.assertEqual(receipt['image_sha256'], sha256(output))
        self.assertEqual(receipt['frames']['0'], sha256(self.run/'frames/000000.png'))
        self.assertEqual(receipt['crop'], [0, 0, 8, 8])
        self.assertIn('no analysis', receipt['method'])

    def test_contact_sheet_rejects_missing_or_outside_image(self):
        with self.assertRaises(IntegrationError):
            review.contact_sheet(self.run, [0], self.root/'bad.png', crop=(7, 7, 3, 3))
        with self.assertRaises(IntegrationError):
            review.contact_sheet(self.run, [999], self.root/'missing.png', crop=(0, 0, 8, 8))


if __name__ == '__main__':
    unittest.main()
