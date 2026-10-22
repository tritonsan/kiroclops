"""Package the supplemental SVG result without modifying the sealed trial."""
from pathlib import Path
import argparse
import json
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256
from kiroclops.local_engine.reporting import _media
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.review import validate_source_review

HTML='''<!doctype html><html lang="en"><meta charset="utf-8"><title>KiroClops · SVG transfer</title>
<style>body{font:16px system-ui;background:#101827;color:#e5edf9;margin:28px}h1{margin-bottom:8px}p{max-width:1000px;line-height:1.5}a{color:#83c5ff}button,select,input{font:inherit;margin:5px;padding:6px;background:#25364d;color:inherit;border:1px solid #647c98;border-radius:5px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.card{background:#18253a;padding:12px;border-radius:9px}img,video{width:100%}table{border-collapse:collapse;width:100%;margin:20px 0}th,td{padding:9px;border-bottom:1px solid #354861;text-align:left}svg{width:100%;height:230px;background:#142035}pre{white-space:pre-wrap;max-height:350px;overflow:auto}.ok{color:#83e4b1}.bad{color:#ff9e97}#scrub{width:60%}small{color:#adc0d7}@media(max-width:800px){.grid{grid-template-columns:1fr}}</style>
<h1>SVG motion transfer</h1><p id="headline"></p>
<p id="run"></p><p>The first separate run was cut off by a contract-binding error and is preserved. This page shows the second run, made with the corrected tooling.</p>
<p><strong>Independent visual review of the final candidate:</strong> <span id="visual"></span>. The numerical result, the source/function check and the independent visual acceptance are assessed separately. This report does not claim that the 18-trial comparative study is complete.</p>
<p><a href="candidate.diff">Kiro's code diff</a> · <a href="validation.json">Numerical result</a> · <a href="source-review.json" id="source">Source review</a></p>
<table><thead><tr><th>Scenario</th><th>Result</th><th>Curve error</th><th>Improvement</th></tr></thead><tbody id="rows"></tbody></table>
<select id="scenario"></select><button id="play">Play together</button><button id="pause">Pause</button><button id="next">Next raw frame</button><br>
<input id="scrub" type="range" step="0.001"><span id="clock"></span>
<p><small>Raw images are shown with their own real timestamps, aligned to the first input. No interpolated images are generated between frames. The videos are for presentation; the measurement was made from the raw PNGs.</small></p>
<div class="grid" id="images"></div><p>Node to track: <select id="object"></select> Axis: <select id="axis"><option>x</option><option>y</option></select></p><svg id="curve" viewBox="0 0 1000 230"></svg>
<p><small>Blue: reference · orange: before · green: after Kiro. The vertical scale is shared by the three series; position is in native image pixels. The error in the table is a comparison score normalized by the reference motion distance.</small></p><details><summary>Details of the selected scenario</summary><pre id="details"></pre></details>
<script>const D=__DATA__,roles=['reference','before','candidate'],labels=['Reference','Before','After Kiro'],colors=['#74bdff','#ffb574','#6ae5a2'];let index=0,playing=false,start=0,base=0,t=0;const el=id=>document.getElementById(id);el('headline').textContent=`Held-out validation: ${D.result.passed}/${D.result.required}. Adding, removing and dragging of nodes not seen in calibration; two screen widths and two repeats.`;el('visual').textContent=D.visual;el('source').hidden=!D.sourceReview;D.cases.forEach((c,i)=>{const o=document.createElement('option');o.value=i;o.textContent=c.id;el('scenario').append(o);const tr=document.createElement('tr');[c.id,c.pass?'Passed':'Failed',c.score==null?'—':c.score.toFixed(5),c.improvement==null?'—':(c.improvement*100).toFixed(2)+'%'].forEach((s,k)=>{let td=document.createElement('td');td.textContent=s;if(k===1)td.className=c.pass?'ok':'bad';tr.append(td)});el('rows').append(tr)});
function data(){return D.cases[index]}function seek(v){t=v;el('scrub').value=t;el('clock').textContent=t.toFixed(3)+' s';for(const role of roles){const m=data().media[role];if(!m)continue;let f=m.frames[0];for(const q of m.frames){if(q.time_s-m.input_origin_s>t)break;f=q}const img=el('img-'+role);if(img.dataset.path!==f.path){img.src=f.path;img.dataset.path=f.path}el('time-'+role).textContent=(f.time_s-m.input_origin_s).toFixed(4)+' s'} }
function select(){playing=false;el('images').replaceChildren();el('object').replaceChildren();const c=data();roles.forEach((role,i)=>{let card=document.createElement('section');card.className='card';let h=document.createElement('h3');h.textContent=labels[i];card.append(h);const m=c.media[role];if(m){let img=document.createElement('img');img.id='img-'+role;img.alt=labels[i];card.append(img);let small=document.createElement('small');small.id='time-'+role;card.append(small);if(m.video){let d=document.createElement('details'),s=document.createElement('summary'),v=document.createElement('video');s.textContent='Presentation video';v.controls=true;v.preload='none';v.src=m.video;d.append(s,v);card.append(d)}}el('images').append(card)});const media=Object.values(c.media);if(!media.length)return;el('scrub').min=Math.max(...media.map(m=>m.frames[0].time_s-m.input_origin_s));el('scrub').max=Math.min(...media.map(m=>m.frames.at(-1).time_s-m.input_origin_s));for(const o of c.media.reference?.objects||[]){let x=document.createElement('option');x.value=o.id;x.textContent=o.id;el('object').append(x)}el('details').textContent=JSON.stringify(c.summary,null,2);seek(Number(el('scrub').min));plot()}
function plot(){const svg=el('curve');svg.replaceChildren();let all=[],series=[];for(const role of roles){const m=data().media[role],o=m?.objects?.find(o=>o.id===el('object').value);let points=(o?.frames||[]).filter(f=>f.valid&&f.present!==false).map(f=>[f.time_s-m.input_origin_s,f[el('axis').value]]);series.push(points);all.push(...points)}if(!all.length)return;const low=Math.min(...all.map(p=>p[1])),high=Math.max(...all.map(p=>p[1])),a=Number(el('scrub').min),b=Number(el('scrub').max);series.forEach((points,i)=>{let p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d',points.map((v,j)=>`${j?'L':'M'}${20+960*(v[0]-a)/(b-a)},${210-190*(v[1]-low)/Math.max(1,high-low)}`).join(' '));p.setAttribute('fill','none');p.setAttribute('stroke',colors[i]);p.setAttribute('stroke-width','2');svg.append(p)})}
function tick(now){if(!playing)return;const v=base+(now-start)/1000;seek(Math.min(v,Number(el('scrub').max)));if(v>=Number(el('scrub').max))playing=false;else requestAnimationFrame(tick)}el('play').onclick=()=>{if(t>=Number(el('scrub').max))seek(Number(el('scrub').min));if(playing)return;playing=true;start=performance.now();base=t;requestAnimationFrame(tick)};el('pause').onclick=()=>playing=false;el('next').onclick=()=>{playing=false;const m=data().media.reference;if(m){const f=m.frames.find(f=>f.time_s-m.input_origin_s>t+.00001);if(f)seek(f.time_s-m.input_origin_s)}};el('scrub').oninput=()=>{playing=false;seek(Number(el('scrub').value))};el('scenario').onchange=()=>{index=Number(el('scenario').value);select()};el('object').onchange=plot;el('axis').onchange=plot;select();</script></html>'''

