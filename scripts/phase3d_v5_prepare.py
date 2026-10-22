"""V5 panel preparation: fresh calibration, private pool and suite (PHASE3D_V5_PLAN.md).

No command sends a model prompt or modifies movement code. The recipes come from
``kiroclops.local_engine.suite_web_v5``. Acceptance is the frozen
realistic-behavior-v1 window rule, whose pixel floors are fixed by the attested
independent calibration review before any private reference is captured. V5 has
no retest allowance.

The session goal (``GOAL``) is the V4 panel goal plus how to read the per-window
calibration feedback and the workspace rules the boundary enforces. It is stored
in the frozen suite before the model starts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.backend import _lock
from kiroclops.local_engine.reliability import qualify_warmups
from kiroclops.local_engine.session import copy_project, source_hashes
from kiroclops.local_engine.media_suite import create_suite, create_suite_session
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from scripts import phase3d_v2_prepare as v2p
from scripts.phase3d_prepare import GOALS

STAGE = 'panel'
KIND = 'V5PanelPreparation'
GOAL = (GOALS[STAGE] + ' After each evaluate_candidate, call get_job: result.comparisons lists every calibration clip by '
        'case_id with each window that fails, is near its limit, or got worse than the unchanged starting version '
        '(position_error versus position_error_before_change and position_limit, as fractions of scale_px). Fix those '
        'windows before evaluating again; a regression on one clip usually repeats on unseen clips of the same kind. '
        'The complete numbers are in result.full_feedback.path inside the target; read only the sections you need and '
        'keep each displayed section under 8000 characters. Workspace rules: never delete files (overwrite or leave '
        'unused helpers in place); keep every path inside the target and use paths relative to the target root or '
        'absolute paths under it; never use .., .kiro/ or anything outside the target in shell commands.')


def recipes():
    from kiroclops.local_engine.suite_web_v5 import scenarios
    return [{'case_id': s['case_id'], 'role': role, 'stage': STAGE, 'purpose': 'primary', 'scenario': s}
            for split, role in (('calibration', 'calibration'), ('regression', 'regression'), ('validation', 'validation'))
            for s in scenarios(STAGE, split)]


def _require(condition, message, code='invalid_suite'):
    if not condition:
        raise VisualError(code, message)


def _reference_project():
    from kiroclops.web_local.specs import fixture_project
    return fixture_project(STAGE, 'reference')


def calibrate(identifier, root=Path('C:/kc3d')):
    from kiroclops.local_engine.suite_web_v5 import V5_VERSION
    output = v2p._new(root, identifier)
    parent, original, historical = v2p.sources(STAGE)
    copy_project(parent, output / 'parent')
    cases = recipes()
    write_json(output / 'recipes.json', cases)
    write_json(output / 'start-binding.json', {
        'kind': KIND, 'stage': STAGE, 'v5_suite_version': V5_VERSION, 'goal': GOAL, 'parent': str(output / 'parent'),
        'original': str(original), 'parent_source_sha256': digest(source_hashes(output / 'parent')),
        'original_source_sha256': digest(source_hashes(original)), 'historical_candidate': historical,
        'model_started': False, 'recipes_sha256': digest(cases)})
    cap = v2p.Capture(STAGE, output, root)
    records = []
    try:
        with _lock(Path(cap.runtime['capture_lock']), timeout=60):
            calibration = [c for c in cases if c['role'] == 'calibration']
            counter = []

            def warmup(path):
                case = calibration[len(counter) % len(calibration)]
                record = cap.run(output / 'parent', case, path / 'capture')
                counter.append(record)
                write_json(path / 'record.json', record)
                return {'status': 'ok', 'timing': record.get('timing', {'status': 'pass', 'errors': []}),
                        'measurement_status': record['measurement_status'], 'record_sha256': digest(record)}
            warmed = qualify_warmups(warmup, output / 'warmups')
            for index, case in enumerate(calibration):
                cap.progress('calibration ' + case['case_id'], index, len(calibration))
                reference = cap.run(_reference_project(), case, output / 'reference' / case['case_id'], 'reference')
                records.append({**case, 'source_role': 'reference', 'record': reference})
                for role, source in (('original', original), ('before', output / 'parent')):
                    measured = cap.run(source, case, output / role / case['case_id'], reference=reference)
                    records.append({**case, 'source_role': role, 'record': measured})
            result = {'status': 'pending_independent_calibration_review', 'stage': STAGE, 'kind': KIND,
                      'output': str(output), 'records': records, 'warmups': warmed, 'runtime': cap.runtime,
                      'model_started': False}
            write_json(output / 'calibration.json', result)
    except BaseException as exc:
        write_json(output / 'failure.json', {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc),
                                             'model_started': False, 'previous_attempts_preserved': True})
        raise
    finally:
        cap.close()
    return result


def review_records(previous, output):
    """Write the calibration rows for ``scripts/phase3d_v2_review.py calibration``."""
    output = Path(output)
    _require(not output.exists(), 'Never overwrite saved review records.', 'output_exists')
    rows = v2p.review_records(previous)
    write_json(output, rows)
    return {'status': 'ok', 'rows': len(rows), 'output': str(output)}


def _check_preparation(previous):
    saved = read_json(previous / 'calibration.json')
    binding = read_json(previous / 'start-binding.json')
    cases = read_json(previous / 'recipes.json')
    _require(saved.get('stage') == STAGE and saved.get('kind') == KIND and binding.get('kind') == KIND,
             'Only a V5 panel calibration preparation can be frozen here.')
    _require(digest(cases) == binding['recipes_sha256'] and cases == recipes(),
             'Calibration recipes differ from the frozen V5 recipes.', 'source_changed')
    _require(digest(source_hashes(binding['parent'])) == binding['parent_source_sha256']
             and digest(source_hashes(binding['original'])) == binding['original_source_sha256'],
             'Calibration start sources changed.', 'source_changed')
    _require(binding.get('goal') == GOAL, 'The calibration was prepared for another session goal.', 'source_changed')
    _require(saved.get('model_started') is False and binding.get('model_started') is False,
             'A preparation that started a model cannot be frozen.')
    return saved, binding, cases


def freeze(previous, identifier, review, root=Path('C:/kc3d')):
    from kiroclops.local_engine import realistic_policy
    from kiroclops.local_engine.execution_policy import resolve_policy, REVISED_ID
    from kiroclops.local_engine.realistic_review import calibration_review
    from kiroclops.local_engine.suite_web import event_contract
    previous = Path(previous)
    saved, binding, cases = _check_preparation(previous)
    _require(review is not None, 'The attested independent calibration review is required.', 'calibration_unqualified')
    v2p.verify_calibration_review(previous, review)
    # The pixel floors are fixed here, before any private reference exists.
    policy = realistic_policy.build_policy(STAGE, calibration_review(review))
    output = v2p._new(root, identifier)
    cap = v2p.Capture(STAGE, output, root)
    records = [{**item, 'record': item['record']} for item in saved['records'] if item['source_role'] == 'reference']
    initial = [{'case_id': item['case_id'], 'role': item['source_role'], 'record': item['record']}
               for item in saved['records'] if item['source_role'] in ('original', 'before')]
    baselines = []
    try:
        with _lock(Path(cap.runtime['capture_lock']), timeout=60):
            for case in (c for c in cases if c['role'] != 'calibration'):
                cap.progress('private reference ' + case['case_id'])
                reference = cap.run(_reference_project(), case, output / 'reference' / case['case_id'], 'reference')
                records.append({**case, 'record': reference})
                for role, source in (('original', binding['original']), ('before', binding['parent'])):
                    measured = cap.run(source, case, output / role / case['case_id'], reference=reference)
                    baselines.append({'case_id': case['case_id'], 'role': role, 'record': measured})
            suite = create_suite(output / 'suite', domain='web_panel', stage=STAGE, records=records,
                                 required_cases=[{k: c[k] for k in ('case_id', 'role')} for c in cases],
                                 original=binding['original'], authoring_parent=binding['parent'], goal=GOAL,
                                 event_contract=event_contract(STAGE), initial_records=initial,
                                 baseline_records=baselines, acceptance_policy=policy,
                                 execution_policy=resolve_policy(REVISED_ID, STAGE))
            session = Path(root) / 's' / identifier
            create_suite_session(session, output / 'suite', binding['parent'], runtime=cap.runtime)
            write_json(session / 'environment-readiness.json', {
                'status': 'pending_raw_review', 'suite_sha256': digest(suite), 'warmups': saved['warmups'],
                'warmup_receipt': saved['warmups']['receipt_path'], 'runtime': cap.runtime,
                'calibration_review': str(Path(review).resolve()), 'measurement_revision_required': False,
                'model_started': False})
            result = {'status': 'pending_admission', 'stage': STAGE, 'session': str(session),
                      'suite': str(output / 'suite'), 'acceptance_profile': suite['acceptance_profile'],
                      'acceptance_policy_sha256': policy['policy_sha256'], 'model_started': False}
            write_json(output / 'result.json', result)
            return result
    except BaseException as exc:
        write_json(output / 'failure.json', {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc),
                                             'model_started': False, 'previous_attempts_preserved': True})
        raise
    finally:
        cap.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest='command', required=True)
    cal = commands.add_parser('calibrate')
    cal.add_argument('--id', required=True)
    rows = commands.add_parser('review-records')
    rows.add_argument('--previous', type=Path, required=True)
    rows.add_argument('--output', type=Path, required=True)
    frozen = commands.add_parser('freeze')
    frozen.add_argument('--previous', type=Path, required=True)
    frozen.add_argument('--id', required=True)
    frozen.add_argument('--review', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    args = parser.parse_args(argv)
    preexisted = args.command != 'review-records' and (Path(args.root) / 'p' / args.id).exists()
    try:
        if args.command == 'calibrate':
            result = calibrate(args.id, args.root)
        elif args.command == 'review-records':
            result = review_records(args.previous, args.output)
        else:
            result = freeze(args.previous, args.id, args.review, args.root)
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        if args.command != 'review-records':
            output = Path(args.root) / 'p' / args.id
            if not preexisted and output.is_dir() and not (output / 'failure.json').exists():
                write_json(output / 'failure.json', {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc),
                                                     'model_started': False, 'previous_attempts_preserved': True})
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}))
        return 1
    print(json.dumps({k: result[k] for k in ('status', 'stage', 'session', 'suite', 'output', 'rows',
                                             'acceptance_policy_sha256', 'model_started') if k in result}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
