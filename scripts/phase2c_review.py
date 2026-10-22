"""Separate post-hoc Heart review commands and self-contained local report snapshots."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from kiroclops import portable_review_v2 as review
from kiroclops.portable import cohort, reporting, review as visual_review
from kiroclops.integration.storage import project_hashes, read_json, sha256, utc_now, write_json
from kiroclops.portable.session import digest

VERSION = 'phase2c-posthoc-report-v2'


def _regular(path, *, within=None):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked/reparse report paths are forbidden')
            if part.is_file() and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise ValueError('Only independent regular report files are allowed')
    if within is not None and not path.is_relative_to(Path(within).resolve()):
        raise ValueError('Report evidence escaped its authorized root')
    return path


def _copy(source, destination, *, within):
    source, destination = _regular(source, within=within), _regular(destination)
    if not source.is_file():
        raise ValueError('Required report evidence is missing: ' + str(source))
    expected = sha256(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256(source) != expected or sha256(destination) != expected:
        raise ValueError('Report evidence changed during copy')
    return expected


def _tree(directory):
    directory = _regular(directory)
    files = []
    for current, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs:
            _regular(Path(current) / name, within=directory)
        for name in names:
            files.append(_regular(Path(current) / name, within=directory))
    return sorted(files)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []

    def handle_starttag(self, tag, attrs):
        self.links += [value for key, value in attrs if key in ('href', 'src', 'poster') and value]


def validate_links(directory):
    """Validate HTML and the original viewer's dynamic native-video URLs."""
    directory = _regular(directory)
    checked = 0
    for file in _tree(directory):
        if file.suffix.lower() != '.html':
            continue
        text = file.read_text(encoding='utf-8')
        parser = _Links(); parser.feed(text)
        links = list(parser.links)
        for match in re.finditer(r'const runs=(.*?);const roles=', text, flags=re.S):
            values = json.loads(match.group(1))
            if not isinstance(values, list):
                raise ValueError('Dynamic report rows are malformed')
            for row in values:
                if not isinstance(row.get('videos'), dict):
                    raise ValueError('Dynamic video map is missing')
                links.extend(row['videos'].values())
        for value in links:
            if not isinstance(value, str):
                raise ValueError('Report link is not a string')
            if value.startswith('#'):
                continue
            parsed = urlsplit(value)
            decoded = unquote(parsed.path)
            if parsed.scheme or parsed.netloc or '\\' in decoded or not decoded or Path(decoded).is_absolute():
                raise ValueError('Report link is not a portable local relative path: ' + value)
            target = _regular((file.parent / decoded).resolve(), within=directory)
            if not target.is_file():
                raise ValueError('Report media/link is missing: ' + value)
            checked += 1
    return {'status': 'pass', 'local_links_checked': checked,
            'dynamic_video_urls_checked': True}


def _seconds(start, end):
    try:
        a, b = datetime.fromisoformat(start.replace('Z', '+00:00')), datetime.fromisoformat(end.replace('Z', '+00:00'))
        value = (b-a).total_seconds()
        return value if a.tzinfo is not None and b.tzinfo is not None and value >= 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def _status(values):
    return 'fail' if 'fail' in values else 'pass' if values and all(v == 'pass' for v in values) else 'pending'


def _read_optional(path):
    return read_json(_regular(path)) if Path(path).is_file() else None


def _selected_source(directory, state):
    cid = state.get('selected_candidate_id')
    if not isinstance(cid, str) or not re.fullmatch(r'candidate-\d+', cid):
        raise ValueError('Selected candidate identity is missing')
    path = _regular(directory / 'candidates' / cid / 'candidate.json', within=directory)
    candidate = read_json(path)
    project = _regular(candidate['project_path'], within=directory)
    if (project != directory / 'candidates' / cid / 'project' or candidate.get('candidate_id') != cid
            or project_hashes(project) != candidate.get('source_hashes')
            or digest(candidate['source_hashes']) != candidate.get('source_sha256')):
        raise ValueError('Selected source identity changed')
    return candidate, path, project


