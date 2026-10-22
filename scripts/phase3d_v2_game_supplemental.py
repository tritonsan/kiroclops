"""Prospective missing game checks for an unchanged, previously Kiro-authored seal.

The failed Session and its old numerical records are never rewritten. This
standalone check captures only warm-ups, eight wall fixtures and the two normal
routes. Its receipt cannot grant final behavior acceptance or invent a model run.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import threading
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import reliability, suite_boundary, suite_game, suite_game_demo
from kiroclops.local_engine.backend import _identity, _lock
from kiroclops.local_engine.execution_policy import policy_from_state, REVISED_ID
from kiroclops.local_engine.session import _ordinary, copy_project, source_hashes
from kiroclops.local_engine.worker import _contain_windows_children
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts.phase3d_v2_partial_final import _terminal

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'unchanged-game-supplemental-v1'
SECONDS = 3600


def _need(value, message, code='invalid_supplemental'):
    if not value:
        raise VisualError(code, message)


def _code_sources():
    return {str(p.resolve()): file_sha256(p) for p in sorted((ROOT / 'kiroclops').rglob('*.py'))} | {
        str(Path(__file__).resolve()): file_sha256(__file__),
        str(ROOT / 'scripts/phase3d_v2_partial_final.py'): file_sha256(ROOT / 'scripts/phase3d_v2_partial_final.py')}


def _preflight(session_directory, native_audit, numerical_diagnostic, output):
    directory, native_path, diagnostic_root, output = map(_ordinary,
        (session_directory, native_audit, numerical_diagnostic, output))
    _need(not output.exists(), 'Use a fresh output; retain all failed attempts.', 'output_exists')
    _need(not output.is_relative_to(directory) and not directory.is_relative_to(output),
          'Supplemental output cannot overlap the historical Session.', 'unsafe_path')
    state_path = directory / 'session.json'
    state = read_json(state_path)
    job_root = directory / 'jobs' / state['final_job_id']
    job, binding = (read_json(job_root / n) for n in ('job.json', 'binding.json'))
    chosen, policy = _terminal(state, job, binding, (job_root / 'result.json').exists())
    _need(policy_from_state(state)['policy_id'] == REVISED_ID and policy['max_validation_seconds'] == SECONDS,
          'Keep the revised 60-minute jump validation policy.')
    source = directory / 'candidates' / chosen['candidate_id'] / 'source'
    hashes = source_hashes(source)
    _need(hashes == job['source_hashes'] and digest(hashes) == chosen['source_sha256'],
          'The sealed Kiro-authored source changed.', 'wrong_candidate')
    native = read_json(native_path)
    proof = native.get('native', {})
    _need(native.get('kind') == 'AppendOnlyNativeReaudit' and native.get('status') == 'pass'
          and proof.get('status') == 'pass' and proof.get('model') == 'claude-opus-5.5'
          and proof.get('effort') == 'max' and proof.get('prompt_count') == 1
          and native.get('original_failed_job_unchanged') is True
          and native.get('new_model_generation') is False and native.get('new_capture') is False,
          'Require the separate source-bound native re-audit, not a replacement trial.', 'native_not_verified')
    consumed = {str(p): file_sha256(p) for p in
        (state_path, job_root/'job.json', job_root/'binding.json', native_path)}
    _need(proof.get('evidence_hashes'), 'The native audit has no evidence identities.', 'native_not_verified')
    for name, sha in proof['evidence_hashes'].items():
        path = safe_path(directory, name)
        _need(file_sha256(path) == sha, 'The actual native trace changed.', 'native_not_verified')
        consumed[str(path)] = sha
    _need(native.get('source_files') and str(state_path) in native['source_files'],
          'The native audit must bind this Session and its actual reader.', 'native_not_verified')
    for path, sha in native['source_files'].items():
        _need(file_sha256(path)==sha, 'Native audit reader/source binding changed.', 'native_not_verified')
        consumed[str(_ordinary(path))]=sha
    diagnostic, receipt = (read_json(diagnostic_root / n) for n in ('diagnostic.json', 'receipt.json'))
    _need(diagnostic.get('kind') == 'FailedFinalNumericalDiagnostic'
          and diagnostic.get('session_id') == state['session_id']
          and diagnostic.get('original_job_id') == job['job_id']
          and diagnostic.get('candidate_id') == chosen['candidate_id']
          and diagnostic.get('source_sha256') == chosen['source_sha256']
          and diagnostic.get('numerical_status') == 'pass'
          and diagnostic.get('passed') == diagnostic.get('required') == 24
          and diagnostic.get('retained_parent_behavior') is True
          and diagnostic.get('full_behavior_acceptance') is False
          and receipt.get('diagnostic_sha256') == file_sha256(diagnostic_root/'diagnostic.json'),
          'Require the exact retained 24/24 numerical diagnosis.', 'wrong_numerical_evidence')
    for name, sha in receipt['output_file_hashes'].items():
        path = safe_path(diagnostic_root, name)
        _need(file_sha256(path) == sha, 'Retained numerical evidence changed.', 'wrong_numerical_evidence')
        consumed[str(path)] = sha
    consumed[str(diagnostic_root/'receipt.json')] = file_sha256(diagnostic_root/'receipt.json')
    comparisons = read_json(diagnostic_root/'comparisons.json')
    _need(len(comparisons) == 24 and len({c['case_id'] for c in comparisons}) == 24
          and all(c.get('pass') is True for c in comparisons), 'The retained comparison matrix is incomplete.')
    suite_root = _ordinary(state['reference'])
    for protected in (suite_root, source, diagnostic_root, native_path.parent):
        _need(not output.is_relative_to(protected) and not protected.is_relative_to(output),
              'New evidence cannot overlap a frozen input.', 'unsafe_path')
    from kiroclops.local_engine.media_suite import load_suite
    # Historical pixels retain their frozen producer epoch. This does not
    # authorize them as captures made by the repaired current producer.
    suite = load_suite(suite_root, verify_analysis_code=False)
    _need(digest(suite) == state['experiment']['suite_sha256'] == diagnostic['suite_sha256'],
          'The frozen suite changed.', 'suite_changed')
    for name in ('suite.json', 'suite-binding.json'):
        consumed[str(suite_root/name)] = file_sha256(suite_root/name)
    for name, sha in hashes.items():
        consumed[str(source/name)] = sha
    return {'kind': 'UnchangedGameSupplementalPlan', 'schema_version': 1, 'version': VERSION,
        'session': str(directory), 'session_id': state['session_id'], 'original_final_job_id': job['job_id'],
        'original_final_job_status': 'failed', 'candidate_id': chosen['candidate_id'],
        'source': str(source), 'source_hashes': hashes, 'source_sha256': chosen['source_sha256'],
        'suite': str(suite_root), 'suite_sha256': digest(suite), 'historical_source_epoch': suite['source_epoch'],
        'native_audit': str(native_path), 'numerical_diagnostic': str(diagnostic_root),
        'retained_numerical_comparisons': 24, 'retained_numerical_source_epoch_is_historical': True,
        'max_seconds': SECONDS, 'common_capture_lock': str(reliability.COMMON_CAPTURE_LOCK.resolve()),
        'consumed_files': consumed, 'new_model_generation': False,
        'target_behavior_edited': False, 'historical_acceptance_changed': False,
        'full_behavior_acceptance': False}


def _unchanged(plan, code_sources):
    _need(source_hashes(plan['source']) == plan['source_hashes'], 'Selected source changed.', 'source_changed')
    _need(code_sources == _code_sources(), 'Supplemental producer epoch changed.', 'source_changed')
    for path, sha in plan['consumed_files'].items():
        _need(file_sha256(path) == sha, 'Historical/native input changed.', 'evidence_changed')


def _deadline(started, cancel_path, *, now=None):
    if Path(cancel_path).exists():
        raise VisualError('cancelled', 'Only this supplemental validation was cancelled.')
    if (time.monotonic() if now is None else now) - started >= SECONDS:
        raise VisualError('deadline_exceeded', 'Supplemental validation exceeded its fixed 3600-second deadline.')


def run(session_directory, native_audit, numerical_diagnostic, output):
    started_mono, started_wall = time.monotonic(), time.time()
    plan = _preflight(session_directory, native_audit, numerical_diagnostic, output)
    output = _ordinary(output)
    _deadline(started_mono, output/'cancel.json')
    code_sources = _code_sources()
    output.mkdir(parents=True)
    write_json(output/'plan.json', plan)
    # Source archives make the fresh producer epoch inspectable without replacing
    # the historical epoch attached to the 24 numerical comparisons.
    archives = []
    for number, (path, sha) in enumerate(code_sources.items()):
        _deadline(started_mono, output/'cancel.json')
        destination = output/'code'/f'{number:03}-{Path(path).name}'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        _need(file_sha256(destination) == sha, 'Source archive copy changed.', 'source_changed')
        archives.append({'origin': path, 'sha256': sha, 'archive': str(destination)})
    write_json(output/'producer-epoch.json', {'code_sources': code_sources,
        'code_sources_sha256': digest(code_sources), 'archives': archives})
    copied = output/'source'
    _need(copy_project(plan['source'], copied) == plan['source_hashes'], 'Snapshot differs.', 'wrong_candidate')
    # The separate process owns its descendants; a watchdog also bounds a stalled
    # native call. Nothing enumerates or terminates the user's applications.
    containment = _contain_windows_children()
    stop, cancelled = threading.Event(), threading.Event()
    mutex = threading.RLock()
    state = {'kind': 'UnchangedGameSupplementalState', 'version': VERSION, 'status': 'running',
        'phase': 'capture_lease', 'pid': os.getpid(), 'process_identity': _identity(os.getpid()),
        'started_at': started_wall, 'deadline_at': started_wall+SECONDS,
        'source_sha256': plan['source_sha256'], 'new_model_generation': False, 'full_behavior_acceptance': False}

    def progress(phase, completed=0, total=1):
        with mutex:
            state.update(phase=phase, completed=completed, total=total, updated_at=time.time())
            write_json(output/'state.json', state)
        print(phase, flush=True)

    def check():
        _deadline(started_mono, output/'cancel.json')
        _need(source_hashes(copied) == plan['source_hashes'], 'Capture source copy changed.', 'source_changed')

    def watchdog():
        while not stop.wait(.2):
            try:
                _deadline(started_mono, output/'cancel.json')
            except VisualError as error:
                cancelled.set()
                with mutex:
                    state.update(status='cancelled' if error.code=='cancelled' else 'failed',
                        finished_at=time.time(), error={'code':error.code,'message':str(error)})
                    write_json(output/'state.json',state)
                    write_json(output/'failure.json',state)
                os._exit(130 if error.code=='cancelled' else 124)

    watcher = threading.Thread(target=watchdog, name='supplemental-deadline', daemon=True)
    watcher.start()
    try:
        _unchanged(plan, code_sources)
        progress('capture_lease')
        with _lock(Path(plan['common_capture_lock']), timeout=60):
            check()
            progress('warmup')
            count = []
            def warmup(path):
                scenario = suite_game.scenarios('jump', 'calibration')[len(count)%6]
                record = suite_game.capture_target(copied, scenario, path/'capture', check,
                    progress, cancelled, stage='jump', variant='final')
                check()
                suite_game._reliable(suite_game._report(record), 'jump')
                write_json(path/'record.json',record)
                count.append(record)
                return {'status':'ok', 'timing':{'status':'pass','errors':[],
                    'basis':'capture_target validated exact 60 Hz native frame/input bridge'},
                    'record_sha256':digest(record), 'evaluated':False}
            warmups = reliability.qualify_warmups(warmup, output/'warmups')
            check()
            progress('visual_boundary',0,8)
            boundary = suite_boundary.capture(copied, output/'visual-boundary.json', plan['candidate_id'],
                check, progress, cancelled)
            check()
            boundary_bindings = suite_boundary.bindings(output/'visual-boundary.json',
                candidate_sha256=plan['source_sha256'],candidate_id=plan['candidate_id'])
            progress('normal_level',0,2)
            normal = suite_game_demo.capture(copied, output/'normal-level', check, cancelled)
            check()
            suite_game_demo.verify(output/'normal-level', source_sha256=plan['source_sha256'])
        _unchanged(plan, code_sources)
        check()
        result = {'kind':'UnchangedGameSupplementalResult','schema_version':1,'version':VERSION,
            'status':'pass' if boundary['status']=='pass' and normal['status']=='pass' else 'fail',
            'started_at':started_wall,'finished_at':time.time(),'elapsed_seconds':time.monotonic()-started_mono,
            'max_seconds':SECONDS,'session':plan['session'],'candidate_id':plan['candidate_id'],
            'source_sha256':plan['source_sha256'],'current_producer_epoch_sha256':digest(code_sources),
            'retained_numerical_status':'historical_pass','retained_numerical_comparisons':24,
            'retained_numerical_source_epoch':plan['historical_source_epoch'],
            'warmups':warmups,'visual_boundary_status':boundary['status'],
            'visual_boundary_observations':sorted(boundary_bindings), 'normal_level_status':normal['status'],
            'normal_slope_diagnostic_status':normal['slope_diagnostic_status'],
            'native_reaudit_status':'pass','independent_visual_review_status':'pending',
            'source_review_status':'pending','full_behavior_acceptance':False,
            'original_failed_job_unchanged':True,'canonical_result_created':False,
            'new_model_generation':False,'target_behavior_edited':False,'capture_lease_released':True,
            'scope':'Fresh missing wall/normal checks only; retained numerical pixels retain their historical producer epoch.'}
        write_json(output/'result.json',result)
        write_json(output/'receipt.json', {'kind':'UnchangedGameSupplementalReceipt','version':VERSION,
            'result_sha256':digest(result),'plan_sha256':digest(plan),
            'file_hashes':{p.relative_to(output).as_posix():file_sha256(p)
                for p in sorted(output.rglob('*')) if p.is_file() and p.name not in ('state.json','receipt.json','cancel.json')},
            'code_sources_sha256':digest(code_sources),'full_behavior_acceptance':False})
        state.update(status='completed',phase='completed',finished_at=time.time(),capture_lease_released=True)
        write_json(output/'state.json',state)
        return result
    except BaseException as error:
        state.update(status='cancelled' if getattr(error,'code',None)=='cancelled' else 'failed',
            finished_at=time.time(),error={'code':getattr(error,'code',type(error).__name__),'message':str(error)},
            capture_lease_released=True)
        write_json(output/'state.json',state)
        write_json(output/'failure.json',state)
        raise
    finally:
        stop.set()
        watcher.join(1)
        # Keep the Windows Job handle alive through process exit.
        _ = containment


def verify(output):
    output = _ordinary(output)
    plan, result, receipt = (read_json(output/n) for n in ('plan.json','result.json','receipt.json'))
    _need(result.get('kind')=='UnchangedGameSupplementalResult' and result.get('version')==VERSION
          and receipt['result_sha256']==digest(result) and receipt['plan_sha256']==digest(plan)
          and result.get('source_sha256')==plan['source_sha256']
          and result.get('full_behavior_acceptance') is False and result.get('new_model_generation') is False
          and result.get('elapsed_seconds',float('inf'))<=SECONDS,
          'Supplemental receipt is incomplete or does not belong to this seal.')
    epoch=read_json(output/'producer-epoch.json')
    _need(epoch['code_sources_sha256']==digest(epoch['code_sources'])==receipt['code_sources_sha256'],
          'Producer epoch binding changed.')
    for row in epoch['archives']:
        _need(file_sha256(row['archive'])==row['sha256']==epoch['code_sources'][row['origin']], 'Archived producer changed.')
    # Receipt verifies archived producer bytes; later review-code edits are not
    # mistaken for the code that actually produced these fresh captures.
    for name,sha in receipt['file_hashes'].items():
        _need(file_sha256(safe_path(output,name))==sha,'Supplemental evidence changed.','evidence_changed')
    for path,sha in plan['consumed_files'].items():
        _need(file_sha256(path)==sha,'Historical inputs changed.','evidence_changed')
    _need(source_hashes(output/'source')==plan['source_hashes'],'Selected capture source changed.','source_changed')
    bound=suite_boundary.bindings(output/'visual-boundary.json',candidate_sha256=plan['source_sha256'],candidate_id=plan['candidate_id'])
    normal=suite_game_demo.verify(output/'normal-level',source_sha256=plan['source_sha256'])
    boundary=read_json(output/'visual-boundary.json')
    _need(result['visual_boundary_observations']==[list(x) for x in sorted(bound)]
          and result['visual_boundary_status']==boundary['status']
          and result['normal_level_status']==normal['status'], 'Functional result labels differ from exact evidence.')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('preflight','run','verify'))
    parser.add_argument('--session',type=Path)
    parser.add_argument('--native-audit',type=Path)
    parser.add_argument('--numerical-diagnostic',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.operation=='verify':value=verify(args.output)
    else:
        for name in ('session','native_audit','numerical_diagnostic'):
            if getattr(args,name) is None:parser.error('--'+name.replace('_','-')+' is required')
        value=(_preflight if args.operation=='preflight' else run)(args.session,args.native_audit,args.numerical_diagnostic,args.output)
    print({k:value.get(k) for k in ('kind','status','source_sha256','visual_boundary_status','normal_level_status','full_behavior_acceptance')})


if __name__=='__main__':
    main()
