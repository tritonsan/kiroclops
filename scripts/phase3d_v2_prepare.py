"""Fresh realistic-profile calibration, private pool and immutable suite.

No command sends a model prompt or modifies movement code. Calibration review
and pixel-floor selection precede all private reference captures.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.backend import _lock
from kiroclops.local_engine.reliability import prepare_runtime, qualify_warmups
from kiroclops.local_engine.session import source_hashes, copy_project, _ordinary
from kiroclops.local_engine.media_suite import create_suite, create_suite_session
from kiroclops.local_engine.suite_revision import measurement_source_hashes, stamp_new_measurement, revise_record
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts.phase3d_prepare import start_sources as old_start_sources, GOALS

ROOT=Path(__file__).resolve().parents[1]


def _new(root, identifier):
    if not identifier or len(identifier)>24 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in identifier):
        raise VisualError('unsafe_path','Use a short lowercase preparation identifier.')
    path=Path(root)/'p'/identifier
    if path.exists(): raise VisualError('output_exists','Never overwrite a preparation or failed capture.')
    path.mkdir(parents=True)
    return path


def sources(stage, jump_session=None):
    if stage!='panel':return old_start_sources(stage,jump_session)
    previous=Path('C:/kc3d/s/p04');state=read_json(previous/'session.json')
    selected=next(c for c in state['candidates'] if c['candidate_id']=='candidate-01')
    source=previous/'candidates/candidate-01/source'
    if digest(source_hashes(source))!=selected['source_sha256'] or source_hashes(previous/'baseline')!=state['baseline_hashes']:
        raise VisualError('source_changed','Pinned p04 candidate/original changed.')
    return source,previous/'baseline',selected


def recipes(stage):
    if stage in ('jump','ground'):
        from scripts.phase3d_prepare import recipes as game_recipes
        return game_recipes(stage)
    from kiroclops.local_engine.suite_web_v2 import scenarios
    return [{'case_id':s['case_id'],'role':role,'stage':stage,'purpose':'primary','scenario':s}
        for split,role in (('calibration','calibration'),('known_regression','regression'),('validation','validation'))
        for s in scenarios(stage,split)]


class Capture:
    def __init__(self, stage, output, root):
        self.stage,self.output,self.root=stage,Path(output),Path(root)
        browser=ROOT/'.tools/browser-local'
        self.runtime=prepare_runtime(root,browser_path=browser if browser.is_dir() else None)
        self.old={k:os.environ.get(k) for k in (*self.runtime['environment'],'KIROCLOPS_ANALYSIS_BACKEND')}
        os.environ.update(self.runtime['environment']);os.environ['KIROCLOPS_ANALYSIS_BACKEND']='local'

    def close(self):
        for k,v in self.old.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v

    def progress(self, phase, completed=0, total=1):
        print(phase,flush=True)
        write_json(self.output/'progress.json',{'phase':phase,'completed':completed,'total':total,
            'updated_at':time.time(),'model_started':False})

    def run(self, project, case, destination, variant='before', reference=None):
        stage=case['stage'];snapshot=measurement_source_hashes(stage);started=time.time()
        if stage in ('jump','ground'):
            from kiroclops.local_engine.suite_game import capture_target,build_reference
            if variant=='reference':
                record=build_reference(stage,case['scenario'],destination,progress=self.progress,
                    runtime_root=self.root/'runtime/game')
            else:
                record=capture_target(project,case['scenario'],destination,variant=variant,
                    progress=self.progress,runtime_root=self.root/'runtime/game')
        else:
            from kiroclops.local_engine.suite_web_v2 import capture_record
            record=capture_record(project,destination,case['scenario'],lambda:None,self.progress,None,variant,
                reference_selection=(reference or {}).get('selection'),reference_record=reference)
        record=stamp_new_measurement(record,stage,expected_sources=snapshot,capture_started_at=started)
        write_json(Path(destination)/'record.json',record)
        return record


def calibrate(stage, identifier, root=Path('C:/kc3d'), jump_session=None):
    output=_new(root,identifier);parent,original,historical=sources(stage,jump_session)
    copy_project(parent,output/'parent')
    cases=recipes(stage);write_json(output/'recipes.json',cases)
    write_json(output/'start-binding.json',{'stage':stage,'parent':str(output/'parent'),'original':str(original),
        'parent_source_sha256':digest(source_hashes(output/'parent')),'original_source_sha256':digest(source_hashes(original)),
        'historical_candidate':historical,'model_started':False,'recipes_sha256':digest(cases)})
    cap=Capture(stage,output,root)
    records=[]
    try:
        with _lock(Path(cap.runtime['capture_lock']),timeout=60):
            calibration=[c for c in cases if c['role']=='calibration']
            counter=[]
            def warmup(path):
                case=calibration[len(counter)%len(calibration)]
                record=cap.run(output/'parent',case,path/'capture');counter.append(record)
                write_json(path/'record.json',record)
                return {'status':'ok','timing':record.get('timing',{'status':'pass','errors':[]}),
                    'measurement_status':record['measurement_status'],'record_sha256':digest(record)}
            warmed=qualify_warmups(warmup,output/'warmups')
            for i,case in enumerate(calibration):
                cap.progress('calibration '+case['case_id'],i,len(calibration))
                if stage in ('panel','svg'):
                    from kiroclops.web_local.specs import fixture_project
                    project=fixture_project(stage,'reference')
                else:project=None
                ref=cap.run(project,case,output/'reference'/case['case_id'],'reference')
                records.append({**case,'source_role':'reference','record':ref})
                for role,source in (('original',original),('before',output/'parent')):
                    measured=cap.run(source,case,output/role/case['case_id'],reference=ref)
                    records.append({**case,'source_role':role,'record':measured})
            if stage=='svg':
                # Later interactions expose e/f. Qualify their appearance now,
                # without revealing held-out add/remove/drag input recipes.
                scenario=deepcopy(calibration[0]['scenario'])
                scenario.update(id='realistic-svg-six-node-measurement',case_id='realistic-svg-six-node-measurement',
                    split='qualification',suite_split='qualification',recipe='six-node-measurement',
                    initial_node_count=6,expected_final_node_count=6,expected_selected_id='b',
                    events=[{'time_s':.4,'action':'click','selector':'[data-node-id="b"]'}])
                case={'case_id':scenario['case_id'],'role':'calibration','stage':'svg',
                    'purpose':'measurement_qualification','qualification_kind':'static_geometry',
                    'behavior_fit_eligible':False,'scenario':scenario}
                from kiroclops.web_local.specs import fixture_project
                ref=cap.run(fixture_project('svg','reference'),case,output/'reference'/case['case_id'],'reference')
                records.append({**case,'source_role':'reference','record':ref})
                for role,source in (('original',original),('before',output/'parent')):
                    measured=cap.run(source,case,output/role/case['case_id'],reference=ref)
                    records.append({**case,'source_role':role,'record':measured})
            result={'status':'pending_independent_calibration_review','stage':stage,'output':str(output),
                'records':records,'warmups':warmed,'runtime':cap.runtime,'model_started':False}
            write_json(output/'calibration.json',result)
    except BaseException as exc:
        write_json(output/'failure.json',{'code':getattr(exc,'code',type(exc).__name__),'message':str(exc),
            'model_started':False,'previous_attempts_preserved':True});raise
    finally:cap.close()
    return result


def review_records(previous):
    """Normalize a separate input artifact; never relabel saved measurements."""
    previous=Path(previous);saved=read_json(previous/'calibration.json')
    binding=read_json(previous/'start-binding.json');stage=saved['stage']
    rows=[]
    for item in saved['records']:
        row={'id':item['source_role']+'-'+digest(item['case_id'])[:16],
            'record':deepcopy(item['record']),'role':item['source_role'],
            'case_id':item['case_id'],'split_role':'calibration','domain':stage,
            'purpose':item['purpose']}
        if item['purpose']=='measurement_qualification':
            scenario=item['scenario']
            if (scenario.get('recipe')!='six-node-measurement' or scenario.get('split')!='qualification'
                    or scenario.get('initial_node_count')!=6 or scenario.get('expected_final_node_count')!=6):
                raise VisualError('invalid_review','Only the explicitly captured static-six geometry record may qualify extra objects.')
            row.update(qualification_kind='static_geometry',behavior_fit_eligible=False)
        if item['source_role'] in ('original','before'):
            source=binding['original'] if item['source_role']=='original' else binding['parent']
            row.update(source_project=source,full_source_hashes=source_hashes(source))
        rows.append(row)
    return rows


def verify_calibration_review(previous, review):
    from kiroclops.local_engine.realistic_review import validate_review
    from kiroclops.local_engine.evidence import observations
    from kiroclops.visual.package import load_package
    inspected=validate_review(review)
    expected=[]
    for row in review_records(previous):
        record=row['record']
        expected.append((row['role'],row['case_id'],row['split_role'],row['purpose'],
            load_package(record['package'],verify='hashes')['package_sha256'],
            digest(observations(record)),record['source_sha256'],digest(record['selection'])))
    actual=[(r['role'],r['case_id'],r['split_role'],r['purpose'],r['package_sha256'],
        r['observations_sha256'],r['source_sha256'],r['selection_sha256']) for r in inspected.get('records',[])]
    if inspected.get('phase')!='calibration' or len(actual)!=len(expected) or set(actual)!=set(expected):
        raise VisualError('wrong_result','Pixel review must bind every exact calibration and static-qualification recording from this preparation.')
    return inspected


def revise_calibration(previous, identifier, root=Path('C:/kc3d')):
    """Append a current measurement epoch over historical, unchanged pixels.

    This does not refresh capture timing or environment readiness. Real warm-up
    receipts remain at their original paths, and new raw review is required.
    """
    from kiroclops.cloud.identity import engine_identity
    from kiroclops.local_engine.evidence import observations
    from kiroclops.local_engine.suite_readiness import _warmups
    from kiroclops.visual.package import load_package

    previous, root = _ordinary(previous), _ordinary(root)
    saved = read_json(previous/'calibration.json')
    binding = read_json(previous/'start-binding.json')
    cases = read_json(previous/'recipes.json')
    stage = saved['stage']
    def require(condition, message, code='measurement_provenance_changed'):
        if not condition: raise VisualError(code, message)
    require(stage in ('jump','ground','panel','svg') and binding['stage']==stage,
        'Preparation stages differ.', 'invalid_suite')
    require(saved.get('model_started') is False and binding.get('model_started') is False,
        'Only a pre-model calibration preparation may be revised.', 'invalid_suite')
    require(digest(cases)==binding['recipes_sha256'], 'Frozen calibration recipes changed.', 'source_changed')
    parent, original = _ordinary(binding['parent']), _ordinary(binding['original'])
    parent_hashes, original_hashes = source_hashes(parent), source_hashes(original)
    require(digest(parent_hashes)==binding['parent_source_sha256']
        and digest(original_hashes)==binding['original_source_sha256'],
        'Calibration starting sources changed.', 'source_changed')
    calibration = {c['case_id']:c for c in cases if c['role']=='calibration'}
    require(bool(calibration) and len(calibration)==sum(c['role']=='calibration' for c in cases),
        'Calibration recipes are missing or duplicated.', 'invalid_suite')
    rows = saved['records']
    groups = {}
    for row in rows:
        require(row.get('stage') in ((stage,'jump') if stage=='ground' else (stage,)) and row.get('role')=='calibration'
            and row.get('source_role') in ('reference','original','before'),
            'Only the recorded calibration roles may be revised.', 'invalid_suite')
        scenario = row['scenario']
        require(isinstance(row['case_id'],str) and row['case_id'] not in ('.','..')
            and '/' not in row['case_id'] and '\\' not in row['case_id'],
            'Calibration case identifier is unsafe.', 'unsafe_path')
        require(row['record']['scenario']==scenario and row['case_id']==scenario['case_id'],
            'Record and canonical case identities differ.', 'suite_recipe_mismatch')
        if row.get('purpose')=='measurement_qualification':
            require(stage=='svg' and row.get('behavior_fit_eligible') is False
                and row.get('qualification_kind')=='static_geometry'
                and scenario.get('recipe')=='six-node-measurement'
                and scenario.get('split')=='qualification'
                and scenario.get('initial_node_count')==scenario.get('expected_final_node_count')==6,
                'Only the historical static-six geometry qualification is supported.', 'invalid_suite')
        else:
            case=calibration.get(row['case_id'])
            require(case is not None and row.get('purpose')==case.get('purpose')
                and row.get('stage')==case['stage'] and case['scenario']==scenario,
                'A calibration record uses a different frozen recipe.', 'suite_recipe_mismatch')
        grouped=groups.setdefault(row['case_id'],{})
        require(row['source_role'] not in grouped, 'A calibration source role is duplicated.', 'invalid_suite')
        grouped[row['source_role']]=row
        if row['source_role']!='reference':
            expected=binding['original_source_sha256'] if row['source_role']=='original' else binding['parent_source_sha256']
            require(row['record'].get('source_sha256')==expected,
                'A calibration target belongs to a different starting source.', 'wrong_result')
    require(all(set(group)=={'reference','original','before'} for group in groups.values())
        and set(calibration).issubset(groups), 'Each calibration case needs all three historical source roles.', 'invalid_suite')
    # Validate real historical readiness without rewriting attempt-directory or
    # record hashes. The copies below use exactly the same parent/source bytes.
    warmup_path=_ordinary(saved['warmups']['receipt_path'])
    warmups=_warmups(warmup_path, {'authoring_parent_source_hashes':parent_hashes,
        'cases':list(calibration.values()), 'domain':'game' if stage in ('jump','ground') else 'web_panel' if stage=='panel' else 'svg'}, parent)
    require(warmups==saved['warmups'], 'Saved warm-up receipt changed.', 'environment_not_ready')
    historical_files={str(path):file_sha256(path) for path in
        (previous/'calibration.json',previous/'start-binding.json',previous/'recipes.json',warmup_path)}
    stages={row['stage'] for row in rows}
    stamps={s:measurement_source_hashes(s) for s in stages}
    engine=engine_identity()
    proposed=_ordinary(root/'p'/identifier)
    require(not proposed.is_relative_to(previous) and not proposed.is_relative_to(parent)
        and not proposed.is_relative_to(original), 'Revision output must be outside historical evidence.', 'unsafe_path')
    output=_new(root,identifier)
    old_backend=os.environ.get('KIROCLOPS_ANALYSIS_BACKEND')
    os.environ['KIROCLOPS_ANALYSIS_BACKEND']='local'
    def progress(phase, completed=0, total=1):
        write_json(output/'progress.json',{'phase':phase,'completed':completed,'total':total,
            'updated_at':time.time(),'native_capture_started':False,'model_started':False})
    def check():
        require({s:measurement_source_hashes(s) for s in stages}==stamps,
            'Analysis pipeline changed during revision.', 'analysis_source_changed')
        require(all(file_sha256(path)==sha for path,sha in historical_files.items())
            and source_hashes(parent)==parent_hashes and source_hashes(original)==original_hashes,
            'Historical preparation changed during revision.', 'source_changed')
    try:
        copy_project(parent,output/'parent')
        copy_project(original,output/'original-source')
        # Byte-identical recipes retain their canonical hash and formatting.
        (output/'recipes.json').write_bytes((previous/'recipes.json').read_bytes())
        new_binding=deepcopy(binding)
        new_binding.update(parent=str(output/'parent'),original=str(output/'original-source'),
            preparation_revision={'previous':str(previous), 'previous_start_binding_sha256':historical_files[str(previous/'start-binding.json')],
                'native_capture_started':False,'model_started':False})
        write_json(output/'start-binding.json',new_binding)
        write_json(output/'revision-plan.json',{'status':'analysis_pending','stage':stage,'previous':str(previous),
            'historical_file_sha256':historical_files,'measurement_source_hashes':stamps,
            'engine_sha256':engine['engine_sha256'],'native_capture_started':False,'model_started':False})
        write_json(output/'warmups/inherited.json',{'kind':'InheritedWarmupEvidence','schema_version':1,
            'receipt_path':str(warmup_path),'receipt_sha256':historical_files[str(warmup_path)],
            'previous_preparation':str(previous),'native_capture_started':False,'model_started':False,
            'environment_requalified':False})
        revised, references={},{}
        for case_id,group in groups.items():
            progress('reference '+case_id,len(revised),len(rows))
            row=group['reference']
            references[case_id]=revise_record(row['record'],output/'reference'/case_id,row['stage'],
                check=check,progress=progress,expected_sources=stamps[row['stage']],current_engine=engine)
            revised[(case_id,'reference')]=references[case_id]
        for row in rows:
            role,case_id=row['source_role'],row['case_id']
            if role=='reference':continue
            progress(role+' '+case_id,len(revised),len(rows))
            ref=references[case_id]
            revised[(case_id,role)]=revise_record(row['record'],output/role/case_id,row['stage'],
                reference_selection=ref['selection'] if row['stage'] in ('panel','svg') else None,
                reference_record=ref,check=check,progress=progress,expected_sources=stamps[row['stage']],current_engine=engine)
        # Assert preservation at the orchestration boundary as well as inside
        # revise_record. An analysis revision cannot silently recapture/replay.
        for row in rows:
            old=row['record'];new=revised[(row['case_id'],row['source_role'])]
            old_package=load_package(old['package'],verify='hashes');new_package=load_package(new['package'],verify='hashes')
            require(new_package==old_package and observations(new)['inputs']==observations(old)['inputs'],
                'Revision changed raw pixels, producer metadata or actual inputs.')
            for key in ('source_sha256','scenario','execution_scenario','input_replay','raw_run',
                'native_renderer_source_sha256','timing','functional','capture_attempts'):
                require(new.get(key)==old.get(key), 'Revision changed historical capture identity: '+key)
            require(new['measurement_revision']['native_capture_started'] is False,
                'A saved-pixel revision cannot claim a fresh capture.')
        check()
        require(_warmups(warmup_path, {'authoring_parent_source_hashes':parent_hashes,
            'cases':list(calibration.values()),'domain':'game' if stage in ('jump','ground') else 'web_panel' if stage=='panel' else 'svg'}, parent)==warmups,
            'Historical warm-up receipts changed during revision.', 'environment_not_ready')
        require(source_hashes(output/'parent')==parent_hashes and source_hashes(output/'original-source')==original_hashes,
            'Copied calibration starts changed.', 'source_changed')
        result=deepcopy(saved)
        result.update(status='pending_independent_calibration_review',output=str(output),
            records=[{**deepcopy(row),'record':revised[(row['case_id'],row['source_role'])]} for row in rows],
            warmups=warmups,native_capture_started=False,model_started=False,
            calibration_revision={'schema_version':1,'previous':str(previous),
                'previous_calibration_sha256':historical_files[str(previous/'calibration.json')],
                'pixels_preserved':True,'reference_pool_changed':False,'warmups_inherited':True,
                'environment_requalified':False,'raw_review_status':'pending',
                'measurement_source_hashes':stamps,'engine_sha256':engine['engine_sha256'],
                'reanalyzed_records':sum(r['measurement_revision']['action']=='reanalyzed_existing_pixels' for r in revised.values()),
                'copied_records':sum(r['measurement_revision']['action']=='copied_current_measurement' for r in revised.values())})
        write_json(output/'calibration.json',result)
        progress('ready for independent calibration review',len(rows),len(rows))
        return result
    except BaseException as exc:
        write_json(output/'failure.json',{'code':getattr(exc,'code',type(exc).__name__),'message':str(exc),
            'native_capture_started':False,'model_started':False,'previous_attempts_preserved':True})
        raise
    finally:
        if old_backend is None:os.environ.pop('KIROCLOPS_ANALYSIS_BACKEND',None)
        else:os.environ['KIROCLOPS_ANALYSIS_BACKEND']=old_backend


def freeze(previous, identifier, review, root=Path('C:/kc3d'), game_acceptance_profile=None):
    previous=Path(previous);saved=read_json(previous/'calibration.json');stage=saved['stage']
    if game_acceptance_profile is not None and stage!='ground':
        raise VisualError('invalid_suite','A game acceptance profile is only selectable for the ground stage.')
    binding=read_json(previous/'start-binding.json');cases=read_json(previous/'recipes.json')
    if (digest(cases)!=binding['recipes_sha256'] or digest(source_hashes(binding['parent']))!=binding['parent_source_sha256']
            or digest(source_hashes(binding['original']))!=binding['original_source_sha256']):
        raise VisualError('source_changed','Calibration start or fixed recipes changed.')
    from kiroclops.local_engine.realistic_policy import build_policy
    from kiroclops.local_engine.realistic_review import calibration_review
    if stage in ('panel','svg'):verify_calibration_review(previous,review)
    policy=build_policy(stage,calibration_review(review)) if stage in ('panel','svg') else None
    output=_new(root,identifier);cap=Capture(stage,output,root)
    records=[{**item,'record':item['record']} for item in saved['records']
        if item['source_role']=='reference' and item['purpose']!='measurement_qualification']
    initial=[{'case_id':item['case_id'],'role':item['source_role'],'record':item['record']}
        for item in saved['records'] if item['source_role'] in ('original','before') and item['purpose']!='measurement_qualification']
    baselines=[]
    try:
        with _lock(Path(cap.runtime['capture_lock']),timeout=60):
            for case in (c for c in cases if c['role']!='calibration'):
                cap.progress('private reference '+case['case_id'])
                if stage in ('panel','svg'):
                    from kiroclops.web_local.specs import fixture_project
                    project=fixture_project(stage,'reference')
                else:project=None
                ref=cap.run(project,case,output/'reference'/case['case_id'],'reference')
                records.append({**case,'record':ref})
                for role,source in (('original',binding['original']),('before',binding['parent'])):
                    measured=cap.run(source,case,output/role/case['case_id'],reference=ref)
                    baselines.append({'case_id':case['case_id'],'role':role,'record':measured})
            from kiroclops.local_engine.execution_policy import resolve_policy,REVISED_ID
            from kiroclops.local_engine.suite_web import event_contract
            suite=create_suite(output/'suite',domain='game' if stage in ('jump','ground') else 'web_panel' if stage=='panel' else 'svg',
                stage=stage,records=records,required_cases=[{k:c[k] for k in ('case_id','role')} for c in cases],
                original=binding['original'],authoring_parent=binding['parent'],goal=GOALS[stage],
                event_contract=event_contract(stage) if stage in ('panel','svg') else None,
                initial_records=initial,baseline_records=baselines,acceptance_policy=policy,
                execution_policy=resolve_policy(REVISED_ID,stage),
                **({'game_acceptance_profile':game_acceptance_profile} if game_acceptance_profile else {}))
            session=Path(root)/'s'/identifier
            create_suite_session(session,output/'suite',binding['parent'],runtime=cap.runtime)
            write_json(session/'environment-readiness.json',{'status':'pending_raw_review','suite_sha256':digest(suite),
                'warmups':saved['warmups'],'warmup_receipt':saved['warmups']['receipt_path'],'runtime':cap.runtime,
                'calibration_review':str(Path(review).resolve()) if review else None,
                'measurement_revision_required':False,'model_started':False})
            result={'status':'pending_admission','stage':stage,'session':str(session),'suite':str(output/'suite'),
                'acceptance_profile':suite['acceptance_profile'],'model_started':False}
            write_json(output/'result.json',result)
            return result
    except BaseException as exc:
        write_json(output/'failure.json',{'code':getattr(exc,'code',type(exc).__name__),'message':str(exc),
            'model_started':False,'previous_attempts_preserved':True});raise
    finally:cap.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    cal=commands.add_parser('calibrate');cal.add_argument('--stage',choices=('jump','ground','panel','svg'),required=True)
    cal.add_argument('--id',required=True);cal.add_argument('--jump-session',type=Path)
    frozen=commands.add_parser('freeze');frozen.add_argument('--previous',type=Path,required=True)
    frozen.add_argument('--id',required=True);frozen.add_argument('--review',type=Path)
    revised=commands.add_parser('revise-calibration');revised.add_argument('--previous',type=Path,required=True)
    revised.add_argument('--id',required=True)
    parser.add_argument('--root',type=Path,default=Path('C:/kc3d'))
    args=parser.parse_args()
    output_preexisted=(Path(args.root)/'p'/args.id).exists()
    try:
        if args.command=='calibrate':result=calibrate(args.stage,args.id,args.root,args.jump_session)
        elif args.command=='revise-calibration':result=revise_calibration(args.previous,args.id,args.root)
        else:result=freeze(args.previous,args.id,args.review,args.root)
    except (VisualError,OSError,ValueError,KeyError,TypeError) as exc:
        output=Path(args.root)/'p'/args.id
        if not output_preexisted and output.is_dir() and not (output/'failure.json').exists():
            write_json(output/'failure.json',{'code':getattr(exc,'code',type(exc).__name__),'message':str(exc),
                'model_started':False,'previous_attempts_preserved':True})
        print(json.dumps({'status':'failed','code':getattr(exc,'code',type(exc).__name__),'message':str(exc)}));return 1
    print(json.dumps({k:result[k] for k in ('status','stage','session','suite','output','model_started') if k in result}));return 0


if __name__=='__main__':raise SystemExit(main())
