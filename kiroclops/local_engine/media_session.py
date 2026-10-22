"""Uploaded pixel reference → a bounded, real local application editing session.

Only fixture-compatible panel/SVG and qualified Heart adapters are supported. The reference is a
portable video package, never a reference implementation. No unseen scenario
or hidden interaction rule is invented; finalization seals a calibration-only
result and explicitly reports missing held-out evidence.
"""
from __future__ import annotations

from copy import deepcopy
import bisect
import math
from pathlib import Path
import re
import statistics

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from kiroclops.visual.package import copy_package, load_package
from .intake import detect_project
from .session import Session, create_session, source_hashes, _ordinary


BUNDLE_VERSION=1
LIMITS={'position_rmse_fraction':.03,'endpoint_fraction':.01,'minimum_valid_ratio':.95,'required_improvement_fraction':.5}
VIEWPORT_ALIGNMENT={'mode':'viewport','units':'natural_image_pixels','size_metric':'absolute_visible_outline'}
ANCHOR_POLICY={'window_s':[-.25,0.],'minimum_samples':3,'minimum_span_s':.05,
               'stationary_band_px':1.5,'minimum_valid_ratio':.95,'max_gap_s':.12}


def _spatial_policy(value,object_ids):
    if not isinstance(value,dict):raise VisualError('needs_spatial_binding','Choose viewport or an explicitly mapped visible anchor')
    mode=value.get('mode');expected={**VIEWPORT_ALIGNMENT,'mode':mode}
    if mode=='visible_anchor':
        if value.get('object_id') not in object_ids:raise VisualError('needs_spatial_binding','Visible anchor must be an explicitly mapped reference object')
        expected['object_id']=value['object_id']
    elif mode!='viewport':raise VisualError('unsupported_binding','Unknown spatial alignment')
    if value!=expected:raise VisualError('invalid_binding','Spatial units, actual outline sizes and translation-only policy are fixed')
    return deepcopy(expected)


def _intake(directory):
    directory=_ordinary(directory);state=read_json(directory/'intake.json');binding=read_json(directory/'intake-binding.json')
    if (state.get('kind')!='LocalVideoIntake' or state.get('schema_version')!=1 or state.get('package')!='package'
        or digest(state.get('request'))!=binding.get('request_sha256') or digest(state.get('project'))!=binding.get('project_info_sha256')
        or digest(state['project']['project'])!=binding.get('project_path_sha256') or state.get('package_sha256')!=binding.get('package_sha256')):
        raise VisualError('intake_binding_changed','The original video, project or request changed')
    project=detect_project(state['project']['project']);manifest=load_package(directory/'package')
    if project['source_sha256']!=binding.get('project_sha256') or manifest['package_sha256']!=binding.get('package_sha256'):
        raise VisualError('source_changed','Reference pixels or original target source changed after intake')
    if project['domain'] not in ('web_panel','svg','game'):
        raise VisualError('unsupported_media_adapter','Uploaded-video sessions support the panel, SVG and qualified Heart adapters')
    if state.get('selection')!='selection.json':raise VisualError('needs_selection','Complete the local object selection first')
    selection=read_json(directory/'selection.json')
    if selection.get('package_sha256')!=manifest['package_sha256'] or not selection.get('objects'):
        raise VisualError('selection_binding_mismatch','Object selection is missing or belongs to another reference')
    for obj in selection['objects']:
        index=obj.get('frame_index',0)
        if type(index) is not int or not 0<=index<len(manifest['frames']) or obj.get('template_frame_sha256')!=manifest['frames'][index]['sha256']:
            raise VisualError('selection_binding_mismatch','A selected template is not bound to the actual reference frame')
        if 'template_package' in obj:raise VisualError('invalid_selection','Reference templates must remain inside the portable package')
    return state,project,manifest,selection


def binding_preset(intake_dir, *, object_map, preset='recorded-timeline', initial_node_count=None, expected_selected_id=None, game_stage=None, body_height=None, spatial_alignment=None):
    """Use only input times that were recorded or explicitly supplied by a user.

    object_map maps the selected reference IDs to visible adapter IDs, e.g.
    ``{'my-panel-handle':'handle'}``. It is user input, not an inferred identity.
    """
    state,project,manifest,selection=_intake(intake_dir)
    if preset!='recorded-timeline':raise VisualError('unsupported_binding','Only the explicit recorded-timeline preset is supported')
    if project['domain']=='game':
        if spatial_alignment is not None:raise VisualError('unsupported_binding','Heart keeps its separate declared body-height coordinate contract')
        from .game_media import make_binding
        return make_binding(project,manifest,selection,object_map,game_stage,body_height)
    if manifest['inputs']['provenance']=='unknown' or not manifest['inputs']['events']:
        return {'status':'needs_input_binding','reason':'The video does not identify input times. Supply a user-declared InputTimeline, then prepare a fresh intake.',
                'inferred_events':False,'model_started':False}
    if not isinstance(object_map,dict):raise VisualError('invalid_binding','Provide reference_id=adapter_id object correspondences')
    refs={o['id'] for o in selection['objects']}
    targets={'handle','bookmark','star'} if project['domain']=='web_panel' else set('abcdef')
    if set(object_map)!=refs or not all(v in targets for v in object_map.values()) or len(set(object_map.values()))!=len(object_map):
        raise VisualError('invalid_binding','Map every selected reference object to one distinct supported visible adapter object')
    clip_start=manifest['frames'][0]['time_s'];clip_end=manifest['frames'][-1]['time_s']
    interval=statistics.median(b['time_s']-a['time_s'] for a,b in zip(manifest['frames'],manifest['frames'][1:]))
    duration=clip_end-clip_start+interval
    events=[]
    for event in manifest['inputs']['events']:
        row={k:deepcopy(event[k]) for k in ('action','selector','destination','fraction') if k in event}
        row['time_s']=event['time_s']-clip_start
        if event['time_s']<clip_start or event['time_s']>=clip_end:
            raise VisualError('invalid_binding','Every declared action must lie inside the actual reference video')
        events.append(row)
    domain='panel' if project['domain']=='web_panel' else 'svg'
    from kiroclops.web_local.specs import TIMING_LIMITS
    scenario={'id':'uploaded-reference-01','recipe':'user-bound-reference','domain':domain,'split':'calibration','repeat':1,
        'viewport':list(manifest['image_size']),'device_scale_factor':1,'duration_s':duration,'events':events,
        'timing_limits':dict(TIMING_LIMITS),'version':'uploaded-media-v1'}
    from .capture_modes import reference_capture_policy,LEGACY_DRIVER
    policy=reference_capture_policy(project['domain'],manifest)
    if policy['frame_driver']!=LEGACY_DRIVER:scenario.update(policy)
    if domain=='svg':
        if type(initial_node_count) is not int or not 3<=initial_node_count<=6:
            raise VisualError('needs_input_binding','Declare the initial SVG node count (3–6); pixels alone do not define hidden application state')
        final_count=initial_node_count+sum(1 if e.get('selector')=='#add-node' else -1 if e.get('selector')=='#remove-node' else 0 for e in events if e['action']=='click')
        scenario.update(initial_node_count=initial_node_count,expected_final_node_count=final_count,expected_selected_id=expected_selected_id)
        from .geometry_acceptance import validate_svg_selection
        validate_svg_selection(selection)
    inverse={target:ref for ref,target in object_map.items()}
    relative=[{'object_id':inverse[child],'relative_to':inverse['handle']} for child in ('bookmark','star') if child in inverse and 'handle' in inverse] if domain=='panel' else []
    value={'schema_version':2,'kind':'MediaCaptureBinding','preset':preset,'reference_package_sha256':manifest['package_sha256'],
        'selection_sha256':digest(selection),'object_map':deepcopy(object_map),'scenario':scenario,
        'relative_motion':relative,
        'input_provenance':manifest['inputs']['provenance'],
        'spatial_alignment':_spatial_policy(VIEWPORT_ALIGNMENT if spatial_alignment is None else spatial_alignment,refs),
        'claim':'conditional_on_user_declared_inputs' if manifest['inputs']['provenance']=='user_declared' else 'recorded_input_response',
        'status':'bound','heldout_status':'not_provided','model_started':False}
    if domain=='svg':
        from .evaluation import _lifecycle_bindings
        value.update(connections=deepcopy(selection['connections']),lifecycle_bindings=_lifecycle_bindings(selection,manifest))
    _validate_binding(value,project,manifest,selection)
    return value


