"""Build a presentation-only SVG viewer from an immutable visual export.

One case is loaded at a time. Existing MP4s are linked and byte-bound, never
encoded or copied. No initial-measurement replay, model, capture or acceptance.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import html
import json
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.session import _ordinary
from kiroclops.local_engine.source_inventory import full_byte_inventory
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json

ROLES = ('reference', 'original', 'before', 'unchanged')
VERSION = 1


def _need(value, message):
    if not value:
        raise VisualError('invalid_svg_presentation_view', message)


def project(report):
    """Keep every saved numeric gate and actual curve; reduce relation display."""
    _need(report.get('kind') == 'NoCodeChangeSVGVisualReport'
          and report.get('full_behavior_acceptance') is False
          and report.get('acceptance_granted_by_report') is False
          and report.get('model_started') is False and report.get('new_model_generation') is False
          and report.get('candidate_count') == 0, 'Source must be the unchanged presentation, without acceptance.')
    groups = report.get('groups')
    _need(isinstance(groups, list) and groups, 'Missing source case matrix.')
    cases = {group['case_id']: group for group in groups}
    _need(len(cases) == len(groups) and report['required_record_count'] == len(groups) * len(ROLES),
          'Source case matrix is duplicated or incomplete.')
    bindings = {(row['case_id'], row['role']): row for row in report['record_bindings']}
    missing = {(row['case_id'], row['role']): row for row in report['missing_records']}
    expected = {(case_id, role) for case_id in cases for role in ROLES}
    _need(len(bindings) == len(report['record_bindings']) == report['record_count']
          and len(missing) == len(report['missing_records']) and not set(bindings).intersection(missing)
          and set(bindings).union(missing) == expected, 'Every role slot must be bound or explicitly unavailable.')
    summaries, bodies, slots = [], [], []
    for index, group in enumerate(groups):
        case_id = group['case_id']
        _need(set(group['roles']) == {role for role in ROLES if (case_id, role) in bindings}
              and set(group['unavailable_roles']) == {role for role in ROLES if (case_id, role) in missing},
              'A source role was lost or silently replaced.')
        body = {key: deepcopy(group[key]) for key in ('case_id', 'split', 'scenario', 'comparison',
                'parent_preservation', 'comparison_available', 'preservation_available', 'windows',
                'alignment', 'curve_scope', 'unavailable_roles')}
        body['source_json_pointer'] = '/groups/' + str(index)
        body['roles'] = {}
        for role in ROLES:
            key = case_id, role
            if key not in bindings:
                slots.append(deepcopy(missing[key])); continue
            binding = bindings[key]
            media = group['roles'][role]
            _need(media['package_sha256'] == binding['package_sha256']
                  and media['source_sha256'] == binding['source_sha256'], 'Role source/package identity changed.')
            # Keep actual frame points once. Pairwise per-frame relation display
            # is summarized; all numeric relationship/geometry gates remain in
            # the unchanged comparison and preservation dictionaries above.
            view = {key: deepcopy(value) for key, value in media.items() if key != 'relations'}
            counts = {}
            for relation in media.get('relations', []):
                kind = relation.get('type', 'unknown')
                counts[kind] = counts.get(kind, 0) + 1
            view['relation_display_summary'] = {'total': len(media.get('relations', [])), 'counts_by_type': counts,
                'source_json_pointer': '/groups/' + str(index) + '/roles/' + role + '/relations',
                'scope': 'Display count only; no inferred pass/contact. Exact per-frame relations remain in the bound source JSON.'}
            body['roles'][role] = view
            slot = {key: value for key, value in binding.items() if key != 'measurement_source_hashes'}
            slot['status'] = 'available'; slots.append(slot)
        if 'before' in body['roles'] and 'unchanged' in body['roles']:
            _need(body['roles']['before']['source_sha256'] == body['roles']['unchanged']['source_sha256'],
                  'Before and unchanged must retain the same historical source.')
        comparison, preservation = group['comparison'] or {}, group['parent_preservation'] or {}
        failed = [row for row in comparison.get('per_window_gates', []) if row.get('pass') is False]
        summaries.append({'case_id': case_id, 'split': group['split'], 'case_file': 'cases/' + str(index) + '.js',
            'comparison_available': group['comparison_available'], 'preservation_available': group['preservation_available'],
            'comparison_pass': comparison.get('pass'), 'measurement_pass': comparison.get('measurement_pass'),
            'baseline_measurement_pass': comparison.get('baseline_measurement_pass'), 'score': comparison.get('score'),
            'failure_class': comparison.get('failure_class'), 'failed_window_gates': failed,
            'preservation_pass': preservation.get('pass'), 'preservation_failure_class': preservation.get('failure_class'),
            'unavailable_roles': deepcopy(group['unavailable_roles'])})
        bodies.append(body)
    summary = {key: deepcopy(report[key]) for key in ('initial_status', 'initial_complete', 'initial_failure_code',
        'initial_failure_message', 'initial_result_sha256', 'source_sha256', 'numerical_pass', 'measurement_qualified',
        'record_count', 'required_record_count', 'missing_records', 'missing_videos', 'representative_cases', 'role_labels',
        'independent_review_status', 'source_review_status', 'historical_generation', 'historical_generation_scope')}
    summary.update(kind='NoCodeChangeSVGDerivedPresentation', schema_version=VERSION,
        full_behavior_acceptance=False, new_model_generation=False, acceptance_granted=False,
        groups=summaries, role_slots=slots,
        scope='Derived presentation only. Saved numerical failures and incomplete matrix remain unchanged.',
        curve_policy='Actual native-pixel frame points, without smoothing/resampling/filling. Adjacent valid points are joined visually.',
        relation_display_scope='Pairwise per-frame relations are counted for display; all saved numerical gates remain exact in each case.')
    return summary, bodies


def _js(path, name, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('window.' + name + '=' + payload + ';\n', encoding='utf-8')


def build(source, output):
    source, output = map(_ordinary, (source, output))
    _need(not output.exists() and not output.is_relative_to(source) and not source.is_relative_to(output),
          'Use a fresh separate viewer output.')
    report_path, receipt_path = source / 'report.json', source / 'report-binding.json'
    source_files = {str(path): file_sha256(path) for path in (report_path, receipt_path)}
    report, receipt = read_json(report_path), read_json(receipt_path)
    _need(receipt.get('kind') == 'NoCodeChangeSVGVisualReportReceipt'
          and receipt.get('file_hashes', {}).get('report.json') == source_files[str(report_path)]
          and receipt.get('report_sha256') == digest(report), 'Original presentation JSON/receipt changed.')
    _need(len(report['groups']) == 28 and report['required_record_count'] == 112, 'Keep the actual approved full planned matrix.')
    summary, cases = project(report)
    videos = []
    for row in report['videos']:
        path = _ordinary(source / row['path'])
        _need(path.is_relative_to(source) and path.is_file() and file_sha256(path) == row['sha256'], 'An existing video changed.')
        videos.append({key: deepcopy(row[key]) for key in ('case_id', 'role', 'path', 'sha256')})
        videos[-1]['absolute_path'] = str(path); videos[-1]['url'] = path.as_uri()
    available_video_keys = {(row['case_id'], row['role']) for row in videos}
    unavailable_video_keys = {(row['case_id'], row['role']) for row in summary['missing_videos']}
    required_video_keys = {(case_id, role) for case_id in summary['representative_cases'] for role in ROLES}
    _need(len(available_video_keys) == len(videos) and not available_video_keys.intersection(unavailable_video_keys)
          and available_video_keys.union(unavailable_video_keys) == required_video_keys,
          'Fixed video slots may not be lost, replaced or duplicated.')
    for body in cases:
        for role, view in body['roles'].items():
            if 'media' in view:
                matches = [row for row in videos if (row['case_id'], row['role']) == (body['case_id'], role)]
                _need(len(matches) == 1, 'Role media must bind an existing fixed video.')
                # No repeated copied frame/path list: actual curves remain above.
                view['media'] = {'video': matches[0]['url'], 'video_sha256': matches[0]['sha256']}
    summary.update(source_directory=str(source), source_files=source_files, source_json_file_sha256=source_files[str(report_path)],
        source_receipt_file_sha256=source_files[str(receipt_path)], videos=videos,
        script_sha256=file_sha256(Path(__file__)), created_at=time.time())
    _need(all(file_sha256(Path(path)) == expected for path, expected in source_files.items()), 'Source changed during projection.')
    output.mkdir(parents=True, exist_ok=False)
    for index, body in enumerate(cases):
        _js(output / 'cases' / (str(index) + '.js'), 'SVG_CASE', body)
    write_json(output / 'summary.json', summary)
    _js(output / 'summary.js', 'SVG_SUMMARY', summary)
    (output / 'index.html').write_text(_html(summary), encoding='utf-8')
    files = full_byte_inventory(output, ignored=(), excluded_suffixes=())
    write_json(output / 'viewer-binding.json', {'kind': 'NoCodeChangeSVGDerivedPresentationReceipt', 'schema_version':VERSION,
        'summary_sha256':digest(summary), 'file_hashes':files, 'source_files':source_files,
        'script_sha256':summary['script_sha256'], 'acceptance_granted':False, 'full_behavior_acceptance':False})
    return summary


def inspect(output):
    output = _ordinary(output)
    summary, receipt = read_json(output / 'summary.json'), read_json(output / 'viewer-binding.json')
    files = full_byte_inventory(output, ignored=(), excluded_suffixes=())
    files.pop('viewer-binding.json', None)
    _need(receipt.get('kind') == 'NoCodeChangeSVGDerivedPresentationReceipt'
          and receipt.get('file_hashes') == files and receipt.get('summary_sha256') == digest(summary)
          and receipt.get('source_files') == summary['source_files']
          and receipt.get('script_sha256') == summary['script_sha256'] == file_sha256(Path(__file__))
          and summary.get('full_behavior_acceptance') is False and summary.get('acceptance_granted') is False
          and receipt.get('acceptance_granted') is False and receipt.get('full_behavior_acceptance') is False,
          'Derived viewer bytes/scope changed.')
    _need(all(file_sha256(_ordinary(path)) == expected for path, expected in summary['source_files'].items())
          and all(file_sha256(_ordinary(video['absolute_path'])) == video['sha256'] for video in summary['videos']),
          'Original JSON/receipt/video bytes changed.')
    return summary


def _html(summary):
    rows=[]
    for index,g in enumerate(summary['groups']):
        values=[g['case_id'],g['split'],'available' if g['comparison_available'] else 'unavailable',g['comparison_pass'],
            g['measurement_pass'],g['baseline_measurement_pass'],g['score'],g['failure_class'],
            len(g['failed_window_gates']) if g['comparison_available'] else 'unavailable',
            g['preservation_pass'],g['preservation_failure_class'],', '.join(g['unavailable_roles']) or 'none']
        rows.append('<tr data-case="'+str(index)+'">'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Unchanged SVG evidence viewer</title>
<style>body{font:16px system-ui;margin:24px;max-width:1500px;background:#101827;color:#edf2f7}a{color:#a6d4ff}.notice{border:2px solid #ccab58;padding:16px}section{margin:28px 0}.scroll{overflow:auto}table{border-collapse:collapse;font-size:13px}td,th{padding:8px;border:1px solid #40516c;text-align:left;overflow-wrap:anywhere}select{padding:8px;margin:4px}canvas{width:100%;background:white}.roles{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}article{background:#1b2b42;padding:12px}video{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary,tr{cursor:pointer}</style>
<h1>Unchanged SVG evidence viewer</h1><div class="notice"><strong>''' + html.escape(summary['initial_status']) + ''' · saved numerical pass: ''' + str(summary['numerical_pass']) + '''</strong>
<p>Presentation only: '''+str(summary['record_count'])+''' of '''+str(summary['required_record_count'])+''' role recordings available; all 28 planned cases remain visible. The actual attempt timed out. Missing measurements and saved failures remain unchanged. No full acceptance, new model generation or repair is granted.</p>
<p>Before and unchanged use the same historical candidate code. Historical authorship/native evidence, raw visual inspection and functional witness remain separate.</p><p>Private validation is present. Do not expose it to a repair model or reuse it as blind after exposure.</p></div>
<p><a href="summary.json">Summary and all role slots</a> · <a href="viewer-binding.json">Derived viewer byte receipt</a></p>
<section><h2>All 28 planned cases</h2><p>Click a row to load its actual points and exact saved numerical gates. Missing comparisons are unavailable, never inferred passes.</p><div class="scroll"><table><thead><tr><th>Case</th><th>Split</th><th>Comparison</th><th>Pass</th><th>Measured</th><th>Original measured</th><th>Mean score</th><th>Failure</th><th>Failed window gates</th><th>Preservation pass</th><th>Preservation failure</th><th>Unavailable roles</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table></div></section>
<section id="curves"><h2>Actual measured motion curves</h2><p>One case loads at a time. Native pixels at recorded seconds from the matching input anchor. No resampling, smoothing or filled points. Lines join adjacent valid present points. Exact scored/censored bounds remain in the saved numerical details.</p>
<label>Case <select id="case"></select></label><label>Motion <select id="window"></select></label><label>Object <select id="object"></select></label><label>Axis <select id="axis"><option>x</option><option>y</option><option>width</option><option>height</option></select></label><p id="loaded"></p><p id="legend"></p><canvas id="plot" width="1200" height="420" aria-label="Recorded motion points"></canvas>
<details><summary>Exact saved comparison and preservation gates</summary><pre id="gates"></pre></details><details><summary>Actual events and relation display scope</summary><pre id="events"></pre></details></section>
<section><h2>Three fixed representative videos</h2><p>Existing immutable videos: new add, new drag, new delete at width 1280, repeat 1. No substitute is selected for the unavailable unchanged delete recording.</p><div id="videos"></div></section>
<section><details><summary>Source and historical scope</summary><pre id="scope"></pre></details></section>
<script src="summary.js"></script><script>
const D=window.SVG_SUMMARY,$=id=>document.getElementById(id),colors={reference:'#1149a1',original:'#db6b06',before:'#7a3197',unchanged:'#00805d'};let G=null,epoch=0;
function option(s,v,t){const o=document.createElement('option');o.value=v;o.textContent=t;s.append(o)}D.groups.forEach((g,i)=>option($('case'),i,g.case_id));
function choose(){const index=+$('case').value,ticket=++epoch;G=null;$('loaded').textContent='Loading selected case…';const script=document.createElement('script');script.src=D.groups[index].case_file;script.onload=()=>{if(ticket!==epoch){script.remove();return}G=window.SVG_CASE;script.remove();$('window').replaceChildren();$('object').replaceChildren();G.windows.forEach((w,i)=>option($('window'),i,w.id));[...new Set(Object.values(G.roles).flatMap(m=>m.objects.map(o=>o.id)))].forEach(id=>option($('object'),id,id));$('loaded').textContent=G.case_id+' · unavailable roles: '+(G.unavailable_roles.join(', ')||'none');$('gates').textContent=JSON.stringify({comparison:G.comparison,parent_preservation:G.parent_preservation},null,2);$('events').textContent=JSON.stringify(Object.fromEntries(Object.entries(G.roles).map(([r,m])=>[r,{events:m.events,unobservable:m.unobservable,relation_display_summary:m.relation_display_summary}])),null,2);plot()};script.onerror=()=>{$('loaded').textContent='Case data unavailable; no substitute.';script.remove()};document.body.append(script)}
function plot(){const c=$('plot'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);if(!G)return;const w=G.windows[+$('window').value],id=$('object').value,axis=$('axis').value;if(!w){$('legend').textContent='Saved comparison/motion windows unavailable.';ctx.fillStyle='#222';ctx.fillText('No saved motion window; no curve substituted.',50,50);return}let low=Infinity,high=-Infinity;const tracks=[];for(const [role,m] of Object.entries(G.roles)){const o=m.objects.find(o=>o.id===id),origin=m.window_origins_s[w.id],points=o?o.frames.filter(f=>f.time_s-origin>=w.start_s-1e-9&&f.time_s-origin<=w.end_s+1e-9):[];tracks.push({role,origin,points});for(const f of points)if(f.valid===true&&f.present===true&&Number.isFinite(f[axis])){low=Math.min(low,f[axis]);high=Math.max(high,f[axis])}}$('legend').replaceChildren();for(const t of tracks){const s=document.createElement('span');s.style.color=colors[t.role];s.textContent=t.role+'  ';$('legend').append(s)}if(!Number.isFinite(low)){ctx.fillStyle='#222';ctx.fillText('No valid present points.',50,50);return}const pad=Math.max(1,(high-low)*.05);low-=pad;high+=pad;const px=t=>70+(t-w.start_s)/Math.max(1e-9,w.end_s-w.start_s)*(c.width-95),py=v=>c.height-50-(v-low)/(high-low)*(c.height-80);ctx.font='14px system-ui';ctx.fillStyle='#222';ctx.strokeStyle='#aaa';ctx.beginPath();ctx.moveTo(70,30);ctx.lineTo(70,c.height-50);ctx.lineTo(c.width-25,c.height-50);ctx.stroke();ctx.fillText(axis+' (native px)',10,18);ctx.fillText(high.toFixed(2),5,40);ctx.fillText(low.toFixed(2),5,c.height-50);ctx.fillText(w.start_s.toFixed(3)+' s',70,c.height-20);ctx.fillText(w.end_s.toFixed(3)+' s',c.width-110,c.height-20);for(const t of tracks){ctx.strokeStyle=colors[t.role];ctx.fillStyle=colors[t.role];ctx.beginPath();let pen=false;for(const f of t.points){if(f.valid!==true||f.present!==true||!Number.isFinite(f[axis])){pen=false;continue}const x=px(f.time_s-t.origin),y=py(f[axis]);if(pen)ctx.lineTo(x,y);else ctx.moveTo(x,y);pen=true}ctx.stroke();for(const f of t.points)if(f.valid===true&&f.present===true&&Number.isFinite(f[axis])){ctx.beginPath();ctx.arc(px(f.time_s-t.origin),py(f[axis]),1.5,0,Math.PI*2);ctx.fill()}}}
for(const caseId of D.representative_cases){const h=document.createElement('h3'),grid=document.createElement('div');h.textContent=caseId;grid.className='roles';$('videos').append(h,grid);for(const role of ['reference','original','before','unchanged']){const row=D.videos.find(v=>v.case_id===caseId&&v.role===role),card=document.createElement('article'),title=document.createElement('h4');title.textContent=role+' · '+D.role_labels[role];card.append(title);if(row){const v=document.createElement('video');v.controls=true;v.preload='metadata';v.src=row.url;card.append(v)}else{const p=document.createElement('p');p.textContent='Unavailable in the retained attempt. No substitute selected.';card.append(p)}grid.append(card)}}
$('scope').textContent=JSON.stringify({scope:D.scope,source_json_file_sha256:D.source_json_file_sha256,initial_failure_code:D.initial_failure_code,missing_records:D.missing_records,historical_generation:D.historical_generation,historical_generation_scope:D.historical_generation_scope,full_behavior_acceptance:false,new_model_generation:false},null,2);$('case').onchange=choose;for(const id of ['window','object','axis'])$(id).onchange=plot;document.querySelectorAll('tr[data-case]').forEach(row=>row.onclick=()=>{$('case').value=row.dataset.case;choose();$('curves').scrollIntoView()});choose();
</script></html>'''


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    create=commands.add_parser('build');create.add_argument('--source',type=Path,required=True);create.add_argument('--output',type=Path,required=True)
    verify=commands.add_parser('inspect');verify.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=build(args.source,args.output) if args.command=='build' else inspect(args.output)
    except (VisualError,OSError,ValueError,TypeError,KeyError) as error:
        print(json.dumps({'status':'failed','message':str(error)}));return 1
    print(json.dumps({'status':'presentation_complete','output':str(args.output),'cases':len(result['groups']),
        'records':result['record_count'],'role_slots':len(result['role_slots']),'videos':len(result['videos']),
        'initial_status':result['initial_status'],'full_behavior_acceptance':False}));return 0


if __name__=='__main__':
    raise SystemExit(main())