def _source_record(directory, state, stage, path):
    """Bind an explicit reviewer verdict; a PASS still needs the full audit replay."""
    path = _regular(path, within=directory)
    candidate, _, _ = _selected_source(directory, state)
    record = read_json(path)
    launch = read_json(_regular(directory / 'kiro/launch.json'))
    expected = {rel: value for rel, value in candidate['source_hashes'].items()
                if rel.endswith('.gd') or rel in {'DefaultMovementData.tres', 'player.tscn'}}
    findings = record.get('findings', [])
    if (record.get('source') not in {'human', 'assistant_visual'} or record.get('verdict') not in {'pass', 'fail'}
            or record.get('candidate_id') != candidate['candidate_id']
            or record.get('source_sha256') != candidate['source_sha256']
            or record.get('trace_sha256') != sha256(directory / 'kiro/stdout.jsonl')
            or record.get('inspected_files') != expected
            or _seconds(launch.get('finished_at'), record.get('reviewed_at')) is None
            or len(findings) != len(review.original.CHECKS[stage])
            or {f.get('check') for f in findings} != review.original.CHECKS[stage]
            or any(f.get('verdict') not in {'pass', 'fail'} or not isinstance(f.get('evidence'), str)
                   or not f['evidence'].strip() for f in findings)
            or (record['verdict'] == 'pass') != all(f['verdict'] == 'pass' for f in findings)):
        raise ValueError('Source reviewer verdict is not bound to this selected candidate')
    return record


def _source_addendum(directory, state, stage):
    path = directory / 'review/source-review-addendum.json'
    if not path.is_file():
        return {'status': 'absent'}
    result = {'status': 'fail', 'valid': False, 'path': str(path), 'sha256': sha256(_regular(path))}
    try:
        record = read_json(path)
        candidate, candidate_path, project = _selected_source(directory, state)
        prior_path = _regular(record['prior_review']['path'], within=directory)
        prior = _source_record(directory, state, stage, prior_path)
        checks = record.get('affected_checks', [])
        findings = record.get('findings', [])
        if (record.get('schema_version') != 1 or record.get('review_kind') != 'immutable_source_scope_addendum'
                or not isinstance(record.get('finding_id'), str) or not record['finding_id']
                or record.get('verdict') != 'fail' or record.get('effective_source_verdict') != 'fail'
                or record.get('session_id') != state['session_id'] or record.get('stage') != stage
                or record.get('candidate_id') != candidate['candidate_id']
                or record.get('source_sha256') != candidate['source_sha256']
                or record['prior_review'].get('verdict') != prior['verdict']
                or record['prior_review'].get('sha256') != sha256(prior_path)
                or _seconds(prior.get('reviewed_at'), record.get('reviewed_at')) is None
                or not checks or len(checks) != len(set(checks)) or not set(checks) <= review.original.CHECKS[stage]
                or len(findings) != len(checks) or {f.get('check') for f in findings} != set(checks)
                or any(f.get('verdict') != 'fail' or not isinstance(f.get('evidence'), str)
                       or not f['evidence'].strip() for f in findings)):
            raise ValueError('Source addendum identity, prior verdict, chronology or findings differ')
        evidence = record.get('evidence_files', {})
        required = [candidate_path, project / 'player.gd', directory / 'kiro/stdout.jsonl',
                    directory / 'kiro/launch.json', prior_path]
        root = directory.parent.parent
        if not isinstance(evidence, dict) or any(str(p) not in evidence for p in required):
            raise ValueError('Source addendum lacks required source/review/native bindings')
        for name, expected in evidence.items():
            if sha256(_regular(name, within=root)) != expected:
                raise ValueError('Source addendum evidence changed')
        if sha256(path) != result['sha256']:
            raise ValueError('Source addendum changed during verification')
        result.update(valid=True, finding_id=record['finding_id'], affected_checks=checks,
                      reviewed_at=record['reviewed_at'], evidence_files=evidence,
                      runtime_confirmation=record.get('runtime_confirmation'),
                      reason='Immutable source review addendum supersedes the historical source verdict')
    except Exception as error:
        result['reason'] = str(error)
    return result


