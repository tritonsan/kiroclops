"""Pixel-reference sessions for the existing Heart recorder only.

The common video core remains variable-rate. This adapter explicitly requires
240 native 60 Hz frames and a supplied action timeline; it never invents one.
"""
from copy import deepcopy
import math
from pathlib import Path
import statistics

from kiroclops.common import ROOT
from kiroclops.visual.schema import VisualError,read_json,write_json,digest,file_sha256
from kiroclops.visual.package import load_package
from kiroclops.visual.compat import import_legacy_run,selection_from_legacy
from kiroclops.portable_v2 import runtime
from .session import source_hashes,copy_project

LIMITS={'minimum_valid_ratio':.95,'maximum_curve_rmse_body':.15,'required_improvement_fraction':.5,
        'maximum_height_relative_error':.1,'maximum_speed_relative_error':.1}


def validate_project(project):
    """Check actual Heart resources, not merely the name of a GDScript file."""
    project=Path(project)
    lock=read_json(ROOT/'config/phase2c-v2/heart-source.json')['files']
    required=('player.tscn','PlayerMovementData.gd','DefaultMovementData.tres','level_tile_map.tscn','level_one.tscn')
    if any(not (project/p).is_file() or file_sha256(project/p)!=lock[p] for p in required):
        raise VisualError('unsupported_media_adapter','Game media requires the supported Heart resources and player.gd controller.')
    if not (project/'player.gd').is_file():raise VisualError('unsupported_media_adapter','Heart controller is missing.')
    return {'target_id':'heart','protected_resource_hashes':{p:lock[p] for p in required}}


def _grid(manifest):
    frames=manifest['frames']
    if len(frames)!=240 or any(abs(f['time_s']-frames[0]['time_s']-i/60)>1e-5 for i,f in enumerate(frames)):
        raise VisualError('unsupported_game_media_timing','Heart adapter requires 240 contiguous 60 Hz frames; the shared video core is not restricted.')


def _scenario(manifest,stage):
    _grid(manifest)
    if stage not in ('jump','ground'):raise VisualError('needs_input_binding','Select game_stage=jump or ground explicitly.')
    events=[];origin=manifest['frames'][0]['time_s']
    for event in manifest['inputs']['events']:
        tick_value=(event['time_s']-origin)*60;tick=round(tick_value)
        if abs(tick-tick_value)>1e-5 or event.get('action') not in ('left','right','jump') or type(event.get('pressed')) is not bool:
            raise VisualError('invalid_input_binding','Heart inputs require exact native-tick left/right/jump press and release events.')
        events.append({'tick':tick,'action':event['action'],'pressed':event['pressed']})
    scenario={'id':'uploaded-game-01','split':'calibration','repeat':1,'spawn_offset':0,
              'events':events,'warmup_ticks':60,'frame_count':240}
    try:scenario=runtime.canonical_scenario(scenario)
    except runtime.PortableRuntimeError as exc:raise VisualError('invalid_input_binding',str(exc)) from exc
    if not events or min(e['tick'] for e in events)<3 or max(e['tick'] for e in events)>208:
        raise VisualError('insufficient_observability','Supply at least three pre-input frames and thirty post-release frames.')
    if stage=='jump':
        if len(events)!=2 or [e['action'] for e in events]!=['jump','jump'] or [e['pressed'] for e in events]!=[True,False]:
            raise VisualError('unsupported_game_media_recipe','Jump calibration supports one supplied press/release, with no directional input.')
    elif any(e['action']=='jump' for e in events):
        raise VisualError('unsupported_game_media_recipe','Ground calibration supports directional transitions only; mixed mechanics need another adapter.')
    return scenario


