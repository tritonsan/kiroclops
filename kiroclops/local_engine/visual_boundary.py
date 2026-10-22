"""Additive pixel-visible wall controls using a non-input private desktop.

Outputs describe visible approach, parallel motion and departure. They never
infer a physics collision, wall normal, contact flag or motor impulse.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import math
import hashlib

import cv2
import numpy as np

from kiroclops.common import ROOT
from kiroclops.integration.storage import copy_project
from kiroclops.portable_v2 import runtime
from kiroclops.visual.schema import VisualError,read_json,write_json,digest,file_sha256
from kiroclops.visual.package import read_image,load_package
from kiroclops.visual.compat import import_legacy_run,selection_from_legacy
from kiroclops.visual.tracking import analyze_package
from kiroclops.visual.events import extract_events
from .backend import _lock

VERSION='heart-visual-boundary-v3'
SCENE='res://kiroclops_visual_boundary_v3/Boundary.tscn'
ADAPTER=ROOT/'adapters/heart-visual-boundary-v3'


def policy():
    files=[Path(__file__),ADAPTER/'Boundary.tscn',ADAPTER/'boundary.gd',
           *[ROOT/f'kiroclops/visual/{name}.py' for name in ('tracking','events','compat','schema','package')]]
    return {'version':VERSION,'source_hashes':{str(p.relative_to(ROOT)):file_sha256(p) for p in files},
            'parent_runtime':runtime.protocol_record(),'viewport':[320,180],'fps':60,'frames':240,'warmup':60,
            'visible_near_distance_px':12.,'minimum_approach_px':6.,'minimum_parallel_px':5.,
            'minimum_departure_px':5.,'observation_scope':'Visible selected patch and image-derived wall edge only; no collision claim.'}


def cases():
    result=[]
    for side in ('left','right'):
        for kind in ('approach','parallel','rebound','near_miss'):
            events=[]
            if kind!='near_miss':events=[(30,side,True),(54 if kind=='rebound' else 90,side,False)]
            if kind!='approach':events += [(30,'jump',True),(33,'jump',False)]
            if kind=='rebound':events += [(54,'jump',True),(57,'jump',False)]
            result.append({'id':f'{side}-{kind}','kind':kind,'wall_side':side,
                'spawn_offset':(48 if side=='left' else 0) if kind=='near_miss' else (0 if side=='left' else 48),
                'split':'regression','repeat':1,'warmup_ticks':60,'frame_count':240,
                'events':[{'tick':t,'action':a,'pressed':p} for t,a,p in sorted(events,key=lambda e:(e[0],e[2],e[1]))]})
    return result


def visible_wall(image, side):
    """Select the inner face of a long visible vertical structure from pixels."""
    edges=cv2.Canny(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),50,150)
    lines=cv2.HoughLinesP(edges,1,np.pi/180,threshold=30,minLineLength=55,maxLineGap=4)
    candidates=[]
    if lines is not None:
        for x1,y1,x2,y2 in np.asarray(lines).reshape(-1,4):
            if abs(int(x1)-int(x2))>1 or abs(int(y2)-int(y1))<55:continue
            x=float((x1+x2)/2)
            if (side=='right' and x<=160) or (side=='left' and x>=160):continue
            candidates.append((x,float(min(y1,y2)),float(max(y1,y2))))
    if not candidates:raise VisualError('tracking_failure','No sufficiently long visible wall edge')
    inner=(min if side=='right' else max)(x[0] for x in candidates)
    selected=[x for x in candidates if abs(x[0]-inner)<=1]
    return {'id':'wall','points':[[inner,min(x[1] for x in selected)],[inner,max(x[2] for x in selected)]],
            'source':'Canny/Hough native pixel edge; not fixture coordinates'}


def classify(observations,events):
    frames=observations['objects'][0]['frames']
    if observations.get('status')!='ok' or not frames or sum(bool(f['valid']) for f in frames)/len(frames)<.95:
        return {'status':'unobservable','classification':None,'physics_collision_inferred':False}
    relation={r['index']:r for r in events['relations'] if r['type']=='boundary_distance' and r['boundary_id']=='wall'}
    measured=[(f,relation[f['index']]['distance_px']) for f in frames if f['valid'] and f['index']>=30 and f['index'] in relation]
    if len(measured)<20:return {'status':'unobservable','classification':None,'physics_collision_inferred':False}
    near=[(f,d) for f,d in measured if d<=12]
    approaches=[];parallel=[];departures=[]
    for start in range(len(measured)-5):
        section=measured[start:start+6]
        if any(b[0]['index']!=a[0]['index']+1 for a,b in zip(section,section[1:])):continue
        a,da=section[0];b,db=section[-1]
        if da-db>=6 and db<=12:approaches.append([a['index'],b['index']])
        if max(d for _,d in section)<=12 and abs(b['x']-a['x'])<=2 and abs(b['y']-a['y'])>=5:
            parallel.append([a['index'],b['index']])
        if da<=12 and db-da>=5:departures.append([a['index'],b['index']])
    # A commanded movement away near a wall is merely visible departure. The
    # label deliberately does not assert contact, rebound force or causation.
    classification='visible_departure_after_approach' if approaches and departures and departures[-1][0]>approaches[0][0] else 'visible_parallel_motion' if parallel else 'visible_approach' if approaches else 'distant_motion' if not near else 'visible_proximity_only'
    return {'status':'observed','classification':classification,'near_frame_count':len(near),
            'approach_windows':approaches,'parallel_windows':parallel,'departure_windows':departures,
            'physics_collision_inferred':False,'scope':'Selected visible patch proximity and motion, not the hidden collision shape or physics state.'}


def _render(project,output,scenario,cancel):
    output.mkdir(parents=True)
    config={**scenario,'output_dir':str(output),'normal_level':False,'game':'heart','variant':'before','viewport':[320,180],'fps':60}
    write_json(output/'capture_config.json',config)
    argv=[str(runtime._engine()),'--path',str(project),'--fixed-fps','60','--resolution','320x180',
          '--rendering-method','gl_compatibility','--windowed','--disable-vsync',SCENE]
    process=output/'private-process';env=runtime._environment(output/'environment',output/'capture_config.json')
    before=runtime.source_hashes(project);protocol=policy()
    with runtime._engine_lock(cancel,120) as remaining:
        proof=runtime.private.run_private_desktop(argv,cwd=project,env=env,output_dir=process,timeout=remaining,cancel=cancel)
    runtime._verify_process(proof,process,argv,project,True)
    if proof!=read_json(process/'process.json'):raise VisualError('isolation_error','Returned process proof differs')
    if proof.get('environment_sha256')!=hashlib.sha256(runtime.private.environment_block(env).encode('utf-16-le')).hexdigest():
        raise VisualError('isolation_error','Private environment binding differs')
    log='\n'.join((process/name).read_text(encoding='utf-8',errors='replace') for name in ('stdout.log','stderr.log'))
    if runtime.ENGINE_VERSION not in log or runtime._SCRIPT_ERROR.search(log):
        raise VisualError('engine_error','Boundary script/resource execution did not complete cleanly')
    if runtime.source_hashes(project)!=before or policy()!=protocol:raise VisualError('source_changed','Boundary source or code changed during capture')
    recording=read_json(output/'recording.json')
    runtime.validate_recording(recording,{k:v for k,v in scenario.items() if k not in ('kind','wall_side')},False)
    frame_hashes=runtime._frames(output)
    manifest={'schema_version':1,'status':'ok','game':'heart','variant':'before','fps':60,'viewport':[320,180],
        'frame_count':240,'warmup_ticks':60,'frames':recording['frames'],'scenario':scenario,
        'raw_frame_hashes':frame_hashes,'provenance':{'protocol':protocol,'source_hashes':before,
        'process_sha256':file_sha256(process/'process.json'),'config_sha256':file_sha256(output/'capture_config.json')}}
    write_json(output/'manifest.json',manifest)
    write_json(output/'runtime-receipt.json',{'status':'pass','protocol':protocol,'project':str(project),
        'source_hashes':before,'argv':argv,'process':str(process/'process.json'),
        'process_sha256':file_sha256(process/'process.json'),'manifest_sha256':file_sha256(output/'manifest.json'),
        'raw_frame_hashes':frame_hashes,'non_input_desktop':True,'owned_cleanup_verified':True})
    return manifest


def run_qualification(source_project,output,*,cancel=None,progress=lambda *args:None):
    """Eight native cases, serialized by the common capture lease, no model run."""
    source_project=Path(source_project).resolve();output=Path(output).resolve()
    if output.exists():raise VisualError('output_exists','Fresh qualification directory required')
    output.mkdir(parents=True)
    original=runtime.source_hashes(source_project)
    project=output/'project';copy_project(source_project,project)
    overlay=project/'kiroclops_visual_boundary_v3';overlay.mkdir()
    for name in ('Boundary.tscn','boundary.gd'):(overlay/name).write_bytes((ADAPTER/name).read_bytes())
    expected={'approach':'visible_approach','parallel':'visible_parallel_motion',
              'rebound':'visible_departure_after_approach','near_miss':'distant_motion'}
    rows=[];registered=policy();write_json(output/'qualification-start.json',{'protocol':registered,'original_sources':original,'cases':cases()})
    with _lock(ROOT/'artifacts/local-engine/capture.lock',timeout=180):
        runtime.import_project(project,output/'import',cancel=cancel)
        for index,scenario in enumerate(cases()):
            runtime._cancel(cancel)
            raw=output/'raw'/scenario['id'];_render(project,raw,scenario,cancel)
            package=output/'packages'/scenario['id'];import_legacy_run(raw,package)
            selection=selection_from_legacy(raw,ROOT/'config/phase2c-v2/heart-ground-annotation.json')
            selection['objects'][0]['horizontal_mirror']=True
            # Keep the same physical midline anchor while excluding the outer
            # outline pixel which the visible wall can legitimately occlude.
            item=selection['objects'][0];item['roi'][0]+=1;item['roi'][2]-=2;item['anchor'][0]-=1
            selection['boundaries']=[visible_wall(read_image(raw/'frames/000000.png'),scenario['wall_side'])]
            observation=analyze_package(package,selection,{'require_static_camera':True,'min_valid_ratio':.95})
            event=extract_events(observation,selection,{'event_options':{'boundary_tolerance_px':12}})
            result=classify(observation,event)
            measured=output/'measurement'/scenario['id']
            for name,value in [('selection',selection),('observations',observation),('events',event),('result',result)]:write_json(measured/f'{name}.json',value)
            row={'id':scenario['id'],'expected':expected[scenario['kind']],'actual':result['classification'],
                'pass':result['status']=='observed' and result['classification']==expected[scenario['kind']],
                'package':str(package),'measurement':str(measured),'runtime_receipt':str(raw/'runtime-receipt.json'),
                'source':'Real unchanged Heart controller and visible native PNGs; private contact telemetry excluded from classifier'}
            rows.append(row);progress('visual_boundary',index+1,8)
    if runtime.source_hashes(source_project)!=original or policy()!=registered:raise VisualError('source_changed','Qualification source changed')
    result={'schema_version':1,'protocol':registered,'status':'pass' if all(r['pass'] for r in rows) else 'fail',
        'rows':rows,'source_preserved':True,'original_sources':original,'project_sources':runtime.source_hashes(project),
        'physics_collision_inferred':False,'independent_review_status':'pending'}
    write_json(output/'result.json',result)
    return result


def verify_qualification(output):
    """Replay actual pixels and private-process evidence; no native execution."""
    try:
        output=Path(output).resolve();result=read_json(output/'result.json');project=output/'project'
        if result['protocol']!=policy() or result.get('physics_collision_inferred') is not False:
            raise VisualError('stale_evidence','Visual-wall policy differs')
        if runtime.source_hashes(project)!=result['project_sources']:
            raise VisualError('source_changed','Visual-wall project differs')
        added={f'kiroclops_visual_boundary_v3/{name}':file_sha256(ADAPTER/name) for name in ('Boundary.tscn','boundary.gd')}
        if result['project_sources']!={**result['original_sources'],**added}:
            raise VisualError('source_changed','Fixture copy changed original controller/assets')
        registered=read_json(output/'qualification-start.json')
        if registered['cases']!=cases() or registered['protocol']!=policy() or registered['original_sources']!=result['original_sources']:
            raise VisualError('stale_evidence','Initial qualification binding differs')
        if len(result['rows'])!=8 or [r['id'] for r in result['rows']]!=[r['id'] for r in cases()]:
            raise VisualError('missing_case','All eight mirrored controls are required')
        verified=[]
        for row,scenario in zip(result['rows'],cases()):
            raw=output/'raw'/scenario['id'];receipt=read_json(raw/'runtime-receipt.json')
            process=raw/'private-process';proof=read_json(process/'process.json')
            argv=[str(runtime._engine()),'--path',str(project),'--fixed-fps','60','--resolution','320x180',
                  '--rendering-method','gl_compatibility','--windowed','--disable-vsync',SCENE]
            runtime._verify_process(proof,process,argv,project,True)
            if (receipt['source_hashes']!=result['project_sources'] or receipt['protocol']!=policy()
                    or receipt['process_sha256']!=file_sha256(process/'process.json')
                    or receipt['manifest_sha256']!=file_sha256(raw/'manifest.json')
                    or receipt['raw_frame_hashes']!=runtime._frames(raw)):
                raise VisualError('stale_evidence','Native source/process/pixels changed')
            manifest=read_json(raw/'manifest.json');config=read_json(raw/'capture_config.json')
            if manifest['scenario']!=scenario or any(config.get(k)!=v for k,v in scenario.items()):
                raise VisualError('wrong_case','Input recipe changed')
            runtime.validate_recording(read_json(raw/'recording.json'),{k:v for k,v in scenario.items() if k not in ('kind','wall_side')},False)
            package=output/'packages'/scenario['id'];pkg=load_package(package)
            if [f['sha256'] for f in pkg['frames']]!=[receipt['raw_frame_hashes'][f'{i:06d}.png'] for i in range(240)]:
                raise VisualError('stale_evidence','Pixel package differs from native frames')
            measured=output/'measurement'/scenario['id'];selection=read_json(measured/'selection.json')
            observations=analyze_package(package,selection,{'require_static_camera':True,'min_valid_ratio':.95})
            events=extract_events(observations,selection,{'event_options':{'boundary_tolerance_px':12}})
            actual=classify(observations,events)
            if observations!=read_json(measured/'observations.json') or events!=read_json(measured/'events.json') or actual!=read_json(measured/'result.json'):
                raise VisualError('stale_evidence','Fresh visual measurement differs')
            passed=actual['status']=='observed' and actual['classification']==row['expected']
            if row['actual']!=actual['classification'] or row['pass']!=passed:raise VisualError('stale_evidence','Saved classification differs')
            verified.append(passed)
        status='pass' if all(verified) else 'fail'
        if result['status']!=status:raise VisualError('stale_evidence','Qualification aggregate differs')
        return {'status':status,'verified_cases':8,'result_sha256':file_sha256(output/'result.json'),
                'independent_review_status':'pending','physics_collision_inferred':False}
    except (OSError,KeyError,TypeError,ValueError,runtime.PortableRuntimeError) as exc:
        return {'status':'fail','reason':str(exc),'physics_collision_inferred':False}
