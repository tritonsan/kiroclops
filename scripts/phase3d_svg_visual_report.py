"""Export an immutable unchanged SVG measurement as a separate visual report.

Presentation only: no Session, model, browser, pixel analysis, attestation or
acceptance is created. Run after native capture has ended. Twelve videos are
fixed in advance, never selected by their numerical outcome.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import html
import json
import math
from pathlib import Path
import shutil
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import evidence, reporting
from kiroclops.local_engine import suite_initial_acceptance as initial
from kiroclops.local_engine.session import _ordinary
from kiroclops.local_engine.source_inventory import full_byte_inventory
from kiroclops.local_engine.suite_evidence import _source_expectation
from kiroclops.visual.comparison import _origin
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


VERSION = 1
ROLES = ('reference', 'original', 'before', 'unchanged')
REPRESENTATIVE_CASES = (
    'suite-svg-validation-three-to-five-new-w1280-r1',
    'suite-svg-validation-five-to-six-drag-new-w1280-r1',
    'suite-svg-validation-six-to-four-new-w1280-r1',
)
FRAME_FIELDS = ('index', 'time_s', 'x', 'y', 'left', 'top', 'width', 'height',
                'valid', 'present', 'expected_active', 'reason')


def _need(value, message, code='invalid_svg_visual_report'):
    if not value:
        raise VisualError(code, message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _bound_json(path):
    path = _ordinary(path)
    before = file_sha256(path)
    value = read_json(path)
    _need(file_sha256(path) == before, 'Evidence changed while being read.', 'source_changed')
    return value, before


def _outside(output, roots):
    for root in roots:
        root = _ordinary(root)
        _need(not output.is_relative_to(root) and not root.is_relative_to(output),
              'Fresh report output must be separate from every evidence/source tree.', 'unsafe_path')


def _row_map(rows, cases, field, *, complete):
    _need(isinstance(rows, list) and len(rows) <= len(cases)
          and all(isinstance(row, dict) for row in rows), 'Incomplete ' + field + ' matrix.')
    values = {row.get('case_id'): row for row in rows}
    _need(len(values) == len(rows) and set(values).issubset(cases)
          and (not complete or set(values) == set(cases)), 'Foreign/duplicate/missing ' + field + ' case.')
    return values


def _curve_record(record, manifest, observed, events, windows, alignment):
    _need(isinstance(observed, dict) and observed.get('kind') == 'ObservationSet'
          and observed.get('package_sha256') == manifest['package_sha256']
          and observed.get('image_size') == manifest['image_size']
          and observed.get('inputs') == manifest['inputs']
          and observed.get('coordinate_system') == 'native_image_pixels',
          'Curves must retain the actual bound image-pixel observation set.')
    _need(isinstance(events, dict) and events.get('kind') == 'EventSet'
          and events.get('package_sha256') == manifest['package_sha256']
          and events.get('observation_sha256') == digest(observed),
          'Events do not bind these exact observations.')
    objects = observed.get('objects')
    _need(isinstance(objects, list) and objects
          and all(isinstance(obj, dict) and isinstance(obj.get('id'), str) for obj in objects)
          and len({obj['id'] for obj in objects}) == len(objects), 'Object identities are missing or repeated.')
    retained = []
    for obj in objects:
        frames = obj.get('frames')
        _need(isinstance(frames, list) and len(frames) == len(manifest['frames']),
              'Curve observations must retain every real frame.')
        for frame, source in zip(frames, manifest['frames']):
            _need(isinstance(frame, dict) and frame.get('index') == source['index']
                  and type(frame.get('index')) is int and frame.get('time_s') == source['time_s']
                  and _number(frame.get('time_s')) and type(frame.get('valid')) is bool,
                  'A curve changed the package frame identity or real timestamp.')
            if frame['valid']:
                _need(all(_number(frame.get(key)) for key in ('x', 'y', 'left', 'top', 'width', 'height')),
                      'Valid measured geometry must be finite; missing points are never filled.')
        retained.append({'id': obj['id'], 'valid_ratio': obj.get('valid_ratio'),
                         'frames': [{key: frame.get(key) for key in FRAME_FIELDS} for frame in frames]})
    return {'package_sha256': manifest['package_sha256'], 'image_size': manifest['image_size'],
        'source_sha256': record.get('source_sha256'), 'input_origin_s': _origin(observed, alignment),
        'window_origins_s': {window['id']: _origin(observed, window.get('alignment', alignment)) for window in windows},
        'objects': retained, 'events': reporting._portable(events.get('events', [])),
        'relations': reporting._portable(events.get('relations', [])),
        'unobservable': reporting._portable(events.get('unobservable', [])),
        'capture_basis': record.get('initial_acceptance_capture_basis', 'retained_reference'),
        'measurement_origin': reporting._portable(reporting.media_measurement_origin(record))}


def build_report(initial_directory, output):
    """Copy presentation assets to a fresh output; never write into initial data."""
    started = time.time()
    initial_directory, output = map(_ordinary, (initial_directory, output))
    _need(not output.exists(), 'Keep every previous report; use a fresh directory.', 'output_exists')
    result = initial.load_result(initial_directory)
    complete = result.get('complete') is True
    _need(complete and result.get('source_unchanged') is True or not complete
          and result.get('complete') is False and result.get('status') == 'measurement_failed'
          and result.get('numerical_pass') is False and result.get('measurement_qualified') is False
          and result.get('repair_preregistration_allowed') is False,
          'The exporter requires a genuine complete result or a retained failed attempt.')
    # A failed result may have a partial matrix. Its plan/source closure is still
    # mandatory; an absent row is unavailable, never an equivalent substitute.
    _, suite = initial.load_plan(initial_directory)
    plan, plan_file_sha = _bound_json(initial_directory / 'initial-plan.json')
    _need(digest(plan) == result['plan_sha256'], 'Initial plan/result binding differs.')
    receipt, receipt_sha = _bound_json(initial_directory / 'result-binding.json')
    _need(receipt.get('result_sha256') == digest(result), 'Initial result receipt differs.')
    cases = {case['case_id']: case for case in plan['cases']}
    _need(len(cases) == len(plan['cases']) == 28 and set(REPRESENTATIVE_CASES).issubset(cases),
          'The approved 28 cases and three fixed representatives are required.')
    protected = [initial_directory, plan['suite'], plan['authoring_parent'], plan['unchanged_source'],
                 plan['historical_generation']['historical_session'], Path(__file__).resolve().parents[1]]
    _outside(output, protected)
    comparison = _row_map(result['comparisons'], cases, 'comparison', complete=complete)
    preservation = _row_map(result['parent_preservation'], cases, 'preservation', complete=complete)
    indexes, input_files = {}, {
        str(initial_directory / 'initial-plan.json'): plan_file_sha,
        str(initial_directory / 'result-binding.json'): receipt_sha,
        str(initial_directory / 'result.json'): file_sha256(initial_directory / 'result.json'),
    }
    for role in ROLES:
        path = initial_directory / 'indexes' / (role + '.json')
        if path.is_file():
            rows, index_sha = _bound_json(path)
            input_files[str(path)] = index_sha
        else:
            _need(not complete, 'A complete result is missing its role index.')
            rows = []
        indexes[role] = _row_map(rows, cases, role, complete=complete)
        for record in rows:
            _outside(output, [record['package']])
    ffmpeg = shutil.which('ffmpeg')
    _need(ffmpeg is not None, 'FFmpeg is required for available fixed presentation videos.', 'missing_dependency')
    encoder = _ordinary(ffmpeg)
    code_files = {str(path): file_sha256(path) for path in (
        Path(__file__).resolve(), Path(reporting.__file__).resolve(),
        Path(initial.__file__).resolve(), Path(evidence.__file__).resolve())}
    encoder_sha = file_sha256(encoder)
    # All source/destination checks happen before the first presentation write.
    output.mkdir(parents=True, exist_ok=False)
    projects = {'original': _ordinary(plan['suite']) / suite['original'],
                'before': _ordinary(plan['authoring_parent']), 'unchanged': _ordinary(plan['unchanged_source'])}
    expectations = {role: _source_expectation(project, plan['original_source_hashes'] if role == 'original'
                    else plan['authoring_parent_source_hashes'], 'svg') for role, project in projects.items()}
    suite_cases = {case['case_id']: case for case in suite['cases']}
    role_labels = {'reference': 'reference pixels', 'original': 'clean original comparison source',
        'before': 'same historical candidate code · retained before measurement',
        'unchanged': 'same historical candidate code · fresh unchanged measurement'}
    groups, bindings, videos, missing_records, missing_videos = [], [], [], [], []
    for case in plan['cases']:
        case_id = case['case_id']
        saved_comparison, saved_preservation = comparison.get(case_id), preservation.get(case_id)
        for row in (saved_comparison, saved_preservation):
            _need(row is None or row.get('role') == case['role'], 'A numerical row changed its split authority.')
        contract = saved_comparison.get('contract') if saved_comparison else None
        _need(contract is None or isinstance(contract, dict) and isinstance(contract.get('windows'), list)
              and contract['windows'], 'Actual saved motion windows are required for a retained comparison.')
        windows = contract['windows'] if contract else []
        _need(len({window['id'] for window in windows}) == len(windows), 'Motion window IDs must be distinct.')
        group = {'case_id': case_id, 'split': case['role'], 'scenario': deepcopy(case['scenario']),
            'comparison': reporting._portable(saved_comparison),
            'parent_preservation': reporting._portable(saved_preservation),
            'comparison_available': saved_comparison is not None, 'preservation_available': saved_preservation is not None,
            'windows': deepcopy(windows), 'alignment': deepcopy(contract.get('alignment', {})) if contract else {'mode': 'absolute'},
            'curve_scope': 'saved comparison motion windows' if contract else 'raw seconds only; comparison/motion windows unavailable',
            'roles': {}, 'unavailable_roles': []}
        for role in ROLES:
            record = indexes[role].get(case_id)
            if record is None:
                slot = {'case_id': case_id, 'role': role, 'status': 'unavailable', 'reason': 'not retained in failed initial attempt'}
                missing_records.append(slot)
                group['unavailable_roles'].append(role)
                if case_id in REPRESENTATIVE_CASES:
                    missing_videos.append(deepcopy(slot))
                continue
            _need(record.get('scenario') == case['scenario'] and record.get('acceptance_source_role') == role
                  and record.get('new_model_generation') is False, 'Retained recording changed source-role or recipe.')
            initial._qualified(record, suite_cases[case_id], suite, expectations.get(role),
                               sources=plan['measurement_source_hashes'])
            manifest = load_package(record['package'], verify='hashes')
            observed, events = evidence.observations(record), evidence.event_set(record)
            view = _curve_record(record, manifest, observed, events, windows, group['alignment'])
            view['label'] = role_labels[role]
            binding = {'case_id': case_id, 'role': role, 'record_sha256': digest(record),
                'scenario_sha256': digest(record['scenario']), 'source_sha256': record.get('source_sha256'),
                'package_sha256': manifest['package_sha256'], 'observations_sha256': digest(observed),
                'events_sha256': digest(events), 'measurement_source_hashes': record.get('measurement_source_hashes')}
            bindings.append(binding)
            if case_id in REPRESENTATIVE_CASES:
                relative = 'media/' + case_id + '/' + role
                media = reporting._media(record, output, relative, encode=True)
                _need(media.get('video') and media['package_sha256'] == binding['package_sha256'],
                      'A fixed representative video is missing or its copied pixels differ.', 'video_encoding_failure')
                # Presentation alignment uses the exact saved comparison anchor,
                # not _media's generic first-input heuristic.
                view['media'] = {key: media[key] for key in ('package', 'frames', 'video', 'image_size')}
                videos.append({'case_id': case_id, 'role': role, 'path': media['video'],
                    'sha256': file_sha256(output / media['video']), 'source_data_binding': deepcopy(binding)})
            group['roles'][role] = view
        groups.append(group)
    _need(len(bindings) + len(missing_records) == 112 and len(videos) + len(missing_videos) == 12
          and (not complete or not missing_records), 'Keep all required slots, including explicit unavailable slots.')
    _need(initial.load_result(initial_directory) == result
          and all(file_sha256(_ordinary(path)) == expected for path, expected in input_files.items())
          and all(file_sha256(_ordinary(path)) == expected for path, expected in code_files.items())
          and file_sha256(encoder) == encoder_sha,
          'Initial evidence, exporter/helper code or encoder changed during export.', 'source_changed')
    report = {'kind': 'NoCodeChangeSVGVisualReport', 'schema_version': VERSION,
        'status': 'presentation_complete', 'initial_directory': str(initial_directory),
        'initial_kind': result['kind'], 'initial_status': result['status'], 'initial_result_sha256': digest(result),
        'initial_plan_sha256': digest(plan), 'initial_receipt_sha256': receipt_sha,
        'suite_sha256': result['suite_sha256'], 'source_sha256': result['source_sha256'],
        'source_unchanged': result.get('source_unchanged'), 'current_plan_sources_verified': True,
        'initial_complete': complete, 'initial_failure_code': result.get('code'), 'initial_failure_message': result.get('message'),
        'model_started': False, 'new_model_generation': False,
        'candidate_count': 0, 'authoring_started': False, 'full_behavior_acceptance': False,
        'acceptance_granted_by_report': False, 'numerical_result_recomputed': False,
        'numerical_pass': result['numerical_pass'], 'measurement_qualified': result['measurement_qualified'],
        'behavior_failure_observed': result['behavior_failure_observed'],
        'independent_review_status': result['independent_review_status'], 'source_review_status': result['source_review_status'],
        'functional': reporting._portable(result.get('functional')), 'summary_groups': deepcopy(result.get('groups')),
        'historical_generation': deepcopy(result['historical_generation']),
        'historical_generation_scope': 'Exact existing historical link only; this exporter does not audit or establish model/source authorship.',
        'scope': 'Presentation of saved unchanged-source numerical evidence; raw image, functional witness and source/native acceptance remain separate.',
        'validation_export_scope': 'Contains private validation pixels/measurements. Do not expose this report to a repair model or reuse validation as blind after exposure.',
        'curve_policy': 'Native image pixels at actual recorded seconds; no resampling, smoothing, hidden coordinates or missing-point filling. Each requested motion window uses its own verified input anchor; exact scored/censored bounds remain in saved comparisons.',
        'video_policy': 'Fixed 1280-wide repeat-1 new add/drag/delete cases, all four source roles. Timestamp-aware VFR encoding on copied packages; terminal 40ms presentation hold is not a new measurement frame.',
        'representative_cases': list(REPRESENTATIVE_CASES), 'required_record_count': 112, 'record_count': len(bindings),
        'complete_matrix': not missing_records, 'missing_records': missing_records, 'missing_videos': missing_videos,
        'role_labels': role_labels, 'record_bindings': bindings, 'videos': videos, 'groups': groups,
        'source_data_files': input_files, 'exporter_code_files': code_files,
        'encoder': {'path': str(encoder), 'sha256': encoder_sha}, 'started_at': started, 'finished_at': time.time()}
    write_json(output / 'report.json', report)
    public = reporting._portable(report)
    payload = json.dumps(public, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    (output / 'report-data.js').write_text('window.SVG_REPORT=' + payload + ';\n', encoding='utf-8')
    (output / 'report.html').write_text(_html(report), encoding='utf-8')
    tree = reporting.report_tree_receipt(output)
    files = full_byte_inventory(output, ignored=(), excluded_suffixes=())
    _need(tree['sha256'] == digest(files), 'Generated report tree changed.', 'source_changed')
    write_json(output / 'report-binding.json', {'kind': 'NoCodeChangeSVGVisualReportReceipt', 'schema_version': VERSION,
        'report_sha256': digest(report), 'file_hashes': files, 'tree': tree, 'full_behavior_acceptance': False,
        'acceptance_granted': False})
    return report


def inspect_report(output):
    """Replay report bytes and genuine initial linkage without granting acceptance."""
    output = _ordinary(output)
    report, receipt = read_json(output / 'report.json'), read_json(output / 'report-binding.json')
    files = full_byte_inventory(output, ignored=(), excluded_suffixes=())
    files.pop('report-binding.json', None)
    _need(report.get('kind') == 'NoCodeChangeSVGVisualReport' and report.get('schema_version') == VERSION
          and receipt.get('kind') == 'NoCodeChangeSVGVisualReportReceipt' and receipt.get('schema_version') == VERSION
          and receipt.get('report_sha256') == digest(report) and receipt.get('file_hashes') == files
          and receipt.get('tree') == {'file_count': len(files), 'sha256': digest(files)}
          and report.get('full_behavior_acceptance') is False and report.get('acceptance_granted_by_report') is False
          and receipt.get('full_behavior_acceptance') is False and receipt.get('acceptance_granted') is False
          and report.get('model_started') is False and report.get('new_model_generation') is False
          and report.get('candidate_count') == 0 and report.get('required_record_count') == 112
          and report.get('record_count') == len(report.get('record_bindings', []))
          and report['record_count'] + len(report.get('missing_records', [])) == 112
          and len(report.get('videos', [])) + len(report.get('missing_videos', [])) == 12
          and report.get('representative_cases') == list(REPRESENTATIVE_CASES),
          'Report bytes or presentation scope changed.', 'svg_visual_report_changed')
    result = initial.load_result(report['initial_directory'])
    _need(digest(result) == report['initial_result_sha256'] and result['source_sha256'] == report['source_sha256']
          and result['numerical_pass'] is report['numerical_pass']
          and all(file_sha256(_ordinary(path)) == expected for path, expected in report['source_data_files'].items()),
          'Report no longer binds the genuine initial result.', 'source_changed')
    return report


def _html(report):
    rows = []
    for group in report['groups']:
        comparison, preservation = group['comparison'] or {}, group['parent_preservation'] or {}
        failed_windows = [row for row in comparison.get('per_window_gates', []) if row.get('pass') is False]
        cells = [group['case_id'], group['split'], 'available' if group['comparison_available'] else 'unavailable',
                 str(comparison.get('pass')),
                 str(comparison.get('measurement_pass')), str(comparison.get('baseline_measurement_pass')),
                 str(comparison.get('score')), str(comparison.get('failure_class')),
                 str(len(failed_windows)) if comparison else 'unavailable',
                 str(preservation.get('pass')), str(preservation.get('failure_class')),
                 ', '.join(group['unavailable_roles']) or 'none']
        rows.append('<tr>' + ''.join('<td>' + html.escape(cell) + '</td>' for cell in cells) + '</tr>')
    banner = html.escape(report['initial_status']) + ' · saved numerical pass: ' + html.escape(str(report['numerical_pass']))
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unchanged SVG visual measurement report</title><style>
body{font:16px system-ui;margin:24px;max-width:1500px;background:#101827;color:#edf2f7}a{color:#a6d4ff}
section{margin:26px 0}select,button{padding:8px;margin:4px}table{border-collapse:collapse;font-size:13px}td,th{padding:8px;border:1px solid #40516c;text-align:left;overflow-wrap:anywhere}
.scroll{overflow:auto}.roles{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}article{padding:12px;background:#1b2b42}video{width:100%}canvas{width:100%;background:#fff;border:1px solid #40516c}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}.notice{border:2px solid #ccab58;padding:16px}
</style><h1>Unchanged SVG visual measurement report</h1><div class="notice"><strong>''' + banner + '''</strong>
<p>This report presents a genuine NoCodeChangeInitialAcceptance, including a failed/incomplete attempt when applicable. Missing cases and source roles stay unavailable. It creates no candidate, new model generation or full acceptance. Historical generation is an existing evidence link; authorship/native review is separate.</p>
<p>Raw image inspection, functional execution/witness and independent source review remain required. Saved failures stay failures. This report contains private validation; exposing it to a repair model invalidates a later blind-validation claim.</p></div>
<p><a href="report.json">Exact saved numerical rows, role bindings and curves</a> · <a href="report-binding.json">Report byte receipt</a></p>
<section><h2>All 28 planned cases</h2><p>Reference vs unchanged uses the clean original for gain. Before and unchanged use the same historical candidate code; their independent recordings check preservation of that exact authoring parent. Every scored object/window gate remains in the detailed rows; the mean score cannot excuse failure.</p><div class="scroll"><table><thead><tr>
<th>Case</th><th>Split</th><th>Comparison availability</th><th>Comparison pass</th><th>Measurement pass</th><th>Original measurement pass</th><th>Saved mean score</th><th>Failure</th><th>Failed object/window gates</th><th>Parent preservation pass</th><th>Preservation failure</th><th>Unavailable recording roles</th></tr></thead><tbody>''' + ''.join(rows) + '''</tbody></table></div></section>
<section><h2>Measured curves for every motion</h2><p>Native image pixels against real seconds from each role's matching recorded input. Points retain original timestamps; lines join adjacent observed points for presentation. Invalid, absent or unknown geometry breaks the line. No smoothing or filled measurements. The requested window is shown; saved details retain the exact scored interruption/censoring bounds.</p>
<label>Case <select id="case"></select></label><label>Motion <select id="window"></select></label><label>Object <select id="object"></select></label><label>Axis <select id="axis"><option>x</option><option>y</option><option>width</option><option>height</option></select></label>
<p id="legend"></p><canvas id="plot" width="1200" height="420" aria-label="Measured motion curves"></canvas><details><summary>Exact numerical comparison and preservation</summary><pre id="details"></pre></details><details><summary>Measured events, relationships and unobservable facts</summary><pre id="events"></pre></details></section>
<section><h2>Three fixed representative videos</h2><p>Preselected new add, new drag and new delete at width 1280, repeat 1; all four roles. Original frame durations drive VFR encoding. A terminal 40 ms presentation hold is not a measured frame. Video playback starts at raw package time zero; curves use separately recorded input anchors.</p><div id="videos"></div></section>
<section><details><summary>Scope and historical generation link</summary><pre id="scope"></pre></details></section>
<script src="report-data.js"></script><script>
const D=window.SVG_REPORT,$=id=>document.getElementById(id),colors=['#1149a1','#db6b06','#7a3197','#00805d'];let G;
function option(s,v,label){const o=document.createElement('option');o.value=v;o.textContent=label;s.append(o)}
D.groups.forEach((g,i)=>option($('case'),i,g.case_id));
function choose(){G=D.groups[+$('case').value];$('window').replaceChildren();$('object').replaceChildren();G.windows.forEach((w,i)=>option($('window'),i,w.id));
const ids=[...new Set(Object.values(G.roles).flatMap(m=>m.objects.map(o=>o.id)))];ids.forEach(id=>option($('object'),id,id));
$('details').textContent=JSON.stringify({comparison:G.comparison,parent_preservation:G.parent_preservation},null,2);
$('events').textContent=JSON.stringify(Object.fromEntries(Object.entries(G.roles).map(([role,m])=>[role,{events:m.events,relations:m.relations,unobservable:m.unobservable}])),null,2);plot()}
function plot(){const c=$('plot'),ctx=c.getContext('2d'),w=G.windows[+$('window').value],axis=$('axis').value,id=$('object').value;
ctx.clearRect(0,0,c.width,c.height);if(!w){$('legend').textContent='Saved comparison/motion windows unavailable; no curve substituted.';ctx.fillStyle='#222';ctx.fillText('Incomplete case: no saved motion-window comparison.',50,50);return}const start=w.start_s,end=w.end_s,tracks=[];let low=Infinity,high=-Infinity;
Object.entries(G.roles).forEach(([role,m],i)=>{const obj=m.objects.find(o=>o.id===id),origin=m.window_origins_s[w.id],frames=obj?obj.frames:[];
const points=frames.filter(f=>f.time_s-origin>=start-1e-9&&f.time_s-origin<=end+1e-9);tracks.push({role,color:colors[i],origin,points});
for(const f of points)if(f.valid===true&&f.present===true&&Number.isFinite(f[axis])){low=Math.min(low,f[axis]);high=Math.max(high,f[axis])}});
$('legend').replaceChildren();tracks.forEach(t=>{let s=document.createElement('span');s.style.color=t.color;s.textContent=t.role+'  ';$('legend').append(s)});
if(!Number.isFinite(low)){ctx.fillStyle='#222';ctx.fillText('No valid present measurements for this object/window.',50,50);return}
const padding=Math.max(1,(high-low)*.05);low-=padding;high+=padding;const px=t=>70+(t-start)/Math.max(1e-9,end-start)*(c.width-95),py=v=>c.height-50-(v-low)/(high-low)*(c.height-80);
ctx.fillStyle='#222';ctx.font='14px system-ui';ctx.strokeStyle='#bbb';ctx.beginPath();ctx.moveTo(70,30);ctx.lineTo(70,c.height-50);ctx.lineTo(c.width-25,c.height-50);ctx.stroke();
ctx.fillText(axis+' (native px)',10,18);ctx.fillText(high.toFixed(2),5,40);ctx.fillText(low.toFixed(2),5,c.height-50);ctx.fillText(start.toFixed(3)+' s',70,c.height-20);ctx.fillText(end.toFixed(3)+' s',c.width-100,c.height-20);
for(const t of tracks){ctx.strokeStyle=t.color;ctx.fillStyle=t.color;ctx.beginPath();let pen=false;for(const f of t.points){if(f.valid!==true||f.present!==true||!Number.isFinite(f[axis])){pen=false;continue}const x=px(f.time_s-t.origin),y=py(f[axis]);if(pen)ctx.lineTo(x,y);else ctx.moveTo(x,y);pen=true}ctx.stroke();for(const f of t.points)if(f.valid===true&&f.present===true&&Number.isFinite(f[axis])){ctx.beginPath();ctx.arc(px(f.time_s-t.origin),py(f[axis]),1.5,0,Math.PI*2);ctx.fill()}}
}
for(const caseId of D.representative_cases){const g=D.groups.find(g=>g.case_id===caseId),heading=document.createElement('h3'),grid=document.createElement('div');heading.textContent=caseId;grid.className='roles';$('videos').append(heading,grid);
for(const role of ['reference','original','before','unchanged']){const m=g.roles[role],card=document.createElement('article'),h=document.createElement('h4');h.textContent=role+' · '+D.role_labels[role];card.append(h);if(m&&m.media){const v=document.createElement('video'),a=document.createElement('a');v.controls=true;v.preload='metadata';v.src=m.media.video;a.href=m.media.package;a.textContent='Exact copied frame manifest';card.append(v,a)}else{const p=document.createElement('p');p.textContent='Unavailable in this retained attempt. No substitute selected.';card.append(p)}grid.append(card)}}
$('scope').textContent=JSON.stringify({scope:D.scope,initial_complete:D.initial_complete,initial_failure_code:D.initial_failure_code,initial_failure_message:D.initial_failure_message,historical_generation_scope:D.historical_generation_scope,historical_generation:D.historical_generation,required_record_count:D.required_record_count,record_count:D.record_count,missing_records:D.missing_records,missing_videos:D.missing_videos,source_sha256:D.source_sha256,full_behavior_acceptance:false,new_model_generation:false,independent_review_status:D.independent_review_status,source_review_status:D.source_review_status},null,2);
$('case').onchange=choose;for(const id of ['window','object','axis'])$(id).onchange=plot;choose();
</script></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build')
    build.add_argument('--initial', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_report(args.initial, args.output) if args.command == 'build' else inspect_report(args.output)
    except (VisualError, OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'code': getattr(error, 'code', 'svg_visual_report_failed'), 'message': str(error)}))
        return 1
    print(json.dumps({'status': report['status'], 'output': str(args.output), 'record_count': report['record_count'],
        'video_count': len(report['videos']), 'initial_status': report['initial_status'], 'numerical_pass': report['numerical_pass'],
        'new_model_generation': False, 'full_behavior_acceptance': False}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
