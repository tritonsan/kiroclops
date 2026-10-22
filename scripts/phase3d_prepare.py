"""Prepare private local 3D recordings. No model prompt or behavior patch."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time

from kiroclops.local_engine.backend import _lock
from kiroclops.local_engine.reliability import prepare_runtime, qualify_warmups
from kiroclops.local_engine.session import copy_project, source_hashes
from kiroclops.local_engine.media_suite import create_suite, create_suite_session
from kiroclops.visual.schema import VisualError, digest, read_json, write_json, file_sha256

ROOT=Path(__file__).resolve().parents[1]
STARTS={
    'jump':('artifacts/phase3b/kiro-integration-01/game/session','candidate-02'),
    'ground':('artifacts/phase3b/kiro-integration-01/game/session','candidate-02'),
    'panel':('artifacts/phase3b/panel-kiro-recovery-02/session','candidate-01'),
    'svg':('artifacts/phase3c/user-flow-kiro-01/intake/workflow/attempt-c66bf546675e227f50df2307/session','candidate-02')}
GOALS={
    'jump':'Match both short and long Tiny Crate jumps together. Preserve airborne control, double jump, direct airborne wall transitions, coyote, landing and hazard return. Write the actual player code; do not replay pixels.',
    'ground':'Match grounded run acceleration, braking on release and reversal. Preserve inherited short/long jumps, airborne control, double jump, wall/coyote behavior and landing. Write actual movement code.',
    'panel':'Match panel opening, closing, title/content timing and continuous interrupted motion from all calibration clips. Preserve pointer release/cancel, keyboard control, resizing and rapid repeated input. Write actual motion code.',
    'svg':'Preserve node layout timing under add/remove/drag, node identity, selection, visible connector attachment and no final overlap. Write real layout behavior if repair is required.'}


def start_sources(stage,parent_override=None):
    path,candidate=STARTS[stage]; historical=ROOT/path; saved=read_json(historical/'session.json')
    selected=next(c for c in saved['candidates'] if c['candidate_id']==candidate)
    parent=historical/'candidates'/candidate/'source'
    if digest(source_hashes(parent))!=selected['source_sha256']:
        raise VisualError('source_changed','Historical authoring candidate differs from its completed result.')
    original=historical/'baseline'
    if source_hashes(original)!=saved['baseline_hashes']:
        raise VisualError('source_changed','Historical original target changed.')
    if stage=='ground':
        if parent_override is None:
            raise VisualError('missing_sealed_jump','Ground starts only after an accepted new jump stage.')
        from kiroclops.local_engine.media_suite import accepted_parent
        parent=accepted_parent(parent_override,'jump')
    return parent,original,selected


def recipes(stage):
    if stage in ('jump','ground'):
        from kiroclops.local_engine.suite_game import scenarios
        rows=[{'case_id':s['case_id'],'role':split,'stage':stage,'purpose':'primary','scenario':s}
              for split in ('calibration','regression','validation') for s in scenarios(stage,split)]
        if stage=='ground':
            # Previous-stage jump evidence is preservation, never a new unseen claim.
            for split in ('calibration','regression','validation'):
                for s in scenarios('jump',split):
                    rows.append({'case_id':s['case_id'],'role':split,'stage':'jump','purpose':'preservation','scenario':s})
        return rows
    from kiroclops.local_engine.suite_web import scenarios
    return [{'case_id':s['case_id'],'role':role,'stage':stage,'purpose':'primary','scenario':s}
            for split,role in (('calibration','calibration'),('known_regression','regression'),('validation','validation'))
            for s in scenarios(stage,split)]


def prepare(stage,identifier,*,runtime_root=Path('C:/kc3d'),parent_override=None):
    if not identifier or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in identifier):
        raise VisualError('unsafe_path','Use a short lowercase stage identifier.')
    browser=ROOT/'.tools/browser-local'
    runtime=prepare_runtime(runtime_root,browser_path=browser if browser.is_dir() else None)
    # This executable is the dedicated capture process, not the user's shell.
    os.environ.update(runtime['environment']);os.environ['KIROCLOPS_ANALYSIS_BACKEND']='local'
    run=Path(runtime_root)/'p'/identifier
    if run.exists():raise VisualError('output_exists','Keep earlier preparation attempts unchanged.')
    run.mkdir(parents=True)
    parent,original,historical=start_sources(stage,parent_override)
    parent_copy=run/'parent';copy_project(parent,parent_copy)
    write_json(run/'start-binding.json',{'stage':stage,'historical_candidate':historical,
        'authoring_parent_sha256':digest(source_hashes(parent_copy)),
        'original_sha256':digest(source_hashes(original)),'parent_is_original':source_hashes(parent)==source_hashes(original),
        'model_started':False,'native_capture_only':True})
    cases=recipes(stage);cal=[c for c in cases if c['role']=='calibration' and c['purpose']=='primary']
    def progress(phase,completed=0,total=1):
        write_json(run/'progress.json',{'phase':phase,'completed':completed,'total':total,'updated_at':time.time(),'model_started':False})
        print(str(phase),flush=True)
    def capture(project,scenario,path,variant='before'):
        if stage in ('jump','ground'):
            from kiroclops.local_engine.suite_game import capture_target
            return capture_target(project,scenario,path,variant=variant,runtime_root=Path(runtime_root)/'runtime/game')
        from kiroclops.local_engine.suite_web import capture_record
        return capture_record(project,path,scenario,lambda:None,progress,None,variant)
    try:
        with _lock(Path(runtime['capture_lock']),timeout=60):
            warmup_records=[]
            def warmup(path):
                index=len(warmup_records);case=cal[index%len(cal)]
                record=capture(parent_copy,case['scenario'],path/'capture')
                warmup_records.append(record)
                write_json(path/'record.json',record)
                timing=record.get('timing') or {'status':'pass','errors':[],
                    'scope':'Native fixed-step frame mapping validated by capture/analysis adapter'}
                return {'status':'ok' if record.get('measurement_status')=='ok' else 'fail','timing':timing,'measurement_status':record.get('measurement_status'),
                        'record_sha256':digest(record)}
            ready=qualify_warmups(warmup,run/'warmups')
            records=[]
            for index,case in enumerate(cases):
                progress('reference '+case['case_id'],index,len(cases))
                if case['stage'] in ('jump','ground'):
                    from kiroclops.local_engine.suite_game import build_reference
                    record=build_reference(case['stage'],case['scenario'],run/'references'/case['case_id'],
                                           progress=progress,runtime_root=Path(runtime_root)/'runtime/game')
                else:
                    from kiroclops.web_local.specs import fixture_project
                    record=capture(fixture_project(stage,'reference'),case['scenario'],run/'references'/case['case_id'],'reference')
                if record.get('measurement_status')!='ok':
                    raise VisualError('tracking_failure','Unqualified reference '+case['case_id'])
                write_json(run/'references'/case['case_id']/'record.json',record)
                records.append({**case,'record':record})
            from kiroclops.local_engine.suite_web import event_contract
            suite_dir=run/'suite'
            suite=create_suite(suite_dir,domain='game' if stage in ('jump','ground') else 'web_panel' if stage=='panel' else 'svg',
                stage=stage,records=records,required_cases=[{k:deepcopy(c[k]) for k in ('case_id','role')} for c in cases],
                original=original,authoring_parent=parent_copy,goal=GOALS[stage],
                event_contract=event_contract(stage) if stage in ('panel','svg') else None)
            # Raw visual review remains a separate explicit step before launching.
            session=Path(runtime['session_root'])/identifier
            create_suite_session(session,suite_dir,parent_copy,runtime=runtime)
            write_json(session/'environment-readiness.json',{'status':'pending_raw_review','suite_sha256':digest(suite),
                'warmup_receipt':ready['receipt_path'],'warmups':ready,
                'runtime':runtime,'reference_raw_review_status':'pending','initial_raw_review_status':'pending',
                'model_started':False})
            result={'status':'ready_for_raw_review','stage':stage,'session':str(session),'suite':str(suite_dir),
                'warmups':ready,'reference_cases':len(records),'model_started':False}
    except BaseException as exc:
        result={'status':'failed','stage':stage,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc),
                'model_started':False,'previous_attempts_preserved':True}
        write_json(run/'result.json',result)
        raise
    write_json(run/'result.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',choices=STARTS,required=True);parser.add_argument('--id',required=True)
    parser.add_argument('--root',type=Path,default=Path('C:/kc3d'));parser.add_argument('--jump-session',type=Path)
    args=parser.parse_args()
    try:result=prepare(args.stage,args.id,runtime_root=args.root,parent_override=args.jump_session)
    except (VisualError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'failed','code':getattr(exc,'code','preparation_failed'),'message':str(exc)}));return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