def run(session,validation,output,source_review=None):
    session,validation,output=map(lambda p:Path(p).resolve(),(session,validation,output))
    if output.exists():raise ValueError('Fresh report output required')
    result=read_json(validation/'validation.json')
    if source_review:
        validate_source_review(source_review,directory=session,candidate_sha256=result['candidate_source_sha256'])
    state=read_json(session/'session.json');launch=read_json(session/'kiro/launch.json')
    run_info={'model':launch['model'],'effort':launch['effort'],'approval_mode':launch['approval_mode'],
        'candidate_count':len(state['candidates']),'authoring_seconds':launch['response_received_at']-launch['prompt_started_at']}
    output.mkdir(parents=True);configure_environment()
    data={'result':result,'cases':[],'visual':'Pending','sourceReview':bool(source_review),'run':run_info}
    for row in result['results']:
        sid=row['scenario_id'];full=read_json(validation/'results'/(sid+'.json'));media={}
        for role in ('reference','before','candidate'):
            p=validation/'records'/sid/(role+'.json')
            if p.exists():
                print('package',sid,role,flush=True);media[role]=_media(read_json(p),output,'media/'+sid+'/'+role,True)
        data['cases'].append({'id':sid,'pass':row['pass'],'score':row['score'],'improvement':row['improvement'],
            'media':media,'summary':{k:v for k,v in full.items() if k!='records'}})
    shutil.copy2(session/'report/candidate.diff',output/'candidate.diff');shutil.copy2(validation/'validation.json',output/'validation.json')
    if source_review:shutil.copy2(source_review,output/'source-review.json')
    write_json(output/'report.json',data)
    page=HTML.replace('__DATA__',json.dumps(data,ensure_ascii=False).replace('</','<\\/'))
    page=page.replace('Held-out validation:', 'Numerical held-out validation:')
    page=page.replace("el('visual').textContent=D.visual;", "el('run').textContent=`${D.run.model} / ${D.run.effort} · Kiro CLI · native Always Allow · ${D.run.candidate_count} candidates · ${(D.run.authoring_seconds/60).toFixed(2)} minutes.`;el('visual').textContent=D.visual;")
    page=page.replace("el('details').textContent=JSON.stringify", "if([...el('object').options].some(o=>o.value==='c'))el('object').value='c';el('details').textContent=JSON.stringify")
    (output/'index.html').write_text(page,encoding='utf-8')
    write_json(output/'receipt.json',{'validation_sha256':file_sha256(validation/'validation.json'),'report_sha256':file_sha256(output/'report.json'),
        'page_sha256':file_sha256(output/'index.html'),'independent_visual_review':'pending','original_session_modified':False})
    print(output/'index.html',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('session','validation','output'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--source-review',type=Path);a=p.parse_args();run(a.session,a.validation,a.output,a.source_review)
