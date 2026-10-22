"""Loopback multi-clip intake UI; pixels and selections only, no model execution."""
from __future__ import annotations

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path
from . import intake, suite_intake
from .intake_ui_server import declared_form_inputs
from .session import _ordinary, _lock, source_hashes

MAX_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_BEHAVIORS = {'game': ['short_jump', 'long_jump'],
    'web_panel': ['open', 'close', 'interrupt', 'reopen'], 'svg': ['add_node', 'remove_node', 'drag']}


def _need(value, code, message):
    if not value:
        raise VisualError(code, message)


class SuiteIntakeWorkflow:
    """Keep each annotation/input declaration in a new immutable intake attempt."""
    def __init__(self, intake_directory, draft_directory=None):
        self.initial = _ordinary(intake_directory)
        self.output = _ordinary(self.initial.parent / (self.initial.name + '-multi-reference-ui'))
        self.output.mkdir(exist_ok=True)
        self.draft_directory = _ordinary(draft_directory or self.initial.parent / (self.initial.name + '-reference-suite'))
        _need(not self.draft_directory.is_relative_to(self.initial) and not self.initial.is_relative_to(self.draft_directory),
              'unsafe_path', 'The reference draft and the first video intake must be in separate folders.')
        self.lock = threading.RLock()
        self.accepting = True
        self.clips = {}
        self.current = self._register(self.initial)
        if self.draft_directory.exists():
            suite_intake.coverage(self.draft_directory)

    def _register(self, directory):
        directory = _ordinary(directory)
        state = read_json(directory / 'intake.json')
        _need(state.get('kind') == 'LocalVideoIntake', 'invalid_intake', 'Prepare the local video intake first.')
        manifest = load_package(directory / 'package')
        self._verify_intake(directory, state, manifest)
        identifier = 'clip-' + secrets.token_hex(12)
        self.clips[identifier] = {'directory': directory, 'state': state, 'manifest': manifest,
            'manifest_sha256': file_sha256(directory / 'package/manifest.json')}
        return identifier

    def _verify_intake(self, directory, state, manifest):
        binding = read_json(directory / 'intake-binding.json')
        _need(read_json(directory / 'intake.json') == state
              and state['package_sha256'] == manifest['package_sha256'] == binding.get('package_sha256')
              and digest(state['request']) == binding.get('request_sha256')
              and digest(state['project']) == binding.get('project_info_sha256')
              and digest(state['project']['project']) == binding.get('project_path_sha256'),
              'intake_binding_changed', 'The video, request or target binding has changed.')
        _need(digest(source_hashes(state['project']['project'])) == binding.get('project_sha256'),
              'source_changed', 'The target source changed after the intake was prepared.')
        _need(state.get('model_started') is False, 'model_already_started', 'An intake that has been used for a model cannot be added to this preparation.')

    def _current(self):
        value = self.clips[self.current]
        self._verify_intake(value['directory'], value['state'], value['manifest'])
        self.package(self.current)
        return value

    def package(self, identifier):
        _need(identifier in self.clips, 'not_found', 'A clip ID belonging to this UI is required.')
        value = self.clips[identifier]
        _need(file_sha256(value['directory'] / 'package/manifest.json') == value['manifest_sha256'],
              'source_changed', 'The video manifest has changed.')
        return value['manifest']

    def metadata(self):
        with self.lock:
            value = self._current(); state, manifest = value['state'], value['manifest']
            domain = state['project']['domain']
            coverage = suite_intake.coverage(self.draft_directory) if self.draft_directory.exists() else None
            required = coverage['required_behaviors'] if coverage else DEFAULT_BEHAVIORS[domain]
            return {'clip_id': self.current, 'domain': domain, 'request': state['request'],
                'target': state['project']['project'], 'inputs': manifest['inputs'],
                'selection': read_json(value['directory'] / 'selection.json') if state.get('selection') else None,
                'selection_status': state['status'], 'required_behaviors': required,
                'supported_behaviors': list(suite_intake.SUPPORTED_BEHAVIORS[domain]),
                'target_ids': ['character'] if domain == 'game' else ['handle', 'bookmark', 'star'] if domain == 'web_panel' else list('abcdef'),
                'coverage': coverage, 'draft_created': coverage is not None,
                'frozen': (self.draft_directory / 'suite.json').exists(), 'execution_ready': False,
                'model_started': False, 'behavior_accepted': False,
                'scope': 'Reference preparation. No model is started; behavior acceptance and validation with new inputs are separate stages.'}

    def new_clip(self, form):
        _need(isinstance(form, dict) and not set(form) - {'video', 'target', 'request'},
              'invalid_request', 'A video path, a target and a natural-language request are required.')
        _need(all(isinstance(form.get(k), str) and form[k].strip() for k in ('video', 'target', 'request')),
              'invalid_request', 'A video path, a target and a natural-language request are required.')
        with self.lock:
            self._editable()
            if self.draft_directory.exists():
                original = read_json(self.draft_directory / 'draft.json')
                _need(form['request'].strip() == original['goal'], 'goal_changed', 'The additional recording must belong to the same natural-language request.')
            destination = self.output / ('prepared-' + secrets.token_hex(12))
            intake.prepare_intake(form['video'], form['target'], form['request'], destination)
            identifier = self._register(destination)
            if self.draft_directory.exists():
                _need(self.clips[identifier]['state']['project']['domain'] == original['domain'],
                      'domain_mismatch', 'The additional recording must belong to the same target domain.')
            self.current = identifier
            return self.metadata()

    def _editable(self):
        _need(self.accepting, 'ui_closed', 'The UI is not accepting a new operation.')
        if self.draft_directory.exists():
            suite_intake._can_edit(self.draft_directory)

    def attach(self, form):
        allowed = {'selection', 'object_map', 'inputs', 'scenario_id', 'behaviors', 'required_behaviors', 'role'}
        _need(isinstance(form, dict) and not set(form) - allowed, 'invalid_request', 'Unexpected form field.')
        with self.lock:
            self._editable(); value = self._current(); original = value['manifest']; state = value['state']
            selection = form.get('selection')
            _need(isinstance(selection, dict) and selection.get('package_sha256') == original['package_sha256'],
                  'selection_binding_mismatch', 'The selection must belong to the video currently shown.')
            for obj in selection.get('objects', []):
                index = obj.get('frame_index')
                _need(type(index) is int and 0 <= index < len(original['frames'])
                      and obj.get('template_frame_sha256') == original['frames'][index]['sha256'],
                      'selection_binding_mismatch', 'The selected raw image has changed.')
            inputs = declared_form_inputs(form['inputs']) if form.get('inputs') is not None else None
            if self.draft_directory.exists():
                registered = read_json(self.draft_directory / 'draft.json')
                _need(form.get('required_behaviors', registered['required_behaviors']) == registered['required_behaviors'],
                      'behavior_changed', 'Required behaviors cannot be changed after the first clip.')
            else:
                suite_intake.draft(self.draft_directory, state['project']['domain'], state['request'],
                    form.get('required_behaviors', DEFAULT_BEHAVIORS[state['project']['domain']]))
            # Original intake and earlier selections stay intact. Input overrides
            # copy the same images to a new user_declared timeline and fresh hash.
            destination = self.output / ('selected-' + secrets.token_hex(12))
            created = intake.prepare_intake(value['directory'] / 'package', state['project']['project'],
                state['request'], destination, inputs=inputs)
            chosen = deepcopy(selection); chosen['package_sha256'] = created['package_sha256']
            intake.resume_selection(destination, chosen)
            result = suite_intake.add_clip(self.draft_directory, destination, form.get('scenario_id'),
                form.get('behaviors'), form.get('object_map'), role=form.get('role', 'calibration'))
            return {**result, 'metadata': self.metadata(), 'model_started': False, 'execution_ready': False}

    def freeze(self):
        with self.lock:
            _need(self.accepting, 'ui_closed', 'The UI is closed.')
            if not self.draft_directory.exists():
                meta = self.metadata()
                return {'status': 'needs_reference_clips', 'frozen': False, 'execution_ready': False,
                        'requests': [suite_intake.MISSING_REQUESTS[b] for b in meta['required_behaviors']]}
            return suite_intake.freeze(self.draft_directory)

    def annotation_html(self):
        with self.lock:
            value = self._current(); manifest = value['manifest']
            data = {'package_sha256': manifest['package_sha256'], 'image_size': manifest['image_size'],
                'frames': [{'path': f'frame/{self.current}/{i}', 'sha256': f['sha256'], 'time_s': f['time_s']}
                           for i, f in enumerate(manifest['frames'])],
                'request': value['state']['request'], 'proposals': [], 'input_provenance': manifest['inputs']['provenance']}
            encoded = json.dumps(data, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
            page = intake._ANNOTATION_HTML.replace('__INTAKE_DATA__', encoded)
            page = page.replace('</html>', '<script>document.querySelector("#download").hidden=true;</script></html>')
            return page.encode('utf-8')

    def frame(self, identifier, index):
        with self.lock:
            manifest = self.package(identifier)
            _need(index.isascii() and index.isdecimal() and 0 <= int(index) < len(manifest['frames']),
                  'not_found', 'The frame does not belong to this video.')
            row = manifest['frames'][int(index)]
            source = _ordinary(safe_path(self.clips[identifier]['directory'] / 'package', row['path']))
            _need(source.suffix == '.png', 'not_found', 'Only bound PNG frames are shown.')
            body = source.read_bytes()
            _need(hashlib.sha256(body).hexdigest() == row['sha256'], 'source_changed', 'The raw video frame has changed.')
            return body


def make_server(intake_directory, draft_directory=None):
    directory = _ordinary(intake_directory)
    output = _ordinary(directory.parent / (directory.name + '-multi-reference-ui')); output.mkdir(exist_ok=True)
    lease = _lock(output / '.ui-server.lock', timeout=.05)
    try:
        lease.__enter__()
    except TimeoutError as exc:
        raise VisualError('ui_already_running', 'The reference UI is already open for this intake.') from exc
    try:
        workflow = SuiteIntakeWorkflow(directory, draft_directory)
    except BaseException:
        lease.__exit__(None, None, None); raise
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(10)

        def log_message(self, *args): pass

        def send_body(self, code, value, content_type='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(code); self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'self'")
            self.end_headers(); self.wfile.write(body)

        def route(self, *, post=False):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            _need(self.headers.get('Host') == origin[7:] and self.headers.get('Origin', origin if not post else '') == origin
                  and self.headers.get('Sec-Fetch-Site', 'same-origin') in ('same-origin', 'none'),
                  'invalid_origin', 'Only this local UI can be used.')
            prefix = '/' + token + '/'; path = urlsplit(self.path).path
            _need(path.startswith(prefix), 'invalid_origin', 'The local UI key is invalid.')
            return path[len(prefix):]

        def do_GET(self):
            try:
                path = self.route()
                if path in ('', 'index.html'): return self.send_body(200, _PAGE.encode('utf-8'), 'text/html; charset=utf-8')
                if path == 'metadata': return self.send_body(200, workflow.metadata())
                if path == 'annotation.html': return self.send_body(200, workflow.annotation_html(), 'text/html; charset=utf-8')
                if path.startswith('frame/'):
                    parts = path.split('/')
                    if len(parts) == 3: return self.send_body(200, workflow.frame(parts[1], parts[2]), 'image/png')
                return self.send_body(404, {'code': 'not_found'})
            except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
                self.send_body(400, {'code': getattr(exc, 'code', 'invalid_request'), 'message': str(exc)})

        def do_POST(self):
            try:
                path = self.route(post=True)
                _need(path in ('newclip', 'attach', 'freeze') and self.headers.get('Content-Type') == 'application/json'
                      and self.headers.get('Transfer-Encoding') is None, 'invalid_request', 'A valid form submission is required.')
                size = int(self.headers.get('Content-Length', '0'))
                _need(0 < size <= MAX_REQUEST_BYTES, 'invalid_request', 'The form size is invalid.')
                raw = self.rfile.read(size)
                _need(len(raw) == size, 'invalid_request', 'The form was sent incompletely.')
                form = json.loads(raw)
                if path == 'freeze':
                    _need(form == {}, 'invalid_request', 'Sealing cannot include additional operations.')
                    result = workflow.freeze()
                else: result = workflow.new_clip(form) if path == 'newclip' else workflow.attach(form)
                self.send_body(200, result)
            except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
                self.send_body(400, {'code': getattr(exc, 'code', 'invalid_request'), 'message': str(exc)})

    class Server(ThreadingHTTPServer):
        released = False
        def server_close(self):
            super().server_close()
            if not self.released:
                self.released = True
                with workflow.lock:
                    workflow.accepting = False
                    lease.__exit__(None, None, None)

    try: server = Server(('127.0.0.1', 0), Handler)
    except BaseException:
        lease.__exit__(None, None, None); raise
    server.daemon_threads = True; server.workflow = workflow
    server.url = f'http://127.0.0.1:{server.server_port}/{token}/index.html'
    return server


def serve(intake_directory, draft_directory=None):
    server = make_server(intake_directory, draft_directory)
    print(json.dumps({'status': 'ready', 'url': server.url, 'model_started': False, 'execution_ready': False}), flush=True)
    try: server.serve_forever()
    finally: server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--intake', required=True, type=Path)
    parser.add_argument('--draft', type=Path)
    args = parser.parse_args(argv); serve(args.intake, args.draft)


_PAGE = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KiroClops · Reference package</title>
<style>body{font:16px system-ui;background:#101827;color:#e7eefb;margin:0}main{max-width:1200px;margin:auto;padding:24px}input,select,button,textarea{font:inherit;color:inherit;background:#1b2c45;border:1px solid #61728d;border-radius:6px;padding:8px}button{cursor:pointer}input[type=text],textarea{width:100%;box-sizing:border-box}label{display:block;margin:12px 0}.row{display:flex;gap:12px;flex-wrap:wrap}.checks label{display:inline-flex;gap:8px;margin-right:18px}iframe{width:100%;height:1060px;border:1px solid #61728d}p{line-height:1.5}#status{color:#ffdb8b;white-space:pre-wrap}input:focus,select:focus,button:focus,textarea:focus{outline:2px solid #a8cfff}</style>
<main><h1>KiroClops · Multiple reference videos</h1><p>Specify the target and your request, select the objects in the video and mark the behaviors this clip shows. For missing behaviors, upload an additional video from the same screen.</p>
<label>Natural-language request<textarea id="request" rows="2"></textarea></label><label>Target project path<input id="target" type="text"></label><label>Local video or PNG package path<input id="video" type="text" placeholder="D:\reference\movement.mp4"></label><button id="newclip">Prepare this video</button>
<h2>Required behaviors</h2><div class="checks" id="required"></div><p id="coverage" role="status"></p><ul id="requests"></ul>
<iframe id="annotation" title="Object selection on the video" src="annotation.html"></iframe>
<h2>Add this clip to the package</h2><label>Scenario name<input id="scenario" value="reference-01" maxlength="80"></label><div class="checks" id="behaviors"></div><label>Clip role<select id="role"><option value="calibration">Calibration example</option><option value="validation">Separate validation example</option></select></label><p>A different recording is required for separate validation. The behavior checkbox is your declaration about the behavior shown in the video.</p>
<button id="mappings">Show object mappings</button><div id="map-list"></div><div id="inputs"></div><p><button id="attach">Save the selection and add the clip</button> <button id="freeze">Seal the reference package</button></p><p id="status" role="status" aria-live="polite"></p></main>
<script>(async()=>{'use strict';let meta,entries=[];const $=s=>document.querySelector(s),status=$('#status'),frame=$('#annotation');
const names={short_jump:'Short jump',long_jump:'Long jump',horizontal_movement:'Horizontal movement',preserved_jump:'Preserved jump',open:'Open',close:'Close',interrupt:'Early interruption',reopen:'Reopen',drag:'Drag',add_node:'Add node',remove_node:'Remove node',select_node:'Select node'};
const api=async(path,body)=>{const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const value=await r.json();if(!r.ok)throw Error(value.message||value.code);return value};
function checks(id,selected){const box=$(id);box.replaceChildren();for(const key of meta.supported_behaviors){const l=document.createElement('label'),i=document.createElement('input');i.type='checkbox';i.value=key;i.checked=selected.includes(key);if(id==='#required')i.disabled=meta.draft_created;l.append(i,document.createTextNode(names[key]||key));box.append(l)}}
const chosen=id=>Array.from($(id).querySelectorAll('input:checked'),i=>i.value);
function selection(){const fn=frame.contentWindow?.kiroclopsSelection;if(!fn)throw Error('Wait while the video selection is loading.');return fn()}
function mappings(){const box=$('#map-list'),prior={};for(const s of box.querySelectorAll('select'))prior[s.dataset.reference]=s.value;box.replaceChildren();for(const obj of selection().objects){const l=document.createElement('label'),s=document.createElement('select');l.textContent=obj.id+' → ';s.dataset.reference=obj.id;s.add(new Option('Select target',''));for(const id of meta.target_ids)s.add(new Option(id,id));s.value=prior[obj.id]||(meta.target_ids.length===1?meta.target_ids[0]:'');l.append(s);box.append(l)}}
$('#mappings').onclick=()=>{try{mappings()}catch(e){status.textContent=e.message}};
function inputForm(){entries=[];const box=$('#inputs');box.replaceChildren();if(meta.inputs.provenance!=='unknown'){const p=document.createElement('p');p.textContent=meta.inputs.provenance==='recorder_verified'?'Input times come from the recording package.':'Input times are bound as a user declaration.';box.append(p);return}const p=document.createElement('p');p.textContent='Input times are unknown. Add the key or interaction times you know; they are recorded as a user declaration.';const list=document.createElement('div'),add=document.createElement('button');add.textContent='Add input time';box.append(p,list,add);add.onclick=()=>{const row=document.createElement('div');row.className='row';const time=document.createElement('input'),action=document.createElement('select'),target=document.createElement('select'),x=document.createElement('input'),y=document.createElement('input'),use=document.createElement('button'),remove=document.createElement('button');time.type='number';time.min='0';time.step='.001';time.setAttribute('aria-label','Input time in seconds');time.placeholder='Seconds';for(const i of [x,y]){i.type='number';i.min='0';i.max='1';i.step='.001'}x.setAttribute('aria-label','Target or progress fraction');y.setAttribute('aria-label','Vertical target fraction');const opts=meta.domain==='game'?[['jump:press','Press jump'],['jump:release','Release jump'],['left:press','Press left'],['left:release','Release left'],['right:press','Press right'],['right:release','Release right']]:[['click','Click'],['drag_start','Start drag'],['drag_move','Drag step'],['drag_end','Release pointer']];for(const [v,n] of opts)action.add(new Option(n,v));function fields(){target.replaceChildren();const nodes=meta.target_ids.map(n=>['[data-node-id="'+n+'"]','Node '+n]);const values=meta.domain==='svg'?(action.value==='drag_start'?nodes:[['#add-node','Add node'],['#remove-node','Remove node'],...nodes]):(action.value==='drag_start'?[['#panel-handle','Panel handle']]:[['#open-panel','Open panel'],['#close-panel','Close panel']]);for(const [v,n] of values)target.add(new Option(n,v));target.hidden=meta.domain==='game'||!['click','drag_start'].includes(action.value);x.hidden=!['drag_start','drag_move'].includes(action.value);y.hidden=action.value!=='drag_start'||meta.domain!=='svg';x.placeholder=action.value==='drag_move'?'Progress 0–1':'Target fraction 0–1';y.placeholder='Vertical 0–1'}action.onchange=fields;fields();use.textContent='Time of this frame';use.onclick=()=>{const f=frame.contentWindow?.kiroclopsCurrentFrame?.();if(!f){status.textContent='Wait for the raw frame to load.';return}time.value=f.time_s};remove.textContent='Remove';remove.onclick=()=>row.remove();row.append(time,use,action,target,x,y,remove);list.append(row);entries.push({row,time,action,target,x,y})}}
function formInputs(){if(meta.inputs.provenance!=='unknown')return null;const number=i=>{if(i.value==='')throw Error('Specify the input time and the required fractions.');const v=Number(i.value);if(!Number.isFinite(v))throw Error('Enter a valid number.');return v};return entries.filter(e=>e.row.isConnected).map(({time,action,target,x,y})=>{const e={time_s:number(time)};if(meta.domain==='game'){const [name,state]=action.value.split(':');Object.assign(e,{action:name,pressed:state==='press'})}else{e.action=action.value;if(['click','drag_start'].includes(e.action))e.selector=target.value;if(e.action==='drag_start')e.destination=meta.domain==='svg'?{viewport_x:number(x),viewport_y:number(y)}:{travel_fraction:number(x)};if(e.action==='drag_move')e.fraction=number(x)}return e})}
function show(value,reload=false){meta=value;$('#request').value=meta.request;$('#target').value=meta.target;$('#request').readOnly=meta.draft_created;$('#target').readOnly=meta.draft_created;checks('#required',meta.required_behaviors);checks('#behaviors',[]);const c=meta.coverage;$('#coverage').textContent=c?(c.status==='coverage_ready'?'References for the declared behaviors have been added.':c.clips.length+' clips added; missing behaviors are listed below.'):'Create the reference package by adding the first clip.';const requests=$('#requests');requests.replaceChildren();for(const text of c?.requests||[]){const li=document.createElement('li');li.textContent=text;requests.append(li)}inputForm();$('#attach').disabled=meta.frozen;$('#newclip').disabled=meta.frozen;$('#freeze').disabled=meta.frozen;if(reload)frame.src='annotation.html?clip='+encodeURIComponent(meta.clip_id);status.textContent=meta.scope}
frame.onload=()=>{try{if(meta?.selection)frame.contentWindow.kiroclopsSetSelection(meta.selection);if(meta?.selection)mappings()}catch(e){status.textContent=e.message}};
$('#newclip').onclick=async()=>{try{status.textContent='Preparing the additional video…';const value=await api('newclip',{video:$('#video').value,target:$('#target').value,request:$('#request').value});$('#map-list').replaceChildren();show(value,true)}catch(e){status.textContent=e.message}};
$('#attach').onclick=async()=>{try{const selected=selection();if(!selected.objects.length)throw Error('Select at least one object.');mappings();const map={};for(const s of $('#map-list').querySelectorAll('select')){if(!s.value)throw Error('Select a target for '+s.dataset.reference+'.');map[s.dataset.reference]=s.value}status.textContent='Saving the selection and input times…';const result=await api('attach',{selection:selected,object_map:map,inputs:formInputs(),scenario_id:$('#scenario').value,behaviors:chosen('#behaviors'),required_behaviors:chosen('#required'),role:$('#role').value});show(result.metadata);status.textContent=result.status==='clip_added'?'Clip added. For missing behaviors, prepare an additional video above.':result.request;$('#scenario').value='reference-'+String((meta.coverage?.clips.length||0)+1).padStart(2,'0')}catch(e){status.textContent=e.message}};
$('#freeze').onclick=async()=>{try{const result=await api('freeze',{});if(result.status==='suite_ready_intake'){show(await api('metadata'));status.textContent='Reference package sealed. No model was started; it is not ready for execution. Behavior acceptance and separate validation have not been done yet.'}else status.textContent=(result.requests||[]).join('\n')}catch(e){status.textContent=e.message}};
show(await api('metadata'));if(frame.contentDocument?.readyState==='complete')frame.onload();
})().catch(e=>document.querySelector('#status').textContent=e.message);</script></html>'''


if __name__ == '__main__': main()
