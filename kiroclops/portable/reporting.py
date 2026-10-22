"""Static, local-only reports with native frames, measured curves and authored diffs."""
from __future__ import annotations
import html
import json
from pathlib import Path
import shutil
import subprocess
from kiroclops.capture import process_options
from kiroclops.integration.storage import read_json, write_json, sha256


def encode_video(run, destination):
    run, destination = Path(run), Path(destination)
    manifest = read_json(run/'manifest.json')
    if manifest.get('frame_count') != 240 or len(manifest.get('frames', [])) != 240:
        raise ValueError('Only complete 240-frame captures may be presented as videos')
    source = [sha256(run/'frames'/f'{i:06}.png') for i in range(240)]
    binding = destination.with_suffix('.source.json')
    if destination.is_file():
        record = read_json(binding)
        if record != {'frames': source, 'video_sha256': sha256(destination)}:
            raise ValueError('Existing video differs from its bound native frames')
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('FFmpeg is required for local report video encoding')
    result = subprocess.run([ffmpeg, '-v', 'error', '-n', '-framerate', '60', '-i', str(run/'frames/%06d.png'),
        '-frames:v', '240', '-c:v', 'libx264', '-crf', '16', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
        str(destination)], capture_output=True, text=True, timeout=90, **process_options())
    if result.returncode:
        raise RuntimeError(result.stderr[-1500:])
    write_json(binding, {'frames': source, 'video_sha256': sha256(destination)})
    return destination


STYLE = '''body{font:16px system-ui;margin:0;background:#0d1320;color:#e5ebf5}main{max-width:1400px;margin:auto;padding:28px}
h1,h2{color:#b9dcff}section{background:#182235;padding:20px;margin:20px 0;border-radius:12px}
table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #3d4b66;text-align:left}
a{color:#9dccff}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#101826;padding:14px;font-size:13px}
.videos{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}video{width:100%;image-rendering:pixelated;background:black}
button,select{padding:8px;margin:4px;background:#28405d;color:white;border:1px solid #6b829d;border-radius:6px}
canvas{width:100%;height:220px;background:#101826}.note{color:#c3cbd8}.pass{color:#92dfad}.fail{color:#ffadad}'''


def _document(title, body):
    return f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{STYLE}</style><main><h1>{html.escape(title)}</h1>{body}</main></html>'


def _scenario_key(report):
    s = report['scenario']
    return f"{s.get('id') or 'h'+str(s.get('hold_ticks')).zfill(2)}-x{s.get('spawn_offset',0)}-r{s['repeat']}"