def make_binding(project,manifest,selection,object_map,stage,body_height=None):
    identity=validate_project(project['project'])
    if manifest['inputs']['provenance']=='unknown' or not manifest['inputs']['events']:
        return {'status':'needs_input_binding','reason':'Supply known or user-declared native action transitions; no inputs are inferred.','model_started':False}
    if len(selection['objects'])!=1 or object_map!={selection['objects'][0]['id']:'character'}:
        raise VisualError('invalid_binding','Map one selected reference character to character.')
    height=body_height if body_height is not None else selection['objects'][0].get('body_height_px')
    if type(height) not in (int,float) or not math.isfinite(height) or not 0<height<=manifest['image_size'][1]:
        raise VisualError('needs_input_binding','Supply a measured reference body height in natural pixels.')
    return {'schema_version':1,'kind':'MediaCaptureBinding','preset':'recorded-timeline','status':'bound',
        'reference_package_sha256':manifest['package_sha256'],'selection_sha256':digest(selection),
        'object_map':deepcopy(object_map),'game_stage':stage,'body_height_px':float(height),
        'scenario':_scenario(manifest,stage),'target_id':identity['target_id'],
        'input_provenance':manifest['inputs']['provenance'],'heldout_status':'not_provided',
        'claim':'conditional_on_user_declared_inputs' if manifest['inputs']['provenance']=='user_declared' else 'recorded_input_response',
        'spatial_alignment':'initial_anchor_body_units','relative_motion':[],'model_started':False}


def validate_binding(binding,project,manifest,selection):
    if (binding.get('kind')!='MediaCaptureBinding' or binding.get('schema_version')!=1 or binding.get('status')!='bound'
            or binding.get('reference_package_sha256')!=manifest['package_sha256'] or binding.get('selection_sha256')!=digest(selection)
            or binding.get('input_provenance')!=manifest['inputs']['provenance'] or manifest['inputs']['provenance']=='unknown'):
        raise VisualError('binding_changed','Game binding does not match the supplied pixels, timeline or selection.')
    claim='conditional_on_user_declared_inputs' if manifest['inputs']['provenance']=='user_declared' else 'recorded_input_response'
    if binding.get('claim')!=claim or binding.get('heldout_status')!='not_provided' or binding.get('target_id')!='heart':
        raise VisualError('invalid_binding','Input certainty, adapter identity or held-out scope cannot be upgraded.')
    if len(selection['objects'])!=1 or binding.get('object_map')!={selection['objects'][0]['id']:'character'}:
        raise VisualError('invalid_binding','One explicitly mapped character is required.')
    height=binding.get('body_height_px')
    if type(height) not in (int,float) or not math.isfinite(height) or not 0<height<=manifest['image_size'][1]:
        raise VisualError('invalid_binding','Reference body height is invalid.')
    if binding.get('scenario')!=_scenario(manifest,binding.get('game_stage')) or binding.get('spatial_alignment')!='initial_anchor_body_units' or binding.get('relative_motion')!=[]:
        raise VisualError('invalid_binding','Native schedule or measurement basis differs from the explicit inputs.')


def prepare_baseline(project,output):
    identity=validate_project(project);original=source_hashes(project)
    output=Path(output)
    if output.exists():raise VisualError('output_exists','Prepared Heart source must use a fresh directory.')
    copy_project(Path(project),output)
    runtime.prepare_project(output)
    prepared=source_hashes(output)
    changed={p:{'before':original.get(p),'after':prepared.get(p)} for p in set(original)|set(prepared) if original.get(p)!=prepared.get(p)}
    if any(p!='project.godot' and not p.startswith('kiroclops_heart/') for p in changed):
        raise VisualError('source_changed','Capture preparation altered application behavior.')
    if source_hashes(project)!=original:raise VisualError('source_changed','Original target changed during preparation.')
    receipt={'kind':'HeartCapturePreparation','original_source_sha256':digest(original),'prepared_source_sha256':digest(prepared),
        'changes':changed,'original_project_unchanged':True,'controller_unchanged':original['player.gd']==prepared['player.gd'],
        'protected_resources':identity,'runtime':runtime.protocol_record(),'scope':'Explicit capture fixture and isolated settings overlay, not an agent behavior edit.'}
    write_json(output.parent/(output.name+'-preparation.json'),receipt)
    return output,receipt


def _phases(binding):
    events=binding['scenario']['events'];points=sorted({e['tick'] for e in events})
    if binding['game_stage']=='jump':return [(points[0],239,'jump_response')]
    active=set();result=[]
    for index,tick in enumerate(points):
        for e in events:
            if e['tick']==tick:(active.add if e['pressed'] else active.discard)(e['action'])
        stop=points[index+1] if index+1<len(points) else tick+31
        result.append((tick,stop,'held' if active else 'coast'))
    return result


