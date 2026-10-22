"""Read-only acceptance for the preregistered ground-motion cohort.

No discovery, captures, re-fitting, model calls or source/receipt repair. Runtime
proof helpers are shared with Phase 2A; its jump-only aggregation is not used.
"""
from __future__ import annotations

import html
import hashlib
import json
from datetime import datetime
from pathlib import Path
import statistics

from kiroclops.common import ROOT
from . import benchmark as proof
from .experiments import GROUND_ID, get_experiment, run_name, verify_ground_lock
from .storage import identifier, project_hashes, read_json, sha256, write_json


class ExperimentMismatch(ValueError):
    """An explicit requested session cannot be silently relabeled."""


def _gate(status='unverified', reason=None, **fields):
    return {'status': status, **({'reason': reason} if reason else {}), **fields}


def _owned(base, raw, *, exists=True):
    base = Path(base).resolve()
    path = Path(raw)
    path = path if path.is_absolute() else base / path
    resolved = path.resolve()
    if not resolved.is_relative_to(base) or resolved == base:
        raise ValueError('Evidence escapes its selected session')
    for item in (path, *path.parents):
        if item == base:
            break
        if item.exists() and (item.is_symlink() or getattr(item.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Linked evidence is not accepted')
    if exists and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def _cohort():
    path = _owned(ROOT, 'config/phase2b/cohort.json')
    record = read_json(path)
    cli, ide = record.get('cli_session_ids'), record.get('ide_session_ids')
    if (record.get('schema_version') != 1 or record.get('experiment_id') != GROUND_ID
            or record.get('registered_before_runs') is not True or not isinstance(cli, list) or len(cli) != 3
            or not isinstance(ide, list) or len(ide) != 1 or record.get('baseline_session_ids') != []
            or len(set(cli + ide)) != 4 or record.get('replacement_runs') is not False
            or record.get('parent_session_id') != 'ide-02' or record.get('parent_candidate_id') != 'candidate-01'
            or record.get('model') != proof.EXPECTED_MODEL or record.get('effort') != proof.EXPECTED_EFFORT
            or record.get('max_candidates') != 3 or record.get('max_seconds') != 1800):
        raise ValueError('Ground cohort must preregister three CLI runs and one IDE run, without a tool-free arm')
    for name in cli + ide:
        identifier(name)
    return record, sha256(path)


def _state_integrity(session, state, candidate, lock):
    if lock is None:
        return _gate(reason='Ground experiment lock is unavailable')
    try:
        times = [datetime.fromisoformat(value.replace('Z', '+00:00')) for value in
                 (lock['tracking']['created_at'], lock['reference']['created_at'], state['created_at'])]
        if any(value.tzinfo is None for value in times) or times != sorted(times):
            return _gate('fail', 'Session was not created after both preregistration freezes')
        if state.get('started_at') and datetime.fromisoformat(state['started_at'].replace('Z', '+00:00')) < times[-1]:
            return _gate('fail', 'Experiment clock predates its session')
    except (KeyError, ValueError, TypeError):
        return _gate('fail', 'Preregistration chronology is missing or malformed')
    expected_locks = {key: lock[key] for key in ('tracking_lock_sha256', 'reference_lock_sha256')}
    if (state.get('schema_version') != 3 or state.get('experiment_id') != GROUND_ID
            or state.get('experiment_spec') != get_experiment(GROUND_ID).record()
            or state.get('mode') != 'tools' or state.get('experiment_locks') != expected_locks
            or state.get('parent') != lock['tracking']['parent']
            or state.get('parent_source_hashes') != lock['tracking']['parent']['source_hashes']
            or state.get('phase1_frozen_files') != lock['tracking']['phase1_frozen_files']
            or state.get('budget') != {'max_candidates': 3, 'max_seconds': 1800}
            or state.get('prepared_baseline_hashes') != state.get('baseline_hashes')):
        return _gate('fail', 'Session differs from its preregistered experiment and source locks')
    if not state.get('baseline_hashes') or project_hashes(_owned(session, state['baseline'])) != state['baseline_hashes']:
        return _gate('fail', 'Prepared baseline source bytes changed')
    if candidate:
        cid = identifier(candidate['candidate_id'])
        if (read_json(_owned(session, f'candidates/{cid}/candidate.json')) != candidate
                or project_hashes(_owned(session, f'candidates/{cid}/project')) != candidate.get('source_hashes')
                or (state.get('final') or {}).get('candidate_id') != cid
                or not state.get('sealed_at') or state.get('status') != 'finished'):
            return _gate('fail', 'Selected candidate, source snapshot or final seal differs')
    return _gate('pass')


def _groups(state, candidate):
    return {
        'ground': {'reference': state.get('reference', {}), 'baseline': state.get('baseline_runs', {}),
                   'candidate': {split: candidate.get(f'{split}_runs', []) for split in ('calibration', 'validation')}},
        'jump': {'reference': state.get('reference', {}).get('jump', {}),
                 'baseline': state.get('baseline_runs', {}).get('jump', {}),
                 'candidate': {split: candidate.get(f'jump_{split}_runs', []) for split in ('calibration', 'validation')}}}


def _capture_identity(session, state, candidate, lock):
    """Exact role/scenario/source identity; receipt verifier binds every raw PNG."""
    if not candidate or lock is None:
        return _gate(reason='No selected complete capture matrix')
    groups, seen, count = _groups(state, candidate), set(), 0
    for mechanic, roles in groups.items():
        for role, splits in roles.items():
            game, variant = ('tiny', 'reference') if role == 'reference' else ('slay', 'before' if role == 'baseline' else 'after')
            for split in ('calibration', 'validation'):
                scenarios = get_experiment(GROUND_ID).scenarios(split) if mechanic == 'ground' else [
                    {'hold_ticks': hold, 'start_tick': 30, 'split': split, 'repeat': repeat, 'spawn_offset': offset}
                    for hold in ((3, 24) if split == 'calibration' else (8, 14))
                    for offset in ((0,) if split == 'calibration' else (0, 24)) for repeat in (1, 2, 3)]
                required = {json.dumps(s, sort_keys=True) for s in scenarios}
                runs, observed = splits.get(split, []), set()
                if not isinstance(runs, list) or len(runs) != len(required):
                    return _gate(reason=f'Incomplete {mechanic}/{role}/{split} matrix')
                for raw in runs:
                    run = _owned(session, raw)
                    if run in seen:
                        return _gate('fail', 'Capture path reused across different experiment slots')
                    seen.add(run)
                    manifest = read_json(_owned(session, run / 'manifest.json'))
                    report = read_json(_owned(session, run / 'analysis.json'))
                    scenario = json.dumps(manifest.get('scenario'), sort_keys=True)
                    if (scenario in observed or scenario not in required or manifest.get('status') != 'ok'
                            or manifest.get('game') != game or manifest.get('variant') != variant
                            or manifest.get('fps') != 60 or manifest.get('frame_count') != 240
                            or any(report.get(key) != manifest.get(key) for key in ('game', 'variant', 'scenario', 'fps'))
                            or report.get('provenance', {}).get('manifest_sha256') != sha256(run / 'manifest.json')):
                        return _gate('fail', 'Capture/analysis identity, manifest hash or exact scenario differs')
                    observed.add(scenario)
                    expected = (lock['tracking']['reference_source_hashes'] if role == 'reference' else
                                state['baseline_hashes'] if role == 'baseline' else candidate['source_hashes'])
                    if mechanic == 'ground' or role == 'candidate':
                        if any(manifest.get('provenance', {}).get(key) != expected
                               for key in ('full_source_hashes_before', 'full_source_hashes_after')):
                            return _gate('fail', 'Capture belongs to a different source snapshot')
                    # All copied references retain exact canonical manifest bytes.
                    if role == 'reference' or mechanic == 'jump' and role == 'baseline':
                        original = (ROOT / 'artifacts/phase2b/reference' / run_name(manifest['scenario']) if mechanic == 'ground'
                                    else ROOT / 'artifacts/runs' / f"{game}-{variant}-h{manifest['scenario']['hold_ticks']:02d}-x{manifest['scenario']['spawn_offset']}-r{manifest['scenario']['repeat']}")
                        if sha256(_owned(ROOT, original / 'manifest.json')) != sha256(run / 'manifest.json'):
                            return _gate('fail', 'Copied reference differs from canonical experiment evidence')
                    count += 1
    return _gate('pass', checked_capture_manifests=count)


def _quality(session, state, candidate, saved):
    from kiroclops.ground.comparison import compare_runs
    from .evaluation import final_comparison
    from .ground_worker import combine_comparisons, _jump_drift
    if not candidate:
        return _gate(reason='No sealed final candidate'), None
    groups = _groups(state, candidate)
    def runs(mechanic, role):
        values = groups[mechanic][role]
        return [_owned(session, raw) / 'analysis.json' for split in ('calibration', 'validation') for raw in values.get(split, [])]
    ground = compare_runs(*(runs('ground', role) for role in ('reference', 'baseline', 'candidate')), split='validation')
    jump = final_comparison(*(runs('jump', role) for role in ('reference', 'baseline', 'candidate')))
    fresh = combine_comparisons(ground, jump, _jump_drift(candidate.get('jump_validation_runs', []), 'validation'))
    if fresh != saved:
        return _gate('fail', 'Saved final result differs from a fresh comparison of the selected analyses'), fresh
    return _gate('pass'), fresh


def _manual(session):
    try:
        from .ground_review import verify_review_receipt
    except ImportError:
        return _gate(reason='Independent ground/jump review verifier is unavailable')
    return verify_review_receipt(session)


def _source_scope(session):
    from .source_review import verify_source_review
    return verify_source_review(session)


def _smoke(session, candidate):
    if not candidate:
        return _gate(reason='No selected gameplay snapshot')
    try:
        from .ground_runner import verify_smoke
    except ImportError:
        return _gate(reason='Ground gameplay receipt verifier is unavailable')
    root = _owned(session, f"candidates/{identifier(candidate['candidate_id'])}")
    return verify_smoke(root / 'project', root / 'smoke', candidate['source_hashes'])


def _evaluation_job(session, candidate):
    """A cancelled/failed job cannot contribute an eligible selected snapshot."""
    if not candidate:
        return _gate(reason='No selected candidate evaluation job')
    matches = []
    for path in (session / 'jobs').glob('*.json'):
        job = read_json(_owned(session, path))
        if job.get('action') == 'evaluate' and job.get('candidate_id') == candidate['candidate_id']:
            matches.append((path, job))
    if len(matches) != 1:
        return _gate('fail' if matches else 'unverified', 'Exactly one evaluation job must own the selected candidate')
    path, job = matches[0]
    result = job.get('result') or {}
    if (job.get('job_id') != path.stem or job.get('session_id') != session.name or job.get('status') != 'succeeded'
            or job.get('error') or not isinstance(result, dict) or result.get('error')
            or result.get('status') != 'evaluated' or result.get('candidate_id') != candidate['candidate_id']
            or result.get('calibration') != candidate.get('feedback')
            or candidate.get('feedback', {}).get('eligible') is not True):
        return _gate('fail', 'Selected candidate feedback is not bound to a succeeded evaluation job')
    return _gate('pass', job_id=job['job_id'], job_sha256=sha256(path), candidate_id=candidate['candidate_id'])


def _configuration_binding(session, lock):
    from .kiro import _load_config
    if lock is None:
        return _gate(reason='No frozen ground workflow')
    config = _load_config(session, 'tools')
    name = 'power/kiroclops/ground-motion.md'
    source = _owned(ROOT, name)
    expected = hashlib.sha256(source.read_text(encoding='utf-8').encode('utf-8')).hexdigest()
    if (config.get('experiment_id') != GROUND_ID or sha256(source) != lock['tracking']['files'].get(name)
            or config.get('instruction_sha256') != expected):
        return _gate('fail', 'Agent/Power instructions differ from the frozen ground workflow')
    return _gate('pass', instruction_sha256=expected)


def _collect(session, expected_surface, lock):
    issues = []
    if not (session / 'session.json').is_file():
        return {'session_id': session.name, 'surface': expected_surface, 'status': 'not_run', 'success': False,
                'demo_status': 'not_run', 'strict_model_configuration': 'unverified', 'gates': {},
                'candidate_count': 0, 'final_error': {'status': 'unavailable', 'mean_rmse_body': None}, 'issues': []}
    state = read_json(_owned(session, 'session.json'))
    if state.get('experiment_id') != GROUND_ID or state.get('mode') != 'tools' or state.get('session_id') != session.name:
        raise ExperimentMismatch(f'{session.name}: not a registered ground tools experiment')
    launch = proof._read(session, 'kiro/launch.json', issues, optional=True) or {}
    candidates = state.get('candidates', [])
    selected = next((c for c in candidates if c.get('candidate_id') == state.get('selected_candidate_id')), None)
    final = state.get('final') or {}
    saved = proof._read(session, final.get('comparison_path', 'final/comparison.json'), issues, optional=True) if selected else None
    source = _state_integrity(session, state, selected, lock)
    workflow = _configuration_binding(session, lock)
    captures = _capture_identity(session, state, selected, lock)
    quality, fresh = _quality(session, state, selected, saved)
    manual, smoke = _manual(session), _smoke(session, selected)
    source_scope = _source_scope(session)
    evaluation_job = _evaluation_job(session, selected)
    fresh = fresh or {}
    gates = {'experiment_sources': source['status'], 'workflow_binding': workflow['status'], 'capture_identity': captures['status'],
             'comparison_integrity': quality['status'], 'numerical': proof._status(fresh.get('ground', {}).get('numerical_status')),
             'jump_regression': proof._status(fresh.get('jump_regression', {}).get('numerical_status')),
             'manual': proof._status(manual.get('status')), 'smoke': proof._status(smoke.get('status')),
             'evaluation_job': evaluation_job['status'], 'source_scope': source_scope['status']}
    if manual.get('status') in {'pass', 'passed'} and fresh.get('manual_validation_status') != 'pass':
        gates['manual'] = proof._status(fresh.get('manual_validation_status'))
    if gates['manual'] == 'pass' and manual.get('manual_validation_status') != 'pass':
        gates['manual'] = proof._status(manual.get('manual_validation_status'))
    stdout, stream_status = proof._jsonl(session, 'kiro/stdout.jsonl', issues)
    audit, audit_status = proof._jsonl(session, 'kiro/tool-audit.jsonl', issues)
    model = proof._model(launch, stdout, stream_status)
    config = proof._configuration(launch, stdout, stream_status)
    native = {'status': 'not_applicable'}
    if expected_surface == 'ide':
        native = proof._ide_evidence(session, proof._read(session, 'ide/evidence.json', issues, optional=True), selected, issues)
        author = native.get('authorship', _gate(reason='Native source replay unavailable'))
        completion = proof._ide_completion(session, state, launch, native, issues)
        model = native.get('model', _gate(reason='Native model evidence unavailable'))
        config = _gate(reason='Native IDE export cannot establish effort for every generation')
    else:
        from .authorship import audit_kiro_authorship
        author = audit_kiro_authorship(session, candidate_id=(selected or {}).get('candidate_id'))
        completion = proof._launch_completion(launch, stdout, config)
    gates['authorship'] = proof._status(author.get('status'))
    if gates['authorship'] == 'pass' and (not selected or author.get('candidate_id') != selected['candidate_id']
                                         or author.get('source_hashes') != selected['source_hashes']):
        gates['authorship'] = 'fail'
    elapsed = launch.get('elapsed_seconds')
    budget_ok = 1 <= len(candidates) <= 3 and proof._finite(elapsed) and elapsed <= 1800
    identity_ok = launch.get('surface') == expected_surface and launch.get('mode') == 'tools'
    strict = (expected_surface == 'cli' and model.get('status') == 'verified' and config.get('status') == 'pass'
              and launch.get('effort_requested') == proof.EXPECTED_EFFORT and identity_ok)
    demonstrated = proof._ide_demo(native, completion, gates, budget_ok and identity_ok,
                             proof._ide_job_bindings(session, state, native, issues)) if expected_surface == 'ide' else {'status': 'not_applicable'}
    success = all(v == 'pass' for v in gates.values()) and strict and completion.get('status') == 'pass' and budget_ok
    failed = (any(v == 'fail' for v in gates.values()) or completion.get('status') == 'fail'
              or config.get('status') == 'fail' or model.get('status') == 'mismatch'
              or launch and not identity_ok or len(candidates) > 3 or proof._finite(elapsed) and elapsed > 1800)
    error = fresh.get('mean_rmse_body')
    error_available = captures['status'] == quality['status'] == 'pass' and proof._finite(error)
    return {'session_id': session.name, 'experiment_id': GROUND_ID, 'surface': expected_surface, 'mode': 'tools',
            'status': 'pass' if success else 'fail' if failed else 'unverified' if launch else 'not_run',
            'success': success, 'session_status': state.get('status'), 'gates': gates,
            'demo_status': demonstrated['status'], 'ide_demonstration': demonstrated,
            'strict_model_configuration': 'pass' if strict else 'fail' if config.get('status') == 'fail' or model.get('status') == 'mismatch' else 'unverified',
            'source_integrity': source, 'workflow_binding': workflow, 'capture_integrity': captures, 'comparison_integrity': quality,
            'manual_evidence': manual, 'source_scope_review': source_scope, 'smoke_evidence': smoke,
            'evaluation_job': evaluation_job, 'authorship': author,
            'model': model, 'configuration': config, 'launch_completion': completion,
            'candidate_count': len(candidates), 'selected_candidate_id': state.get('selected_candidate_id'),
            'elapsed_seconds': elapsed, 'budget': {'within_planned_budget': budget_ok, 'max_candidates': 3, 'max_seconds': 1800},
            'final_error': {'status': 'available' if error_available else 'unavailable', 'mean_rmse_body': error if error_available else None},
            'cohort_identity': {'baseline_hashes': state.get('baseline_hashes'), 'experiment_locks': state.get('experiment_locks'),
                                'cli_version': launch.get('cli_version'), 'kas_version': launch.get('kas_version')},
            'tool_audit': proof._tool_audit(session, audit, audit_status), 'issues': issues}


def build_benchmark(session_ids=None, output=None):
    """Return JSON-friendly evidence; optionally write a separate local report."""
    cohort, cohort_sha = _cohort()
    registered = cohort['cli_session_ids'] + cohort['ide_session_ids']
    ids = registered if session_ids is None else list(session_ids)
    if not ids or len(set(ids)) != len(ids) or any(name not in registered for name in ids):
        raise ValueError('Only explicit preregistered ground cohort IDs are accepted; no replacement or baseline runs')
    lock, lock_status = None, _gate(reason='Ground freeze is not complete')
    if (ROOT / 'config/phase2b/frozen.json').is_file():
        try:
            lock = verify_ground_lock(root=ROOT)
            lock_status = _gate('pass')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            lock_status = _gate('fail', f'Ground freeze verification failed: {type(exc).__name__}')
    rows = []
    for name in ids:
        session = _owned(ROOT, Path('sessions') / identifier(name), exists=False)
        surface = 'cli' if name in cohort['cli_session_ids'] else 'ide'
        try:
            row = _collect(session, surface, lock)
        except ExperimentMismatch:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            failed = not isinstance(exc, (FileNotFoundError, KeyError))
            row = {'session_id': name, 'surface': surface, 'status': 'fail' if failed else 'unverified', 'success': False,
                   'demo_status': 'unverified', 'gates': {}, 'strict_model_configuration': 'unverified',
                   'final_error': {'status': 'unavailable', 'mean_rmse_body': None}, 'issues': [f'Incomplete evidence: {type(exc).__name__}']}
        rows.append(row)
    cli = [r for r in rows if r['surface'] == 'cli']
    ide = [r for r in rows if r['surface'] == 'ide']
    identities = [r.get('cohort_identity') for r in cli]
    same = (len(cli) == 3 and all(identities) and all(i == identities[0] for i in identities)
            and all(identities[0].get(key) for key in ('baseline_hashes', 'experiment_locks', 'cli_version', 'kas_version')))
    complete = set(ids) == set(registered)
    cli_pass = len(cli) == 3 and same and all(r['success'] for r in cli)
    ide_demo = len(ide) == 1 and ide[0]['demo_status'] == 'demonstrated'
    accepted = complete and lock_status['status'] == 'pass' and cli_pass and ide_demo
    result = {'schema_version': 1, 'experiment_id': GROUND_ID, 'cohort': cohort, 'cohort_sha256': cohort_sha,
              'selected_session_ids': ids, 'registration_complete': complete, 'frozen_experiment': lock_status,
              'sessions': rows, 'cli': {'planned_runs': 3, 'selected_runs': len(cli), 'same_cohort': same,
                  'successes': sum(r['success'] for r in cli), 'all_pass': cli_pass,
                  'mean_final_rmse_body': statistics.mean(r['final_error']['mean_rmse_body'] for r in cli)
                      if len(cli) == 3 and all(r['final_error']['status'] == 'available' for r in cli) else None},
              'ide': {'planned_runs': 1, 'demonstrated': ide_demo, 'strict_model_configuration': 'unverified',
                      'limitation': 'Native export and UI prove the observed demonstration; historical per-generation effort is not exported.'},
              'phase_acceptance': 'demonstrated_with_ide_configuration_limit' if accepted else 'pending_or_failed',
              'strict_all_surfaces_acceptance': False, 'tool_free_comparison': 'not_part_of_this_experiment'}
    if output is not None:
        destination = _owned(ROOT, output, exists=False)
        boundary = (ROOT / 'artifacts/phase2b-report').resolve()
        if destination != boundary and not destination.is_relative_to(boundary):
            raise ValueError('Ground report output must stay under artifacts/phase2b-report')
        write_json(destination / 'benchmark.json', result)
        content = '<!doctype html><meta charset="utf-8"><title>KiroClops ground benchmark</title><h1>Ground motion cohort</h1>'
        content += '<p>' + html.escape(result['phase_acceptance']) + '</p><pre>' + html.escape(json.dumps(result, indent=2, ensure_ascii=False)) + '</pre>'
        (destination / 'index.html').write_text(content, encoding='utf-8')
    return result