def build_session_report(directory):
    directory = Path(directory).resolve()
    state = read_json(directory/'session.json')
    report = directory/'report'
    report.mkdir(exist_ok=True)
    final_path = directory/'final/comparison.json'
    final = read_json(final_path) if final_path.is_file() else None
    audit = read_json(directory/'final/independent-review.json') if (directory/'final/independent-review.json').is_file() else None
    known_failure = ((final and any(final.get(k) == 'fail' for k in ('numerical_status','automatic_integrity_status','normal_gameplay_status')))
                     or audit and audit.get('status') == 'fail')
    if known_failure:
        final_status = 'fail'
    elif audit and audit.get('status') == 'pass':
        from .audit import verify_review
        final_status = verify_review(directory)['status']
    else:
        final_status = 'pending'
    body = f'<p>CLI / GPT-5.6 Sol / max · {html.escape(state["experiment_id"])}</p><p class="{final_status}">Independent acceptance: {final_status}</p>'
    body += '<p class="note">The numerical result and the independent visual/source acceptance are separate. A prepared reference and a one-time game connection are used.</p>'
    attempts = [{'candidate': c['candidate_id'], 'status': c['status'],
                 'eligible': c.get('feedback', {}).get('eligible'), 'rmse': c.get('feedback', {}).get('mean_rmse_body'),
                 'integrity': c.get('feedback', {}).get('automatic_integrity_status'), 'error': c.get('error')}
                for c in state['candidates']]
    body += '<section><h2>Candidate history</h2><pre>'+html.escape(json.dumps(attempts, ensure_ascii=False, indent=2))+'</pre></section>'
    rows = []
    if final:
        body += '<section><h2>Acceptance summary</h2><pre>'+html.escape(json.dumps({
            'numerical_status': final['numerical_status'], 'automatic_integrity_status': final['automatic_integrity_status'],
            'measurement_status': final.get('measurement_status'), 'normal_gameplay_status': final.get('normal_gameplay_status'),
            'selected_candidate': state.get('selected_candidate_id'), 'source_sha256': final.get('source_sha256')}, ensure_ascii=False, indent=2))+'</pre></section>'
        collections = [('validation',mechanic,sources) for mechanic,sources in final.get('runs',{}).items()]
        chosen = next((c for c in state['candidates'] if c['candidate_id']==state.get('selected_candidate_id')),None)
        if chosen:
            feedback=chosen.get('feedback',{})
            for mechanic,refs in state.get('reference',{}).items():
                after=feedback.get('calibration_runs' if mechanic==state.get('experiment_spec',{}).get('stage') else 'jump_calibration_runs')
                if after:
                    collections.append(('calibration',mechanic,{'reference':refs,'before':state['baseline_runs'][mechanic],'after':after}))
        for split, mechanic, sources in collections:
            role_maps = {role: {_scenario_key(r): r for r in [read_json(p) for p in paths]}
                         for role, paths in sources.items()}
            for key in sorted(role_maps['reference']):
                item = {'id': split+'-'+mechanic+'-'+key, 'stage': mechanic, 'split':split, 'videos': {}, 'curves': {}}
                for role, mapping in role_maps.items():
                    data = mapping[key]
                    run = Path(data['run_id'])
                    path = encode_video(run, report/'media'/f'{split}-{mechanic}-{key}-{role}.mp4')
                    item['videos'][role] = path.relative_to(report).as_posix()
                    takeoff = data.get('events', {}).get('takeoff_frame', 0) if mechanic == 'jump' else 30
                    field = 'height_body' if mechanic == 'jump' else 'x_body'
                    item['curves'][role] = [[(f['frame_index']-takeoff)/60, f.get(field) if f['valid'] else None]
                                           for f in data['frames']]
                rows.append(item)
        normal = Path(final['normal_capture'])
        if (normal/'manifest.json').is_file():
            path = encode_video(normal, report/'media/normal.mp4')
            body += f'<section><h2>Normal level</h2><video controls src="{path.relative_to(report).as_posix()}"></video></section>'
        slope = Path(final.get('slope_capture',str(directory/'final/slope')))
        if (slope/'manifest.json').is_file():
            path=encode_video(slope,report/'media/slope.mp4')
            body += f'<section><h2>Walking on a sloped floor</h2><video controls src="{path.relative_to(report).as_posix()}"></video></section>'
        if rows:
            options = ''.join(f'<option value="{i}">{html.escape(r["id"])}</option>' for i,r in enumerate(rows))
            body += '<section><h2>Reference / before / after</h2><select id="scenario">'+options+'</select><button id="play">Play together</button><button id="pause">Pause</button><button id="frame">+1 frame</button><div class="videos">'
            body += ''.join(f'<div><p>{label}</p><video id="{role}" controls muted></video></div>' for role,label in (('reference','Reference'),('before','Before'),('after','After')))
            body += '</div><p class="note">Videos are aligned to the input. Jump curves are aligned to takeoff and horizontal curves to the input; the scale is body height.</p><canvas id="curve" width="1200" height="220"></canvas></section>'
            data = json.dumps(rows, ensure_ascii=False).replace('<', '\\u003c')
            body += '<script>const runs='+data+''';const roles=['reference','before','after'];const vids=roles.map(x=>document.getElementById(x));
function load(){const r=runs[+document.getElementById('scenario').value];vids.forEach((v,i)=>{v.src=r.videos[roles[i]];v.currentTime=0});const c=document.getElementById('curve'),g=c.getContext('2d');g.clearRect(0,0,c.width,c.height);const pts=Object.values(r.curves).flat().filter(p=>p[1]!==null),lo=Math.min(0,...pts.map(p=>p[1])),hi=Math.max(1,...pts.map(p=>p[1]));g.fillStyle='#bac8da';g.fillText('body / second',10,15);roles.forEach((role,k)=>{g.strokeStyle=['#93c8ff','#ffb693','#9fe1ad'][k];g.beginPath();let pen=false;r.curves[role].forEach(p=>{if(p[1]===null){pen=false;return}let x=45+(p[0]+.5)*250,y=200-(p[1]-lo)/(hi-lo)*170;if(pen)g.lineTo(x,y);else g.moveTo(x,y);pen=true});g.stroke()})}
document.getElementById('scenario').onchange=load;document.getElementById('play').onclick=()=>{let t=vids[0].currentTime;vids.forEach(v=>{v.currentTime=t;v.play()})};document.getElementById('pause').onclick=()=>vids.forEach(v=>v.pause());document.getElementById('frame').onclick=()=>{let t=Math.min(239/60,vids[0].currentTime+1/60);vids.forEach(v=>{v.pause();v.currentTime=t})};load();</script>'''
        cid = state.get('selected_candidate_id')
        diff = directory/'candidates'/cid/'changes.diff' if cid else None
        if cid:
            candidate = read_json(directory/'candidates'/cid/'candidate.json')
            diff = Path(candidate.get('diff_path', diff))
        if diff and diff.is_file():
            body += '<section><h2>Code diff by Kiro</h2><pre>'+html.escape(diff.read_text(encoding='utf-8'))+'</pre></section>'
    else:
        body += '<p>There is no completed final validation for this session.</p>'
    path = report/'index.html'
    path.write_text(_document(state['session_id'], body), encoding='utf-8')
    write_json(report/'summary.json', {'session_id': state['session_id'], 'status': final_status,
        'attempts': attempts, 'scenario_count': len(rows), 'model': 'gpt-5.6-sol', 'effort': 'max'})
    return path


