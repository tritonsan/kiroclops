"""Loopback-only continuation of local annotation, binding and Kiro CLI work.

The browser never receives a filesystem write primitive. Each submitted form
creates a fresh attempt, preserving earlier selections and failed sessions.
This is a local engineering UI, not the future hosted product service.
"""
from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from kiroclops.visual.schema import VisualError, read_json, safe_path, file_sha256, digest
from kiroclops.visual.package import load_package
from .session import _ordinary, source_hashes
from .backend import _atomic as write_json


MAX_REQUEST_BYTES = 2 * 1024 * 1024


def declared_form_inputs(events):
    """A form declaration can never become a recorder-verified input."""
    from kiroclops.visual.schema import validate_inputs
    if not isinstance(events, list) or not events:
        raise VisualError('needs_input_binding', 'Specify the input times and actions.')
    allowed = {'time_s', 'action', 'selector', 'pressed', 'destination', 'fraction'}
    if any(not isinstance(e, dict) or set(e) - allowed for e in events):
        raise VisualError('invalid_input_timeline', 'Unexpected input field.')
    return validate_inputs({'kind': 'InputTimeline', 'schema_version': 1,
        'provenance': 'user_declared', 'events': [dict(e, source='explicit_local_form_declaration') for e in events]})


class IntakeWorkflow:
    def __init__(self, directory):
        self.directory = _ordinary(directory)
        self.intake = read_json(self.directory / 'intake.json')
        if self.intake.get('kind') != 'LocalVideoIntake':
            raise VisualError('invalid_intake', 'Prepare the local video intake first.')
        self.manifest = load_package(self.directory / 'package')
        self.manifest_sha = file_sha256(self.directory / 'package/manifest.json')
        self.frame_files = {f['path']: f['sha256'] for f in self.manifest['frames']}
        self._verify_intake()
        self.output = self.directory / 'workflow'
        self.output.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.accepting = True
        self.finished = threading.Event()
        self.finished.set()
        self.current = None
        self.report_views = {}
        # An interrupted server cannot turn an unfinished submission into a pass.
        for path in sorted(self.output.glob('attempt-*/status.json'), key=lambda p: p.stat().st_mtime):
            state = read_json(path)
            self.current = state['job_id']
            if state.get('status') in ('queued', 'running'):
                state.update(status='interrupted', error={'code': 'ui_worker_interrupted',
                    'message': 'The local UI worker was interrupted; the incomplete result was not used.'})
                write_json(path, state)

    def _verify_intake(self):
        binding = read_json(self.directory / 'intake-binding.json')
        if (read_json(self.directory / 'intake.json') != self.intake
                or digest(self.intake['request']) != binding.get('request_sha256')
                or digest(self.intake['project']) != binding.get('project_info_sha256')
                or digest(self.intake['project']['project']) != binding.get('project_path_sha256')
                or self.intake['package_sha256'] != binding.get('package_sha256')
                or self.manifest['package_sha256'] != binding.get('package_sha256')):
            raise VisualError('intake_binding_changed', 'The video, request or target binding has changed.')
        if digest(source_hashes(self.intake['project']['project'])) != binding.get('project_sha256'):
            raise VisualError('source_changed', 'The target source changed after the intake was prepared.')

    def package(self):
        if file_sha256(self.directory / 'package/manifest.json') != self.manifest_sha:
            raise VisualError('source_changed', 'The video manifest has changed.')
        return self.manifest

    def metadata(self):
        manifest = self.package()
        domain = self.intake['project']['domain']
        targets = ['character'] if domain == 'game' else ['handle', 'bookmark', 'star'] if domain == 'web_panel' else list('abcdef')
        selection = read_json(self.directory / 'selection.json') if self.intake.get('selection') else None
        last_form = read_json(self.output / self.current / 'request.json') if self.current and (self.output / self.current / 'request.json').is_file() else None
        from .launcher import MODEL, EFFORT, APPROVAL
        return {'domain': domain, 'target_ids': targets, 'inputs': manifest['inputs'],
            'selection': selection, 'current_job': self.current, 'last_form': last_form,
            'model': {'id': MODEL, 'effort': EFFORT, 'approval': APPROVAL},
            'scope': 'Single video; no held-out validation. The result does not count as comparative experiment success.'}

    def submit(self, form):
        if not isinstance(form, dict) or set(form) - {'selection', 'object_map', 'inputs', 'initial_nodes', 'selected_node', 'game_stage', 'launch'}:
            raise VisualError('invalid_request', 'Unexpected form field.')
        if type(form.get('launch', False)) is not bool:
            raise VisualError('invalid_request', 'The Kiro launch choice must be a boolean.')
        with self.lock:
            if not self.accepting:
                raise VisualError('ui_closed', 'The UI is not accepting a new operation.')
            if self.current and self.status(self.current)['status'] in ('queued', 'running'):
                raise VisualError('job_running', 'The current operation has not completed yet.')
            identifier = 'attempt-' + secrets.token_hex(12)
            directory = self.output / identifier
            directory.mkdir()
            write_json(directory / 'request.json', form)
            write_json(directory / 'status.json', {'job_id': identifier, 'status': 'queued',
                'created_at': time.time(), 'model_requested': form.get('launch', False), 'benchmark_trial': False})
            self.current = identifier
            self.finished.clear()
            def execute():
                try:
                    self._execute(identifier, deepcopy(form))
                finally:
                    self.finished.set()
            threading.Thread(target=execute, daemon=True).start()
            return {'job_id': identifier, 'status': 'queued'}

    def status(self, identifier):
        if not isinstance(identifier, str) or not identifier.startswith('attempt-') or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier[8:]):
            raise VisualError('job_not_found', 'A job ID belonging to this UI is required.')
        result = read_json(safe_path(self.output, identifier + '/status.json'))
        session = self.output / identifier / 'session'
        from .workflow_status import progress_view, acceptance_view
        # Reading the atomic session snapshot does not reconcile jobs, scan
        # target source trees or touch the recorder while it is running.
        result['progress'] = progress_view(session)
        result['report_available'] = result['status'] not in ('queued', 'running') and (session / 'report/index.html').is_file()
        report = session / 'report/report.json'
        if result['report_available'] and report.is_file():
            stat = report.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            cached = self.report_views.get(identifier)
            if cached is None or cached[0] != key:
                cached = key, acceptance_view(read_json(report))
                self.report_views[identifier] = cached
            result['acceptance'] = cached[1]
        return result

    def _execute(self, identifier, form):
        from .intake import prepare_intake, resume_selection
        from .media_session import binding_preset, create_media_session
        directory = self.output / identifier
        state = self.status(identifier)
        state.update(status='running', step='selection_and_binding')
        write_json(directory / 'status.json', state)
        try:
            self._verify_intake()
            selection = form.get('selection')
            original = load_package(self.directory / 'package')
            # Validate against the original exact images before rebinding an
            # explicitly declared timeline to a fresh copied intake.
            if not isinstance(selection, dict) or selection.get('package_sha256') != original['package_sha256']:
                raise VisualError('selection_binding_mismatch', 'The selection must belong to this video.')
            for obj in selection.get('objects', []):
                index = obj.get('frame_index')
                if type(index) is not int or not 0 <= index < len(original['frames']) or obj.get('template_frame_sha256') != original['frames'][index]['sha256']:
                    raise VisualError('selection_binding_mismatch', 'The selected image has changed.')
            inputs = None
            if form.get('inputs') is not None:
                inputs = declared_form_inputs(form['inputs'])
            target = directory / 'intake'
            created = prepare_intake(self.directory / 'package', self.intake['project']['project'],
                self.intake['request'], target, inputs=inputs)
            selected = deepcopy(selection)
            selected['package_sha256'] = created['package_sha256']
            resume_selection(target, selected)
            binding = binding_preset(target, object_map=form.get('object_map'),
                initial_node_count=form.get('initial_nodes'), expected_selected_id=form.get('selected_node'),
                game_stage=form.get('game_stage'))
            if binding.get('status') != 'bound':
                raise VisualError('needs_input_binding', binding.get('reason', 'Input mapping is missing.'))
            result = create_media_session(target, directory / 'session', binding)
            state.update(step='session_ready', session=result['session'], result=result)
            write_json(directory / 'status.json', state)
            if form.get('launch', False):
                from .launcher import run_media
                state.update(step='kiro_cli', model_requested=True)
                write_json(directory / 'status.json', state)
                state['launch_result'] = run_media(result['session'])
                # Process completion and actual numerical acceptance are distinct.
                from .session import Session
                state['session_state'] = Session(result['session']).inspect()
                if state['launch_result'].get('status') != 'completed':
                    launch = state['launch_result']
                    raise VisualError(launch.get('status', 'launch_failed'),
                        launch.get('error') or 'The Kiro run did not complete; details were preserved.')
            state.update(status='completed', step='done', finished_at=time.time())
        except Exception as exc:
            state.update(status='failed', finished_at=time.time(), error={
                'code': getattr(exc, 'code', 'local_workflow_error'), 'message': str(exc)})
        write_json(directory / 'status.json', state)


