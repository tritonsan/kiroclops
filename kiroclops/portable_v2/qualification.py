"""Model-free qualification, including native receipts and independent pixels."""
from __future__ import annotations
from pathlib import Path
import shutil
import time
import cv2
import numpy as np
from kiroclops.common import ROOT
from kiroclops.integration.storage import read_json, write_json, sha256, project_hashes, IntegrationError
from . import specs, measurement
from .review import validate_observations


def collect_native(output, *, progress=print, cancel=None):
    """Reproduce setup on a fresh copy, using only the private engine transport."""
    from kiroclops.integration.storage import copy_project
    from .runtime import prepare_project, import_project, capture_project, protocol_record
    from .integrity import run_checks
    from .qualification_controls import collect_controls
    output=Path(output).resolve()
    if output.exists(): raise IntegrationError('output_exists','Native setup needs a fresh output directory')
    specs.verify_source(); specs.verify_legacy()
    project=output/'baseline'
    copy_project(ROOT/'vendor/heart',project); prepare_project(project)
    progress('Importing original Heart on a private desktop')
    import_project(project,output/'import',cancel=cancel)
    sources=project_hashes(project); captures=[]
    for stage in ('jump','ground'):
        for scenario in specs.scenarios(stage,'calibration'):
            progress(stage+' '+specs.run_name(scenario))
            run=output/'calibration'/stage/specs.run_name(scenario)
            actual={k:v for k,v in scenario.items() if k not in ('fps','mechanic')}
            t=time.monotonic(); manifest=capture_project(project,run,actual,cancel=cancel)
            captures.append({'id':scenario['id'],'repeat':scenario['repeat'],'path':str(run),
                'seconds':time.monotonic()-t,'raw_frames_sha256':manifest['raw_frames_sha256']})
    for name in ('normal-combined','normal-slope'):
        events=[{'tick':30,'action':'right','pressed':True},{'tick':78,'action':'right','pressed':False}]
        if name=='normal-combined':
            events += [{'tick':36,'action':'jump','pressed':True},{'tick':60,'action':'jump','pressed':False}]
            events.sort(key=lambda x:x['tick'])
        scenario={'id':name,'split':'smoke','repeat':1,'spawn_offset':0,'events':events,'frame_count':240,'warmup_ticks':60}
        run=output/name; progress(name)
        manifest=capture_project(project,run,scenario,normal_level=True,cancel=cancel)
        captures.append({'id':name,'repeat':1,'path':str(run),'raw_frames_sha256':manifest['raw_frames_sha256']})
    native={'status':'pass','protocol':protocol_record(),'captures':captures,'project':str(project),'prepared_source':sources,
        'source_unchanged':sources==project_hashes(project),'code_unchanged':True,
        'determinism':{name:len({c['raw_frames_sha256'] for c in captures if c['id']==name})==1 for name in ('h03','r06')}}
    write_json(output/'runtime-receipt.json',native)
    summary={'baseline':[],'negative_controls':[]}
    for stage in ('jump','ground'):
        progress(stage+' preservation baseline')
        path=output/'integrity'/stage/'baseline'
        run_checks(project,project,path,stage,cancel=cancel)
        summary['baseline'].append({'stage':stage,'path':str(path)})
    # Known-bad shared parameters exercise preservation; they are never model seeds.
    for label,old,new in (('shared-speed','speed = 100.0','speed = 117.0'),('shared-jump','jump_velocity = -250.0','jump_velocity = -275.0')):
        bad=output/'negative-projects'/label; copy_project(project,bad)
        resource=bad/'DefaultMovementData.tres'; text=resource.read_text(encoding='utf-8')
        if text.count(old)!=1: raise IntegrationError('fixture_mismatch','Pinned negative-control resource differs')
        resource.write_text(text.replace(old,new),encoding='utf-8')
        for stage in ('jump','ground'):
            progress(stage+' '+label+' negative control')
            path=output/'integrity'/stage/label
            run_checks(bad,project,path,stage,cancel=cancel)
            summary['negative_controls'].append({'stage':stage,'kind':label,'path':str(path)})
    summary['wall_coyote_controls'] = collect_controls(project, output/'wall-coyote-controls',
                                                      progress=progress, cancel=cancel)
    write_json(output/'integrity-receipt.json',summary)
    return {'baseline':str(project),'runtime_receipt':str(output/'runtime-receipt.json'),
            'integrity_receipt':str(output/'integrity-receipt.json'),'calibration_root':str(output/'calibration')}