def _inherit_parent(row, state, parent_row, parent_state, directory):
    parent_status = parent_row['status']
    binding = 'pending'
    if parent_state is not None and state.get('parent'):
        try:
            parent_dir = directory.parent / parent_row['session_id']
            candidate, candidate_path, _ = _selected_source(parent_dir, parent_state)
            parent = state['parent']
            if (Path(parent['session']).resolve() != parent_dir or parent.get('candidate_id') != candidate['candidate_id']
                    or parent.get('source_hashes') != candidate['source_hashes']
                    or parent.get('source_sha256') != candidate['source_sha256']
                    or parent.get('candidate_record_sha256') != sha256(candidate_path)
                    or state.get('baseline_hashes') != candidate['source_hashes']):
                raise ValueError('Ground baseline differs from its registered selected jump parent')
            binding = 'pass'
        except Exception as error:
            binding = 'fail'; row['parent_binding_error'] = str(error)
    elif parent_state is not None and state.get('status') != 'new':
        binding = 'fail'
    row['own_acceptance_status'] = row['status']
    row['parent_jump'] = {'session_id': parent_row['session_id'], 'effective_acceptance': parent_status,
                          'source_review': parent_row['source_review'], 'binding_status': binding}
    row['status'] = _status([row['status'], parent_status, binding])


def _visual_replay(directory, state, stage, saved):
    """Replay only independent pixel observations, without granting full acceptance."""
    if not saved.get('observations_path'):
        return {'status': 'pending', 'reason': 'No audit-bound independent observation path is available'}
    evidence, groups = {}, {}
    result = {'status': 'fail', 'groups': groups,
              'scope': 'Independent raw-pixel observation replay only; not a full V2 audit, source or gameplay acceptance.'}
    try:
        root = directory.parent.parent
        candidate, candidate_path, _ = _selected_source(directory, state)
        def bind(path, within=root):
            path = _regular(path, within=within)
            evidence[str(path)] = sha256(path)
            return path
        observations_path = bind(saved['observations_path'])
        comparison_path = bind(directory / 'final/comparison.json', directory)
        bind(candidate_path, directory)
        observations, comparison = read_json(observations_path), read_json(comparison_path)
        if (observations.get('candidate_id') != candidate['candidate_id']
                or observations.get('source_sha256') != candidate['source_sha256']
                or observations.get('comparison_sha256') != evidence[str(comparison_path)]
                or comparison.get('candidate_id') != candidate['candidate_id']
                or comparison.get('source_sha256') != candidate['source_sha256']
                or comparison.get('session_id') != state['session_id']
                or saved.get('comparison_sha256', evidence[str(comparison_path)]) != evidence[str(comparison_path)]):
            raise ValueError('Independent observations differ from the exact selected candidate/comparison')
        mechanics = {'jump'} if stage == 'jump' else {'ground', 'jump'}
        if set(comparison.get('runs', {})) != mechanics or set(observations.get('groups', {})) != mechanics:
            raise ValueError('Independent visual mechanic groups differ')
        for mechanic in sorted(mechanics):
            runs, controls = comparison['runs'][mechanic], observations['groups'][mechanic]
            if set(runs) != {'reference', 'before', 'after'} or set(controls) != set(runs):
                raise ValueError('Independent visual role groups differ')
            for role, values in runs.items():
                paths = [bind(path, directory) for path in values]
                if len(paths) != (12 if mechanic == 'jump' else 18) or len(set(paths)) != len(paths):
                    raise ValueError('Independent visual final matrix is incomplete or duplicated')
                control = controls[role]
                if control.get('analysis_files') != {str(path): evidence[str(path)] for path in paths}:
                    raise ValueError('Independent visual analysis file linkage differs')
                for path in paths:
                    analysis = read_json(path)
                    run = _regular(analysis['run_id'], within=root)
                    manifest_path = bind(run / 'manifest.json')
                    manifest = read_json(manifest_path)
                    if analysis.get('provenance', {}).get('manifest_sha256') != evidence[str(manifest_path)]:
                        raise ValueError('Independent visual analysis manifest linkage changed')
                    if role != 'reference':
                        expected = (candidate['source_hashes'] if role == 'after' else
                            state['original_baseline']['source_hashes'] if mechanic == 'jump' and stage == 'ground'
                            else state['baseline_hashes'])
                        provenance = manifest.get('provenance', {})
                        if provenance.get('full_source_hashes_before') != expected or provenance.get('full_source_hashes_after') != expected:
                            raise ValueError('Independent visual capture source differs from its role')
                for point in control.get('points', []):
                    bind(Path(point['run']) / 'frames' / f'{point["frame_index"]:06d}.png')
                for event in control.get('events', []):
                    for index in event.get('frame_hashes', {}):
                        bind(Path(event['run']) / 'frames' / f'{int(index):06d}.png')
                checked = visual_review.validate_observations(paths, control, minimum=20,
                    require_events=mechanic == 'jump',
                    required_categories=('brake', 'reverse', 'flip') if mechanic == 'ground' else ())
                groups[mechanic + '/' + role] = {key: value for key, value in checked.items()
                                                if key not in {'points', 'analysis_files'}}
                if checked.get('status') != 'pass':
                    raise ValueError('Independent visual observations failed or are incomplete')
        if any(sha256(path) != expected for path, expected in evidence.items()):
            raise ValueError('Independent visual evidence changed during replay')
        result.update(status='pass', candidate_id=candidate['candidate_id'], source_sha256=candidate['source_sha256'],
                      observations_path=str(observations_path), observations_sha256=evidence[str(observations_path)],
                      comparison_sha256=evidence[str(comparison_path)], evidence_files=evidence,
                      validator_sha256=sha256(Path(visual_review.__file__)))
    except Exception as error:
        result['reason'] = str(error)
    return result