def _series(record,body,start):
    objects=record['observations']['objects']
    if record.get('measurement_status')!='ok' or len(objects)!=1:
        raise VisualError('tracking_failure','One reliable pixel character track is required.')
    frames=objects[0]['frames']
    baseline=[f for f in frames[:start] if f.get('valid')]
    if len(baseline)<3:raise VisualError('tracking_failure','Initial observed body anchor is missing.')
    origin=[statistics.median(f[axis] for f in baseline) for axis in ('x','y')]
    return [((f['x']-origin[0])/body,(origin[1]-f['y'])/body) if f.get('valid') else None for f in frames]


def _jump_metrics(series,start,body):
    tolerance=max(.5,.025*body)/body
    takeoff=next((i-1 for i in range(start,len(series)-1) if series[i] is not None and series[i+1] is not None and min(series[i][1],series[i+1][1])>tolerance),None)
    if takeoff is None:raise VisualError('unobservable_behavior','A jump takeoff is not visible in the supplied segment.')
    landing=next((i for i in range(takeoff+3,len(series)-2) if all(series[j] is not None and abs(series[j][1])<=tolerance for j in range(i,i+3))),None)
    if landing is None:raise VisualError('unobservable_behavior','A same-level landing is not visible in the supplied segment.')
    valid=[i for i in range(takeoff+1,landing) if series[i] is not None]
    peak=max(series[i][1] for i in valid);plateau=[i for i in valid if peak-series[i][1]<=.25/body]
    apex=plateau[len(plateau)//2]
    return {'takeoff':takeoff,'apex':apex,'landing':landing,'height_body':peak,'rise_s':(apex-takeoff)/60,'fall_s':(landing-apex)/60}


def compare(reference,before,candidate,binding,*,require_improvement=False):
    start=binding['scenario']['events'][0]['tick'];bodies=[binding['body_height_px'],before['body_height_px'],candidate['body_height_px']]
    sequences=[_series(r,h,start) for r,h in zip((reference,before,candidate),bodies)]
    phases=_phases(binding);metric_gates={};metrics={}
    if binding['game_stage']=='jump':
        values=[]
        for role,series,body in zip(('reference','before','candidate'),sequences,bodies):
            try:value=_jump_metrics(series,start,body)
            except VisualError as exc:
                if role=='reference':raise
                # Valid pixels showing no completed jump are a behavior
                # mismatch, not an imaginary tracker or capture failure.
                value={'status':'expected_jump_not_observed','reason':str(exc)}
            values.append(value)
        ref,old,new=values;metrics={'reference':ref,'before':old,'candidate':new}
        if 'landing' in new:
            metric_gates={'jump_observed':True,'height':abs(new['height_body']-ref['height_body'])/ref['height_body']<=.1,
                'rise':abs(new['rise_s']-ref['rise_s'])<=max(2/60,.1*ref['rise_s']),
                'fall':abs(new['fall_s']-ref['fall_s'])<=max(2/60,.1*ref['fall_s'])}
        else:metric_gates={'jump_observed':False}
        phases=[(start,max(v['landing'] for v in values if 'landing' in v)+1,'visible_flight')]
    axis=1 if binding['game_stage']=='jump' else 0;rows=[]
    for first,end,label in phases:
        indexes=[i for i in range(first,end) if all(s[i] is not None for s in sequences)]
        if len(indexes)/(end-first)<.95:raise VisualError('tracking_failure','Every compared movement phase requires 95% common valid frames.')
        old=math.sqrt(statistics.mean((sequences[0][i][axis]-sequences[1][i][axis])**2 for i in indexes))
        new=math.sqrt(statistics.mean((sequences[0][i][axis]-sequences[2][i][axis])**2 for i in indexes))
        gain=1-new/old if old>1e-9 else None
        gates={'curve':new<=.15,'improvement':not require_improvement or gain is not None and gain>=.5}
        if binding['game_stage']=='ground':
            tolerance=max(.15,1/bodies[0]+1/bodies[2])
            if any(s[end-1] is None for s in sequences):raise VisualError('tracking_failure','Phase endpoint is not observed.')
            gates['endpoint']=abs(sequences[0][end-1][0]-sequences[2][end-1][0])<=tolerance
            if label=='held' and end-first>=18:
                ids=list(range(end-8,end));tmean=statistics.mean(ids)
                if any(s[i] is None for s in sequences for i in ids):raise VisualError('tracking_failure','Last-eight-frame speed is unobserved.')
                speed=[60*sum((i-tmean)*s[i][0] for i in ids)/sum((i-tmean)**2 for i in ids) for s in sequences]
                gates['speed']=abs(speed[0])>1e-9 and abs(speed[2]-speed[0])/abs(speed[0])<=.1
            if label=='coast':
                if any(s[first-1] is None for s in sequences):raise VisualError('tracking_failure','Release position is unobserved.')
                coast=[s[end-1][0]-s[first-1][0] for s in sequences]
                gates['brake_distance']=abs(coast[0]-coast[2])<=tolerance
        rows.append({'phase':label,'frames':[first,end],'before_rmse_body':old,'after_rmse_body':new,
            'improvement_fraction':gain,'gates':gates,'pass':all(gates.values())})
    score=math.sqrt(statistics.mean(r['after_rmse_body']**2 for r in rows))
    return {'pass':all(r['pass'] for r in rows) and all(metric_gates.values()),'score':score,'phases':rows,
        'metrics':metrics,'metric_gates':metric_gates,'input_provenance':binding['input_provenance'],
        'claim':binding['claim'],'scope':'Supplied native input-response segment only; no benchmark or unseen-input claim.'}


def _reference(bundle_dir,bundle,binding,output,check,progress,*,analyze):
    from .evaluation import _analysis
    package=Path(bundle_dir)/bundle['record']['package'];selection=read_json(Path(bundle_dir)/bundle['record']['selection'])
    row={'scenario':binding['scenario'],'package':str(package),'body_height_px':binding['body_height_px'],'reference_kind':'uploaded_pixels',
         'functional':{'status':'not_observed_reference_video'}}
    if not analyze:return row
    measured=_analysis(package,selection,output,check,progress,contract={'min_valid_ratio':.95,'require_static_camera':True,'event_options':{'axis':'y' if binding['game_stage']=='jump' else 'x'}})
    return {**row,**{k:measured[k] for k in ('observations','events','measurement_status')},'selection':selection,'analysis':str(Path(output)/'analysis.json')}


def _target(project,output,binding,check,progress,cancel,*,analyze):
    from .game import _annotation,_Cancellation
    from .evaluation import _analysis
    cancel=_Cancellation(cancel,check);output=Path(output);original=source_hashes(project)
    rendered=output/'project';copy_project(Path(project),rendered)
    if source_hashes(rendered)!=original:raise VisualError('source_changed','Disposable Heart capture source differs.')
    rendered_sources=runtime.source_hashes(rendered)
    runtime.import_project(rendered,output/'import',cancel=cancel)
    raw=output/'raw';recipe={**binding['scenario'],'variant':'after'}
    runtime.capture_project(rendered,raw,recipe,cancel=cancel);check()
    package=output/'package';import_legacy_run(raw,package)
    # Godot imports may create .uid sidecars in the disposable renderer copy.
    # The qualified native source policy excludes those generated sidecars;
    # the original sealed candidate still uses its complete session hash map.
    if source_hashes(project)!=original or runtime.source_hashes(rendered)!=rendered_sources:raise VisualError('source_changed','Heart source changed while recording.')
    row={'scenario':binding['scenario'],'package':str(package),'raw_run':str(raw),'source_sha256':digest(original),
         'body_height_px':16.,'functional':{'status':'captured_only','scope':'Runtime completed; native preservation is evaluated separately.'}}
    if not analyze:return row
    annotation=_annotation(binding['game_stage'],'heart');value=read_json(annotation)
    selection=selection_from_legacy(raw,annotation);selection['objects'][0]['id']=next(iter(binding['object_map']))
    selection['objects'][0]['search_radius']=max(8,math.ceil(value['body_height_px']*.75))
    measured=_analysis(package,selection,output/'measurement',check,progress,contract={'min_valid_ratio':.95,'require_static_camera':True,'event_options':{'axis':'y' if binding['game_stage']=='jump' else 'x'}},candidate_sha256=digest(original))
    return {**row,**{k:measured[k] for k in ('observations','events','measurement_status')},'selection':selection,
        'body_height_px':value['body_height_px'],'analysis':str(output/'measurement/analysis.json')}


def run_job(directory,job,state,check,progress,cancel):
    from .media_session import load_bundle,_store_prepared,_load_prepared
    from .evaluation import _export_calibration,_export_candidate,_profile
    from .game import _preservation,_Cancellation
    root=Path(directory);bundle_dir=Path(state['reference']);bundle=load_bundle(bundle_dir)
    if digest(bundle)!=state['experiment']['bundle_sha256']:raise VisualError('bundle_changed','Game reference bundle differs.')
    binding=read_json(bundle_dir/bundle['record']['capture_binding']);output=root/'measurements'/job['job_id'];op=job['operation'];tools=state['mode']=='tools'
    if op=='report':
        from .reporting import build_report
        return build_report(root)
    if op=='prepare':
        reference=_reference(bundle_dir,bundle,binding,output/'reference',check,progress,analyze=tools)
        before=_target(state['baseline'],output/'before-capture',binding,check,progress,cancel,analyze=tools)
        if tools and any(r['measurement_status']!='ok' for r in (reference,before)):raise VisualError('tracking_failure','Reference or baseline pixel measurement rejected.')
        write_json(output/'reference.json',[reference]);write_json(output/'before.json',[before]);_store_prepared(root,job,state,output/'reference.json',output/'before.json')
        result={'status':'ready','reference_kind':'uploaded_pixels','heldout_status':'not_provided','benchmark_qualified':False,
                'prepared_evidence_sha256':file_sha256(root/'prepared-private.json'),
                'reference_media':_export_calibration(root,[reference],'reference'),'before_media':_export_calibration(root,[before],'before'),
                'reference_body_height_px':binding['body_height_px'],'target_body_height_px':16.}
        if tools:result['profile']=_profile([reference],full_output=root/'target/.kiroclops-evidence/reference/pixel-evidence.json')
        return result
    reference,before=_load_prepared(root,state)
    candidate=_target(job['source_path'],output/'candidate-capture',binding,check,progress,cancel,analyze=tools or op=='finalize')
    write_json(output/('final.json' if op=='finalize' else 'candidate.json'),[candidate])
    if op=='evaluate' and not tools:return {'eligible':True,'score':None,'neutral_capture':True,'candidate_media':_export_candidate(root,[candidate],job['candidate_id'])}
    if op=='finalize' and not tools:
        reference=_reference(bundle_dir,bundle,binding,output/'reference-analysis',check,progress,analyze=True)
        before=_target(state['baseline'],output/'before-final',binding,check,progress,cancel,analyze=True)
    if any(r.get('measurement_status')!='ok' for r in (reference,before,candidate)):raise VisualError('tracking_failure','Game pixel measurement rejected; no motion score is issued.')
    preservation=_preservation(job['source_path'],state['baseline'],output/'preservation',binding['game_stage'],check,_Cancellation(cancel,check))
    measured=compare(reference,before,candidate,binding,require_improvement=op=='finalize')
    passed=measured['pass'] and preservation['status']=='pass';write_json(output/'comparison.json',[measured])
    if op=='evaluate':return {'eligible':passed,'score':measured['score'],'comparisons':[measured],'preservation':preservation,
        'candidate_media':_export_candidate(root,[candidate],job['candidate_id']),'heldout_status':'not_provided','independent_review_status':'pending'}
    write_json(output/'reference.json',[reference]);write_json(output/'before.json',[before])
    return {'pass':False,'calibration_pass':passed,'comparisons':[measured],'preservation':preservation,
        'acceptance_status':'partial_unobserved_holdout','heldout_status':'not_provided','benchmark_qualified':False,
        'independent_review_status':'pending','source_review_status':'pending','reason':'Only the supplied native input segment was tested; unseen transfer behavior remains unverified.'}