def _validate_binding(binding,project,manifest,selection):
    if project['domain']=='game':
        from .game_media import validate_binding
        return validate_binding(binding,project,manifest,selection)
    from kiroclops.web_local.specs import validate_scenario,TIMING_LIMITS
    if binding.get('kind')!='MediaCaptureBinding' or binding.get('schema_version') not in (1,2) or binding.get('status')!='bound':
        raise VisualError('invalid_binding','Expected an explicit MediaCaptureBinding')
    if (binding.get('reference_package_sha256')!=manifest['package_sha256'] or binding.get('selection_sha256')!=digest(selection)
        or binding.get('input_provenance')!=manifest['inputs']['provenance'] or manifest['inputs']['provenance']=='unknown'):
        raise VisualError('binding_changed','Capture binding does not match the actual reference and selected objects')
    expected_claim='conditional_on_user_declared_inputs' if manifest['inputs']['provenance']=='user_declared' else 'recorded_input_response'
    if binding.get('claim')!=expected_claim or binding.get('heldout_status')!='not_provided':
        raise VisualError('invalid_binding','Input certainty or unseen-validation status cannot be upgraded')
    if binding['schema_version']==1:
        if binding.get('spatial_alignment')!='initial_anchor_displacement':raise VisualError('unsupported_binding','Historical binding alignment changed')
    else:_spatial_policy(binding.get('spatial_alignment'),{o['id'] for o in selection['objects']})
    scenario=binding['scenario'];domain='panel' if project['domain']=='web_panel' else 'svg'
    from .capture_modes import reference_capture_policy,LEGACY_DRIVER
    policy=reference_capture_policy(project['domain'],manifest)
    actual={'frame_driver':scenario.get('frame_driver',LEGACY_DRIVER)}
    actual.update({k:scenario[k] for k in ('analysis_policy','visual_frame_clock') if k in scenario})
    if actual!=policy:
        raise VisualError('binding_changed','Target capture driver and clock policy must match the qualified reference recorder')
    if scenario.get('domain')!=domain or scenario.get('viewport')!=manifest['image_size'] or scenario.get('split')!='calibration':
        raise VisualError('invalid_binding','Domain, natural viewport and calibration scope are fixed')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',scenario.get('id','')):raise VisualError('invalid_binding','Scenario ID must be a simple path-free name')
    times=[f['time_s'] for f in manifest['frames']]
    duration=times[-1]-times[0]+statistics.median(b-a for a,b in zip(times,times[1:]))
    if abs(scenario.get('duration_s',0)-duration)>1e-9 or scenario.get('timing_limits')!=TIMING_LIMITS:
        raise VisualError('invalid_binding','Reference duration and capture timing limits are fixed')
    refs={o['id'] for o in selection['objects']};mapping=binding.get('object_map',{});allowed={'handle','bookmark','star'} if domain=='panel' else set('abcdef')
    if set(mapping)!=refs or len(set(mapping.values()))!=len(mapping) or not set(mapping.values())<=allowed:
        raise VisualError('invalid_binding','Every reference object requires a distinct supported target identity')
    inverse={target:ref for ref,target in mapping.items()}
    expected_relative=[{'object_id':inverse[child],'relative_to':inverse['handle']} for child in ('bookmark','star') if child in inverse and 'handle' in inverse] if domain=='panel' else []
    if binding.get('relative_motion')!=expected_relative:raise VisualError('invalid_binding','Declared adapter parent/child correspondence changed')
    if domain=='svg':
        from .geometry_acceptance import validate_svg_selection
        from .evaluation import _lifecycle_bindings
        connections=validate_svg_selection(selection)
        if binding.get('connections')!=connections or binding.get('lifecycle_bindings')!=_lifecycle_bindings(selection,manifest):
            raise VisualError('invalid_connection_binding','Visible graph, stroke appearance or reference lifecycle binding changed.')
        count=scenario.get('initial_node_count');maximum=count
        if type(count) is not int or not 3<=count<=6:raise VisualError('needs_input_binding','Declare the initial SVG node count.')
        for event in scenario.get('events',[]):
            if event['action']=='click':
                count+=1 if event.get('selector')=='#add-node' else -1 if event.get('selector')=='#remove-node' else 0
                maximum=max(maximum,count)
        if set(mapping.values())!=set('abcdef'[:maximum]):
            raise VisualError('needs_connection_binding','Map all initially visible and added adapter nodes; partial graphs cannot certify preservation.')
    events=scenario.get('events',[]);original=manifest['inputs']['events'];origin=manifest['frames'][0]['time_s']
    if not original or len(events)!=len(original):raise VisualError('invalid_binding','Do not invent or omit reference interactions')
    actual_clicks={a['selector'] for a in project['recommended_capture_actions'] if a['action']=='click'}
    for event,source in zip(events,original):
        if event.get('action')!=source['action'] or abs(event['time_s']-(source['time_s']-origin))>1e-9:
            raise VisualError('invalid_binding','Action kinds and times must come from the supplied timeline')
        if any(event.get(k)!=source.get(k) for k in ('selector','destination','fraction')):
            raise VisualError('invalid_binding','Input correspondence cannot silently change selector or drag geometry')
        selector=event.get('selector')
        if event['action']=='click' and selector not in actual_clicks and not(domain=='svg' and selector in {f'[data-node-id="{n}"]' for n in 'abcdef'}):
            raise VisualError('unsupported_binding','Click selector is not provided by the detected adapter')
        if event['action']=='drag_start' and selector not in ({'#panel-handle'} if domain=='panel' else {f'[data-node-id="{n}"]' for n in 'abcdef'}):
            raise VisualError('unsupported_binding','Unsupported drag target')
    try:validate_scenario(scenario)
    except (ValueError,KeyError,TypeError) as exc:raise VisualError('invalid_binding',str(exc)) from exc