def _session_summary(directory, stage, stage_run, verification):
    state = read_json(_regular(directory / 'session.json'))
    launch = _read_optional(directory / 'kiro/launch.json') or {}
    final = _read_optional(directory / 'final/comparison.json') or {}
    saved = _read_optional(directory / 'final' / review.DESTINATION) or {}
    if verification['status'] == 'pass' and sha256(directory / 'final' / review.DESTINATION) != verification.get('audit_sha256'):
        raise ValueError('Replayed review receipt changed before reporting')
    known_fail = (state.get('status') in ('closed_failed', 'failed', 'cancelled')
        or launch.get('status') in ('timeout', 'timed_out', 'failed', 'cancelled', 'error')
        or any(final.get(k) == 'fail' for k in ('numerical_status', 'automatic_integrity_status', 'normal_gameplay_status'))
        or stage_run.get('status') == 'fail')
    status = 'fail' if known_fail or verification['status'] == 'fail' else verification['status']
    if status not in ('pass', 'fail'):
        status = 'pending'
    counts = {mechanic: {'passed': value.get('passed'), 'required': value.get('required'),
                         'status': value.get('numerical_status')}
              for mechanic, value in final.get('comparisons', {}).items()}
    candidates = state.get('candidates')
    elapsed = launch.get('elapsed_seconds')
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        elapsed = None
    jobs = []
    for path in sorted((directory / 'jobs').glob('job-*.json')):
        value = read_json(_regular(path))
        jobs.append({'job_id': value.get('job_id'), 'action': value.get('action'),
                     'candidate_id': value.get('candidate_id'), 'status': value.get('status'),
                     'seconds': _seconds(value.get('started_at'), value.get('finished_at'))})
    gates = saved.get('gates', {}) if verification['status'] == 'pass' else {}
    row = {'session_id': state['session_id'], 'stage': stage, 'status': status,
           'session_status': state.get('status'), 'launch_status': launch.get('status', 'not_started'),
           'candidate_count': len(candidates) if isinstance(candidates, list) else None,
           'selected_candidate_id': state.get('selected_candidate_id'), 'elapsed_seconds': elapsed,
           'job_durations': jobs, 'numerical_status': final.get('numerical_status', 'pending'),
           'numerical_counts': counts, 'automatic_integrity_status': final.get('automatic_integrity_status', 'pending'),
           'source_review': gates.get('independent_source_review', {}).get('status', 'pending'),
           'manual_review': gates.get('independent_visual', {}).get('status', 'pending'),
           'review_replay': verification, 'review_version': review.VERSION,
           'original_review_status': (_read_optional(directory / 'final/independent-review.json') or {}).get('status', 'pending'),
           'recorded_measurement_status': final.get('measurement_status', 'pending')}
    row['historical_v2_replay_status'] = verification['status']
    row['historical_source_review'] = row['source_review']
    row['historical_v2_manual_gate'] = row['manual_review']
    # A source rejection can stop the full audit before it reaches its visual
    # gate. Replay that separate gate here; it can never promote row.status.
    if verification['status'] != 'pass' and state.get('status') in ('finished', 'closed_failed', 'failed') and not state.get('active_job_id'):
        row['independent_visual_replay'] = _visual_replay(directory, state, stage, saved)
        row['manual_review'] = row['independent_visual_replay']['status']
        if row['manual_review'] == 'fail':
            row['status'] = 'fail'
    source_path = saved.get('source_review_path') or directory / 'review/source-review.json'
    if Path(source_path).is_file():
        try:
            record = _source_record(directory, state, stage, source_path)
            row['bound_source_record_verdict'] = record['verdict']
            row['bound_source_record'] = {'path': str(source_path), 'sha256': sha256(source_path)}
            if record['verdict'] == 'fail':
                row['source_review'] = row['status'] = 'fail'
        except Exception as error:
            row['source_record_error'] = str(error)
            row['source_review'] = row['status'] = 'fail'
    row['source_addendum'] = _source_addendum(directory, state, stage)
    if row['source_addendum']['status'] == 'fail':
        row['source_review'] = row['status'] = 'fail'
    return row, state, saved


