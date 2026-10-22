"""V5b: connection attachment honors the measurement's occlusion flags (PHASE3D_V5B_PLAN.md 3.1)."""
import unittest

from kiroclops.visual.events import extract_events


def node(x, oid, count=3):
    """A 4x4 px box centred on (x, 10) in every frame."""
    return {'id': oid, 'frames': [{'index': i, 'time_s': i * .05, 'valid': True, 'x': x, 'y': 10,
                                   'left': x - 2, 'top': 8, 'width': 4, 'height': 4} for i in range(count)]}


def connection(ends, flags=None):
    frames = []
    for i, end in enumerate(ends):
        frame = {'index': i, 'time_s': i * .05, 'valid': True, 'start': [12, 10], 'end': end}
        if flags is not None and flags[i] is not None:
            frame['end_occluded'] = flags[i]
        frames.append(frame)
    return {'id': 'ab', 'from_object': 'a', 'to_object': 'b', 'frames': frames}


class OcclusionAttachmentTests(unittest.TestCase):
    # a spans x 8-12 and b spans x 28-32; the start (12, 10) lies on a's edge.
    def relations(self, ends, flags=None):
        result = extract_events({'objects': [node(10, 'a'), node(30, 'b')], 'connections': [connection(ends, flags)]})
        rows = [r for r in result['relations'] if r['type'] == 'connection_attachment']
        return rows, result

    def test_a_flagged_end_beyond_the_tolerance_is_attached(self):
        rows, result = self.relations([[25, 10]] * 3, [True] * 3)
        self.assertEqual([True] * 3, [r['visually_attached'] for r in rows])
        self.assertEqual([[False, True]] * 3, [r['occluded_ends'] for r in rows])
        self.assertEqual([[0., 3.]] * 3, [r['endpoint_distances_px'] for r in rows])
        self.assertFalse([e for e in result['events'] if e['type'] == 'connection_detached'])

    def test_an_unflagged_end_beyond_the_tolerance_stays_detached(self):
        rows, result = self.relations([[25, 10]] * 3)
        self.assertEqual([False] * 3, [r['visually_attached'] for r in rows])
        self.assertEqual([[False, False]] * 3, [r['occluded_ends'] for r in rows])
        self.assertEqual(['connection_detached'], [e['type'] for e in result['events'] if e['type'].startswith('connection')])

    def test_a_false_flag_keeps_the_distance_rule(self):
        rows, _ = self.relations([[25, 10], [27, 10], [25, 10]], [False] * 3)
        self.assertEqual([False, True, False], [r['visually_attached'] for r in rows])

    def test_only_a_literal_true_counts_as_a_flag(self):
        rows, _ = self.relations([[25, 10]] * 3, ['yes', 1, None])
        self.assertEqual([False] * 3, [r['visually_attached'] for r in rows])

    def test_a_flag_on_the_other_end_does_not_excuse_a_detached_end(self):
        frames = [{'index': i, 'time_s': i * .05, 'valid': True, 'start': [12, 10], 'end': [25, 10],
                   'start_occluded': True} for i in range(3)]
        result = extract_events({'objects': [node(10, 'a'), node(30, 'b')],
                                 'connections': [{'id': 'ab', 'from_object': 'a', 'to_object': 'b', 'frames': frames}]})
        rows = [r for r in result['relations'] if r['type'] == 'connection_attachment']
        self.assertEqual([False] * 3, [r['visually_attached'] for r in rows])


if __name__ == '__main__':
    unittest.main()
