"""Godot adapter using the common pixel tracker and established motion math.

The old game trackers are never invoked. Native state is confined to separate
preservation checks; neither fitting nor motion comparison receives it.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path

from kiroclops.common import ROOT
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.visual.package import copy_package
from kiroclops.visual.compat import import_legacy_run, selection_from_legacy
from kiroclops.cloud.execution import analyze_package
from kiroclops.integration.storage import copy_project
from kiroclops.portable_v2 import runtime, integrity, measurement
from kiroclops.portable_v2.specs import scenarios, run_name


def _stage(value):
    if value not in ('jump','ground'):
        raise VisualError('unsupported_target','Game stage must be jump or ground')
    return value


class _Cancellation:
    """Bridge the local session callback to the native runtime Event contract."""
    def __init__(self, cancel, check):
        self.cancel,self.check=cancel,check

    def is_set(self):
        self.check()  # Includes the original session deadline; never resets it.
        return bool(self.cancel() if callable(self.cancel) else self.cancel.is_set() if self.cancel else False)


def _annotation(stage, game):
    if game=='tiny':
        return ROOT/('config/tiny-annotation.json' if stage=='jump' else 'config/phase2b/tiny-annotation.json')
    if game=='heart':return ROOT/f'config/phase2c-v2/heart-{stage}-annotation.json'
    raise VisualError('unsupported_target','No qualified visual annotation for this game')


def observation_to_motion(observations, manifest, annotation, stage):
    """Project common coordinates into existing domain metrics, without re-tracking."""
    _stage(stage)
    if (manifest.get('fps')!=60 or len(manifest.get('frames',[]))!=240
            or len(observations.get('objects',[]))!=1):
        raise VisualError('capture_timing_failure','Game metric projection requires one object and 240 native 60 Hz frames')
    obj=observations['objects'][0]
    if len(obj.get('frames',[]))!=240 or len(observations.get('camera',[]))!=240:
        raise VisualError('capture_timing_failure','Common observations are incomplete')
    body=annotation.get('body_height_px')
    if type(body) not in (int,float) or not math.isfinite(body) or body<=0:
        raise VisualError('invalid_selection','A fixed, visually measured body height is required')
    frames=[]
    for index,(native,observed,camera) in enumerate(zip(manifest['frames'],obj['frames'],observations['camera'])):
        if (native.get('frame_index')!=index or native.get('physics_tick')!=60+index
                or observed.get('index')!=index or abs(observed['time_s']-index/60)>1e-5
                or abs(native['time_s']-observed['time_s'])>1e-8):
            raise VisualError('capture_timing_failure','Frame timing does not match native input receipts')
        frames.append({'frame_index':index,'physics_tick':native['physics_tick'],'time_s':observed['time_s'],
            'actions':deepcopy(native.get('actions',[])),
            **{k:observed.get(k) for k in ('x','y','score','margin','valid')},
            'mirrored':bool(observed.get('mirrored',False)),
            'template_rotation':observed.get('rotation_deg',0),'camera_valid':camera['status']=='pass',
            'camera_shift_px':camera.get('max_shift_px'),'height_body':None,'x_body':None})
    # Only immutable input timing enters metric extraction, never native position.
    scenario=deepcopy(manifest['scenario'])
    errors=[str(e.get('code','tracking_failure')) for e in observations.get('errors',[])]
    if stage=='jump':
        from kiroclops.analysis import measure_jump
        events,metrics,metric_errors=measure_jump(frames,scenario,60,body)
        extra={}
    else:
        from kiroclops.ground.analysis import measure_ground
        _,phases,metrics,metric_errors=measure_ground(frames,scenario,body)
        events={'first_input_tick':30,'final_release_tick':phases[-1]['start'],
                'reversal_ticks':[p['start'] for p in phases if p['name']=='reversal']}
        extra={'phases':phases,'mechanic':'ground'}
    errors+=metric_errors
    camera_valid=all(f['camera_valid'] for f in frames)
    ratio=sum(bool(f['valid']) for f in frames)/240
    if not camera_valid:errors.append('fixed-camera condition failed')
    if ratio<.95:errors.append('valid frame ratio below 0.95')
    return {'schema_version':1,'status':'rejected' if errors else 'ok','game':manifest['game'],
            'variant':manifest['variant'],'scenario':scenario,'fps':60,'body_height_px':float(body),
            'valid_ratio':ratio,'camera_valid':camera_valid,'events':events,'metrics':metrics,
            'frames':frames,'errors':errors,'annotation_validation':{'status':'pending','reason':'New common measurements require independent raw-pixel control review'},
            'provenance':{'measurement':'common visual core; rendered pixels only',
                'package_sha256':observations['package_sha256'],'observation_sha256':digest(observations),
                'opencv_version':observations.get('opencv_version'),'domain_math_reuse_only':True},**extra}


def analyze_game_run(run, stage, output, *, annotation=None, check=lambda:None, progress=lambda *args:None, candidate_sha256=None):
    """Read legacy-compatible raw frames, write only a new common package/result."""
    stage=_stage(stage);run=Path(run).resolve();output=Path(output).resolve()
    if output==run or output.is_relative_to(run):
        raise VisualError('unsafe_path','Common replay output must not mutate its raw source tree')
    if output.exists():raise VisualError('output_exists','Use a fresh common measurement directory')
    check();manifest=read_json(run/'manifest.json')
    annotation=Path(annotation or _annotation(stage,manifest['game']))
    annotation_value=read_json(annotation)
    output.mkdir(parents=True)
    import_legacy_run(run,output/'package')
    selection=selection_from_legacy(run,annotation)
    # Keep the qualified bounded search extent while sharing the new algorithm.
    selection['objects'][0]['search_radius']=max(8,math.ceil(annotation_value['body_height_px']*.75))
    observations=analyze_package(output/'package',selection,
        {'min_valid_ratio':.95,'require_static_camera':True,
         'camera_max_shift_px':annotation_value.get('camera_max_shift_px',1.)},check,progress,
         output=output/'common-analysis',candidate_sha256=candidate_sha256)
    report=observation_to_motion(observations,manifest,annotation_value,stage)
    report['run_id']=str(run)
    report['provenance'].update(manifest_sha256=file_sha256(run/'manifest.json'),annotation_sha256=file_sha256(annotation))
    write_json(output/'selection.json',selection)
    write_json(output/'observations.json',observations)
    write_json(output/'analysis.json',report)
    return {'scenario':report['scenario'],'package':str(output/'package'),'analysis':str(output/'analysis.json'),
            'raw_run':str(run),'measurement_status':report['status'],'body_height_px':report['body_height_px']}


def _canonical_record(record, scenario, stage):
    """Bind an adapter envelope to a registered recipe without rewriting evidence.

    Historical Tiny jump manifests predate scenario IDs. Their name alone is
    insufficient: every recorded input/physics step and the import provenance
    must agree before assigning the current canonical envelope identity.
    """
    def need(condition, message, code='scenario_mismatch'):
        if not condition:raise VisualError(code, message)

    stage=_stage(stage)
    need(isinstance(scenario,dict) and scenario.get('split') in ('calibration','validation'),
         'Missing registered game recipe')
    need(any(digest(scenario)==digest(s) for s in scenarios(stage,scenario['split'])),
         'Game recipe is not in the registered experiment')
    raw_path=Path(record['raw_run'])/'manifest.json'
    native=read_json(raw_path);raw=native.get('scenario')
    need(isinstance(raw,dict),'Native recording has no scenario')
    required=('split','repeat','spawn_offset')+(('hold_ticks','start_tick') if stage=='jump' else ('id','events'))
    for key in required:
        need(key in raw and digest(raw[key])==digest(scenario[key]),'Native recipe differs: '+key)
    for key in ('id','mechanic','fps','frame_count','warmup_ticks','events'):
        need(key not in raw or digest(raw[key])==digest(scenario[key]),'Native recipe differs: '+key)
    need('variant' not in raw or raw['variant']==native.get('variant'),'Native recipe variant differs')
    need(record.get('scenario')==raw,'Analysis envelope differs from its original native recipe')
    frames=native.get('frames',[])
    need(native.get('status')=='ok' and native.get('fps')==60 and native.get('frame_count')==240
         and len(frames)==240 and native.get('warmup_ticks',60)==60,
         'Native recording is not a complete 60 Hz/240 frame run','capture_timing_failure')
    actions=set();previous=set();timeline=[]
    for index,frame in enumerate(frames):
        time_s=frame.get('time_s')
        need(frame.get('frame_index')==index and frame.get('physics_tick')==60+index
             and type(time_s) in (int,float) and math.isfinite(time_s) and abs(time_s-index/60)<=1e-5,
             'Native frame/physics mapping differs','capture_timing_failure')
        for event in scenario['events']:
            if event['tick']==index:
                if event['pressed']:actions.add(event['action'])
                else:actions.discard(event['action'])
        actual=frame.get('actions')
        need(isinstance(actual,list) and all(isinstance(a,str) for a in actual)
             and len(actual)==len(set(actual)) and set(actual)==actions,
             'Recorded actions differ from registered recipe at frame '+str(index),'invalid_input_timeline')
        for action in sorted(previous ^ actions):
            timeline.append({'time_s':time_s,'action':action,'pressed':action in actions})
        previous=set(actions)
    package_path=Path(record['package'])/'manifest.json';package=read_json(package_path)
    metadata=package.get('metadata',{});native_sha=file_sha256(raw_path)
    need(package.get('kind')=='VideoPackage' and package.get('schema_version')==1
         and package.get('package_sha256')==digest({k:v for k,v in package.items() if k!='package_sha256'}),
         'Imported package manifest identity differs','source_changed')
    need(metadata.get('adapter')=='legacy-godot-v1' and metadata.get('source_manifest_sha256')==native_sha
         and metadata.get('scenario')==raw and metadata.get('source_id')==raw_path.parent.name
         and metadata.get('domain')=='game' and metadata.get('game')==native.get('game')
         and metadata.get('variant')==native.get('variant'),
         'Imported package does not bind the native recording','source_changed')
    imported=package.get('frames',[])
    need(len(imported)==240 and all(f.get('index')==i and f.get('time_s')==frames[i]['time_s']
         for i,f in enumerate(imported)),'Imported frame timeline differs','capture_timing_failure')
    need(package.get('inputs')=={'provenance':'recorder_verified','events':timeline},
         'Imported input timeline differs from recorded actions','invalid_input_timeline')
    if 'raw_frame_hashes' in native:
        need(native['raw_frame_hashes']=={f'{i:06d}.png':f.get('sha256') for i,f in enumerate(imported)},
             'Imported pixel hashes differ from native receipt','source_changed')
    binding={'policy':'registered-native-scenario-envelope-v1','canonical_scenario_sha256':digest(scenario),
             'raw_scenario_sha256':digest(raw),'source_manifest_sha256':native_sha,
             'package_sha256':package['package_sha256'],'input_timeline_sha256':digest(package['inputs'])}
    if record.get('analysis'):
        report=read_json(record['analysis']);provenance=report.get('provenance',{})
        need(report.get('scenario')==raw and provenance.get('manifest_sha256')==native_sha
             and provenance.get('package_sha256')==package['package_sha256'],
             'Analysis belongs to a different native recording','source_changed')
        binding['analysis_sha256']=file_sha256(record['analysis'])
    return {**deepcopy(record),'scenario':deepcopy(scenario),'scenario_binding':binding}


def _reference(stage,split,output,check,progress,*,analyze=True):
    records=[]
    for scenario in scenarios(stage,split):
        check();name=run_name(scenario)
        run=ROOT/(f'artifacts/runs/tiny-reference-{name}' if stage=='jump' else f'artifacts/phase2b/reference/{name}')
        if analyze:
            record=analyze_game_run(run,stage,Path(output)/name,check=check,progress=progress)
        else:
            package=Path(output)/name/'package';import_legacy_run(run,package)
            record={'scenario':read_json(run/'manifest.json')['scenario'],'package':str(package),
                    'raw_run':str(run),'measurement_status':'not_run','analysis':None}
        records.append(_canonical_record(record,scenario,stage))
    return records


def _capture(project,stage,split,output,variant,check,progress,cancel,*,analyze=True):
    project=Path(project);output=Path(output)
    check();before=runtime.source_hashes(project)
    # Editor import can create .uid sidecars. Keep those outside the sealed
    # candidate even though they are not part of the native source hash policy.
    rendered=output/'project';copy_project(project,rendered)
    if runtime.source_hashes(rendered)!=before:raise VisualError('source_changed','Capture copy differs from the immutable source')
    runtime.import_project(rendered,output/'import',cancel=cancel)
    records=[]
    for index,scenario in enumerate(scenarios(stage,split)):
        check();name=run_name(scenario)
        recipe={k:deepcopy(v) for k,v in scenario.items() if k not in ('mechanic','fps')}
        recipe['variant']=variant
        raw=output/'raw'/name
        runtime.capture_project(rendered,raw,recipe,cancel=cancel)
        if analyze:
            record=analyze_game_run(raw,stage,output/'measured'/name,check=check,progress=progress,candidate_sha256=digest(before))
        else:
            package=output/'packages'/name;import_legacy_run(raw,package)
            record={'scenario':read_json(raw/'manifest.json')['scenario'],'package':str(package),
                    'raw_run':str(raw),'measurement_status':'not_run','analysis':None}
        records.append(_canonical_record(record,scenario,stage))
        progress('game_capture',index+1,len(scenarios(stage,split)))
    check()
    if runtime.source_hashes(project)!=before or runtime.source_hashes(rendered)!=before:
        raise VisualError('source_changed','Source changed during native capture')
    return records


def _reports(records):return [r['analysis'] for r in records]


def _export(directory,records,role):
    result=[]
    for record in records:
        destination=Path(directory)/'target/.kiroclops-evidence'/role/run_name(record['scenario'])
        copy_package(record['package'],destination)
        result.append({'scenario':record['scenario'],'package':str(destination)})
    return result


def _qualified(records):
    if any(r['measurement_status']!='ok' for r in records):
        raise VisualError('tracking_failure','Common pixel tracking did not qualify every game calibration run')


def _preservation(project,baseline,output,stage,check,cancel):
    check();result=integrity.run_checks(Path(project),Path(baseline),Path(output),stage,cancel=cancel);check()
    if result.get('status') not in ('pass','fail'):
        raise VisualError(result.get('code','preservation_capture_failure'),result.get('message','Native preservation suite did not complete'))
    return {'status':result['status'],'failed_cases':result.get('failed_cases'),
            'mismatches':result.get('mismatches',[]),'evidence_path':str(Path(output)/'comparison.json'),
            'scope':'Qualified native preservation is separate from shared-pixel motion fitting.'}


def _visual_wall(project,output,candidate_id,check,progress,cancel):
    """Run all eight pixel boundary cases under the session's existing lease.

    This deliberately uses the qualified renderer primitives rather than
    reacquiring run_qualification's global capture lease inside the worker.
    """
    from . import visual_boundary_v4 as boundary
    from kiroclops.visual.events import extract_events
    from kiroclops.visual.package import read_image
    project=Path(project).resolve();output=Path(output).resolve();check()
    if output.exists():raise VisualError('output_exists','Visual-wall output must be fresh')
    original=runtime.source_hashes(project);registered=boundary.policy()
    rendered=output/'project';copy_project(project,rendered)
    if runtime.source_hashes(rendered)!=original:raise VisualError('source_changed','Boundary copy differs from selected source')
    overlay=rendered/boundary.OVERLAY;overlay.mkdir()
    for name in ('Boundary.tscn','boundary.gd'):(overlay/name).write_bytes((boundary.ADAPTER/name).read_bytes())
    added={f'{boundary.OVERLAY}/{name}':file_sha256(boundary.ADAPTER/name) for name in ('Boundary.tscn','boundary.gd')}
    expected_sources={**original,**added}
    if runtime.source_hashes(rendered)!=expected_sources:raise VisualError('source_changed','Boundary fixture has unexpected source changes')
    write_json(output/'qualification-start.json',{'protocol':registered,'original_sources':original,'cases':boundary.cases()})
    runtime.import_project(rendered,output/'import',cancel=cancel);check()
    expected={'approach':'visible_approach','parallel':'visible_parallel_motion',
              'rebound':'visible_departure_after_approach','near_miss':'distant_motion'}
    rows=[];records=[]
    for index,scenario in enumerate(boundary.cases()):
        check();raw=output/'raw'/scenario['id'];boundary._render(rendered,raw,scenario,cancel)
        fixture=boundary.fixture_validity(raw,scenario)
        package=output/'packages'/scenario['id'];import_legacy_run(raw,package)
        selection=selection_from_legacy(raw,_annotation('ground','heart'))
        obj=selection['objects'][0];obj['horizontal_mirror']=True
        obj['roi'][0]+=1;obj['roi'][2]-=2;obj['anchor'][0]-=1
        selection['boundaries']=[boundary.visible_wall(read_image(raw/'frames/000000.png'),scenario['wall_side'])]
        destination=output/'measurement'/scenario['id']
        observations=analyze_package(package,selection,{'require_static_camera':True,'min_valid_ratio':.95},check,progress,
                                     output=destination/'common-analysis',candidate_sha256=digest(original))
        # Same qualified boundary policy; generic event axes remain recorded in
        # the EventSet. Collision telemetry never enters this classifier.
        events=extract_events(observations,selection,{'event_options':{'boundary_tolerance_px':12}})
        measured=boundary.classify(observations,events)
        for name,value in [('selection',selection),('observations',observations),('events',events),('result',measured)]:
            write_json(destination/f'{name}.json',value)
        passed=fixture['status']=='pass' and measured['status']=='observed' and measured['classification']==expected[scenario['kind']]
        rows.append({'id':scenario['id'],'expected':expected[scenario['kind']],'actual':measured['classification'],'pass':passed,
            'package':str(package),'measurement':str(destination),'runtime_receipt':str(raw/'runtime-receipt.json'),
            'fixture_status':fixture['status'],'fixture_validity_path':str(raw/'private-fixture-validity.json'),
            'source':'Selected sealed controller; rendered native pixels; no collision telemetry.'})
        records.append({'scenario':scenario,'package':str(package),'raw_run':str(raw),'selection':str(destination/'selection.json'),
            'observations':str(destination/'observations.json'),'events':str(destination/'events.json'),
            'analysis':str(destination/'result.json'),'measurement_status':observations['status'],
            'candidate_id':candidate_id,'source_hashes':original,'role':'candidate','mechanic':'visual_boundary'})
        progress('visual_boundary',index+1,8)
    check()
    if runtime.source_hashes(project)!=original or runtime.source_hashes(rendered)!=expected_sources or boundary.policy()!=registered:
        raise VisualError('source_changed','Selected controller, fixture or analysis policy changed during boundary capture')
    result={'schema_version':1,'protocol':registered,
        'status':'invalid_fixture_for_behavior' if any(r['fixture_status']!='pass' for r in rows) else 'pass' if all(r['pass'] for r in rows) else 'fail',
        'rows':rows,'source_preserved':True,'original_sources':original,'project_sources':expected_sources,
        'selected_source':str(project),'candidate_id':candidate_id,'physics_collision_inferred':False,
        'independent_review_status':'pending'}
    write_json(output/'result.json',result);write_json(output/'candidate.json',records)
    return {'status':result['status'],'passed':sum(r['pass'] for r in rows),'required':8,
        'candidate_id':candidate_id,'source_hashes':original,'evidence_path':str(output/'result.json'),
        'evidence_sha256':file_sha256(output/'result.json'),'records_path':str(output/'candidate.json'),
        'independent_review_status':'pending','physics_collision_inferred':False}


def run_game_job(directory,job,state,check,progress,cancel):
    """Local session dispatch. Held-out references open only after final selection."""
    from .prepared_evidence import store_prepared,load_prepared
    directory=Path(directory);stage=_stage(state.get('experiment',{}).get('stage'))
    operation=job.get('operation',job.get('action'));output=directory/'measurements'/job['job_id']
    mode=state['mode']
    cancel=_Cancellation(cancel,check)
    if operation=='report':
        from .reporting import build_report
        return build_report(directory)
    if operation=='prepare':
        measured=mode!='baseline'
        refs=_reference(stage,'calibration',output/'reference',check,progress,analyze=measured)
        before=_capture(state['baseline'],stage,'calibration',output/'before','before',check,progress,cancel,analyze=measured)
        result={'status':'ready','reference_media':_export(directory,refs,'calibration/reference'),
                'before_media':_export(directory,before,'calibration/before'),'tracking_backend':'common_visual_core',
                'independent_review_status':'pending'}
        if measured:
            _qualified(refs+before)
            profile=measurement.fit_reference(stage,_reports(refs),output/'profile.json')
            public_profile={k:deepcopy(v) for k,v in profile.items() if k!='provenance'}
            public_profile['provenance']={'source':'common pixel measurements of exported calibration packages',
                                          'calibration_packages':result['reference_media']}
            profile_path=directory/'target/.kiroclops-evidence/calibration/profile.json';write_json(profile_path,public_profile)
            result.update(profile=public_profile,profile_path=str(profile_path),
                units={'position':'body heights','time':'seconds','reference_body_height_px':refs[0]['body_height_px'],
                       'target_body_height_px':before[0]['body_height_px']})
        write_json(output/'reference.json',refs);write_json(output/'before.json',before)
        prepared={'reference':str(output/'reference.json'),'before':str(output/'before.json'),'stage':stage,
                  'source_bindings':{str(Path(state['baseline']).resolve()):runtime.source_hashes(Path(state['baseline']))}}
        if stage=='ground':
            original=state['experiment'].get('original_baseline')
            if not original:raise VisualError('missing_original_baseline','Ground requires the original jump denominator')
            prepared['source_bindings'][str(Path(original).resolve())]=runtime.source_hashes(Path(original))
            if measured:
                jump_refs=_reference('jump','calibration',output/'jump/reference',check,progress)
                jump_before=_capture(original,'jump','calibration',output/'jump/before','before',check,progress,cancel)
                _qualified(jump_refs+jump_before)
                write_json(output/'jump/reference.json',jump_refs);write_json(output/'jump/before.json',jump_before)
                prepared['jump']={'reference':str(output/'jump/reference.json'),'before':str(output/'jump/before.json')}
        indexes={'reference':output/'reference.json','before':output/'before.json'}
        if prepared.get('jump'):
            indexes.update({'jump_reference':output/'jump/reference.json','jump_before':output/'jump/before.json'})
        result['prepared_evidence_sha256']=store_prepared(directory,job,state,prepared,indexes,
            {'profile':output/'profile.json'} if measured else None)
        return result
    if operation not in ('evaluate','finalize'):raise VisualError('unsupported_operation',str(operation))
    source=job['source_path'];prepared=load_prepared(directory,state)
    if prepared['stage']!=stage:raise VisualError('wrong_stage','Prepared and current game stages differ')
    for path,expected in prepared.get('source_bindings',{}).items():
        if runtime.source_hashes(Path(path))!=expected:
            raise VisualError('baseline_changed','A prepared starting-source binding changed')
    if operation=='evaluate':
        if mode=='baseline':
            candidates=_capture(source,stage,'calibration',output/'candidate','after',check,progress,cancel,analyze=False)
            return {'eligible':True,'score':None,'neutral_capture':True,
                    'candidate_media':_export(directory,candidates,f"candidates/{job['candidate_id']}"),
                    'message':'Raw recording completed. No pixel metrics or preservation feedback were supplied.'}
        preserve=_preservation(source,state['baseline'],output/'preservation',stage,check,cancel)
        if preserve['status']=='fail':
            return {'eligible':False,'score':None,'motion_status':'not_run','preservation':preserve,'failure_class':'preservation_violation'}
        refs,before=read_json(prepared['reference']),read_json(prepared['before'])
        candidates=_capture(source,stage,'calibration',output/'candidate','after',check,progress,cancel)
        _qualified(candidates)
        compared=measurement.compare_motion(stage,_reports(refs),_reports(before),_reports(candidates),'calibration')
        jump=None
        if stage=='ground':
            jump_refs=read_json(prepared['jump']['reference']);jump_before=read_json(prepared['jump']['before'])
            jump_after=_capture(source,'jump','calibration',output/'jump/candidate','after',check,progress,cancel)
            _qualified(jump_after)
            jump=measurement.compare_motion('jump',_reports(jump_refs),_reports(jump_before),_reports(jump_after),'calibration')
            write_json(output/'jump/candidate.json',jump_after);write_json(output/'jump/comparison.json',jump)
        write_json(output/'comparison.json',compared);write_json(output/'candidate.json',candidates)
        profile=read_json(Path(prepared['reference']).parent/'profile.json')
        return {'eligible':bool(compared['pass'] and (jump is None or jump['pass'])),'score':compared['mean_rmse_body'],
                'comparison':measurement.public_comparison(compared),'preservation':preserve,
                'jump_preservation':measurement.public_comparison(jump) if jump else None,
                'diagnostics':measurement.diagnostics(stage,_reports(refs),_reports(candidates),profile),
                'candidate_media':_export(directory,candidates,f"candidates/{job['candidate_id']}"),
                'independent_review_status':'pending'}
    if not state.get('sealed_at') or state.get('selected_candidate_id')!=job.get('candidate_id'):
        raise VisualError('unsealed_candidate','Held-out capture requires this session selected sealed candidate')
    refs=_reference(stage,'validation',output/'reference',check,progress)
    before=_capture(state['baseline'],stage,'validation',output/'before','before',check,progress,cancel)
    final=_capture(source,stage,'validation',output/'candidate','after',check,progress,cancel)
    _qualified(refs+before+final)
    compared=measurement.compare_motion(stage,_reports(refs),_reports(before),_reports(final),'validation')
    preserve=_preservation(source,state['baseline'],output/'preservation',stage,check,cancel)
    jump=None
    if stage=='ground':
        original=state['experiment'].get('original_baseline')
        if not original:raise VisualError('missing_original_baseline','Ground preservation requires the original jump denominator')
        jump_refs=_reference('jump','validation',output/'jump/reference',check,progress)
        jump_before=_capture(original,'jump','validation',output/'jump/before','before',check,progress,cancel)
        jump_after=_capture(source,'jump','validation',output/'jump/candidate','after',check,progress,cancel)
        _qualified(jump_refs+jump_before+jump_after)
        jump=measurement.compare_motion('jump',_reports(jump_refs),_reports(jump_before),_reports(jump_after),'validation')
        for name,rows in [('reference',jump_refs),('before',jump_before),('candidate',jump_after)]:write_json(output/f'jump/{name}.json',rows)
    for name,rows in [('reference',refs),('before',before),('candidate',final)]:write_json(output/f'{name}.json',rows)
    wall=_visual_wall(source,output/'boundary',job['candidate_id'],check,progress,cancel)
    motion_pass=bool(compared['pass'] and (jump is None or jump['pass']))
    result={'pass':bool(motion_pass and preserve['status']=='pass' and wall['status']=='pass'),
            'passed':compared['passed'],'required':compared['required'],'comparison':compared,
            'jump_preservation':jump,'preservation':preserve,'independent_review_status':'pending',
            'source_review_status':'pending','visual_wall_status':wall['status'],'visual_wall':wall,
            'motion_status':'pass' if motion_pass else 'fail',
            'numerical_acceptance_only':True,'overall_acceptance':'pending'}
    write_json(output/'comparison.json',result)
    return result


def qualification_game_corpus(output, *, check=lambda:None, progress=lambda *args:None):
    """Six existing real-video controls, remeasured without native execution.

    Historical held-out recordings are now a public engineering fixture corpus.
    They are not presented as new unseen model trials or new source acceptance.
    """
    output=Path(output).resolve()
    if output.exists():raise VisualError('output_exists','Game corpus output must be fresh')
    analyzer_paths=[Path(__file__).resolve(),*sorted((ROOT/'kiroclops/visual').glob('*.py')),
                    ROOT/'kiroclops/analysis.py',ROOT/'kiroclops/ground/analysis.py',
                    ROOT/'kiroclops/ground/comparison.py',ROOT/'kiroclops/integration/ground_comparison_v2.py',
                    ROOT/'kiroclops/portable_v2/measurement.py',ROOT/'kiroclops/portable_v2/specs.py']
    analyzer_hashes={path.relative_to(ROOT).as_posix():file_sha256(path) for path in analyzer_paths}
    write_json(output/'analyzer-registration.json',{'schema_version':1,'source_hashes':analyzer_hashes,
        'scope':'Existing raw image engineering fixtures; analyzer registered before measurement.'})
    source_root=ROOT/'sessions'
    cases=[('game-jump-correct','jump','h08','correct'),
           ('game-ground-correct','ground','reverse24','correct'),
           ('game-jump-height-wrong','jump','h08','behavioral_deviation'),
           ('game-jump-duration-wrong','jump','h14','behavioral_deviation'),
           ('game-ground-speed-wrong','ground','r18','behavioral_deviation'),
           ('game-ground-reversal-wrong','ground','reverse24','behavioral_deviation')]
    rows=[]
    for index,(name,stage,scenario_id,expected) in enumerate(cases):
        check();destination=output/name;run=f'{scenario_id}-x0-r1'
        historic=source_root/f'heart-heart-v2-01-02-{stage}'/'final'/stage
        sources={'reference':ROOT/(f'artifacts/runs/tiny-reference-{run}' if stage=='jump' else f'artifacts/phase2b/reference/{run}'),
                 'before':historic/'before'/run,
                 'candidate':historic/('after' if expected=='correct' else 'before')/run}
        measured={role:analyze_game_run(path,stage,destination/role,check=check,progress=progress) for role,path in sources.items()}
        _qualified(list(measured.values()))
        ref,old,new=[read_json(measured[role]['analysis']) for role in ('reference','before','candidate')]
        if stage=='jump':
            compared=measurement._jump_row(ref,old,new,True)
        else:
            from kiroclops.ground.comparison import _triplet
            from kiroclops.integration.ground_comparison_v2 import speed_triplet
            compared=speed_triplet(_triplet,ref,old,new,require_improvement=True)
        observed='correct' if compared['pass'] else 'behavioral_deviation'
        settings={'stage':stage,'scenario':ref['scenario'],'body_heights':{k:read_json(v['analysis'])['body_height_px'] for k,v in measured.items()},
                  'policy':'Existing pure game-motion gates; fixed reference body scale, real frame time, no time warp',
                  'thresholds_sha256':digest(__import__('kiroclops.portable_v2.specs',fromlist=['experiment']).experiment(stage)['acceptance'])}
        write_json(destination/'contract.json',settings);write_json(destination/'result.json',compared)
        native_hashes={str(path/'manifest.json'):file_sha256(path/'manifest.json') for path in sources.values()}
        row={'id':name,'domain':'game','kind':'recorded_behavior','expected_class':expected,'observed_class':observed,
             'classification_pass':expected==observed,'package_dir':measured['candidate']['package'],
             'reference_package_dir':measured['reference']['package'],'selection_path':str(destination/'candidate/selection.json'),
             'contract_path':str(destination/'contract.json'),'analysis_path':measured['candidate']['analysis'],
             'result_path':str(destination/'result.json'),'source_hashes':native_hashes,'settings_sha256':digest(settings),
             'independent_review_status':'pending','provenance':'Previously completed, source-bound native recordings; fresh common-pixel measurement',
             'interpretation':'These six per-scenario engineering controls do not establish full game or new-model acceptance.'}
        write_json(destination/'sample.json',row);rows.append(row)
        progress('game_qualification',index+1,len(cases))
    if any(file_sha256(ROOT/path)!=sha for path,sha in analyzer_hashes.items()):
        raise VisualError('source_changed','Game corpus analyzer changed during measurement')
    write_json(output/'manifest.json',{'schema_version':1,'kind':'GameVisualQualificationCorpus','samples':rows,
        'source_hashes':analyzer_hashes,'source_hashes_unchanged':True,
        'classification_status':'pass' if all(r['classification_pass'] for r in rows) else 'fail',
        'independent_review_status':'pending','status':'pending'})
    return rows
