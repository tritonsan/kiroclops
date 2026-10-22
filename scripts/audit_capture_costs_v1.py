"""Read-only timing diagnosis from an ended model run; never starts an engine."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kiroclops.integration.storage import project_hashes, sha256, utc_now


def _read(path, evidence):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('Evidence must remain in this workspace')
    evidence[path.relative_to(ROOT).as_posix()] = sha256(path)
    return json.loads(path.read_text(encoding='utf-8'))


def audit(session_directory, output, *, engine=None, source_timing_repeats=3):
    session = Path(session_directory).resolve()
    output = Path(output).resolve()
    if session.parent != ROOT / 'sessions':
        raise ValueError('Expected an immediate workspace session')
    if not output.is_relative_to(ROOT / 'artifacts') or output.exists():
        raise ValueError('Use a new JSON output under workspace artifacts')
    if source_timing_repeats not in (1, 2, 3):
        raise ValueError('Source timing repeats must be 1, 2 or 3')
    evidence = {}
    state = _read(session / 'session.json', evidence)
    launch = _read(session / 'kiro/launch.json', evidence)
    if state.get('active_job_id') or launch.get('status') not in {'completed', 'timeout', 'cancelled', 'error', 'cleanup_failed'}:
        raise ValueError('Only ended runs with no active job may be profiled')
    historical = ROOT / 'artifacts/model-trials/reviews' / session.name / 'timing-diagnostic.json'
    timing = _read(historical, evidence)
    candidates = []
    jobs = [_read(path, evidence) for path in sorted((session / 'jobs').glob('job-*.json'))]
    for row in state['candidates']:
        metadata = _read(session / 'candidates' / row['candidate_id'] / 'candidate.json', evidence)
        if metadata != row:
            raise ValueError('Candidate metadata differs from the historical session')
        job = [j for j in jobs if j.get('action') == 'evaluate' and j.get('candidate_id') == row['candidate_id']]
        completed = len(job) == 1 and job[0].get('status') == 'succeeded'
        duration = None
        if completed and job[0].get('started_at') and job[0].get('finished_at'):
            duration = (datetime.fromisoformat(job[0]['finished_at']) - datetime.fromisoformat(job[0]['started_at'])).total_seconds()
        candidates.append({'candidate_id': row['candidate_id'], 'status': row['status'],
                           'recorded_eligible': (row.get('feedback') or {}).get('eligible'),
                           'recorded_calibration_rmse_body': (row.get('feedback') or {}).get('mean_rmse_body'),
                           'evaluation_succeeded': completed, 'evaluation_seconds': duration})
    eligible_index = next((n for n,c in enumerate(candidates) if c['recorded_eligible'] is True and c['evaluation_succeeded']), None)
    later = candidates[eligible_index+1:] if eligible_index is not None else []
    baseline = Path(state['baseline']).resolve()
    if not baseline.is_relative_to(session):
        raise ValueError('Baseline must be session owned')
    expected = state['baseline_hashes']
    source_spans=[]
    for _ in range(source_timing_repeats):
        started=time.perf_counter()
        actual=project_hashes(baseline)
        source_spans.append(time.perf_counter()-started)
        if actual != expected:
            raise ValueError('Baseline source bytes no longer match their snapshot')
    imports = timing.get('candidate_startup_observations', [])
    import_intervals=[r['import_log_activity_seconds'] for r in imports]
    summary={
        'schema_version':1, 'kind':'ended_run_performance_diagnosis_v1', 'created_at':utc_now(),
        'session_id':session.name, 'canonical_outcome_unchanged':True, 'acceptance_credit':False,
        'historical_timings':{'totals':timing['totals'], 'filesystem_interval_summaries':timing['filesystem_interval_summaries'],
                              'candidate_startup_observations':imports},
        'calibration_decisions':{'candidates':candidates,
            'first_eligible_candidate':candidates[eligible_index]['candidate_id'] if eligible_index is not None else None,
            'later_evaluations':later,
            'later_completed_evaluation_seconds':sum(c['evaluation_seconds'] or 0 for c in later),
            'interpretation':'Observed extra evaluation cost after an eligible candidate, not a counterfactual final-validation pass.'},
        'source_read_microtiming':{'project':str(baseline),'file_count':len(expected),'seconds':source_spans,
            'minimum_seconds':min(source_spans),'maximum_seconds':max(source_spans),
            'scope':'Current read-only project_hashes calls include directory traversal, path/link checks and file hashing; filesystem caches may be warm. Not historical CPU attribution.'},
        'opportunities':[
            {'change':'Finalize once a candidate is eligible in the new protocol instructions.',
             'observed_avoidable_evaluation_seconds':sum(c['evaluation_seconds'] or 0 for c in later),
             'qualification':'Keep maximum three attempts, original deadline, all final repeats and thresholds. No retroactive acceptance.'},
            {'change':'Seed only content-bound unchanged texture products, then still run the editor import.',
             'observed_import_log_intervals_seconds':import_intervals,
             'qualification':'Measure total seed+editor cost and require unchanged native pixels in a separately coordinated qualification. Source, engine and .import metadata must match; script/editor/UID caches are excluded.'},
            {'change':'Read engine version once per engine binary fingerprint and copy its bound receipt into each manifest.',
             'observed_call_count':timing['totals']['completed_capture_count'],
             'measured_isolated_seconds':None,
             'qualification':'Existing runners issue --version for every capture. Logs do not isolate this subprocess from the post-recording interval; fresh binary identity must still be checked.'},
            {'change':'Consolidate redundant source scans at the same execution boundary in a new runner.',
             'qualification':'Preserve fresh before-process and after-process/encoding source hashes and link rejection. Do not substitute stale global fingerprints.'},
            {'change':'Reuse a newly qualified fixed-source baseline recording pack across a preregistered group.',
             'qualification':'Only unchanged baseline, exact engine/adapter/render/input/source identity and every repeat retained; never substitute candidate captures or expose held-out movement before the seal. No such reuse is performed by this audit.'}],
        'limitations':[
            'Historical file times are endpoint observations, not independently instrumented process spans.',
            'The PNG write span does not isolate rendering, inactive-window pacing, compression or disk cost.',
            'An import cache can cost time itself; no speedup is established until real cold/warm capture qualification.',
            'No held-out positions, metrics or source-game physics are read by this script.',
            'No engine/model process, source/threshold mutation, repeat removal or outcome repair occurs.'],
        'official_sources':['https://docs.godotengine.org/en/4.5/tutorials/assets_pipeline/import_process.html',
            'https://raw.githubusercontent.com/godotengine/godot/4.5.1-stable/editor/file_system/editor_file_system.cpp'],
    }
    if engine is not None:
        from kiroclops.integration.import_cache_v1 import inspect_import_inputs
        summary['import_cache_readiness']=inspect_import_inputs(baseline,engine=engine,expected_source_hashes=expected)
    for path in ['kiroclops/integration/runner.py','kiroclops/integration/ground_runner.py',
                 'kiroclops/integration/ground_worker.py','kiroclops/integration/storage.py',
                 'kiroclops/integration/import_cache_v1.py','scripts/audit_capture_costs_v1.py']:
        evidence[path]=sha256(ROOT/path)
    # In particular, do not rewrite historical session/candidate/job records.
    for name,digest in evidence.items():
        if sha256(ROOT/name)!=digest:
            raise ValueError('An inspected input changed during this diagnosis')
    summary['inspected_input_hashes']=evidence
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:
        json.dump(summary,stream,ensure_ascii=False,indent=2);stream.write('\n')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--engine',type=Path,help='Optional existing engine binary; only its bytes are read')
    parser.add_argument('--source-timing-repeats',type=int,default=3)
    args=parser.parse_args()
    result=audit(args.session,args.output,engine=args.engine,source_timing_repeats=args.source_timing_repeats)
    print(json.dumps({'output':str(args.output.resolve()),'status':'diagnostic_only',
                      'first_eligible_candidate':result['calibration_decisions']['first_eligible_candidate'],
                      'later_evaluation_seconds':result['calibration_decisions']['later_completed_evaluation_seconds']}))


if __name__=='__main__':
    main()
