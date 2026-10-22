"""Unsigned event aids from independent raw-pixel anchors.

This forward-window implementation does not call the production event detector
or consume its coordinates. Public event definitions must match before a
reviewer compares intervals. A proposal is never an acceptance receipt.
"""
from __future__ import annotations

import math
import statistics

import numpy as np

from .schema import VisualError

LEGACY = 'observed-rest-band-departure-v2'
CONFIRMED = 'observed-confirmed-rest-departure-v3'


def _policy(value):
    if value not in (LEGACY, CONFIRMED):
        raise VisualError('unsupported_event_definition', 'Raw review needs a supported explicit event definition.')
    return value


def relative_timing(left, right, cadence, *, policy=CONFIRMED):
    """Use the declared temporal resolution, without inventing subframe order."""
    _policy(policy)
    aa, ab = left
    ba, bb = right
    if not all(math.isfinite(x) for x in (*left, *right)) or aa > ab or ba > bb:
        raise VisualError('invalid_event_interval', 'Ordered finite intervals are required.')
    intervals = [x for x in cadence if math.isfinite(x) and 0 < x <= .12]
    resolution = min(.1, max(.05, 1.5 * statistics.median(intervals))) if intervals else .1
    if policy == LEGACY:
        resolution = 0.
    lag = [ba - ab, bb - aa]
    return {'lag_interval_s': lag,
            'order': 'before' if lag[0] > resolution else 'after' if lag[1] < -resolution else 'overlapping_uncertainty',
            'order_resolution_s': resolution,
            'uncertainty_s': (ab - aa) + (bb - ba),
            'timing_status': 'resolved' if (ab - aa) + (bb - ba) <= .1 + 1e-9 else 'unresolved'}


def _segment(rows, objects, duration, policy, radius):
    t = np.asarray([r['time_s'] for r in rows])
    p = np.asarray([[r['points'][objects[0]][k] for k in ('x', 'y')] for r in rows])
    if len(objects) == 2:
        p -= np.asarray([[r['points'][objects[1]][k] for k in ('x', 'y')] for r in rows])
    axis = int(np.argmax(np.ptp(p, axis=0)))
    out, cursor, search_from = [], 0, 0

    def emit(kind, a, b, **extra):
        out.append({'type': kind, 'object_ids': list(objects), 'indices': [int(a), int(b)],
                    'observed_interval_s': [float(t[a]), float(t[b])], 'predicate_version': policy, **extra})

    while cursor < len(t) - 1:
        departure = np.flatnonzero(np.linalg.norm(p[search_from:] - p[cursor], axis=1) > radius)
        if not len(departure):
            break
        start = search_from + int(departure[0])
        emit('motion_start', max(cursor, start - 1), start)
        quiet = quiet_end = None
        for i in range(start + 1, len(t)):
            end = int(np.searchsorted(t, t[i] + duration - 1e-9))
            if end >= len(t):
                break
            if np.linalg.norm(np.ptp(p[i:end + 1], axis=0)) <= radius:
                quiet, quiet_end = i, end
                break
        finish = quiet_end if quiet_end is not None else len(t) - 1
        step = p[start, axis] - p[max(cursor, start - 1), axis]
        direction = np.sign(step) if abs(step) / (t[start] - t[max(cursor, start - 1)]) > 5. else 0
        peak, turn = start, None
        for i in range(start + 1, finish + 1):
            delta = p[i, axis] - p[i - 1, axis]
            if abs(delta) / (t[i] - t[i - 1]) <= 5.:
                continue
            if direction == 0:
                direction, peak = np.sign(delta), i
            if np.sign(delta) == direction:
                if direction * (p[i, axis] - p[peak, axis]) >= 0:
                    peak = i
                turn = None
            else:
                if turn is None:
                    turn = i
                if direction * (p[peak, axis] - p[i, axis]) > radius:
                    emit('reversal', turn - 1, turn)
                    direction, peak, turn = np.sign(delta), i, None
        if quiet is None:
            break
        for kind in ('stop', 'settled'):
            emit(kind, quiet, min(quiet + 1, len(t) - 1), support_indices=[quiet, quiet_end])
        endpoint = np.median(p[quiet:quiet_end + 1], axis=0)
        vector = endpoint - p[start - 1]
        length = np.linalg.norm(vector)
        if length > radius:
            projection = (p[start - 1:quiet + 1] - endpoint) @ (vector / length)
            overshoot = np.flatnonzero(projection > radius)
            if len(overshoot):
                first = start - 1 + int(overshoot[0])
                emit('overshoot', max(start - 1, first - 1), first)
        cursor = quiet_end if policy == CONFIRMED else quiet
        search_from = quiet_end + 1
    return out


def candidates(rows, objects, rest_duration=.2, *, active_intervals_s=None,
               max_gap_s=.12, policy=CONFIRMED, rest_radius_px=1.5):
    _policy(policy)
    if not 1 <= len(objects) <= 2 or rest_duration <= 0 or rest_radius_px <= 0 or max_gap_s <= 0:
        raise VisualError('invalid_event_definition', 'Valid raw review objects and positive limits are required.')
    active_intervals_s = active_intervals_s or {}
    segments, segment, previous_time = [], [], None
    for row in rows:
        stamp = row['time_s']
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or previous_time is not None and stamp <= previous_time:
            raise VisualError('invalid_frame_time', 'Raw review frame times must strictly increase.')
        previous_time = stamp
        valid = True
        for oid in objects:
            point = row.get('points', {}).get(oid, {})
            valid &= all(type(point.get(k)) in (int, float) and math.isfinite(point[k]) for k in ('x', 'y'))
            intervals = active_intervals_s.get(oid)
            if intervals is not None:
                valid &= any(a <= stamp <= b for a, b in intervals)
        if not valid or segment and stamp - segment[-1]['time_s'] > max_gap_s:
            if segment:
                segments.append(segment)
                segment = []
        if valid:
            segment.append(row)
    if segment:
        segments.append(segment)
    result = []
    for segment in segments:
        if len(segment) < 2:
            continue
        for event in _segment(segment, objects, rest_duration, policy, rest_radius_px):
            event['indices'] = [segment[i]['index'] for i in event['indices']]
            if 'support_indices' in event:
                event['support_indices'] = [segment[i]['index'] for i in event['support_indices']]
            event.update(status='unsigned', raw_segment_indices=[segment[0]['index'], segment[-1]['index']],
                         physical_motion_inferred=False)
            result.append(event)
    return result
