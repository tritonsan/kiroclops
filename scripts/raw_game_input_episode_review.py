"""Independent raw-image episode censoring under a public input contract.

Only independently extracted raw anchors, unsigned raw events and the public
InputTimeline enter. No production coordinates, events, detector, code, or
behavior verdict is read. This module does not grant visual acceptance.
"""
from bisect import bisect_left
from copy import deepcopy
import math

VERSION = 'raw-known-input-endpoint-overshoot-v1'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _commands(timeline):
    if not isinstance(timeline, dict) or timeline.get('provenance') not in {
            'recorder_verified', 'user_declared', 'unknown'}:
        raise ValueError('An explicit public InputTimeline provenance is required')
    if timeline['provenance'] == 'unknown':
        return [], 'unknown_inputs_no_interruption_inferred'
    events = timeline.get('events')
    if not isinstance(events, list):raise ValueError('Public input events must be a list')
    prior, states, result = -math.inf, {}, []
    for index, event in enumerate(events):
        if (not isinstance(event, dict) or not _number(event.get('time_s'))
                or event['time_s'] < 0 or event['time_s'] < prior
                or not isinstance(event.get('action'), str) or not event['action']):
            raise ValueError('Known public inputs need ordered finite times and actions')
        prior = event['time_s']
        pressed = event.get('pressed')
        if type(pressed) is bool:
            key = tuple(event.get(name) for name in ('action', 'selector', 'key', 'button'))
            if key in states and states[key] is pressed:continue
            states[key] = pressed
        result.append({'public_input_index': index, **deepcopy(event)})
    return result, 'known_public_transitions'


def _segments(rows, object_id, max_gap_s):
    if not isinstance(rows, list):raise ValueError('Independent raw rows must be a list')
    segments, segment, prior, seen = [], [], None, set()
    for row in rows:
        if (not isinstance(row, dict) or not _number(row.get('time_s'))
                or type(row.get('index')) is not int or row['index'] < 0
                or row['index'] in seen or prior is not None and row['time_s'] <= prior):
            raise ValueError('Raw rows require unique indices and strictly increasing finite times')
        seen.add(row['index'])
        prior = row['time_s']
        point = row.get('points', {}).get(object_id, {})
        valid = all(_number(point.get(axis)) for axis in ('x', 'y'))
        if not valid or segment and row['time_s'] - segment[-1]['time_s'] > max_gap_s:
            if segment:segments.append(segment)
            segment = []
        if valid:
            segment.append({'index': row['index'], 'time_s': row['time_s'],
                            'point': [point['x'], point['y']]})
    if segment:segments.append(segment)
    return segments


def _episodes(segments, object_id, rest_duration_s, rest_radius_px):
    """Use the frozen raw helper's forward-window rest/departure definition.

    Episode input bounds are its second observed departure point and first
    stable-tail point. The event uncertainty interval's lower bound is never
    substituted for either point. Invalid/gapped rows split raw segments.
    """
    episodes = []
    for segment in segments:
        if len(segment) < 2:continue
        times = [row['time_s'] for row in segment]
        cursor = search_from = 0
        while cursor < len(segment) - 1:
            start = next((i for i in range(search_from, len(segment))
                          if math.dist(segment[i]['point'], segment[cursor]['point']) > rest_radius_px), None)
            if start is None:break
            first = max(cursor, start - 1)
            quiet = quiet_end = None
            for i in range(start + 1, len(segment)):
                end = bisect_left(times, times[i] + rest_duration_s)
                if end >= len(segment):break
                tail = segment[i:end + 1]
                extent = math.hypot(*(max(row['point'][axis] for row in tail)
                                      - min(row['point'][axis] for row in tail) for axis in (0, 1)))
                if extent <= rest_radius_px:
                    quiet, quiet_end = i, end
                    break
            endpoint = quiet is not None
            final = quiet if endpoint else len(segment) - 1
            episodes.append({'episode_id': 'raw-episode-' + str(len(episodes)).zfill(3),
                'object_ids': [object_id], 'raw_segment_indices': [segment[0]['index'], segment[-1]['index']],
                'start_indices': [segment[first]['index'], segment[start]['index']],
                'start_interval_s': [times[first], times[start]],
                'second_observed_point_s': times[start],
                'endpoint_observed': endpoint, 'stable_tail_first_s': times[quiet] if endpoint else None,
                'stable_tail_indices': [segment[quiet]['index'], segment[quiet_end]['index']] if endpoint else None,
                'support_interval_s': [times[quiet], times[quiet_end]] if endpoint else None,
                'motion_observed_indices': [row['index'] for row in segment[first:final + 1]],
                'physical_motion_inferred': False,
                'endpoint_scope': 'independently_observed_stable_tail' if endpoint else 'clip_or_gap_censored'})
            if not endpoint:break
            # Confirmed-rest v3 resets the anchor at the observed support end.
            cursor, search_from = quiet_end, quiet_end + 1
    return episodes


