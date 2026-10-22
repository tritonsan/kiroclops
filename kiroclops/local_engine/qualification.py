"""Engineering-only controls. Never copy these private sources into model sessions.

Good controls are independent captures of the reference application. Deliberately
broken controls are ordinary runnable source mutations, not forged measurements.
Numerical classification and independent review are separate acceptance gates.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json

ROOT=Path(__file__).resolve().parents[2]


def analyzer_sources():
    paths=list((ROOT/'kiroclops/visual').glob('*.py'))+list((ROOT/'kiroclops/web_local').glob('*.py'))
    # Python executes package initializers before the actual detached worker.
    # Its result hashes bind data, so the worker code must be frozen explicitly.
    paths.append(ROOT/'kiroclops/__init__.py')
    paths += list((ROOT/'kiroclops/cloud').glob('*.py'))
    paths += [ROOT/'kiroclops/local_engine'/name for name in
              ('__init__.py','worker.py','qualification.py','evaluation.py','backend.py','game.py','prepared_evidence.py','calibration_planning.py','geometry_acceptance.py','reference_acceptance.py','reference_observability.py','reference_cache.py')]
    return {p.relative_to(ROOT).as_posix():file_sha256(p) for p in sorted(paths)}


def controls(domain):
    if domain not in ('panel','svg'):raise ValueError('Expected panel or svg')
    return [{'id':f'{domain}-{name}','domain':domain,'mutation':mutation,'scenario_index':index,
             'expected_class':expected} for name,mutation,index,expected in [
        ('correct-first','none',0,'correct'),('correct-second','none',2,'correct'),
        ('slow','baseline',0,'behavioral_deviation'),
        ('child-timing' if domain=='panel' else 'overdamped','child_timing' if domain=='panel' else 'overdamped',0,'behavioral_deviation'),
        ('wrong-endpoint','wrong_endpoint',0,'behavioral_deviation'),
        ('lost-return' if domain=='panel' else 'detached-edge',
         'lost_return' if domain=='panel' else 'detached_edge',2 if domain=='panel' else 0,
         'behavioral_deviation' if domain=='panel' else 'application_integrity_failure')]]


def _control_source(domain,mutation,output):
    from kiroclops.web_local.specs import fixture_project,editable_files
    destination=Path(output)
    shutil.copytree(fixture_project(domain,'reference'),destination)
    motion=destination/editable_files(domain)[0]
    original=motion.read_text(encoding='utf-8');changed=original
    if mutation=='baseline':changed=(fixture_project(domain,'target')/editable_files(domain)[0]).read_text(encoding='utf-8')
    elif mutation=='overdamped':
        changed=original.replace('17*velocity','42*velocity') if domain=='panel' else original.replace('16*vx','40*vx').replace('16*vy','40*vy')
    elif mutation=='child_timing':
        changed=original.replace('titleDelay=input.open ? .08 : .12,contentDelay=input.open ? .16 : .04',
                                 'titleDelay=input.open ? .18 : .04,contentDelay=input.open ? 0 : .18')
    elif mutation=='wrong_endpoint':
        changed=original.replace('155*(target-x)','155*((target>100?target-60:target)-x)') if domain=='panel' else original.replace('145*(target.x-x)','145*((node.id===\'c\'?target.x-45:target.x)-x)')
    elif mutation=='lost_return':
        changed='let farthest=0;\n'+original.replace('let x=state.x,velocity=state.velocity;','farthest=Math.max(farthest,target);target=farthest;let x=state.x,velocity=state.velocity;')
    elif mutation=='detached_edge':
        app=destination/'src/app.js';text=app.read_text(encoding='utf-8')
        modified=text.replace("line.setAttribute('x1',a.x)","line.setAttribute('x1',a.x+120)")
        if modified==text:raise ValueError('Detached-edge control did not change app source')
        app.write_text(modified,encoding='utf-8')
    elif mutation!='none':raise ValueError('Unknown private engineering control')
    if mutation not in ('none','detached_edge') and changed==original:raise ValueError('Control mutation did not change source')
    if changed!=original:motion.write_text(changed,encoding='utf-8')
    return destination


def _classify(capture,measurement,comparison):
    if capture.get('timing',{}).get('status')!='pass':return 'capture_timing_failure'
    if capture.get('functional',{}).get('status')!='pass':return 'application_integrity_failure'
    if not measurement or measurement.get('measurement_status')!='ok':return 'tracking_failure'
    return 'correct' if comparison and comparison.get('pass') else 'behavioral_deviation'


def bind_evidence(row):
    """Bind the actual immutable evidence files, separately from source hashes.

    PNG hashes are transitively bound by each package manifest. Missing/corrupt
    PNGs intentionally remain evidence for rejection controls; do not load them
    here or manufacture successful observations for a failed capture.
    """
    paths=[]
    for name in ('package_dir','reference_package_dir'):
        if row.get(name):paths.append(Path(row[name])/'manifest.json')
    for name in ('selection_path','contract_path','analysis_path','result_path',
                 'reference_selection_path','reference_analysis_path'):
        if row.get(name):
            path=Path(row[name]);paths.append(path)
            if name.endswith('analysis_path') and (path.parent/'observations.json').is_file():
                paths.append(path.parent/'observations.json')
    missing=[str(path) for path in paths if not path.is_file()]
    if missing:raise VisualError('missing_evidence','Qualification evidence missing: '+', '.join(missing))
    return {**row,'evidence_hashes':{str(path.resolve()):file_sha256(path) for path in sorted(set(paths))}}


def _record_capture(project,output,scenario,progress,cancel):
    from kiroclops.web_local.capture import capture_run
    captured=capture_run(project,output,scenario,variant='private_engineering_control',cancel=cancel,progress=progress)
    return {'scenario':scenario,'package':captured['package_dir'],'timing':captured['timing'],
            'functional':captured['functional'],'source_sha256':digest(captured['source_hashes'])},captured


def run_web_corpus(output,*,progress=print,cancel=None,capture_policy=None):
    """Capture then measure 12 web controls; never silently retry or replace one."""
    from kiroclops.web_local.specs import scenarios,fixture_project,capture_scenarios
    from .evaluation import _measure_record,_compare_web
    output=Path(output).resolve()
    if output.exists():raise VisualError('output_exists','Qualification output must be fresh')
    output.mkdir(parents=True);sources=analyzer_sources()
    specification=[case for domain in ('panel','svg') for case in controls(domain)]
    recipes={d:scenarios(d,'calibration') if capture_policy is None else capture_scenarios(d,'calibration',capture_policy)
             for d in ('panel','svg')}
    write_json(output/'registration.json',{'kind':'EngineeringControlRegistration','schema_version':1,
        'controls':specification,'source_hashes':sources,'independent_review_status':'pending',
        **({'capture_policy':capture_policy} if capture_policy is not None else {}),
        'scope':'Qualification controls, not Kiro attempts, held-out model evidence, or private reference disclosure.'})
    references={};captured_cases=[]
    # Browser operations are serialized and finish before CPU-heavy analysis.
    for domain in ('panel','svg'):
        reference_project=output/'private-projects'/('reference-'+domain)
        shutil.copytree(fixture_project(domain,'reference'),reference_project)
        for index in (0,2):
            scenario=recipes[domain][index]
            progress(f'Reference {domain} {scenario["id"]}')
            record,captured=_record_capture(reference_project,output/'references'/domain/scenario['id'],scenario,progress,cancel)
            references[(domain,index)]=(record,captured)
        for case in controls(domain):
            project=_control_source(domain,case['mutation'],output/'private-projects'/case['id'])
            scenario=recipes[domain][case['scenario_index']]
            progress(f'Control {case["id"]}')
            record,captured=_record_capture(project,output/'cases'/case['id']/'capture',scenario,progress,cancel)
            captured_cases.append((case,record,captured))
    write_json(output/'capture-index.json',{'references':[{ 'domain':key[0],'scenario_index':key[1],'record':v[0],'capture':v[1]} for key,v in references.items()],
                                          'cases':[{'case':c,'record':r,'capture':v} for c,r,v in captured_cases]})
    return measure_web_corpus(output,progress=progress)


def requalify_web_corpus(source,output,*,progress=print):
    """Register a new analyzer against unchanged recorded pixels after a tool fix.

    A changed recorder/oracle/scenario requires new native captures. Selection
    and analysis changes can reuse the same hash-verified image observations.
    Earlier registrations, source maps and failed verdicts stay untouched.
    """
    from kiroclops.visual.package import load_package
    from kiroclops.web_local.capture import adapter_hashes
    source=Path(source).resolve();output=Path(output).resolve()
    if output.exists():raise VisualError('output_exists','Requalification must have its own output')
    index=read_json(source/'capture-index.json');old=read_json(source/'registration.json');current=adapter_hashes()
    for entry in index['references']+index['cases']:
        captured=entry['capture'];manifest=load_package(entry['record']['package'])
        if manifest['metadata']['source_hashes']!=captured['source_hashes']:
            raise VisualError('source_changed','Captured project fingerprint disagrees with immutable package')
        # Pixel selection is re-executed by the analyzer. Every other adapter
        # dependency belongs to the original recording, including the clock,
        # delivery ordering, and future helpers. A partial fixed allowlist
        # would silently reuse old timing evidence after a recorder change.
        recorded=captured.get('adapter_hashes')
        if not isinstance(recorded,dict):
            raise VisualError('source_changed','Missing original recorder dependency receipt')
        for name in sorted((set(recorded)|set(current))-{'selection.py'}):
            if recorded.get(name)!=current.get(name):
                raise VisualError('source_changed',f'Recorder dependency changed: {name}; recapture required')
        if manifest['metadata']['adapter_hashes']!=captured['adapter_hashes']:
            raise VisualError('source_changed','Capture adapter receipts disagree')
    output.mkdir(parents=True)
    write_json(output/'registration.json',{'kind':'EngineeringControlRegistration','schema_version':1,
        'controls':old['controls'],'source_hashes':analyzer_sources(),'independent_review_status':'pending',
        'reuses_immutable_captures_from':str(source),'previous_registration_sha256':file_sha256(source/'registration.json'),
        'previous_manifest_sha256':file_sha256(source/'manifest.json') if (source/'manifest.json').exists() else None,
        'scope':'New measurement version over unchanged recorded image/input evidence; not a new model run or retried behavior.'})
    write_json(output/'capture-index.json',index)
    return measure_web_corpus(output,progress=progress)


def measure_web_corpus(output,*,progress=print,analysis_output=None):
    """Reanalyze preserved captures explicitly; source drift is reported, not hidden."""
    from .evaluation import _measure_record,_compare_web
    output=Path(output).resolve();index=read_json(output/'capture-index.json');registration=read_json(output/'registration.json')
    analysis_root=Path(analysis_output).resolve() if analysis_output else output/'analysis'
    if analysis_root.exists():raise VisualError('output_exists','Use a fresh analysis_output; preserve earlier qualification results')
    sources=analyzer_sources();references={};reference_failures=[];rows=[]
    for entry in index['references']:
        key=(entry['domain'],entry['scenario_index']);record=entry['record'];captured=entry['capture']
        if captured['status']!='ok':
            reference_failures.append({'domain':key[0],'scenario_index':key[1],'capture':captured});continue
        measured=_measure_record(record,analysis_root/'references'/key[0]/str(key[1]),lambda:None,lambda *args:None)
        references[key]=measured
        if measured['measurement_status']!='ok':reference_failures.append({'domain':key[0],'scenario_index':key[1],'reason':'reference_tracking_failure'})
    for entry in index['cases']:
        case,record,captured=entry['case'],entry['record'],entry['capture'];progress(f'Measure {case["id"]}')
        directory=analysis_root/case['id'];reference=references.get((case['domain'],case['scenario_index']))
        measured=None;comparison=None;error=None
        try:
            if reference and captured['timing']['status']=='pass':
                measured=_measure_record(record,directory,lambda:None,lambda *args:None,reference['selection'])
                if measured['measurement_status']=='ok':comparison=_compare_web(reference,reference,measured,improvement=False)
        except (VisualError,ValueError) as exc:error={'code':getattr(exc,'code','tracking_failure'),'message':str(exc)}
        observed='reference_failure' if not reference else _classify(captured,measured,comparison)
        result={'expected_class':case['expected_class'],'observed_class':observed,'capture':captured,
                'comparison':comparison,'error':error,'independent_review_status':'pending'}
        write_json(directory/'result.json',result)
        contract=comparison['contract'] if comparison else {'unavailable':error or observed}
        write_json(directory/'contract.json',contract)
        row={**case,'kind':'recorded_behavior','observed_class':observed,'classification_pass':observed==case['expected_class'],
             'package_dir':record['package'],'reference_package_dir':reference['package'] if reference else None,
             'selection_path':str(directory/'selection.json') if (directory/'selection.json').exists() else None,
             'contract_path':str(directory/'contract.json'),'analysis_path':str(directory/'analysis.json') if (directory/'analysis.json').exists() else None,
             'result_path':str(directory/'result.json'),'source_hashes':captured['source_hashes'],
             'settings_sha256':digest(contract),'independent_review_status':'pending'}
        if reference:
            reference_dir=analysis_root/'references'/case['domain']/str(case['scenario_index'])
            row['reference_analysis_path']=str(reference_dir/'analysis.json')
            row['reference_selection_path']=str(reference_dir/'selection.json')
        rows.append(bind_evidence(row))
    manifest_path=output/'manifest.json' if not (output/'manifest.json').exists() else analysis_root/'manifest.json'
    after_sources=analyzer_sources()
    return write_manifest(manifest_path,rows,source_hashes=sources,
        extra={'registration_source_hashes':registration['source_hashes'],'registration_sources_unchanged':sources==registration['source_hashes'],
               'completed_source_hashes':after_sources,'reference_failures':reference_failures,
               'source_binding_status':'pass' if sources==registration['source_hashes']==after_sources else 'fail'},required=12)


def write_manifest(path,samples,*,source_hashes,extra=None,required=24):
    ids=[s.get('id') for s in samples]
    classification=len(samples)==required and len(set(ids))==required and all(s.get('classification_pass') is True for s in samples)
    classification &= not (extra or {}).get('reference_failures') and (extra or {}).get('source_binding_status','pass')=='pass'
    reviewed=all(s.get('independent_review_status')=='pass' for s in samples) and len(samples)==required
    result={'schema_version':1,'kind':'LocalVisualQualificationCorpus','required_count':required,'count':len(samples),
            'classification_status':'pass' if classification else 'fail','independent_review_status':'pass' if reviewed else 'pending',
            'status':'pass' if classification and reviewed else 'pending_review' if classification else 'fail',
            'source_hashes':source_hashes,'samples':samples,**(extra or {})}
    write_json(path,result);return result


def combine_manifests(paths,output):
    manifests=[read_json(p) for p in paths];samples=[s for m in manifests for s in m['samples']]
    component_status=all(m.get('classification_status')=='pass' for m in manifests)
    combined_sources=analyzer_sources()
    for manifest in manifests:
        binding=manifest.get('source_hashes')
        if not binding:component_status=False;continue
        for relative,expected in binding.items():
            path=(ROOT/relative).resolve()
            if ROOT.resolve() not in path.parents or not path.is_file() or file_sha256(path)!=expected:
                component_status=False
            if relative in combined_sources and combined_sources[relative]!=expected:
                component_status=False
            else:
                combined_sources[relative]=expected
    return write_manifest(output,samples,source_hashes=combined_sources,
        extra={'component_manifests':{str(Path(p).resolve()):file_sha256(p) for p in paths},
               'source_binding_status':'pass' if component_status else 'fail'})


def run_error_corpus(source_package,output):
    """Six explicit corruptions of copied real pixels/timing; originals are untouched."""
    import cv2
    import numpy as np
    from kiroclops.visual.package import copy_package,load_package,frame_paths,read_image
    from kiroclops.visual.tracking import analyze_package
    from kiroclops.web_local.selection import select_package_from_pixels
    output=Path(output).resolve()
    if output.exists():raise VisualError('output_exists','Error corpus output must be fresh')
    original=load_package(source_package);selection=select_package_from_pixels(source_package,'panel')
    recipes=[('duplicate-time','capture_timing_failure'),('missing-index','capture_timing_failure'),
             ('missing-frame','capture_integrity_failure'),('wrong-selection','tracking_failure'),
             ('occluded-pixels','tracking_failure'),('camera-shift','tracking_failure')]
    output.mkdir(parents=True);sources=analyzer_sources();rows=[]
    write_json(output/'registration.json',{'schema_version':1,'recipes':recipes,'source_hashes':sources,
        'original_package_sha256':original['package_sha256'],'scope':'Explicit engineering corruption controls, not real model output.'})
    for name,expected in recipes:
        directory=output/name;package=directory/'package';manifest=copy_package(source_package,package)
        chosen=deepcopy(selection);mutation={'name':name,'original_package_sha256':original['package_sha256']}
        if name=='duplicate-time':manifest['frames'][31]['time_s']=manifest['frames'][30]['time_s']
        elif name=='missing-index':manifest['frames'][31]['index']=33
        elif name=='missing-frame':
            # One asset in the freshly copied control only, never source frames.
            asset=(package/manifest['frames'][31]['path']).resolve()
            if package.resolve() not in asset.parents:raise VisualError('unsafe_path','Corruption escaped control package')
            asset.unlink()
        elif name=='wrong-selection':
            chosen['objects'][0].update(roi=[300,0,30,20],anchor=[15,10])
        else:
            for index,path in enumerate(frame_paths(package,manifest)):
                if index<30:continue
                image=read_image(path)
                if name=='occluded-pixels':image[:]=image[0,0]
                else:image=cv2.warpAffine(image,np.float32([[1,0,12],[0,1,0]]),(image.shape[1],image.shape[0]),borderMode=cv2.BORDER_CONSTANT,borderValue=tuple(int(v) for v in image[0,0]))
                ok,data=cv2.imencode('.png',image)
                if not ok:raise VisualError('invalid_frame','Control encoding failed')
                path.write_bytes(data.tobytes());manifest['frames'][index]['sha256']=file_sha256(path)
        manifest['metadata']={**manifest.get('metadata',{}),'engineering_corruption':mutation}
        manifest['package_sha256']=digest({k:v for k,v in manifest.items() if k!='package_sha256'})
        write_json(package/'manifest.json',manifest);write_json(directory/'selection.json',chosen)
        contract={'min_valid_ratio':.95,'require_static_camera':True}
        write_json(directory/'contract.json',contract);observations=None;error=None
        try:
            observations=analyze_package(package,chosen,contract)
            observed='tracking_failure' if observations['measurement_status']=='fail' else 'unexpected_success'
            write_json(directory/'analysis.json',observations)
        except VisualError as exc:
            error={'code':exc.code,'message':str(exc)}
            if exc.code=='capture_timing_failure':observed='capture_timing_failure'
            elif exc.code in ('missing_frame','package_integrity_failure','invalid_frame'):observed='capture_integrity_failure'
            elif exc.code in ('insufficient_observability','invalid_selection','tracking_failure'):observed='tracking_failure'
            else:observed=exc.code
        write_json(directory/'result.json',{'mutation':mutation,'expected_class':expected,'observed_class':observed,
            'error':error,'measurement_status':observations['measurement_status'] if observations else 'error',
            'independent_review_status':'pending'})
        rows.append(bind_evidence({'id':'error-'+name,'domain':'errors','kind':'explicit_pixel_or_record_corruption',
            'expected_class':expected,'observed_class':observed,'classification_pass':observed==expected,
            'package_dir':str(package),'reference_package_dir':str(Path(source_package).resolve()),
            'selection_path':str(directory/'selection.json'),'contract_path':str(directory/'contract.json'),
            'analysis_path':str(directory/'analysis.json') if observations else None,'result_path':str(directory/'result.json'),
            'source_hashes':{'source_package_manifest':file_sha256(Path(source_package)/'manifest.json')},
            'settings_sha256':digest(contract),'independent_review_status':'pending'}))
    return write_manifest(output/'manifest.json',rows,source_hashes=sources,required=6,
                          extra={'source_binding_status':'pass' if sources==analyzer_sources() else 'fail'})
