"""V3 floor-aware ground gain gate: applied only where the measurement resolves it."""
from copy import deepcopy
import math
import tempfile
import unittest
from pathlib import Path

from kiroclops.local_engine import media_suite
from kiroclops.local_engine import suite_game as suite
from kiroclops.local_engine.execution_policy import REVISED_ID, resolve_policy
from kiroclops.visual.schema import VisualError

FLOOR = media_suite.GROUND_FLOOR_PROFILE
LEGACY = media_suite.LEGACY_GAME_PROFILE


def recipe(stage, split, sid):
    return next(s for s in suite.scenarios(stage, split) if s['id'] == sid)


def _native_frames(scenario):
    raw = suite.native_scenario(scenario['mechanic'], scenario)
    active, frames = set(), []
    for i in range(240):
        for event in scenario['events']:
            if event['tick'] == i:
                (active.add if event['pressed'] else active.remove)(event['action'])
        frames.append({'frame_index': i, 'physics_tick': 60 + i, 'time_s': i / 60, 'actions': sorted(active)})
    assert raw is not None
    return frames


def ground_report(scenario, error=0., *, role='reference', body=16.):
    """A constant post-input displacement error leaves speed/coast unchanged."""
    frames = [dict(f, valid=True, x=0. if i < 30 else (i - 30) / 6, y=40.)
              for i, f in enumerate(_native_frames(scenario))]
    plan, metrics, errors = suite._measure_ground(frames, scenario, 16.)
    assert not errors
    for frame in frames[30:]:
        frame['x_body'] += error
    metrics['end_position_body'] += error
    return {'status': 'ok', 'fps': 60, 'frames': frames, 'valid_ratio': 1., 'camera_valid': True,
            'body_height_px': body, 'scenario': deepcopy(scenario),
            'game': 'tiny' if role == 'reference' else 'heart', 'variant': role,
            'metrics': metrics, 'phases': plan}


def triple(sid, original_error, candidate_error, split='calibration'):
    s = recipe('ground', split, sid)
    return (ground_report(s, body=7.), ground_report(s, original_error, role='before'),
            ground_report(s, candidate_error, role='after'))


