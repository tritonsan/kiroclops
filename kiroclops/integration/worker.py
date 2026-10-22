"""One cancellable experiment job; no model invocation or controller generation."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import threading
import time
import traceback

from .session import Session, create_candidate_snapshot
from .storage import IntegrationError, file_lock, process_identity, project_hashes, read_json, utc_now, write_json


def run_job(session_directory, job_id):
    session = Session(session_directory)
    job_path = session.directory / 'jobs' / f'{job_id}.json'
    with file_lock(session.directory / '.session.lock'):
        job = read_json(job_path)
        job.update(status='running',started_at=utc_now(),worker_pid=os.getpid(),worker_identity=process_identity(os.getpid()))
        write_json(job_path,job)
    cancel = threading.Event()
    done = threading.Event()
    cancellation_reason = {'code':'cancelled'}

    def watch():
        while not done.wait(.25):
            try:
                state = session.read()
            except (OSError, ValueError):
                cancellation_reason['code']='session_unreadable'
                cancel.set()
                return
            if (job_path.with_suffix('.cancel')).exists():
                cancellation_reason['code']='cancelled'
                cancel.set()
            elif process_identity(job['owner_pid']) != job['owner_identity']:
                cancellation_reason['code']='owner_disconnected'
                cancel.set()
            elif job['action'] != 'report' and state.get('deadline_at') and time.time() >= state['deadline_at']:
                cancellation_reason['code']='budget_exhausted'
                cancel.set()
    threading.Thread(target=watch,daemon=True).start()

    def check():
        if cancel.is_set():
            raise IntegrationError(cancellation_reason['code'],'Job cancelled, disconnected, or timed out')

    def progress(message, completed=None, total=None):
        check()
        job['progress']={'message':message,'completed':completed,'total':total}
        write_json(job_path,job)

    def verify_source(project, expected):
        if project_hashes(project) != expected:
            raise IntegrationError('experiment_integrity', 'Source differs from the frozen session/candidate snapshot')

    def analyze_group(project, base, variant, split, expected):
        from .runner import capture_project
        from .evaluation import analyze_capture
        holds = (3,24) if split=='calibration' else (8,14)
        offsets = (0,) if split=='calibration' else (0,24)
        scenarios=[(h,o,r) for h in holds for o in offsets for r in (1,2,3)]
        runs=[]
        for index,(hold,offset,repeat) in enumerate(scenarios):
            verify_source(project, expected)
            progress(f'{variant}: {split}, hold={hold}, x={offset}, repeat={repeat}',index,len(scenarios))
            path=Path(base)/f'slay-{variant}-h{hold:02d}-x{offset}-r{repeat}'
            capture_project(project,path,hold_ticks=hold,variant=variant,repeat=repeat,
                            spawn_offset=offset,cancel=cancel)
            verify_source(project, expected)
            analysis=analyze_capture(path,session.directory/'annotations')
            if analysis.get('status') != 'ok':
                raise IntegrationError('tracking_failure',f'Invalid pixel tracking: {analysis.get("errors")}')
            runs.append(str(path))
        return runs

    try:
        from .runner import import_project, smoke_project
        from .evaluation import prepare_reference, calibration_feedback, final_comparison
        state = session.read()
        action = job['action']
        if state.get('experiment_id') == 'ground-motion-v1':
            from .ground_worker import run_action
            job['result'] = run_action(session, job, cancel=cancel, check=check, progress=progress)
        elif action=='prepare':
            progress('Re-analyzing reference calibration pixels')
            reference=prepare_reference(session.directory/'reference',split='calibration')
            check()
            baseline_runs=analyze_group(Path(state['baseline']),session.directory/'baseline-runs'/job_id/'calibration','before','calibration',state['baseline_hashes'])
            session.update(lambda s:s.update(status='ready',reference={'calibration':reference['runs'],
                               'profile':reference['profile'],'profile_path':reference.get('profile_path')},
                               baseline_runs={'calibration':baseline_runs}))
            profile=reference['profile']
            job['result']={'status':'ready','reference_profile':{key:profile.get(key) for key in
                           ('family','v0_body_s','gravity_body_s2','hold_ticks','fit_diagnostics')},
                           'units':{'v0_body_s':'positive UP, standing body heights per second',
                                    'gravity_body_s2':'standing body heights per second squared, downward',
                                    'hold_ticks':'maximum held-velocity physics steps at 60Hz'},
                           'target_body_height_px':24,'reference_body_height_px':7,
                           'integration':'Held steps keep upward velocity without applying gravity; after release/cap, gravity is applied before movement. Preserve original double/wall jumps.',
                           'baseline_runs':len(baseline_runs),'reference_runs':len(reference['runs'])}
            export_evidence(session.directory,'baseline',baseline_runs)
        elif action=='evaluate':
            cid=job['candidate_id']
            progress('Snapshotting candidate source')
            root=create_candidate_snapshot(session,cid)
            import_project(root/'project',runtime_dir=session.directory/'runtime'/cid/'import',cancel=cancel)
            candidate=read_json(root/'candidate.json')
            if project_hashes(root/'project') != candidate['source_hashes']:
                raise IntegrationError('experiment_integrity','Candidate source changed during Godot import')
            runs=analyze_group(root/'project',root/'runs/calibration','after','calibration',candidate['source_hashes'])
            state=session.read()
            feedback=calibration_feedback(state['reference']['calibration'],state['baseline_runs']['calibration'],runs)
            progress('Checking normal level, ground, walk, double jump and wall movement')
            smoke=smoke_project(root/'project',root/'smoke',cancel=cancel)
            verify_source(root/'project',candidate['source_hashes'])
            feedback['eligible'] = (feedback['status'] == 'ok' and feedback['diagnostic_gates_pass']
                                    and smoke.get('status') in {'pass', 'passed'})
            candidate.update(status='evaluated',calibration_runs=runs,feedback=feedback,smoke=smoke,finished_at=utc_now())
            write_json(root/'candidate.json',candidate)
            session.update(lambda s:(s['candidates'].__setitem__(int(cid[-2:])-1,candidate),s.update(status='ready')))
            exported=export_evidence(session.directory,cid,runs)
            job['result']={'candidate_id':cid,'status':'evaluated','calibration':feedback,'gameplay':smoke,
                           'raw_evidence':exported,'next_action':'finalize_candidate if eligible, otherwise inspect feedback and edit within remaining budget'}
        elif action=='finalize':
            cid=job['candidate_id']
            root=session.directory/'candidates'/cid
            candidate=read_json(root/'candidate.json')
            if project_hashes(root/'project') != candidate['source_hashes']:
                raise IntegrationError('experiment_integrity','Sealed candidate was modified')
            progress('Re-analyzing held-out reference pixels after candidate seal')
            reference=prepare_reference(session.directory/'reference',split='validation')
            before=analyze_group(Path(state['baseline']),session.directory/'baseline-runs/validation','before','validation',state['baseline_hashes'])
            after=analyze_group(root/'project',root/'runs/validation','after','validation',candidate['source_hashes'])
            state=session.read()
            refs=state['reference']['calibration']+reference['runs']
            all_before=state['baseline_runs']['calibration']+before
            all_after=candidate['calibration_runs']+after
            comparison=final_comparison(refs,all_before,all_after)
            final_dir=session.directory/'final'
            comparison_path=write_json(final_dir/'comparison.json',comparison)
            candidate.update(validation_runs=after,status='finalized')
            write_json(root/'candidate.json',candidate)
            final={'comparison_path':str(comparison_path),
                   'numerical_status':comparison.get('numerical_status'),
                   'measurement_status':comparison.get('manual_validation_status',comparison.get('measurement_status','pending')),
                   'smoke_status':candidate['smoke']['status'],'candidate_id':cid}
            def final_update(s):
                s['reference']['validation']=reference['runs']
                s['baseline_runs']['validation']=before
                s['candidates'][int(cid[-2:])-1]=candidate
                s.update(status='finished',final=final,finished_at=utc_now())
            session.update(final_update)
            job['result']={'status':'finished',**final,'validation':comparison}
        else:
            from .reporting import generate_session_report
            progress('Building local report')
            verify_source(Path(state['baseline']),state['baseline_hashes'])
            for candidate in state['candidates']:
                if candidate.get('source_hashes'):
                    verify_source(session.directory/'candidates'/candidate['candidate_id']/'project',candidate['source_hashes'])
            page=generate_session_report(session.directory)
            job['result']={'status':'ready','report_path':str(page)}
        check()
        job.update(status='succeeded',finished_at=utc_now())
    except Exception as exc:
        code=getattr(exc,'code','internal_error')
        if cancel.is_set():
            code=cancellation_reason['code']
        job.update(status='cancelled' if cancel.is_set() else 'failed',finished_at=utc_now(),
                   error={'code':code,'message':str(exc),'details':getattr(exc,'details',{})})
        traceback.print_exc()
        cid=job.get('candidate_id')
        log=Path(getattr(exc,'details',{}).get('log',''))
        if log.is_file() and cid:
            export=session.directory/'target/.kiroclops-evidence'/cid
            export.mkdir(parents=True,exist_ok=True)
            shutil.copy2(log,export/'engine.log')
        def failed_update(s):
            if job['action']=='evaluate' and cid:
                entry=s['candidates'][int(cid[-2:])-1]
                entry.update(status='failed',error=job['error'])
                write_json(session.directory/'candidates'/cid/'candidate.json',entry)
            s['status']='closed_failed' if s.get('selected_candidate_id') else ('ready' if s.get('reference') else 'new')
        session.update(failed_update)
    finally:
        done.set()
        write_json(job_path,job)
        session.update(lambda s:s.update(active_job_id=None) if s.get('active_job_id') == job_id else None)
    return 0 if job['status']=='succeeded' else 1


def export_evidence(directory, label, runs):
    """Raw pixel-only evidence export, identical for tool and baseline arms."""
    from scripts.inspect_frames import contact_sheet
    target=Path(directory)/'target/.kiroclops-evidence'/label
    target.mkdir(parents=True,exist_ok=True)
    exported=[]
    for raw in runs:
        run=Path(raw)
        manifest=read_json(run/'manifest.json')
        if manifest['scenario']['repeat'] != 1:
            continue
        out=target/run.name
        out.mkdir(exist_ok=True)
        shutil.copy2(run/'video.mp4',out/'video.mp4')
        frames=[0,29,30,33,36,39,42,45,48,51,54,57,60,63,66,69,72,80]
        contact_sheet(run,frames,[300,150,340,295],out/'frames.png',scale=4,columns=6)
        write_json(out/'inputs.json',{'fps':60,'scenario':manifest['scenario'],'frames':frames,
                                    'body_height_px':24,'video':'video.mp4','raw_contact_sheet':'frames.png'})
        exported.append(str(out))
    return exported


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--session',required=True)
    p.add_argument('--job',required=True)
    a=p.parse_args()
    return run_job(a.session,a.job)


if __name__=='__main__':
    raise SystemExit(main())
