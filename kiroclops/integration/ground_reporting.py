"""Offline, pixel-derived horizontal evidence; reading never changes acceptance."""
from __future__ import annotations

import html
import json
import math
from pathlib import Path
import shutil

from .storage import identifier, read_json, sha256
from .source_review import verify_source_review


def _workflow_summary(state, launch, jobs):
    """Show recorded workflow completion separately from published measurements."""
    escape = lambda value: html.escape(str(value), quote=True)
    selected = state.get('selected_candidate_id')
    finals = [job for job in jobs if job.get('action') == 'finalize' and job.get('candidate_id') == selected]
    interrupted = (state.get('status') == 'closed_failed' or launch.get('status') in
                   {'timeout', 'cancelled', 'launch_failed', 'cleanup_failed'} or
                   any(job.get('status') in {'interrupted', 'cancelled', 'failed'} for job in finals))
    unfinished = bool(state.get('active_job_id')) or not finals or any(job.get('status') != 'succeeded' for job in finals)
    headline = ('Workflow interrupted; not accepted as a completed run.' if interrupted else
                'Waiting for the workflow closing record.' if unfinished else
                'The closing of the candidate finalization job was recorded.')
    facts = [('Session status', state.get('status', 'pending')), ('Kiro launcher', launch.get('status', 'pending'))]
    if type(launch.get('elapsed_seconds')) in (int, float):
        facts.append(('Launcher duration', f'{launch["elapsed_seconds"]:.3f} seconds'))
    if launch.get('surface') == 'cli':
        end_turn = (launch.get('prompt_result') or {}).get('stopReason') == 'end_turn'
        facts.append(('CLI end_turn record', 'Present' if end_turn else 'Absent; the normal end of the model turn was not recorded'))
    facts.append(('Candidate finalization job', ' · '.join(f'{j.get("job_id", "?")}: {j.get("status", "pending")}' for j in finals)
                  or 'Waiting for record'))
    if launch.get('error'):
        facts.append(('Launcher explanation', launch['error']))
    body = '<p><b>' + escape(headline) + '</b></p><ul>' + ''.join(
        f'<li><b>{escape(label)}:</b> {escape(value)}</li>' for label, value in facts) + '</ul>'
    if interrupted or unfinished:
        body += ('<p>A finalization job may have published numerical results before it was interrupted. '
                 'A numerical PASS does not show that the model turn or the finalization job completed. '
                 'Diagnostic review documents collected afterwards do not turn an interrupted job into a successful one.</p>')
    return body


