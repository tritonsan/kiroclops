"""Read actual recorded measurement groups, including nested preservation runs."""
from pathlib import Path
import math

from kiroclops.visual.schema import read_json,digest,VisualError,safe_path,file_sha256
from kiroclops.visual.package import load_package

ROLES=('reference','before','candidate','final')


def observations(record):
    if isinstance(record.get('observations'),dict):return record['observations']
    if record.get('analysis'):
        path=Path(record['analysis'])
        if path.is_file():
            value=read_json(path)
            if isinstance(value.get('observations'),dict):return value['observations']
        sibling=path.parent/'observations.json'
        if sibling.is_file():return read_json(sibling)
    return None


def event_set(record):
    if isinstance(record.get('events'),dict):return record['events']
    if record.get('analysis'):
        path=Path(record['analysis'])
        if path.is_file():
            value=read_json(path)
            if isinstance(value.get('events'),dict):return value['events']
        sibling=path.parent/'events.json'
        if sibling.is_file():return read_json(sibling)
    return None


def measurement_groups(directory,*,job_ids=None):
    """Yield record indexes, not worker internals or arbitrary individual JSONs."""
    root=Path(directory)/'measurements'
    roles_to_read=ROLES
    session_path=Path(directory)/'session.json'
    if session_path.is_file() and read_json(session_path).get('experiment',{}).get('reference_mode')=='media_suite':
        roles_to_read=('reference','original','before','candidate','final')
    for job in sorted(root.glob('*')):
        if not job.is_dir() or job_ids is not None and job.name not in job_ids:continue
        contexts=sorted({p.parent for role in roles_to_read for p in job.rglob(role+'.json')})
        for context in contexts:
            roles={}
            for role in roles_to_read:
                path=context/(role+'.json')
                if not path.is_file():continue
                value=read_json(path)
                if isinstance(value,list) and value and all(isinstance(r,dict) and isinstance(r.get('package'),str) and isinstance(r.get('scenario'),dict) for r in value):
                    roles[role]=value
            if roles:yield {'job_id':job.name,'id':context.relative_to(root).as_posix(),'directory':context,'roles':roles}
        boundary=job/'visual-boundary.json'
        if roles_to_read!=ROLES and boundary.is_file():
            receipt=read_json(boundary)
            from .suite_boundary import bindings as boundary_bindings
            boundary_bindings(boundary,candidate_sha256=receipt['source_sha256'],candidate_id=receipt['candidate_id'])
            rows=read_json(receipt['records_path'])
            yield {'job_id':job.name,'id':job.name+'/visual-boundary','directory':job,
                   'roles':{'final':rows}}


def _available_bindings(directory,state):
    selected=state.get('selected_candidate_id')
    jobs={state.get('prepare_job_id'),state.get('final_job_id')}
    jobs.update(c.get('job_id') for c in state.get('candidates',[]) if c.get('candidate_id')==selected)
    required=set()
    for group in measurement_groups(directory,job_ids=jobs):
        for records in group['roles'].values():
            for record in records:
                obs=observations(record)
                if obs:required.add((obs['package_sha256'],digest(obs)))
    return required


def _need(value,message):
    if not value:raise VisualError('incomplete_review',message)


def _scenario(value,kind):
    """Explicit legacy Godot projection, not an ID-only compatibility bypass."""
    _need(isinstance(value,dict),'Measurement scenario is missing')
    if kind=='media':return {k:v for k,v in value.items() if k!='variant'}
    if kind not in ('jump','ground'):return value
    result={k:value.get(k) for k in ('split','repeat','spawn_offset')}
    result['id']=value.get('id')
    if kind=='jump':
        hold=value.get('hold_ticks');start=value.get('start_tick')
        _need(type(hold) is int and type(start) is int,'Jump input timing is missing')
        result.update(id=result['id'] or f'h{hold:02}',hold_ticks=hold,start_tick=start)
        expected=[{'tick':start,'action':'jump','pressed':True},{'tick':start+hold,'action':'jump','pressed':False}]
        _need(value.get('events',expected)==expected,'Jump action schedule differs from declared hold')
        result['events']=expected
    else:
        _need(isinstance(value.get('events'),list),'Ground input schedule is missing')
        result['events']=value['events']
    for key,expected in (('fps',60),('frame_count',240),('warmup_ticks',60),('mechanic',kind)):
        _need(key not in value or value[key]==expected,'Godot measurement configuration differs: '+key)
    return result