def filter_raw_events(rows, raw_events, manifest_inputs, *, object_id,
                      rest_duration_s=.2, rest_radius_px=1.5, max_gap_s=.12):
    """Censor only retrospectively defined overshoot after a known interruption.

    Initiating inputs and stable-tail boundary inputs are excluded by strict
    inequalities. Uninterrupted overshoot and every other event stay intact.
    Unknown input provenance cannot suppress an event; ambiguous episode
    binding stays visible in ``unsupported`` and never becomes a pass.
    """
    if not isinstance(object_id, str) or not object_id:
        raise ValueError('Select one explicit independent raw object identity')
    for value in (rest_duration_s, rest_radius_px, max_gap_s):
        if not _number(value) or value <= 0:raise ValueError('Public episode limits must be positive and finite')
    if not isinstance(raw_events, list):raise ValueError('Independent raw events must be a list')
    commands, input_scope = _commands(manifest_inputs)
    episodes = _episodes(_segments(rows, object_id, max_gap_s), object_id, rest_duration_s, rest_radius_px)
    for episode in episodes:
        end = episode['stable_tail_first_s']
        inside = ([event for event in commands if episode['second_observed_point_s'] < event['time_s'] < end]
                  if end is not None else [])
        episode.update(interrupted_by_known_input=bool(inside), interrupting_inputs=deepcopy(inside),
                       interruption_predicate='second_observed_point < input < stable_tail_first (strict)')
    result, decisions, unsupported = [], [], []
    for index, event in enumerate(raw_events):
        if not isinstance(event, dict):raise ValueError('Independent raw event must be an object')
        if event.get('type') != 'overshoot' or event.get('object_ids') != [object_id]:
            result.append(deepcopy(event));continue
        ids = event.get('indices')
        matches = ([episode for episode in episodes if episode['endpoint_observed']
                    and all(i in episode['motion_observed_indices'] for i in ids)]
                   if isinstance(ids, list) and len(ids) == 2 and all(type(i) is int for i in ids) else [])
        own = matches[0] if len(matches) == 1 else None
        suppress = own is not None and own['interrupted_by_known_input']
        reason = ('known_input_interrupts_retrospective_endpoint' if suppress else
                  'uninterrupted_observed_episode' if own is not None else 'raw_episode_binding_unresolved')
        if own is None:unsupported.append({'event_index': index, 'reason': reason, 'matching_episode_count': len(matches)})
        decisions.append({'event_index': index, 'event_type': event['type'], 'indices': deepcopy(ids),
                          'decision': 'suppressed' if suppress else 'retained', 'reason': reason,
                          'episode_id': own['episode_id'] if own is not None else None,
                          'interrupting_public_input_indices': [value['public_input_index'] for value in own['interrupting_inputs']] if own else []})
        if not suppress:result.append(deepcopy(event))
    return {'kind': 'IndependentRawInputEpisodeReview', 'schema_version': 1, 'version': VERSION,
            'status': 'unsigned', 'events': result, 'episodes': episodes, 'decisions': decisions,
            'unsupported': unsupported, 'public_input_provenance': manifest_inputs['provenance'],
            'input_scope': input_scope, 'suppressed_count': sum(row['decision'] == 'suppressed' for row in decisions),
            'production_coordinates_used': False, 'production_events_used': False,
            'motion_times_rescaled': False, 'full_behavior_acceptance': False}