def _ground_summary(ground):
    """Present saved decisions in the planned matrix without reevaluating gates."""
    planned = [(sid, offset, repeat) for sid in ('r18', 'l30', 'reverse24')
               for offset in (0, 24) for repeat in (1, 2, 3)]
    entries, unexpected = {}, 0
    for entry in ground.get('scenario_checks', []) or []:
        scenario = entry.get('scenario', {}) if isinstance(entry, dict) else {}
        key = (scenario.get('id'), scenario.get('spawn_offset'), scenario.get('repeat'))
        if key not in planned:
            unexpected += 1
            continue
        entries.setdefault(key, []).append(entry)
    labels = {'curve_rmse': 'curve error', 'improvement': 'improvement', 'speed': 'speed',
              'stop_distance': 'braking distance', 'end_position': 'final position'}
    escape = lambda value: html.escape(str(value), quote=True)

    def number(value, *, percent=False):
        if type(value) not in (int, float) or not math.isfinite(value):
            return 'Pending'
        return f'{100 * value:.1f}%' if percent else f'{value:.4f}'

    rows, counts = [], {'pass': 0, 'fail': 0, 'pending': 0}
    for sid, offset, repeat in planned:
        matches = entries.get((sid, offset, repeat), [])
        entry = matches[0] if len(matches) == 1 else {}
        verdict = 'pass' if entry.get('pass') is True else 'fail' if entry.get('pass') is False else 'pending'
        counts[verdict] += 1
        result = {'pass': 'Passed', 'fail': 'Did not pass', 'pending': 'Pending'}[verdict]
        if len(matches) > 1:
            result += ' · duplicate entry'
        gates = entry.get('gates') or {}
        failed = [labels.get(key, str(key)) for key, value in gates.items() if value is False]
        if failed:
            result += ' · ' + ', '.join(failed)
        values = (f'{sid} · x{offset} · repeat {repeat}', number(entry.get('after_rmse_body')),
                  number(entry.get('improvement_fraction'), percent=True),
                  'Not applicable' if entry.get('speed_eligible') is False else number(entry.get('speed_relative_error'), percent=True),
                  number(entry.get('stop_distance_error_body')), number(entry.get('end_position_error_body')), result)
        rows.append(f'<tr data-ground-status="{verdict}">' + ''.join(f'<td>{escape(v)}</td>' for v in values) + '</tr>')
    summary = (f'<p><b>{counts["pass"]} / 18</b> passed · <b>{counts["fail"]}</b> did not pass · '
               f'<b>{counts["pending"]}</b> pending.</p>')
    if unexpected:
        summary += f'<p>There are {unexpected} out-of-plan entries; inspect the details in the comparison evidence.</p>'
    return (summary + '<p class="muted">Recorded held-out validation results. Error measures are expressed in body heights. '
            'Improvement is relative to the baseline version; the speed difference is the absolute percentage difference from the reference. '
            'Visual review and source scope are separate acceptance checks.</p>'
            '<div style="overflow-x:auto"><table><thead><tr><th scope="col">Scenario</th>'
            '<th scope="col">Curve error (body heights)</th><th scope="col">Improvement</th>'
            '<th scope="col">Speed difference</th><th scope="col">Braking distance difference (body heights)</th>'
            '<th scope="col">Final position difference (body heights)</th><th scope="col">Result / failed check</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')


def _unpublished_calibration(folder, candidate, session):
    """Read a complete unpublished calibration only; never promote it to a result."""
    from .ground_review import _local_file
    from .ground_runner import canonical_scenario, validate_recording
    import cv2

    try:
        files = {name: _local_file(folder / name, session) for name in ('manifest.json', 'analysis.json', 'video.mp4')}
        if any(not path.is_file() or not path.stat().st_size for path in files.values()):
            return None
        manifest, analysis = read_json(files['manifest.json']), read_json(files['analysis.json'])
        if not isinstance(manifest, dict) or not isinstance(analysis, dict):
            return None
        scenario = canonical_scenario(manifest['scenario'])
        duration = {'r06': 6, 'r48': 48}.get(scenario['id'])
        expected_events = [{'tick': 30, 'action': 'right', 'pressed': True},
                           {'tick': 30 + duration, 'action': 'right', 'pressed': False}] if duration else None
        if (manifest.get('schema_version') != 2 or manifest.get('mechanic') != 'ground_horizontal'
                or manifest.get('game') != 'slay' or manifest.get('variant') != 'after'
                or manifest.get('normal_level') is not False or manifest.get('warmup_ticks') != 60
                or scenario['split'] != 'calibration' or scenario['spawn_offset'] != 0
                or scenario['events'] != expected_events or manifest.get('scenario') != scenario
                or folder.name != f'{scenario["id"]}-x0-r{scenario["repeat"]}'
                or manifest.get('run_id') != folder.name):
            return None
        validate_recording(manifest, scenario, 'slay')
        provenance = manifest.get('provenance') or {}
        analysis_provenance = analysis.get('provenance') or {}
        if not isinstance(provenance, dict) or not isinstance(analysis_provenance, dict):
            return None
        hashes = candidate.get('source_hashes')
        project = session / 'candidates' / identifier(candidate['candidate_id']) / 'project'
        if (not hashes or provenance.get('full_source_hashes_before') != hashes
                or provenance.get('full_source_hashes_after') != hashes
                or Path(provenance.get('project', '')).resolve() != project):
            return None
        if (analysis.get('mechanic') != 'ground' or analysis.get('status') not in {'ok', 'rejected'}
                or analysis.get('game') != 'slay' or analysis.get('variant') != 'after'
                or analysis.get('scenario') != scenario or analysis.get('fps') != 60
                or Path(analysis.get('run_id', '')).resolve() != folder
                or analysis_provenance.get('manifest_sha256') != sha256(files['manifest.json'])):
            return None
        measured = analysis.get('frames')
        if not isinstance(measured, list) or len(measured) != 240:
            return None
        for index, frame in enumerate(measured):
            if (not isinstance(frame, dict) or type(frame.get('frame_index')) is not int
                    or frame['frame_index'] != index or type(frame.get('time_s')) not in (int, float)
                    or not math.isfinite(frame['time_s']) or abs(frame['time_s'] - index / 60) > 1e-5):
                return None
        json.dumps(analysis, allow_nan=False)
        frames = _local_file(folder / 'frames/000000.png', session).parent
        expected = {frames / f'{index:06d}.png' for index in range(240)}
        if set(frames.glob('*.png')) != expected:
            return None
        for path in expected:
            path = _local_file(path, session)
            image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if image is None or image.shape[:2] != (360, 640):
                return None
        return {'manifest': manifest, 'analysis': analysis}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, cv2.error):
        return None


