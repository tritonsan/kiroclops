"""Read-only acceptance interpretation; never rescoring or changing saved gates.

An absent holdout, invalid recording, and observed motion mismatch are different
claims. These axes describe the evidence scope; the legacy aggregate stays intact.
"""
from __future__ import annotations


_MEASUREMENT_ERRORS = frozenset({
    'capture_timing_failure', 'tracking_failure', 'insufficient_observability',
    'invalid_package', 'package_integrity_failure', 'missing_frame',
    'invalid_timestamps', 'capture_failure', 'recording_failure',
})


def recording_fact(record, *, job_id, role, context):
    """Keep portable recorded facts, not private engine/DOM coordinates."""
    return {'job_id': job_id, 'role': role, 'context': context,
            'scenario_id': record.get('scenario', {}).get('id'),
            'measurement_status': record.get('measurement_status'),
            'timing_status': (record.get('timing') or {}).get('status'),
            'functional_status': (record.get('functional') or {}).get('status')}


def _axis(status, scope, reason, evidence=None):
    return {'status': status, 'scope': scope, 'reason_codes': [reason],
            'evidence': evidence or {}}


def _verdict(flags):
    if any(flag is False for flag in flags):
        return 'fail'
    return 'pass' if flags and all(flag is True for flag in flags) else 'incomplete'


def _boolean_status(value):
    if value in ('pass', 'ok'):
        return True
    if value in ('fail', 'rejected', 'invalid', 'insufficient_observability'):
        return False
    return None


def _motion_flags(details):
    """Explicit pixel/dynamic gates only; an application failure is separate."""
    if details.get('motion_status') in ('pass', 'fail'):
        flags = [_boolean_status(details['motion_status'])]
        if 'visual_wall_status' in details:
            flags.append(_boolean_status(details['visual_wall_status']))
        return flags
    compared = details.get('comparison')
    if isinstance(compared, dict):
        return [_boolean_status(compared.get('numerical_status'))]
    flags = []
    for row in details.get('comparisons', []):
        suite_game = bool(details.get('suite_sha256') and row.get('stage') in ('jump', 'ground'))
        if suite_game:
            # Project these saved explicit gates; never rescore historical rows.
            flags.append(_boolean_status(row.get('numerical_status')))
        windows = row.get('windows') or ([row['pixel_comparison']] if row.get('pixel_comparison') else [])
        for window in windows:
            flags.append(_boolean_status(window.get('numerical_status')))
        for dynamic in row.get('dynamics', []):
            if dynamic.get('observable') is True:
                flags.append(dynamic.get('pass'))
        flags.extend(item.get('pass') for item in row.get('relative_timing', []))
        if row.get('improvement_required') is True:
            flags.append(row.get('gates', {}).get('improvement') if suite_game else row.get('improvement_pass'))
        elif 'improvement_required' not in row and row.get('improvement'):
            flags.extend(item.get('pass') for item in row['improvement'])
        geometry = row.get('svg_geometry_preservation')
        if isinstance(geometry, dict):
            flags.append(geometry.get('pass'))
    # Old saved partial-media schemas may expose only this explicit calibration
    # verdict. Never substitute the aggregate `pass: false` for an absent holdout.
    if not flags and details.get('calibration_pass') is True:
        flags.append(True)
    if details.get('suite_sha256') and 'visual_wall_status' in details:
        flags.append(_boolean_status(details['visual_wall_status']))
    return flags


def _recorded_native_turn_completed(launch):
    """Lifecycle evidence only; a failed worker still requires full handoff audit."""
    if launch.get('status') == 'completed':
        return launch.get('native_turn_status') in (None, 'completed')
    handoff = launch.get('postturn_finalization') or {}
    cleanup = launch.get('terminal_cleanup') or {}
    request = cleanup.get('request') or {}
    return (launch.get('status') == 'finalization_failed'
            and launch.get('native_turn_status') == 'completed'
            and handoff.get('status') == 'terminal'
            and handoff.get('job_status') in ('failed', 'cancelled', 'interrupted')
            and cleanup.get('status') == 'pass'
            and request.get('preserved_finalization') is True)


