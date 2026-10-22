"""Observed visibility brackets and common sampled support; no inferred pixels."""
from .schema import VisualError

POLICY = 'observed_response_v2'


def sample_before(observation, origin, bound, max_gap):
    """Last recorded image before an interrupt, regardless of tracking validity."""
    times = sorted({f['time_s']-origin for o in observation['objects'] for f in o['frames']})
    earlier = [t for t in times if t < bound-1e-9]
    if not earlier or bound-earlier[-1] > max_gap+1e-9:
        raise VisualError('capture_timing_failure', 'No timely image before the terminating input')
    return earlier[-1]


def observed_brackets(frames, start_index, end_index):
    """Appearance lies between last absent/first present; removal is the reverse.

    The caller supplies indices identified from pixels. This does not turn a
    missing detection in the interior into a disappearance.
    """
    def bounds(index):
        frame = frames[index]
        return frame.get('time_interval_s', [frame['time_s'], frame['time_s']])
    return {
        'start': {'interval_s': [bounds(max(0, start_index-1))[0], bounds(start_index)[1]],
                  'frame_indices': sorted({max(0, start_index-1), start_index}),
                  'clip_censored': start_index == 0},
        'end': {'interval_s': [bounds(end_index)[0], bounds(min(len(frames)-1, end_index+1))[1]],
                'frame_indices': sorted({end_index, min(len(frames)-1, end_index+1)}),
                'clip_censored': end_index == len(frames)-1},
    }


def shared_lifecycle_segment(start, end, ref_interval, target_interval, gap, tolerance):
    """Separate already-validated visibility timing from observable geometry.

    Only lifecycle edges may censor a segment, never invalid interior samples.
    The excluded duration and its bound are reported. No time stretching occurs.
    """
    a, b = ref_interval
    c, d = target_interval
    left, right = max(start, a, c), min(end, b, d)
    allowed = min(gap, tolerance)
    if max(abs(a-c), abs(b-d)) > allowed+1e-9:
        # Do not make a delayed/missing object disappear from the error budget.
        return (max(start, a), min(end, b)), {'status': 'unmatched_lifecycle', 'censored': False}
    return ((left, right) if right > left else None), {
        'status': 'shared_visible_samples' if right > left else 'visibility_transition_only',
        'reference_interval_s': [a, b], 'candidate_interval_s': [c, d],
        'requested_window_s': [start, end], 'censored': True,
        'maximum_edge_difference_s': max(abs(a-c), abs(b-d)),
        'maximum_allowed_edge_difference_s': allowed,
        'duration_rescaled': False,
    }