def _completed_job(directory,state,job_id,operation,candidate=None):
    from .session import JOB_BINDING_FIELDS
    _need(isinstance(job_id,str) and Path(job_id).name==job_id and job_id not in ('.','..'),'Missing job identity')
    folder=directory/'jobs'/job_id;job=read_json(folder/'job.json');result=read_json(folder/'result.json')
    _need(job.get('job_id')==job_id and job.get('operation')==operation and job.get('status')=='succeeded',
          'Required measurement job did not complete: '+operation)
    _need(digest({k:job.get(k) for k in JOB_BINDING_FIELDS})==read_json(folder/'binding.json').get('sha256'),
          'Measurement job source/configuration binding changed')
    _need(job.get('result_sha256')==digest(result),'Required job result changed')
    expected=candidate['source_sha256'] if candidate else digest(state['baseline_hashes'])
    _need(job.get('source_sha256')==expected and digest(job.get('source_hashes'))==expected,
          'Measurement job belongs to a different source')
    _need(job.get('candidate_id')==(candidate['candidate_id'] if candidate else None),'Measurement candidate identity differs')
    expected_source=directory/'candidates'/candidate['candidate_id']/'source' if candidate else Path(state['baseline'])
    _need(Path(job.get('source_path','')).resolve()==expected_source.resolve(),'Measurement source path differs')
    return result


def _producer_expectation(directory,state,candidate,role,context,kind,scenario,cache):
    """Resolve trusted role sources once; never infer them from the record under review."""
    from .session import source_hashes
    media=state['experiment'].get('reference_mode')=='media_bundle'
    if role=='reference' and media:return {'kind':'uploaded'}
    if role=='reference' and state['domain']=='game':
        from kiroclops.common import ROOT
        from kiroclops.portable_v2.specs import run_name
        name=run_name(scenario)
        raw=ROOT/(f'artifacts/runs/tiny-reference-{name}' if kind=='jump' else f'artifacts/phase2b/reference/{name}')
        return {'kind':'frozen_reference','raw_run':str(raw.resolve())}
    key=(role in ('candidate','final'),role=='reference',context=='jump' and role=='before')
    if key not in cache:
        selected=role in ('candidate','final')
        if selected:
            job=read_json(directory/'jobs'/candidate['job_id']/'job.json')
            project=directory/'candidates'/candidate['candidate_id']/'source';expected=job['source_hashes']
        elif role=='reference':project=Path(state['reference']);expected=state['reference_hashes']
        elif context=='jump' and state['domain']=='game':
            # Ground inherits a changed jump baseline; its jump denominator is
            # the ORIGINAL game, bound by completed preparation, not that baseline.
            project=Path(state['experiment']['original_baseline'])
            receipt=read_json(directory/'prepared-evidence.json')
            prepared_path=directory/'prepared-private.json'
            result=read_json(directory/'jobs'/state['prepare_job_id']/'result.json')
            _need(result.get('prepared_evidence_sha256')==file_sha256(directory/'prepared-evidence.json')
                  and receipt.get('prepared_private_sha256')==file_sha256(prepared_path),
                  'Original jump denominator is not bound to completed preparation')
            original=read_json(prepared_path).get('source_bindings',{}).get(str(project.resolve()))
            from kiroclops.portable_v2.runtime import source_hashes as native_hashes
            _need(original and native_hashes(project)==original,'Original jump denominator source changed')
            cache[key]={'kind':'native','sources':original}
            return cache[key]
        else:project=Path(state['baseline']);expected=state['baseline_hashes']
        _need(source_hashes(project)==expected,'Expected recording producer source changed')
        if state['domain']=='game':
            from kiroclops.portable_v2.runtime import source_hashes as native_hashes
            value={'kind':'native','sources':native_hashes(project),'session_sources':expected}
        else:
            from kiroclops.web_local.capture import project_hashes
            value={'kind':'web','sources':project_hashes(project)}
        if selected:value['candidate_id']=candidate['candidate_id']
        cache[key]=value
    expected=dict(cache[key])
    if kind=='boundary':
        from .visual_boundary_v4 import ADAPTER
        expected.update(kind='boundary',rendered_sources={**expected['sources'],
            **{f'kiroclops_visual_boundary_v4/{name}':file_sha256(ADAPTER/name) for name in ('Boundary.tscn','boundary.gd')}})
    if role=='reference' and state['experiment'].get('web_reference_bundle'):
        pack_path=Path(state['experiment']['web_reference_bundle'])
        _need(file_sha256(pack_path)==state['experiment'].get('web_reference_bundle_sha256'),'Shared reference pack changed')
        pack=read_json(pack_path);domain='svg' if state['domain']=='svg' else 'panel'
        matches=[r for r in pack['records'] if r['domain']==domain and r['split']==scenario['split'] and r['scenario']==scenario]
        _need(len(matches)==1,'Required shared reference recipe is missing or duplicated')
        expected['package_sha256']=matches[0]['package_sha256']
    return expected


