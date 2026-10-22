"""Build a portable, read-only presentation of a registered local cohort.

Usage:
  .venv-local/Scripts/python.exe -B scripts/build_local_cohort_report.py \
      --registration artifacts/phase3a/COHORT/registration.json --output OUTPUT
  .venv-local/Scripts/python.exe -B scripts/build_local_cohort_report.py --self-test

The real cohort.summarize replay is mandatory. It refreshes summary.json as its
normal side effect; this script never launches a model, recorder or report job.
Any running trial blocks packaging before session details are accessed. Existing
terminal reports are re-rendered from portable data and explicitly referenced
media only; no target tree, private oracle, native transcript or host path is
copied. Use a fresh output directory. Three repeats are descriptive evidence.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import html
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.local_engine import cohort
from kiroclops.local_engine.reporting import HTML as STAGE_HTML
from kiroclops.local_engine.report_costs import cohort_costs
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest, VisualError

TERMINAL = {'completed', 'failed', 'interrupted', 'not_run_predecessor_failed'}
PRIVATE_KEYS = {'source_path', 'raw_run', 'analysis', 'oracle_path', 'profile_path',
    'command', 'executable', 'source_code', 'prompt', 'messages', 'trace', 'session',
    'workspace', 'source_directory'}


def portable(value):
    if isinstance(value, dict):
        return {portable(str(k)): portable(v) for k, v in value.items() if k not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [portable(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'[A-Za-z]:[\\/][^\r\n\"<>]*', '[local artifact]', value)
        value = re.sub(r'\\\\[^\s\"<>]+', '[local artifact]', value)
        value = re.sub(r'(?<![\w:])/(?:home|Users|tmp|var|mnt|workspace|opt)/[^\s\"<>]*', '[local artifact]', value)
        return '[local artifact]' if value.startswith('/') else value
    return value


def safe_file(root, name, extensions):
    text = str(name)
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '\\' in text:
        raise VisualError('unsafe_report_link', 'Only plain relative report assets can be copied')
    relative = Path(unquote(parsed.path))
    if relative.is_absolute() or '..' in relative.parts:
        raise VisualError('unsafe_report_link', 'Report asset escapes its report directory')
    source = (root / relative).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file() or source.suffix.lower() not in extensions:
        raise VisualError('unsafe_report_link', 'Missing or unsupported report asset')
    return source, relative


def replay_stage_acceptance(session, state, chosen):
    """Current, source-bound terminal evidence; saved report badges are not inputs."""
    from kiroclops.local_engine.review import validate_independent_review, validate_source_review, audit_native_run
    from kiroclops.local_engine.evidence import required_observation_bindings
    session=Path(session)
    if state.get('active_job_id'):
        raise VisualError('cohort_running','Cannot replay a stage with an active job')
    names=('session.json','binding.json','independent_review.json','source_review.json','kiro/launch.json',
           'kiro/config.json','kiro/run/stdout.jsonl','kiro/run/outbound.jsonl','kiro/tool-audit.jsonl')
    before={name:file_sha256(session/name) for name in names if (session/name).is_file()}
    value={'kind':'CurrentTerminalAcceptanceReplay','candidate_sha256':chosen.get('source_sha256'),
        'captured_numerical_pass':None,'independent_review_status':'pending','source_review_status':'pending',
        'native_run_status':'pending','review_coverage_status':'pending','diagnostics':[],
        'evidence_file_hashes':before,'saved_report_claims_used':False}
    try:
        result=cohort._stage_result(session)
        if result is not None:value['captured_numerical_pass']=result.get('pass') is True
    except (VisualError,OSError,ValueError,KeyError) as exc:
        value['numerical_evidence_status']='fail';value['diagnostics'].append({'scope':'numerical','reason':str(exc)})
    image_review=None
    for name,validator in (('independent_review',validate_independent_review),('source_review',validate_source_review)):
        path=session/(name+'.json')
        if not path.is_file():continue
        try:
            checked=validator(path,**({'directory':session} if name=='source_review' else {}),candidate_sha256=chosen.get('source_sha256'))
            value[name+'_status']='pass'
            if name=='independent_review':image_review=checked
            else:value['native_run_status']=checked['native_validation']['status']
        except (VisualError,OSError,ValueError,KeyError) as exc:
            value[name+'_status']='fail';value['diagnostics'].append({'scope':name,'reason':str(exc)})
    if image_review is not None:
        try:
            actual=required_observation_bindings(session,state)
            if not actual or actual!={tuple(v) for v in image_review['required_observations']}:
                raise VisualError('incomplete_review','Independent review omits current required recordings')
            value['review_coverage_status']='pass'
        except (VisualError,OSError,ValueError,KeyError) as exc:
            value['review_coverage_status']='fail';value['diagnostics'].append({'scope':'coverage','reason':str(exc)})
    if value['native_run_status']!='pass' and (session/'kiro/launch.json').is_file():
        try:value['native_run_status']=audit_native_run(session)['status']
        except (VisualError,OSError,ValueError,KeyError) as exc:
            value['native_run_status']='fail';value['diagnostics'].append({'scope':'native','reason':str(exc)})
    statuses=[value[k] for k in ('independent_review_status','source_review_status','native_run_status','review_coverage_status')]
    value['overall_acceptance']=('fail' if value['captured_numerical_pass'] is False or 'fail' in statuses or value.get('numerical_evidence_status')=='fail'
        else 'pass' if value['captured_numerical_pass'] is True and all(s=='pass' for s in statuses) else 'pending')
    value['native_qualified_stage_pass']=(value['captured_numerical_pass'] is True and value['native_run_status']=='pass')
    after={name:file_sha256(session/name) for name in names if (session/name).is_file()}
    if before!=after:raise VisualError('evidence_changed','Terminal evidence changed during presentation replay')
    return portable(value)


def stage_time_scopes(session, stage):
    """Read terminal receipt timestamps only; no inference/token-time estimate."""
    def duration(start,end):
        return end-start if all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) and end>=start else None
    value={'kind':'TerminalStageTimeScopes',
        'stage_wall_seconds':duration(stage.get('started_at'),stage.get('finished_at')),
        'stage_wall_scope':'Registered stage wall time, including prerequisite validation, preparation, native setup, task and final audit.',
        'native_task_wall_seconds':None,'native_task_status':'unavailable',
        'native_prompt_to_terminal_wall_seconds':None,'native_launcher_wall_seconds':None,
        'native_task_scope':'Prompt to response wall time includes authoring, tools and waits; it is not model inference-only time.',
        'whole_harness_wall_seconds':None,'whole_harness_scope':'No outer wrapper receipt is supplied to this exporter; unavailable, not zero.'}
    path=Path(session)/'kiro/launch.json'
    if not path.is_file():return value
    sha=file_sha256(path);launch=read_json(path)
    value['launch_receipt_sha256']=sha
    value['native_launcher_wall_seconds']=duration(launch.get('started_at'),launch.get('finished_at'))
    if launch.get('prompt_sent') is True:
        complete=duration(launch.get('prompt_started_at'),launch.get('response_received_at'))
        censored=duration(launch.get('prompt_started_at'),launch.get('finished_at'))
        if (duration(launch.get('started_at'),launch.get('prompt_started_at')) is None or
                censored is None or launch.get('response_received_at') is not None and
                duration(launch.get('response_received_at'),launch.get('finished_at')) is None):
            complete=None;censored=None
        if complete is not None:
            value.update(native_task_wall_seconds=complete,native_task_status='recorded_prompt_to_response')
        elif launch.get('response_received_at') is None and censored is not None:
            value.update(native_task_status='censored_no_recorded_response',native_prompt_to_terminal_wall_seconds=censored,
                censored_scope='Prompt to launcher terminal time includes cleanup; this is not a completed response duration.')
        else:value['native_task_status']='invalid_or_missing_timestamps'
    if file_sha256(path)!=sha:raise VisualError('evidence_changed','Launch receipt changed while reading time scopes')
    return value


def terminal_validation_diagnostic(session, state, chosen):
    """Show a bound worker failure alongside, never in place of, primary status."""
    from kiroclops.local_engine.session import JOB_BINDING_FIELDS, source_hashes
    job_id = state.get('final_job_id')
    if job_id is None:
        return None
    if not isinstance(job_id, str) or re.fullmatch(r'[0-9a-f]{32}', job_id) is None:
        raise VisualError('wrong_result', 'Malformed final job identity')
    session = Path(session).resolve()
    paths = [session / 'jobs' / job_id / name for name in ('job.json', 'binding.json')]
    hashes = {p.name: file_sha256(p) for p in paths}
    job, binding = map(read_json, paths)
    selected = state.get('selected_candidate_id')
    if (not isinstance(selected, str) or re.fullmatch(r'candidate-[0-9]+', selected) is None
            or chosen.get('candidate_id') != selected
            or job.get('job_id') != job_id or job.get('operation') != 'finalize'
            or job.get('candidate_id') != selected
            or job.get('source_sha256') != chosen.get('source_sha256')
            or digest({key: job.get(key) for key in JOB_BINDING_FIELDS}) != binding.get('sha256')):
        raise VisualError('wrong_result', 'Terminal worker belongs to another selected source or binding')
    source = session / 'candidates' / selected / 'source'
    if (Path(job['source_path']).resolve() != source
            or source_hashes(source) != job.get('source_hashes')
            or digest(job['source_hashes']) != chosen['source_sha256']):
        raise VisualError('wrong_result', 'Terminal worker source no longer matches the selected snapshot')
    if job.get('status') not in ('succeeded', 'failed', 'cancelled', 'interrupted'):
        raise VisualError('cohort_running', 'Final worker has not reached a terminal state')
    final = state.get('final') or {}
    if final.get('job_id') != job_id or final.get('status') != job['status']:
        raise VisualError('wrong_result', 'Session and final worker status differ')
    if (final.get('error') or None) != (job.get('error') or None):
        raise VisualError('wrong_result', 'Session and final worker diagnostics differ')
    error = job.get('error')
    if error is not None and (not isinstance(error, dict)
            or any(not isinstance(error.get(k), str) or not error[k] for k in ('code', 'message'))):
        raise VisualError('wrong_result', 'Incomplete terminal worker error')
    if hashes != {p.name: file_sha256(p) for p in paths}:
        raise VisualError('evidence_changed', 'Terminal worker changed during diagnostic export')
    return portable({'kind': 'SourceBoundTerminalValidationDiagnostic',
        'job_id': job_id, 'status': job['status'], 'candidate_sha256': chosen['source_sha256'],
        'error': None if error is None else {k: error[k] for k in ('code', 'message')},
        'evidence_hashes': hashes, 'primary_outcome_modified': False,
        'scope': 'Worker outcome only. This does not establish native model configuration or a complete motion result.'})


def copy_stage_report(source, destination, current):
    """Copy named presentation assets, replacing historical badges with current replay."""
    original = read_json(source / 'report.json')
    if (current.get('kind')!='CurrentTerminalAcceptanceReplay' or not current.get('candidate_sha256')
            or original.get('candidate_sha256')!=current['candidate_sha256']):
        raise VisualError('wrong_report_candidate','Saved report is not the current replayed candidate')
    data = portable(original)
    if data.get('kind') != 'LocalEvidenceReport':
        raise VisualError('unsupported_report', 'Only the local evidence report format can be packaged')
    data['historical_saved_claims']={k:deepcopy(data.get(k)) for k in ('numerical','acceptance_details','overall_acceptance',
        'independent_review_status','source_review_status','native_run_status','review_coverage_status',
        'independent_review','source_review','native_run','review_coverage_error')}
    for key in ('independent_review','source_review','native_run','review_coverage_error'):
        data.pop(key,None)
    data['historical_report_sha256']=file_sha256(source/'report.json')
    data['current_acceptance_replay']=deepcopy(current)
    for key in ('overall_acceptance','independent_review_status','source_review_status','native_run_status','review_coverage_status'):
        data[key]=current[key]
    data['numerical']={'pass':current['captured_numerical_pass'],'scope':'Current source-bound final result, not the saved report claim.'}
    data['acceptance_details']={'current_replay':current,'historical_saved_claims':data['historical_saved_claims'],
        'historical_scope':'Prior presentation snapshot only. Its PASS claims do not establish current acceptance.'}
    data.setdefault('limitations',[]).append('Displayed acceptance badges were replayed while packaging. Original saved claims are labelled historical; original report files were not changed.')
    destination.mkdir(parents=True)
    copied = set()

    def asset(name, extensions):
        path, relative = safe_file(source, name, extensions)
        out = destination / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        if relative.as_posix() not in copied:
            if path.suffix.lower() == '.json':
                write_json(out, portable(read_json(path)))
            elif path.suffix.lower() in ('.diff', '.txt'):
                out.write_text(portable(path.read_text('utf-8')), encoding='utf-8')
            else:
                shutil.copyfile(path, out)
            copied.add(relative.as_posix())
        return relative.as_posix()

    # Paths are copied from the original JSON, then normalized explicitly. A
    # path rejected by portable() must never silently become a missing link.
    for group, raw_group in zip(data.get('groups', []), original.get('groups', [])):
        if raw_group.get('comparison'):
            group['comparison'] = asset(raw_group['comparison'], {'.json'})
        for role, media in group.get('roles', {}).items():
            raw = raw_group['roles'][role]
            for frame, raw_frame in zip(media.get('frames', []), raw.get('frames', [])):
                frame['path'] = asset(raw_frame['path'], {'.png', '.jpg', '.jpeg', '.webp'})
            if raw.get('video'):
                media['video'] = asset(raw['video'], {'.mp4', '.webm'})
            if raw.get('event_file'):
                media['event_file'] = asset(raw['event_file'], {'.json'})
    if original.get('candidate_diff'):
        data['candidate_diff'] = asset(original['candidate_diff'], {'.diff', '.txt'})
    write_json(destination / 'report.json', data)
    script = json.dumps(data, ensure_ascii=False, allow_nan=False).replace('</', '<\\/')
    notice='<section><h2>Current acceptance replay</h2><p>Badges below use the current source-bound numerical, image, source and native checks. Previous saved claims appear only as historical evidence in Acceptance and preserved behavior. Media and candidate history are copied historical presentation assets for this candidate, not newly captured evidence.</p></section>'
    (destination / 'index.html').write_text(STAGE_HTML.replace('<header>',notice+'<header>',1).replace('__DATA__', script), encoding='utf-8')
    return data


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.values = []
    def handle_starttag(self, tag, attrs):
        self.values.extend(v for k, v in attrs if k in ('href', 'src') and v)


def validate_links(root):
    for path in root.rglob('*.html'):
        parser = Links(); parser.feed(path.read_text('utf-8'))
        for value in parser.values:
            if value.startswith('#'):
                continue
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc:
                raise VisualError('nonportable_report', 'An external link entered the local package')
            target = (path.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                raise VisualError('broken_report_link', 'A local HTML link does not resolve')


def stable_snapshot(registration):
    registry = read_json(registration)
    progress_path = registration.parent / 'progress.json'
    progress = read_json(progress_path)
    if progress.get('registration_sha256') != file_sha256(registration):
        raise VisualError('invalid_registration', 'Progress belongs to another registration')
    cohort._verify_progress_layout(registry, progress)
    if len(progress.get('rows', [])) != 18:
        raise VisualError('invalid_registration', 'Expected all eighteen registered trials')
    if any(row.get('status') == 'running' or any(s.get('status') == 'running' for s in row['stages'])
           for row in progress['rows']):
        raise VisualError('cohort_running', 'A trial is running. Package only after it is terminal; no live session was inspected.')
    return registry, progress, file_sha256(registration), file_sha256(progress_path)


def render(data):
    esc = lambda v: html.escape(str(v))
    fmt = lambda v: '—' if v is None else f'{v:.4g}' if isinstance(v, float) else str(v)
    groups = ''.join('<tr>' + ''.join(f'<td>{esc(fmt(g.get(k)))}</td>' for k in (
        'domain', 'mode', 'independently_verified_successes', 'captured_numerical_successes', 'numerical_successes',
        'failed_trials', 'pending_trials', 'trials_waiting_independent_review',
        'candidate_count', 'elapsed_seconds', 'median_final_error', 'evaluated_stage_count','required_stage_count','error_units')) +
        f'<td>{esc("Pooled jump + ground measurements; no separate stage values supplied by summary." if g.get("domain")=="game" else "Available finalized measurements only.")}</td></tr>'
        for g in data['summary']['groups'])
    trials = []
    for row in data['rows']:
        stage_items = []
        for stage in row['stages']:
            link = f'<a href="{esc(stage["report_link"])}">Media and code diff</a>' if stage.get('report_link') else 'Report unavailable'
            detail = esc(stage.get('reason') or stage.get('review_reason') or '')
            worker = stage.get('terminal_validation') or {}
            worker_error = worker.get('error') or {}
            worker_detail = (f'<br>Sealed validation worker: {esc(worker.get("status"))}; '
                             f'{esc(worker_error.get("code", ""))} {esc(worker_error.get("message", ""))}'
                             '<br>This diagnostic does not replace the registered outcome.') if worker else ''
            times=stage.get('time_scopes',{})
            task=(fmt(times.get('native_task_wall_seconds')) if times.get('native_task_status')=='recorded_prompt_to_response' else
                f"censored prompt→terminal {fmt(times.get('native_prompt_to_terminal_wall_seconds'))}" if times.get('native_task_status')=='censored_no_recorded_response' else 'unavailable')
            stage_items.append(f'<li><b>{esc(stage["stage"])}</b>: {esc(stage["status"])} · {link}'
                f'<br>Captured numerical: {esc(stage.get("captured_numerical_pass", "pending"))}; registered native-qualified stage pass: {esc(stage.get("numerical_pass", "pending"))}'
                f'<br>Current image review: {esc(stage.get("independent_review_status", "pending"))}; source audit: {esc(stage.get("source_review_status", "pending"))}; native validation: {esc(stage.get("native_validation_status", "pending"))}'
                f'<br>Stage wall seconds: {esc(fmt(times.get("stage_wall_seconds")))}; native task wall seconds: {esc(task)} (includes tools/waits, not inference-only)'
                f'<br><code>{esc(stage.get("candidate_sha256") or "No selected candidate")}</code><br>{detail}{worker_detail}</li>')
        trials.append(f'<tr><td>{esc(row["id"])}</td><td>{esc(row["domain"])}</td><td>{esc(row["mode"])}</td><td>{esc(row["repeat"])}</td><td>{esc(row["status"])}</td><td><ul>{"".join(stage_items)}</ul></td></tr>')
    errors = esc(json.dumps(data['summary'].get('evidence_replay_errors', []), ensure_ascii=False, indent=2))
    costs = esc(json.dumps(data.get('cost_scopes', {'status':'unrecorded'}), ensure_ascii=False, indent=2))
    notes = ''.join(f'<li><b>{esc(n["title"])}</b>: {esc(n["detail"])}</li>'
                    for n in data.get('study_notes', {}).get('notes', []))
    notes_section = (f'<section class="note"><h2>Interpretation limits and diagnostic findings</h2><ul>{notes}</ul>'
                     '<p>These observations do not change registered outcomes, thresholds, or the denominator.</p></section>') if notes else ''
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>KiroClops · registered local cohort</title><style>body{{font:15px system-ui;background:#0d1625;color:#deebfb;margin:32px auto;padding:0 24px;max-width:1500px}}h1{{font-size:34px}}a{{color:#91d8ff}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border:1px solid #34455d;padding:9px;text-align:left;vertical-align:top}}section{{margin:28px 0;overflow:auto}}code,pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.note{{padding:18px;background:#182b41;border-left:4px solid #f0bf73}}li{{margin:6px 0}}</style>
<h1>KiroClops · 18 registered trials</h1><p>Model: {esc(data['summary'].get('model'))} / {esc(data['summary'].get('effort'))}. Integration: <b>{esc(data['summary']['integration_status'])}</b>. Incremental benefit: <b>{esc(data['summary']['incremental_benefit_status'])}</b>.</p>
<div class="note">Three independent repeats per domain and arm. These are descriptive results, not a general success-rate or significance claim. Failed and interrupted trials remain in the denominator; no replacement trials. Missing measurements are missing, never zero. Numerical results, independent visual review and source audit are separate.</div>
{notes_section}
<section><h2>Shared prerequisite cost · counted once</h2><p>Reference capture and analysis precede model trials. Their original computation cost is separate from stage time. Cached preparation is not first-time user-video latency; unknown times are not zero.</p><pre>{costs}</pre></section>
<section><h2>Six domain × mode groups</h2><p>Time is the sum of finished stage wall times, including prerequisite validation, preparation and final native audit. It is not model-only inference time. Shared prerequisites and later independent review are excluded. Native-qualified numerical success includes the native audit; captured numerical counts alone do not. Error medians cover only actual finalized measurements: missing stages are excluded from that metric, never from the three-trial success denominator. Game medians pool jump and ground; arms with different stage coverage are not a matched error comparison. Native prompt-to-response times appear separately below when recorded. Censored prompt-to-terminal times include cleanup; an outer harness total is unavailable without its own receipt.</p><table><thead><tr><th>Domain</th><th>Mode</th><th>Verified /3</th><th>Captured numerical /3</th><th>Native-qualified numerical /3</th><th>Failed</th><th>Pending</th><th>Review pending</th><th>Candidates</th><th>Stage wall seconds</th><th>Median error</th><th>Evaluated stages</th><th>Required stages</th><th>Units</th><th>Error scope</th></tr></thead><tbody>{groups}</tbody></table></section>
<section><h2>All registered trials</h2><table><thead><tr><th>Trial</th><th>Domain</th><th>Mode</th><th>Repeat</th><th>Status</th><th>Stages and evidence</th></tr></thead><tbody>{''.join(trials)}</tbody></table></section>
<section><h2>Evidence replay diagnostics</h2><pre>{errors}</pre></section><p><a href="cohort-report.json">Portable cohort data</a> · <a href="package-manifest.json">Packaged file hashes</a></p><p>Existing terminal stage reports are presentation copies. The validated cohort summary controls the group counts. This package contains no private project tree or native conversation transcript.</p></html>'''


def build(registration, output):
    registration = Path(registration).resolve(); output = Path(output).resolve()
    if output.exists():
        raise VisualError('output_exists', 'Use a fresh destination to preserve earlier reports')
    registry, progress, reg_sha, progress_sha = stable_snapshot(registration)
    notes_path=registration.parent/'study-notes.json'
    notes_hash=None
    study_notes={'notes': []}
    if notes_path.exists():
        notes_hash=file_sha256(notes_path)
        supplied=read_json(notes_path)
        if supplied.get('registration_sha256')!=reg_sha or not isinstance(supplied.get('notes'),list):
            raise VisualError('invalid_study_notes','Study notes must bind to this registration')
        if any(not isinstance(n,dict) or any(not isinstance(n.get(k),str) or not n[k].strip()
               for k in ('title','detail')) for n in supplied['notes']):
            raise VisualError('invalid_study_notes','Study notes require a title and factual detail')
        study_notes={'source_sha256':notes_hash,'notes':[
            {'title':n['title'],'detail':n['detail']} for n in supplied['notes']]}
    summary = cohort.summarize(registration)  # actual complete evidence replay
    if (file_sha256(registration), file_sha256(registration.parent / 'progress.json')) != (reg_sha, progress_sha):
        raise VisualError('cohort_changed', 'Cohort progressed during summary replay; no package was created')
    stable_snapshot(registration)
    output.mkdir(parents=True)
    rows = []
    for row in progress['rows']:
        item = {k: row[k] for k in ('id', 'domain', 'mode', 'repeat', 'status')}; item['stages'] = []
        for stage in row['stages']:
            current = {k: portable(stage[k]) for k in ('stage', 'status', 'numerical_pass', 'captured_numerical_pass', 'native_validation_status', 'independent_review_status', 'source_review_status', 'candidate_count') if k in stage}
            if stage.get('error'):
                current['reason'] = portable(stage['error'].get('message') or stage['error'].get('code'))
            # Never inspect even a report directory for a pending/nonterminal row.
            if row['status'] in TERMINAL and stage['status'] in TERMINAL and stage['status'] != 'not_run_predecessor_failed':
                session = Path(stage['session']); state_path = session / 'session.json'
                state = read_json(state_path) if state_path.is_file() else {}
                if state.get('active_job_id'):
                    raise VisualError('cohort_running', 'A terminal row still owns an active job; stop packaging')
                selected = state.get('selected_candidate_id')
                chosen = next((c for c in state.get('candidates', []) if c.get('candidate_id') == selected), {})
                current['candidate_sha256'] = chosen.get('source_sha256')
                acceptance=replay_stage_acceptance(session,state,chosen)
                current['current_acceptance_replay']=acceptance
                current['time_scopes']=stage_time_scopes(session,stage)
                current['terminal_validation']=terminal_validation_diagnostic(session,state,chosen)
                current.update(captured_numerical_pass=acceptance['captured_numerical_pass'],
                    independent_review_status=acceptance['independent_review_status'],source_review_status=acceptance['source_review_status'],
                    native_validation_status=acceptance['native_run_status'],review_status=acceptance['overall_acceptance'])
                report = session / 'report'
                if (report / 'report.json').is_file():
                    saved = read_json(report / 'report.json')
                    if not current['candidate_sha256'] or saved.get('candidate_sha256') != current['candidate_sha256']:
                        current['reason'] = 'Existing report omitted: selected candidate hash differs.'
                    else:
                        relative = Path('trials') / row['id'] / stage['stage']
                        copy_stage_report(report, output / relative,acceptance)
                        current['report_link'] = (relative / 'index.html').as_posix()
            item['stages'].append(current)
        rows.append(item)
    if (file_sha256(registration), file_sha256(registration.parent / 'progress.json')) != (reg_sha, progress_sha):
        raise VisualError('cohort_changed', 'Cohort progressed while copying; this incomplete output is not a report')
    data = {'kind': 'PortableLocalCohortReport', 'schema_version': 1,
        'registration_sha256': reg_sha, 'progress_sha256': progress_sha,
        'summary': portable(summary), 'budget': portable(registry.get('budget')),
        'cost_scopes': portable(cohort_costs(registry)),
        'study_notes':portable(study_notes),
        'rows': rows, 'execution': 'Presentation only; no model/game/browser/recorder/report generation job.'}
    if (file_sha256(notes_path) if notes_path.exists() else None)!=notes_hash:
        raise VisualError('evidence_changed','Study notes changed during report export')
    write_json(output / 'cohort-report.json', data)
    (output / 'index.html').write_text(render(data), encoding='utf-8')
    files = {p.relative_to(output).as_posix(): file_sha256(p) for p in sorted(output.rglob('*')) if p.is_file()}
    write_json(output / 'package-manifest.json', {'kind': 'PortableReportFiles', 'files': files})
    validate_links(output)
    return {'status': 'built', 'report': str(output / 'index.html'), 'trials': len(rows), 'groups': len(summary['groups']), 'files': len(files) + 1}


def self_test():
    # Synthetic fixture is temporary and never confused with registered evidence.
    with tempfile.TemporaryDirectory(prefix='kiroclops-report-smoke-') as temporary:
        root = Path(temporary); registration = root / 'registration.json'; rows = []
        for repeat in (1, 2, 3):
            for domain in ('game', 'panel', 'svg'):
                for mode in ('tools', 'baseline'):
                    name = f'{domain}-{mode}-{repeat:02}'
                    rows.append({'id': name, 'domain': domain, 'mode': mode, 'repeat': repeat, 'status': 'registered', 'stages': [{'stage': domain, 'session': str(root / name), 'status': 'registered'}]})
        registry = {'rows': deepcopy(rows), 'budget': {'max_candidates': 3, 'max_seconds': 1800}}
        write_json(registration, registry); progress = {'registration_sha256': file_sha256(registration), 'rows': rows}
        write_json(root / 'progress.json', progress)
        groups = [{'domain': d, 'mode': m, 'pending_trials': 3, 'required_trials': 3, 'numerical_successes': 0, 'independently_verified_successes': 0, 'median_final_error': None} for d in ('game', 'panel', 'svg') for m in ('tools', 'baseline')]
        summary = {'groups': groups, 'model': 'synthetic smoke only', 'effort': 'max', 'integration_status': 'pending', 'incremental_benefit_status': 'pending'}
        with patch.object(cohort, 'summarize', return_value=summary) as mocked:
            result = build(registration, root / 'out')
            assert result['trials'] == 18 and result['groups'] == 6
            mocked.assert_called_once()
        text = (root / 'out/cohort-report.json').read_text()
        assert str(root) not in text and 'candidate_sha256' not in text
        costs = read_json(root / 'out/cohort-report.json')['cost_scopes']
        assert costs['shared_prerequisites_counted_once'] is True
        assert costs['group_elapsed_is_model_only'] is False
        assert costs['shared_prerequisites']['first_time_user_video_latency_seconds'] is None
        assert 'Stage wall seconds' in (root / 'out/index.html').read_text()
        write_json(root/'study-notes.json',{'registration_sha256':'wrong','notes':[]})
        with patch.object(cohort,'summarize',side_effect=AssertionError('Wrong notes must block before replay')):
            try:build(registration,root/'wrong-notes')
            except VisualError as exc:assert exc.code=='invalid_study_notes'
            else:raise AssertionError('Cross-registration notes accepted')
        write_json(root/'study-notes.json',{'registration_sha256':file_sha256(registration),
            'notes':[{'title':'<Observed limit>','detail':'Synthetic interpretation only; no outcome override.',
                      'ignored_private_value':str(root)}]})
        with patch.object(cohort,'summarize',return_value=summary):
            build(registration,root/'with-notes')
        notes_data=read_json(root/'with-notes/cohort-report.json')
        assert notes_data['summary']==summary and len(notes_data['rows'])==18
        assert 'ignored_private_value' not in (root/'with-notes/cohort-report.json').read_text()
        assert '&lt;Observed limit&gt;' in (root/'with-notes/index.html').read_text()
        # A failed terminal stage may have a useful pre-existing report. Copy
        # exactly its named media/diff while leaving private siblings behind.
        session = root / rows[0]['id']; report = session / 'report'; report.mkdir(parents=True)
        write_json(session / 'session.json', {'active_job_id': None,
            'selected_candidate_id': 'candidate-01', 'candidates': [{'candidate_id': 'candidate-01', 'source_sha256': 'a' * 64}]})
        (report / 'media').mkdir(); (report / 'media/frame.png').write_bytes(b'synthetic-image-bytes')
        (report / 'media/presentation.mp4').write_bytes(b'synthetic-video-bytes')
        (report / 'candidate.diff').write_text('--- before/motion.js\n+++ after/motion.js\n-test\n+changed\n')
        (report / 'private-controller.js').write_text('PRIVATE MUST NOT COPY')
        saved = {'kind': 'LocalEvidenceReport', 'candidate_sha256': 'a' * 64,
            'candidate_diff': 'candidate.diff', 'goal': 'Synthetic terminal fixture',
            'groups': [{'id': 'one', 'roles': {'reference': {'frames': [{'path': 'media/frame.png', 'time_s': 0}],
                'video': 'media/presentation.mp4', 'input_origin_s': 0, 'image_size': [1, 1]}}}],
            'acceptance_details': {'pass': False, 'host_diagnostic': str(root)}}
        saved['engineering_control']={'kind':'OperatorFixtureControllerChange','operator':'Synthetic test driver',
                                     'model_started':False,'kiro_success_claim':False}
        write_json(report / 'report.json', saved)
        progress['rows'][0]['status'] = 'failed'; progress['rows'][0]['stages'][0]['status'] = 'failed'
        write_json(root / 'progress.json', progress)
        with patch.object(cohort, 'summarize', return_value=summary):
            build(registration, root / 'terminal-out')
        packaged = root / 'terminal-out/trials' / rows[0]['id'] / 'game'
        assert (packaged / 'media/frame.png').is_file() and (packaged / 'candidate.diff').is_file()
        assert not (packaged / 'private-controller.js').exists()
        assert 'reference' in read_json(packaged / 'report.json')['groups'][0]['roles']
        assert str(root) not in (packaged / 'report.json').read_text()
        assert read_json(packaged / 'report.json')['engineering_control']==saved['engineering_control']
        assert 'not autonomous Kiro success evidence' in (packaged / 'index.html').read_text()
        progress['rows'][0]['status'] = 'running'; write_json(root / 'progress.json', progress)
        with patch.object(cohort, 'summarize', side_effect=AssertionError('Live summary must not run')):
            try:
                build(registration, root / 'blocked')
            except VisualError as exc:
                assert exc.code == 'cohort_running'
            else:
                raise AssertionError('Running cohort was not blocked')
        source = root / 'asset-check'; source.mkdir(); (source / 'safe.json').write_text('{}')
        try:
            safe_file(source, '../registration.json', {'.json'})
        except VisualError:
            pass
        else:
            raise AssertionError('Escaping path was accepted')
    return {'status': 'pass', 'scope': 'Synthetic pending18/groups6, terminal media/diff packing, private-source exclusion, running-cohort guard, local links and escaping-path guard; no real cohort or model run.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registration', type=Path); parser.add_argument('--output', type=Path)
    parser.add_argument('--self-test', action='store_true'); args = parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False))
    elif args.registration and args.output:
        print(json.dumps(build(args.registration, args.output), ensure_ascii=False))
    else:
        parser.error('Use --registration and --output, or --self-test')
