"""Deterministic advice from an explicit calibration pool and bound measurements.

No reference discovery, capture, fitting, held-out data, or code generation lives
here. Advice never changes the benchmark's mandatory complete calibration matrix.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json


def _scenario(value):
    if not isinstance(value, dict) or value.get('split') != 'calibration':
        raise VisualError('invalid_calibration_pool', 'Only explicitly declared calibration scenarios are allowed.')
    # A rendering role is not a different input recipe or test case.
    return {k: deepcopy(v) for k, v in value.items() if k != 'variant'}


def _declared_pool(value):
    # Older API clients could store display labels instead of runnable recipes.
    # Preserve their inspection compatibility without inventing test inputs.
    return value if isinstance(value, list) and all(isinstance(case, dict) for case in value) else []


def _tags(case):
    tags = set()
    named = {'#open-panel': 'panel_open', '#close-panel': 'panel_close',
             '#add-node': 'node_add', '#remove-node': 'node_remove'}
    for event in case.get('events', []):
        action = event.get('action')
        if action == 'click':
            tags.add(named.get(event.get('selector'), 'click:' + str(event.get('selector'))))
        elif action in ('drag_start', 'drag_move', 'drag_end'):
            tags.add({'drag_start': 'drag_grab', 'drag_move': 'drag_motion', 'drag_end': 'drag_release'}[action])
        elif action in ('jump', 'left', 'right'):
            tags.add(action + ('_press' if event.get('pressed') else '_release'))
    if 'hold_ticks' in case:
        tags.add('jump_hold_ticks:' + str(case['hold_ticks']))
    elif any(event.get('action') in ('left', 'right') for event in case.get('events', [])):
        presses = [e['tick'] for e in case.get('events', []) if e.get('pressed') and 'tick' in e]
        releases = [e['tick'] for e in case.get('events', []) if e.get('pressed') is False and 'tick' in e]
        if presses and releases:
            tags.add('ground_input_ticks:' + str(max(releases) - min(presses)))
    if 'initial_node_count' in case and 'expected_final_node_count' in case:
        tags.add(f"node_count:{case['initial_node_count']}->{case['expected_final_node_count']}")
    if case.get('viewport'):
        tags.add('viewport:' + 'x'.join(str(n) for n in case['viewport']))
    return sorted(tags or {'declared_response'})


def recommend_calibration(calibration, *, source_sha256, measurements=(), blocked_reason=None):
    """Pure pool ranking; a measured mismatch is covered, never 'untested'."""
    cases = [_scenario(case) for case in calibration]
    keys = [digest(case) for case in cases]
    if len(set(keys)) != len(keys):
        raise VisualError('invalid_calibration_pool', 'Duplicate calibration recipes are not separate coverage.')
    receipts = {}
    for receipt in measurements:
        if receipt.get('source_sha256') != source_sha256 or receipt.get('case_key') not in keys:
            raise VisualError('calibration_evidence_mismatch', 'Measurement belongs to another source or calibration recipe.')
        if receipt['case_key'] in receipts:
            raise VisualError('calibration_evidence_mismatch', 'Duplicate measurement receipt.')
        receipts[receipt['case_key']] = receipt
    rows = []
    covered = set()
    for case, key in zip(cases, keys):
        receipt = receipts.get(key)
        measured = bool(receipt and receipt.get('measurement_status') == 'ok'
                        and receipt.get('analysis_sha256') and receipt.get('package_sha256'))
        status = ('measured_mismatch' if receipt.get('behavior_pass') is False else 'measured') if measured else (
            'measurement_rejected' if receipt else 'not_measured')
        tags = _tags(case)
        if measured:
            covered.update(tags)
        rows.append({'scenario_id': case.get('id'), 'case_key': key, 'scenario': case,
                     'behavior_tags': tags, 'status': status, 'measurement_receipt': deepcopy(receipt)})
    all_tags = {tag for row in rows for tag in row['behavior_tags']}
    missing = all_tags - covered
    unmeasured = [row for row in rows if row['status'] == 'not_measured']
    # Preserve declared ordering for ties. Repetitions do not invent new behavior.
    next_row = max(unmeasured, key=lambda row: len(set(row['behavior_tags']) & missing), default=None)
    new_tags = sorted(set(next_row['behavior_tags']) & missing) if next_row else []
    if blocked_reason:
        next_row = None
    status = ('blocked' if blocked_reason else 'not_configured' if not rows else
              'recommendation' if next_row else 'measurement_repair_required' if any(
                  row['status'] == 'measurement_rejected' for row in rows) else 'known_pool_measured')
    if blocked_reason:
        why = blocked_reason
    elif not rows:
        why = 'No additional runnable calibration scenario was supplied.'
    elif next_row:
        why = ('Next declared case covers unmeasured behavior: ' + ', '.join(new_tags) if new_tags else
               'Remaining declared repeat; all known behavior tags were already observed.')
    elif status == 'measurement_repair_required':
        why = 'Repair rejected measurement evidence before drawing a behavior conclusion.'
    else:
        why = 'Every supplied calibration case was measured; behavior failures remain failures, not untested cases.'
    return {'kind': 'CalibrationCoverageAdvice', 'schema_version': 1, 'status': status,
            'source_sha256': source_sha256, 'calibration_pool_sha256': digest(cases),
            'covered_behavior_tags': sorted(covered), 'missing_behavior_tags': sorted(missing),
            'cases': rows, 'next_case': deepcopy(next_row) if next_row else None, 'why': why,
            'measured_mismatch_case_keys': [row['case_key'] for row in rows if row['status'] == 'measured_mismatch'],
            'execution_policy': 'Advisory only; every benchmark evaluation still runs its complete fixed calibration matrix.',
            'unknown_behaviors_generated': False, 'heldout_accessed': False}


def advice_for_job(state, job, output, result):
    """Read only this prepare/evaluate job's calibration measurement index.

    Execution already binds the job's immutable source. The returned advice is
    included in its hashed result; no different job or finalization file is read.
    """
    if state['mode'] != 'tools' or job.get('operation', job.get('action')) not in ('prepare', 'evaluate'):
        return result
    output = Path(output).resolve()
    operation = job.get('operation', job.get('action'))
    index = output / ('before.json' if operation == 'prepare' else 'candidate.json')
    records = read_json(index) if index.is_file() else []
    declared = _declared_pool(state.get('experiment', {}).get('calibration', []))
    # Game prepare explicitly provides its calibration-only pool in this index.
    # Uploaded media supplies exactly its one bound calibration condition.
    if not declared:
        declared = [record['scenario'] for record in records if record.get('scenario', {}).get('split') == 'calibration']
    pool = [_scenario(case) for case in declared]
    keys = {digest(case) for case in pool}
    receipts = []
    compared = {row.get('scenario_id'): row.get('pass') for row in result.get('comparisons', [])}
    for record in records:
        if record.get('scenario', {}).get('split') != 'calibration':
            raise VisualError('invalid_calibration_pool', 'A job calibration index contains another split.')
        key = digest(_scenario(record['scenario']))
        if key not in keys:
            raise VisualError('calibration_evidence_mismatch', 'Measured recipe differs from the explicit calibration pool.')
        if record.get('source_sha256') and record['source_sha256'] != job['source_sha256']:
            raise VisualError('calibration_evidence_mismatch', 'Measured source differs from the immutable job source.')
        analysis = Path(record['analysis']).resolve() if record.get('analysis') else None
        package = (Path(record['package']) / 'manifest.json').resolve()
        if not package.is_relative_to(output) or (analysis and not analysis.is_relative_to(output)):
            raise VisualError('calibration_evidence_mismatch', 'Calibration evidence must belong to this execution output.')
        package_value = read_json(package) if package.is_file() else None
        receipt = {'case_key': key, 'source_sha256': job['source_sha256'],
                   'measurement_status': record.get('measurement_status'),
                   'package_sha256': package_value.get('package_sha256') if package_value else None,
                   'package_manifest_file_sha256': file_sha256(package) if package_value else None,
                   'analysis_sha256': file_sha256(analysis) if analysis and analysis.is_file() else None,
                   'behavior_pass': compared.get(record['scenario'].get('case_id',record['scenario'].get('id')))}
        receipts.append(receipt)
    blocked = 'Preservation checks stopped motion measurement; repair the reported invariant before testing motion.' if result.get('motion_status') == 'not_run' else None
    return {**result, 'calibration_planning': recommend_calibration(pool,
            source_sha256=job['source_sha256'], measurements=receipts, blocked_reason=blocked)}


def inspect_advice(directory, state, current_source_sha256):
    """Only hash-verified completed calibration jobs can supply covered tags."""
    directory = Path(directory)
    pool = _declared_pool(state.get('experiment', {}).get('calibration', []))
    if state.get('sealed_at') is not None:
        return recommend_calibration(pool, source_sha256=current_source_sha256,
                                     blocked_reason='The candidate is sealed; calibration authoring is closed.')
    identifiers = [candidate['job_id'] for candidate in reversed(state.get('candidates', []))]
    if state.get('prepare_job_id'):
        identifiers.append(state['prepare_job_id'])
    for identifier in identifiers:
        from .backend import _validate_job_id
        _validate_job_id(identifier)
        job_path = directory / 'jobs' / identifier / 'job.json'
        job = read_json(job_path)
        if job.get('status') != 'succeeded' or job.get('operation') not in ('prepare', 'evaluate'):
            continue
        from .session import JOB_BINDING_FIELDS
        if digest({key: job[key] for key in JOB_BINDING_FIELDS}) != read_json(job_path.with_name('binding.json')).get('sha256'):
            raise VisualError('calibration_evidence_mismatch', 'Calibration job source binding changed.')
        result_path = job_path.with_name('result.json')
        result = read_json(result_path)
        if digest(result) != job.get('result_sha256'):
            raise VisualError('result_integrity_failure', 'Calibration planning result changed.')
        advice = result.get('calibration_planning')
        if not advice:
            continue
        if advice.get('source_sha256') != job.get('source_sha256'):
            raise VisualError('calibration_evidence_mismatch', 'Planning result belongs to another source.')
        old_pool = [row['scenario'] for row in advice['cases']]
        if digest(old_pool) != advice.get('calibration_pool_sha256'):
            raise VisualError('calibration_evidence_mismatch', 'Planning pool changed.')
        if not pool:
            pool = old_pool
        if job['source_sha256'] == current_source_sha256:
            return {**deepcopy(advice), 'evidence_job_id': identifier, 'evidence_result_sha256': job['result_sha256']}
    return recommend_calibration(pool, source_sha256=current_source_sha256)