def _write_session_view(directory, row, state, saved, root):
    report = _regular(directory / 'report', within=directory)
    report.mkdir(exist_ok=True)
    standard = report / 'index.html'
    # Preserve an existing model/original report byte-for-byte. A live stage gets
    # only a derived pending view, without rendering incomplete held-out files.
    if not standard.exists() and state.get('status') in ('finished', 'closed_failed', 'failed') and not state.get('active_job_id'):
        reporting.build_session_report(directory)
    original_hashes = {str(p): sha256(p) for p in (standard, report / 'summary.json') if p.is_file()}
    evidence = report / 'review-v2-evidence'
    links, copied = [], {}
    for rel, label in [('session.json', 'Session record'), ('kiro/launch.json', 'Model run record'),
                       ('kiro/stdout.jsonl', 'Raw local tool log'), ('kiro/tool-audit.jsonl', 'Permission and scope log'),
                       ('kiro/outbound.jsonl', 'Single-task submission log'),
                       ('final/comparison.json', 'Numerical comparison'),
                       ('review/source-review.json', 'Bound source review'),
                       ('review/source-review-addendum.json', 'Immutable source review addendum'),
                       ('final/independent-review.json', 'Original independent audit'),
                       ('final/' + review.DESTINATION, 'Second review version')]:
        source = directory / rel
        if source.is_file():
            destination = evidence / rel
            copied[str(source)] = _copy(source, destination, within=directory)
            links.append((destination.relative_to(report).as_posix(), label))
    for key in ('observations_path', 'source_review_path'):
        if saved.get(key):
            source = _regular(saved[key], within=root)
            if source.is_file():
                destination = evidence / (key + '.json')
                copied[str(source)] = _copy(source, destination, within=root)
                links.append((destination.relative_to(report).as_posix(), key))
    addendum = row.get('source_addendum', {})
    bindings = dict(addendum.get('evidence_files', {})) if addendum.get('valid') else {}
    visual = row.get('independent_visual_replay', {})
    if visual.get('status') == 'pass':
        bindings.update(visual['evidence_files'])
    for proof in (row.get('bound_source_record'), addendum if addendum.get('path') else None):
        if proof:
            bindings[proof['path']] = proof['sha256']
    for index, (name, expected) in enumerate(bindings.items()):
        source = _regular(name, within=root)
        if sha256(source) != expected:
            raise ValueError('Bound source review evidence changed before packaging')
        if name not in copied:
            destination = evidence / 'source-addendum' / (f'{index:02}-' + source.name)
            copied[name] = _copy(source, destination, within=root)
            links.append((destination.relative_to(report).as_posix(), 'Additional source evidence: ' + source.name))
        if copied[name] != expected:
            raise ValueError('Copied source review differs from its verified binding')
    notice = ('<section><h2>Post-experiment review — v2</h2>'
        '<p>This view evaluates the local notification format of blocked file reads with a separate audit version. '
        'The frozen model experiment, thresholds and raw logs were not changed. The status of the original report is kept separate.</p>'
        f'<p>Historical V2 audit replay: {row["historical_v2_replay_status"]}. '
        'Later source findings and the acceptance of the parent jump stage are applied separately.</p>'
        f'<p class="{row["status"]}">Current independent acceptance: <strong>{row["status"]}</strong></p>')
    if standard.is_file():
        notice += '<p><a href="index.html">Open the original index.html report</a></p>'
    notice += '<ul>' + ''.join(f'<li><a href="{html.escape(path, quote=True)}">{html.escape(label)}</a></li>' for path, label in links) + '</ul>'
    notice += '<pre>' + html.escape(json.dumps(row, ensure_ascii=False, indent=2)) + '</pre></section>'
    if standard.is_file():
        text = standard.read_text(encoding='utf-8')
        text = re.sub(r'<p class="(?:pass|fail|pending)">Independent acceptance: (?:pass|fail|pending)</p>',
                      '<p>The acceptance assessment of the original report is preserved in the linked index.html file.</p>', text, count=1)
        if '<main>' not in text:
            raise ValueError('Original report has no expected document container')
        text = text.replace('<main>', '<main>' + notice, 1)
    else:
        text = reporting._document(row['session_id'], notice + '<p>The standard completed report is not available yet.</p>')
    (report / 'index-v2.html').write_text(text, encoding='utf-8')
    write_json(report / 'summary-v2.json', row)
    if any(sha256(path) != expected for path, expected in original_hashes.items()):
        raise ValueError('Original report was changed by the post-hoc wrapper')
    for source, expected in copied.items():
        if sha256(source) != expected:
            raise ValueError('Session evidence changed while packaging')
    validate_links(report)
    return report


