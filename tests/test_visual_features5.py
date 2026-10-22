"""OpenCV 5 learned features (ALIKED + LightGlue) as a tracking method and a cross-check (V6 plan WP1.4)."""
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from kiroclops.visual import features5
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError
from kiroclops.visual.tracking import analyze_package

MODELS = features5.models_available()


def card(rng):
    image = np.full((48, 72, 3), (236, 226, 214), np.uint8)
    cv2.rectangle(image, (2, 2), (69, 45), (120, 80, 40), 1, cv2.LINE_AA)
    cv2.putText(image, 'Ab7', (8, 30), cv2.FONT_HERSHEY_SIMPLEX, .8, (30, 30, 30), 1, cv2.LINE_AA)
    cv2.circle(image, (58, 24), 7, (60, 140, 220), -1, cv2.LINE_AA)
    noise = rng.integers(-12, 12, image.shape, dtype=np.int16)
    return np.clip(image.astype(np.int16)+noise, 0, 255).astype(np.uint8)


def sweep_pair(na, nb):
    """Seeded random keypoint sets of the V6 LightGlue sweep, as ``FeatureEngine.match`` arguments."""
    rng = np.random.default_rng(na*100003+nb)
    da = rng.normal(size=(na, 128)).astype(np.float32)
    da /= np.linalg.norm(da, axis=1, keepdims=True)+1e-9
    db = rng.normal(size=(nb, 128)).astype(np.float32)
    db /= np.linalg.norm(db, axis=1, keepdims=True)+1e-9
    pa = rng.uniform(0, 200, size=(na, 2)).astype(np.float32)
    pb = rng.uniform(0, 300, size=(nb, 2)).astype(np.float32)
    return pa, da, (200, 150), pb, db, (300, 250)


class StubMatcher:
    def __init__(self, error):
        self.error, self.cleared = error, 0

    def setPairInfo(self, *args):
        pass

    def clearPairInfo(self):
        self.cleared += 1

    def match(self, *args):
        raise self.error


# A stand-in LightGlue helper speaking the real framing; argv[1] picks its behaviour.
FAKE_HELPER = '''
import os, sys
from kiroclops.visual.features5 import _receive, _send
mode = sys.argv[1]
if mode == 'silent':
    sys.stderr.write('model could not load')
    sys.exit(2)
_send(sys.stdout.buffer, ('ready', None))
while (request := _receive(sys.stdin.buffer)) is not None:
    if mode == 'die':
        os._exit(3)
    _send(sys.stdout.buffer, ('ok', [(0, 1, .5)]) if mode == 'ok' else ('error', 'unrelated failure'))
'''