def _record_origin(directory,state,job,context,role,record):
    """A valid old capture is not a newly completed selected-job capture."""
    media=state['experiment'].get('reference_mode')=='media_bundle'
    if media and role=='reference':return # The supplied immutable bundle is reused explicitly.
    root=directory/'measurements'/job/context
    if media:
        roots=[root]
        if role=='before':roots.append(directory/'measurements'/state['prepare_job_id'])
    else:roots=[root/('candidate' if role in ('candidate','final') else role)]
    if context=='boundary':roots=[root/'packages']
    package=Path(record['package']).resolve()
    _need(any(package.is_relative_to(p.resolve()) for p in roots),'Recording is outside its required job/context/role')
    if record.get('raw_run') and role!='reference':
        # Media raw folders and boundary raw folders sit beside packages.
        raw=Path(record['raw_run']).resolve()
        raw_roots=roots if context!='boundary' else [root/'raw']
        _need(any(raw.is_relative_to(p.resolve()) for p in raw_roots),'Native recording belongs to another job/context')


def _native_frame_bridge(manifest,raw_manifest,*,legacy_reference):
    """Preserve native clock values and prove input edges from recorded steps.

    Only the caller's trusted frozen-reference role may use the existing legacy
    importer's 1e-5 nominal clock policy. Package values must still equal raw
    values exactly; this is not a tolerance for package/input mutations.
    """
    raw_frames=raw_manifest.get('frames',[]);frames=manifest.get('frames',[])
    _need(raw_manifest.get('status')=='ok' and raw_manifest.get('fps')==60
          and isinstance(raw_frames,list) and isinstance(frames,list)
          and len(raw_frames)==len(frames)==240,'Native frame count or rate differs')
    _need(manifest.get('metadata',{}).get('adapter')=='legacy-godot-v1',
          'Native package does not use the recorded frame importer')
    if legacy_reference:
        _need(raw_manifest.get('game')=='tiny','Legacy precision applies only to the trusted frozen Tiny reference')
    edges=[];package_edges=[];previous=set()
    for index,(raw,frame) in enumerate(zip(raw_frames,frames)):
        time=raw.get('time_s');actions=raw.get('actions')
        _need(type(raw.get('frame_index')) is int and raw['frame_index']==index
              and type(raw.get('physics_tick')) is int and raw['physics_tick']==60+index
              and type(time) in (int,float) and math.isfinite(time),
              'Native frame identity or clock is malformed')
        error=abs(time-index/60)
        _need(error<=1e-5 if legacy_reference else error<1e-8,'Native nominal clock policy differs')
        _need(type(frame.get('index')) is int and frame['index']==index
              and type(frame.get('time_s')) in (int,float) and frame['time_s']==time,
              'Package frame time or identity differs from its actual native frame')
        _need(isinstance(actions,list) and all(isinstance(action,str) for action in actions)
              and len(actions)==len(set(actions)),'Native held action receipt is malformed')
        current=set(actions)
        for action in sorted(previous^current):
            pressed=action in current
            edges.append({'tick':index,'action':action,'pressed':pressed})
            package_edges.append({'time_s':time,'action':action,'pressed':pressed})
        previous=current
    inputs=manifest.get('inputs',{});actual=inputs.get('events',[])
    _need(inputs.get('provenance')=='recorder_verified' and isinstance(actual,list)
          and all(isinstance(e,dict) and type(e.get('time_s')) in (int,float)
                  and math.isfinite(e['time_s']) and isinstance(e.get('action'),str)
                  and type(e.get('pressed')) is bool for e in actual),
          'Native input provenance differs')
    # There is no observable sub-frame ordering between simultaneous actions.
    actual=sorted(actual,key=lambda event:(event.get('time_s',float('inf')),event.get('action','')))
    package_edges=sorted(package_edges,key=lambda event:(event['time_s'],event['action']))
    _need(len(actual)==len(package_edges) and all(
        type(a.get('time_s')) in (int,float) and a['time_s']==e['time_s']
        and a.get('action')==e['action'] and a.get('pressed') is e['pressed']
        for a,e in zip(actual,package_edges)),
        'Package inputs differ from the actual native frame/action edges')
    return {'frame_edges':edges,'legacy_reference':legacy_reference}


