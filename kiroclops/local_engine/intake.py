"""Local video + actual target + natural-language request intake.

Uploaded media remains an unqualified reference until a separate experiment is
constructed and validated. Selecting an object is never benchmark acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

import cv2
import numpy as np

from kiroclops.visual.package import copy_package, create_package, frame_paths, import_video, load_package, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, validate_inputs, write_json
from kiroclops.visual.selection import propose_objects
from .session import source_hashes, _ordinary


def _text(path):
    if path.stat().st_size > 2*1024*1024:
        raise VisualError('unsupported_project', 'Entry source exceeds the inspection limit.')
    return path.read_text(encoding='utf-8-sig')


def _resolve_entry(project, parent, value):
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or value.startswith('//'):
        return None
    relative = unquote(parsed.path)
    if not relative:
        return None
    path = (project / relative.lstrip('/')) if relative.startswith('/') else (parent / relative)
    path = _ordinary(path)
    if not path.is_relative_to(project):
        raise VisualError('unsafe_project_entry', 'A project entry escapes the supplied project.')
    return path if path.is_file() else None


def _strip_js_comments(source):
    """Keep quoted literals intact while removing comments from static entry scans."""
    result, index, quote = [], 0, None
    while index < len(source):
        char = source[index]
        if quote:
            result.append(char)
            if char == '\\' and index+1 < len(source):
                result.append(source[index+1]); index += 2; continue
            if char == quote:
                quote = None
        elif char in ('"', "'", '`'):
            quote = char; result.append(char)
        elif source.startswith('//', index):
            end = source.find('\n', index)
            index = len(source) if end < 0 else end
            result.append('\n'); continue
        elif source.startswith('/*', index):
            end = source.find('*/', index+2)
            index = len(source) if end < 0 else end+2
            result.append(' '); continue
        else:
            result.append(char)
        index += 1
    return ''.join(result)


def _js_tokens(source):
    pattern = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(?:[A-Za-z_$][\w$]*)|[(){};,*]'''
    return re.findall(pattern,_strip_js_comments(source),re.S)


def _js_imports(source):
    tokens = _js_tokens(source)
    imports = []
    for index, token in enumerate(tokens):
        if token != 'import':
            continue
        following = tokens[index+1:index+129]
        if not following:
            continue
        quoted = lambda value: len(value)>1 and value[0] in ('"',"'")
        if quoted(following[0]):
            imports.append(following[0][1:-1]); continue
        if following[0]=='(':
            if len(following)>=3 and quoted(following[1]) and following[2]==')':
                imports.append(following[1][1:-1])
            continue
        for offset, part in enumerate(following[:-1]):
            if part in (';','import'):
                break
            if part=='from' and quoted(following[offset+1]):
                imports.append(following[offset+1][1:-1]); break
    return imports


def _exports_function(source, name):
    tokens = _js_tokens(source)
    for index, token in enumerate(tokens):
        if token=='export':
            following = tokens[index+1:index+5]
            if following[:1]==['async']:
                following = following[1:]
            if following[:3]==['function',name,'(']:
                return True
    return False


def detect_project(project):
    """Require a supported real controller reachable from the application's entry."""
    project = _ordinary(project)
    if not project.is_dir():
        raise VisualError('unsupported_project', 'Expected an existing local project directory.')
    found = []
    if (project / 'index.html').is_file():
        html = re.sub(r'<!--.*?-->', '', _text(project / 'index.html'), flags=re.S)
        sources = re.findall(r'<script\b[^>]*\bsrc\s*=\s*[\"\']([^\"\']+)[\"\']', html, re.I)
        pending = [path for value in sources if (path := _resolve_entry(project, project, value))]
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            if len(visited) >= 128:
                raise VisualError('unsupported_project', 'Entry graph exceeds the supported inspection limit.')
            visited.add(path)
            if path.suffix.lower() not in ('.js', '.mjs'):
                continue
            content = _text(path)
            imports = _js_imports(content)
            pending.extend(child for value in imports if value.startswith(('.', '/')) and (child := _resolve_entry(project, path.parent, value)))
        for domain, relative, symbol in [('web_panel','src/panel-motion.js','advanceMotion'), ('svg','src/layout-motion.js','advanceLayout')]:
            entry = project / relative
            if entry in visited and _exports_function(_text(entry),symbol):
                found.append({'domain': domain, 'entry': relative, 'reachable_from': 'index.html', 'controller_export': symbol})
    if (project / 'project.godot').is_file():
        config = _text(project / 'project.godot')
        match = re.search(r'^run/main_scene\s*=\s*"res://([^"\r\n]+)"', config, re.M)
        if match:
            main = _resolve_entry(project, project, match.group(1))
            pending, visited = ([main] if main else []), set()
            while pending:
                path = pending.pop()
                if path in visited:
                    continue
                if len(visited) >= 256:
                    raise VisualError('unsupported_project', 'Scene graph exceeds the supported inspection limit.')
                visited.add(path)
                if path.suffix.lower() not in ('.tscn', '.gd'):
                    continue
                refs = re.findall(r'[\"\']res://([^\"\']+)[\"\']', _text(path))
                pending.extend(child for value in refs if (child := _resolve_entry(project, project, value)) and child.suffix.lower() in ('.tscn','.gd'))
            controller = project / 'player.gd'
            if controller in visited and re.search(r'\bextends\s+(?:KinematicBody2D|CharacterBody2D)\b', _text(controller)):
                found.append({'domain':'game','entry':'player.gd','reachable_from':'project.godot','main_scene':match.group(1)})
    if len(found) != 1:
        raise VisualError('ambiguous_project' if found else 'unsupported_project',
                          'Several supported controllers are reachable; provide one target project.' if found else
                          'No supported controller is reachable from the actual entry. Supported: panel/SVG fixture-compatible modules or a player.gd Godot scene.')
    selected = found[0]
    actions = []
    if selected['domain'] in ('web_panel','svg'):
        ids = set(re.findall(r'\bid\s*=\s*[\"\']([^\"\']+)[\"\']', html, re.I))
        proposed = ('open-panel','close-panel','reset') if selected['domain']=='web_panel' else ('add-node','remove-node','reset')
        actions = [{'action':'click','selector':'#'+value,'evidence':'actual index.html id'} for value in proposed if value in ids]
    elif selected['domain']=='game':
        input_section = re.search(r'^\[input\]\s*\n(.*?)(?=^\[|\Z)',config,re.M|re.S)
        names = re.findall(r'^([A-Za-z0-9_]+)\s*=\s*\{',input_section.group(1),re.M) if input_section else []
        actions = [{'action':'engine_action','name':name,'evidence':'actual project.godot InputMap'} for name in names]
    return {**selected, 'project': str(project), 'source_sha256': digest(source_hashes(project)),
            'supported_adapter': selected['domain'], 'recommended_capture_actions':actions,
            'capture_binding_status':'requires_review_of_action_schedule_and_object_correspondence',
            'qualification_status': 'adapter_shape_detected_not_benchmark_qualified'}


