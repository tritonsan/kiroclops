"""V5 focus-ring blind qualification: new drags, and both endpoint accuracy and decision agreement are required."""
import json
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine import suite_web_v2
from scripts import phase3d_v3_svg_focus_ring as v3ring
from scripts import phase3d_v5_svg_focus_ring as ring

BOX = [400., 200., 540., 260.]


def write_sample(root, rows):
    root = Path(root)
    root.mkdir(parents=True)
    sealed = [{'sample': i, 'file': f'blind-{i:02d}.png', 'case_id': 'c', 'stratum': 'le1', 'node_box': BOX,
               'produced': [r['produced'][0], r['produced'][1]], 'produced_occluded': r.get('occluded', False),
               'produced_attached': r['attached'], 'raw': r['produced'], 'raw_attached': r['attached']}
              for i, r in enumerate(rows)]
    (root / 'sealed-method-coordinates.json').write_text(json.dumps(sealed), encoding='utf-8')
    (root / 'blind-manifest.json').write_text(json.dumps({'sealed_file_sha256': ring._sha(root / 'sealed-method-coordinates.json')}),
                                              encoding='utf-8')
    return root


def write_marks(path, marks, protocol=ring.MARK_PROTOCOL):
    value = {'kind': 'BlindFocusRingEndpointMarks', 'protocol': protocol, 'reviewer': 'test',
             'marks': {'%02d' % i: {'x': x, 'y': y, 'contact': contact} for i, (x, y, contact) in enumerate(marks)}}
    Path(path).write_text(json.dumps(value), encoding='utf-8')
    return path


class ScenarioTests(unittest.TestCase):
    def test_two_new_qualification_drags(self):
        suite_events = [s['events'] for split in ('calibration', 'known_regression', 'validation')
                        for s in suite_web_v2.scenarios('svg', split)]
        v3_events = v3ring.scenario()['events']
        destinations = set()
        for case_id, destination in ring.CASES.items():
            value = ring.scenario(case_id)
            self.assertEqual(case_id, value['case_id'])
            self.assertEqual('qualification', value['suite_split'])
            self.assertEqual(destination, value['events'][0]['destination'])
            self.assertAlmostEqual(ring.DRAG['start'], value['events'][0]['time_s'])
            self.assertNotIn(value['events'], suite_events)
            self.assertNotEqual(v3_events, value['events'])
            destinations.add(tuple(sorted(destination.items())))
        self.assertEqual(2, len(destinations))


class ScoreTests(unittest.TestCase):
    def score(self, rows, marks, protocol=ring.MARK_PROTOCOL):
        with tempfile.TemporaryDirectory() as tmp:
            sample = write_sample(Path(tmp) / 'sample', rows)
            path = write_marks(Path(tmp) / 'marks.json', marks, protocol)
            return ring.score(sample, path, Path(tmp) / 'score')

    def test_passes_only_with_position_and_decision_agreement(self):
        rows = [{'produced': [470., 262.], 'attached': True}] * 32
        good = [(470., 263., 'touching')] * 32
        self.assertEqual('pass', self.score(rows, good)['status'])
        # Three ends 3 px away fail the position criterion.
        far = [(470., 265.5, 'touching')] * 3 + good[3:]
        result = self.score(rows, far)
        self.assertEqual((29, 'fail'), (result['production_within_2px'], result['status']))
        # Three marked real gaps that production called attached fail the decision criterion.
        gaps = [(470., 265., 'gap')] * 3 + good[3:]
        rows_close = [{'produced': [470., 264.], 'attached': True}] * 3 + rows[3:]
        result = self.score(rows_close, gaps)
        self.assertEqual(29, result['decision_agreement'])
        self.assertEqual('fail', result['status'])

    def test_a_small_marked_gap_is_still_attached(self):
        rows = [{'produced': [470., 261.5], 'attached': True}] * 32
        marks = [(470., 261.5, 'gap')] * 32
        result = self.score(rows, marks)
        self.assertEqual(32, result['decision_agreement'])
        self.assertEqual('pass', result['status'])

    def test_marks_under_another_protocol_are_refused(self):
        rows = [{'produced': [470., 262.], 'attached': True}] * 32
        with self.assertRaises(SystemExit):
            self.score(rows, [(470., 263., 'touching')] * 32, protocol='other')


if __name__ == '__main__':
    unittest.main()
