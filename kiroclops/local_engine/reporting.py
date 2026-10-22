"""Standalone, portable evidence reports. Acceptance states remain separate."""
from __future__ import annotations

import difflib
import html
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import os

from kiroclops.visual.package import copy_package, load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from .session import source_hashes, Session
from .review import validate_independent_review, validate_source_review, audit_native_run
from .evidence import observations as _observations, event_set as _events, measurement_groups, required_observation_bindings
from .report_costs import media_measurement_origin, session_costs
from .acceptance import recording_fact, summarize_acceptance
from .uncertainty import assess_measurement_limits


def _slug(value):
    text=str(value)
    return (re.sub(r'[^a-zA-Z0-9_-]+','-',text).strip('-')[:70] or 'item')+'-'+digest(text)[:10]


def report_tree_receipt(root):
    """Bind every report file by its portable path and exact bytes."""
    root=Path(root)

    def checked_type(path):
        info=path.lstat()
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info,'st_file_attributes',0) & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0)):
            raise VisualError('invalid_report_tree','Report contains a symlink or reparse point: '+str(path))
        return info.st_mode

    if not stat.S_ISDIR(checked_type(root)):
        raise VisualError('invalid_report_tree','Report root is not a directory')
    files={}
    pending=[root]
    while pending:
        directory=pending.pop()
        # scandir raises on unreadable directories; rglob can silently omit them.
        with os.scandir(directory) as scanned:
            paths=sorted((Path(entry.path) for entry in scanned),key=lambda path:path.name)
        for path in paths:
            mode=checked_type(path)
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                files[path.relative_to(root).as_posix()]=file_sha256(path)
            else:
                raise VisualError('invalid_report_tree','Report contains a nonregular entry: '+str(path))
    return {'file_count':len(files),'sha256':digest(files)}


def _portable(value):
    """Report values have no host paths; evidence files have explicit relative links."""
    if isinstance(value, dict):
        return {key:_portable(item) for key,item in value.items() if key not in
                ('source_path','raw_run','analysis','oracle_path','profile_path','command','executable')}
    if isinstance(value,list):return [_portable(item) for item in value]
    if isinstance(value,str):
        value=re.sub(r'[A-Za-z]:[\\/][^\r\n\"<>]*', '[local artifact]', value)
        if value.startswith(('/', '\\\\')):return '[local artifact]'
    return value