def build_cohort_report(registry_path):
    from .cohort import verify_registry
    registry = verify_registry(registry_path)
    root = Path(registry['root'])
    output = root/'artifacts/phase2c/reports'/registry['cohort_id']
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for chain in registry['chains']:
        states = {}
        for stage in ('jump', 'ground'):
            directory = root/'sessions'/chain[f'{stage}_session']
            if not (directory/'session.json').is_file():
                states[stage] = {'status': 'not_run'}
                continue
            report = build_session_report(directory)
            target = output/chain[f'{stage}_session']
            if target.exists():
                shutil.copytree(report.parent, target, dirs_exist_ok=True)
            else:
                shutil.copytree(report.parent, target)
            states[stage] = read_json(report.parent/'summary.json')
            states[stage]['report'] = target.relative_to(output).as_posix()+'/index.html'
        status = 'pass' if all(s['status'] == 'pass' for s in states.values()) else 'fail' if any(s['status'] in ('fail','not_run') for s in states.values()) else 'pending'
        rows.append({'chain_id': chain['chain_id'], 'status': status, 'stages': states})
    body = '<p>Heart Platformer · three independent two-chat CLI chains · Sol/max</p><table><tr><th>Chain</th><th>Jump</th><th>Combined movement</th><th>Acceptance</th></tr>'
    for row in rows:
        body += '<tr><td>'+html.escape(row['chain_id'])+'</td>'
        for stage in ('jump','ground'):
            s = row['stages'][stage]
            body += '<td>'+(f'<a href="{s["report"]}">{s["status"]}</a>' if 'report' in s else s['status'])+'</td>'
        body += '<td>'+row['status']+'</td></tr>'
    body += '</table><p>Failed attempts are not replaced. A result awaiting independent review is not full acceptance.</p>'
    (output/'index.html').write_text(_document('KiroClops — Second target game', body), encoding='utf-8')
    write_json(output/'summary.json', {'cohort_id': registry['cohort_id'], 'chains': rows,
        'passed': sum(r['status']=='pass' for r in rows), 'required': 3})
    return output/'index.html'


def build_readiness_report(qualification_path, connection_path):
    """Show engineering readiness without inventing a model-authored after result."""
    from .specs import ARTIFACTS
    qualification=read_json(qualification_path); connection=read_json(connection_path)
    output=ARTIFACTS/'readiness'; output.mkdir(parents=True,exist_ok=True)
    body='<p>Second target: Heart Platformer. This page shows the local readiness.</p>'
    body+='<section><h2>Real Kiro runs</h2><pre>'+html.escape(json.dumps(connection,ensure_ascii=False,indent=2))+'</pre></section>'
    body+='<section><h2>Local qualification</h2><table><tr><th>Check</th><th>Result</th></tr>'
    for gate,value in qualification['gates'].items():
        body+=f'<tr><td>{html.escape(gate)}</td><td>{html.escape(value["status"])}</td></tr>'
    body+='</table><p>The final candidate produced by Kiro and the held-out transfer acceptance are kept separate from this readiness check.</p></section>'
    for stage,groups in qualification['calibration_analyses'].items():
        chosen={role:{_scenario_key(r):r for r in [read_json(p) for p in paths] if r['scenario']['repeat']==1} for role,paths in groups.items()}
        for key in sorted(chosen['reference']):
            body+=f'<section><h2>{html.escape(stage+" / "+key)}</h2><div class="videos">'
            for role,label in (('reference','Tiny Crate reference'),('before','Original Heart baseline')):
                data=chosen[role][key]
                video=encode_video(Path(data['run_id']),output/'media'/f'{stage}-{key}-{role}.mp4')
                body+=f'<div><p>{label}</p><video controls src="{video.relative_to(output).as_posix()}"></video></div>'
            body+='</div></section>'
    (output/'index.html').write_text(_document('KiroClops — Heart readiness status',body),encoding='utf-8')
    write_json(output/'summary.json',{'local_qualification':qualification['status'],
        'model_connection':connection,'qualification_sha256':sha256(qualification_path),
        'connection_sha256':sha256(connection_path),'model_motion_acceptance':'not_run'})
    return output/'index.html'