def summarize_acceptance(state, report, *, recordings=()):
    """Project saved results into scoped axes without modifying their verdicts.

    ``report`` contains the existing source/result checks and native/review
    validation outcomes from reporting.build_report. This is a presentation
    contract, not a replacement for those validators or experiment admission.
    """
    details = report.get('acceptance_details') or {}
    final = state.get('final') or {}
    final_id = final.get('job_id')
    selected = state.get('selected_candidate_id')
    scope = 'selected_final_recordings' if selected and final_id else 'candidate_attempts'
    relevant = [dict(r) for r in recordings if final_id and r.get('job_id') == final_id]
    errors = []
    if final_id:
        errors.append((details.get('error') or final.get('error') or {}).get('code'))
    else:
        errors.extend((c.get('error') or {}).get('code') for c in state.get('candidates', []))
    errors = sorted({e for e in errors if e})
    measurement_errors = sorted(set(errors) & _MEASUREMENT_ERRORS)
    measured = []
    media_reference = state.get('experiment', {}).get('reference_mode') in ('media_bundle', 'media_suite')
    for record in relevant:
        measured.append(_boolean_status(record.get('measurement_status')))
        # Uploaded references carry validated source timestamps, not a new
        # browser-capture timing verdict. Preserve that distinction rather than
        # demanding a native-capture status the reference adapter never emits.
        source_timestamps = (media_reference and record.get('role') == 'reference'
                             and record.get('timing_status') == 'source_timestamps')
        if record.get('timing_status') is not None and not source_timestamps:
            measured.append(_boolean_status(record['timing_status']))
    for row in details.get('comparisons', []):
        if 'measurement_pass' in row:
            measured.append(row['measurement_pass'])
        if 'measurement_status' in row:
            measured.append(_boolean_status(row['measurement_status']))
        windows = row.get('windows') or ([row['pixel_comparison']] if row.get('pixel_comparison') else [])
        for window in windows:
            if 'measurement_status' in window:
                measured.append(_boolean_status(window['measurement_status']))
    measurement_status = ('invalid' if measurement_errors or False in measured else
                          'pass' if measured and all(x is True for x in measured) else
                          'incomplete' if relevant or details else 'not_evaluated')
    axes = {'measurement_validity': _axis(measurement_status, scope,
        'recording_or_tracking_invalid' if measurement_status == 'invalid' else
        'recorded_measurement_checks_passed' if measurement_status == 'pass' else 'measurement_evidence_incomplete',
        {'recordings': relevant, 'error_codes': errors,
         'timing_scope': 'native_target_capture_checks_and_preserved_source_timestamps' if media_reference else 'recorded_adapter_checks'})}

    motion = _motion_flags(details)
    behavior_status = ('not_evaluated' if measurement_status == 'invalid' or not motion else
                       _verdict(motion) if measurement_status == 'pass' else 'incomplete')
    is_partial = details.get('acceptance_status') == 'partial_unobserved_holdout'
    unseen_absent = is_partial or details.get('heldout_status') == 'not_provided' or state.get('experiment', {}).get('heldout_status') == 'not_provided'
    required, passed = details.get('required'), details.get('passed')
    has_unseen_count = type(required) is int and required > 0 and type(passed) is int and passed >= 0
    unseen_executed = bool(not unseen_absent and final_id and final.get('status') == 'succeeded' and has_unseen_count)
    behavior_scope = 'supplied_clip_calibration_only' if is_partial else 'recorded_selected_candidate_scenarios'
    axes['observed_behavior'] = _axis(behavior_status, behavior_scope,
        'measurement_invalid_no_behavior_verdict' if measurement_status == 'invalid' else
        'observed_behavior_matches' if behavior_status == 'pass' else
        'observed_behavior_mismatch' if behavior_status == 'fail' else 'no_qualified_behavior_verdict',
        {'job_id': final_id, 'candidate_id': selected, 'saved_calibration_pass': details.get('calibration_pass'),
         'saved_motion_status': details.get('motion_status'), 'scored_checks': len(motion)})

    functional = [_boolean_status(r['functional_status']) for r in relevant
                  if r['role'] in ('candidate', 'final') and r.get('functional_status') is not None]
    preservation = details.get('preservation')
    if isinstance(preservation, dict):
        functional.append(_boolean_status(preservation.get('status')))
    for row in details.get('comparisons', []):
        if isinstance(row.get('functional'), dict):
            functional.append(_boolean_status(row['functional'].get('status')))
    integrity_status = _verdict(functional) if functional else 'not_evaluated'
    axes['application_integrity'] = _axis(integrity_status, 'executed_application_checks_only',
        'executed_application_checks_passed' if integrity_status == 'pass' else
        'application_check_failed' if integrity_status == 'fail' else 'application_evidence_incomplete',
        {'checks_recorded': len(functional), 'unseen_interactions_included': unseen_executed})

    run = report.get('kiro_run') or {}
    response = run.get('response') or {}
    generation = (run.get('stream_configuration') or {}).get('generation_configuration_verified')
    completed = (_recorded_native_turn_completed(run) and response.get('stopReason') == 'end_turn'
                 and generation is True and (run.get('terminal_cleanup') or {}).get('status') == 'pass')
    axes['model_completion'] = _axis('pass' if completed else 'incomplete' if run else 'not_evaluated',
        'recorded_native_turn_lifecycle_not_audit_certification',
        'normal_model_turn_end_recorded' if completed else 'normal_model_turn_end_not_established',
        {'launch_status': run.get('status'), 'native_turn_status': run.get('native_turn_status'),
         'postturn_job_status': (run.get('postturn_finalization') or {}).get('job_status'),
         'stop_reason': response.get('stopReason'),
         'model': run.get('model'), 'effort': run.get('effort'),
         'generation_configuration_verified': generation,
         'root_cause_of_missing_evidence': 'unknown'})

    native = report.get('native_run_status')
    axes['native_evidence'] = _axis('pass' if native == 'pass' else 'invalid' if native == 'fail' else 'incomplete',
        'native_run_and_tool_evidence_validation',
        'native_evidence_validated' if native == 'pass' else 'native_evidence_invalid' if native == 'fail' else 'native_evidence_missing',
        {'validator_status': native, 'validation': report.get('native_run')})

    unseen_status = ('not_evaluated' if unseen_absent or not final_id else
                     'invalid' if measurement_status == 'invalid' else
                     ('pass' if passed == required and details.get('pass') is True else 'fail')
                     if unseen_executed and measurement_status == 'pass' else 'incomplete')
    axes['unseen_validation'] = _axis(unseen_status, 'sealed_candidate_heldout_scenarios',
        'unseen_tests_not_provided' if unseen_absent else 'unseen_checks_passed' if unseen_status == 'pass' else
        'unseen_checks_failed' if unseen_status == 'fail' else 'unseen_evidence_incomplete',
        {'required': required, 'passed': passed, 'heldout_status': details.get('heldout_status')})

    visual, coverage = report.get('independent_review_status'), report.get('review_coverage_status')
    visual_status = 'pass' if visual == coverage == 'pass' else 'invalid' if 'fail' in (visual, coverage) else 'incomplete'
    axes['independent_visual_review'] = _axis(visual_status, 'image_bound_independent_points_and_events',
        'independent_review_complete' if visual_status == 'pass' else 'independent_review_invalid' if visual_status == 'invalid' else 'independent_review_pending',
        {'review_status': visual, 'coverage_status': coverage})
    source = report.get('source_review_status')
    axes['source_review'] = _axis('pass' if source == 'pass' else 'invalid' if source == 'fail' else 'incomplete',
        'selected_source_behavior_implementation_review',
        'source_review_passed' if source == 'pass' else 'source_review_invalid' if source == 'fail' else 'source_review_pending',
        {'review_status': source, 'candidate_sha256': report.get('candidate_sha256')})

    certified = (all(axis['status'] == 'pass' for axis in axes.values())
                 and report.get('overall_acceptance') == 'pass' and details.get('pass') is True)
    summary_status = ('fully_validated' if certified else 'measurement_invalid' if measurement_status == 'invalid' else
                      'behavior_mismatch' if behavior_status == 'fail' else 'not_fully_validated')
    title = ('All scoped acceptance checks passed' if certified else
             'Recording invalid; behavior was not judged' if measurement_status == 'invalid' else
             'Observed behavior differs from the reference' if behavior_status == 'fail' else
             'Observed behavior matches; validation remains incomplete' if behavior_status == 'pass' else
             'Validation remains incomplete')
    return {'schema_version': 1, 'kind': 'AcceptanceInterpretation', 'axes': axes,
            'summary': {'status': summary_status, 'title': title, 'certified_success': certified,
                        'unresolved_axes': [name for name, axis in axes.items() if axis['status'] != 'pass'],
                        'legacy_aggregate': report.get('overall_acceptance'),
                        'legacy_aggregate_scope': 'unchanged_strict_combined_certification_not_a_behavior_verdict'},
            'policy': {'rescored': False, 'thresholds_changed': False, 'historical_results_rewritten': False}}