def _encode(package, output):
    """Encode variable frame durations for presentation; original timestamps win."""
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg:return None
    manifest=load_package(package)
    concat=Path(package)/'presentation.ffconcat'
    lines=['ffconcat version 1.0']
    frames=manifest['frames']
    for index,frame in enumerate(frames):
        lines.append("file '"+frame['path']+"'")
        if index+1<len(frames):lines.append(f"duration {frames[index+1]['time_s']-frame['time_s']:.9f}")
    # concat's last frame needs its own terminal duration. This presentation
    # frame is not added to or confused with the measurement package.
    lines.extend(['duration 0.04',"file '"+frames[-1]['path']+"'"])
    concat.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    try:
        result=subprocess.run([ffmpeg,'-v','error','-nostdin','-f','concat','-safe','1','-i',str(concat),
             '-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2','-fps_mode','vfr','-c:v','libx264','-crf','20',
             '-pix_fmt','yuv420p','-movflags','+faststart','-y',str(output)],capture_output=True,timeout=120,
             creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:raise VisualError('video_encoding_failure',result.stderr.decode('utf-8','replace')[-1000:])
    finally:concat.unlink(missing_ok=True)
    return output.name


def _media(record, root, relative, encode):
    destination=root/relative
    manifest=copy_package(record['package'],destination)
    origin=next((e['time_s'] for e in manifest['inputs']['events'] if e.get('state') not in ('released',False)),0)
    result={'package':relative+'/manifest.json','frames':[{'path':relative+'/'+f['path'],'time_s':f['time_s']} for f in manifest['frames']],
            'image_size':manifest['image_size'],'input_origin_s':origin,'package_sha256':manifest['package_sha256'],
            'measurement_origin':_portable(media_measurement_origin(record))}
    observations=_observations(record)
    if observations:
        write_json(destination/'observations.json',_portable(observations))
        result['objects']=[{'id':o['id'],'frames':[{k:f.get(k) for k in ('index','time_s','x','y','left','top','width','height','valid','present','reason')} for f in o['frames']],
                            'valid_ratio':o['valid_ratio']} for o in observations['objects']]
    events=_events(record)
    if events:
        write_json(destination/'events.json',_portable(events))
        result['events']=_portable(events.get('events',[]))
        result['relations']=_portable(events.get('relations',[]))
        result['unobservable']=_portable(events.get('unobservable',[]))
        result['event_file']=relative+'/events.json'
    if encode:
        _encode(destination,destination/'presentation.mp4')
        if (destination/'presentation.mp4').exists():result['video']=relative+'/presentation.mp4'
    return result


def _diff(state,candidate,root):
    before,after=Path(state['baseline']),candidate
    original,current=source_hashes(before),source_hashes(after)
    chunks=[]
    for name in sorted(set(original)|set(current)):
        if original.get(name)==current.get(name):continue
        try:
            a=(before/name).read_text('utf-8').splitlines(True) if name in original else []
            b=(after/name).read_text('utf-8').splitlines(True) if name in current else []
        except UnicodeError:
            chunks.append(f'Binary asset changed: {name}\n');continue
        chunks.extend(difflib.unified_diff(a,b,fromfile='before/'+name,tofile='after/'+name))
    path=root/'candidate.diff';path.write_text(''.join(chunks),encoding='utf-8')
    return path.name


def build_report(directory, *, output=None, encode=True):
    directory=Path(directory).resolve();session=Session(directory);state=session._load()
    root=Path(output).resolve() if output else directory/'report'
    if root.exists():raise VisualError('output_exists','Preserve the earlier report; use a fresh report output')
    root.mkdir(parents=True)
    data={'schema_version':1,'kind':'LocalEvidenceReport','domain':state['domain'],'mode':state['mode'],
          'goal':state['goal'],'selected_candidate_id':state.get('selected_candidate_id'),
          'candidates':_portable(state.get('candidates',[])),'session_status':state['status'],
          'numerical':_portable(state.get('final')),'independent_review_status':'pending','source_review_status':'pending',
          'overall_acceptance':'pending','groups':[],'limitations':[
              'Numerical acceptance, application integrity and independent visual review are separate.',
              'Videos are presentation copies. Frame stepping uses the original images and timestamps.',
              'Visible proximity does not prove physical collision.',
              'Independent reviews are explicit operator-recorded observations bound to exact evidence. They are not third-party attestation.',
              'Native CLI logs and scoped tool rules are an auditable experiment boundary, not an operating-system security sandbox.']}
    data['cost_scopes']=session_costs(directory,state)
    control=state.get('experiment',{}).get('engineering_control')
    if isinstance(control,dict):
        data['engineering_control']=_portable(control)
    if state.get('experiment',{}).get('reference_mode')=='media_suite':
        from .media_suite import load_suite
        suite=load_suite(state['reference'])
        data['reference_suite']={'schema_version':suite['schema_version'],'suite_sha256':digest(suite),
            'stage':suite['stage'],'coverage':suite['coverage'],'heldout_status':suite['heldout_status'],
            'cases':[{k:c[k] for k in ('case_id','role','purpose','stage','input_provenance')} for c in suite['cases']],
            'baseline_roles':{'original':'Original unchanged comparison target; 50% improvement baseline',
                              'before':'Successful authoring parent; preservation baseline'},
            'historical_3a_cohort_changed':False}
        if suite['schema_version']==2:
            data['reference_suite'].update(acceptance_profile=suite['acceptance_profile'],
                acceptance_policy=suite.get('acceptance_policy'),execution_policy=suite['execution_policy'],
                source_epoch=suite['source_epoch'])
            data['reference_suite']['baseline_roles']['original']='Original unchanged target; improvement reported separately from final quality'
    # Only immutable candidate records are reported. Never compare the mutable
    # workspace with a result belonging to an earlier source snapshot.
    selected=state.get('selected_candidate_id')
    if selected:
        candidate=directory/'candidates'/selected/'source'
        saved=next(c for c in state['candidates'] if c['candidate_id']==selected)
        if digest(source_hashes(candidate))!=saved['source_sha256']:
            raise VisualError('source_changed','Selected candidate changed before reporting')
        data['candidate_diff']=_diff(state,candidate,root)
        data['candidate_sha256']=saved['source_sha256']
        for review_name in ('independent_review','source_review'):
            path=directory/(review_name+'.json')
            if path.exists():
                validator=validate_independent_review if review_name=='independent_review' else validate_source_review
                try:
                    validated=validator(path,**({'directory':directory} if review_name=='source_review' else {}),candidate_sha256=saved['source_sha256'])
                    data[review_name]=_portable(validated);data[review_name+'_status']='pass'
                except (VisualError,OSError,ValueError,KeyError) as exc:
                    data[review_name+'_status']='fail';data[review_name]={'status':'fail','code':getattr(exc,'code','invalid_review'),'message':str(exc)}
    if state.get('final',{}):
        detail=session.get_job(state['final']['job_id'])
        if detail.get('candidate_id')!=selected or detail.get('source_sha256')!=data.get('candidate_sha256'):
            raise VisualError('wrong_result','Final evidence does not belong to the selected completed source')
        if ((detail.get('result') or {}).get('pass') is True)!=state['final'].get('pass'):
            raise VisualError('wrong_result','Final session summary differs from its immutable result')
        data['acceptance_details']=_portable(detail['result'] if detail['status']=='succeeded' else {'status':detail['status'],'error':detail.get('error')})
    recording_facts=[]
    for dataset in measurement_groups(directory):
        measurement=dataset['directory'];roles=dataset['roles'];measurement_id=dataset['id']
        scenarios={}
        for role,records in roles.items():
            for index,record in enumerate(records):
                recording_facts.append(recording_fact(record,job_id=dataset['job_id'],role=role,context=measurement_id))
                identifier=record.get('scenario',{}).get('id',f'scenario-{index:02}')
                # Index also disambiguates old game manifests lacking IDs.
                key=f'{index:02}-{_slug(identifier)}'
                group=scenarios.setdefault(key,{'id':measurement_id+'/'+key,'scenario':_portable(record.get('scenario',{})),'roles':{}})
                relative=f'media/{_slug(measurement_id)}/{key}/{role}'
                group['roles'][role]=_media(record,root,relative,encode)
        data['groups'].extend(scenarios.values())
        comparison=measurement/'comparison.json'
        if comparison.exists():
            name='data/'+_slug(measurement_id)+'-comparison.json';write_json(root/name,_portable(read_json(comparison)))
            for group in scenarios.values():group['comparison']=name
    # Open on the complete result when present. Candidate-only records can
    # sort before the final job ID, which otherwise leaves the report showing
    # a lone video instead of the reference/before/after comparison.
    def presentation_rank(group):
        roles=set(group['roles'])
        if {'reference','before','final'}<=roles:return 0
        if {'reference','before','candidate'}<=roles:return 1
        if {'reference','before'}<=roles:return 2
        return 3
    data['groups'].sort(key=presentation_rank)
    launch=directory/'kiro/launch.json'
    if launch.exists():data['kiro_run']=_portable(read_json(launch))
    data['native_run_status']='pending'
    if launch.exists():
        try:data['native_run']=_portable(audit_native_run(directory));data['native_run_status']='pass'
        except (VisualError,OSError,ValueError,KeyError) as exc:
            data['native_run_status']='fail';data['native_run']={'code':getattr(exc,'code','invalid_native_evidence'),'message':str(exc)}
    data['review_coverage_status']='pending'
    if data['independent_review_status']=='pass':
        reviewed={tuple(v) for v in data['independent_review'].get('required_observations',[])}
        try:
            required=required_observation_bindings(directory,state)
            data['review_coverage_status']='pass' if required and required==reviewed else 'fail'
        except (VisualError,OSError,ValueError,KeyError) as exc:
            data['review_coverage_status']='fail'
            data['review_coverage_error']={'code':getattr(exc,'code','incomplete_review'),'message':str(exc)}
    data['overall_acceptance']='pass' if isinstance(data['numerical'],dict) and data['numerical'].get('pass') is True and all(
        data[k]=='pass' for k in ('independent_review_status','source_review_status','native_run_status','review_coverage_status')) else 'pending'
    if (isinstance(data['numerical'],dict) and data['numerical'].get('pass') is False) or any(data[k]=='fail' for k in
            ('independent_review_status','source_review_status','native_run_status','review_coverage_status')):data['overall_acceptance']='fail'
    # A user-supplied clip cannot establish unobserved holdout behavior. Preserve
    # its explicit partial verdict without changing the numerical pass flag.
    details=data.get('acceptance_details') or {}
    if (details.get('acceptance_status')=='partial_unobserved_holdout' and details.get('calibration_pass') is True
            and not any(data[k]=='fail' for k in ('independent_review_status','source_review_status','native_run_status','review_coverage_status'))):
        data['overall_acceptance']='partial'
    interpretation=summarize_acceptance(state,data,recordings=recording_facts)
    data['acceptance_axes']=interpretation['axes']
    data['acceptance_summary']=interpretation['summary']
    data['acceptance_interpretation_policy']=interpretation['policy']
    tolerance_comparisons=[{**row,'windows':row.get('windows') or ([row['pixel_comparison']] if row.get('pixel_comparison') else [])}
                          for row in details.get('comparisons',[])]
    data['tolerance_assessment']=assess_measurement_limits(tolerance_comparisons,None)
    write_json(root/'report.json',data)
    script=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('</','<\\/')
    (root/'index.html').write_text(HTML.replace('__DATA__',script),encoding='utf-8')
    return {'status':'built','report':str(root/'index.html'),'data':str(root/'report.json'),
            'groups':len(data['groups']),'overall_acceptance':data['overall_acceptance'],
            'acceptance_summary':data['acceptance_summary'],
            'independent_review_status':data['independent_review_status']}


HTML=r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>KiroClops · local evidence</title><style>
:root{color-scheme:dark;font:15px system-ui;background:#0a111d;color:#dbe9fa}body{max-width:1400px;margin:32px auto;padding:0 24px}h1{font-size:34px;color:#fff}h2{font-size:20px}p{line-height:1.55}.muted{color:#96aac5}a{color:#75c6ff}button,select{padding:10px;border:1px solid #426081;border-radius:6px;background:#17283c;color:inherit}section{padding:20px;margin:18px 0;background:#111f32;border:1px solid #28415d;border-radius:12px}.cards{display:flex;gap:12px;flex-wrap:wrap}.card{flex:1;min-width:260px}img{width:100%;max-height:480px;object-fit:contain;background:#080c15}canvas{width:100%;height:220px;background:#091525}code,pre{white-space:pre-wrap;overflow-wrap:anywhere}input{width:100%}.badge{display:inline-block;padding:6px 12px;border-radius:20px;background:#2b4059;margin-right:8px}details{margin:12px 0}small{color:#a5b9d2}</style>
<header><div class="muted">KIROCLOPS / LOCAL ENGINEERING</div><h1>See the behavior. Verify the change.</h1><p id="goal"></p><h2 id="acceptance-title"></h2><div id="status"></div></header>
<section><h2>Separate acceptance checks</h2><div id="acceptance-axes" class="cards"></div><details><summary>Unchanged strict certification aggregate</summary><p>This compatibility verdict combines behavior, unseen tests and evidence completeness. It is not a verdict about motion alone. Existing results and thresholds remain unchanged.</p><pre id="legacy-acceptance"></pre></details></section>
<section><h2>Measurement limits and uncertainty</h2><p>Limits are shown in natural pixels. Pixel rounding examples are diagnostic assumptions, not measured tracker error. This assessment changes no acceptance threshold or saved verdict.</p><details><summary>Read the tolerance assessment</summary><pre id="tolerance-assessment"></pre></details></section>
<section id="engineering-control" hidden><h2>Engineering control metadata</h2><p id="engineering-notice"></p><pre id="engineering-detail"></pre></section>
<section><h2>Cost and measurement provenance</h2><p>Reference precomputation is a shared prerequisite outside the stage budget. Reused measurements did not start a new analysis worker. Preparation time is not first-time video-analysis latency. Missing times are unknown, not zero.</p><pre id="costs"></pre></section>
<section><h2>Reference · before · after</h2><select id="scenario"></select><p><button id="play">Play</button> <button id="back">Previous frame</button> <button id="next">Next frame</button> <span id="time"></span></p><input id="slider" type="range" min="0" step="0.001"><div id="media" class="cards"></div><h2>Measured horizontal / vertical position</h2><canvas id="plot" width="1200" height="260"></canvas><p class="muted">Solid: x. Dashed: y. Colors identify roles. Missing or invalid measurements are gaps. Playback is aligned to the first recorded input.</p><div id="compare"></div></section>
<section><h2>Acceptance and preserved behavior</h2><pre id="acceptance"></pre><p id="diff"></p><details><summary>Candidate history and Kiro run</summary><pre id="history"></pre></details><details><summary>Interpretation limits</summary><ul id="limits"></ul></details><p><a href="report.json">Portable report data</a></p></section>
<script>const D=__DATA__;const $=id=>document.getElementById(id);$('goal').textContent=D.goal;
$('tolerance-assessment').textContent=JSON.stringify(D.tolerance_assessment,null,2);
$('costs').textContent=JSON.stringify(D.cost_scopes||{status:'unrecorded',first_time_video_latency_claimed:false},null,2);if(D.engineering_control){$('engineering-control').hidden=false;$('engineering-detail').textContent=JSON.stringify(D.engineering_control,null,2);let c=D.engineering_control;let declared=c.kind==='OperatorFixtureControllerChange'&&typeof c.operator==='string'&&c.model_started===false&&c.kiro_success_claim===false;$('engineering-notice').textContent=declared?'This report declares an operator-authored engineering control. It is not autonomous Kiro success evidence.':'Control metadata does not establish autonomous code authorship.'}
$('acceptance-title').textContent=D.acceptance_summary.title;for(const [label,status] of [['Observed behavior',D.acceptance_axes.observed_behavior.status],['Measurement',D.acceptance_axes.measurement_validity.status],['Validation',D.acceptance_summary.status]]){let s=document.createElement('span');s.className='badge';s.textContent=label+': '+status;$('status').append(s)}
const axisLabels={measurement_validity:'Measurement validity',observed_behavior:'Observed behavior',application_integrity:'Executed application checks',model_completion:'Model turn completion',native_evidence:'Native evidence validation',unseen_validation:'Unseen interactions',independent_visual_review:'Independent visual review',source_review:'Source implementation review'};for(const [key,axis] of Object.entries(D.acceptance_axes)){let card=document.createElement('article');card.className='card';let title=document.createElement('h3'),status=document.createElement('p'),details=document.createElement('details'),summary=document.createElement('summary'),evidence=document.createElement('pre');title.textContent=axisLabels[key]||key;status.textContent=axis.status+' · '+axis.scope.replaceAll('_',' ');summary.textContent=axis.reason_codes.join(', ').replaceAll('_',' ');evidence.textContent=JSON.stringify(axis.evidence,null,2);details.append(summary,evidence);card.append(title,status,details);$('acceptance-axes').append(card)}$('legacy-acceptance').textContent=JSON.stringify({overall_acceptance:D.overall_acceptance,numerical:D.numerical,certified_success:D.acceptance_summary.certified_success},null,2);
$('acceptance').textContent=JSON.stringify(D.acceptance_details||D.numerical||{status:'pending',reason:'No final candidate has been evaluated.'},null,2);$('history').textContent=JSON.stringify({candidates:D.candidates,kiro:D.kiro_run},null,2);for(let text of D.limitations){let li=document.createElement('li');li.textContent=text;$('limits').append(li)}if(D.candidate_diff){let a=document.createElement('a');a.href=D.candidate_diff;a.textContent='Review the actual candidate code diff';$('diff').append(a)}
let current=null,t=0,playing=false,last=0;const colors=['#70ccff','#ffb66c','#84e8ad','#db9aff'];
for(let i=0;i<D.groups.length;i++){let o=document.createElement('option');o.value=i;o.textContent=D.groups[i].id;$('scenario').append(o)}
function endOf(m){return m.frames[m.frames.length-1].time_s-m.input_origin_s}
function show(){if(!current)return;$('time').textContent=t.toFixed(3)+' s';$('slider').value=t;for(const [role,m] of Object.entries(current.roles)){let f=m.frames[0];for(const x of m.frames){if(x.time_s-m.input_origin_s>t)break;f=x}document.getElementById('image-'+role).src=f.path;}plot()}
function choose(){current=D.groups[+$('scenario').value];$('media').replaceChildren();if(!current)return;let min=Math.min(...Object.values(current.roles).map(m=>-m.input_origin_s));$('slider').min=min;$('slider').max=Math.max(...Object.values(current.roles).map(endOf));t=min;for(const [role,m] of Object.entries(current.roles)){let box=document.createElement('div');box.className='card';let title=document.createElement('h2');title.textContent=role;let im=document.createElement('img');im.id='image-'+role;im.alt=role+' original frame';box.append(title,im);if(m.measurement_origin){let provenance=document.createElement('details'),heading=document.createElement('summary'),body=document.createElement('pre');heading.textContent=m.measurement_origin.worker_started===false?'Reused reference measurement · no new worker':'Measurement provenance';body.textContent=JSON.stringify(m.measurement_origin,null,2);provenance.append(heading,body);box.append(provenance)}if(m.video){let a=document.createElement('a');a.href=m.video;a.textContent='Open presentation video';box.append(a)}$('media').append(box)}$('compare').replaceChildren();if(current.comparison){let a=document.createElement('a');a.href=current.comparison;a.textContent='Measurements, events and acceptance checks for this group';$('compare').append(a)}show()}
function plot(){let c=$('plot'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);let low=Infinity,high=-Infinity;for(const m of Object.values(current.roles))for(const o of m.objects||[])for(const f of o.frames)if(f.valid)for(const v of [f.x,f.y])if(Number.isFinite(v)){low=Math.min(low,v);high=Math.max(high,v)}if(!Number.isFinite(low))return;let span=Math.max(1,high-low),start=+$('slider').min,end=+$('slider').max;let roleIndex=0;for(const [role,m] of Object.entries(current.roles)){ctx.strokeStyle=colors[roleIndex++%colors.length];for(const o of m.objects||[])for(const axis of ['x','y']){ctx.beginPath();ctx.setLineDash(axis==='y'?[6,5]:[]);let pen=false;for(const f of o.frames){if(!f.valid||!Number.isFinite(f[axis])){pen=false;continue}let x=20+(f.time_s-m.input_origin_s-start)/Math.max(1e-9,end-start)*(c.width-40),y=c.height-20-(f[axis]-low)/span*(c.height-40);if(pen)ctx.lineTo(x,y);else ctx.moveTo(x,y);pen=true}ctx.stroke()}}ctx.setLineDash([]);ctx.strokeStyle='#fff';let x=20+(t-start)/Math.max(1e-9,end-start)*(c.width-40);ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,c.height);ctx.stroke()}
$('scenario').onchange=()=>{playing=false;choose()};$('slider').oninput=()=>{t=+$('slider').value;show()};$('play').onclick=()=>{playing=!playing;$('play').textContent=playing?'Pause':'Play'};function step(dir){playing=false;let ts=[...new Set(Object.values(current.roles).flatMap(m=>m.frames.map(f=>f.time_s-m.input_origin_s)))].sort((a,b)=>a-b);t=dir>0?(ts.find(x=>x>t+1e-6)??t):([...ts].reverse().find(x=>x<t-1e-6)??t);show()}$('next').onclick=()=>step(1);$('back').onclick=()=>step(-1);function tick(now){if(playing&&current){t+=Math.min(.1,(now-last)/1000);if(t>+$('slider').max){t=+$('slider').max;playing=false;$('play').textContent='Play'}show()}last=now;requestAnimationFrame(tick)}choose();requestAnimationFrame(tick);
</script>
<style>.image-stage{position:relative}.image-stage img{display:block;max-height:none}.image-stage svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}.overlay-control{display:block;margin:12px 0}.overlay-control input{width:auto}.event-list{max-height:240px;overflow:auto;font-size:13px}.event-list button{display:block;width:100%;text-align:left;margin:3px 0;padding:5px 8px}.event-list button.active{border-color:#9be9a9}.relation-state{min-height:1.5em}</style>
<script>
const NS='http://www.w3.org/2000/svg';let overlayEnabled=true;
let control=document.createElement('label');control.className='overlay-control';let checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=true;control.append(checkbox,document.createTextNode(' Show measured objects and geometry'));$('media').before(control);checkbox.onchange=()=>{overlayEnabled=checkbox.checked;show()};
function node(tag,attributes){let n=document.createElementNS(NS,tag);for(let [k,v] of Object.entries(attributes))n.setAttribute(k,v);return n}
const originalShow=show,originalChoose=choose;
show=function(){originalShow();if(!current)return;$('play').textContent=playing?'Pause':'Play';for(const [role,m] of Object.entries(current.roles)){
 let layer=document.getElementById('overlay-'+role);if(!layer)continue;layer.replaceChildren();let time=t+m.input_origin_s;
 for(let [oi,o] of (m.objects||[]).entries()){let f=o.frames[0];for(let x of o.frames){if(x.time_s>time)break;f=x}if(!overlayEnabled||!f||!f.valid||f.present===false)continue;let color=colors[oi%colors.length];
  if([f.left,f.top,f.width,f.height].every(Number.isFinite))layer.append(node('rect',{x:f.left,y:f.top,width:f.width,height:f.height,fill:'none',stroke:color,'stroke-width':1.2}));
  if(Number.isFinite(f.x)&&Number.isFinite(f.y)){layer.append(node('circle',{cx:f.x,cy:f.y,r:3,fill:color,stroke:'#000','stroke-width':.7}));let label=node('text',{x:f.x+5,y:f.y-6,fill:color,'font-size':12,stroke:'#08111d','stroke-width':2,'paint-order':'stroke'});label.textContent=o.id;layer.append(label)}
 }
 let events=document.getElementById('events-'+role);for(let button of events.querySelectorAll('button'))button.classList.toggle('active',time>=+button.dataset.start-.025&&time<=+button.dataset.end+.025);
 let latest=new Map();for(let r of m.relations||[])if(Number.isFinite(r.time_s)&&Math.abs(r.time_s-time)<.025){let key=JSON.stringify([r.type,r.connection_id,r.object_id,r.object_ids]);let old=latest.get(key);if(!old||Math.abs(r.time_s-time)<Math.abs(old.time_s-time))latest.set(key,r)}document.getElementById('relations-'+role).textContent=[...latest.values()].filter(r=>r.type==='connection_attachment'||r.type==='boundary_distance').map(r=>r.type+': '+(r.connection_id||r.object_id||(r.object_ids||[]).join(' / '))+(r.visually_attached===false?' — detached':r.visually_attached===true?' — attached':'')).join(' · ');
}};
choose=function(){originalChoose();if(!current)return;for(const [role,m] of Object.entries(current.roles)){
 let im=document.getElementById('image-'+role),box=im.parentElement,stage=document.createElement('div');stage.className='image-stage';im.before(stage);stage.append(im);let size=m.image_size;let svg=node('svg',{id:'overlay-'+role,viewBox:'0 0 '+size[0]+' '+size[1]});stage.append(svg);
 let details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Visual events ('+(m.events||[]).length+')';details.append(summary);let list=document.createElement('div');list.id='events-'+role;list.className='event-list';details.append(list);
 for(let e of m.events||[]){let interval=e.time_interval_s;if(!interval)continue;let button=document.createElement('button');button.dataset.start=interval[0];button.dataset.end=interval[1];button.textContent=(interval[0]-m.input_origin_s).toFixed(3)+'–'+(interval[1]-m.input_origin_s).toFixed(3)+' s · '+e.type+' · '+(e.object_ids||[]).join(', ');button.onclick=()=>{playing=false;t=interval[0]-m.input_origin_s;show()};list.append(button)}
 if(m.event_file){let link=document.createElement('a');link.href=m.event_file;link.textContent='Full event and relationship evidence';details.append(link)}
 box.append(details);let rel=document.createElement('small');rel.className='relation-state';rel.id='relations-'+role;box.append(rel);
 }show()};choose();
</script></html>'''