def _record_source(record,manifest,expected):
    """Validate existing producer receipts; no capture, tracking or native execution."""
    kind=expected['kind'];metadata=manifest.get('metadata',{})
    if kind=='uploaded':return # Exact supplied bundle package is checked by the caller.
    if kind=='web':
        _need(metadata.get('source_hashes')==expected['sources'] and
              record.get('source_sha256')==digest(expected['sources']),
              'Recording belongs to a different browser source')
        if 'package_sha256' in expected:
            _need(manifest['package_sha256']==expected['package_sha256'],'Reference pixels differ from the shared frozen recording')
        return
    raw=Path(record.get('raw_run') or expected.get('raw_run','')).resolve()
    if kind=='frozen_reference':
        _need(raw==Path(expected['raw_run']),'Reference run is not the required frozen Tiny recording')
    raw_manifest=read_json(raw/'manifest.json');provenance=raw_manifest.get('provenance',{})
    _need(metadata.get('source_manifest_sha256')==file_sha256(raw/'manifest.json'),
          'Pixel package belongs to another native capture manifest')
    _need(metadata.get('scenario')==raw_manifest.get('scenario'),'Native manifest and package recipe differ')
    clock=_native_frame_bridge(manifest,raw_manifest,legacy_reference=kind=='frozen_reference')
    # The common importer preserves native PNG bytes. Check this bridge as well
    # as the separately verified package, including historical Tiny recordings.
    hashes={f'{i:06d}.png':file_sha256(raw/'frames'/f'{i:06d}.png') for i in range(len(manifest['frames']))}
    _need([f['sha256'] for f in manifest['frames']]==list(hashes.values()),'Package pixels differ from the native producer')
    if kind=='frozen_reference':return clock
    if kind=='boundary':
        sources=expected['rendered_sources'];receipt=read_json(raw/'runtime-receipt.json')
        _need(record.get('source_hashes')==expected['sources'] and record.get('candidate_id')==expected['candidate_id']
              and provenance.get('source_hashes')==sources and receipt.get('source_hashes')==sources,
              'Boundary recording is not the selected controller plus the fixed fixture')
        _need(receipt.get('status')=='pass' and receipt.get('manifest_sha256')==file_sha256(raw/'manifest.json') and receipt.get('raw_frame_hashes')==hashes
              and receipt.get('process_sha256')==file_sha256(raw/'private-process/process.json')
              and provenance.get('process_sha256')==receipt['process_sha256']
              and provenance.get('config_sha256')==file_sha256(raw/'capture_config.json'),
              'Boundary producer receipt or pixels changed')
    else:
        sources=expected['sources']
        _need(provenance.get('full_source_hashes_before')==provenance.get('full_source_hashes_after')==sources
              and provenance.get('source_digest_before')==provenance.get('source_digest_after')==digest(sources),
              'Native recording belongs to a different controller source')
        receipt_path=Path(provenance.get('runtime_receipt',''));receipt=read_json(receipt_path)
        _need(provenance.get('runtime_receipt_sha256')==file_sha256(receipt_path)
              and receipt_path.resolve().parent.parent==raw and Path(receipt.get('receipt_path','')).resolve()==receipt_path.resolve()
              and receipt.get('status')=='completed' and receipt.get('source_hashes_before')==receipt.get('source_hashes_after')==sources
              and raw_manifest.get('raw_frame_hashes')==hashes
              and provenance.get('recording_sha256')==file_sha256(raw/'recording.json')
              and provenance.get('capture_config_sha256')==file_sha256(raw/'capture_config.json'),
              'Native producer receipt or pixels changed')
        if record.get('source_sha256') is not None:
            _need(record['source_sha256']==digest(expected.get('session_sources',sources)), 'Native record source identity differs')
    return clock


