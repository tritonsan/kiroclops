"""Explain existing motion gates without changing scores or acceptance limits."""
from __future__ import annotations

import math


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def explain_comparison(comparison, reference_inputs=(), candidate_inputs=()):
    """Expose failed object/window gates, native-pixel margins and input history.

    This is diagnostic evidence, not an uncertainty bound or a new acceptance
    policy. Different preceding input intervals can change an interrupted
    response even when each response is aligned to its own triggering input.
    """
    failures = []
    scored_windows = set()
    for window in comparison.get('windows', []):
        for row in window.get('object_windows', []):
            scored_windows.add(row.get('window_id'))
            scale = row.get('scale_px')
            limits = row.get('limits', {})
            for gate, passed in row.get('gates', {}).items():
                if passed is not False:
                    continue
                metric, limit = {
                    'position': ('position_rmse', 'rmse_limit'),
                    'size': ('size_rmse', 'size_rmse_limit'),
                    'endpoint': ('endpoint_error', 'endpoint_limit'),
                    'coverage': ('valid_time_ratio', None),
                }.get(gate, (None, None))
                value = row.get(metric) if metric else None
                bound = limits.get(limit) if limit else None
                dimensional = gate in ('position', 'size', 'endpoint') and _finite(scale)
                coverage_missing = row.get('gates', {}).get('coverage') is False
                failures.append({
                    'category': 'measurement_coverage' if gate == 'coverage' or coverage_missing else 'motion_mismatch',
                    'gate': gate, 'object_id': row.get('reference_id'),
                    'relative_to': row.get('relative_to_reference'),
                    'window_id': row.get('window_id'), 'segment_index': row.get('segment_index'),
                    'time_window_s': row.get('time_window_s'),
                    'coverage_sufficient': not coverage_missing,
                    'measured': value, 'limit': bound,
                    'measured_px': value * scale if dimensional and _finite(value) else None,
                    'limit_px': bound * scale if dimensional and _finite(bound) else None,
                })
    for row in comparison.get('dynamics', []):
        if row.get('observable') and row.get('pass') is False:
            failures.append({'category': 'motion_dynamics', 'object_id': row.get('object_id'),
                             'window_id': row.get('window_id'), 'details': dict(row)})
    if comparison.get('improvement_required'):
        for row in comparison.get('baseline_coverage_failures', []):
            failures.append({'category': 'baseline_measurement_coverage',
                             'object_id': row.get('reference_id'), 'window_id': row.get('window_id'),
                             'time_window_s': row.get('time_window_s'),
                             'valid_time_ratio': row.get('valid_time_ratio')})
        for row in comparison.get('improvement_gates', []):
            if row.get('pass') is False:
                failures.append({'category': 'insufficient_improvement', 'object_id': row.get('object_id'),
                                 'window_id': row.get('window_id'), 'details': dict(row)})
    histories = []
    for i, (ref, actual) in enumerate(zip(reference_inputs, candidate_inputs)):
        identity_matches = (ref.get('action'), ref.get('selector')) == (actual.get('action'), actual.get('selector'))
        ref_interval = actual_interval = None
        if i and all(_finite(v) for v in (ref.get('time_s'), actual.get('time_s'),
                                         reference_inputs[i-1].get('time_s'), candidate_inputs[i-1].get('time_s'))):
            ref_interval = ref['time_s'] - reference_inputs[i-1]['time_s']
            actual_interval = actual['time_s'] - candidate_inputs[i-1]['time_s']
        histories.append({'input_index': i, 'action': ref.get('action'),
                          'identity_matches': identity_matches,
                          'reference_previous_interval_s': ref_interval,
                          'candidate_previous_interval_s': actual_interval,
                          'previous_interval_difference_s': actual_interval-ref_interval if ref_interval is not None else None,
                          'has_scored_response_window': f'input-{i}' in scored_windows})
    return {
        'kind': 'ComparisonDiagnostic', 'acceptance_changed': False,
        'original_pass': comparison.get('pass'), 'failed_gates': failures,
        'unobservable_dynamics': [dict(r) for r in comparison.get('dynamics', []) if not r.get('observable')],
        'input_count_matches': len(reference_inputs) == len(candidate_inputs),
        'input_history': histories,
        'limitations': [
            'Natural-pixel errors are reported under the original limits; no pixel floor is applied.',
            'Input-history differences are observations, not proof of the cause of a motion mismatch.',
            'Unobservable dynamics are not failed motion gates and do not certify unseen behavior.',
            'Actions without a scored response window are not certified by the curve comparison.',
        ],
    }