def make_server(directory):
    from .backend import _lock
    directory = _ordinary(directory)
    # Exclusive lifetime lease prevents a second UI from interrupting or
    # relaunching an operation which the first server is still running.
    lease = _lock(directory / 'intake-ui.lock', timeout=.05)
    try:
        lease.__enter__()
    except TimeoutError as exc:
        raise VisualError('ui_already_running', 'The local UI is already running for this video.') from exc
    try:
        workflow = IntakeWorkflow(directory)
    except BaseException:
        lease.__exit__(None, None, None)
        raise
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_body(self, code, value, content_type='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def route(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != origin.removeprefix('http://') or self.headers.get('Origin', origin) != origin:
                raise VisualError('invalid_origin', 'Only this local UI can be used.')
            prefix = '/' + token + '/'
            path = urlsplit(self.path).path
            if not path.startswith(prefix):
                raise VisualError('invalid_origin', 'The local UI key is invalid.')
            return path[len(prefix):]

        def do_GET(self):
            try:
                path = self.route()
                if path == 'metadata':
                    return self.send_body(200, workflow.metadata())
                if path.startswith('job/'):
                    parts = path.split('/', 3)
                    status = workflow.status(parts[1])
                    if len(parts) == 2:
                        return self.send_body(200, status)
                    if len(parts) == 4 and parts[2] == 'report' and status['report_available']:
                        source = safe_path(workflow.output / parts[1] / 'session/report', parts[3])
                        types = {'.html': 'text/html; charset=utf-8', '.json': 'application/json; charset=utf-8',
                            '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                            '.png': 'image/png', '.mp4': 'video/mp4', '.diff': 'text/plain; charset=utf-8', '.txt': 'text/plain; charset=utf-8'}
                        if source.suffix.lower() in types:
                            return self.send_body(200, source.read_bytes(), types[source.suffix.lower()])
                    return self.send_body(404, {'code': 'not_found'})
                if path == 'annotation.html':
                    from .intake import _ANNOTATION_HTML
                    manifest = workflow.package()
                    data = {'package_sha256': manifest['package_sha256'], 'image_size': manifest['image_size'],
                        'frames': [{'path': 'package/' + f['path'], 'sha256': f['sha256'], 'time_s': f['time_s']} for f in manifest['frames']],
                        'request': workflow.intake['request'], 'proposals': [], 'input_provenance': manifest['inputs']['provenance']}
                    encoded = json.dumps(data, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
                    page = _ANNOTATION_HTML.replace('__INTAKE_DATA__', encoded)
                    return self.send_body(200, page.replace('</html>', _WORKFLOW_HTML + '</html>').encode('utf-8'), 'text/html; charset=utf-8')
                # Only manifest-bound frame files are exposed, never target code,
                # worker results, private oracle records or arbitrary host paths.
                if path.startswith('package/'):
                    workflow.package()
                    relative = path[len('package/'):]
                    if relative in workflow.frame_files:
                        source = safe_path(workflow.directory / 'package', relative)
                        image = source.read_bytes()
                        import hashlib
                        if hashlib.sha256(image).hexdigest() != workflow.frame_files[relative]:
                            raise VisualError('source_changed', 'The selected video frame has changed.')
                        return self.send_body(200, image, 'image/png')
                self.send_body(404, {'code': 'not_found'})
            except (VisualError, OSError, ValueError) as exc:
                self.send_body(400, {'code': getattr(exc, 'code', 'invalid_request'), 'message': str(exc)})

        def do_POST(self):
            try:
                if self.route() != 'submit' or self.headers.get('Content-Type') != 'application/json':
                    raise VisualError('invalid_request', 'A JSON form submission is required.')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_REQUEST_BYTES:
                    raise VisualError('invalid_request', 'The form size is invalid.')
                self.send_body(202, workflow.submit(json.loads(self.rfile.read(size))))
            except (VisualError, OSError, ValueError) as exc:
                self.send_body(400, {'code': getattr(exc, 'code', 'invalid_request'), 'message': str(exc)})

    class Server(ThreadingHTTPServer):
        released = False

        def server_close(self):
            super().server_close()
            if not self.released:
                self.released = True
                with workflow.lock:
                    workflow.accepting = False
                    if workflow.finished.is_set():
                        lease.__exit__(None, None, None)
                    else:
                        # In an embedding host, HTTP shutdown must not release
                        # ownership while its workflow thread is still alive.
                        # A full process exit closes the native Job Object.
                        def release_after_work():
                            workflow.finished.wait()
                            lease.__exit__(None, None, None)
                        threading.Thread(target=release_after_work, daemon=True).start()

    try:
        server = Server(('127.0.0.1', 0), Handler)
    except BaseException:
        lease.__exit__(None, None, None)
        raise
    server.daemon_threads = True
    server.workflow = workflow
    server.url = f'http://127.0.0.1:{server.server_port}/{token}/annotation.html'
    return server


def serve(directory):
    server = make_server(directory)
    print(json.dumps({'status': 'ready', 'url': server.url, 'model_started': False}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    serve(args.directory)


_WORKFLOW_HTML = r'''<section style="max-width:1100px;margin:auto;padding:24px">
<h2>Continue from the selection with Kiro</h2><p>Match the objects you selected with their counterparts in the target. The operation runs in a new working copy.</p>
<button id="kc-mappings">Show object mappings</button><div id="kc-map-list"></div>
<div id="kc-domain"></div><div id="kc-inputs"></div>
<label><input id="kc-launch" type="checkbox" checked> Start Kiro CLI when the session is ready (<span id="kc-model">Opus 5.5 / max</span>; uses the subscription)</label>
<p><button id="kc-submit">Save and continue</button> <button id="kc-refresh">Reconnect</button></p><pre id="kc-status" role="status" style="white-space:pre-wrap"></pre>
<p><a id="kc-report" hidden target="_blank" rel="noopener" style="color:#a8cfff">Open local report</a></p>
<div id="kc-progress" role="status"></div><div id="kc-acceptance"></div>
<details><summary>Operation details</summary><pre id="kc-details" style="white-space:pre-wrap"></pre></details>
</section><script>
(async()=>{'use strict'; const out=document.querySelector('#kc-status');
const api=async(path,body)=>{let r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});let v=await r.json();if(!r.ok)throw Error(v.message||v.code);return v};
let meta=await api('metadata');out.textContent=meta.scope;let draft=meta.last_form||{},initialSelection=draft.selection||meta.selection;
document.querySelector('#kc-model').textContent=meta.model.id+' / '+meta.model.effort;
if(initialSelection&&window.kiroclopsSetSelection)window.kiroclopsSetSelection(initialSelection);
document.querySelector('#kc-refresh').onclick=()=>location.reload();
let download=document.querySelector('#download');if(download)download.hidden=true;
if(download)for(let p of document.querySelectorAll('main>p'))if(p.textContent.startsWith('Give the downloaded file'))p.textContent='Save the selection with the form below. Video selection and analysis run on this computer; if Kiro is started, the model connection and subscription are used.';
function sel(){if(!window.kiroclopsSelection)throw Error('The object selection UI must be prepared again.');return window.kiroclopsSelection()}
function mappings(){let box=document.querySelector('#kc-map-list'),prior={};for(let s of box.querySelectorAll('select'))prior[s.dataset.reference]=s.value;box.replaceChildren();for(let o of sel().objects){let label=document.createElement('label');label.textContent=o.id+' → ';let s=document.createElement('select');s.dataset.reference=o.id;s.setAttribute('aria-label',o.id+' target mapping');s.add(new Option('Select target',''));for(let id of meta.target_ids)s.add(new Option(id,id));let value=prior[o.id]??draft.object_map?.[o.id]??(meta.target_ids.includes(o.id)?o.id:'');if(meta.target_ids.includes(value))s.value=value;label.append(s);box.append(label)}}
document.querySelector('#kc-mappings').onclick=mappings;
const domain=document.querySelector('#kc-domain');if(meta.domain==='svg')domain.innerHTML='<label>Initial node count <input id="kc-nodes" type="number" min="3" max="6" value="3"></label><label>Expected selected node <select id="kc-selected"><option value="">Not specified</option>'+meta.target_ids.map(n=>'<option>'+n+'</option>').join('')+'</select></label>';
if(meta.domain==='game')domain.innerHTML='<label>Transfer <select id="kc-stage"><option value="jump">Jump</option><option value="ground">Ground movement</option></select></label>';
let entries=[];let inputs=document.querySelector('#kc-inputs');if(meta.inputs.provenance==='unknown'){
 inputs.innerHTML='<p>Input times are unknown in this video. Add the inputs you know; they are recorded as a user declaration.</p><div id="kc-event-list"></div><button id="kc-add-input">Add input</button>';
 document.querySelector('#kc-add-input').onclick=()=>{let row=document.createElement('div'),time=document.createElement('input'),action=document.createElement('select'),target=document.createElement('select'),x=document.createElement('input'),y=document.createElement('input'),remove=document.createElement('button');row.className='bar';time.type='number';time.min='0';time.step='.001';time.placeholder='Seconds';time.setAttribute('aria-label','Input time (seconds)');
 let choices=meta.domain==='game'?[['jump:press','Press jump'],['jump:release','Release jump'],['left:press','Press left'],['left:release','Release left'],['right:press','Press right'],['right:release','Release right']]:[['click','Click'],['drag_start','Start drag'],['drag_move','Drag step'],['drag_end','Release pointer']];
 for(let [value,label] of choices)action.add(new Option(label,value));for(let n of [x,y]){n.type='number';n.min='0';n.max='1';n.step='.001'}x.setAttribute('aria-label','Target or progress fraction');y.setAttribute('aria-label','Vertical target fraction');
 function fields(){target.replaceChildren();let values=action.value==='drag_start'?(meta.domain==='svg'?meta.target_ids.map(n=>['[data-node-id="'+n+'"]','Node '+n]):[['#panel-handle','Panel handle']]):(meta.domain==='svg'?[['#add-node','Add node'],['#remove-node','Remove node'],...meta.target_ids.map(n=>['[data-node-id="'+n+'"]','Node '+n])]:[['#open-panel','Open panel'],['#close-panel','Close panel']]);for(let [value,label] of values)target.add(new Option(label,value));target.hidden=meta.domain==='game'||!['click','drag_start'].includes(action.value);x.hidden=!['drag_start','drag_move'].includes(action.value);y.hidden=action.value!=='drag_start'||meta.domain!=='svg';x.placeholder=action.value==='drag_move'?'Progress 0–1':'Target fraction 0–1';y.placeholder='Vertical 0–1'}
 action.onchange=fields;fields();remove.textContent='Remove';remove.onclick=()=>row.remove();let useFrame=document.createElement('button');useFrame.textContent='Time of this frame';useFrame.onclick=()=>{let f=window.kiroclopsCurrentFrame?.();if(!f){out.textContent='Wait for the video frame to load.';return;}time.value=f.time_s;};row.append(time,useFrame,action,target,x,y,remove);document.querySelector('#kc-event-list').append(row);entries.push({row,time,action,target,x,y});};
}
function formInputs(){if(meta.inputs.provenance!=='unknown')return null;let number=(input)=>{if(input.value==='')throw Error('Specify the input time and the required fractions.');return Number(input.value)};return entries.filter(e=>e.row.isConnected).map(({time,action,target,x,y})=>{let e={time_s:number(time)};if(meta.domain==='game'){let [name,state]=action.value.split(':');Object.assign(e,{action:name,pressed:state==='press'})}else{e.action=action.value;if(['click','drag_start'].includes(e.action))e.selector=target.value;if(e.action==='drag_start')e.destination=meta.domain==='svg'?{viewport_x:number(x),viewport_y:number(y)}:{travel_fraction:number(x)};if(e.action==='drag_move')e.fraction=number(x)}return e})}
function showProgress(s){let p=s.progress,box=document.querySelector('#kc-progress');box.textContent=p?'Candidate '+p.candidates_used+' / '+p.candidate_limit+(['queued','running'].includes(s.status)&&p.remaining_seconds!=null?' · Remaining '+Math.ceil(p.remaining_seconds/60)+' minutes':'')+(p.active_job?' · '+({prepare:'Baseline measurement',evaluate:'Candidate measurement',finalize:'Final validation'}[p.active_job.operation]||p.active_job.operation):''):'';let a=document.querySelector('#kc-acceptance');a.replaceChildren();if(!s.acceptance)return;let heading=document.createElement('h3');heading.textContent=s.acceptance.certified_success?'All acceptance checks passed':'Operation result: checks evaluated separately';a.append(heading);let names={measurement_validity:'Video measurement',observed_behavior:'Demonstrated movement',application_integrity:'Application integrity',model_completion:'Kiro run',native_evidence:'Tool records',unseen_validation:'New inputs',independent_visual_review:'Independent visual review',source_review:'Source review'},values={pass:'Passed',fail:'Did not pass',invalid:'Invalid evidence',incomplete:'Incomplete',not_evaluated:'Not tested',pending:'Pending',unknown:'Unknown'};for(let [key,value] of Object.entries(s.acceptance.axes)){let line=document.createElement('p');line.textContent=names[key]+': '+(values[value.status]||value.status);a.append(line)}}
let pollFailures=0;async function poll(id){try{let s=await api('job/'+id);pollFailures=0;document.querySelector('#kc-details').textContent=JSON.stringify(s,null,2);showProgress(s);let report=document.querySelector('#kc-report');report.hidden=!s.report_available;if(s.report_available)report.href='job/'+id+'/report/index.html';let names={selection_and_binding:'Preparing the selection and target mapping.',session_ready:'Working copy ready.',kiro_cli:'Kiro CLI is running. The result will be verified by measurement.',done:s.model_requested?'Kiro run finished. Acceptance and preservation results are below.':'Session and MCP connection ready; model not started.'};out.textContent=s.error?s.error.message:(names[s.step]||'Operation queued.');if(['queued','running'].includes(s.status))setTimeout(()=>poll(id),1000)}catch(e){out.textContent='Could not reach the job status; reconnecting. '+e.message;if(++pollFailures<=5)setTimeout(()=>poll(id),Math.min(30000,pollFailures*3000));else out.textContent+=' You can use the Reconnect button.'}}
document.querySelector('#kc-submit').onclick=async()=>{try{let selection=sel();if(!selection.objects.length)throw Error('Select at least one object.');mappings();let object_map={},used=new Set();for(let s of document.querySelectorAll('#kc-map-list select')){if(!s.value)throw Error('Select the target object for '+s.dataset.reference+'.');if(used.has(s.value))throw Error('Each target object can be mapped only once.');used.add(s.value);object_map[s.dataset.reference]=s.value;}
 let body={selection,object_map,launch:document.querySelector('#kc-launch').checked,inputs:formInputs()};if(meta.domain==='svg'){body.initial_nodes=Number(document.querySelector('#kc-nodes').value);body.selected_node=document.querySelector('#kc-selected').value||null}if(meta.domain==='game')body.game_stage=document.querySelector('#kc-stage').value;
 let submitted=await api('submit',body);await poll(submitted.job_id)}catch(e){out.textContent=e.message}};
if(initialSelection)mappings();if(typeof draft.launch==='boolean')document.querySelector('#kc-launch').checked=draft.launch;
for(let [id,key] of [['kc-nodes','initial_nodes'],['kc-selected','selected_node'],['kc-stage','game_stage']])if(document.getElementById(id)&&draft[key]!=null)document.getElementById(id).value=draft[key];
if(meta.inputs.provenance==='unknown'&&Array.isArray(draft.inputs))for(let event of draft.inputs){document.querySelector('#kc-add-input').click();let entry=entries.at(-1);entry.time.value=event.time_s;entry.action.value=meta.domain==='game'?event.action+':'+(event.pressed?'press':'release'):event.action;entry.action.onchange();if(event.selector)entry.target.value=event.selector;if(event.destination){entry.x.value=event.destination.travel_fraction??event.destination.viewport_x;entry.y.value=event.destination.viewport_y??''}if(event.fraction!=null)entry.x.value=event.fraction;}
if(meta.current_job)poll(meta.current_job);
})().catch(e=>document.querySelector('#kc-status').textContent=e.message);
</script>'''


if __name__ == '__main__':
    main()