def _record_observation(record,scenario,kind,*,measured,reference_package=None,expected_source=None):
    """Read required evidence strictly; optional display readers remain permissive."""
    _need(isinstance(record,dict) and isinstance(record.get('package'),str),'Required recording is malformed')
    _need(_scenario(record.get('scenario'),kind)==_scenario(scenario,kind),'Recording scenario/configuration differs')
    manifest=load_package(record['package'])
    clock=_record_source(record,manifest,expected_source) if expected_source is not None else None
    if reference_package is not None:
        _need(manifest['package_sha256']==reference_package,'Media reference is not the bound supplied package')
    else:
        _need(_scenario(manifest.get('metadata',{}).get('scenario'),kind)==_scenario(scenario,kind),
              'Recorded package scenario differs from its index')
    if kind in ('jump','ground','boundary') and clock is not None:
        expected=_scenario(scenario,kind)['events']
        _need(sorted(clock['frame_edges'],key=lambda e:(e['tick'],e['action']))==
              sorted(expected,key=lambda e:(e['tick'],e['action'])),
              'Native recorded frame/action edges differ from the required recipe')
    elif kind in ('jump','ground','boundary'):
        frames=manifest['frames']
        _need(len(frames)==240 and all(f['index']==i and abs(f['time_s']-i/60)<1e-8 for i,f in enumerate(frames)),
              'Native frame count or timing differs')
        expected=_scenario(scenario,kind)['events']
        events=[{'time_s':e['tick']/60,'action':e['action'],'pressed':e['pressed']} for e in expected]
        actual=manifest['inputs']['events']
        # Legacy frame receipts expose held actions as a set. Simultaneous
        # edges have frame-level time, not a provable sub-frame ordering.
        events=sorted(events,key=lambda x:(x['time_s'],x['action']))
        actual=sorted(actual,key=lambda x:(x['time_s'],x['action']))
        _need(len(events)==len(actual) and all(a.get('action')==e['action'] and a.get('pressed') is e['pressed']
              and abs(a.get('time_s',float('inf'))-e['time_s'])<1e-8 for a,e in zip(actual,events)),
              'Native recorded inputs differ from the required recipe')
    if not measured:
        _need(record.get('observations') is None and record.get('analysis') is None,'Baseline calibration must remain raw-only')
        return None
    _need(isinstance(record.get('analysis'),str),'Required analysis result is missing')
    analysis_path=Path(record['analysis']);analysis=read_json(analysis_path)
    has_worker_source=(expected_source is not None and
        (expected_source['kind'] in ('web','uploaded') or record.get('source_sha256') is not None or analysis.get('kind')=='AnalysisResult'))
    if has_worker_source:
        wanted=None if expected_source['kind']=='uploaded' else digest(expected_source.get('session_sources',expected_source.get('sources')))
        _need('candidate_sha256' in analysis and analysis['candidate_sha256']==wanted,'Analysis belongs to a different producer source')
    values=[]
    declared=record.get('observations')
    if declared is not None:values.append(read_json(declared) if isinstance(declared,str) else declared)
    if isinstance(analysis.get('observations'),dict):values.append(analysis['observations'])
    elif (analysis_path.parent/'observations.json').is_file():values.append(read_json(analysis_path.parent/'observations.json'))
    _need(bool(values) and all(isinstance(v,dict) for v in values),'Required observations are missing')
    obs=values[0]
    _need(all(v==obs for v in values),'Inline and saved observations differ')
    _need(obs.get('package_sha256')==manifest['package_sha256'] and obs.get('image_size')==manifest['image_size']
          and obs.get('inputs')==manifest['inputs'],'Observation pixels, dimensions or inputs differ')
    objects=obs.get('objects',[])
    _need(bool(objects) and all(isinstance(o,dict) and isinstance(o.get('id'),str) for o in objects)
          and len({o['id'] for o in objects})==len(objects),'Required measured objects are missing or duplicated')
    for obj in objects:
        frames=obj.get('frames',[])
        _need(len(frames)==len(manifest['frames']) and all(f.get('index')==i
              and abs(f.get('time_s',float('inf'))-m['time_s'])<1e-9 for i,(f,m) in enumerate(zip(frames,manifest['frames']))),
              'Observation frame identities differ')
    if analysis.get('package_sha256') is not None:_need(analysis['package_sha256']==manifest['package_sha256'],'Analysis package differs')
    provenance=analysis.get('provenance',{})
    if provenance.get('observation_sha256') is not None:_need(provenance['observation_sha256']==digest(obs),'Analysis observation digest differs')
    if analysis.get('scenario') is not None:_need(_scenario(analysis['scenario'],kind)==_scenario(scenario,kind),'Analysis scenario differs')
    selection=record.get('selection')
    if isinstance(selection,str):selection=read_json(selection)
    if selection is None:selection=read_json(analysis_path.parent/'selection.json')
    _need(isinstance(selection,dict) and isinstance(selection.get('objects'),list)
          and {o.get('id') for o in selection['objects']}=={o['id'] for o in objects},'Required selection configuration differs')
    _need(obs.get('selection')==selection,'Observation selection/configuration binding differs')
    return (manifest['package_sha256'],digest(obs))