class Features5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'café'
        self.root.mkdir()
        self.rng = np.random.default_rng(5)
        self.card = card(self.rng)
        self.background = cv2.GaussianBlur(self.rng.integers(40, 120, (240, 320, 3), dtype=np.uint8), (9, 9), 0)

    def frame(self, x, y, scale=1., angle=0.):
        image = self.background.copy()
        h, w = self.card.shape[:2]
        image[y:y+h, x:x+w] = self.card
        if scale != 1. or angle:
            matrix = cv2.getRotationMatrix2D((x+w/2, y+h/2), angle, scale)
            image = cv2.warpAffine(image, matrix, (320, 240), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return image

    def package(self, images):
        directory = self.root/str(len(list(self.root.iterdir())))
        directory.mkdir()
        paths = []
        for i, image in enumerate(images):
            path = directory/f'{i}.png'
            path.write_bytes(cv2.imencode('.png', image)[1].tobytes())
            paths.append(path)
        output = directory/'package'
        create_package(output, paths, [i*.04 for i in range(len(images))])
        return output

    def test_models_status_and_missing_models_are_explicit(self):
        status = features5.model_status(self.root/'nowhere')
        self.assertEqual(set(status), {'aliked', 'lightglue'})
        self.assertFalse(any(v['present'] for v in status.values()))
        with self.assertRaisesRegex(VisualError, 'fetch_opencv5_models'):
            features5.require_models(self.root/'nowhere')
        # Selecting the method without models fails as a measurement error, not a crash.
        package = self.package([self.frame(40, 60), self.frame(44, 60)])
        selection = {'schema_version': 1, 'objects': [{'id': 'c', 'roi': [40, 60, 72, 48], 'anchor': [36, 24], 'method': 'features'}]}
        if not MODELS:
            with self.assertRaises(VisualError) as caught:
                analyze_package(package, selection)
            self.assertEqual(caught.exception.code, 'models_missing')

    def test_feature_options_are_validated(self):
        package = self.package([self.frame(40, 60), self.frame(44, 60)])
        base = {'id': 'c', 'roi': [40, 60, 72, 48], 'anchor': [36, 24]}
        for extra, pattern in (({'method': 'features', 'peak_refinement': 'upsampled_v1'}, 'fixed-template'),
                               ({'method': 'features', 'pixel_anchor': {'method': 'observed_color_mass_v1', 'foreground_rgb': [1, 2, 3]}}, 'features tracking'),
                               ({'method': 'flow', 'cross_check': 'features_v1'}, 'cross_check requires'),
                               ({'cross_check': 'other'}, 'cross_check supports')):
            with self.subTest(extra=extra), self.assertRaisesRegex(VisualError, pattern):
                analyze_package(package, {'schema_version': 1, 'objects': [{**base, **extra}]})

    def test_engine_failure_is_a_not_located_reason_and_other_errors_propagate(self):
        points, descriptors = np.zeros((3, 2), np.float32), np.zeros((3, 128), np.float32)
        call = (points, descriptors, (10, 10), points, descriptors, (10, 10))
        matcher = StubMatcher(cv2.error(features5.ENGINE_FAILURE))
        with self.assertRaises(features5.MatcherEngineFailure):
            features5._lightglue_pairs(matcher, *call)
        self.assertEqual(1, matcher.cleared)
        matcher = StubMatcher(cv2.error('(-215:Assertion failed) unrelated'))
        with self.assertRaises(cv2.error) as caught:
            features5._lightglue_pairs(matcher, *call)
        self.assertNotIsInstance(caught.exception, features5.MatcherEngineFailure)
        self.assertEqual(1, matcher.cleared)

        class Engine:
            def describe(self, image):
                ys, xs = np.mgrid[0:image.shape[0]:4, 0:image.shape[1]:4]
                return np.float32(np.stack([xs.ravel(), ys.ravel()], 1)), np.zeros((xs.size, 128), np.float32)

            def match(self, *args):
                raise features5.MatcherEngineFailure(features5.ENGINE_FAILURE)
        template = self.frame(40, 60)
        located = features5.locate(Engine(), template, (40, 60, 72, 48), (36, 24), template, center=(76, 84), search_radius=40)
        self.assertFalse(located['valid'])
        self.assertEqual('matcher_engine_failure', located['reason'])
        self.assertGreaterEqual(located['template_keypoints'], features5.MIN_MATCHES)

    def test_isolated_matcher_reports_a_dead_helper_and_starts_a_fresh_one(self):
        points, descriptors = np.zeros((3, 2), np.float32), np.zeros((3, 128), np.float32)
        call = (points, descriptors, (10, 10), points, descriptors, (10, 10))
        matcher = features5.IsolatedLightGlue('unused')
        self.addCleanup(matcher.close)

        def use(mode):
            matcher.close()
            matcher.command = [sys.executable, '-B', '-c', FAKE_HELPER, mode]
        use('ok')
        self.assertEqual([(0, 1, .5)], matcher.match(*call))
        use('die')
        for restarts in (1, 2):
            with self.assertRaisesRegex(features5.MatcherEngineFailure, 'exit code 3'):
                matcher.match(*call)
            self.assertEqual(restarts, matcher.restarts)
        use('error')
        with self.assertRaisesRegex(cv2.error, 'unrelated failure') as caught:
            matcher.match(*call)
        self.assertNotIsInstance(caught.exception, features5.MatcherEngineFailure)
        use('silent')
        with self.assertRaisesRegex(VisualError, 'model could not load') as caught:
            matcher.match(*call)
        self.assertEqual('models_missing', caught.exception.code)

    @unittest.skipUnless(MODELS, 'OpenCV 5 feature models are not fetched (scripts/fetch_opencv5_models.py)')
    def test_warmed_up_matcher_is_order_independent_and_survives_the_engine_failure(self):
        # Without the warm-up on OpenCV 5.0.0, (50, 200), (1000, 1000) or (23, 2) right after (8, 8)
        # ends the process, and (8, 8) after (33, 2) returns other distances than a first (8, 8).
        sequence = [(8, 8), (50, 200), (23, 2), (33, 2), (1000, 1000), (3, 33), (2, 64), (8, 8)]
        empty = [(23, 2), (33, 2), (3, 33), (2, 64)]
        results, engines = {}, (features5.FeatureEngine(), features5.FeatureEngine())
        for engine, pairs in zip(engines, (sequence, sequence[::-1])):
            self.addCleanup(engine.close)
            for pair in pairs:
                try:
                    value = engine.match(*sweep_pair(*pair))
                except features5.MatcherEngineFailure:
                    value = 'engine_failure'
                results.setdefault(pair, []).append(value)
        for pair, values in results.items():
            with self.subTest(pair=pair):
                self.assertTrue(all(value == values[0] for value in values), values)
        self.assertEqual([0, 0], [engine.matcher.restarts for engine in engines])
        self.assertTrue(all(results[pair][0] != 'engine_failure' for pair in sequence if pair not in empty))
        if cv2.__version__ == '5.0.0':
            # These pairs failed in every sweep run on 5.0.0; a fixed OpenCV may simply match them.
            self.assertEqual(['engine_failure']*4, [results[pair][0] for pair in empty])

    @unittest.skipUnless(MODELS, 'OpenCV 5 feature models are not fetched (scripts/fetch_opencv5_models.py)')
    def test_locate_recovers_translation_scale_and_rotation(self):
        engine = features5.FeatureEngine()
        template = self.frame(40, 60)
        for (dx, dy, scale, angle) in ((7, -4, 1., 0.), (-12, 9, 1.2, 0.), (3, 5, 1., 8.)):
            moved = self.frame(40+dx, 60+dy, scale, angle)
            located = features5.locate(engine, template, (40, 60, 72, 48), (36, 24), moved, center=(76, 84), search_radius=40)
            with self.subTest(dx=dx, dy=dy, scale=scale, angle=angle):
                self.assertTrue(located['valid'], located)
                self.assertAlmostEqual(located['x'], 76+dx, delta=1.5)
                self.assertAlmostEqual(located['y'], 84+dy, delta=1.5)
                self.assertAlmostEqual(located['scale'], scale, delta=.08)
                self.assertAlmostEqual(located['rotation_deg'], -angle if angle else 0., delta=3.)
                self.assertGreaterEqual(located['inliers'], features5.MIN_MATCHES)

    @unittest.skipUnless(MODELS, 'OpenCV 5 feature models are not fetched (scripts/fetch_opencv5_models.py)')
    def test_features_method_and_cross_check_through_analyze_package(self):
        package = self.package([self.frame(40, 60), self.frame(47, 56), self.frame(54, 52, 1.1)])
        selection = {'schema_version': 1, 'objects': [{'id': 'c', 'roi': [40, 60, 72, 48], 'anchor': [36, 24], 'method': 'features', 'search_radius': 40}]}
        result = analyze_package(package, selection)
        frames = result['objects'][0]['frames']
        self.assertTrue(all(f['valid'] for f in frames), [f.get('reason') for f in frames])
        self.assertAlmostEqual(frames[1]['x'], 83., delta=1.5)
        self.assertAlmostEqual(frames[2]['scale'], 1.1, delta=.08)
        checked = analyze_package(self.package([self.frame(40, 60), self.frame(47, 56)]),
                                  {'schema_version': 1, 'objects': [{'id': 'c', 'roi': [40, 60, 72, 48], 'anchor': [36, 24],
                                                                      'method': 'template', 'cross_check': 'features_v1', 'search_radius': 40}]})
        frame = checked['objects'][0]['frames'][1]
        self.assertTrue(frame['valid'])
        self.assertTrue(frame['features_cross_check']['valid'])
        self.assertLess(frame['features_cross_check']['consistency_px'], 1.5)


if __name__ == '__main__':
    unittest.main()