class GroundFloorGateTests(unittest.TestCase):
    def test_floor_is_the_rounding_rms_of_two_pixel_grids(self):
        expected = math.sqrt(((1 / 7) ** 2 + (1 / 16) ** 2) / 12)
        self.assertAlmostEqual(media_suite.ground_measurement_floor(7., 16.), expected, places=12)
        self.assertAlmostEqual(expected, .0450133, places=6)
        for bad in (0, -1., float('nan'), float('inf'), '7'):
            with self.assertRaises(VisualError):
                media_suite.ground_measurement_floor(bad, 16.)

    def test_unresolvable_short_case_requires_preservation_instead_of_gain(self):
        ref, original, candidate = triple('r06', .0643, .0374)
        legacy = media_suite._compare(ref, original, candidate, 'ground', require_improvement=True)
        self.assertFalse(legacy['gates']['improvement'])
        self.assertFalse(legacy['pass'])
        row = media_suite._compare(ref, original, candidate, 'ground', require_improvement=True,
                                   acceptance_profile=FLOOR)
        self.assertTrue(row['pass'])
        self.assertTrue(row['gates']['improvement'])
        self.assertEqual('floor_preservation', row['improvement_gate_mode'])
        self.assertAlmostEqual(row['floor_waiver_bound_body'], 1.5 * row['measurement_floor_body'])
        self.assertEqual(FLOOR, row['acceptance_profile'])
        self.assertEqual(media_suite.GROUND_FLOOR_RULE, row['improvement_rule'])
        self.assertLess(row['improvement_fraction'], .5)  # The gain is still reported, not hidden.

    def test_preservation_bound_rejects_a_worse_short_case(self):
        ref, original, candidate = triple('r06', .0643, .07)
        row = media_suite._compare(ref, original, candidate, 'ground', require_improvement=True,
                                   acceptance_profile=FLOOR)
        self.assertGreater(.07, row['floor_waiver_bound_body'])
        self.assertFalse(row['gates']['improvement'])
        self.assertFalse(row['pass'])
        self.assertEqual('fail', row['numerical_status'])

    def test_resolvable_case_keeps_the_fifty_percent_gate(self):
        ref, original, weak = triple('r48', .2, .12)
        row = media_suite._compare(ref, original, weak, 'ground', require_improvement=True,
                                   acceptance_profile=FLOOR)
        self.assertEqual('relative_gain', row['improvement_gate_mode'])
        self.assertIsNone(row['floor_waiver_bound_body'])
        self.assertTrue(all(v for k, v in row['gates'].items() if k != 'improvement'))
        self.assertFalse(row['gates']['improvement'])
        self.assertFalse(row['pass'])
        _, _, strong = triple('r48', .2, .08)
        self.assertTrue(media_suite._compare(ref, original, strong, 'ground', require_improvement=True,
                                             acceptance_profile=FLOOR)['pass'])

    def test_heldout_cases_keep_the_gain_gate(self):
        for split, sid, original_error in (('regression', 'r18', .2935), ('validation', 'r21', .37),
                                           ('validation', 'l33', .5509), ('regression', 'reverse24', .4417)):
            with self.subTest(sid=sid):
                ref, original, candidate = triple(sid, original_error, .1, split=split)
                row = media_suite._compare(ref, original, candidate, 'ground', require_improvement=True,
                                           acceptance_profile=FLOOR)
                self.assertEqual('relative_gain', row['improvement_gate_mode'])

    def test_absolute_gates_are_never_waived(self):
        ref, original, candidate = triple('r06', .0643, .0374)
        candidate['metrics']['end_position_body'] += 1.
        row = media_suite._compare(ref, original, candidate, 'ground', require_improvement=True,
                                   acceptance_profile=FLOOR)
        self.assertFalse(row['gates']['end_position'])
        self.assertFalse(row['pass'])

    def test_legacy_profile_and_non_primary_rows_are_unchanged(self):
        ref, original, candidate = triple('r06', .0643, .0374)
        plain = media_suite._compare(ref, original, candidate, 'ground', require_improvement=True)
        self.assertEqual(plain, media_suite._compare(ref, original, candidate, 'ground', require_improvement=True,
                                                     acceptance_profile=LEGACY))
        preserved = media_suite._compare(ref, original, candidate, 'ground', require_improvement=False,
                                         acceptance_profile=FLOOR)
        self.assertNotIn('improvement_gate_mode', preserved)
        self.assertTrue(preserved['gates']['improvement'])

    def test_unknown_profile_is_rejected(self):
        ref, original, candidate = triple('r06', .0643, .0374)
        with self.assertRaises(VisualError) as caught:
            media_suite._compare(ref, original, candidate, 'ground', require_improvement=True,
                                 acceptance_profile='relaxed-game-v9')
        self.assertEqual('invalid_suite', caught.exception.code)

    def test_mismatched_target_body_heights_are_rejected(self):
        ref, original, candidate = triple('r06', .0643, .0374)
        candidate['body_height_px'] = 15.
        with self.assertRaises(VisualError):
            media_suite._compare(ref, original, candidate, 'ground', require_improvement=True,
                                 acceptance_profile=FLOOR)

    def test_suite_creation_accepts_the_rule_only_for_revised_ground_suites(self):
        policy = resolve_policy(REVISED_ID, 'ground')
        with tempfile.TemporaryDirectory(prefix='v3 ground ') as temp:
            root = Path(temp)
            for kwargs, label in (
                    ({'stage': 'jump', 'execution_policy': resolve_policy(REVISED_ID, 'jump'),
                      'game_acceptance_profile': FLOOR}, 'jump'),
                    ({'stage': 'ground', 'execution_policy': None, 'game_acceptance_profile': FLOOR}, 'schema-1'),
                    ({'stage': 'ground', 'execution_policy': policy, 'game_acceptance_profile': 'other'}, 'unknown')):
                with self.subTest(label=label):
                    with self.assertRaises(VisualError) as caught:
                        media_suite.create_suite(root / label, records=[], required_cases=[], domain='game',
                                                 original=root / 'original', authoring_parent=root / 'parent',
                                                 goal='Ground motion.', **kwargs)
                    self.assertEqual('invalid_suite', caught.exception.code)
                    self.assertFalse((root / label).exists())


if __name__ == '__main__':
    unittest.main()