def _map(paths):
    result={}
    for path in paths:
        path=Path(path).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise IntegrationError('qualification_binding','Evidence must be a workspace file')
        result[path.relative_to(ROOT).as_posix()]=sha256(path)
    return result


def _run_files(run):
    run=Path(run)
    return [p for p in run.rglob('*') if p.is_file() and '.runtime' not in p.parts]


def tracking_negative_controls(run, stage, output):
    """Deliberately damaged engineering copies; never called candidate trials."""
    output=Path(output)
    if output.exists():
        raise IntegrationError('output_exists','Use a new negative-control directory')
    checks={}
    for case in ('missing_frame','camera_shift','bad_selection'):
        target=output/case
        (target/'frames').mkdir(parents=True)
        shutil.copy2(Path(run)/'manifest.json',target/'manifest.json')
        for i in range(240):
            if case=='missing_frame' and i==40: continue
            source=Path(run)/'frames'/f'{i:06d}.png'
            dest=target/'frames'/source.name
            if case=='camera_shift' and i==40:
                raw=cv2.imread(str(source)); shifted=cv2.warpAffine(raw,np.float32([[1,0,3],[0,1,0]]),(raw.shape[1],raw.shape[0]))
                cv2.imwrite(str(dest),shifted)
            else: shutil.copy2(source,dest)
        annotation=read_json(specs.CONFIG/f'heart-{stage}-annotation.json')
        if case=='bad_selection': annotation['roi']=[10,10,12,7]
        ap=target/'annotation.json'; write_json(ap,annotation)
        try:
            measurement.analyze(target,stage,annotation=ap)
            checks[case]={'status':'fail','reason':'Damaged input produced successful measurement'}
        except (ValueError, OSError, IntegrationError) as exc:
            checks[case]={'status':'pass','rejected_as':getattr(exc,'code',type(exc).__name__),'reason':str(exc)}
    result={'status':'pass' if all(c['status']=='pass' for c in checks.values()) else 'fail',
            'engineering_only':True,'checks':checks}
    write_json(output/'result.json',result)
    return result