def required_observation_bindings(directory,state,*,require_complete=True):
    """Validate the complete acceptance matrix, independently of found files.

    Partial report display may explicitly request existing bindings; that path
    cannot establish completed acceptance. No missing index shrinks this matrix.
    """
    if state.get('experiment',{}).get('reference_mode')=='media_suite':
        from .suite_evidence import required_bindings
        return required_bindings(directory,state,require_complete=require_complete)
    if not require_complete:return _available_bindings(directory,state)
    directory=Path(directory)
    try:
        mode=state.get('mode');domain=state.get('domain');experiment=state.get('experiment')
        _need(mode in ('tools','baseline') and isinstance(experiment,dict),'Session mode/experiment configuration is missing')
        _need(domain in ('game','web_panel','web-panel','web','panel','svg'),'Unsupported review domain')
        selected=state.get('selected_candidate_id')
        candidates=[c for c in state.get('candidates',[]) if c.get('candidate_id')==selected]
        _need(selected and len(candidates)==1,'One selected immutable candidate is required')
        candidate=candidates[0]
        _need(candidate.get('status')=='succeeded','Selected candidate evaluation did not complete')
        final=state.get('final') or {}
        _need(state.get('sealed_at') is not None and final.get('status')=='succeeded'
              and final.get('job_id')==state.get('final_job_id'),'A completed sealed final measurement is required')
        prepare_id=state.get('prepare_job_id');chosen_id=candidate.get('job_id');final_id=state['final_job_id']
        _need(len({prepare_id,chosen_id,final_id})==3,'Preparation, evaluation and final jobs must be distinct')
        results={prepare_id:_completed_job(directory,state,prepare_id,'prepare'),
                 chosen_id:_completed_job(directory,state,chosen_id,'evaluate',candidate),
                 final_id:_completed_job(directory,state,final_id,'finalize',candidate)}
        _need(results[final_id].get('pass') is final.get('pass'),'Final result and session summary differ')
        media=experiment.get('reference_mode')=='media_bundle';reference_package=None
        if media:
            from .media_session import load_bundle
            bundle=load_bundle(state['reference'])
            _need(digest(bundle)==experiment.get('bundle_sha256'),'Supplied video bundle configuration changed')
            binding=read_json(safe_path(Path(state['reference']),bundle['record']['capture_binding']))
            calibration=validation=[binding['scenario']];kind='media'
            reference_package=bundle['record']['package_sha256']
            _need(results[final_id].get('heldout_status')=='not_provided' and results[final_id].get('pass') is False,
                  'A supplied clip cannot be promoted to unseen validation')
        elif domain=='game':
            from kiroclops.portable_v2.specs import scenarios
            kind=experiment.get('stage');_need(kind in ('jump','ground'),'Game stage configuration is missing')
            calibration=scenarios(kind,'calibration');validation=scenarios(kind,'validation')
        else:
            from kiroclops.web_local.specs import scenarios,capture_scenarios
            kind='web';canonical='svg' if domain=='svg' else 'panel'
            policy=experiment.get('capture_policy')
            if policy is None:
                calibration=scenarios(canonical,'calibration');validation=scenarios(canonical,'validation')
            else:
                calibration=capture_scenarios(canonical,'calibration',policy)
                validation=capture_scenarios(canonical,'validation',policy)
            _need(experiment.get('calibration')==calibration and experiment.get('validation')==validation,
                  'Registered browser scenario matrix differs')
        layout=[]
        def add(job,context,roles,cases,record_kind,measured=True):
            layout.extend((job,context,role,cases,record_kind,measured) for role in roles)
        add(prepare_id,'',('reference','before'),calibration,kind,mode=='tools')
        # Legacy neutral game evaluation stores its raw exported records only in
        # the bound result. It never produced candidate.json or observations.
        baseline_game=domain=='game' and not media and mode=='baseline'
        if not baseline_game:add(chosen_id,'',('candidate',),calibration,kind,mode=='tools')
        add(final_id,'',('reference','before','candidate' if domain=='game' and not media else 'final'),validation,kind)
        if domain=='game' and not media:
            from .visual_boundary_v4 import cases
            add(final_id,'boundary',('candidate',),cases(),'boundary')
            if kind=='ground':
                from kiroclops.portable_v2.specs import scenarios
                if mode=='tools':
                    add(prepare_id,'jump',('reference','before'),scenarios('jump','calibration'),'jump')
                    add(chosen_id,'jump',('candidate',),scenarios('jump','calibration'),'jump')
                add(final_id,'jump',('reference','before','candidate'),scenarios('jump','validation'),'jump')
        expected_paths={directory/'measurements'/job/context/(role+'.json') for job,context,role,*_ in layout}
        for job in (prepare_id,chosen_id,final_id):
            found={p for role in ROLES for p in (directory/'measurements'/job).rglob(role+'.json')}
            _need(found=={p for p in expected_paths if p.is_relative_to(directory/'measurements'/job)},
                  'Required measurement group/role index is missing or unexpected')
        required=set();producer_cache={}
        for job,context,role,cases,record_kind,measured in layout:
            rows=read_json(directory/'measurements'/job/context/(role+'.json'))
            _need(isinstance(rows,list) and len(rows)==len(cases),'Required recording count differs')
            keys=[digest(_scenario(r.get('scenario'),record_kind)) for r in rows if isinstance(r,dict)]
            expected=[digest(_scenario(s,record_kind)) for s in cases]
            _need(len(keys)==len(rows) and len(set(keys))==len(keys) and set(keys)==set(expected),
                  'Required scenarios are missing, duplicated, or wrong')
            by_key=dict(zip(expected,cases))
            for row,key in zip(rows,keys):
                _record_origin(directory,state,job,context,role,row)
                value=_record_observation(row,by_key[key],record_kind,measured=measured,
                    reference_package=reference_package if media and role=='reference' else None,
                    expected_source=_producer_expectation(directory,state,candidate,role,context,record_kind,by_key[key],producer_cache))
                if value:required.add(value)
        if baseline_game:
            rows=results[chosen_id].get('candidate_media',[])
            _need(len(rows)==len(calibration),'Neutral game evaluation raw records are missing')
            keys=[digest(_scenario(r.get('scenario'),kind)) for r in rows]
            expected={digest(_scenario(s,kind)):s for s in calibration}
            _need(len(set(keys))==len(keys) and set(keys)==set(expected),'Neutral game scenarios differ')
            for row,key in zip(rows,keys):
                # Neutral exports contain only the copied package and scenario;
                # their actual native raw receipt stays in the selected job.
                from kiroclops.portable_v2.specs import run_name
                exported=directory/'target/.kiroclops-evidence/candidates'/candidate['candidate_id']/run_name(expected[key])
                _need(Path(row['package']).resolve()==exported.resolve(),'Neutral export belongs to another candidate')
                native={**row,'raw_run':str(directory/'measurements'/chosen_id/'candidate/raw'/run_name(expected[key]))}
                _record_observation(native,expected[key],kind,measured=False,
                    expected_source=_producer_expectation(directory,state,candidate,'candidate','',kind,expected[key],producer_cache))
        _need(bool(required),'No complete measured observation set')
        return required
    except (OSError,KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,VisualError):raise
        raise VisualError('incomplete_review','Missing or malformed required measurement evidence: '+str(exc)) from exc
