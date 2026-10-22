"""Explicit fresh registration after an operator-interrupted, unevaluated run.

This never retries a model prompt or overwrites its failed evidence. Only a
zero-candidate, unsealed session with confirmed terminal cleanup is eligible.
"""
from pathlib import Path
import argparse
import json
import time
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.local_engine.session import Session, source_hashes, LIMITS
from kiroclops.local_engine import media_suite, suite_readiness
from kiroclops.local_engine.operator_recovery_gate import inspect_eligibility, validate_eligibility, replay_cleanup


def register(previous, directory, *, eligibility):
    previous, directory = Path(previous).resolve(), Path(directory).resolve()
    scope, exposure = validate_eligibility(previous, eligibility)
    state = Session(previous)._load()
    incident = read_json(previous / 'operator-interruption.json')
    cleanup = read_json(previous / 'operator-terminal-cleanup.json')
    launch = read_json(previous / 'kiro/launch.json')
    if (incident.get('kind') != 'OperatorInterruptedTrial'
            or incident.get('status') != 'operator_interrupted'
            or incident.get('behavior_outcome') != 'unevaluated'
            or incident.get('session_id') != state['session_id']
            or incident.get('cleanup_sha256') != file_sha256(previous / 'operator-terminal-cleanup.json')
            or cleanup.get('status') != 'pass' or cleanup.get('capture_lease_released') is not True
            or state.get('candidates') or state.get('sealed_at') is not None
            or state.get('final_job_id') is not None
            or launch.get('run_kind') != 'multi_reference_repair'
            or incident.get('launch_snapshot_sha256') != file_sha256(previous / 'kiro/launch.json')):
        raise VisualError('recovery_not_eligible', 'Preserve evaluated runs; require an exact operator interruption and cleanup.')
    if directory.exists() or directory == previous or directory.is_relative_to(previous):
        raise VisualError('output_exists', 'Register recovery in an unused sibling session directory.')
    if source_hashes(state['target']) != state['baseline_hashes']:
        raise VisualError('source_changed', 'The interrupted run already edited code; do not silently reset its outcome.')
    for job_file in (previous / 'jobs').glob('*/job.json'):
        if read_json(job_file).get('status') not in ('cancelled', 'failed', 'succeeded'):
            raise VisualError('job_running', 'Confirm every interrupted job is terminal before new registration.')
    cleanup_now=replay_cleanup(previous)
    if inspect_eligibility(previous)!=scope:
        raise VisualError('stale_recovery_evidence','Interrupted prefix changed during cleanup replay.')
    admission = read_json(previous / 'admission.json')
    ready = read_json(previous / 'environment-readiness.json')
    suite = media_suite.load_suite(state['reference'])
    media_suite.create_suite_session(directory, state['reference'], state['baseline'], runtime=ready['runtime'])
    created=Session(directory)._load()
    if created['budget']!=LIMITS or LIMITS!={'max_candidates':3,'max_seconds':1800,'max_validation_seconds':1800}:
        raise VisualError('wrong_recovery_budget','Fresh Session must enforce the fixed authoring and finalization limits.')
    write_json(directory / 'environment-readiness.json', {**ready, 'model_started': False})
    registration = {'kind': 'ExplicitOperatorRecoveryRegistration', 'schema_version': 1,
        'created_at': time.time(), 'previous_session': str(previous), 'previous_session_id': state['session_id'],
        'interruption_sha256': file_sha256(previous / 'operator-interruption.json'),
        'suite_sha256': digest(suite), 'calibration_pool_changed': False,
        'validation_exposed': False, 'validation_exposure_proof':'explicit_operator_prefix_attestation',
        'eligibility_attestation':str(Path(eligibility).resolve()), 'eligibility_attestation_sha256':file_sha256(eligibility),
        'eligibility_scope_sha256':digest(scope), 'previous_evidence_hashes':scope['evidence_hashes'],
        'launcher_process_identity_status':scope['launcher_process_identity_status'],
        'launcher_exit_proof':scope['launcher_exit_proof'], 'cleanup_replay':cleanup_now,
        'residual_owned_processes':exposure['residual_owned_processes'],
        'previous_behavior_outcome': 'unevaluated', 'previous_failure_preserved': True,
        'max_candidates': 3, 'authoring_budget_s': 1800, 'finalization_budget_s': 1800,
        'automatic_model_retry': False, 'model_started': False, 'behavior_acceptance': False}
    registration['status']='pending_readiness'
    write_json(directory / 'operator-recovery-registration.json', registration)
    try:
        suite_readiness.certify(directory, admission['raw_review']['directory'])
        if inspect_eligibility(previous)!=scope or file_sha256(eligibility)!=registration['eligibility_attestation_sha256']:
            raise VisualError('stale_recovery_evidence','Interrupted prefix or operator attestation changed during admission.')
    except (VisualError,OSError,ValueError,KeyError) as exc:
        registration.update(status='registration_failed',error={'code':getattr(exc,'code','recovery_failed'),'message':str(exc)})
        write_json(directory / 'operator-recovery-registration.json',registration)
        raise
    registration['status']='registered'
    write_json(directory / 'operator-recovery-registration.json',registration)
    return {'status': 'registered', 'session': str(directory), **registration}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--eligibility', type=Path, required=True,
                        help='Explicit operator-written whole-prefix exposure attestation; never generated automatically.')
    args = parser.parse_args()
    try:
        result = register(args.previous, args.session, eligibility=args.eligibility)
    except (VisualError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', 'recovery_failed'), 'message': str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
