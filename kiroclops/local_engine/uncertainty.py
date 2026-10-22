"""Read-only tolerance diagnostics; these never change an acceptance decision.

Pixel rounding examples and repeated recordings are not ground-truth accuracy.
In particular, a planned manual-review tolerance is not measured tracker noise.
"""
from __future__ import annotations

from copy import deepcopy
import math
import statistics

from kiroclops.visual.schema import digest


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _comparisons(value):
    return value if isinstance(value, list) else [value]


def assess_measurement_limits(comparison, contract, *, independent_review=None,
                              reproducibility=None, timing=None):
    """Explain persisted limits in natural pixels without manufacturing certainty.

    ``independent_review`` is an optional summary of separately verified evidence,
    not a boolean approval. Merely supplying it cannot qualify uncertainty: point
    errors alone do not establish a confidence bound or reference/target bias.
    All input objects remain unchanged; this function has no filesystem effects.
    """
    contract = contract or {}
    rows = []
    for comparison_index, item in enumerate(_comparisons(comparison)):
        for window in item.get('windows', []):
            for row in window.get('object_windows', []):
                scale = row.get('scale_px')
                limits = row.get('limits', {})
                rmse_limit = limits.get('rmse_limit')
                endpoint_limit = limits.get('endpoint_limit')
                good = _number(scale) and scale > 0 and _number(rmse_limit) and rmse_limit >= 0
                relative = bool(row.get('relative_to_reference'))
                result = {
                    'comparison_index': comparison_index,
                    'object_id': row.get('reference_id'),
                    'window_id': row.get('window_id'),
                    'segment_index': row.get('segment_index'),
                    'relative_to_object': row.get('relative_to_reference'),
                    'reference_package_sha256': window.get('reference_package_sha256'),
                    'candidate_package_sha256': window.get('candidate_package_sha256'),
                    'normalization_scale_px': scale if _number(scale) else None,
                    'reference_motion_extent_px': scale if good and scale > 1.5 and
                        rmse_limit == contract.get('position_rmse_fraction') else None,
                    'extent_semantics': 'Diagonal of observed x/y ranges; not cumulative path length. '
                        'A static normalization sentinel is not a measured travel distance.',
                    'current_rmse_limit_px': scale * rmse_limit if good else None,
                    'current_endpoint_limit_px': scale * endpoint_limit if good and _number(endpoint_limit) else None,
                    'endpoint_required': bool(row.get('endpoint_required')),
                    'measured_rmse_px': scale * row['position_rmse'] if good and _number(row.get('position_rmse')) else None,
                    'measured_endpoint_error_px': scale * row['endpoint_error'] if good and _number(row.get('endpoint_error')) else None,
                    'original_gate_pass': row.get('pass'),
                    'assessment': 'uncertainty_unqualified',
                    'nominal_pixel_sensitivity': None,
                }
                if good:
                    # This is a hypothetical independent uniform nearest-pixel
                    # rounding model. Real color-mass centroids are subpixel and
                    # can have different, correlated, systematic errors.
                    result['nominal_pixel_sensitivity'] = {
                        'status': 'hypothetical_not_empirical_uncertainty',
                        'one_pixel_normalized': 1. / scale,
                        'one_pixel_to_rmse_limit_ratio': 1. / (scale * rmse_limit) if rmse_limit > 0 else None,
                        'independent_nearest_pixel_pair_2d_rms_px': math.sqrt(2. / 3. if relative else 1. / 3.),
                        'assumptions': 'Independent uniform rounding in [-0.5,+0.5] px per coordinate; '
                            + ('four measured anchors form two relative positions.' if relative else 'two measured anchors are compared.'),
                        'not_a_minimum_achievable_error': True,
                    }
                rows.append(result)
    return {
        'schema_version': 1, 'kind': 'ToleranceAssessment',
        'status': 'uncertainty_unqualified', 'acceptance_changed': False,
        'comparison_sha256': digest(comparison), 'contract_sha256': digest(contract),
        'object_windows': rows,
        'independent_measurement_error': deepcopy(independent_review) if independent_review else {
            'status': 'unknown', 'reason': 'No independently verified image control-point error evidence was supplied.'},
        'reproducibility': deepcopy(reproducibility), 'timing': deepcopy(timing),
        'interpretation': [
            'Subpixel localization is possible; a subpixel tolerance is not automatically impossible.',
            'The planned max(1.5 px, 0.5% reference extent) manual check is an acceptance limit, not an observed noise floor.',
            'Repeated same-source captures combine execution, input, sampling and tracking variation; they cannot establish truth accuracy.',
            'Unknown empirical uncertainty prevents claiming that current numeric margins are statistically qualified.',
        ],
        'prospective_protocol': [
            'Freeze a new protocol before any new model run; preserve every existing result under its original rules.',
            'Collect independently marked, image-hash-bound controls across objects, directions, acceleration, braking, overshoot and settled views; record reviewer disagreement.',
            'Use known image transforms and independent raw-image marks to estimate bias and dispersion separately; validate subpixel anchors and relative child-minus-parent covariance.',
            'Repeat identical-source recordings with actual input/clock evidence; estimate capture variability separately from tracker accuracy.',
            'Estimate uncertainty of the final per-window metric, including temporal interpolation and both reference and candidate errors; do not substitute median point error for RMSE uncertainty.',
            'Pre-register any absolute pixel error floor only after calibration supports it; do not install a numeric floor from the nominal rounding example.',
            'For a separately validated metric confidence interval, accept only when its upper bound is within the behavioral limit, reject when its lower bound exceeds it, otherwise report inconclusive.',
            'Evaluate event-aware sampling prospectively on both correct and broken behaviors. Require image-derived support around inputs, departures, reversals and settling; missing images cannot prove rest.',
        ],
    }