def _inputs(value):
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        value = read_json(value)
    value = deepcopy(value)
    if not isinstance(value, dict):
        raise VisualError('invalid_input_timeline', 'Provide a declared InputTimeline object or its file.')
    value.setdefault('provenance', 'user_declared')
    if value['provenance'] == 'recorder_verified':
        raise VisualError('unverified_input_claim', 'A separately supplied timeline is user-declared. Preserve verified inputs by importing their recorder package.')
    return validate_inputs(value)


def _overlap(first, second):
    x,y,w,h = first
    a,b,c,d = second
    return max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))


def _background_regions(package, proposals):
    """Find textured peripheral regions outside every observed motion envelope."""
    manifest = load_package(package)
    paths = frame_paths(package, manifest)
    initial = read_image(paths[0])
    height,width = initial.shape[:2]
    size = max(8,min(24,min(width,height)//6))
    motion = [p['roi'] for p in proposals if p['basis']=='motion_envelope']
    candidates = [[x,y,size,size] for y in (2,max(2,height-size-2)) for x in (2,max(2,width-size-2))]
    sampled = sorted(set(np.linspace(0,len(paths)-1,min(12,len(paths)),dtype=int)))
    accepted = []
    for roi in candidates:
        if any(_overlap(roi,area) for area in motion):
            continue
        x,y,w,h = roi
        patch = initial[y:y+h,x:x+w]
        if float(np.max(np.std(patch.astype(float),axis=(0,1)))) < 8:
            continue
        if all(float(np.max(cv2.absdiff(read_image(paths[i])[y:y+h,x:x+w],patch))) <= 3 for i in sampled):
            accepted.append(roi)
    return accepted[:2]


def _qualify_automatic(package, proposals):
    outlines = [p for p in proposals['proposals'] if p['basis']=='visible_outline' and p['activity_pixels'] >= 9]
    if len(outlines) != 1:
        return None, {'status':'needs_confirmation','reason':'moving_object_count_ambiguous'}
    proposal = outlines[0]
    moving = [p for p in proposals['proposals'] if p['basis']=='motion_envelope']
    if not moving or any(not _overlap(proposal['roi'],p['roi']) for p in moving):
        return None, {'status':'needs_confirmation','reason':'separate_or_unbound_motion'}
    backgrounds = _background_regions(package, proposals['proposals'])
    if not backgrounds:
        return None, {'status':'needs_confirmation','reason':'static_camera_not_qualified'}
    x,y,w,h = proposal['roi']
    selection = {'schema_version':1,'kind':'ObjectSelection','package_sha256':proposals['package_sha256'],
                 'objects':[{'id':'object-01','roi':[x,y,w,h],'anchor':[w/2,h/2], 'frame_index':0,
                             'body_height_px':h,'body_height_source':'visible_outline','method':'template',
                             'min_score':.95,'min_margin':.08,'search_radius':max(12,min(64,max(w,h)))}],
                 'background_rois':backgrounds,'selection_source':'single_outline_full_clip_qualified'}
    from kiroclops.cloud.execution import analyze_package
    try:
        observed = analyze_package(package,selection,{'min_valid_ratio':.99,'require_static_camera':True,'camera_max_shift_px':1.})
    except VisualError as exc:
        # A transport/execution failure is not ambiguity in the user's object
        # choice. Preserve the durable job and expose operational failures.
        if exc.code not in {'invalid_selection','insufficient_observability','camera_motion','tracking_failure','low_tracking_confidence',
                            'ambiguous_tracking','unsupported_tracking','invalid_roi','invalid_template'}:
            raise
        return None, {'status':'needs_confirmation','reason':exc.code}
    obj = observed['objects'][0]
    if observed['status'] != 'ok' or obj['valid_ratio'] < .99 or any(not frame['valid'] or frame.get('score',0)<.95 or frame.get('margin',0)<.08 for frame in obj['frames']):
        return None, {'status':'needs_confirmation','reason':'full_clip_tracking_not_qualified'}
    return selection, {'status':'qualified_single_object','valid_ratio':obj['valid_ratio'],
                       'observation_sha256':digest(observed),'scope':'Object and camera observability only; target transfer remains unqualified.'}


def _ui(output, manifest, proposals, request):
    data = {'package_sha256':manifest['package_sha256'], 'image_size':manifest['image_size'],
            'frames':[{'path':'package/'+f['path'],'sha256':f['sha256'],'time_s':f['time_s']} for f in manifest['frames']],
            'request':request,'proposals':proposals['proposals'],'input_provenance':manifest['inputs']['provenance']}
    encoded = json.dumps(data, ensure_ascii=True).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html = _ANNOTATION_HTML.replace('__INTAKE_DATA__',encoded)
    (output/'annotation.html').write_text(html,encoding='utf-8')


def _readiness(selected, provenance):
    reasons = ['target_capture_and_object_binding_required', 'behavior_contract_not_accepted', 'independent_measurement_review_pending']
    if provenance == 'unknown':
        reasons.append('reference_inputs_unknown_no_interaction_rule_claim')
    elif provenance == 'user_declared':
        reasons.append('reference_input_times_are_user_declared')
    return {'analysis':{'ready':bool(selected),'reasons':[] if selected else ['object_selection_required']},
            'autonomous_transfer':{'ready':False,'reasons':reasons}}


def _draft_contract(state, selection=None):
    return {'schema_version':1,'kind':'BehaviorContract','status':'draft_needs_binding','acceptance_status':'not_accepted',
            'intent':state['request'],'reference_package_sha256':state['package_sha256'],
            'input_provenance':state['input_provenance'],'reference_object_ids':[o['id'] for o in (selection or {}).get('objects',[])],
            'comparison_claim':state['response_claim'],
            'required_bindings':['target_object_correspondence','actual_target_capture_schedule','measurement_windows',
                                 'fixed_scale_and_tolerances','preservation_checks','independent_visual_review'],
            'limitations':['No inferred hidden input rules.','No numeric success until a complete frozen contract is evaluated.']}


def prepare_intake(video, project, request, output, inputs=None):
    if not isinstance(request,str) or not request.strip():
        raise VisualError('invalid_request','Describe the desired behavior in natural language.')
    project_info = detect_project(project)
    supplied_inputs = _inputs(inputs)
    video, output = _ordinary(video), _ordinary(output)
    if not video.exists():
        raise VisualError('missing_video','The local video or PNG package does not exist.')
    if output.exists():
        raise VisualError('output_exists','Use a fresh intake directory; existing evidence is never overwritten.')
    if output.is_relative_to(video) or output.is_relative_to(Path(project_info['project'])):
        raise VisualError('unsafe_path','Keep the intake outside the input package and target project.')
    output.mkdir(parents=True)
    package = output/'package'
    if video.is_dir():
        original = load_package(video)
        if supplied_inputs is None:
            manifest = copy_package(video,package)
        else:
            metadata = {**original['metadata'],'input_override_source_package_sha256':original['package_sha256']}
            fields = ('timestamp_source','uncertainty_s','time_interval_s')
            timing = [{k:f[k] for k in fields if k in f} for f in original['frames']]
            manifest = create_package(package,frame_paths(video,original),[f['time_s'] for f in original['frames']],
                                      supplied_inputs,metadata,frame_metadata=timing if all(timing) else None)
    else:
        manifest = import_video(video,package,inputs=supplied_inputs)
    proposals = propose_objects(package)
    proposals['package_sha256'] = manifest['package_sha256']
    write_json(output/'proposals.json',proposals)
    selection, qualification = _qualify_automatic(package,proposals)
    if selection:
        for obj in selection['objects']:
            obj['template_frame_sha256'] = manifest['frames'][obj['frame_index']]['sha256']
        write_json(output/'selection.json',selection)
    _ui(output,manifest,proposals,request)
    state = {'schema_version':1,'kind':'LocalVideoIntake','status':'ready_for_analysis' if selection else 'needs_selection',
             'request':request,'project':project_info,'package':'package','package_sha256':manifest['package_sha256'],
             'input_provenance':manifest['inputs']['provenance'],'selection':'selection.json' if selection else None,
             'proposal_path':'proposals.json','annotation_ui':'annotation.html','contract_draft':'behavior-contract-draft.json','selection_qualification':qualification,
             'benchmark_qualified':False,'qualification_status':'uploaded_reference_not_benchmark_qualified',
             'response_claim':'verified_input_observation' if manifest['inputs']['provenance']=='recorder_verified' else 'observational_only',
             'transfer_status':'not_started','model_started':False,
             'readiness':_readiness(selection,manifest['inputs']['provenance'])}
    write_json(output/'behavior-contract-draft.json',_draft_contract(state,selection))
    write_json(output/'intake.json',state)
    write_json(output/'intake-binding.json',{'package_sha256':manifest['package_sha256'],'project_sha256':project_info['source_sha256'],
                                           'request_sha256':digest(request),'project_path_sha256':digest(project_info['project']),
                                           'project_info_sha256':digest(project_info)})
    return _public(state,output)


def _public(state, output):
    return {**state,'output':str(output),'package_path':str(output/state['package']),
            'selection_path':str(output/state['selection']) if state['selection'] else None,
            'contract_draft_path':str(output/'behavior-contract-draft.json'),
            'annotation_path':str(output/state['annotation_ui'])}


def resume_selection(output, selection):
    output = _ordinary(output)
    state, binding = read_json(output/'intake.json'), read_json(output/'intake-binding.json')
    if state.get('schema_version') != 1 or state.get('kind') != 'LocalVideoIntake':
        raise VisualError('unsupported_schema','Expected LocalVideoIntake v1.')
    if (state.get('package') != 'package' or state.get('annotation_ui') != 'annotation.html'
            or state.get('package_sha256') != binding['package_sha256']
            or digest(state.get('request')) != binding['request_sha256']
            or digest(state['project']) != binding['project_info_sha256']
            or digest(state['project']['project']) != binding['project_path_sha256']):
        raise VisualError('intake_binding_changed','Intake request, package or target binding changed.')
    manifest = load_package(output/'package')
    if manifest['package_sha256'] != binding['package_sha256']:
        raise VisualError('source_changed','Input package changed after annotation was prepared.')
    if digest(source_hashes(state['project']['project'])) != binding['project_sha256']:
        raise VisualError('source_changed','Target project changed after intake; prepare a fresh intake.')
    if isinstance(selection,(str,Path)):
        selection = read_json(selection)
    selection = deepcopy(selection)
    if not isinstance(selection,dict) or selection.get('schema_version') != 1 or selection.get('package_sha256') != manifest['package_sha256']:
        raise VisualError('selection_binding_mismatch','Selection must bind this exact video package.')
    objects = selection.get('objects')
    if not isinstance(objects,list) or not 1 <= len(objects) <= 32:
        raise VisualError('invalid_selection','Select between one and 32 objects.')
    width,height = manifest['image_size']
    ids = set()
    def roi(value):
        if not isinstance(value,list) or len(value)!=4 or any(type(v) is not int for v in value):
            raise VisualError('invalid_selection','ROI must contain four integer pixel values.')
        x,y,w,h = value
        if min(x,y)<0 or min(w,h)<3 or x+w>width or y+h>height:
            raise VisualError('invalid_selection','ROI lies outside the natural image.')
        return value
    for obj in objects:
        if not isinstance(obj,dict) or not isinstance(obj.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',obj['id']) or obj['id'] in ids:
            raise VisualError('invalid_selection','Use distinct simple object IDs.')
        ids.add(obj['id'])
        x,y,w,h = roi(obj.get('roi'))
        anchor = obj.get('anchor')
        if not isinstance(anchor,list) or len(anchor)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in anchor) or not 0<=anchor[0]<=w or not 0<=anchor[1]<=h:
            raise VisualError('invalid_selection','Choose an anchor inside the selected body.')
        body = obj.get('body_height_px')
        if type(body) not in (int,float) or not math.isfinite(body) or not 0<body<=height:
            raise VisualError('invalid_selection','Body height must be a positive natural-pixel measurement.')
        index = obj.get('frame_index')
        if type(index) is not int or not 0<=index<len(manifest['frames']):
            raise VisualError('invalid_selection','Template frame index is outside the video.')
        if obj.get('template_frame_sha256') != manifest['frames'][index]['sha256']:
            raise VisualError('selection_binding_mismatch','Selected raw frame changed or belongs to another video.')
        if obj.get('method','template') not in ('template','flow','contour') or 'template_package' in obj:
            raise VisualError('invalid_selection','Use an in-package supported tracking method.')
        if 'geometry' in obj:
            geometry=obj['geometry']
            if not isinstance(geometry,dict) or geometry.get('method')!='contour':
                raise VisualError('invalid_selection','Visible outer geometry must use a selected contour.')
            roi(geometry.get('roi'))
        if 'active_intervals_s' in obj:
            intervals=obj['active_intervals_s']
            if not isinstance(intervals,list) or len(intervals)!=1 or not isinstance(intervals[0],list) or len(intervals[0])!=2:
                raise VisualError('invalid_selection','Select one first/last visible interval per object.')
            begin,end=intervals[0]
            if (any(type(v) not in (int,float) or not math.isfinite(v) for v in (begin,end))
                    or begin!=manifest['frames'][index]['time_s'] or end<=begin
                    or end not in {f['time_s'] for f in manifest['frames'][index:]}):
                raise VisualError('invalid_selection','Visibility boundaries must be selected recorded frames, starting at the template frame.')
    backgrounds = selection.get('background_rois',[])
    if not isinstance(backgrounds,list) or len(backgrounds)>16:
        raise VisualError('invalid_selection','Select at most sixteen fixed background regions.')
    for background in backgrounds:
        roi(background)
    connections=selection.get('connections',[]);edge_ids=set();edge_pairs=set();images={}
    if not isinstance(connections,list) or len(connections)>64:
        raise VisualError('invalid_connection_binding','Select at most 64 visible connections.')
    for edge in connections:
        if not isinstance(edge,dict):raise VisualError('invalid_connection_binding','Expected a connection object.')
        cid=edge.get('id');a=edge.get('from_object');b=edge.get('to_object')
        if (not isinstance(cid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',cid) or cid in edge_ids
                or not isinstance(a,str) or not isinstance(b,str) or a not in ids or b not in ids or a==b
                or frozenset((a,b)) in edge_pairs or edge.get('method','line') not in ('line','polyline')):
            raise VisualError('invalid_connection_binding','Use unique connections between distinct selected objects.')
        edge_ids.add(cid);edge_pairs.add(frozenset((a,b)))
        if edge.get('method')=='polyline':
            rgb=edge.get('stroke_rgb');sample=edge.get('stroke_sample',{})
            if not isinstance(rgb,list) or len(rgb)!=3 or any(type(v) is not int or not 0<=v<=255 for v in rgb):
                raise VisualError('invalid_connection_binding','Sample the visible stroke color from the raw image.')
            if not isinstance(sample,dict):raise VisualError('invalid_connection_binding','Missing raw stroke sample.')
            index=sample.get('frame_index');pixel=sample.get('pixel')
            if (type(index) is not int or not 0<=index<len(manifest['frames']) or not isinstance(pixel,list)
                    or len(pixel)!=2 or any(type(v) is not int for v in pixel)
                    or not 0<=pixel[0]<width or not 0<=pixel[1]<height
                    or sample.get('frame_sha256')!=manifest['frames'][index]['sha256']):
                raise VisualError('selection_binding_mismatch','Stroke color sample must bind an actual raw frame and pixel.')
            if index not in images:images[index]=read_image(frame_paths(output/'package',manifest)[index])
            actual=images[index][pixel[1],pixel[0],:3][::-1].tolist()
            if actual!=rgb:raise VisualError('selection_binding_mismatch','Selected stroke color differs from the bound raw pixel.')
            for key,default,maximum in (('stroke_tolerance',12,40),('search_margin_px',64,1920)):
                value=edge.get(key,default)
                if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=maximum:
                    raise VisualError('invalid_connection_binding',f'Invalid {key}.')
    selection['selection_source'] = 'user_confirmed_local_annotation'
    write_json(output/'selection.json',selection)
    state.update(status='ready_for_analysis',selection='selection.json',selection_qualification={
        'status':'user_confirmed_not_measurement_qualified','camera_regions_selected':len(backgrounds),
        'scope':'Run pixel analysis and independent checks before accepting any transfer.'})
    state['readiness'] = _readiness(selection,state['input_provenance'])
    write_json(output/'behavior-contract-draft.json',_draft_contract(state,selection))
    write_json(output/'intake.json',state)
    return _public(state,output)


_ANNOTATION_HTML = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KiroClops · Local object selection</title>
<style>body{font:16px system-ui;margin:0;background:#101827;color:#e7eefb}main{max-width:1100px;margin:auto;padding:24px}h1{font-size:26px}p{line-height:1.5;color:#bccbe0}.bar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:14px 0}button,input,select{font:inherit;padding:9px;border:1px solid #61728d;border-radius:6px;background:#1b2c45;color:white}button{cursor:pointer}button:focus,input:focus,select:focus{outline:2px solid #a8cfff}canvas{display:block;max-width:100%;height:auto;border:1px solid #526786;touch-action:none;background:#030815}label{display:flex;gap:7px;align-items:center}#message{min-height:28px;color:#ffdb8b}li{margin:8px 0}.notice{border-left:3px solid #80b5ff;padding-left:12px}input[type=number]{width:88px}#timeline{width:100%}</style>
<main><h1>KiroClops · Select the object in the video</h1><p id="goal"></p><p class="notice" id="provenance"></p>
<p>Drag to frame the stable visible part of the body. In “Tracking point” mode, click on the body. Enter the body height in natural pixels and add the object. For the fixed-camera check, also select a textured background region far from the object in the first frame.</p>
<label>Frame <output id="frame-number">0</output><span id="time"></span></label><input id="timeline" type="range" min="0" value="0" aria-label="Video frame">
<canvas id="canvas" aria-label="Object and background selection area"></canvas>
<div class="bar"><label>Selection <select id="mode"><option value="body">Body frame</option><option value="anchor">Tracking point</option><option value="geometry">SVG outer boundary</option><option value="background">Fixed background</option><option value="stroke">Connection color</option></select></label><label>Object ID <input id="object-id" value="object-01" maxlength="64"></label><label>Body height (px) <input id="body-height" type="number" min="1" step="0.5"></label><label>Last visible frame <input id="last-visible" type="number" min="1" step="1"></label><button id="add" type="button">Add object</button></div>
<p>For SVG, in addition to the tracking mark, draw the actual outer boundary of the card. Select the object in its first visible frame; if it is deleted, specify the last visible frame. The default is the end of the clip. Do not mark occlusion as deletion; complex disappearing and reappearing is not supported in this selection.</p>
<p id="message" role="status" aria-live="polite"></p><ul id="objects"></ul>
<div class="bar"><label>Start <select id="connection-from"></select></label><label>End <select id="connection-to"></select></label><label>Type <select id="connection-method"><option value="line">Straight line</option><option value="polyline">Polyline</option></select></label><button id="sample-stroke" type="button">Pick color from raw image</button><output id="stroke-color">No color selected</output><label>Color tolerance <input id="stroke-tolerance" type="number" min="0" max="40" value="12"></label><label>Search margin (px) <input id="search-margin" type="number" min="0" max="1920" value="64"></label><button id="add-connection" type="button">Add connection</button></div><ul id="connections"></ul>
<p>Attach connections only to the objects they visibly connect in the video. For a polyline, pick the color by clicking a pixel of the line. Lines that branch in the same color or are not visible may remain unmeasurable; this form is not a success confirmation.</p>
<div class="bar"><button id="clear" type="button">Clear selections</button><button id="download" type="button">Download selection.json</button></div>
<p>Give the downloaded file to the KiroClops “resume selection” command. You do not need to write any JSON. This selection does not mean that the transfer or the experiment succeeded. The page runs entirely locally; it makes no network requests and uploads no video.</p></main>
<script>
'use strict';const data=__INTAKE_DATA__;
const canvas=document.querySelector('#canvas'),ctx=canvas.getContext('2d'),image=new Image();canvas.width=data.image_size[0];canvas.height=data.image_size[1];
const slider=document.querySelector('#timeline'),mode=document.querySelector('#mode'),message=document.querySelector('#message');let frame=0,start=null,box=null,anchor=null,geometryBox=null,draft=null,objects=[],backgrounds=[],connections=[],selectionExtras={},strokeRgb=null,strokeSample=null;
const clone=value=>JSON.parse(JSON.stringify(value));const lastVisible=document.querySelector('#last-visible');lastVisible.max=data.frames.length-1;lastVisible.value=data.frames.length-1;
document.querySelector('#goal').textContent=data.request;document.querySelector('#provenance').textContent=data.input_provenance==='recorder_verified'?'Input times come from the recording package. This new video has not yet been validated as a comparative experiment.':'Input times '+(data.input_provenance==='unknown'?'are unknown':'are user-declared')+'. Motion in the video can be measured; invisible key presses or interaction rules will not be inferred.';
slider.max=data.frames.length-1;function tell(text){message.textContent=text;}function render(){ctx.clearRect(0,0,canvas.width,canvas.height);if(image.complete&&image.naturalWidth)ctx.drawImage(image,0,0);ctx.lineWidth=2;for(const o of objects.filter(o=>o.frame_index===frame)){ctx.strokeStyle='#80ffba';ctx.strokeRect(...o.roi);if(o.geometry){ctx.strokeStyle='#edb3ff';ctx.strokeRect(...o.geometry.roi);}}if(frame===0){ctx.strokeStyle='#8cc4ff';for(const r of backgrounds)ctx.strokeRect(...r);}if(geometryBox){ctx.strokeStyle='#edb3ff';ctx.strokeRect(...geometryBox);}if(draft){ctx.strokeStyle='#ffffff';ctx.strokeRect(...draft);}if(box){ctx.strokeStyle='#ffdc79';ctx.strokeRect(...box);if(anchor){ctx.beginPath();ctx.arc(box[0]+anchor[0],box[1]+anchor[1],4,0,Math.PI*2);ctx.stroke();}}}
function load(){frame=Number(slider.value);box=null;anchor=null;geometryBox=null;draft=null;start=null;document.querySelector('#frame-number').value=frame;document.querySelector('#time').textContent=data.frames[frame].time_s.toFixed(3)+' s';image.src=data.frames[frame].path;}image.onload=render;image.onerror=()=>tell('Local PNG not found. Keep annotation.html together with the package folder.');slider.addEventListener('input',load);
function point(e){const r=canvas.getBoundingClientRect();return [Math.max(0,Math.min(canvas.width-1,Math.round((e.clientX-r.left)*canvas.width/r.width))),Math.max(0,Math.min(canvas.height-1,Math.round((e.clientY-r.top)*canvas.height/r.height)))];}
canvas.addEventListener('pointerdown',e=>{if(!image.complete||!image.naturalWidth)return tell('Wait for the raw frame to load.');const p=point(e);if(mode.value==='stroke'){const raw=document.createElement('canvas');raw.width=canvas.width;raw.height=canvas.height;const rawCtx=raw.getContext('2d');rawCtx.drawImage(image,0,0);strokeRgb=Array.from(rawCtx.getImageData(p[0],p[1],1,1).data).slice(0,3);strokeSample={frame_index:frame,pixel:p,frame_sha256:data.frames[frame].sha256};document.querySelector('#stroke-color').textContent='RGB '+strokeRgb.join(', ');tell('The color was taken from the raw frame, without the marker drawings.');return;}if(mode.value==='anchor'){if(!box)return tell('Draw a body frame first.');if(p[0]<box[0]||p[1]<box[1]||p[0]>box[0]+box[2]||p[1]>box[1]+box[3])return tell('The tracking point must be inside the body.');anchor=[p[0]-box[0],p[1]-box[1]];render();return;}if(mode.value==='background'&&frame!==0)return tell('Select the fixed background in the first frame.');start=p;draft=null;canvas.setPointerCapture(e.pointerId);});
canvas.addEventListener('pointermove',e=>{if(!start)return;const p=point(e);draft=[Math.min(start[0],p[0]),Math.min(start[1],p[1]),Math.abs(p[0]-start[0]),Math.abs(p[1]-start[1])];render();});
canvas.addEventListener('pointerup',()=>{if(!start)return;start=null;const chosen=draft;draft=null;if(!chosen||chosen[2]<3||chosen[3]<3){render();return tell('Select an area of at least 3×3 pixels.');}if(mode.value==='background'){backgrounds.push(chosen);tell(backgrounds.length+' background regions selected.');}else if(mode.value==='geometry'){geometryBox=chosen;tell('Outer boundary selected; the tracking mark box is kept separately.');}else{box=chosen;anchor=[box[2]/2,box[3]/2];document.querySelector('#body-height').value=box[3];tell('Check the tracking point, the outer boundary and the visible frame range, then add the object.');}render();});
canvas.addEventListener('pointercancel',()=>{start=null;draft=null;render();});function list(){const ul=document.querySelector('#objects');ul.replaceChildren();for(const o of objects){const li=document.createElement('li');li.textContent=o.id+' · frame '+o.frame_index+' · body '+o.body_height_px+' px'+(o.geometry?' · outer boundary selected':'');ul.append(li);}for(const key of ['#connection-from','#connection-to']){const select=document.querySelector(key),previous=select.value;select.replaceChildren();for(const o of objects){const option=document.createElement('option');option.value=o.id;option.textContent=o.id;select.append(option);}if(objects.some(o=>o.id===previous))select.value=previous;}const edges=document.querySelector('#connections');edges.replaceChildren();for(const edge of connections){const li=document.createElement('li');li.textContent=edge.id+' · '+edge.from_object+' → '+edge.to_object+' · '+(edge.method||'line');const remove=document.createElement('button');remove.type='button';remove.textContent='Remove';remove.onclick=()=>{connections=connections.filter(e=>e.id!==edge.id);list();};li.append(remove);edges.append(li);}}
document.querySelector('#add').onclick=()=>{const id=document.querySelector('#object-id').value.trim(),height=Number(document.querySelector('#body-height').value),end=Number(lastVisible.value);if(!box||!anchor)return tell('Select the body frame and the tracking point first.');if(!/^[A-Za-z0-9_-]{1,64}$/.test(id)||objects.some(o=>o.id===id))return tell('Use a unique object ID.');if(!Number.isFinite(height)||height<=0||height>canvas.height)return tell('Enter a valid body height.');if(!Number.isInteger(end)||end<=frame||end>=data.frames.length)return tell('The last visible frame must be after the start and within the clip.');if(objects.length>=32)return tell('At most 32 objects are supported.');const object={id,roi:box,anchor,body_height_px:height,body_height_source:'user_selection',frame_index:frame,template_frame_sha256:data.frames[frame].sha256,method:'template',active_intervals_s:[[data.frames[frame].time_s,data.frames[end].time_s]]};if(geometryBox)object.geometry={method:'contour',segmentation:'reference_fill',roi:geometryBox,min_area_ratio:.7};objects.push(object);box=null;anchor=null;geometryBox=null;document.querySelector('#object-id').value='object-'+String(objects.length+1).padStart(2,'0');lastVisible.value=data.frames.length-1;list();render();tell('Object added. Select another object if needed.');};
document.querySelector('#sample-stroke').onclick=()=>{mode.value='stroke';tell('Click a visible line pixel of the connection in the raw image.');};
document.querySelector('#add-connection').onclick=()=>{const a=document.querySelector('#connection-from').value,b=document.querySelector('#connection-to').value,method=document.querySelector('#connection-method').value;if(!a||!b||a===b||connections.some(e=>[e.from_object,e.to_object].includes(a)&&[e.from_object,e.to_object].includes(b)))return tell('Select a connection between two different objects that has not been added yet.');const edge={id:'edge-'+a+'-'+b,from_object:a,to_object:b,method};if(method==='polyline'){const tolerance=Number(document.querySelector('#stroke-tolerance').value),margin=Number(document.querySelector('#search-margin').value);if(!strokeRgb||!strokeSample)return tell('Pick the line color from the raw frame first.');if(!Number.isFinite(tolerance)||tolerance<0||tolerance>40||!Number.isFinite(margin)||margin<0||margin>1920)return tell('The color tolerance or search margin is invalid.');Object.assign(edge,{stroke_rgb:clone(strokeRgb),stroke_sample:clone(strokeSample),stroke_tolerance:tolerance,search_margin_px:margin});}connections.push(edge);list();tell('Visible connection saved.');};
window.kiroclopsSelection=()=>clone({...selectionExtras,schema_version:1,kind:'ObjectSelection',package_sha256:data.package_sha256,objects,background_rois:backgrounds,connections,selection_source:'user_confirmed_local_annotation'});
window.kiroclopsCurrentFrame=()=>image.complete&&image.naturalWidth&&image.currentSrc===new URL(data.frames[frame].path,document.baseURI).href?({index:frame,time_s:data.frames[frame].time_s,image_sha256:data.frames[frame].sha256}):null;
window.kiroclopsSetSelection=selection=>{if(!selection||typeof selection!=='object'||(selection.package_sha256&&selection.package_sha256!==data.package_sha256))throw new Error('Selection belongs to another video package.');selectionExtras=clone(selection);objects=clone(selection.objects||[]);backgrounds=clone(selection.background_rois||[]);connections=clone(selection.connections||[]);box=null;anchor=null;geometryBox=null;draft=null;list();render();return window.kiroclopsSelection();};
document.querySelector('#clear').onclick=()=>{objects=[];backgrounds=[];connections=[];selectionExtras={};box=null;anchor=null;geometryBox=null;strokeRgb=null;strokeSample=null;document.querySelector('#stroke-color').textContent='No color selected';list();render();tell('Selections cleared.');};document.querySelector('#download').onclick=()=>{if(!objects.length)return tell('Select at least one object.');const selection=window.kiroclopsSelection();const url=URL.createObjectURL(new Blob([JSON.stringify(selection,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='selection.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);tell('selection.json downloaded. Continue the selection with KiroClops.');};load();
</script></html>'''
