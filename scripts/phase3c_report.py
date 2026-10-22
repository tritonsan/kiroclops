"""Package small sealed interaction checks, keeping earlier failures visible."""
from pathlib import Path
import argparse
import difflib
import html
import json
import shutil

from kiroclops.visual.schema import read_json, write_json, digest, file_sha256
from kiroclops.visual.package import load_package


def refresh_presentation(output):
    """Iterate the viewer without recapturing, rescoring or re-encoding evidence."""
    if not (output/'initial-build-integrity.json').exists():
        shutil.copyfile(output/'integrity.json',output/'initial-build-integrity.json')
    public=read_json(output/'data.json');result=read_json(output/'result.json')
    public['producer_sha256']=result['producer_sha256']=file_sha256(Path(__file__))
    write_json(output/'data.json',public);write_json(output/'result.json',result)
    page=PAGE.replace('__DATA__',json.dumps(public,ensure_ascii=True,allow_nan=False).replace('<','\\u003c'))
    (output/'index.html').write_text(page,encoding='utf-8')
    write_json(output/'integrity.json',{'files':{p.relative_to(output).as_posix():file_sha256(p)
        for p in output.rglob('*') if p.is_file() and p.name!='integrity.json'}})


def build(output):
    from kiroclops.common import ROOT
    from kiroclops.local_engine.reporting import _encode, _portable
    if output.exists():raise ValueError('Use a new report directory.')
    output.mkdir(parents=True);(output/'media').mkdir()
    original=ROOT/'artifacts/phase3c/interaction-probes-01'
    additional=ROOT/'artifacts/phase3c/interaction-probes-panel-warmup-02'
    groups=[];checks=[]
    roots=[original,additional]
    for root in roots:
        summary=read_json(root/'result.json')
        for n,row in enumerate(summary['rows']):
            record=read_json(row['path']);key=root.name+'-'+str(n)
            group={'id':key,'domain':row['domain'],'scenario':row['scenario']['id'],
                'status':record['status'],'initial_series':root==original,
                'comparison':record.get('comparison'),'error':record.get('error'), 'roles':{}}
            write_json(output/(key+'.json'),_portable(record))
            group['evidence']=key+'.json'
            for evidence in record.get('records',[]):
                role=evidence['role'];package=Path(evidence['package']);manifest=load_package(package,verify='hashes')
                analysis=read_json(evidence['analysis'])
                if manifest['package_sha256']!=evidence['package_sha256'] or file_sha256(evidence['analysis'])!=evidence['analysis_sha256']:
                    raise ValueError('Evidence changed: '+key+' '+role)
                video='media/'+key+'-'+role+'.mp4'
                _encode(package,output/video)
                inputs=manifest['inputs']['events'];origin=inputs[0]['time_s'] if inputs else manifest['frames'][0]['time_s']
                item={'video':video,'input_origin_s':origin-manifest['frames'][0]['time_s'],
                      'frame_times':[f['time_s']-origin for f in manifest['frames']],
                      'unique_images':evidence['unique_images'],'frame_count':evidence['frame_count'],
                      'package_sha256':evidence['package_sha256'],'analysis_sha256':evidence['analysis_sha256']}
                if row['domain']=='game':
                    frames=analysis['frames'];body=analysis['body_height_px']
                    baseline=sum(f['y'] for f in frames[20:30])/10
                    item['curves']=[{'id':'character','points':[[f['time_s']-origin,(baseline-f['y'])/body] for f in frames if f['valid']]}]
                    item['curve_units']='body height'
                else:
                    objects=analysis['observations']['objects']
                    item['curves']=[{'id':obj['id'],'points':[[f['time_s']-origin,f['x']] for f in obj['frames'] if f['valid']]} for obj in objects]
                    item['curve_units']='native image pixels (x)'
                group['roles'][role]=item
                checks.append({'id':key,'role':role,'package_sha256':manifest['package_sha256'],
                               'video_sha256':file_sha256(output/video)})
            groups.append(group)
    registration=read_json(original/'registration.json')
    for domain,s in registration['sources'].items():
        name={'game':'player.gd','panel':'src/panel-motion.js','svg':'src/layout-motion.js'}[domain]
        old=(Path(s['baseline'])/name).read_text(encoding='utf-8-sig').splitlines(keepends=True)
        new=(Path(s['candidate'])/name).read_text(encoding='utf-8-sig').splitlines(keepends=True)
        (output/(domain+'.diff')).write_text(''.join(difflib.unified_diff(old,new,fromfile='before/'+name,tofile='kiro/'+name)),encoding='utf-8')
    user=ROOT/'artifacts/phase3c/user-flow-kiro-01/result.json'
    data={'kind':'Phase3CReport','status':'partial','groups':groups,'image_bound_evidence':checks,
        'initial_series':{'pass':4,'fail':2,'invalid':2,'required':8},
        'post_warmup_panel':{'pass':1,'fail':1,'invalid':0,'required':2},
        'full_3a_acceptance':False,'independent_visual_review':'incomplete',
        'ui_check':read_json(ROOT/'artifacts/phase3c/ui-check-04/result.json'),
        'real_ui_kiro':read_json(user) if user.exists() else {'status':'running'},
        'historical_acceptance_changed':False,'thresholds_changed':False,
        'producer_sha256':file_sha256(Path(__file__))}
    # Public viewer data has relative artifacts and no local workspace paths.
    public=_portable({k:v for k,v in data.items() if k!='ui_check'})
    if user.exists() and read_json(user).get('session'):
        ui_report=Path(read_json(user)['session'])/'report'
        if ui_report.is_dir():shutil.copytree(ui_report,output/'user-flow')
    shutil.copyfile(ROOT/'artifacts/phase3c/ui-check-04/desktop.png',output/'ui-check.png')
    (output/'data.json').write_text(json.dumps(public,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    write_json(output/'result.json',data)
    page=PAGE.replace('__DATA__',json.dumps(public,ensure_ascii=True,allow_nan=False).replace('<','\\u003c'))
    (output/'index.html').write_text(page,encoding='utf-8')
    lines=['# Phase 3C — User flow and new interaction checks','',
        '**Acceptance is partial.** Production repairs and the local user flow were applied. The earlier Kiro code was kept sealed; it was not edited for these additional inputs.','',
        '## New inputs','',
        '| Domain/input | Result |','|---|---|']
    for g in groups:lines.append('| '+g['domain']+' / '+g['scenario']+(' (after warm-up repair)' if not g['initial_series'] else '')+' | '+g['status']+' |')
    lines += ['','The first registered set is preserved as 4 passed / 2 behavior mismatches / 2 recording failures. The first-screenshot timeout of the panel before the experiment was re-tested with a separate repair and separate recordings. These two new panel recordings do not replace the old failures.','',
        '- Game: the 8-step press passed at two positions. The 14-step press failed at two positions: curve improvement 0%, height difference 24.75%. The success on the previously given short clip cannot be generalized to all press durations.','',
        '- Panel: drag-and-drop passed. Early close improved markedly on three objects (81.44–98.87%); the curve error of the star exceeded the 3% limit at about 6.08%. This is a real behavior mismatch.','',
        '- SVG: 3→5 and 6→4 nodes passed at different screen widths; the source/node/connection checks were assessed separately from the same measurement.','',
        '## Repairs and checks','',
        '- In the independent event helper, the v2/v3 rest definition, rest duration and time resolution were separated. The panel diagnosis resolved the count mismatches; two old baseline event boundaries and a 123.68 ms uncertainty remain open. This alone is not a new independent visual acceptance.','',
        '- When delivering results, the local worker waits for its own processes/log file to close. Reconnect, cancel and PID identity checks are preserved.','',
        '- The warm-up of the first screenshot takes at most 2 seconds before the recording clock. The warm-up image cannot be a measurement frame; the 120 ms limit of the active recording and all acceptance thresholds stayed the same.','',
        '- The user screen takes the declared input time from the frame, preserves the mapping, shows candidate/job progress read-only and separates the eight acceptance dimensions. 12 real headless UI checks passed. A synthetic UI image is not evidence of motion success.','',
        '- 95 relevant regression tests passed (54 worker/input/shared-executor + 41 recording/event/UI tests). The historical broad test set was not re-run. The Windows file lock in the first test and three test setup/automation errors were retained.','',
        '## Limits','',
        'The successful examples in the new inputs cover three domains; the full game chain, the full 3A matrix and the independent review of all the new frames are not complete. No held-out validation or new acceptance result was added to the old 3B sessions. No external user, IDE, AWS resource or license communication was opened in this phase.','',
        'Presentation videos are produced from the real PNG times. The player is aligned to the first input; the multi-input acceptance calculation is aligned separately to each real input. A presentation video does not replace the original image/timestamp evidence.','',
        '[Report with visuals](index.html) · [Raw report](result.json).']
    real=data['real_ui_kiro']
    lines += ['', '## Real UI → Kiro CLI run', '',
        'Model: Opus 5.5/max; native Always Allow; local analysis. Result: '+str(real.get('status'))+
        '; native audit: '+str(real.get('native_audit_status','pending'))+'.', '',
        'The previously verified object selection was reused only on the same image digests. A new chat was opened on a clean target; the old solution or the results of the new interaction tests were not given to Kiro. There is no human solution patch or live steering.', '',
        '[Real run report](user-flow/index.html) · [UI check screenshot](ui-check.png).']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    write_json(output/'integrity.json',{'files':{p.relative_to(output).as_posix():file_sha256(p) for p in output.rglob('*') if p.is_file()}})
    print({'report':str(output/'index.html'),'groups':len(groups),'videos':len(checks)})


PAGE='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KiroClops · New interactions</title><style>
*{box-sizing:border-box}body{margin:0;background:#101827;color:#e7eefb;font:16px system-ui}main{max-width:1200px;margin:auto;padding:28px}p{line-height:1.6;color:#bacbe0}h1{font-size:32px}.notice{background:#263443;padding:20px;border-left:4px solid #ffcd6d}button,select,input{font:inherit;background:#1b2c45;color:white;border:1px solid #61728d;border-radius:6px;padding:9px;max-width:100%}button{cursor:pointer}button:focus,select:focus,input:focus{outline:2px solid #b5d4ff}a{color:#b3d0ff}.controls{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.media{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.card{background:#1b293b;padding:14px;border-radius:12px}video{width:100%;image-rendering:auto;background:#02060c}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px monospace}svg{width:100%;height:auto;background:#142034}#timeline{width:100%}@media(max-width:720px){main{padding:16px}.media{grid-template-columns:1fr}h1{font-size:26px}}</style>
<main><p>KiroClops / Phase 3C</p><h1>Same code. New interaction.</h1>
<div class="notice"><strong>Result is partial</strong><p>First set: 4 passed, 2 behavior mismatches, 2 recording failures. Separate panel check after the warm-up repair: 1 passed, 1 behavior mismatch. The failures are preserved; this report is not full 3A acceptance.</p></div>
<p>The real character, panel and SVG code that Kiro wrote earlier was kept sealed. The new inputs were measured; these results were not fed back into the editing loop.</p>
<label>Scenario <select id="scenario"></select></label><div class="controls"><button id="play">Play together</button><button id="pause">Pause</button><button id="back">Previous reference frame</button><button id="next">Next reference frame</button><output id="time"></output></div>
<label>Time relative to the first input <input id="timeline" type="range" step=".001" value="0"></label>
<div id="media" class="media"></div><h2 id="verdict"></h2><p id="note"></p><p><a id="evidence">Evidence for this scenario</a></p>
<h2>Curve measured from the image</h2><label>Object <select id="object"></select></label><svg id="plot" viewBox="0 0 1080 300" role="img" aria-label="Reference, before and after motion curves"></svg><p id="units"></p>
<details><summary>Numerical checks and preserved properties</summary><pre id="metrics"></pre></details>
<h2>User flow and limits</h2><p>Choosing the input time from a frame, preserving the object mapping, a new session and reconnecting were tested in a headless browser. The new real Kiro CLI run is separate evidence. The model finishing, the motion passing and the independent visual review are separate results.</p>
<p>The 8-step jump passed; with 14 steps the long-press behavior was not transferred to the reference. Panel drag-and-drop passed; early close exceeded the limit. SVG 3→5 and 6→4 passed. The independent review of the new images is missing.</p>
<p id="real-ui"></p><p><a href="user-flow/index.html">New real UI → Kiro CLI report</a> · <a href="ui-check.png">UI check</a></p>
<p><a href="REPORT.md">Technical result</a> · <a href="game.diff">Game code diff</a> · <a href="panel.diff">Panel code diff</a> · <a href="svg.diff">SVG code diff</a></p>
<p>The videos are for presentation. Real frame times are preserved in the analysis; a measurement video is not accepted by replaying the same motion.</p></main>
<script>const D=__DATA__,el=id=>document.getElementById(id);let group,t=0,playing=false,stamp=0;const colors={reference:'#80b5ff',before:'#ffcc76',after:'#7ce0b5'},names={reference:'Reference',before:'Before',after:'After Kiro'};
for(let [i,g] of D.groups.entries())el('scenario').add(new Option(g.domain+' · '+g.scenario+' · '+g.status+(g.initial_series?'':' · after warm-up repair'),i));el('real-ui').textContent='New real Kiro run: '+(D.real_ui_kiro.status||'unknown')+' · native audit: '+(D.real_ui_kiro.native_audit_status||'pending')+' · remaining reviews are shown separately.';
function seek(force=true){el('time').textContent=t.toFixed(3)+' s';el('timeline').value=t;for(let [role,r] of Object.entries(group.roles)){let v=el('v-'+role);if(v&&v.readyState>=1){let raw=t+r.input_origin_s,target=Math.max(0,Math.min(v.duration,raw));if(force||Math.abs(v.currentTime-target)>.08)v.currentTime=target;if(playing&&raw>=0&&raw<v.duration){if(v.paused)v.play().catch(()=>stop())}else v.pause()}}}
function stop(){playing=false;for(let v of document.querySelectorAll('video'))v.pause()}
function plot(){let oid=el('object').value,curves=Object.entries(group.roles).map(([role,r])=>({role,points:r.curves.find(c=>c.id===oid)?.points||[]})),points=curves.flatMap(c=>c.points);el('plot').replaceChildren();if(!points.length)return;let xs=points.map(p=>p[0]),ys=points.map(p=>p[1]),xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);function node(name,attrs,text){let n=document.createElementNS('http://www.w3.org/2000/svg',name);for(let [k,v] of Object.entries(attrs))n.setAttribute(k,v);n.textContent=text||'';el('plot').append(n)};node('line',{x1:30,y1:255,x2:1050,y2:255,stroke:'#788ca8'});for(let [i,c] of curves.entries()){node('path',{d:c.points.map((p,j)=>(j?'L':'M')+(30+1020*(p[0]-xmin)/(xmax-xmin||1)).toFixed(2)+' '+(255-215*(p[1]-ymin)/(ymax-ymin||1)).toFixed(2)).join(' '),stroke:colors[c.role],fill:'none','stroke-width':2});node('text',{x:30+i*280,y:20,fill:colors[c.role]},names[c.role])}el('units').textContent=(Object.values(group.roles)[0]?.curve_units||'')+' · Aligned to the first input; real time is preserved.'}
function choose(){stop();group=D.groups[+el('scenario').value];el('media').replaceChildren();let all=[];for(let [role,r] of Object.entries(group.roles)){let card=document.createElement('section');card.className='card';let h=document.createElement('h2');h.textContent=names[role];let v=document.createElement('video');v.id='v-'+role;v.src=r.video;v.preload='metadata';v.muted=true;v.addEventListener('loadedmetadata',()=>seek());let p=document.createElement('p');p.textContent=r.frame_count+' real frames / '+r.unique_images+' distinct images';card.append(h,v,p);el('media').append(card);all.push(...r.frame_times)}el('timeline').min=Math.min(...all,0);el('timeline').max=Math.max(...all,0);t=0;el('verdict').textContent={pass:'The measurement checks of this scenario passed',fail:'The behavior thresholds were not met',invalid:'Recording invalid; behavior was not scored'}[group.status];el('note').textContent=group.error?.message||'The independent visual review of the new frames is separate and not yet complete.';el('evidence').href=group.evidence;el('metrics').textContent=JSON.stringify(group.comparison||group.error,null,2);el('object').replaceChildren();for(let c of Object.values(group.roles)[0]?.curves||[])el('object').add(new Option(c.id,c.id));el('units').textContent='';plot();seek()}
el('scenario').onchange=choose;el('object').onchange=plot;el('pause').onclick=stop;el('play').onclick=()=>{playing=true;stamp=performance.now();seek(false)};el('timeline').oninput=()=>{stop();t=+el('timeline').value;seek()};function step(direction){stop();let fs=group.roles.reference?.frame_times||[];let candidates=fs.filter(f=>direction>0?f>t+.0001:f<t-.0001);if(candidates.length)t=direction>0?candidates[0]:candidates.at(-1);seek()}el('back').onclick=()=>step(-1);el('next').onclick=()=>step(1);function tick(now){if(playing){t+=(now-stamp)/1000;stamp=now;if(t>=+el('timeline').max){t=+el('timeline').max;stop()}seek(false)}requestAnimationFrame(tick)}choose();requestAnimationFrame(tick);</script></html>'''


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--refresh-presentation',action='store_true')
    a=p.parse_args();(refresh_presentation if a.refresh_presentation else build)(a.output.resolve())
