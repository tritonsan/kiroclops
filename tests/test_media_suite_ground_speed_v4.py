"""V4 ground speed gate and V4 game recipe registry (PHASE3D_V4_PLAN.md)."""
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from kiroclops.local_engine import media_suite
from kiroclops.local_engine import suite_game as suite
from kiroclops.local_engine.execution_policy import REVISED_ID, resolve_policy
from kiroclops.visual.schema import VisualError

from tests.test_media_suite_ground_floor_v3 import _native_frames

SPEED = media_suite.GROUND_SPEED_PROFILE
FLOOR = media_suite.GROUND_FLOOR_PROFILE


def recipe(split, sid, registry='v4'):
    return next(s for s in suite.scenarios('ground', split, registry) if s['id'] == sid)


def report(scenario, body, *, speed=.13, scale=1., role='reference'):
    """Ramp up over 6 frames, hold `speed` body/frame, stop over 6 frames; whole-pixel positions."""
    phases = suite.phases(scenario)
    start, end, direction = phases[0]['start'], phases[-2]['end'], phases[0]['direction']
    frames, x, v = [], 0., 0.
    for frame in _native_frames(scenario):
        i = frame['frame_index']
        frames.append(dict(frame, valid=True, x=float(round(100. + direction * x * body)), y=40.))
        if start <= i < end:
            v = speed * scale * min(1., (i - start + 1) / 6)
        else:
            v = max(0., v - speed * scale / 6)
        x += v
    plan, metrics, errors = suite._measure_ground(frames, scenario, body)
    assert not errors, errors
    return {'status': 'ok', 'fps': 60, 'frames': frames, 'valid_ratio': 1., 'camera_valid': True,
            'body_height_px': body, 'scenario': deepcopy(scenario), 'game': 'tiny' if role == 'reference' else 'heart',
            'variant': role, 'metrics': metrics, 'phases': plan}


def triple(split, sid, *, candidate_scale=1.):
    s = recipe(split, sid)
    return (report(s, 7.), report(s, 16., scale=.6, role='before'),
            report(s, 16., scale=candidate_scale, role='after'))


def compare(rows, profile=SPEED, require_improvement=True):
    return media_suite._compare(*rows, 'ground', require_improvement=require_improvement, acceptance_profile=profile)


class SpeedGateTests(unittest.TestCase):
    def test_stall_bound(self):
        self.assertAlmostEqual(48 / 510, media_suite.speed_stall_bound(16), places=12)
        self.assertAlmostEqual(24 / 126, media_suite.speed_stall_bound(8), places=12)

    def test_same_motion_on_two_pixel_grids_passes_with_the_quantization_floor(self):
        row = compare(triple('validation', 'r36'))
        self.assertTrue(row['speed_eligible_v4'])
        self.assertEqual(36, row['speed_held_frames'])
        self.assertEqual([50, 65], row['speed_window_v4'])
        self.assertLess(row['speed_relative_error_v4'], .05)
        quantum = media_suite.speed_stall_bound() * 60 * (1 / 7 + 1 / 16)
        self.assertAlmostEqual(max(.10, quantum / abs(row['reference_speed_v4'])), row['speed_tolerance_v4'])
        self.assertGreater(row['speed_tolerance_v4'], .10)
        self.assertTrue(row['gates']['speed'])
        self.assertEqual(SPEED, row['acceptance_profile'])
        self.assertEqual(media_suite.GROUND_SPEED_RULE, row['speed_rule_document'])

    def test_a_clearly_faster_candidate_fails(self):
        row = compare(triple('validation', 'r36', candidate_scale=1.3))
        self.assertGreater(row['speed_relative_error_v4'], row['speed_tolerance_v4'])
        self.assertFalse(row['gates']['speed'])
        self.assertFalse(row['pass'])

    def test_short_holds_are_not_speed_gated(self):
        row = compare(triple('regression', 'r21', candidate_scale=1.06))
        self.assertFalse(row['speed_eligible_v4'])
        self.assertEqual(21, row['speed_held_frames'])
        self.assertTrue(row['gates']['speed'])
        self.assertNotIn('speed_relative_error_v4', row)

    def test_an_incomplete_window_is_a_tracking_failure(self):
        rows = triple('validation', 'r36')
        rows[2]['frames'][52]['valid'] = False
        with self.assertRaises(VisualError) as caught:
            compare(rows)
        self.assertEqual('tracking_failure', caught.exception.code)

    def test_other_profiles_keep_the_v1_speed_gate(self):
        rows = triple('validation', 'r36')
        for profile in (None, media_suite.LEGACY_GAME_PROFILE, FLOOR):
            with self.subTest(profile=profile):
                row = compare(rows, profile)
                self.assertNotIn('speed_rule', row)
        preserved = compare(rows, SPEED, require_improvement=False)
        self.assertEqual(SPEED, preserved['speed_rule'])

    def test_suite_creation_accepts_the_v4_rule_only_for_revised_ground_suites(self):
        with tempfile.TemporaryDirectory(prefix='v4 ground ') as temp:
            root = Path(temp)
            for kwargs, label in (
                    ({'stage': 'jump', 'execution_policy': resolve_policy(REVISED_ID, 'jump'),
                      'game_acceptance_profile': SPEED}, 'jump'),
                    ({'stage': 'ground', 'execution_policy': None, 'game_acceptance_profile': SPEED}, 'schema-1')):
                with self.subTest(label=label):
                    with self.assertRaises(VisualError) as caught:
                        media_suite.create_suite(root / label, records=[], required_cases=[], domain='game',
                                                 original=root / 'original', authoring_parent=root / 'parent',
                                                 goal='Ground motion.', **kwargs)
                    self.assertEqual('invalid_suite', caught.exception.code)


class RegistryTests(unittest.TestCase):
    def test_v4_registry_counts_and_unseen_validation(self):
        counts = [len(suite.scenarios('ground', split, 'v4')) for split in suite.SPLITS]
        self.assertEqual([6, 36, 18], counts)
        used = {s['id'] for split in suite.SPLITS for s in suite.scenarios('ground', split)}
        validation = suite.scenarios('ground', 'validation', 'v4')
        self.assertEqual({'r36', 'l42', 'reverse30'}, {s['id'] for s in validation})
        self.assertFalse(used & {s['id'] for s in validation})
        for scenario in validation:
            self.assertGreaterEqual(suite.phases(scenario)[-2]['end'] - suite.phases(scenario)[-2]['start'], 24)
        regression = {s['id'] for s in suite.scenarios('ground', 'regression', 'v4')}
        self.assertEqual({'r18', 'l30', 'reverse24', 'r21', 'l33', 'reverse27'}, regression)

    def test_identity_resolves_against_its_own_registry(self):
        v4 = recipe('regression', 'l33')
        self.assertEqual('v4', v4['suite_registry'])
        self.assertEqual(v4, suite.canonical_scenario('ground', v4))
        self.assertNotIn('suite_registry', suite.native_scenario('ground', v4))
        v1 = recipe('validation', 'l33', 'v1')
        self.assertNotIn('suite_registry', v1)
        self.assertEqual(v1, suite.canonical_scenario('ground', v1))
        moved = dict(v4)
        del moved['suite_registry']
        with self.assertRaises(VisualError):
            suite.canonical_scenario('ground', moved)
        with self.assertRaises(VisualError):
            suite.canonical_scenario('ground', dict(v4, suite_registry='v9'))


if __name__ == '__main__':
    unittest.main()