def generate_ground_report(session_dir) -> Path:
    session = Path(session_dir).resolve(strict=True)
    state = read_json(session / 'session.json')
    if state.get('experiment_id') != 'ground-motion-v1':
        raise ValueError('ground report requires ground-motion-v1')
    output = session / 'report'
    if output.is_symlink() or output.resolve() != output.absolute():
        raise ValueError('report output must be an ordinary session directory')
    output.mkdir(exist_ok=True)
    escape = lambda value: html.escape(str(value), quote=True)
    issues, evidence_links = [], []

    def owned(value):
        path = Path(value)
        path = (path if path.is_absolute() else session / path)
        resolved = path.resolve()
        if not resolved.is_relative_to(session) or resolved != path.absolute():
            raise ValueError('report evidence escaped session or used a link')
        return resolved

    def copy(source, relative, label=None):
        source = owned(source)
        if not source.is_file():
            issues.append(f'Missing evidence: {source.relative_to(session)}')
            return None
        info = source.stat()
        if info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('report evidence cannot be a hardlink or reparse point')
        destination = output / relative
        if not destination.resolve().is_relative_to(output) or destination.resolve() != destination.absolute():
            raise ValueError('report asset path escaped output')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.stat().st_nlink != 1:
            raise ValueError('report destination must be an independent file')
        shutil.copy2(source, destination)
        if label:
            evidence_links.append(f'<a href="{escape(relative)}">{escape(label)}</a>')
        return relative

    selected = state.get('selected_candidate_id')
    candidates = state.get('candidates', [])
    candidate = next((c for c in candidates if c.get('candidate_id') == selected), None) if selected else None
    if not selected:
        candidate = next((c for c in reversed(candidates) if c.get('status') in {'evaluated','finalized','failed'}), None)
    visual_id = candidate.get('candidate_id') if candidate else None
    grouped = {'reference': state.get('reference') or {}, 'before': state.get('baseline_runs') or {},
               'after': {split: (candidate or {}).get(split + '_runs', []) for split in ('calibration','validation')}}
    runs, seen = [], set()
    for role, groups in grouped.items():
        for split in ('calibration', 'validation'):
            for value in groups.get(split, []):
                folder = owned(value)
                if str(folder) in seen:
                    continue
                seen.add(str(folder))
                manifest_path = folder / 'manifest.json'
                if not manifest_path.is_file():
                    issues.append(f'Missing manifest: {folder.name}')
                    continue
                manifest = read_json(manifest_path)
                analysis = read_json(folder / 'analysis.json') if (folder / 'analysis.json').is_file() else None
                prefix = f'assets/run-{len(runs):03d}'
                video = copy(folder / 'video.mp4', prefix + '/video.mp4')
                mf = copy(manifest_path, prefix + '/manifest.json')
                af = copy(folder / 'analysis.json', prefix + '/analysis.json') if analysis else None
                scenario = manifest.get('scenario') or {}
                runs.append({'role': role, 'split': split, 'name': folder.name, 'scenario': scenario,
                             'key': f'{scenario.get("id")}|{scenario.get("spawn_offset")}|{scenario.get("repeat")}',
                             'video': video, 'manifest': mf, 'analysis_file': af, 'analysis': analysis})
    final = state.get('final') or {}
    comparison_path = owned(final.get('comparison_path') or 'final/comparison.json')
    comparison = read_json(comparison_path) if comparison_path.is_file() else {}
    copy('session.json', 'evidence/session.json', 'Session')
    for relative in ('experiment.json', 'kiro/launch.json', 'kiro/stdout.jsonl', 'kiro/tool-audit.jsonl',
                     'kiro/authorship.json', 'ide/evidence.json', 'ide/session.zip', 'ide/finish-receipt.json',
                     'ide/environment-policy.json', 'review/operator-contribution.json',
                     'final/comparison.json', 'final/jump-comparison.json', 'final/manual-review.json'):
        if owned(relative).is_file():
            copy(relative, 'evidence/' + relative, relative)
    operator_links = []
    for relative, label in (
            ('ide/settings.png', 'IDE initial settings'),
            ('ide/settings-after.png', 'IDE final settings observation'),
            ('ide/permission-power.png', 'Power permission screen'),
            ('ide/permission-power-allowed.png', 'Granting of Power permission'),
            ('ide/power-install.json', 'Power installation record'),
            ('ide/permission-wait.json', 'Permission wait record'),
            ('ide/permission-resolution.json', 'Permission wait resolution record'),
            ('ide/operator-stop.json', 'Operator stop record'),
            ('ide/stopped.png', 'Stopped IDE screen'),
            ('kiro/launch-start.json', 'Launch start record')):
        if owned(relative).is_file():
            copied = copy(relative, 'evidence/' + relative)
            operator_links.append(f'<li><a href="{escape(copied)}">{escape(label)} · {escape(relative)}</a></li>')
    operator_observations = ''
    if operator_links:
        operator_observations = ('<details><summary>Operator settings and permission observations</summary>'
            '<p>These documents record the settings at the time of the screenshot, the installation, permission waits or operator actions. '
            'They do not verify the historical settings of each model generation and do not count as acceptance evidence on their own.</p><ul>'
            + ''.join(operator_links) + '</ul></details>')
    if final.get('manual_review_path'):
        receipt = owned(final['manual_review_path'])
        if receipt.is_file():
            copy(receipt, 'evidence/final/' + receipt.name, 'Independent visual review receipt')
    source_scope = verify_source_review(session)
    if owned('final/source-review.json').is_file():
        copy('final/source-review.json', 'evidence/final/source-review.json', 'Source scope review receipt')
    jobs = []
    for job in sorted((session / 'jobs').glob('*.json')):
        copy(job, 'evidence/jobs/' + job.name, job.name)
        jobs.append(read_json(owned(job)))
    launch_path = owned('kiro/launch.json')
    launch = read_json(launch_path) if launch_path.is_file() else {}
    diagnostic_links = []
    diagnostic_documents = []
    directories = [owned('review')] if owned('review').is_dir() else []
    while directories:
        directory = owned(directories.pop())
        if getattr(directory.stat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('diagnostic directory cannot be a reparse point')
        for item in directory.iterdir():
            item = owned(item)
            if item.is_dir():
                directories.append(item)
            elif item.suffix == '.json':
                diagnostic_documents.append(item)
    for document in sorted(diagnostic_documents):
        diagnostic_relative = document.relative_to(session / 'review').as_posix()
        if diagnostic_relative == 'operator-contribution.json':
            continue
        relative = copy(document, 'evidence/review/' + diagnostic_relative)
        if relative:
            diagnostic_links.append(f'<li><a href="{escape(relative)}">{escape(diagnostic_relative)}</a></li>')
    diagnostics = ''
    if diagnostic_links:
        diagnostics = ('<details><summary>Diagnostic review documents · not an acceptance receipt</summary>'
                       '<p>Source review drafts, independent raw observations and interruption/recovery records '
                       'are kept here. These links do not make the visual or source review PASS; '
                       'no acceptance result is derived from diagnostic documents.</p><ul>'
                       + ''.join(diagnostic_links) + '</ul></details>')
    unpublished_cards = []
    if not state.get('active_job_id'):
        for entry in candidates:
            if (entry.get('candidate_id') == selected or entry.get('status') not in {'failed', 'interrupted', 'cancelled'}
                    or entry.get('calibration_runs')):
                continue
            cid = identifier(entry['candidate_id'])
            for sid in ('r06', 'r48'):
                for repeat in (1, 2, 3):
                    name = f'{sid}-x0-r{repeat}'
                    folder = session / 'candidates' / cid / 'runs' / 'calibration' / name
                    diagnostic = _unpublished_calibration(folder, entry, session)
                    if diagnostic is None:
                        continue
                    prefix = f'assets/unpublished/{cid}/{name}'
                    video = copy(folder / 'video.mp4', prefix + '/video.mp4')
                    manifest_link = copy(folder / 'manifest.json', prefix + '/manifest.json')
                    analysis_link = copy(folder / 'analysis.json', prefix + '/analysis.json')
                    unpublished_cards.append(f'<article data-unpublished-calibration="true"><h3>{escape(cid)} · {escape(name)}</h3>'
                        f'<p>Unpublished calibration · not acceptance evidence</p>'
                        f'<video controls preload="metadata" src="{escape(video)}"></video>'
                        f'<p><a href="{escape(manifest_link)}">Recording manifest</a> · '
                        f'<a href="{escape(analysis_link)}">Recorded diagnostic measurement</a></p></article>')
    unpublished = ''
    if unpublished_cards:
        unpublished = ('<section id="unpublished-calibration"><h2>Unpublished calibration · not acceptance evidence</h2>'
            '<p>Complete 240-frame horizontal calibration recordings left over from an interrupted candidate evaluation. '
            'Because the job did not complete, they have not been added to the session candidate results. '
            'They are only displayed here; they do not count as a selected candidate, feedback or a held-out validation result. '
            'They do not change the numerical or visual acceptance statuses.</p><div class="grid">' + ''.join(unpublished_cards) + '</div></section>')
    history = []
    for entry in candidates:
        cid = entry['candidate_id']
        folder = owned('candidates/' + cid)
        if (folder / 'candidate.json').is_file():
            copy(folder / 'candidate.json', f'evidence/{cid}/candidate.json', f'{cid} record')
        diff = folder / 'patch.diff'
        diff_text = diff.read_text(encoding='utf-8-sig') if diff.is_file() else 'No code diff.'
        if diff.is_file():
            copy(diff, f'evidence/{cid}/patch.diff', f'{cid} code diff')
        facts = {k:entry.get(k) for k in ('status','feedback','smoke','jump_regression','error')}
        history.append(f'<details><summary>{escape(cid)} · {escape(entry.get("status"))}</summary>'
                       f'<pre>{escape(json.dumps(facts,ensure_ascii=False,indent=2))}</pre>'
                       f'<pre>{escape(diff_text)}</pre></details>')
    normal = []
    if visual_id:
        for name in ('normal', 'combined', 'wall'):
            video = owned(f'candidates/{visual_id}/smoke/{name}/video.mp4')
            if video.is_file():
                src = copy(video, f'assets/{name}.mp4')
                normal.append(f'<article><h3>{escape(name)}</h3><video controls preload="metadata" src="{escape(src)}"></video></article>')
    numerical = comparison.get('ground', {}).get('numerical_status',
        final.get('ground_status', comparison.get('numerical_status','pending')))
    manual = comparison.get('manual_validation_status', final.get('measurement_status','pending'))
    jump = final.get('jump_status', (candidate or {}).get('jump_regression', {}).get('status','pending'))
    smoke = final.get('smoke_status', (candidate or {}).get('smoke', {}).get('status','pending'))
    jump_rows = comparison.get('jump_regression', {}).get('validation_scenarios', [])
    jump_summary = '<p>The 12 held-out jump comparisons have not been completed yet.</p>'
    if jump_rows:
        rows = []
        for entry in jump_rows:
            scenario = entry['scenario']
            rows.append('<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in (
                f"{scenario['hold_ticks']} steps · x{scenario['spawn_offset']} · repeat {scenario['repeat']}",
                f"{entry['after_rmse_body']:.4f}", f"{100 * entry['height_relative_error']:.1f}%",
                f"{60 * entry['rise_error_s']:.2f}", f"{60 * entry['fall_error_s']:.2f}",
                'Passed' if entry.get('pass') else 'Did not pass')) + '</tr>')
        passed = sum(entry.get('pass') is True for entry in jump_rows)
        jump_summary = (f'<p><b>{passed} / 12</b> held-out jump comparisons passed.</p>'
            '<div style="overflow-x:auto"><table><thead><tr><th>Scenario</th><th>Curve error (body heights)</th>'
            '<th>Height difference</th><th>Rise difference (frames)</th><th>Fall difference (frames)</th>'
            '<th>Result</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
        drift = comparison.get('parent_jump_drift', {}).get('scenarios', [])
        if drift:
            maximum = max(entry['rmse_from_parent_body'] for entry in drift)
            jump_summary += (f'<p>Largest curve deviation from the original successful IDE02 jump: '
                f'<b>{maximum:.4f} body heights</b> (diagnostic measurement).</p>')
        jump_summary += ('<p class="muted">Jump acceptance is checked against the first-stage reference and thresholds. '
            'The baseline for the 50% improvement calculation is the original SlayHorizon; no further 50% '
            'improvement is expected from the already corrected IDE02. Independent visual review is checked separately.</p>')
    data = json.dumps({'runs':runs,'comparison':comparison},ensure_ascii=False,allow_nan=False)
    data = data.replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KiroClops · Running and braking</title><style>
:root{color-scheme:dark}body{margin:0;background:#10141e;color:#e9edf5;font:16px system-ui}main{max-width:1400px;margin:auto;padding:32px}h1{font-size:38px;margin:8px 0}h2{margin-top:0}p{line-height:1.6}a{color:#91c8ff}section,details{background:#1a2232;padding:22px;border-radius:12px;margin:20px 0}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}article{min-width:0}video{width:100%;background:#090d14;image-rendering:pixelated}button,select{font:inherit;padding:10px;border-radius:7px;border:1px solid #66758b;background:#27354c;color:white;margin:4px}svg{width:100%;height:310px;background:#111827}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.muted{color:#acb8ca}.status{display:flex;gap:12px;flex-wrap:wrap}.status span{padding:10px;background:#24334a;border-radius:7px}table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #445168;text-align:left}tr[data-ground-status="fail"]{background:#42252d}tr[data-ground-status="pass"] td:last-child{color:#a7f3c4}tr[data-ground-status="pending"] td:last-child{color:#fbd38d}summary{cursor:pointer}@media(max-width:800px){main{padding:16px}.grid{grid-template-columns:1fr}}
</style><body><main><p class="muted">KIROCLOPS / STAGE 2B</p><h1>Running, braking and changing direction</h1>
<p>__SESSION__ · Candidate shown: __CANDIDATE__</p><div class="status"><span>Horizontal numerical: __NUM__</span><span>Independent visual: __MANUAL__</span><span>Jump: __JUMP__</span><span>Gameplay check: __SMOKE__</span><span>Source scope: __SOURCE_SCOPE__</span></div>
<p class="muted">Numerical and visual statuses are recorded results. The source scope receipt is verified against the current source hashes. Whether only ground movement changed and the airborne horizontal control and jump paths were preserved is assessed by an independent code review. The camera is fixed; position is measured from the video. The normal gameplay recording is separate from the controlled comparison.</p>
<section id="workflow-summary"><h2>Run completion</h2>__WORKFLOW__</section>
<section><h2>Reference → before → after</h2><label>Scenario <select id="scenario"></select></label><p id="scenario-info"></p><div class="grid" id="panes"></div>
<p><button id="play">Play together</button><button id="prev" aria-label="Back one frame">← Frame</button><button id="next" aria-label="Forward one frame">Frame →</button><button id="start">Input time</button><span id="clock"></span></p>
<input id="seek" type="range" min="0" max="239" step="1" value="30" aria-label="Shared video frame" style="width:100%"></section>
<section><h2>Horizontal position aligned to input</h2><p>Reference <span style="color:#7dd3fc">●</span> · Before <span style="color:#fbbf24">●</span> · After <span style="color:#86efac">●</span></p><svg id="curve" role="img" aria-label="Horizontal displacement over time, measured in body heights" viewBox="0 0 1000 310"></svg><p class="muted">Vertical axis: displacement in body heights. Horizontal axis: real seconds elapsed since the input started. Invalid tracking intervals are not joined.</p><div id="metrics"></div></section>
<section id="ground-summary"><h2>18 held-out horizontal movement conditions</h2>__GROUND_SUMMARY__</section>
<section><h2>Preserved jump and gameplay integrity</h2>__JUMP_SUMMARY__<details><summary>Gameplay and jump evidence</summary><pre>__PRESERVATION__</pre></details><details><summary>Source scope review</summary><pre>__SOURCE_REVIEW__</pre></details><div class="grid">__NORMAL__</div></section>
__UNPUBLISHED_CALIBRATION__<section><h2>Kiro attempts and code diffs</h2>__HISTORY__</section>
<section><h2>Validation and evidence</h2><p>__LINKS__</p>__OPERATOR_OBSERVATIONS____DIAGNOSTICS__<details><summary>All comparison results</summary><pre>__COMPARISON__</pre></details><p>__ISSUES__</p></section>
<p class="muted">Offline report. Move this folder together with the video and data files.</p></main>
<script id="data" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent), roles=['reference','before','after'], names=['Reference','Before','After'], colors=['#7dd3fc','#fbbf24','#86efac'];
const el=id=>document.getElementById(id); let selected=[],playing=false,frame=30;
const keys=[...new Set(data.runs.map(r=>r.key))]; for(const key of keys){const r=data.runs.find(r=>r.key===key),o=document.createElement('option');o.value=key;o.textContent=`${r.split} · ${r.scenario.id} · x${r.scenario.spawn_offset} · repeat ${r.scenario.repeat}`;el('scenario').append(o)}
function videos(){return [...document.querySelectorAll('#panes video')]}
function seek(value){playing=false;el('play').textContent='Play together';frame=Math.max(0,Math.min(239,Math.round(value)));for(const v of videos()){v.pause();if(v.readyState)v.currentTime=frame/60}el('seek').value=frame;el('clock').textContent=`Frame ${frame} · relative to input ${((frame-30)/60).toFixed(3)} s`}
function plot(){const svg=el('curve');svg.replaceChildren();const ns='http://www.w3.org/2000/svg';const end=Math.max(31,...selected.map(r=>r?.analysis?.metrics?.window_end??66)), span=end-30;let points=selected.flatMap(r=>(r?.analysis?.frames??[]).filter(f=>f.valid&&Number.isFinite(f.x_body)&&f.frame_index>=30&&f.frame_index<=end));let min=Math.min(0,...points.map(f=>f.x_body)),max=Math.max(1,...points.map(f=>f.x_body));const y=v=>275-(v-min)/(max-min)*245;function line(tag,attrs,text){const n=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);if(text)n.textContent=text;svg.append(n)}for(let i=0;i<=4;i++){const val=min+(max-min)*i/4;line('line',{x1:55,x2:975,y1:y(val),y2:y(val),stroke:'#334155'});line('text',{x:4,y:y(val)+5,fill:'#cad5e3','font-size':14},val.toFixed(2))}for(let i=0;i<=4;i++)line('text',{x:55+i*230,y:302,fill:'#cad5e3','font-size':14},(i*span/60/4).toFixed(2)+'s');selected.forEach((r,ri)=>{let d='',connected=false;for(const f of r?.analysis?.frames??[]){if(f.frame_index<30||f.frame_index>end)continue;if(!f.valid||!Number.isFinite(f.x_body)){connected=false;continue}d+=(connected?'L':'M')+(55+(f.frame_index-30)/span*920)+','+y(f.x_body)+' ';connected=true}line('path',{d,stroke:colors[ri],fill:'none','stroke-width':2})})}
function choose(){seek(30);selected=roles.map(role=>data.runs.find(r=>r.key===el('scenario').value&&r.role===role));el('panes').replaceChildren();el('metrics').replaceChildren();selected.forEach((r,i)=>{const a=document.createElement('article'),h=document.createElement('h3');h.textContent=names[i];a.append(h);if(r?.video){const v=document.createElement('video');v.src=r.video;v.preload='auto';v.muted=true;v.setAttribute('aria-label',names[i]+' video');v.addEventListener('loadedmetadata',()=>v.currentTime=frame/60);a.append(v)}else{const p=document.createElement('p');p.textContent='Recording not available';a.append(p)}const p=document.createElement('p');p.textContent=r?.analysis?`Tracking: ${((r.analysis.valid_ratio??0)*100).toFixed(1)}% · ${r.analysis.status}`:'Measurement pending';a.append(p);el('panes').append(a);if(r){const d=document.createElement('details'),s=document.createElement('summary'),pre=document.createElement('pre');s.textContent=names[i]+' measurements';pre.textContent=JSON.stringify(r.analysis?.metrics??{},null,2);d.append(s,pre);el('metrics').append(d)}});el('scenario-info').textContent=selected.find(Boolean)?.split==='validation'?'Held-out validation: these recordings are evaluated after the candidate is sealed.':'Calibration: Kiro guides its fixes using only these recordings.';plot()}
el('scenario').onchange=choose;el('seek').oninput=()=>seek(Number(el('seek').value));el('prev').onclick=()=>seek(frame-1);el('next').onclick=()=>seek(frame+1);el('start').onclick=()=>seek(30);el('play').onclick=async()=>{if(playing){seek(frame);return}if(frame>=238)seek(0);try{await Promise.all(videos().map(v=>v.play()));playing=true;el('play').textContent='Pause together'}catch(e){el('clock').textContent='Video could not be played: '+e.message}};function tick(){if(playing){const v=videos()[0];if(v){frame=Math.round(v.currentTime*60);el('seek').value=frame;el('clock').textContent=`Frame ${frame} · relative to input ${((frame-30)/60).toFixed(3)} s`;if(v.ended)seek(239)}}requestAnimationFrame(tick)}choose();requestAnimationFrame(tick);
</script></body></html>'''
    replacements = {'SESSION':escape(state.get('session_id')), 'CANDIDATE':escape(visual_id or 'none'),
        'NUM':escape(numerical),'MANUAL':escape(manual),'JUMP':escape(jump),'SMOKE':escape(smoke),
        'SOURCE_SCOPE':escape(source_scope['status']), 'SOURCE_REVIEW':escape(json.dumps(source_scope,ensure_ascii=False,indent=2)),
        'JUMP_SUMMARY':jump_summary,
        'GROUND_SUMMARY':_ground_summary(comparison.get('ground', {})),
        'WORKFLOW':_workflow_summary(state, launch, jobs), 'DIAGNOSTICS':diagnostics,
        'OPERATOR_OBSERVATIONS':operator_observations,
        'UNPUBLISHED_CALIBRATION':unpublished,
        'PRESERVATION':escape(json.dumps({k:v for k,v in final.items() if k != 'comparison_path'},ensure_ascii=False,indent=2)),
        'NORMAL':''.join(normal) or '<p>Waiting for recording.</p>', 'HISTORY':''.join(history) or '<p>No candidates.</p>',
        'LINKS':' · '.join(evidence_links), 'COMPARISON':escape(json.dumps(comparison,ensure_ascii=False,indent=2)),
        'ISSUES':escape(' · '.join(issues)), 'DATA':data}
    for key,value in replacements.items():
        page=page.replace('__'+key+'__', value)
    destination=output / 'index.html'
    if destination.exists() and (destination.is_symlink() or destination.stat().st_nlink != 1):
        raise ValueError('report index must be an independent file')
    destination.write_text(page, encoding='utf-8')
    return destination
