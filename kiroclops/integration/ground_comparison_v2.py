"""Explicit new speed-observability policy over unchanged ground/jump gates.

Never used by old sessions. Short/reversal trajectories still require every
curve, endpoint and braking gate; steady speed is only asserted for fixed input
legs of at least 30 ticks. Unresolved long-leg speed cannot pass acceptance.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from .ground_speed_v2 import LONG_HOLD_POLICY, compare_speeds, estimate_speed

POLICY_ID = 'ground-speed-long-hold-v2'


def speed_triplet(original, reference, before, target, *, require_improvement=True):
    # Reuse the exact frozen curve/endpoint/coast/improvement calculations.
    # The substituted scalar disables only its obsolete eight-frame speed
    # calculation; the native report and all other metrics stay unchanged.
    def curve_input(report):
        return {**report, 'metrics': {**report['metrics'], 'speed_body_s': 1.0}}
    row = original(curve_input(reference), before, curve_input(target),
                   require_improvement=require_improvement)
    speed = compare_speeds(reference, target, policy=LONG_HOLD_POLICY)
    if speed['status'] == 'invalid':
        raise ValueError('invalid v2 speed measurement: ' + '; '.join(speed['errors']))
    row.update(speed_policy=POLICY_ID, speed_comparison=speed,
               speed_eligible=speed['speed_eligible'],
               speed_relative_error=speed['speed_relative_error'],
               speed_measurement_status=speed['status'],
               legacy_last_eight_speed_body_s={
                   'reference': reference['metrics'].get('speed_body_s'),
                   'target': target['metrics'].get('speed_body_s'),
                   'acceptance_use': False})
    row['gates']['speed'] = speed['status'] in {'pass', 'not_applicable'}
    row['pass'] = all(row['gates'].values())
    return row


def classify_comparison(result):
    """Keep uncertain measurement separate from a measured motion failure."""
    result.update(measurement_policy=POLICY_ID, speed_policy=LONG_HOLD_POLICY.record())
    rows = result['scenario_checks']
    unresolved = [row for row in rows if row.get('speed_measurement_status') == 'indeterminate']
    failed = [row for row in rows if row.get('speed_measurement_status') == 'fail'
              or any(not value for key, value in row['gates'].items() if key != 'speed')]
    invalid = any('invalid v2 speed' in error for error in result['errors'])
    incomplete = result['coverage']['complete_triplets'] < result['coverage']['required_triplets']
    result['speed_measurement_status'] = ('invalid' if invalid else 'indeterminate' if unresolved
                                           else 'incomplete' if incomplete else 'resolved')
    if unresolved and not failed:
        result['errors'] = [error for error in result['errors']
                            if error != 'one or more horizontal transfer gates failed']
        if not result['errors']:
            result['numerical_status'] = 'pending'
            result['status'] = 'fail' if result['manual_validation_status'] == 'fail' else 'pending'
            result['success'] = False
            result['pending'].append('Long-hold speed remains unresolved at native pixel precision')
    result['gates']['speed_eligibility'] = 'last held leg >=30 ticks; fixed last two-thirds, maximum32/minimum20 frames'
    result['gates']['speed_decision'] = 'every rounding-feasible pair must satisfy the unchanged 10% limit'
    return result


@contextmanager
def repaired_measurements():
    """Process-local, explicitly invoked policy; restores all legacy entrypoints."""
    from kiroclops.ground import comparison
    from . import ground_worker
    original_triplet, original_compare = comparison._triplet, comparison.compare_runs
    original_prepare, original_combine = ground_worker._prepare_feedback, ground_worker.combine_comparisons

    def triplet(*args, **kwargs):
        return speed_triplet(original_triplet, *args, **kwargs)

    def compare(*args, **kwargs):
        return classify_comparison(original_compare(*args, **kwargs))

    def prepare(session, reference, before, check):
        from .storage import read_json
        result = original_prepare(session, reference, before, check)
        result['measurement_policy'] = POLICY_ID
        result['speed_policy'] = LONG_HOLD_POLICY.record()
        result['steady_speed_measurements'] = {
            role: [estimate_speed(read_json(Path(path) / 'analysis.json'), policy=LONG_HOLD_POLICY)
                   for path in paths]
            for role, paths in (('reference', reference['runs']), ('baseline', before))}
        result['speed_interpretation'] = ('Only >=30 tick legs have a steady-speed gate. The old speed_body_s scalar in raw exports '
            'is last-eight-frame diagnostics; use speed_comparison in evaluation for the registered v2 decision.')
        return result

    def combine(ground, jump, drift):
        result = original_combine(ground, jump, drift)
        result.update(measurement_policy=POLICY_ID, speed_policy=LONG_HOLD_POLICY.record())
        if ground.get('numerical_status') == 'pending' and jump.get('numerical_status') != 'fail':
            result['numerical_status'] = 'pending'
            result['status'] = 'fail' if result['manual_validation_status'] == 'fail' else 'pending'
            result['success'] = False
        return result

    comparison._triplet, comparison.compare_runs = triplet, compare
    ground_worker._prepare_feedback, ground_worker.combine_comparisons = prepare, combine
    try:
        yield
    finally:
        comparison._triplet, comparison.compare_runs = original_triplet, original_compare
        ground_worker._prepare_feedback, ground_worker.combine_comparisons = original_prepare, original_combine
