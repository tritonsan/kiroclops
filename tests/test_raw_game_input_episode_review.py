"""Independent public-input censoring, with raw geometric excursions.

No application, model, capture, or production motion detector is involved.
"""
from copy import deepcopy
import importlib.util
import math
from pathlib import Path
import unittest

from scripts.raw_game_input_episode_review import filter_raw_events
from tests.evidence import skip_module_without

skip_module_without('artifacts/phase3a/tools/raw_motion_event_proposals_v4.py')
_PATH = Path(__file__).resolve().parents[1] / 'artifacts/phase3a/tools/raw_motion_event_proposals_v4.py'
_SPEC = importlib.util.spec_from_file_location('frozen_input_episode_raw_helper', _PATH)
_RAW = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RAW)
candidates = _RAW.candidates


def trajectory(offset=0., index_offset=0):
    # A real endpoint excursion beyond a later stable position. Departure
    # observed at t=.15; first quiet tail at .40, supported through .60.
    values = [0., 0., 0., 3., 8., 12., 14., 12., 10., 10., 10., 10., 10., 10., 10., 10., 10.]
    return [{'index': i + index_offset, 'time_s': i * .05 + offset,
             'points': {'character': {'x': x, 'y': 100., 'status': 'unsigned'}}}
            for i, x in enumerate(values)]


def inputs(*events, provenance='recorder_verified'):
    return {'provenance': provenance, 'events': list(events)}


def event(t, action='jump', **extra):
    return {'time_s': t, 'action': action, **extra}


class RawGameInputEpisodeReview(unittest.TestCase):
    def review(self, timeline, rows=None, raw_events=None):
        rows = trajectory() if rows is None else rows
        proposals = candidates(rows, ['character'], .2) if raw_events is None else raw_events
        self.assertTrue(any(e['type'] == 'overshoot' for e in proposals), proposals)
        before = deepcopy((rows, proposals, timeline))
        result = filter_raw_events(rows, proposals, timeline, object_id='character')
        self.assertEqual(before, (rows, proposals, timeline))
        self.assertFalse(result['production_coordinates_used'])
        self.assertFalse(result['production_events_used'])
        self.assertFalse(result['full_behavior_acceptance'])
        return result, proposals

    def test_additional_known_input_inside_raw_episode_censors_only_overshoot(self):
        result, proposals = self.review(inputs(event(.1, pressed=True), event(.25, pressed=False)))
        self.assertFalse(any(e['type'] == 'overshoot' for e in result['events']))
        self.assertEqual([e for e in proposals if e['type'] != 'overshoot'], result['events'])
        self.assertEqual(1, result['suppressed_count'])
        episode = result['episodes'][0]
        self.assertAlmostEqual(.15, episode['second_observed_point_s'])
        self.assertAlmostEqual(.40, episode['stable_tail_first_s'])
        self.assertEqual([1], result['decisions'][0]['interrupting_public_input_indices'])

    def test_user_declared_extra_command_inside_episode_also_censors_endpoint(self):
        result, _ = self.review(inputs(event(.3, 'left', pressed=True), provenance='user_declared'))
        self.assertFalse(any(e['type'] == 'overshoot' for e in result['events']))
        self.assertEqual('user_declared', result['public_input_provenance'])

    def test_uninterrupted_overshoot_preserved_exactly(self):
        result, proposals = self.review(inputs(event(.1, pressed=True)))
        self.assertEqual(proposals, result['events'])
        self.assertEqual(0, result['suppressed_count'])

    def test_strict_second_point_and_tail_bounds_exclude_boundary_inputs(self):
        rows = trajectory()
        for t in (rows[3]['time_s'], rows[8]['time_s']):
            result, proposals = self.review(inputs(event(t)), rows=rows)
            self.assertEqual(proposals, result['events'])
            self.assertFalse(result['episodes'][0]['interrupted_by_known_input'])

    def test_known_input_in_onset_bracket_is_not_an_interruption(self):
        # .125 lies after motion_start.interval[0] but before the second
        # observed point. Using the bracket's lower edge would wrongly censor.
        result, proposals = self.review(inputs(event(.125)))
        self.assertEqual(proposals, result['events'])

    def test_unknown_timeline_cannot_invent_interruption_from_declared_events(self):
        result, proposals = self.review(inputs(event(.25), provenance='unknown'))
        self.assertEqual(proposals, result['events'])
        self.assertEqual('unknown_inputs_no_interruption_inferred', result['input_scope'])

    def test_repeated_held_state_receipt_does_not_create_a_new_command(self):
        result, proposals = self.review(inputs(event(.1, pressed=True), event(.25, pressed=True)))
        self.assertEqual(proposals, result['events'])
        self.assertFalse(result['episodes'][0]['interrupted_by_known_input'])

    def test_non_overshoot_events_and_other_object_excursions_remain(self):
        rows = trajectory()
        proposals = candidates(rows, ['character'], .2)
        unrelated = [dict(e, type='oscillation') for e in proposals if e['type'] == 'overshoot']
        unrelated += [dict(e, object_ids=['different-character']) for e in proposals if e['type'] == 'overshoot']
        result, _ = self.review(inputs(event(.25)), rows=rows, raw_events=proposals + unrelated)
        self.assertEqual(unrelated, result['events'][-len(unrelated):])

    def test_unbound_overshoot_is_retained_as_explicit_unresolved_not_suppressed(self):
        bad = [{'type': 'overshoot', 'object_ids': ['character'], 'indices': [900, 901],
                'observed_interval_s': [.25, .3]}]
        result, _ = self.review(inputs(event(.25)), raw_events=bad)
        self.assertEqual(bad, result['events'])
        self.assertEqual('raw_episode_binding_unresolved', result['unsupported'][0]['reason'])

    def test_invalid_or_gapped_observation_cannot_join_two_episodes(self):
        rows = trajectory()
        rows[7]['points'] = {}
        result = filter_raw_events(rows, candidates(rows, ['character'], .2), inputs(event(.25)), object_id='character')
        self.assertFalse(result['episodes'][0]['endpoint_observed'])
        self.assertIsNone(result['episodes'][0]['stable_tail_first_s'])
        spaced = deepcopy(trajectory())
        for row in spaced[7:]:row['time_s'] += .20
        result = filter_raw_events(spaced, candidates(spaced, ['character'], .2), inputs(event(.25)), object_id='character')
        self.assertFalse(result['episodes'][0]['endpoint_observed'])

    def test_input_in_another_episode_does_not_censor_uninterrupted_excursion(self):
        rows = trajectory() + trajectory(1., 100)
        result, proposals = self.review(inputs(event(1.25)), rows=rows)
        self.assertEqual(2, sum(e['type'] == 'overshoot' for e in proposals))
        self.assertEqual(1, sum(e['type'] == 'overshoot' for e in result['events']))
        self.assertEqual([False, True], [e['interrupted_by_known_input'] for e in result['episodes']])

    def test_nonfinite_or_unordered_known_inputs_and_rows_are_rejected(self):
        rows = trajectory()
        for timeline in (inputs(event(math.nan)), inputs(event(.3), event(.2))):
            with self.assertRaises(ValueError):
                filter_raw_events(rows, [], timeline, object_id='character')
        for bad in ([rows[0], rows[0]], [dict(rows[0], time_s=math.inf)]):
            with self.assertRaises(ValueError):
                filter_raw_events(bad, [], inputs(), object_id='character')


if __name__ == '__main__':unittest.main()