def load_bundle(directory):
    directory=_ordinary(directory);bundle=read_json(directory/'bundle.json');binding=read_json(directory/'binding.json')
    if bundle.get('kind')!='ReferenceMediaBundle' or bundle.get('schema_version')!=BUNDLE_VERSION or digest(bundle)!=binding.get('bundle_sha256'):
        raise VisualError('bundle_changed','Immutable reference media bundle changed')
    expected=bundle.get('file_hashes',{})
    if not expected:raise VisualError('bundle_changed','Reference bundle has no bound pixel evidence')
    actual={p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
    if actual!=set(expected)|{'bundle.json','binding.json'}:raise VisualError('bundle_changed','Unexpected or missing reference bundle files')
    for relative,sha in expected.items():
        if file_sha256(safe_path(directory,relative))!=sha:raise VisualError('bundle_changed','Reference media evidence changed')
    record=bundle['record'];package=safe_path(directory,record['package'],must_exist=False);manifest=load_package(package)
    selection=read_json(safe_path(directory,record['selection']))
    if manifest['package_sha256']!=record['package_sha256'] or digest(selection)!=record['selection_sha256']:
        raise VisualError('bundle_changed','Portable recording identity changed')
    capture_binding=read_json(safe_path(directory,record['capture_binding']))
    _validate_binding(capture_binding,bundle['adapter'],manifest,selection)
    contract=read_json(safe_path(directory,record['contract']))
    limits=LIMITS
    if bundle['domain']=='game':
        from .game_media import LIMITS as game_limits
        limits=game_limits
    if (contract.get('kind')!='BehaviorContract' or contract.get('status')!='calibration_only' or contract.get('heldout_status')!='not_provided'
        or any(contract.get(key)!=value for key,value in limits.items()) or contract.get('input_provenance')!=manifest['inputs']['provenance']
        or contract.get('spatial_alignment')!=capture_binding.get('spatial_alignment')):
        raise VisualError('invalid_contract','Unsupported media acceptance contract')
    return bundle


def create_media_session(intake_dir,session_dir,binding,*,bundle_dir=None,mode='tools'):
    state,project,manifest,selection=_intake(intake_dir)
    if isinstance(binding,(str,Path)):binding=read_json(binding)
    if not isinstance(binding,dict):raise VisualError('invalid_binding','Expected a capture binding object')
    if mode not in ('tools','baseline'):raise VisualError('invalid_session','Expected tools or baseline mode')
    if binding.get('status')=='needs_input_binding':return deepcopy(binding)
    _validate_binding(binding,project,manifest,selection)
    session_dir=_ordinary(session_dir);bundle_dir=_ordinary(bundle_dir or session_dir.with_name(session_dir.name+'-reference-media'))
    if session_dir.exists() or bundle_dir.exists():raise VisualError('output_exists','Keep previous sessions and media bundles unchanged')
    for source in (_ordinary(intake_dir),_ordinary(project['project'])):
        if bundle_dir.is_relative_to(source) or source.is_relative_to(bundle_dir):raise VisualError('unsafe_path','Reference bundle must be separate from intake and target')
    if bundle_dir.is_relative_to(session_dir) or session_dir.is_relative_to(bundle_dir):raise VisualError('unsafe_path','Reference media must stay outside the editable session')
    baseline=project['project'];preparation=None;limits=LIMITS
    if project['domain']=='game':
        from .game_media import prepare_baseline,LIMITS as game_limits
        baseline,preparation=prepare_baseline(baseline,session_dir.with_name(session_dir.name+'-prepared-heart'))
        limits=game_limits
    copy_package(Path(intake_dir)/'package',bundle_dir/'package');write_json(bundle_dir/'selection.json',selection)
    write_json(bundle_dir/'capture-binding.json',binding)
    contract={'schema_version':1,'kind':'BehaviorContract','intent':state['request'],'status':'calibration_only',
        'claim':binding['claim'],'input_provenance':manifest['inputs']['provenance'],
        **limits,
        'spatial_alignment':binding['spatial_alignment'],'heldout_status':'not_provided','independent_review_status':'pending',
        'preservation':'Detected adapter runtime and source-scope checks; no unobserved interaction guarantee.'}
    write_json(bundle_dir/'behavior-contract.json',contract)
    bundle={'schema_version':BUNDLE_VERSION,'kind':'ReferenceMediaBundle','domain':project['domain'],'goal':state['request'],
        'project_entry':project['entry'],'target_baseline_sha256':project['source_sha256'],
        'adapter':{k:deepcopy(project[k]) for k in ('domain','entry','controller_export','recommended_capture_actions') if k in project},
        'input_provenance':manifest['inputs']['provenance'],'benchmark_qualified':False,'heldout_status':'not_provided',
        'record':{'id':binding['scenario']['id'],'package':'package','selection':'selection.json','package_sha256':manifest['package_sha256'],
                  'selection_sha256':digest(selection),'capture_binding':'capture-binding.json','contract':'behavior-contract.json'},
        'file_hashes':{p.relative_to(bundle_dir).as_posix():file_sha256(p) for p in sorted(bundle_dir.rglob('*')) if p.is_file()}}
    write_json(bundle_dir/'bundle.json',bundle);write_json(bundle_dir/'binding.json',{'bundle_sha256':digest(bundle)})
    experiment={'experiment_id':'uploaded-media-v1','reference_mode':'media_bundle','bundle_sha256':digest(bundle),
        'calibration':[deepcopy(binding['scenario'])],'description':'User-supplied pixels and explicitly bound inputs; calibration only.',
        'heldout_status':'not_provided','input_provenance':manifest['inputs']['provenance'],'benchmark_qualified':False}
    if project['domain']=='game':experiment.update(stage=binding['game_stage'],source_preparation=preparation)
    create_session(session_dir,project['domain'],baseline,bundle_dir,state['request'],mode=mode,experiment=experiment)
    from .launcher import configure
    configure(session_dir)
    return {'status':'ready','session':str(session_dir),'target':str(session_dir/'target'),'reference_bundle':str(bundle_dir),
        'bundle_sha256':digest(bundle),'domain':project['domain'],'mode':mode,'model_started':False,
        'readiness':'configured_for_local_mcp_or_cli','heldout_status':'not_provided','benchmark_qualified':False,
        'acceptance_status':'not_evaluated','session_state':Session(session_dir).inspect()}


def _reference_record(bundle_dir,bundle,check,progress,output,*,analyze):
    from .evaluation import _analysis
    record=bundle['record'];binding=read_json(Path(bundle_dir)/record['capture_binding']);package=Path(bundle_dir)/record['package']
    manifest=load_package(package);selection=read_json(Path(bundle_dir)/record['selection'])
    selection=deepcopy(selection);selection['relative_motion']=deepcopy(binding.get('relative_motion',[]))
    if binding['scenario'].get('analysis_policy')=='observed_response_v2':
        selection['lifecycle_bindings']=deepcopy(binding['lifecycle_bindings'])
    row={'scenario':binding['scenario'],'package':str(package),'functional':{'status':'not_observed_reference_video'},
        'timing':{'status':'source_timestamps','input_provenance':manifest['inputs']['provenance']},'reference_kind':'uploaded_pixels'}
    if not analyze:return row
    measured=_analysis(package,selection,output,check,progress,contract={'min_valid_ratio':.95,'require_static_camera':True,
        'event_options':{'connection_tolerance_px':2.,**({'rest_duration_s':.25} if binding['scenario'].get('analysis_policy')=='observed_response_v2' else {})}})
    return {**row,'analysis':str(Path(output)/'analysis.json'),'selection':selection,'measurement_status':measured['measurement_status'],
        'observations':measured['observations'],'events':measured['events']}


def _measurement_signature(path):
    """Bind cached measurements to their exact images and observed timestamps."""
    rows=read_json(path)
    if not isinstance(rows,list) or len(rows)!=1:raise VisualError('measurement_binding_changed','Expected one bound media record.')
    row=rows[0];manifest=load_package(row['package'])
    observations=row.get('observations')
    if observations is not None:
        if observations.get('package_sha256')!=manifest['package_sha256'] or digest(observations.get('inputs'))!=digest(manifest['inputs']):
            raise VisualError('measurement_binding_changed','Cached observations belong to different pixels or inputs.')
        for obj in observations.get('objects',[]):
            frames=obj.get('frames',[])
            if len(frames)!=len(manifest['frames']) or any(f.get('index')!=i or abs(f.get('time_s',float('inf'))-m['time_s'])>1e-9 for i,(f,m) in enumerate(zip(frames,manifest['frames']))):
                raise VisualError('measurement_binding_changed','Cached observation frame identities differ.')
    return {'file_sha256':file_sha256(path),'package_sha256':manifest['package_sha256'],
            'observations_sha256':digest(observations) if observations is not None else None,
            'events_sha256':digest(row.get('events')) if row.get('events') is not None else None}


def _store_prepared(root,job,state,reference_path,before_path):
    root=Path(root)
    value={'schema_version':1,'kind':'PreparedMediaEvidence','prepare_job_id':job['job_id'],
        'bundle_sha256':state['experiment']['bundle_sha256'],'baseline_source_sha256':digest(state['baseline_hashes']),
        'reference':Path(reference_path).relative_to(root).as_posix(),'before':Path(before_path).relative_to(root).as_posix(),
        'records':{'reference':_measurement_signature(reference_path),'before':_measurement_signature(before_path)}}
    write_json(root/'prepared-private.json',value)


def _load_prepared(root,state):
    root=Path(root);saved=read_json(root/'prepared-private.json')
    if (saved.get('kind')!='PreparedMediaEvidence' or saved.get('schema_version')!=1
            or saved.get('bundle_sha256')!=state['experiment']['bundle_sha256']
            or saved.get('baseline_source_sha256')!=digest(state['baseline_hashes'])
            or state.get('prepare_job_id') and saved.get('prepare_job_id')!=state['prepare_job_id']):
        raise VisualError('measurement_binding_changed','Prepared evidence belongs to another source, bundle or preparation job.')
    if state.get('prepare_job_id'):
        job_dir=root/'jobs'/state['prepare_job_id'];receipt=read_json(job_dir/'job.json');result=read_json(job_dir/'result.json')
        if (receipt.get('status')!='succeeded' or receipt.get('operation')!='prepare'
                or receipt.get('result_sha256')!=digest(result)
                or result.get('prepared_evidence_sha256')!=file_sha256(root/'prepared-private.json')):
            raise VisualError('measurement_binding_changed','Prepared evidence is not the completed preparation job result.')
    records=[]
    for role in ('reference','before'):
        path=safe_path(root,saved[role])
        expected=root/'measurements'/saved['prepare_job_id']/(role+'.json')
        if path!=expected.resolve() or _measurement_signature(path)!=saved.get('records',{}).get(role):
            raise VisualError('measurement_binding_changed','Prepared '+role+' evidence changed or was replaced.')
        records.append(read_json(path)[0])
    bundle=load_bundle(state['reference'])
    if saved['records']['reference']['package_sha256']!=bundle['record']['package_sha256']:
        raise VisualError('measurement_binding_changed','Prepared reference is not the immutable supplied video.')
    return records


def _point_anchor_policy(value):
    if (not isinstance(value,dict) or value.get('method')!='foreground_color_mass_v1'
            or set(value)!={'method','foreground_rgb'}
            or not isinstance(value.get('foreground_rgb'),list) or len(value['foreground_rgb'])!=3
            or any(type(c) is not int or not 0<=c<=255 for c in value['foreground_rgb'])):
        raise VisualError('point_semantics_mismatch','A foreground anchor requires its selected method and actual RGB appearance')
    return value


def _preserve_point_semantics(reference_spec,target_spec,manifest):
    """A target auto-selection cannot silently opt a user's ROI into a centroid."""
    obj=deepcopy(target_spec);reference_anchor=reference_spec.get('pixel_anchor')
    if reference_anchor is None:
        obj.pop('pixel_anchor',None)
        obj['point_semantics_binding']={'mode':'selected_roi_anchor','reference_pixel_anchor_opted_in':False}
        return obj
    _point_anchor_policy(reference_anchor)
    target_anchor=_point_anchor_policy(obj.get('pixel_anchor'))
    if obj.get('selection_source')!='rendered_natural_artwork_pixels':
        raise VisualError('point_semantics_mismatch','Target foreground appearance must be independently selected from its rendered pixels')
    index=obj.get('frame_index')
    if type(index) is not int or not 0<=index<len(manifest['frames']):
        raise VisualError('point_semantics_mismatch','Target appearance needs an actual bound template frame')
    obj['template_frame_sha256']=manifest['frames'][index]['sha256']
    obj['point_semantics_binding']={'mode':'centroid_of_observed_foreground_color_mass',
        'reference_pixel_anchor':deepcopy(reference_anchor),'target_pixel_anchor':deepcopy(target_anchor),
        'target_appearance_source':'independently_selected_target_rendered_pixels',
        'target_package_sha256':manifest['package_sha256'],'target_template_frame_sha256':obj['template_frame_sha256']}
    return obj


def _target_record(project,output,binding,reference,check,progress,cancel,variant,*,analyze):
    from .evaluation import _capture_web,_analysis
    from kiroclops.web_local.selection import select_package_from_pixels
    row=_capture_web(project,output,binding['scenario'],check,progress,cancel,variant,analyze=False)
    if not analyze:return row
    detected=select_package_from_pixels(row['package'],binding['scenario']['domain']);by_id={o['id']:o for o in detected['objects']}
    ref_objects={o['id']:o for o in reference['selection']['objects']};target_manifest=load_package(row['package'])
    objects=[]
    for ref_id,target_id in binding['object_map'].items():
        if target_id not in by_id:raise VisualError('tracking_failure',f'Bound target object is missing: {target_id}')
        if ref_id not in ref_objects:raise VisualError('point_semantics_mismatch','The reference point definition is missing')
        obj=_preserve_point_semantics(ref_objects[ref_id],by_id[target_id],target_manifest)
        obj['id']=ref_id;objects.append(obj)
    if binding['scenario']['domain']=='svg' and binding['scenario'].get('analysis_policy')!='observed_response_v2':
        from .evaluation import _map_lifecycle
        expected=_map_lifecycle(binding['lifecycle_bindings'],load_package(row['package']))
        for obj in objects:obj['active_intervals_s']=expected[obj['id']]
    selection={**detected,'objects':objects,'connections':[],'relative_motion':[]}
    inverse={target:ref for ref,target in binding['object_map'].items()}
    for edge in detected.get('connections',[]):
        if edge['from_object'] in inverse and edge['to_object'] in inverse:
            selection['connections'].append({**edge,'from_object':inverse[edge['from_object']],'to_object':inverse[edge['to_object']]})
    if binding['scenario']['domain']=='svg':
        # Reference IDs already name the remapped target objects. Retain the
        # explicitly selected line/polyline appearance; do not infer a graph
        # from whichever edges happen to survive in a candidate recording.
        selection['connections']=deepcopy(binding['connections'])
    for relation in detected.get('relative_motion',[]):
        if relation['object_id'] in inverse and relation['relative_to'] in inverse:
            selection['relative_motion'].append({'object_id':inverse[relation['object_id']],'relative_to':inverse[relation['relative_to']]})
    analysis_dir=Path(output)/'measurement';write_json(analysis_dir/'selection.json',selection)
    measured=_analysis(row['package'],selection,analysis_dir,check,progress,contract={'min_valid_ratio':.95,'require_static_camera':True,
        'event_options':{'connection_tolerance_px':2.,**({'rest_duration_s':.25} if binding['scenario'].get('analysis_policy')=='observed_response_v2' else {})}},candidate_sha256=row['source_sha256'])
    return {**row,'selection':selection,'analysis':str(analysis_dir/'analysis.json'),'measurement_status':measured['measurement_status'],
        'observations':measured['observations'],'events':measured['events']}


def _relative_observations(observations,origin,reference_sizes=None):
    """Historical metrics, only after a shared stationary pre-input guard.

    Never assign a newborn object's first visible moving sample as its origin.
    Existing immutable bindings/results are not rewritten into a new policy.
    """
    result=deepcopy(observations);sizes={}
    for obj in result['objects']:
        anchor_time=observations['inputs']['events'][0]['time_s']
        initial=[f for f in obj['frames'] if f['valid'] and f['time_s']<=anchor_time]
        if not initial:raise VisualError('needs_spatial_binding','Historical displacement has no pre-input origin; explicitly bind viewport or a visible anchor')
        initial=initial[-8:];x=statistics.median(f['x'] for f in initial);y=statistics.median(f['y'] for f in initial)
        w=statistics.median(f['width'] for f in initial);h=statistics.median(f['height'] for f in initial);sizes[obj['id']]=(w,h)
        rw,rh=(reference_sizes or {}).get(obj['id'],(w,h))
        for frame in obj['frames']:
            frame['time_s']-=origin
            if frame.get('valid'):frame.update(x=frame['x']-x,y=frame['y']-y,width=frame['width']-w+rw,height=frame['height']-h+rh)
        if obj.get('active_intervals_s'):obj['active_intervals_s']=[[a-origin,b-origin] for a,b in obj['active_intervals_s']]
    result['inputs']={**result.get('inputs',{}),'events':[{**e,'time_s':e['time_s']-origin} for e in result.get('inputs',{}).get('events',[])]}
    result['comparison_transform']={'source_observations_sha256':digest(observations),'time_translation_s':origin,
        'spatial_alignment':'initial_anchor_displacement','size_metric':'change_from_initial_visible_size','duration_rescaled':False}
    return result,sizes


def _common_stationary_anchor(observations,object_id):
    """Actual shared input-relative support; never extrapolate through a birth."""
    groups=[]
    for obs in observations:
        obj=next((o for o in obs['objects'] if o['id']==object_id),None)
        if obj is None:raise VisualError('needs_spatial_binding','Mapped visible anchor is absent')
        origin=obs['inputs']['events'][0]['time_s']
        frames=[f for f in obj['frames'] if -.25<=f['time_s']-origin<0]
        if len(frames)<3 or frames[-1]['time_s']-frames[0]['time_s']<.05:
            raise VisualError('needs_spatial_binding','Anchor needs three actual pre-input samples spanning at least50ms')
        good=[f for f in frames if f.get('valid') and f.get('expected_active',True)]
        if len(good)/len(frames)<.95 or not good:
            raise VisualError('needs_spatial_binding','Anchor pre-input support is not reliably visible')
        for key in ('x','y','width','height'):
            values=[f.get(key) for f in good]
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
                raise VisualError('needs_spatial_binding','Anchor geometry is not observable')
        if math.hypot(max(f['x'] for f in good)-min(f['x'] for f in good),
                      max(f['y'] for f in good)-min(f['y'] for f in good))>1.5:
            raise VisualError('needs_spatial_binding','Visible anchor is moving before the first input')
        if any(max(f[k] for f in good)-min(f[k] for f in good)>1.5 for k in ('width','height')):
            raise VisualError('needs_spatial_binding','Visible anchor outline changes before input')
        groups.append((obs,obj,origin,frames))
    low=max(fs[0]['time_s']-origin for _,_,origin,fs in groups)
    high=min(fs[-1]['time_s']-origin for _,_,origin,fs in groups)
    if high-low<.05:raise VisualError('needs_spatial_binding','No shared stationary pre-input interval')
    result=[]
    for obs,obj,origin,frames in groups:
        # Inspect actual adjacent samples too; filtering invalid rows could hide
        # an occlusion and manufacture an interpolation bridge.
        times=[f['time_s']-origin for f in frames]
        support=[f for t,f in zip(times,frames) if low<=t<=high]
        if len(support)<3 or any(b-a>.12 for a,b in zip(times,times[1:])):
            raise VisualError('needs_spatial_binding','Common pre-input anchor support is sparse')
        if any(not f.get('valid') or not f.get('expected_active',True) for f in support):
            raise VisualError('needs_spatial_binding','Common pre-input anchor crosses an invalid sample')
        for interval in obj.get('active_intervals_s',[[frames[0]['time_s'],frames[-1]['time_s']]]):
            if interval[0]<=origin+low and origin+high<=interval[1]:break
        else:raise VisualError('needs_spatial_binding','Common anchor crosses a lifecycle boundary')
        i=bisect.bisect_left(times,high)
        if i<len(times) and abs(times[i]-high)<=1e-9:bracket=[frames[i]];sample={k:frames[i][k] for k in ('x','y','width','height')}
        else:
            if i==0 or i==len(times) or times[i]-times[i-1]>.12:
                raise VisualError('needs_spatial_binding','No bounded interpolation at common anchor time')
            bracket=frames[i-1:i+1]
            if any(not f.get('valid') or not f.get('expected_active',True) for f in bracket):
                raise VisualError('needs_spatial_binding','Common anchor bracket is invalid')
            fraction=(high-times[i-1])/(times[i]-times[i-1])
            sample={k:bracket[0][k]+fraction*(bracket[1][k]-bracket[0][k]) for k in ('x','y','width','height')}
        result.append({'source_observations_sha256':digest(obs),'package_sha256':obs.get('package_sha256'),
            'object_id':object_id,'input_origin_s':origin,'common_offset_s':high,'support_interval_s':[low,high],
            'bracket_indices':[f['index'] for f in bracket],'bracket_times_s':[f['time_s'] for f in bracket],
            'bracket_sha256':digest(bracket),'sample':sample,'policy':dict(ANCHOR_POLICY)})
    return result


def _spatial_observations(observations,binding):
    """Choose one declared spatial frame once, before any response comparison."""
    ids={o['id'] for o in observations[0]['objects']};policy=binding.get('spatial_alignment')
    if any({o['id'] for o in obs['objects']}!=ids for obs in observations):
        raise VisualError('tracking_failure','Spatial correspondence is incomplete')
    for oid in ids:
        anchors=[next(o for o in obs['objects'] if o['id']==oid).get('pixel_anchor') for obs in observations]
        if any(a is not None for a in anchors):
            if any(not isinstance(a,dict) for a in anchors):
                raise VisualError('point_semantics_mismatch','Foreground and ROI anchor observations cannot be mixed')
            for anchor in anchors:_point_anchor_policy({k:anchor.get(k) for k in ('method','foreground_rgb')})
        methods=[a.get('method') if isinstance(a,dict) else None for a in anchors]
        if len(set(methods))!=1:
            raise VisualError('point_semantics_mismatch','Reference and target measure different physical point definitions')
        if methods[0] is not None and methods[0]!='foreground_color_mass_v1':
            raise VisualError('point_semantics_mismatch','Unknown observed foreground anchor method')
        if methods[0] is not None:
            for anchor in anchors:
                _point_anchor_policy({k:anchor.get(k) for k in ('method','foreground_rgb')})
                if anchor.get('semantics')!='centroid_of_observed_foreground_color_mass':
                    raise VisualError('point_semantics_mismatch','Observed foreground point semantics are missing')
    if policy=='initial_anchor_displacement':
        anchors={oid:_common_stationary_anchor(observations,oid) for oid in sorted(ids)}
        median_support=[]
        for obs in observations:
            origin=obs['inputs']['events'][0]['time_s'];role=[]
            for obj in obs['objects']:
                # Keep the historical median formula, but guard the samples it
                # actually consumes. Sparse cadence must not reach back into
                # earlier motion outside the qualified pre-input interval.
                frames=[f for f in obj['frames'] if f.get('valid') and f['time_s']<=origin][-8:]
                if any(f['time_s']<origin-.25 or not f.get('expected_active',True) for f in frames):
                    raise VisualError('needs_spatial_binding','Legacy median reaches outside its validated stationary pre-input support')
                if any(type(f.get(k)) not in (int,float) or not math.isfinite(f[k]) for f in frames for k in ('x','y','width','height')):
                    raise VisualError('needs_spatial_binding','Legacy median consumes unobservable geometry')
                if (math.hypot(max(f['x'] for f in frames)-min(f['x'] for f in frames),max(f['y'] for f in frames)-min(f['y'] for f in frames))>1.5
                        or any(max(f[k] for f in frames)-min(f[k] for f in frames)>1.5 for k in ('width','height'))):
                    raise VisualError('needs_spatial_binding','Actual legacy median samples are not stationary')
                role.append({'object_id':obj['id'],'indices':[f['index'] for f in frames],
                    'times_s':[f['time_s'] for f in frames],'samples_sha256':digest(frames)})
            median_support.append(role)
        reference,sizes=_relative_observations(observations[0],0.)
        transformed=[reference]+[_relative_observations(obs,0.,sizes)[0] for obs in observations[1:]]
        return transformed,{'mode':'legacy_initial_anchor_displacement','historical_metrics_preserved':True,
            'stationary_preinput_guard':anchors,'actual_legacy_median_support':median_support,'birth_fallback_allowed':False}
    policy=_spatial_policy(policy,ids)
    if any(obs.get('image_size')!=observations[0].get('image_size') for obs in observations):
        raise VisualError('needs_spatial_binding','Natural image viewports differ; implicit scale fitting is forbidden')
    anchors=_common_stationary_anchor(observations,policy['object_id']) if policy['mode']=='visible_anchor' else []
    offsets=[(anchors[0]['sample']['x']-a['sample']['x'],anchors[0]['sample']['y']-a['sample']['y']) for a in anchors] if anchors else [(0.,0.)]*len(observations)
    transformed=[];receipts=[]
    for obs,(dx,dy) in zip(observations,offsets):
        result=deepcopy(obs)
        for obj in result['objects']:
            for frame in obj['frames']:
                if frame.get('valid'):
                    frame['x']+=dx;frame['y']+=dy
                    if frame.get('left') is not None:frame['left']+=dx
                    if frame.get('top') is not None:frame['top']+=dy
        receipt={'source_observations_sha256':digest(obs),'policy':policy,'translation_px':[dx,dy],
            'per_object_origin_removed':False,'size_values_unchanged':True,'duration_rescaled':False}
        result['comparison_transform']=receipt;transformed.append(result);receipts.append(receipt)
    return transformed,{'policy':policy,'roles':['reference','before','candidate'],'transforms':receipts,'anchor_evidence':anchors}


def _input_time_observations(observations,origin):
    result=deepcopy(observations)
    for obj in result['objects']:
        for frame in obj['frames']:frame['time_s']-=origin
        if obj.get('active_intervals_s'):obj['active_intervals_s']=[[a-origin,b-origin] for a,b in obj['active_intervals_s']]
    result['inputs']={**result['inputs'],'events':[{**e,'time_s':e['time_s']-origin} for e in result['inputs']['events']]}
    result['comparison_time_translation_s']=origin
    return result


def _compare(reference,before,candidate,binding,*,limits=None,require_improvement=False):
    if binding['scenario'].get('analysis_policy')=='observed_response_v2':
        from .observed_response import compare_media
        return compare_media(reference,before,candidate,binding,limits=limits,require_improvement=require_improvement)
    from kiroclops.visual.comparison import compare_observations,relative_object
    from .evaluation import _dynamic_checks
    limits=limits or LIMITS
    if any(limits.get(k)!=v for k,v in LIMITS.items()):raise VisualError('invalid_contract','Unsupported acceptance limits')
    source_events=reference['observations']['inputs']['events'];target_events=candidate['observations']['inputs']['events'];before_events=before['observations']['inputs']['events']
    if not len(source_events)==len(target_events)==len(before_events):raise VisualError('capture_timing_failure','Input receipt counts differ')
    spatial,spatial_receipt=_spatial_observations([reference['observations'],before['observations'],candidate['observations']],binding)
    comparisons=[];old_comparisons=[];dynamics=[];gains=[];covered=set()
    svg=binding['scenario']['domain']=='svg'
    mapped_lifetimes=[]
    if svg:
        from .evaluation import _map_lifecycle
        for item in (candidate,before):
            obs=item['observations']
            mapped_lifetimes.append(_map_lifecycle(binding['lifecycle_bindings'],{'frames':obs['objects'][0]['frames'],'inputs':obs['inputs']}))
    for index,event in enumerate(source_events):
        if event['action'] not in ('click','drag_end'):continue
        for actual in (target_events[index],before_events[index]):
            if (actual['action'],actual.get('selector'))!=(event['action'],event.get('selector')):raise VisualError('capture_timing_failure','Input identities differ')
        ref=_input_time_observations(spatial[0],event['time_s'])
        target=_input_time_observations(spatial[2],target_events[index]['time_s'])
        initial=_input_time_observations(spatial[1],before_events[index]['time_s'])
        ends=[]
        for obs,events in ((reference['observations'],source_events),(candidate['observations'],target_events),(before['observations'],before_events)):
            stop=events[index+1]['time_s'] if index+1<len(events) else obs['objects'][0]['frames'][-1]['time_s']
            # Do not borrow pixels from after the next actual input.
            end=max((f['time_s'] for f in obs['objects'][0]['frames'] if f['time_s']<stop),default=stop)
            ends.append(end-events[index]['time_s'])
        end=min(ends)
        if end<=.05:
            raise VisualError('insufficient_observability','An actual input-response window is too short to measure; later responses cannot substitute for it.')
        pairs=[];active_ids=[];initial_ids=[];initial_start=max(-.25,ref['objects'][0]['frames'][0]['time_s'])
        relative={r['object_id']:r['relative_to'] for r in binding.get('relative_motion',[])}
        lookup={o['id']:o for o in ref['objects']}
        for obj in ref['objects']:
            measured=relative_object(obj,lookup[relative[obj['id']]]) if obj['id'] in relative else obj
            start=0.;stop=end
            if svg and obj.get('active_intervals_s') and not any(min(end,b)>max(0.,a) for a,b in obj['active_intervals_s']):
                if index==0 and all(b<=0 for a,b in obj['active_intervals_s']):
                    start=initial_start;stop=0.;initial_ids.append(obj['id'])
                else:continue # Only the reference lifecycle can omit a response window.
            else:active_ids.append(obj['id'])
            valid=[f for f in measured['frames'] if f.get('valid') and start<=f['time_s']<=stop]
            if not valid:raise VisualError('tracking_failure','Reference response has no visible samples')
            distance=math.hypot(max(f['x'] for f in valid)-min(f['x'] for f in valid),max(f['y'] for f in valid)-min(f['y'] for f in valid))
            pair={'reference_id':obj['id'],'candidate_id':obj['id'],'scale_px':distance if distance>1.5 else 1.,'static':distance<=1.5,
                'rmse_limit':limits['position_rmse_fraction'] if distance>1.5 else 1.5,
                'size_rmse_limit':limits['position_rmse_fraction'] if distance>1.5 else 1.5,
                'endpoint_limit':limits['endpoint_fraction'] if distance>1.5 else 1.5}
            if obj['id'] in relative:pair.update(relative_to_reference=relative[obj['id']],relative_to_candidate=relative[obj['id']])
            if svg:pair['candidate_active_intervals_s']=[[a-target_events[index]['time_s'],b-target_events[index]['time_s']] for a,b in mapped_lifetimes[0][obj['id']]]
            pairs.append(pair)
        covered.update(p['reference_id'] for p in pairs)
        windows=[{'id':f'input-{index}','start_s':0.,'end_s':end,'endpoint_required':index==len(source_events)-1}]
        if svg:
            windows[0]['object_ids']=active_ids
            if initial_ids:windows.insert(0,{'id':'initial-preservation','start_s':initial_start,'end_s':0.,'object_ids':initial_ids,'endpoint_required':False})
        contract={'alignment':{'mode':'absolute'},'min_valid_ratio':limits['minimum_valid_ratio'],'max_interpolation_gap_s':.12,'allow_unlisted_objects':svg,
            'object_pairs':pairs,'windows':windows,
            'lifecycle_tolerance_s':.1,'scope':'Explicit spatial binding and input-local time; observed outlines and source provenance are retained.'}
        old_contract=deepcopy(contract)
        if svg:
            for pair in old_contract['object_pairs']:pair['candidate_active_intervals_s']=[[a-before_events[index]['time_s'],b-before_events[index]['time_s']] for a,b in mapped_lifetimes[1][pair['reference_id']]]
        comparisons.append(compare_observations(ref,target,contract));old_comparisons.append(compare_observations(ref,initial,old_contract))
        dynamics.extend(_dynamic_checks({'observations':ref},{'observations':target},contract))
        old_rows={(r['reference_id'],r['window_id'],r['segment_index']):r for r in old_comparisons[-1]['object_windows']}
        static={p['reference_id']:p['static'] for p in pairs}
        for row in comparisons[-1]['object_windows']:
            key=row['reference_id'],row['window_id'],row['segment_index'];prior=old_rows.get(key,{}).get('position_rmse');current=row.get('position_rmse')
            gain=1-current/prior if prior is not None and prior>1e-9 and current is not None else None
            is_static=static[row['reference_id']]
            gains.append({'object_id':key[0],'window_id':key[1],'segment_index':key[2],'before_rmse':prior,'after_rmse':current,
                'improvement_fraction':gain,'required_fraction':limits['required_improvement_fraction'],'applicable':not is_static,
                'pass':True if is_static else gain is not None and gain>=limits['required_improvement_fraction'],
                'reason':'static_reference_no_gain_required' if is_static else 'baseline_error_zero_no_gain_demonstrable' if prior is not None and prior<=1e-9 else 'measured_window_gain'})
    errors=[row['position_rmse'] for result in comparisons for row in result['object_windows'] if row.get('position_rmse') is not None]
    old=[row['position_rmse'] for result in old_comparisons for row in result['object_windows'] if row.get('position_rmse') is not None]
    if not comparisons or not errors or covered!={o['id'] for o in reference['observations']['objects']}:
        raise VisualError('insufficient_observability','Every reference object needs an observed response or initial-preservation window')
    score=statistics.mean(errors);old_score=statistics.mean(old) if old else None
    improvement_pass=any(g['applicable'] for g in gains) and all(g['pass'] for g in gains)
    svg_geometry=None;reference_geometry=None;failure_class=None
    measurement_status='ok' if all(r['measurement_status']=='ok' for r in (reference,before,candidate)) else 'rejected'
    if binding['scenario']['domain']=='svg':
        from .geometry_acceptance import svg_preservation
        svg_geometry=svg_preservation(reference['observations'],candidate['observations'],binding.get('connections',[]))
        # This is a matching transfer, not an instruction to repair a graph
        # absent from the source video. Verify its selected active edges too.
        reference_geometry=svg_preservation(reference['observations'],reference['observations'],binding.get('connections',[]))
        failure_class=svg_geometry['failure_class']
        if reference_geometry['measurement_status']!='ok' or svg_geometry['measurement_status']!='ok':
            measurement_status='insufficient_observability';failure_class='insufficient_observability'
        elif not reference_geometry['pass']:
            failure_class='reference_invalid'
    if any(row.get('gates',{}).get('coverage') is False for compared in comparisons for row in compared['object_windows']):
        # Valid tracker samples do not imply sufficient shared temporal support.
        # Keep the original numeric verdict; prevent blaming missing pixels on
        # the candidate's motion code or reporting the measurement as sound.
        measurement_status='insufficient_observability'
        if failure_class is None:failure_class='comparison_coverage_failure'
    baseline_coverage=[row for compared in old_comparisons for row in compared['object_windows']
                       if row.get('gates',{}).get('coverage') is False]
    baseline_measured=before['measurement_status']=='ok' and all(
        compared['measurement_status']=='pass' for compared in old_comparisons)
    if require_improvement and not baseline_measured:
        measurement_status='insufficient_observability'
        if failure_class is None:
            failure_class='baseline_comparison_coverage_failure' if baseline_coverage else 'baseline_measurement_failure'
    result={'pass':all(r['success'] for r in comparisons) and all(r['pass'] for r in dynamics if r['observable']) and
                   (not require_improvement or improvement_pass) and candidate['functional']['status']=='pass' and
                   (svg_geometry is None or svg_geometry['pass']) and
                   (reference_geometry is None or reference_geometry['pass']) and
                   all(r['measurement_status']=='ok' for r in (reference,before,candidate)) and
                   (not require_improvement or baseline_measured),
        'score':score,'before_score':old_score,'improvement_fraction':1-score/old_score if old_score and old_score>1e-9 else None,
        'windows':comparisons,'dynamics':dynamics,'improvement_gates':gains,'improvement_required':require_improvement,'improvement_pass':improvement_pass,
        'input_provenance':binding['input_provenance'],'claim':binding['claim'],'svg_geometry_preservation':svg_geometry,
        'reference_svg_geometry_preservation':reference_geometry,'spatial_alignment_evidence':spatial_receipt,
        'failure_class':failure_class,'measurement_status':measurement_status,
        'baseline_measurement_pass':baseline_measured,'baseline_coverage_failures':baseline_coverage,
        'reference_failure_class':reference_geometry['failure_class'] if reference_geometry else None,
        'interpretation':'Calibration on the supplied clip only; no unseen interaction or held-out success is claimed.'}
    from .comparison_diagnostics import explain_comparison
    result['diagnostic']=explain_comparison(result,source_events,target_events)
    return result


def run_media_job(directory,job,state,check,progress,cancel):
    if state['domain']=='game':
        from .game_media import run_job
        return run_job(directory,job,state,check,progress,cancel)
    from .evaluation import _export_calibration,_export_candidate,_profile
    root=Path(directory);bundle_dir=Path(state['reference']);bundle=load_bundle(bundle_dir)
    if digest(bundle)!=state['experiment']['bundle_sha256']:raise VisualError('bundle_changed','Session belongs to a different reference bundle')
    binding=read_json(bundle_dir/bundle['record']['capture_binding']);output=root/'measurements'/job['job_id'];operation=job['operation'];tools=state['mode']=='tools'
    if operation not in ('prepare','evaluate','finalize','report'):raise VisualError('unsupported_operation',operation)
    if operation=='report':
        from .reporting import build_report
        return build_report(root)
    if operation=='prepare':
        reference=_reference_record(bundle_dir,bundle,check,progress,output/'reference-analysis',analyze=tools)
        before=_target_record(state['baseline'],output/'before-capture',binding,reference,check,progress,cancel,'before',analyze=tools)
        if tools and any(r['measurement_status']!='ok' for r in (reference,before)):raise VisualError('tracking_failure','Reference or target measurements require better selection')
        if before['functional']['status']!='pass':raise VisualError('preservation_violation','Baseline adapter checks failed')
        write_json(output/'reference.json',[reference]);write_json(output/'before.json',[before]);_store_prepared(root,job,state,output/'reference.json',output/'before.json')
        result={'status':'ready','reference_kind':'uploaded_pixels','benchmark_qualified':False,'heldout_status':'not_provided',
            'prepared_evidence_sha256':file_sha256(root/'prepared-private.json'),
            'input_provenance':binding['input_provenance'],'reference_media':_export_calibration(root,[reference],'reference'),
            'before_media':_export_calibration(root,[before],'before'),'finalization_limit':'calibration_only; unseen behavior will remain unverified'}
        if tools:result['profile']=_profile([reference],full_output=root/'target/.kiroclops-evidence/calibration/full-measurements.json')
        return result
    reference,before=_load_prepared(root,state)
    candidate=_target_record(job['source_path'],output/'candidate-capture',binding,reference,check,progress,cancel,'candidate',analyze=tools or operation=='finalize')
    write_json(output/('final.json' if operation=='finalize' else 'candidate.json'),[candidate])
    if operation=='evaluate' and not tools:
        return {'eligible':candidate['functional']['status']=='pass','score':None,'neutral_capture':True,
            'candidate_media':_export_candidate(root,[candidate],job['candidate_id']),'heldout_status':'not_provided'}
    if operation=='finalize' and not tools:
        reference=_reference_record(bundle_dir,bundle,check,progress,output/'reference-analysis',analyze=True)
        # Baseline arm incurs pixel-analysis work only after irreversible sealing.
        before=_target_record(state['baseline'],output/'before-final-capture',binding,reference,check,progress,cancel,'before-final',analyze=True)
    rejected=[role for role,record in (('reference',reference),('before',before),('candidate',candidate)) if record.get('measurement_status')!='ok']
    if rejected:
        # A small RMSE over the few surviving frames is not motion feedback.
        # The immutable capture/analysis above remains available for diagnosis.
        raise VisualError('tracking_failure','Pixel measurement rejected for '+', '.join(rejected)+'; incomplete tracks cannot be scored as movement performance.')
    limits=read_json(bundle_dir/bundle['record']['contract'])
    comparison=_compare(reference,before,candidate,binding,limits=limits,require_improvement=operation=='finalize');write_json(output/'comparison.json',[comparison])
    if operation=='evaluate':
        return {'eligible':comparison['pass'],'score':comparison['score'],'comparisons':[comparison],
            'candidate_media':_export_candidate(root,[candidate],job['candidate_id']),'independent_review_status':'pending','heldout_status':'not_provided'}
    if operation=='finalize':
        write_json(output/'reference.json',[reference]);write_json(output/'before.json',[before])
        return {'pass':False,'calibration_pass':comparison['pass'],'acceptance_status':'partial_unobserved_holdout',
            'heldout_status':'not_provided','comparisons':[comparison],'independent_review_status':'pending','source_review_status':'pending',
            'benchmark_qualified':False,'reason':'Only the user-supplied response was measured. No unseen test or interaction rule was supplied.'}
    raise VisualError('unsupported_operation',operation)