def build_report(registry_path, *, output=None):
    registry_path = _regular(registry_path)
    registry = cohort.verify_registry(registry_path)
    registry_hash = sha256(registry_path)
    root = _regular(registry['root'])
    reports_root = root / 'artifacts/phase2c/reports'
    output = _regular(output or reports_root / (registry['cohort_id'] + '-posthoc-v2'), within=reports_root)
    if output.exists():
        raise ValueError('Use a fresh report snapshot directory; existing bundles are preserved')
    output.mkdir(parents=True)
    cohort_run = _read_optional(root / 'artifacts/phase2c/cohorts' / registry['cohort_id'] / 'run.json') or {}
    progress = {(chain['chain_id'], stage): info for chain in cohort_run.get('chains', [])
                for stage, info in chain.get('stages', {}).items()}
    # Independent read-only replays are expensive. Never run more than three,
    # and never replay a live stage while it is still writing capture evidence.
    replay = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        pending = {}
        for chain in registry['chains']:
            for stage in ('jump', 'ground'):
                sid = chain[stage + '_session']
                directory = _regular(root / 'sessions' / sid, within=root / 'sessions')
                state = _read_optional(directory / 'session.json')
                if state is None:
                    continue
                if state.get('status') in ('finished', 'closed_failed', 'failed') and not state.get('active_job_id'):
                    pending[sid] = executor.submit(review.verify_review, directory)
                else:
                    replay[sid] = {'status': 'pending', 'reason': 'Stage is not terminal; live evidence is not replayed'}
        for sid, future in pending.items():
            replay[sid] = future.result()
    chains = []
    for chain in registry['chains']:
        stages = {}
        parent_state = None
        for stage in ('jump', 'ground'):
            sid = chain[stage + '_session']
            directory = _regular(root / 'sessions' / sid, within=root / 'sessions')
            destination = output / sid
            stage_run = progress.get((chain['chain_id'], stage), {})
            if (directory / 'session.json').is_file():
                row, state, saved = _session_summary(directory, stage, stage_run, replay[sid])
                if row['session_id'] != sid:
                    raise ValueError('Session identity differs from its registered cohort member')
                if stage == 'jump':
                    parent_state = state
                else:
                    _inherit_parent(row, state, stages['jump'], parent_state, directory)
                source = _write_session_view(directory, row, state, saved, root)
                source_files_before = {p.relative_to(source).as_posix(): sha256(p) for p in _tree(source)}
                copied = {}
                for path in _tree(source):
                    rel = path.relative_to(source)
                    copied[rel.as_posix()] = _copy(path, destination / rel, within=source)
                if ({p.relative_to(source).as_posix(): sha256(p) for p in _tree(source)} != source_files_before
                        or copied != source_files_before):
                    raise ValueError('Source report changed while packaging')
                if {p.relative_to(destination).as_posix(): sha256(p) for p in _tree(destination)} != copied:
                    raise ValueError('Copied report tree differs from its verified source files')
                validate_links(destination)
                row['packaged_files'] = copied
            else:
                row = {'session_id': sid, 'stage': stage,
                       'status': 'fail' if stage_run.get('status') in ('fail', 'not_run') else 'pending',
                       'session_status': 'not_started', 'candidate_count': 0, 'elapsed_seconds': None,
                       'numerical_status': 'pending', 'numerical_counts': {}, 'source_review': 'pending',
                       'manual_review': 'pending', 'reason': stage_run.get('reason', stage_run.get('error'))}
                destination.mkdir()
                (destination / 'index-v2.html').write_text(reporting._document(sid,
                    '<p>This registered stage has not started yet. Acceptance: ' + row['status'] + '</p>'), encoding='utf-8')
            row['report'] = sid + '/index-v2.html'
            stages[stage] = row
        chains.append({'chain_id': chain['chain_id'], 'status': _status([v['status'] for v in stages.values()]), 'stages': stages})
    evidence_links = []
    inputs = [registry_path]
    cohort_directory = root / 'artifacts/phase2c/cohorts' / registry['cohort_id']
    inputs += sorted(cohort_directory.glob('*diagnosis*.json'))
    if (cohort_directory / 'run.json').is_file():
        inputs.append(cohort_directory / 'run.json')
    source_files = {}
    for index, path in enumerate(inputs):
        destination = output / 'evidence' / (f'{index:02}-' + path.name)
        source_files[str(path)] = _copy(path, destination, within=root)
        evidence_links.append((destination.relative_to(output).as_posix(), path.name))
    if cohort.verify_registry(registry_path) != registry or sha256(registry_path) != registry_hash:
        raise ValueError('Frozen registry changed while reporting')
    summary = {'schema_version': 1, 'report_version': VERSION, 'generated_at': utc_now(),
        'cohort_id': registry['cohort_id'], 'registry_path': str(registry_path), 'registry_sha256': registry_hash,
        'registry_verified': True, 'review_version': review.VERSION, 'review_sources': review.source_binding(),
        'review_replay_max_workers': 3,
        'report_script_sha256': sha256(Path(__file__)), 'chains': chains, 'required_chains': 3, 'required_stages': 6,
        'passed_chains': sum(c['status'] == 'pass' for c in chains),
        'passed_stages': sum(s['status'] == 'pass' for c in chains for s in c['stages'].values()),
        'status': _status([c['status'] for c in chains]), 'copied_source_evidence': source_files,
        'scope': 'Explicit post-hoc review v2. Original reports, outcomes and registered thresholds remain separate and unchanged.'}
    body = '<p>Three registered chains / six separate CLI stages · GPT-5.6 Sol / max</p>'
    body += '<section><h2>Independent post-experiment review v2</h2><p>This report is not a new model experiment. '
    body += 'A separate review version is used for blocked local-read notifications. Every pass result is re-verified while the report is being prepared. '
    body += 'Old reports and old rejection reasons are preserved; missing stages are not counted as completed.</p>'
    body += f'<p>Acceptance: {summary["status"]}; chains {summary["passed_chains"]}/3, stages {summary["passed_stages"]}/6.</p></section>'
    body += '<p>Historical V2 PASS records are preserved. Later bound source rejections and the rejection status of the parent jump stage are reflected in the current acceptance.</p>'
    body += '<table><tr><th>Chain / stage</th><th>Candidates</th><th>Time (s)</th><th>Numerical</th><th>Source</th><th>Visual</th><th>Current acceptance</th></tr>'
    for chain in chains:
        for stage, row in chain['stages'].items():
            cells = [str(row['candidate_count']), str(row.get('elapsed_seconds') if row.get('elapsed_seconds') is not None else 'not available'),
                     json.dumps(row.get('numerical_counts', {}), ensure_ascii=False), row['source_review'], row['manual_review'], row['status']]
            body += '<tr><td><a href="' + html.escape(row['report'], quote=True) + '">' + html.escape(chain['chain_id'] + ' / ' + stage) + '</a></td>'
            body += ''.join('<td>' + html.escape(cell) + '</td>' for cell in cells) + '</tr>'
    body += '</table><p>The time covers the entire model run; it includes recording/tool waits and is not pure thinking time.</p><ul>'
    body += ''.join('<li><a href="' + html.escape(p, quote=True) + '">' + html.escape(label) + '</a></li>' for p, label in evidence_links) + '</ul>'
    (output / 'index.html').write_text(reporting._document('KiroClops — Heart / independent review v2', body), encoding='utf-8')
    summary['link_validation'] = validate_links(output)
    write_json(output / 'summary.json', summary)
    return output / 'index.html'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    audit_parser = sub.add_parser('audit')
    audit_parser.add_argument('session', type=Path)
    audit_parser.add_argument('--observations', type=Path, required=True)
    audit_parser.add_argument('--source-review', type=Path, required=True)
    audit_parser.add_argument('--original-rejection', type=Path)
    verify_parser = sub.add_parser('verify'); verify_parser.add_argument('session', type=Path)
    report_parser = sub.add_parser('report'); report_parser.add_argument('cohort')
    report_parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'audit':
        saved = review.audit_session(args.session.resolve(), args.observations.resolve(), args.source_review.resolve(),
            original_rejection_path=args.original_rejection.resolve() if args.original_rejection else None)
        result = {key: saved.get(key) for key in ('status', 'audit_version', 'candidate_id', 'source_sha256')}
        result['receipt_path'] = str(args.session.resolve() / 'final' / review.DESTINATION)
        if saved.get('reason') is not None:
            result['reason'] = saved['reason']
    elif args.command == 'verify':
        result = review.verify_review(args.session.resolve())
    else:
        registry = Path(args.cohort)
        if not registry.is_file():
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', args.cohort):
                parser.error('Use a cohort identifier or an existing registry file')
            registry = ROOT / 'config/phase2c/cohorts' / (args.cohort + '.json')
        result = {'report': str(build_report(registry, output=args.output))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get('status') == 'fail' else 0


if __name__ == '__main__':
    raise SystemExit(main())