def qualify(runtime_receipt, integrity_receipt, calibration_root, observations_path, test_receipt, *, output=None):
    """Assemble a replayable qualification only after all evidence is present.

This command does not run a model, alter movement code or relax thresholds.
Native collection is a separate private-desktop operation.
"""
    from .runtime import protocol_record, verify_runtime_receipt, _frames, _digest, validate_recording, gameplay_checks
    from .integrity import verify_checks
    from .qualification_controls import verify_controls
    if any((specs.CONFIG/'cohorts').glob('*.json')):
        raise IntegrationError('protocol_frozen','Qualification is immutable after cohort registration')
    output=Path(output or specs.CONFIG/'qualification.json')
    root=Path(calibration_root).resolve()
    work=specs.ARTIFACTS/'setup'/('qualification-'+str(time.time_ns()))
    work.mkdir(parents=True)
    native=read_json(runtime_receipt); integrity=read_json(integrity_receipt)
    controls=read_json(observations_path); tests=read_json(test_receipt)
    files=[Path(runtime_receipt),Path(integrity_receipt),Path(observations_path),Path(test_receipt)]
    evidence=[]; reference_files=[]
    prepared=project_hashes(native['project'])
    native_ok=(native.get('status')=='pass' and native.get('protocol')==protocol_record()
        and native.get('source_unchanged') is True and native.get('code_unchanged',native.get('protocol_unchanged')) is True
        and native.get('determinism',native.get('room_determinism'))=={'h03':True,'r06':True}
        and prepared==native.get('prepared_source'))
    native_ids=set(); native_paths=set(); native_slots=set(); determinism={name:[] for name in ('h03','r06')}
    for run in native.get('captures',native.get('runs',[])):
        directory=Path(run['path']); manifest=read_json(directory/'manifest.json')
        slot=(run['id'],run['repeat'])
        native_ok &= str(directory.resolve()) not in native_paths and slot not in native_slots
        native_paths.add(str(directory.resolve())); native_slots.add(slot)
        native_ok &= (run['id']==manifest['scenario']['id'] and run['repeat']==manifest['scenario']['repeat']
            and manifest['normal_level']==run['id'].startswith('normal-')
            and Path(manifest['provenance']['runtime_receipt']).resolve()==(directory/'engine.portable-heart-runtime-v1/bridge.json').resolve()
            and manifest['provenance']['full_source_hashes_before']==manifest['provenance']['full_source_hashes_after']==prepared)
        replay=verify_runtime_receipt(manifest['provenance']['runtime_receipt'],expected_source_hashes=prepared)
        native_ok &= replay['status']=='pass'
        actual=_frames(directory)
        native_ok &= _digest(actual)==manifest['raw_frames_sha256']
        recording=read_json(directory/'recording.json')
        validate_recording(recording,manifest['scenario'],manifest['normal_level'])
        native_ok &= sha256(directory/'recording.json')==manifest['provenance']['recording_sha256']
        if run['id'] in determinism: determinism[run['id']].append(_digest(actual))
        native_ids.add(run['id'])
        if manifest['normal_level']:
            replay_gameplay=gameplay_checks(recording,manifest['scenario'])
            native_ok &= replay_gameplay==manifest['gameplay'] and replay_gameplay['status']=='pass'
        evidence += _run_files(directory)
    native_ok &= {'h03','r06','normal-combined','normal-slope'} <= native_ids
    native_ok &= all(len(v)>=2 and len(set(v))==1 for v in determinism.values())
    # Receipts list actual differential outputs, not operator-provided success flags.
    integrity_gates={}
    for kind in ('baseline','negative_controls'):
        checks=[]; identities=set(); coverage=set()
        for item in integrity.get(kind,[]):
            directory=Path(item['path'])
            identities.add(str(directory.resolve()))
            result=read_json(directory/'comparison.json')
            stage=item['stage']; control=item.get('control',item.get('kind','baseline')).replace('-','_')
            coverage.add((stage,control))
            candidate_sources=result.get('sources',{}).get('candidate',{})
            candidate_ok=candidate_sources==prepared
            if kind=='negative_controls' and control in ('shared_speed','shared_jump'):
                changed=[p for p in set(candidate_sources)|set(prepared) if candidate_sources.get(p)!=prepared.get(p)]
                candidate_ok=changed==['DefaultMovementData.tres']
                suite=read_json(directory/'candidate/suite.json')
                resource=Path(suite['source'])/'DefaultMovementData.tres'
                original=(Path(native['project'])/'DefaultMovementData.tres').read_text(encoding='utf-8')
                old,new=('speed = 100.0','speed = 117.0') if control=='shared_speed' else ('jump_velocity = -250.0','jump_velocity = -275.0')
                candidate_ok &= resource.read_text(encoding='utf-8')==original.replace(old,new)
            replay=verify_checks(directory,expected_baseline_sources=prepared,expected_candidate_sources=candidate_sources)
            expected='pass' if kind=='baseline' else 'fail'
            checks.append(candidate_ok and result.get('stage')==stage and replay.get('status')=='pass' and result.get('status')==expected)
            evidence += _run_files(directory)
        needed=2 if kind=='baseline' else 4
        planned={(s,c) for s in ('jump','ground') for c in (('baseline',) if kind=='baseline' else ('shared_speed','shared_jump'))}
        integrity_gates[kind]={'status':'pass' if len(checks)==len(identities)==needed and coverage==planned and all(checks) else 'fail','count':len(checks),'required':needed}
    overlap_receipt = Path(integrity['wall_coyote_controls'])
    overlap_gate = verify_controls(overlap_receipt, expected_original_sources=prepared)
    files.append(overlap_receipt)
    evidence += _run_files(overlap_receipt.parent)
    groups={}; analyses={}; negatives={}; profiles={}
    for stage in ('jump','ground'):
        refs=measurement.reference_runs(stage,'calibration',work/'reference'/stage)
        target=[]
        for scenario in specs.scenarios(stage,'calibration'):
            run=root/stage/specs.run_name(scenario)
            manifest=read_json(run/'manifest.json')
            if (manifest['scenario']['id']!=scenario['id'] or manifest['scenario']['repeat']!=scenario['repeat']
                    or manifest['normal_level'] or Path(manifest['provenance']['runtime_receipt']).resolve()!=(run/'engine.portable-heart-runtime-v1/bridge.json').resolve()
                    or manifest['provenance']['full_source_hashes_before']!=prepared or manifest['provenance']['full_source_hashes_after']!=prepared):
                raise IntegrationError('wrong_capture','Calibration identity/source/runtime belongs to another capture')
            if verify_runtime_receipt(manifest['provenance']['runtime_receipt'],expected_source_hashes=prepared)['status']!='pass':
                raise IntegrationError('native_unverified','Calibration capture failed private/native replay')
            measurement.analyze(run,stage)
            target.append(str(run/'analysis.json')); evidence += _run_files(run)
        profiles[stage]=measurement.fit_reference(stage,refs,work/f'{stage}-profile.json')
        analyses[stage]={'reference':refs,'before':target}
        groups[stage]={}
        for role,paths in analyses[stage].items():
            groups[stage][role]=validate_observations(paths,controls['groups'][stage][role],minimum=20,require_events=stage=='jump')
            if role=='reference':
                for p in paths: reference_files += _run_files(Path(read_json(p)['run_id']))
        negatives[stage]=tracking_negative_controls(Path(target[0]).parent,stage,work/'negative'/stage)
    # Bind all pre-existing reference PNGs, including held-outs, without fitting them.
    for stage in ('jump','ground'):
        for scenario in specs.scenarios(stage,'validation'):
            name=specs.run_name(scenario)
            run=ROOT/(f'artifacts/runs/tiny-reference-{name}' if stage=='jump' else f'artifacts/phase2b/reference/{name}')
            reference_files += [run/'manifest.json',*[run/'frames'/f'{i:06d}.png' for i in range(240)]]
    tests_ok=(tests.get('status')=='pass' and tests.get('failures')==0 and tests.get('errors')==0
              and tests.get('tests_run',0)>0 and tests.get('protocol_sources')==specs.protocol_sources())
    for rel,expected in tests.get('evidence_files',{}).items():
        p=ROOT/rel; tests_ok &= p.is_file() and sha256(p)==expected; files.append(p)
    legacy=specs.verify_legacy(); specs.verify_source()
    gates={'runtime_determinism':{'status':'pass' if native_ok else 'fail'},
        'tracking_calibration':{'status':'pass' if all(v['status']=='pass' for v in negatives.values()) else 'fail','negative_controls':negatives},
        'manual_calibration':{'status':'pass' if all(v['status']=='pass' for g in groups.values() for v in g.values()) else 'fail','groups':groups},
        'integrity_baseline':integrity_gates['baseline'],'integrity_negative_controls':integrity_gates['negative_controls'],
        'wall_coyote_controls':overlap_gate,
        'software_tests':{'status':'pass' if tests_ok else 'fail','tests_run':tests.get('tests_run')},
        'legacy_sources':{'status':'pass','count':len(legacy)}}
    evidence += [p for p in work.rglob('*') if p.is_file()]
    files += [specs.CONFIG/f'heart-{s}-annotation.json' for s in ('jump','ground')]
    files += [ROOT/'config/tiny-annotation.json',ROOT/'config/phase2b/tiny-annotation.json']
    for annotation_path in list(files[-4:]):
        annotation=read_json(annotation_path)
        files.append(Path(annotation['template_run'])/'frames'/f"{annotation.get('template_frame',0):06d}.png")
    result={'schema_version':1,'status':'pass' if all(g['status']=='pass' for g in gates.values()) else 'fail',
        'gates':gates,'protocol_sources':specs.protocol_sources(),'files':_map(files),'evidence_files':_map(evidence),
        'reference_files':_map(reference_files),'calibration_analyses':analyses,
        'runtime_budget_hint':{'capture_seconds_estimate':6,'integrity_pair_seconds_estimate':90,
             'jump_final_seconds_estimate':300,'ground_final_seconds_estimate':540,
             'note':'Engineering estimates; source/code errors still consume one candidate. Reserve final-validation time.'}}
    write_json(output,result)
    return result