def assess_timing_scope(manifest, observations, saved_timing, *, response_window_s=1.8):
    """Describe fixed response-window coverage using actual image evidence only.

    Same endpoint images never prove there was no intervening motion. The
    diagnostics intentionally do not mark a gap safe or waive the saved gate.
    """
    frames = manifest.get('frames', [])
    times = [f.get('time_s') for f in frames]
    if (len(times) < 2 or any(not _number(t) for t in times) or
            any(b <= a for a, b in zip(times, times[1:]))):
        raise ValueError('Actual strictly increasing frame timestamps are required')
    if observations.get('package_sha256') != manifest.get('package_sha256'):
        raise ValueError('Observation and image package hashes do not match')
    if not _number(response_window_s) or response_window_s <= 0:
        raise ValueError('The diagnostic response window must be positive')
    tracks = {o['id']: {f['index']: f for f in o.get('frames', [])} for o in observations.get('objects', [])}
    windows = [(e['time_s'], e['time_s'] + response_window_s) for e in manifest.get('inputs', {}).get('events', [])
               if e.get('action') in ('click', 'drag_end')]
    details = []
    for a, b in zip(frames, frames[1:]):
        active_overlap = any(a['time_s'] < hi and b['time_s'] > lo for lo, hi in windows)
        valid = bool(tracks)
        displacement = []
        for track in tracks.values():
            aa, bb = track.get(a['index']), track.get(b['index'])
            if not aa or not bb or not aa.get('valid') or not bb.get('valid') or any(
                    not _number(f.get(k)) for f in (aa, bb) for k in ('x', 'y')):
                valid = False
                continue
            displacement.append(math.hypot(bb['x']-aa['x'], bb['y']-aa['y']))
        same_image = bool(a.get('sha256')) and a.get('sha256') == b.get('sha256')
        state = ('pixel_endpoints_identical' if same_image else
                 'tracking_incomplete' if not valid else
                 'selected_anchors_changed' if any(d > 1e-9 for d in displacement) else
                 'selected_anchors_unchanged_other_pixels_may_change')
        details.append({'frame_indices': [a['index'], b['index']],
                        'time_interval_s': [a['time_s'], b['time_s']],
                        'gap_s': b['time_s']-a['time_s'],
                        'overlaps_fixed_response_window': active_overlap,
                        'endpoint_evidence': state,
                        'maximum_observed_anchor_displacement_px': max(displacement) if valid else None,
                        'intervening_motion': 'unknown'})
    counts = {}
    durations = {}
    for row in details:
        if row['overlaps_fixed_response_window']:
            state = row['endpoint_evidence']
            counts[state] = counts.get(state, 0)+1
            durations[state] = durations.get(state, 0.)+row['gap_s']
    return {'kind': 'TimingScopeDiagnostic', 'package_sha256': manifest['package_sha256'],
            'observations_sha256': digest(observations), 'saved_timing': deepcopy(saved_timing),
            'response_window_s': response_window_s, 'fixed_windows_s': windows,
            'endpoint_evidence_counts': counts, 'overlapping_interval_duration_s': durations,
            'largest_intervals': sorted(details, key=lambda r:r['gap_s'], reverse=True)[:10],
            'all_intervals': details, 'gate_waivers': [], 'dom_oracle_used': False,
            'settled_state_certified': False,
            'note': 'Fixed windows include sampled stationary views. Equal endpoints do not reveal intervening motion; '
                    'no failed timing gate is excused. Overlapping-interval totals are diagnostic, not exact window duration.'}


def summarize_same_source_pair(first, second, comparison):
    """Reject unrelated sources before calling a pair a reproducibility sample."""
    source = first.get('source_sha256')
    if not source or source != second.get('source_sha256'):
        raise ValueError('Reproducibility requires the identical candidate source hash')
    first_package = first['observations'].get('package_sha256')
    second_package = second['observations'].get('package_sha256')
    if not first_package or not second_package or first_package == second_package:
        raise ValueError('Use two distinct image packages, not one recording compared with itself')
    rows = []
    for item in _comparisons(comparison):
        for window in item.get('windows', []):
            if (window.get('reference_package_sha256'), window.get('candidate_package_sha256')) != (first_package, second_package):
                raise ValueError('Comparison belongs to a different pair of image packages')
            for row in window.get('object_windows', []):
                error, scale = row.get('position_rmse'), row.get('scale_px')
                if _number(error) and _number(scale):
                    rows.append({'object_id': row['reference_id'], 'window_id': row['window_id'],
                                 'normalized_rmse': error, 'rmse_px': error*scale, 'scale_px': scale})
    return {'kind': 'SameSourceReproducibility', 'source_sha256': source,
            'package_sha256s': [first_package, second_package],
            'observation_sha256s': [digest(first['observations']), digest(second['observations'])],
            'comparison_sha256': digest(comparison), 'pair_count': 1, 'object_windows': rows,
            'mean_normalized_rmse': statistics.mean(r['normalized_rmse'] for r in rows) if rows else None,
            'accuracy_inferred': False, 'confidence_bound_estimated': False,
            'scope': 'One unchanged-source pair; variation includes runtime/input/capture/tracking. No ground-truth accuracy or general repeatability claim.'}
