"""V4 final game visual review logistics: grouping, the fixed sample and verdict merging."""
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from kiroclops.visual.schema import VisualError, file_sha256, read_json, write_json
from scripts import phase3d_v4_visual_review as visual


def _png(path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), image)
    return file_sha256(path)


def folders(root):
    review, zoom = Path(root) / 'review', Path(root) / 'zoom'
    base = np.full((460, 600, 3), 24, np.uint8)
    base[100:120, 50:70] = 200
    relabelled = base.copy()
    relabelled[5:20, 5:200] = 240          # only the label strip differs
    other = base.copy()
    other[300:310, 400:410] = 90           # the frame differs below the label strip
    sheets = [{'path': f'sheets/{name}.png', 'sha256': _png(review / f'sheets/{name}.png', image)}
              for name, image in (('a-000', base), ('b-000', relabelled), ('c-000', other))]
    write_json(review / 'result.json', {'numeric_status': 'pass', 'failures': [], 'sheets': sheets})
    crop = np.zeros((100, 100, 3), np.uint8)
    pages = []
    for row in sheets:
        name = Path(row['path']).stem + '-zoom.png'
        pages.append({'path': name, 'mirrors_sheet': row['path'], 'mirrored_sheet_sha256': row['sha256'],
                      'frames': [0], 'sha256': _png(zoom / name, crop)})
    write_json(zoom / 'manifest.json', {'kind': 'MagnifiedRawReviewPages', 'pages': pages,
                                        'review_result_sha256': file_sha256(review / 'result.json')})
    return review, zoom


def verdicts(output, prefix, batches, verdict):
    for kind, rows in batches.items():
        for number, names in enumerate(rows, 1):
            write_json(output / 'verdicts' / f'{prefix}-{kind}-{number:02}.json',
                       {'groups': [{'group': name, 'verdict': verdict(name), 'note': ''} for name in names]})


class VisualReviewPlanTests(unittest.TestCase):
    def test_groups_dedupe_zoom_bytes_and_label_only_sheet_differences(self):
        with tempfile.TemporaryDirectory() as root:
            review, zoom = folders(root)
            _, _, found = visual.groups(review, zoom)
            self.assertEqual([['a-000-zoom.png', 'b-000-zoom.png', 'c-000-zoom.png']], [r['members'] for r in found['zoom']])
            self.assertEqual([['sheets/a-000.png', 'sheets/b-000.png'], ['sheets/c-000.png']],
                             [r['members'] for r in found['sheet']])

    def test_changed_bytes_or_a_foreign_manifest_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            review, zoom = folders(root)
            manifest = read_json(zoom / 'manifest.json')
            write_json(zoom / 'manifest.json', dict(manifest, review_result_sha256='0' * 64))
            with self.assertRaises(VisualError):
                visual.groups(review, zoom)
            write_json(zoom / 'manifest.json', manifest)
            _png(zoom / 'a-000-zoom.png', np.ones((100, 100, 3), np.uint8))
            with self.assertRaises(VisualError):
                visual.groups(review, zoom)

    def test_two_passes_merge_with_sonnet_final_and_sample_agreement(self):
        with tempfile.TemporaryDirectory() as root:
            review, zoom = folders(root)
            output = Path(root) / 'plan'
            summary = visual.plan(review, zoom, output, batch_images=1)
            self.assertEqual({'zoom': 1, 'sheet': 2}, summary['groups'])
            with self.assertRaises(VisualError):
                visual.plan(review, zoom, output)
            value = read_json(output / 'plan.json')
            self.assertEqual(1, len(value['sample']['groups']))
            with self.assertRaises(VisualError):
                visual.second(output)
            verdicts(output, 'haiku', value['first_pass'], lambda name: 'uncertain' if name == 's0001' else 'ok')
            second = visual.second(output)
            self.assertEqual(1, second['flagged'])
            chosen = read_json(output / 'plan.json')['second_pass']['batches']
            names = sorted(n for rows in chosen.values() for batch in rows for n in batch)
            self.assertEqual(sorted({'s0001'} | set(value['sample']['groups'])), names)
            verdicts(output, 'sonnet', chosen, lambda _: 'ok')
            merged = visual.merge(output)
            self.assertEqual({'ok': 3, 'anomaly': 0, 'uncertain': 0}, merged['final_counts'])
            self.assertEqual({'zoom': 3, 'sheet': 3}, merged['covered'])
            self.assertEqual(['s0001'], read_json(output / 'summary.json')['second_pass']['flagged_cleared'])

    def test_a_verdict_file_must_cover_its_batch_in_order(self):
        with tempfile.TemporaryDirectory() as root:
            review, zoom = folders(root)
            output = Path(root) / 'plan'
            visual.plan(review, zoom, output, batch_images=5)
            write_json(output / 'verdicts' / 'haiku-zoom-01.json', {'groups': [{'group': 'z0000', 'verdict': 'ok'}]})
            write_json(output / 'verdicts' / 'haiku-sheet-01.json', {'groups': [{'group': 's0001', 'verdict': 'ok'},
                                                                              {'group': 's0000', 'verdict': 'ok'}]})
            with self.assertRaises(VisualError):
                visual.second(output)


if __name__ == '__main__':
    unittest.main()
