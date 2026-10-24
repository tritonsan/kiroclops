"""Post-hoc: a read-only, multi-session evidence table for the 13 logged Kiro CLI sessions.

The entry documents one logged agent session (the V6 panel run v6p01) in detail. This script
extracts the same facts for every logged Kiro CLI session on disk: what the coding agent did
(audited tool calls, MCP calls by name, guard refusals, full evaluations and partial probes,
scripts and file writes between the first candidate result and the next evaluation) and how
the recorded OpenCV measurements evolved (calibration score and eligibility of every candidate
in order, the unchanged app on the same score formula, the sealed held-out outcome) together
with the recorded run verdict.

Scores are recomputed from the recorded per-window rows exactly like
``scripts/v6_agent_baseline_posthoc.py``: a case value is the mean over its windows and a job
score is the mean of the case values (``kiroclops/local_engine/evaluation.py``). Game sessions
record one error per case instead of window rows; the same formula then reduces to the mean over
cases. Every recomputed score must reproduce the recorded score; the unchanged app is the
``before`` side of the same rows. The sessions, results logs and closure files are only read.
Nothing is estimated: a value that is not recorded is null and ``null_reasons`` says why.

    python scripts/agent_sessions_posthoc.py --output artifacts/phase4-v6/agent-sessions-posthoc-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_ROOT = Path('C:/kc3d/s')
CLOSURE_ROOT = Path('C:/kc3d/r')
V6_BASELINE = ROOT/'artifacts/phase4-v6/agent-baseline-posthoc-01/result.json'

SESSIONS = ('gj04', 'gj05', 'p04', 'v2g01', 'v2j03', 'v2p08', 'v3g02', 'v3p01', 'v4g01', 'v4p01', 'v5p01', 'v5p02c', 'v6p01')
VERSION = {'gj04': 'V1', 'gj05': 'V1', 'p04': 'V1', 'v2g01': 'V2', 'v2j03': 'V2', 'v2p08': 'V2', 'v3g02': 'V3', 'v3p01': 'V3',
           'v4g01': 'V4', 'v4p01': 'V4', 'v5p01': 'V5', 'v5p02c': 'V5b', 'v6p01': 'V6'}
MCP_PREFIX = 'mcp_kiroclops_'
MCP_TOOLS = ('build_report', 'cancel_job', 'evaluate_candidate', 'finalize_candidate', 'get_job', 'inspect_session',
             'inspect_window', 'prepare_experiment')
# The agent's own scripts and file writes. Refused calls are counted separately (``allow`` false).
WORK_TOOLS = ('execute_bash', 'execute_pwsh', 'fs_write', 'str_replace')
SHELL_TOOLS = ('execute_bash', 'execute_pwsh')
EPS = 1e-12
# Protocol set-up messages the client sends before the single task prompt (any other outbound message would be a follow-up).
SETUP_METHODS = ('initialize', 'session/new', 'session/set_mode', 'session/set_config_option', '_kiro/hooks/list')

# Recorded run verdicts, transcribed from the results logs (English paraphrase, path and line range).
# category: accepted / failed / infrastructure_failure / not_finalized.
VERDICTS = {
    'gj04': ('not_finalized',
             'Operator interruption: a helper process stopped the live launcher during the first preparation job; no candidate '
             'was produced. The log records the behaviour as unevaluated and does not count it as an agent failure.',
             (('PHASE3D_STATUS.md', 'L66-L68'),)),
    'gj05': ('not_finalized',
             'The 1800 s request deadline was reached after 31 audited calls with zero candidates, so behaviour was not '
             'evaluated; the log leaves the cause of the stalled generation undetermined (the CLI reported two upstream pauses).',
             (('PHASE3D_STATUS.md', 'L11'), ('PHASE3D_STATUS.md', 'L76-L78'))),
    'p04': ('failed',
            'candidate-01 passed 8/8 calibration cases and was sealed; held-out numerical acceptance is false '
            '(regression 10/12, validation 9/12).',
            (('PHASE3D_STATUS.md', 'L12'), ('PHASE3D_STATUS.md', 'L80'))),
    'v2g01': ('failed',
              'Two candidates were evaluated and neither was eligible, so finalize was refused (no eligible candidate) and no '
              'held-out case was evaluated. A later log analysis puts the 50% improvement gate of the shortest calibration '
              'recipe close to the measurement floor.',
              (('PHASE3D_V2_PENDING_RESULTS.md', 'L36-L53'), ('PHASE3D_V2_PENDING_RESULTS.md', 'L140'),
               ('PHASE3D_V2_PENDING_RESULTS.md', 'L172-L176'))),
    'v2j03': ('infrastructure_failure',
              'Two eligible candidates, candidate-02 sealed. The canonical finalize job failed with a harness execution error '
              '(a cancellation callback of the wrong type) before any held-out comparison was recorded; the log classes it as '
              'neither a behaviour nor a model failure. A later separate re-verification of the same code passed; it is outside '
              'the session and does not change the recorded verdict.',
              (('PHASE3D_V2_STATUS.md', 'L194-L200'), ('PHASE3D_V2_RESULTS.md', 'L30-L31'), ('PHASE3D_V2_REPAIR.md', 'L107-L132'))),
    'v2p08': ('failed',
              'candidate-02 was sealed after 8/8 calibration cases; held-out regression 11/12 and validation 12/12. The single '
              'failing known-regression window is above the 2 px absolute limit, so numerical acceptance is false.',
              (('PHASE3D_V2_PENDING_RESULTS.md', 'L138'), ('PHASE3D_V2_PENDING_RESULTS.md', 'L145-L152'))),
    'v3g02': ('failed',
              'candidate-01 was sealed; held-out regression 30/30 and validation 24/30. All six failures are repeats of one '
              'validation recipe at the speed gate; a labelled post-hoc diagnostic attributes this to the gate looking at a short '
              'frame window.',
              (('reviews/phase3d-v3/v3g02-game-result.md', 'L3-L23'),)),
    'v3p01': ('failed',
              'candidate-02 was sealed; held-out regression 21/24 and validation 8/12. All four validation failures use a recipe '
              'whose required event order is unobservable (a recipe design flaw according to the log). The native origin check '
              'failed as well.',
              (('reviews/phase3d-v3/v3p01-panel-result.md', 'L3-L24'),)),
    'v4g01': ('accepted',
              'candidate-01 was sealed; held-out regression 48/48 and validation 30/30. Independent raw review, source review and '
              'native origin passed, and a separate domain closure artifact records behaviour acceptance for the game domain '
              '(scope: this domain in its V3a source epoch; the closure states that the canonical session acceptance is unchanged '
              'and that no overall phase acceptance is given).',
              (('reviews/phase3d-v4/v4-results.md', 'L3-L25'),)),
    'v4p01': ('failed',
              'candidate-01 was sealed; held-out validation 12/12 but regression 27/36: four cases use an event order that is '
              'unobservable even in the reference (a recipe design flaw according to the log) and five fail on pixel windows. '
              'The native tool-result audit failed as well.',
              (('reviews/phase3d-v4/v4-results.md', 'L44-L51'),)),
    'v5p01': ('failed',
              'Two candidates were evaluated inside the 60 minute budget (about 15 minutes each) and both were ineligible at 21/22 '
              'calibration cases (one wide-viewport star window above the absolute limit); finalize was refused (no eligible '
              'candidate). The native audit failed on one offloaded shell output.',
              (('reviews/phase3d-v5/v5-results.md', 'L56-L104'),)),
    'v5p02c': ('infrastructure_failure',
               'candidate-01 was found with a single evaluation (16/16 calibration cases, eligible). The finalize job failed with a '
               'capture timing error (an input deadline missed by 0.239 s) while an operating-system update was installing, so no '
               'held-out case could be measured and the session cannot be re-finalized by design.',
               (('reviews/phase3d-v5/v5-results.md', 'L164-L183'),)),
    'v6p01': ('failed',
              'Two eligible candidates; between them the agent started a partial probe on three cases and cancelled it after 151 s. candidate-02 was sealed. '
              'Held-out 55/56 (regression 43/44, validation 12/12); overall acceptance is fail: the single failed case is flagged '
              'required_order_unobservable and the native evidence axis is invalid because the pinned audit reader does not cover '
              'kiro-cli 2.31.0. Presented in the log as an additional demonstration, not as an acceptance claim.',
              (('reviews/phase4-v6/v6-results.md', 'L246-L285'),)),
}

# Native tool-audit certification as recorded in the logs (the certified reader is pinned to kiro-cli 2.27.x).
NATIVE_AUDIT = {
    'gj04': ('not_certified', 'Interrupted run; an 8-call prefix matched after a narrow compatibility fix, which is not native acceptance.',
             'PHASE3D_STATUS.md', 'L68'),
    'gj05': ('not_certified', 'Run ended at the deadline; a 31-call prefix matched, which is not native acceptance of a completed generation.',
             'PHASE3D_STATUS.md', 'L78'),
    'p04': ('reaudit_pass', 'The first v2 native audit failed (retained); a later v3 re-audit of the same record passed with 60 tool calls.',
            'PHASE3D_STATUS.md', 'L82-L84'),
    'v2g01': ('reaudit_pass', 'A re-audit with the 2.27.1 compatibility reader passed with 113 tool calls; the earlier failure is retained.',
              'PHASE3D_V2_PENDING_RESULTS.md', 'L15-L30'),
    'v2j03': ('reaudit_pass', 'The canonical native axis failed (the frozen reader rejected one post-response update); a separate '
              'read-only audit with the repaired reader passed.', 'PHASE3D_V2_STATUS.md; PHASE3D_V2_REPAIR.md', 'L225-L229; L41-L42'),
    'v2p08': ('reaudit_pass', 'A re-audit with the 2.27.1 compatibility reader passed with 92 tool calls; the earlier failure is retained.',
              'PHASE3D_V2_PENDING_RESULTS.md', 'L15-L30'),
    'v3g02': ('pass', 'Native origin passed with the 2.27.1 identity mapping.', 'reviews/phase3d-v3/v3g02-game-result.md', 'L11'),
    'v3p01': ('fail', 'Native origin failed: a second MCP server connected after generation had started.',
              'reviews/phase3d-v3/v3p01-panel-result.md', 'L9; L32'),
    'v4g01': ('pass', 'Native origin passed in the single-server environment extension (74 calls, 74 hooks).',
              'reviews/phase3d-v4/v4-results.md', 'L11; L61'),
    'v4p01': ('fail', 'The native tool-result audit failed.', 'reviews/phase3d-v4/v4-results.md', 'L51'),
    'v5p01': ('fail', 'The native audit failed because one shell output was offloaded to a file.', 'reviews/phase3d-v5/v5-results.md', 'L71; L92-L102'),
    'v5p02c': ('pass', 'The native audit passed.', 'reviews/phase3d-v5/v5-results.md', 'L172'),
    'v6p01': ('not_certified', 'kiro-cli 2.31.0 is outside the pinned 2.27.x reader; the native evidence axis is invalid '
              '(unexpected post-response event).', 'reviews/phase4-v6/v6-results.md', 'L8; L278'),
}

REFUSAL_TEXT = {'cli_tool_output_file_outside_boundary': "access to the CLI's own tool-output file outside the experiment folder",
                'other_path_outside_boundary': 'a path outside the experiment boundary', 'file_deletion': 'file deletion'}

# Domain closure artifacts (only the accepted verdict needs one).
CLOSURES = {'v4g01': 'v4g01-closure-02'}

LOG_FILES = ('PHASE3A_STATUS.md', 'PHASE3D_STATUS.md', 'PHASE3D_V2_STATUS.md', 'PHASE3D_V2_RESULTS.md', 'PHASE3D_V2_PENDING_RESULTS.md', 'PHASE3D_V2_REPAIR.md',
             'reviews/phase3d-v3/v3g02-game-result.md', 'reviews/phase3d-v3/v3p01-panel-result.md', 'reviews/phase3d-v4/v4-results.md',
             'reviews/phase3d-v5/v5-results.md', 'reviews/phase4-v6/v6-results.md')


# --------------------------------------------------------------------------- reading

def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


class Sources:
    """Every file that is read, with size and SHA-256 (nothing is ever written here)."""

    def __init__(self):
        self.files = {}

    def note(self, path, role, data=None):
        key = Path(path).as_posix()
        if key not in self.files:
            self.files[key] = {'path': key, 'role': role, 'bytes': len(data) if data is not None else Path(path).stat().st_size,
                               'sha256': hashlib.sha256(data).hexdigest() if data is not None else sha256_path(path)}
        return self.files[key]['sha256']

    def read_bytes(self, path, role):
        data = Path(path).read_bytes()
        self.note(path, role, data)
        return data

    def read_json(self, path, role):
        return json.loads(self.read_bytes(path, role).decode('utf-8'))

    def read_jsonl(self, path, role):
        text = self.read_bytes(path, role).decode('utf-8')
        return [json.loads(line) for line in text.split('\n') if line.strip()]

    def listing(self):
        return [self.files[key] for key in sorted(self.files)]


def utc(seconds):
    return None if seconds is None else datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def clock(stamp, hours=0):
    """Time of day of a UTC stamp, optionally shifted (the logs use local time, UTC+3)."""
    return None if stamp is None else (datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')+timedelta(hours=hours)).strftime('%H:%M:%S')


def span(start, end, digits=3):
    return None if start is None or end is None else round(end-start, digits)


def dig(obj, path):
    for part in path.split('.'):
        if obj is None:
            return None
        obj = obj[int(part)] if isinstance(obj, list) else obj.get(part)
    return obj


# --------------------------------------------------------------------------- scores (same formula as the V6 script)

def case_values(comparison):
    """Candidate error, unchanged-app error, row format and row count of one comparison.

    Panel rows have per-window ``improvement`` rows (``before_rmse`` = unchanged app, ``after_rmse`` = candidate) and the
    case value is the mean over windows. Game rows record one ``before_rmse_body`` / ``after_rmse_body`` pair per case.
    """
    windows = [g for g in (comparison.get('improvement') or []) if isinstance(g, dict)]
    if windows:
        after = [g['after_rmse'] for g in windows if g.get('after_rmse') is not None]
        before = [g['before_rmse'] for g in windows if g.get('before_rmse') is not None]
        form, count = 'per_window_rows', len(windows)
    else:
        after = [] if comparison.get('after_rmse_body') is None else [comparison['after_rmse_body']]
        before = [] if comparison.get('before_rmse_body') is None else [comparison['before_rmse_body']]
        form, count = ('per_case_body_rmse', 1) if after or before else (None, 0)
    return (statistics.mean(after) if after else None, statistics.mean(before) if before else None, form, count)


def failure_signature(comparison):
    if comparison.get('pass') is not False:
        return None
    if comparison.get('failure_class'):
        return comparison['failure_class']
    failed = sorted(name for name, ok in (comparison.get('gates') or {}).items() if ok is False)
    return 'failed_gates:' + '+'.join(failed) if failed else 'unspecified'


def cases(comparisons):
    rows = []
    for c in comparisons:
        candidate, unchanged, form, count = case_values(c)
        rows.append({'case_id': c.get('case_id'), 'role': c.get('role'), 'pass': c.get('pass'), 'recorded_score': c.get('score'),
                     'candidate': candidate, 'unchanged_app': unchanged, 'rows_format': form, 'rows': count,
                     'failure': failure_signature(c)})
    return rows


def summary(rows, recorded_job_score=None):
    usable = [r for r in rows if r['candidate'] is not None and r['unchanged_app'] is not None]
    out = {'cases': len(rows), 'cases_with_both': len(usable), 'cases_passed': sum(r['pass'] is True for r in rows),
           'candidate_score': None, 'unchanged_app_score': None, 'ratio_unchanged_over_candidate': None,
           'cases_candidate_better': None, 'recomputed_case_scores_match': None}
    if usable:
        candidate = statistics.mean(r['candidate'] for r in usable)
        unchanged = statistics.mean(r['unchanged_app'] for r in usable)
        out.update(candidate_score=candidate, unchanged_app_score=unchanged,
                   ratio_unchanged_over_candidate=unchanged/candidate if candidate else None,
                   cases_candidate_better=sum(r['candidate'] < r['unchanged_app'] for r in usable),
                   recomputed_case_scores_match=all(r['recorded_score'] is None or abs(r['recorded_score']-r['candidate']) < EPS
                                                    for r in rows if r['candidate'] is not None))
        if recorded_job_score is not None:
            out['recorded_job_score'] = recorded_job_score
            out['recomputed_job_score_matches'] = len(usable) == len(rows) and abs(recorded_job_score-candidate) < EPS
    return out


def changes(first, last, form):
    """Per-window (panel) or per-case (game) change of the candidate error from the first to the last candidate."""
    def table(result):
        rows = {}
        for c in result['comparisons']:
            if form == 'per_window_rows':
                for g in c.get('improvement') or []:
                    rows[(c['case_id'], g['object_id'], g['window_id'], g.get('segment_index', 0))] = (g['after_rmse'], g.get('pass'))
            else:
                rows[(c['case_id'],)] = (c.get('after_rmse_body'), c.get('pass'))
        return rows
    a, b = table(first), table(last)
    keys = sorted(set(a) & set(b))
    pairs = [(a[k], b[k]) for k in keys if a[k][0] is not None and b[k][0] is not None]
    return {'unit': 'windows' if form == 'per_window_rows' else 'cases', 'compared': len(pairs),
            'only_in_one_candidate': len(set(a) ^ set(b)),
            'improved': sum(y[0] < x[0] for x, y in pairs), 'worsened': sum(y[0] > x[0] for x, y in pairs),
            'unchanged': sum(y[0] == x[0] for x, y in pairs),
            'failing_in_first': sum(x[1] is False for x, y in pairs),
            'failing_in_first_and_passing_in_last': sum(x[1] is False and y[1] is True for x, y in pairs),
            'failing_in_last': sum(y[1] is False for x, y in pairs)}


# --------------------------------------------------------------------------- audit

def refusal_kind(row):
    text = json.dumps(row.get('arguments'), sort_keys=True).replace('\\\\', '/').lower()
    if row['tool'] == 'delete_file':
        return 'file_deletion'
    if 'tool-outputs' in text and '/.kiro/sessions/' in text:
        return 'cli_tool_output_file_outside_boundary'
    return 'other_path_outside_boundary'


def audit_block(rows):
    tools = Counter(r['tool'] for r in rows)
    mcp = {name: 0 for name in MCP_TOOLS}
    for tool, count in tools.items():
        if tool.startswith(MCP_PREFIX):
            mcp[tool[len(MCP_PREFIX):]] = count
    t0 = min(r['at'] for r in rows)
    refusals = [{'row': i, 'offset_seconds': round(r['at']-t0, 1), 'tool': r['tool'], 'reason': r['reason'], 'kind': refusal_kind(r)}
                for i, r in enumerate(rows) if r['allow'] is False]
    return {'rows': len(rows), 'rows_without_tool_load': len(rows)-tools.get('tool_load', 0), 'mcp_calls': sum(mcp.values()),
            'mcp_calls_by_name': dict(sorted(mcp.items())), 'other_calls_by_tool': dict(sorted((t, n) for t, n in tools.items() if not t.startswith(MCP_PREFIX))),
            'scripts_and_file_writes': sum(tools.get(t, 0) for t in WORK_TOOLS),
            'scripts_and_file_writes_refused': sum(r['tool'] in WORK_TOOLS and r['allow'] is False for r in rows), 'guard_refusals': len(refusals),
            'refusal_details': refusals, 'rows_in_time_order': all(a['at'] <= b['at'] for a, b in zip(rows, rows[1:])),
            'largest_backward_step_seconds': round(max([a['at']-b['at'] for a, b in zip(rows, rows[1:]) if b['at'] < a['at']], default=0.0), 4)}


def activity(rows, start, end):
    """Audited calls strictly between two instants; a descriptive count, no causal claim."""
    inside = [r for r in rows if start < r['at'] < end]
    tools = Counter(r['tool'] for r in inside)
    work = {t: tools[t] for t in WORK_TOOLS if tools.get(t)}
    return {'window_start_utc': utc(start), 'window_end_utc': utc(end), 'window_seconds': round(end-start, 1),
            'all_audited_calls': len(inside), 'scripts_and_file_writes': sum(work.values()),
            'shell_script_calls': sum(tools.get(t, 0) for t in SHELL_TOOLS), 'file_write_calls': tools.get('fs_write', 0)+tools.get('str_replace', 0),
            'scripts_and_file_writes_by_tool': work, 'refused_calls': sum(r['allow'] is False for r in inside),
            'other_calls_by_tool': dict(sorted((t, n) for t, n in tools.items() if t not in WORK_TOOLS))}


# --------------------------------------------------------------------------- one session

def load_jobs(sdir, src):
    jobs = []
    for folder in sorted(p for p in (sdir/'jobs').iterdir() if p.is_dir()):
        meta_path = folder/'job.json'
        if meta_path.exists():
            jobs.append({'dir': folder, 'meta': src.read_json(meta_path, 'job record')})
    jobs.sort(key=lambda j: (j['meta'].get('created_at') or 0, j['meta']['job_id']))
    return jobs


def scan_opencv(sdir, jobs, src):
    pattern = re.compile(rb'"opencv_version"\s*:\s*"([^"]*)"')
    found, files = Counter(), []
    for job in jobs:
        for name in ('reference.json', 'candidate.json'):
            path = sdir/'measurements'/job['meta']['job_id']/name
            if path.exists():
                data = src.read_bytes(path, 'measurement records (OpenCV version scan)')
                counts = Counter(m.decode('ascii', 'replace') for m in pattern.findall(data))
                found.update(counts)
                files.append({'path': path.as_posix(), 'records_with_opencv_version': sum(counts.values())})
    return dict(sorted(found.items())), files


def head_value(path, key, size=262144):
    with open(path, 'rb') as handle:
        head = handle.read(size).decode('utf-8', 'replace')
    match = re.search('"' + key + '"' + r'\s*:\s*"([^"]*)"', head)
    return match.group(1) if match else None


def derive_outcome(final_attempts, result):
    if not final_attempts:
        return 'not_finalized'
    last = final_attempts[-1]
    if last['status'] == 'succeeded':
        return 'numerical_pass' if result and result.get('pass') is True else 'numerical_fail'
    return 'no_eligible_candidate' if last['error_code'] == 'no_eligible_candidate' else 'finalize_infrastructure_error'


DERIVED_TO_CATEGORY = {'numerical_pass': 'accepted', 'numerical_fail': 'failed', 'no_eligible_candidate': 'failed',
                       'finalize_infrastructure_error': 'infrastructure_failure', 'not_finalized': 'not_finalized'}


def analyse(sid, src, logs):
    sdir = SESSIONS_ROOT/sid
    missing = {}
    session = src.read_json(sdir/'session.json', 'session record')
    launch = src.read_json(sdir/'kiro/launch.json', 'launcher record')
    rows = src.read_jsonl(sdir/'kiro/tool-audit.jsonl', 'tool audit')
    jobs = load_jobs(sdir, src)
    exp = session.get('experiment') or {}
    budget = session.get('budget') or {}
    t0, t1 = min(r['at'] for r in rows), max(r['at'] for r in rows)
    rec = {'session': sid, 'session_dir': sdir.as_posix(), 'version': VERSION[sid]}

    # --- identification
    rec['domain'] = session.get('domain')
    rec['mechanic'] = exp.get('mechanic')
    rec['experiment'] = {'experiment_id': exp.get('experiment_id'), 'stage': exp.get('stage'), 'acceptance_profile': exp.get('acceptance_profile'),
                         'suite_sha256': exp.get('suite_sha256'), 'source_epoch': exp.get('source_epoch'),
                         'calibration_cases_in_suite': len(exp.get('calibration') or []), 'heldout_status': exp.get('heldout_status'),
                         'session_schema_version': session.get('schema_version'), 'recorded_phase': launch.get('phase'),
                         'run_kind': launch.get('run_kind')}
    if exp.get('acceptance_profile') is None:
        missing['experiment.acceptance_profile'] = 'the V1 session format does not record an acceptance profile'
    if exp.get('source_epoch') is None:
        missing['experiment.source_epoch'] = 'the V1 session format does not record a source epoch'
    rec['budget'] = {'max_candidates': budget.get('max_candidates'), 'max_seconds': budget.get('max_seconds'),
                     'max_validation_seconds': budget.get('max_validation_seconds')}

    # --- agent / CLI
    cli = launch.get('cli_version')
    stream = launch.get('stream_configuration') or {}
    stdout_path = sdir/'kiro/run/stdout.jsonl'
    lines = src.read_bytes(stdout_path, 'native event log (line count only)').count(b'\n') if stdout_path.exists() else None
    rec['agent'] = {'kiro_cli_version': cli.split()[-1] if cli else None, 'kas_version': launch.get('kas_version'), 'model': launch.get('model'),
                    'effort': launch.get('effort'), 'approval_mode': launch.get('approval_mode'), 'observed_models': stream.get('observed_models'),
                    'launch_status': launch.get('status'),
                    'native_turn_status': launch.get('native_turn_status'), 'stop_reason': (launch.get('response') or {}).get('stopReason'),
                    'in_pinned_native_audit_range': bool(cli and ' 2.27.' in cli), 'events_seen_by_launcher': stream.get('events_seen'),
                    'native_event_log_lines': lines}
    outbound_path = sdir/'kiro/run/outbound.jsonl'
    methods = Counter(r['message'].get('method') for r in src.read_jsonl(outbound_path, 'client messages sent to the CLI') if isinstance(r.get('message'), dict)) if outbound_path.exists() else None
    passive = launch.get('passive_activity')
    preflight = launch.get('stdio_preflight') or {}
    attempts_ = launch.get('startup_attempts') or []
    rec['agent'].update({
        'task_prompts_sent': None if methods is None else methods.get('session/prompt', 0),
        'other_client_messages': None if methods is None else dict(sorted((k, n) for k, n in methods.items() if k != 'session/prompt')),
        'autopilot': (launch.get('preflight') or {}).get('autopilot'),
        'startup_check_seconds': attempts_[0].get('seconds') if attempts_ else None, 'mcp_tools_at_preflight': len(preflight.get('tool_names') or []) or None,
        'upstream_pause_notifications': None if passive is None else passive.get('pause_notifications'),
        'automatic_resume_sent': None if passive is None else passive.get('automatic_resume_sent'),
        'prompt_retried': None if passive is None else passive.get('prompt_retried'),
        'deadline_extended': None if passive is None else passive.get('deadline_extended')})
    if passive is None:
        for key in ('upstream_pause_notifications', 'automatic_resume_sent', 'prompt_retried', 'deadline_extended'):
            missing['agent.'+key] = 'launch.json has no passive_activity record (not captured by the launcher of this session)'
    unfinished = 'launch.json was never completed (the launcher was interrupted before it recorded this)'
    for key, value in (('kas_version', launch.get('kas_version')), ('observed_models', stream.get('observed_models')),
                       ('native_turn_status', launch.get('native_turn_status')), ('events_seen_by_launcher', stream.get('events_seen'))):
        if value is None:
            missing['agent.'+key] = unfinished
    if rec['agent']['stop_reason'] is None:
        missing['agent.stop_reason'] = (unfinished if launch.get('finished_at') is None and launch.get('status') == 'running'
                                        else 'the run ended at the request deadline; no stop reason was received')
    rec['native_audit_per_logs'] = {'status': NATIVE_AUDIT[sid][0], 'note': NATIVE_AUDIT[sid][1], 'log': NATIVE_AUDIT[sid][2], 'lines': NATIVE_AUDIT[sid][3]}

    # --- tool audit
    rec['audit'] = audit_block(rows)

    # --- jobs and evaluations
    full = [j for j in jobs if j['meta']['operation'] == 'evaluate' and not j['meta'].get('partial')]
    probes = [j for j in jobs if j['meta']['operation'] == 'evaluate' and j['meta'].get('partial')]
    calls = [(i, r) for i, r in enumerate(rows) if r['tool'] == MCP_PREFIX+'evaluate_candidate']
    partial_calls = [c for c in calls if (c[1]['arguments'] or {}).get('case_ids')]
    prepare = [j for j in jobs if j['meta']['operation'] == 'prepare']
    rec['evaluations'] = {
        'full_candidate_evaluations': len(full), 'partial_probes': len(probes), 'evaluate_candidate_calls_in_audit': len(calls),
        'evaluate_candidate_calls_with_case_ids': len(partial_calls),
        'audit_matches_jobs': len(calls) == len(full)+len(probes) and len(partial_calls) == len(probes),
        'candidates_used_of_budget': [len(session.get('candidates') or []), budget.get('max_candidates')],
        'probes': [{'job_id': j['meta']['job_id'], 'status': j['meta']['status'], 'cases_requested': len(j['meta'].get('case_ids') or []),
                    'seconds': span(j['meta'].get('started_at'), j['meta'].get('finished_at'), 1),
                    'error_code': (j['meta'].get('error') or {}).get('code')} for j in probes],
        'prepare_job_seconds': span(prepare[0]['meta'].get('started_at'), prepare[0]['meta'].get('finished_at'), 1) if prepare else None,
        'prepare_job_status': prepare[0]['meta']['status'] if prepare else None}

    listed = {c['job_id']: c for c in session.get('candidates') or []}
    candidates, results = [], []
    for j in full:
        meta = j['meta']
        result_path = j['dir']/'result.json'
        if not result_path.exists():
            candidates.append({'candidate_id': meta['candidate_id'], 'job_id': meta['job_id'], 'status': meta['status'], 'error': meta.get('error')})
            continue
        result = src.read_json(result_path, 'evaluation result')
        results.append(result)
        rows_ = cases(result['comparisons'])
        s = summary(rows_, result.get('score'))
        entry = listed.get(meta['job_id'], {})
        form = next((r['rows_format'] for r in rows_ if r['rows_format']), None)
        previous = candidates[-1]['source_sha256'] if candidates and 'source_sha256' in candidates[-1] else None
        candidates.append({
            'candidate_id': meta['candidate_id'], 'job_id': meta['job_id'], 'status': meta['status'],
            'requested_utc': utc(meta.get('created_at')), 'finished_utc': utc(meta.get('finished_at')),
            'evaluation_seconds': span(meta.get('started_at'), meta.get('finished_at'), 1),
            'eligible': result.get('eligible'), 'eligible_listed_in_session': entry.get('eligible'),
            'calibration_score_recorded': result.get('score'), 'calibration_score_listed_in_session': entry.get('score'),
            'calibration_score_recomputed': s['candidate_score'], 'recomputed_matches_recorded': s.get('recomputed_job_score_matches'),
            'case_scores_match': s['recomputed_case_scores_match'], 'unchanged_app_score': s['unchanged_app_score'],
            'ratio_unchanged_over_candidate': s['ratio_unchanged_over_candidate'], 'rows_format': form,
            'cases_passed': s['cases_passed'], 'cases_total': s['cases'], 'cases_candidate_better_than_unchanged_app': s['cases_candidate_better'],
            'measurement_pass': result.get('measurement_pass'), 'quality_pass': result.get('quality_pass'),
            'preservation_pass': result.get('preservation_pass'),
            'source_sha256': meta.get('source_sha256'), 'source_changed_since_previous_candidate': None if previous is None else previous != meta.get('source_sha256'),
            'cases': rows_})
        for key in ('measurement_pass', 'quality_pass', 'preservation_pass'):
            if result.get(key) is None:
                missing['candidates.%d.%s' % (len(candidates)-1, key)] = 'the evaluation result of this version does not record this field'
    rec['candidates'] = candidates
    done = [c for c in candidates if c.get('calibration_score_recorded') is not None]
    first, last = (done[0], done[-1]) if done else (None, None)
    block = {'candidates_with_scores': len(done), 'unchanged_app_score': first['unchanged_app_score'] if first else None,
             'unchanged_app_score_identical_for_all_candidates': all(abs(c['unchanged_app_score']-first['unchanged_app_score']) < EPS for c in done) if first else None,
             'rows_format': first['rows_format'] if first else None,
             'error_units': None if not first else ('object-scale units (window position RMSE / scale_px)' if first['rows_format'] == 'per_window_rows'
                                                    else 'body heights (game position RMSE)')}
    if first is None:
        for key in ('unchanged_app_score', 'unchanged_app_score_identical_for_all_candidates', 'rows_format', 'error_units', 'first_candidate_score',
                    'last_candidate_score', 'last_vs_first', 'last_vs_unchanged_app', 'changes_first_to_last'):
            missing['calibration.'+key] = 'no candidate was evaluated in this session'
    else:
        block['first_candidate_score'] = first['calibration_score_recorded']
        block['last_candidate_score'] = last['calibration_score_recorded']
        if len(done) >= 2:
            block['last_vs_first'] = {'error_fell': last['calibration_score_recorded'] < first['calibration_score_recorded'],
                                      'relative_change': (last['calibration_score_recorded']-first['calibration_score_recorded'])/first['calibration_score_recorded'],
                                      'eligible_first': first['eligible'], 'eligible_last': last['eligible'],
                                      'cases_passed_first': first['cases_passed'], 'cases_passed_last': last['cases_passed'],
                                      'source_changed_between_candidates': last['source_sha256'] != first['source_sha256']}
            block['changes_first_to_last'] = changes(results[0], results[-1], first['rows_format'])
        else:
            block['last_vs_first'] = None
            block['changes_first_to_last'] = None
            missing['calibration.last_vs_first'] = 'only one candidate was evaluated'
            missing['calibration.changes_first_to_last'] = 'only one candidate was evaluated'
        block['last_vs_unchanged_app'] = {'last_candidate_below_unchanged_app': last['calibration_score_recorded'] < first['unchanged_app_score'],
                                          'ratio_unchanged_over_last_candidate': last['ratio_unchanged_over_candidate'],
                                          'cases_candidate_better': last['cases_candidate_better_than_unchanged_app'], 'cases': last['cases_total'],
                                          'every_evaluated_candidate_below_unchanged_app': all(c['calibration_score_recorded'] < c['unchanged_app_score'] for c in done)}
    rec['calibration'] = block

    # --- timeline and the agent's activity after seeing feedback (descriptive only)
    final_calls = [r for r in rows if r['tool'] == MCP_PREFIX+'finalize_candidate']
    full_calls = [c[1] for c in calls if c not in partial_calls]
    rec['timeline'] = {'first_tool_call_utc': utc(t0), 'first_tool_call_date_utc': utc(t0)[:10], 'last_tool_call_utc': utc(t1), 'audited_span_seconds': round(t1-t0, 1),
                       'prompt_started_utc': utc(launch.get('prompt_started_at')), 'response_received_utc': utc(launch.get('response_received_at')),
                       'agent_turn_seconds': span(launch.get('prompt_started_at'), launch.get('response_received_at')),
                       'sealed_utc': utc(session.get('sealed_at')),
                       'authoring_clock_seconds': span(session.get('started_at'), session.get('sealed_at')),
                       'launcher_started_utc': utc(launch.get('started_at')), 'launcher_finished_utc': utc(launch.get('finished_at')),
                       'launcher_wall_seconds': span(launch.get('started_at'), launch.get('finished_at'), 1),
                       'first_evaluate_call_offset_seconds': round(calls[0][1]['at']-t0, 1) if calls else None,
                       'finalize_call_offset_seconds': round(final_calls[0]['at']-t0, 1) if final_calls else None}
    if launch.get('response_received_at') is None:
        missing['timeline.agent_turn_seconds'] = 'no model response was received (the run ended without an end_turn)'
        missing['timeline.response_received_utc'] = missing['timeline.agent_turn_seconds']
    if session.get('sealed_at') is None:
        missing['timeline.authoring_clock_seconds'] = 'no candidate was sealed'
        missing['timeline.sealed_utc'] = 'no candidate was sealed'
    if launch.get('finished_at') is None:
        missing['timeline.launcher_wall_seconds'] = 'launch.json was never completed (the launcher was interrupted)'
        missing['timeline.launcher_finished_utc'] = missing['timeline.launcher_wall_seconds']
    if not calls:
        missing['timeline.first_evaluate_call_offset_seconds'] = 'the agent never called evaluate_candidate'
    if not final_calls:
        missing['timeline.finalize_call_offset_seconds'] = 'the agent never called finalize_candidate'
    act = {'definition': 'audited calls strictly between the finished_at of the first full evaluation and the next call; scripts and file '
                         'writes = execute_bash, execute_pwsh, fs_write, str_replace; a descriptive count, not evidence of causality',
           'to_next_full_evaluation': None, 'to_next_evaluate_call_including_probes': None, 'to_finalize_call': None}
    if full and done:
        start = full[0]['meta']['finished_at']
        later_full = [r for r in full_calls if r['at'] > start]
        later_any = [c[1] for c in calls if c[1]['at'] > start]
        if later_full:
            act['to_next_full_evaluation'] = activity(rows, start, later_full[0]['at'])
        if later_any:
            act['to_next_evaluate_call_including_probes'] = activity(rows, start, later_any[0]['at'])
        if final_calls:
            act['to_finalize_call'] = activity(rows, start, final_calls[0]['at'])
    for key, reason in (('to_next_full_evaluation', 'no second full evaluation followed the first candidate result'),
                        ('to_next_evaluate_call_including_probes', 'no second evaluate_candidate call followed the first candidate result'),
                        ('to_finalize_call', 'no finalize_candidate call, or no candidate result to start from')):
        if act[key] is None:
            missing['agent_activity.'+key] = reason
    rec['agent_activity'] = act

    # --- finalize and held-out
    finals = [j for j in jobs if j['meta']['operation'] == 'finalize']
    attempts = [{'job_id': j['meta']['job_id'], 'candidate_id': j['meta'].get('candidate_id'), 'status': j['meta']['status'],
                 'error_code': (j['meta'].get('error') or {}).get('code'), 'error_message': (j['meta'].get('error') or {}).get('message'),
                 'requested_utc': utc(j['meta'].get('created_at')), 'finished_utc': utc(j['meta'].get('finished_at')),
                 'seconds': span(j['meta'].get('started_at'), j['meta'].get('finished_at'), 1)} for j in finals]
    fin = {'attempts': attempts, 'session_status': session.get('status'), 'session_final': session.get('final'),
           'selected_candidate_id': session.get('selected_candidate_id'), 'heldout': None}
    result = None
    if finals and finals[-1]['meta']['status'] == 'succeeded' and (finals[-1]['dir']/'result.json').exists():
        job = finals[-1]
        result = src.read_json(job['dir']/'result.json', 'finalize result')
        comparison_path = sdir/'measurements'/job['meta']['job_id']/'comparison.json'
        comparisons = src.read_json(comparison_path, 'held-out comparison')
        held = cases(comparisons)
        by_role = {role: summary([r for r in held if r['role'] == role]) for role in sorted({r['role'] for r in held})}
        overall = summary(held, result.get('score'))
        fin['heldout'] = {
            'job_id': job['meta']['job_id'], 'cases_passed': overall['cases_passed'], 'cases_total': overall['cases'],
            'by_role': {role: {'passed': s['cases_passed'], 'total': s['cases']} for role, s in by_role.items()},
            'by_role_recorded_groups': result.get('groups'),
            'groups_match_rows': all((result.get('groups') or {}).get(role) == {'passed': s['cases_passed'], 'required': s['cases']} for role, s in by_role.items()),
            'comparison_json_equals_result_comparisons': comparisons == result.get('comparisons'),
            'candidate_score': overall['candidate_score'], 'unchanged_app_score': overall['unchanged_app_score'],
            'ratio_unchanged_over_candidate': overall['ratio_unchanged_over_candidate'], 'cases_candidate_better': overall['cases_candidate_better'],
            'recorded_job_score': result.get('score'), 'recomputed_job_score_matches': overall.get('recomputed_job_score_matches'),
            'case_scores_match': overall['recomputed_case_scores_match'], 'rows_format': next((r['rows_format'] for r in held if r['rows_format']), None),
            'summary_by_role': {role: {k: v for k, v in s.items() if k not in ('cases_with_both',)} for role, s in by_role.items()},
            'failed_cases_by_signature': dict(sorted(Counter(r['failure'] for r in held if r['failure']).items())),
            'failed_cases': [{'case_id': r['case_id'], 'role': r['role'], 'failure': r['failure'], 'candidate': r['candidate'], 'unchanged_app': r['unchanged_app'],
                              'failing_windows': [{'object_id': g['object_id'], 'window_id': g['window_id'], 'candidate': g['after_rmse'], 'unchanged_app': g['before_rmse']}
                                                  for g in c.get('improvement') or [] if isinstance(g, dict) and g.get('pass') is False]}
                             for r, c in zip(held, comparisons) if r['pass'] is False],
            'numerical_pass': result.get('pass'), 'eligible': result.get('eligible'), 'cases': held}
    else:
        reason = ('no finalize job was started' if not finals else
                  'the finalize job ended as %s (%s), so no held-out comparison was recorded' % (finals[-1]['meta']['status'], attempts[-1]['error_code']))
        missing['finalize.heldout'] = reason
        if finals and finals[-1]['meta']['status'] != 'succeeded':
            final_measure = sdir/'measurements'/finals[-1]['meta']['job_id']/'final.json'
            if final_measure.exists():
                fin['note'] = ('measurements/%s/final.json holds raw per-case measurement records without comparison verdicts' % finals[-1]['meta']['job_id'])
    for index, attempt in enumerate(attempts):
        if attempt['seconds'] is None:
            missing['finalize.attempts.%d.seconds' % index] = 'the finalize job was refused before it started (no start time)'
        if attempt['candidate_id'] is None:
            missing['finalize.attempts.%d.candidate_id' % index] = 'the finalize job was refused before a candidate was selected'
    if session.get('final') is None:
        missing['finalize.session_final'] = 'session.json records no final result (nothing was finalized in the session record)'
    if session.get('selected_candidate_id') is None:
        missing['finalize.selected_candidate_id'] = 'no candidate was sealed'
    rec['finalize'] = fin

    # --- recorded verdict
    category, detail, refs = VERDICTS[sid]
    derived = derive_outcome(attempts, result)
    verdict = {'category': category, 'detail': detail, 'recorded_in': [{'file': f, 'lines': l, 'sha256': logs[f]} for f, l in refs],
               'derived_from_session_files': derived, 'session_files_agree': DERIVED_TO_CATEGORY[derived] == category}
    interruption = sdir/'operator-interruption.json'
    if interruption.exists():
        record = src.read_json(interruption, 'operator interruption record')
        verdict['session_record'] = {'file': interruption.as_posix(), 'status': record.get('status'), 'behavior_outcome': record.get('behavior_outcome'),
                                     'kiro_behavior_failure_inferred': record.get('kiro_behavior_failure_inferred'), 'candidate_count': record.get('candidate_count')}
    report = sdir/'report/report.json'
    if report.exists():
        src.note(report, 'report (first 256 KiB read for overall_acceptance)')
        verdict['report_overall_acceptance'] = head_value(report, 'overall_acceptance')
        verdict['report_session_status'] = head_value(report, 'session_status')
        verdict['report_file'] = report.as_posix()
    else:
        missing['verdict.report_overall_acceptance'] = 'no report/report.json was written for this session'
    diff_path = sdir/'report/candidate.diff'
    if diff_path.exists():
        diff_lines = src.read_bytes(diff_path, 'sealed candidate diff against the baseline').decode('utf-8', 'replace').split('\n')
        names = [line[4:].strip() for line in diff_lines if line.startswith('+++ ')]
        rec['finalize']['sealed_candidate_diff'] = {
            'file': diff_path.as_posix(), 'files_changed': len(names), 'file_names': [n[len('after/'):] if n.startswith('after/') else n for n in names],
            'lines_added': sum(line.startswith('+') and not line.startswith('+++ ') for line in diff_lines),
            'lines_removed': sum(line.startswith('-') and not line.startswith('--- ') for line in diff_lines)}
    else:
        rec['finalize']['sealed_candidate_diff'] = None
        missing['finalize.sealed_candidate_diff'] = 'no report/candidate.diff exists (no report was written for this session)'
    if sid in CLOSURES:
        folder = CLOSURE_ROOT/CLOSURES[sid]
        closure = src.read_json(folder/'closure.json', 'domain closure')
        summary_ = src.read_json(folder/'summary.json', 'domain closure summary')
        verdict['closure'] = {'folder': folder.as_posix(), 'status': closure.get('status'), 'domain_behavior_acceptance': closure.get('domain_behavior_acceptance'),
                              'canonical_session_acceptance_changed': closure.get('canonical_session_acceptance_changed'),
                              'full_phase_acceptance': closure.get('full_phase_acceptance'), 'scope': closure.get('scope'),
                              'axes': summary_.get('axes'), 'closure_sha256': src.note(folder/'closure.json', 'domain closure'),
                              'receipt_sha256': src.note(folder/'receipt.json', 'domain closure receipt')}
        verdict['session_files_agree'] = verdict['session_files_agree'] and closure.get('status') == 'pass' and closure.get('domain_behavior_acceptance') is True
    rec['verdict'] = verdict

    # --- OpenCV version recorded in the measurement records
    versions, files = scan_opencv(sdir, jobs, src)
    rec['opencv'] = {'versions_in_measurement_records': versions or None, 'records_scanned_files': files,
                     'every_record_reports_opencv_5': all(v.startswith('5.') for v in versions) if versions else None}
    if not versions:
        missing['opencv.versions_in_measurement_records'] = 'no measurement records exist in this session (the only job was cancelled before it measured)'
        missing['opencv.every_record_reports_opencv_5'] = missing['opencv.versions_in_measurement_records']
    rec['null_reasons'] = dict(sorted(missing.items()))
    return rec


# --------------------------------------------------------------------------- cross-checks against the results logs

def checks(S, logs):
    """(version, session, quantity, value written in the log, log file, lines, observed from the files, digits)."""
    c, k = [], {'n': 0}

    def add(version, sid, quantity, logged, source, lines, observed, digits=None, note=None):
        k['n'] += 1
        c.append({'id': 'C%02d' % k['n'], 'version': version, 'session': sid, 'quantity': quantity, 'log_value': logged, 'log_file': source,
                  'log_lines': lines, 'file_value': observed, 'compared_at_decimals': digits, 'log_sha256': logs[source], 'note': note})

    st, v2s, v2p = 'PHASE3D_STATUS.md', 'PHASE3D_V2_STATUS.md', 'PHASE3D_V2_PENDING_RESULTS.md'
    v3g, v3p = 'reviews/phase3d-v3/v3g02-game-result.md', 'reviews/phase3d-v3/v3p01-panel-result.md'
    v4, v5, v6 = 'reviews/phase3d-v4/v4-results.md', 'reviews/phase3d-v5/v5-results.md', 'reviews/phase4-v6/v6-results.md'
    ho = lambda sid, role: (dig(S[sid], 'finalize.heldout.by_role.'+role+'.passed'), dig(S[sid], 'finalize.heldout.by_role.'+role+'.total'))
    sc = lambda sid, i: dig(S[sid], 'candidates.%d.calibration_score_recorded' % i)
    passed = lambda sid, i: (dig(S[sid], 'candidates.%d.cases_passed' % i), dig(S[sid], 'candidates.%d.cases_total' % i))

    # V1
    add('V1', 'gj04', 'audited tool calls', 8, st, 'L68', dig(S['gj04'], 'audit.rows'))
    add('V1', 'gj05', 'audited tool calls', 31, st, 'L11; L78', dig(S['gj05'], 'audit.rows'))
    add('V1', 'gj05', 'candidates evaluated', 0, st, 'L76', len(S['gj05']['candidates']))
    add('V1', 'gj05', 'request deadline in seconds (budget)', 1800, st, 'L11', dig(S['gj05'], 'budget.max_seconds'))
    add('V1', 'gj05', 'native event log lines', 1386, st, 'L78', dig(S['gj05'], 'agent.native_event_log_lines'))
    add('V1', 'gj05', 'prompt start time of day (UTC)', '00:26:13', st, 'L76', clock(dig(S['gj05'], 'timeline.prompt_started_utc')))
    add('V1', 'p04', 'prompt start time of day (UTC)', '01:00:51', st, 'L80', clock(dig(S['p04'], 'timeline.prompt_started_utc')))
    add('V1', 'p04', 'audited tool calls', 60, st, 'L84', dig(S['p04'], 'audit.rows'))
    add('V1', 'p04', 'native event log lines', 3037, st, 'L84', dig(S['p04'], 'agent.native_event_log_lines'))
    add('V1', 'p04', 'calibration cases passed by candidate-01 (passed, total)', [8, 8], st, 'L12; L80', list(passed('p04', 0)))
    add('V1', 'p04', 'held-out regression (passed, total)', [10, 12], st, 'L12; L80', list(ho('p04', 'regression')))
    add('V1', 'p04', 'held-out validation (passed, total)', [9, 12], st, 'L12; L80', list(ho('p04', 'validation')))
    # V2
    add('V2', 'v2g01', 'audited tool calls', 113, v2p, 'L27-L28', dig(S['v2g01'], 'audit.rows'))
    add('V2', 'v2p08', 'audited tool calls', 92, v2p, 'L27-L28', dig(S['v2p08'], 'audit.rows'))
    add('V2', 'v2p08', 'calibration cases passed (passed, total)', [8, 8], v2p, 'L138', list(passed('v2p08', 1)))
    add('V2', 'v2p08', 'held-out regression (passed, total)', [11, 12], v2s, 'L14', list(ho('v2p08', 'regression')))
    add('V2', 'v2p08', 'held-out validation (passed, total)', [12, 12], v2s, 'L14', list(ho('v2p08', 'validation')))
    add('V2', 'v2j03', 'candidate-01 calibration error', 0.07634287, v2s, 'L179', sc('v2j03', 0), 8)
    add('V2', 'v2j03', 'candidate-02 calibration error', 0.04984168, v2s, 'L181', sc('v2j03', 1), 8)
    add('V2', 'v2j03', 'authoring clock in seconds (sealed minus started)', 1919.727, v2s, 'L186', dig(S['v2j03'], 'timeline.authoring_clock_seconds'), 3)
    add('V2', 'v2j03', 'finalize error message', "'function' object has no attribute 'is_set'", v2s, 'L194-L195',
        dig(S['v2j03'], 'finalize.attempts.0.error_message'))
    add('V2', 'v2j03', 'prompt start, sealing and final-job end time of day (UTC)', ['12:12:23', '12:44:23', '12:56:32'], v2s, 'L167; L189; L194',
        [clock(dig(S['v2j03'], 'timeline.prompt_started_utc')), clock(dig(S['v2j03'], 'timeline.sealed_utc')), clock(dig(S['v2j03'], 'finalize.attempts.0.finished_utc'))])
    add('V2', 'v2g01', 'model response time in seconds (response minus prompt)', 2898.493, v2p, 'L159-L160', dig(S['v2g01'], 'timeline.agent_turn_seconds'), 3)
    add('V2', 'v2g01', 'finalize error code', 'no_eligible_candidate', v2p, 'L172', dig(S['v2g01'], 'finalize.attempts.0.error_code'))
    add('V2', 'v2j03', 'startup check seconds and MCP tools verified at preflight', [8.609, 7], v2s, 'L168',
        [round(dig(S['v2j03'], 'agent.startup_check_seconds'), 3), dig(S['v2j03'], 'agent.mcp_tools_at_preflight')])
    add('V2', 'v2j03', 'upstream pause notifications', 0, v2s, 'L197', dig(S['v2j03'], 'agent.upstream_pause_notifications'))
    add('V2', 'v2g01', 'task prompts sent, automatic resume sent, deadline extended, pause notifications', [1, False, False, 0], v2p, 'L157-L160',
        [dig(S['v2g01'], 'agent.task_prompts_sent'), dig(S['v2g01'], 'agent.automatic_resume_sent'), dig(S['v2g01'], 'agent.deadline_extended'),
         dig(S['v2g01'], 'agent.upstream_pause_notifications')])
    add('V2', 'v2g01', 'candidate-02 calibration cases passed (passed, total): long run 3/3, protected jumps 6/6, short run 0/3', [9, 12], v2p, 'L140', list(passed('v2g01', 1)))
    case = lambda sid, i, cid, key: next((r[key] for r in S[sid]['candidates'][i]['cases'] if r['case_id'] == cid), None)
    add('V2', 'v2g01', 'right-6 scenario: unchanged-app error', 0.0643216, v2p, 'L167', case('v2g01', 0, 'ground-calibration-r06-x0-r1', 'unchanged_app'), 7)
    add('V2', 'v2g01', 'right-6 scenario: candidate-01 error', 0.0417506, v2p, 'L167', case('v2g01', 0, 'ground-calibration-r06-x0-r1', 'candidate'), 7)
    add('V2', 'v2g01', 'right-6 scenario: candidate-02 error', 0.0374311, v2p, 'L167', case('v2g01', 1, 'ground-calibration-r06-x0-r1', 'candidate'), 7)
    add('V2', 'v2g01', 'right-48 scenario: candidate-02 error', 0.0872237, v2p, 'L168', case('v2g01', 1, 'ground-calibration-r48-x0-r1', 'candidate'), 7)
    # V3
    add('V3', 'v3g02', 'held-out regression (passed, total)', [30, 30], v3g, 'L9', list(ho('v3g02', 'regression')))
    add('V3', 'v3g02', 'held-out validation (passed, total)', [24, 30], v3g, 'L9', list(ho('v3g02', 'validation')))
    add('V3', 'v3g02', 'failed held-out cases that belong to recipe ground-validation-l33 (of all failed)', [6, 6], v3g, 'L17',
        [sum('ground-validation-l33' in f['case_id'] for f in dig(S['v3g02'], 'finalize.heldout.failed_cases')), len(dig(S['v3g02'], 'finalize.heldout.failed_cases'))])
    add('V3', 'v3g02', 'sealed candidate source starts with', 'e0054455', v3g, 'L27', (dig(S['v3g02'], 'candidates.0.source_sha256') or '')[:8])
    add('V3', 'v3g02', 'task prompts sent', 1, v3g, 'L12', dig(S['v3g02'], 'agent.task_prompts_sent'))
    add('V3', 'v3p01', 'held-out regression (passed, total)', [21, 24], v3p, 'L7', list(ho('v3p01', 'regression')))
    add('V3', 'v3p01', 'held-out validation (passed, total)', [8, 12], v3p, 'L7', list(ho('v3p01', 'validation')))
    add('V3', 'v3p01', 'final job id starts with', 'a7163f41', v3p, 'L7', (dig(S['v3p01'], 'finalize.attempts.0.job_id') or '')[:8])
    add('V3', 'v3p01', 'sealed candidate and its source prefix', ['candidate-02', '52ff07b0'], v3p, 'L7',
        [dig(S['v3p01'], 'finalize.selected_candidate_id'), (dig(S['v3p01'], 'candidates.1.source_sha256') or '')[:8]])
    add('V3', 'v3p01', 'validation failures flagged required_order_unobservable (of validation failures)', [4, 4], v3p, 'L24',
        [sum(f['failure'] == 'required_order_unobservable' and f['role'] == 'validation' for f in dig(S['v3p01'], 'finalize.heldout.failed_cases')),
         sum(f['role'] == 'validation' for f in dig(S['v3p01'], 'finalize.heldout.failed_cases'))])
    # V4
    add('V4', 'v4g01', 'held-out regression (passed, total)', [48, 48], v4, 'L7', list(ho('v4g01', 'regression')))
    add('V4', 'v4g01', 'held-out validation (passed, total)', [30, 30], v4, 'L8', list(ho('v4g01', 'validation')))
    add('V4', 'v4g01', 'sealed candidate', 'candidate-01', v4, 'L10', dig(S['v4g01'], 'finalize.selected_candidate_id'))
    add('V4', 'v4g01', 'audited tool calls (74 calls and 74 hooks)', 74, v4, 'L61', dig(S['v4g01'], 'audit.rows'))
    add('V4', 'v4g01', 'task prompts sent', 1, v4, 'L61', dig(S['v4g01'], 'agent.task_prompts_sent'))
    add('V4', 'v4g01', 'closure.json SHA-256', 'a3232cd47fea1d2e6a1b59e8375d6e9f20663a426073087569145115198726dc', v4, 'L25',
        dig(S['v4g01'], 'verdict.closure.closure_sha256'))
    add('V4', 'v4g01', 'closure receipt.json SHA-256', '0800150d9f0771548f7cfb0915d372508b2f4f6c6699290579414f5845eafce5', v4, 'L25',
        dig(S['v4g01'], 'verdict.closure.receipt_sha256'))
    add('V4', 'v4p01', 'held-out validation (passed, total)', [12, 12], v4, 'L46', list(ho('v4p01', 'validation')))
    add('V4', 'v4p01', 'held-out regression (passed, total)', [27, 36], v4, 'L47', list(ho('v4p01', 'regression')))
    add('V4', 'v4p01', 'failed cases flagged required_order_unobservable', 4, v4, 'L48',
        dig(S['v4p01'], 'finalize.heldout.failed_cases_by_signature.required_order_unobservable'))
    add('V4', 'v4p01', 'failed cases that are pixel-window failures', 5, v4, 'L49',
        dig(S['v4p01'], 'finalize.heldout.failed_cases_by_signature.per_window_pixel_failure'))
    # V5 / V5b
    add('V5', 'v5p01', 'audited rows (hook completions)', 104, v5, 'L94', dig(S['v5p01'], 'audit.rows'))
    add('V5', 'v5p01', 'candidate-01 calibration cases passed (passed, total)', [21, 22], v5, 'L79', list(passed('v5p01', 0)))
    add('V5', 'v5p01', 'candidate-02 calibration cases passed (passed, total)', [21, 22], v5, 'L80', list(passed('v5p01', 1)))
    add('V5', 'v5p01', 'candidate-01 calibration error (rounded to 3 decimals)', 0.023, v5, 'L169', sc('v5p01', 0), 3)
    add('V5', 'v5p01', 'candidate-02 calibration error (rounded to 3 decimals)', 0.022, v5, 'L169', sc('v5p01', 1), 3)
    add('V5', 'v5p01', 'prompt duration in minutes', 55, v5, 'L68', None if dig(S['v5p01'], 'timeline.agent_turn_seconds') is None else dig(S['v5p01'], 'timeline.agent_turn_seconds')/60, 0)
    add('V5', 'v5p01', 'prompt start and response time of day (logs use local time, UTC+3)', ['16:42:55', '17:37:55'], v5, 'L68',
        [clock(dig(S['v5p01'], 'timeline.prompt_started_utc'), 3), clock(dig(S['v5p01'], 'timeline.response_received_utc'), 3)])
    add('V5', 'v5p01', 'evaluation duration in minutes (candidate-01)', 15, v5, 'L84', dig(S['v5p01'], 'candidates.0.evaluation_seconds')/60, 0)
    add('V5', 'v5p01', 'finalize error code', 'no_eligible_candidate', v5, 'L75', dig(S['v5p01'], 'finalize.attempts.0.error_code'))
    add('V5', 'v4p01', 'evaluation duration in minutes of the V4 panel run with 16 calibration cases (quoted in the V5 log)', 11, v5, 'L84',
        dig(S['v4p01'], 'candidates.0.evaluation_seconds')/60, 0,
        note='The only V4 panel agent evaluation (v4p01 candidate-01, 16 cases) took %.1f minutes (%.1f s started to finished) in its job record; the V5 log may refer to another measurement, which the session files do not show.' % (
            dig(S['v4p01'], 'candidates.0.evaluation_seconds')/60, dig(S['v4p01'], 'candidates.0.evaluation_seconds')))
    fail01 = [r for r in S['v5p01']['candidates'][0]['cases'] if r['pass'] is False]
    fail02 = [r for r in S['v5p01']['candidates'][1]['cases'] if r['pass'] is False]
    add('V5', 'v5p01', 'failing calibration case of candidate-01 is the wide reverse 1280 px repeat 1', 'realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1', v5, 'L79',
        fail01[0]['case_id'] if len(fail01) == 1 else None)
    add('V5', 'v5p01', 'failing calibration case of candidate-02 is the wide reverse 1280 px repeat 2', 'realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2', v5, 'L80',
        fail02[0]['case_id'] if len(fail02) == 1 else None)
    add('V5b', 'v5p02c', 'candidate-01 calibration cases passed (passed, total)', [16, 16], v5, 'L169', list(passed('v5p02c', 0)))
    add('V5b', 'v5p02c', 'candidate-01 calibration error (rounded to 4 decimals)', 0.0075, v5, 'L169', sc('v5p02c', 0), 4)
    add('V5b', 'v5p02c', 'finalize error message', 'Input deadline missed by 0.239428s; late commands will not be compressed into a burst', v5, 'L170',
        dig(S['v5p02c'], 'finalize.attempts.0.error_message'))
    add('V5b', 'v5p02c', 'prompt start and response time of day (logs use local time, UTC+3)', ['22:45:43', '23:32:56'], v5, 'L168',
        [clock(dig(S['v5p02c'], 'timeline.prompt_started_utc'), 3), clock(dig(S['v5p02c'], 'timeline.response_received_utc'), 3)])
    add('V5b', 'v5p02c', 'finalize job start and end time of day (logs use local time, UTC+3)', ['23:31', '00:07'], v5, 'L170',
        [clock(dig(S['v5p02c'], 'finalize.attempts.0.requested_utc'), 3)[:5], clock(dig(S['v5p02c'], 'finalize.attempts.0.finished_utc'), 3)[:5]])
    # V6
    counts = dig(S['v6p01'], 'audit.mcp_calls_by_name')
    other = dig(S['v6p01'], 'audit.other_calls_by_tool')
    add('V6', 'v6p01', 'audit rows', 230, v6, 'L264', dig(S['v6p01'], 'audit.rows'))
    add('V6', 'v6p01', 'MCP tools exposed at preflight', 8, v6, 'L22', dig(S['v6p01'], 'agent.mcp_tools_at_preflight'))
    for name, logged in (('get_job', 126), ('inspect_session', 4), ('evaluate_candidate', 3), ('prepare_experiment', 1), ('cancel_job', 1), ('finalize_candidate', 1)):
        add('V6', 'v6p01', 'MCP calls: ' + name, logged, v6, 'L264-L265', counts[name])
    for name, logged in (('execute_pwsh', 61), ('fs_write', 18), ('str_replace', 7), ('read_file', 6)):
        add('V6', 'v6p01', 'tool calls: ' + name, logged, v6, 'L264-L265', other.get(name))
    add('V6', 'v6p01', 'preparation job in seconds', 426, v6, 'L259', dig(S['v6p01'], 'evaluations.prepare_job_seconds'), 0)
    add('V6', 'v6p01', 'candidate-01 calibration error (rounded to 4 decimals)', 0.0244, v6, 'L260', sc('v6p01', 0), 4)
    add('V6', 'v6p01', 'candidate-02 calibration error (rounded to 4 decimals)', 0.0067, v6, 'L262', sc('v6p01', 1), 4)
    add('V6', 'v6p01', 'candidate-02 versus candidate-01 improvement factor (rounded to 1 decimal)', 3.6, v6, 'L262',
        sc('v6p01', 0)/sc('v6p01', 1), 1)
    add('V6', 'v6p01', 'partial probe cancelled after seconds', 151, v6, 'L261', dig(S['v6p01'], 'evaluations.probes.0.seconds'), 0)
    add('V6', 'v6p01', 'partial probe cases requested', 3, v6, 'L261', dig(S['v6p01'], 'evaluations.probes.0.cases_requested'))
    add('V6', 'v6p01', 'held-out cases passed (passed, total)', [55, 56], v6, 'L268', [dig(S['v6p01'], 'finalize.heldout.cases_passed'), dig(S['v6p01'], 'finalize.heldout.cases_total')])
    add('V6', 'v6p01', 'held-out validation (passed, total)', [12, 12], v6, 'L269', list(ho('v6p01', 'validation')))
    add('V6', 'v6p01', 'held-out regression (passed, total)', [43, 44], v6, 'L270', list(ho('v6p01', 'regression')))
    add('V6', 'v6p01', 'the failed held-out case', 'realistic-v4-suite-panel-validation-drag-release-v4-w1440-r1', v6, 'L270',
        (dig(S['v6p01'], 'finalize.heldout.failed_cases.0.case_id') if len(dig(S['v6p01'], 'finalize.heldout.failed_cases')) == 1 else None))
    windows = dig(S['v6p01'], 'finalize.heldout.failed_cases.0.failing_windows') or []
    add('V6', 'v6p01', 'failing post-release windows: candidate error range (2 decimals) and unchanged-app error range (3 decimals)', [0.78, 0.86, 0.005, 0.006], v6, 'L270',
        [round(min(w['candidate'] for w in windows), 2), round(max(w['candidate'] for w in windows), 2),
         round(min(w['unchanged_app'] for w in windows), 3), round(max(w['unchanged_app'] for w in windows), 3)] if windows else None)
    add('V6', 'v6p01', 'finalize job in minutes', 38, v6, 'L253', dig(S['v6p01'], 'finalize.attempts.0.seconds')/60, 0)
    add('V6', 'v6p01', 'report.json SHA-256 starts with', '0b1427f9', v6, 'L273', (S['v6p01']['report_sha256'] or '')[:8])
    add('V6', 'v6p01', 'kiro-cli, KAS and model', ['2.31.0', '0.66.34', 'claude-opus-5.5'], v6, 'L255',
        [dig(S['v6p01'], 'agent.kiro_cli_version'), dig(S['v6p01'], 'agent.kas_version'), dig(S['v6p01'], 'agent.model')])
    add('V6', 'v6p01', 'inspect_window calls across all 13 audited sessions', 0, v6, 'L266; L310', sum(dig(s, 'audit.mcp_calls_by_name.inspect_window') for s in S.values()))
    for item in c:
        a, b, d = item['log_value'], item['file_value'], item['compared_at_decimals']
        if d is not None and isinstance(b, (int, float)) and isinstance(a, (int, float)):
            item['agree'] = round(b, d) == round(a, d)
        else:
            item['agree'] = a == b
    return c


def explained_differences(S):
    """Places where a log and the files use different definitions (not numeric contradictions)."""
    v5, v6, v4 = S['v5p01'], S['v6p01'], S['v4g01']
    return [
        {'id': 'D1', 'session': 'v5p01', 'log': 'reviews/phase3d-v5/v5-results.md L94',
         'log_says': '103 application calls and 104 hook completions',
         'files_say': 'the audit file has %d rows (one row per hook decision, including one tool_load row)' % v5['audit']['rows'],
         'explanation': 'The audit file counts hook decisions; the separate application-call count of 103 cannot be derived from it. The 104 matches.'},
        {'id': 'D2', 'session': 'v6p01', 'log': 'reviews/phase4-v6/v6-results.md L252',
         'log_says': 'Kiro turn 20:04-21:31 (87 minutes) and a 38 minute finalize job',
         'files_say': 'agent turn (prompt %s to end_turn %s local time, UTC+3) %.1f minutes; launcher %s to %s local time = %.1f minutes wall time, of which the held-out finalize job took %.1f minutes' % (
             clock(v6['timeline']['prompt_started_utc'], 3), clock(v6['timeline']['response_received_utc'], 3), v6['timeline']['agent_turn_seconds']/60,
             clock(v6['timeline']['launcher_started_utc'], 3), clock(v6['timeline']['launcher_finished_utc'], 3), v6['timeline']['launcher_wall_seconds']/60,
             v6['finalize']['attempts'][0]['seconds']/60),
         'explanation': 'The 87 minutes match the launcher wall time (which includes the post-turn held-out finalize), not the agent turn; the clock times printed in the log differ from the launcher\'s recorded start and end by about 6 to 7 minutes and the files do not show where they come from. The 38 minutes match the finalize job.'},
        {'id': 'D3', 'session': 'v4g01', 'log': 'reviews/phase3d-v4/v4-results.md L15-L25',
         'log_says': 'the game domain closure passed (behaviour acceptance given)',
         'files_say': "the session's own report.json records overall_acceptance '%s' and the closure states canonical_session_acceptance_changed %s" % (
             v4['verdict'].get('report_overall_acceptance'), json.dumps(v4['verdict']['closure']['canonical_session_acceptance_changed'])),
         'explanation': "The accepted verdict rests on a separate domain closure artifact. The session report is written at the end of the run, when the independent review status is still '%s'." %
                        v4['finalize']['session_final']['independent_review_status']},
        {'id': 'D4', 'session': 'p04', 'log': 'PHASE3D_STATUS.md L84',
         'log_says': '3,037 events',
         'files_say': 'kiro/run/stdout.jsonl has %d lines; launch.json events_seen is %d' % (S['p04']['agent']['native_event_log_lines'], S['p04']['agent']['events_seen_by_launcher']),
         'explanation': 'The log counts stdout lines (events_seen plus the post-response notifications); both numbers are recorded and consistent.'},
        {'id': 'D5', 'session': 'all', 'log': 'all results logs',
         'log_says': 'dates and clock times in local time (UTC+3 according to the logs that name the zone)',
         'files_say': 'this table uses UTC from the audit timestamps (for example gj04 first call %s)' % S['gj04']['timeline']['first_tool_call_utc'],
         'explanation': 'Sessions that started between 21:00 and 24:00 UTC fall on the next calendar day in the logs.'},
        {'id': 'D6', 'session': 'v2g01; v5p01', 'log': 'PHASE3D_V2_PENDING_RESULTS.md L172-L173; reviews/phase3d-v5/v5-results.md L75',
         'log_says': 'finalize_candidate was refused with no_eligible_candidate',
         'files_say': "session.json keeps status '%s' (v2g01) and '%s' (v5p01) with final null although a failed finalize job exists in each" % (
             S['v2g01']['finalize']['session_status'], S['v5p01']['finalize']['session_status']),
         'explanation': 'The refusal is recorded in the job record only; the session state machine was never advanced.'},
        {'id': 'D7', 'session': 'gj04', 'log': 'PHASE3D_STATUS.md L66',
         'log_says': 'operator_interrupted, behaviour unevaluated',
         'files_say': "launch.json status '%s' and session.json status '%s'; operator-interruption.json carries the final status" % (
             S['gj04']['agent']['launch_status'], S['gj04']['finalize']['session_status']),
         'explanation': 'The launcher was stopped before it could close its own record.'},
        {'id': 'D8', 'session': 'v6p01', 'log': 'reviews/phase4-v6/v6-results.md L264-L265',
         'log_says': 'a per-tool list for 230 audit rows',
         'files_say': 'the listed tools sum to 228; the audit file also has list_directory %d and tool_load %d' % (
             v6['audit']['other_calls_by_tool'].get('list_directory', 0), v6['audit']['other_calls_by_tool'].get('tool_load', 0)),
         'explanation': 'The log omits two rows from its breakdown; its total of 230 matches.'}]


# --------------------------------------------------------------------------- totals

def totals(S):
    sessions = list(S.values())
    cand = [c for s in sessions for c in s['candidates'] if c.get('calibration_score_recorded') is not None]
    multi = [s for s in sessions if s['calibration'].get('last_vs_first')]
    withc = [s for s in sessions if s['candidates']]
    held = [s for s in sessions if s['finalize']['heldout']]
    roles = {}
    for s in held:
        for role, v in s['finalize']['heldout']['by_role'].items():
            roles.setdefault(role, [0, 0])
            roles[role][0] += v['passed']
            roles[role][1] += v['total']
    ratios = lambda domain: [s['calibration']['last_vs_unchanged_app']['ratio_unchanged_over_last_candidate'] for s in withc if s['domain'] == domain]
    mcp = Counter()
    for s in sessions:
        mcp.update(s['audit']['mcp_calls_by_name'])
    windows = [s for s in multi if s['agent_activity']['to_next_full_evaluation']]
    return {
        'sessions_logged': len(sessions), 'sessions_by_version': dict(Counter(s['version'] for s in sessions)),
        'sessions_by_domain': dict(Counter('%s (%s)' % (s['domain'], s['mechanic']) for s in sessions)),
        'first_tool_call_utc_range': [min(s['timeline']['first_tool_call_utc'] for s in sessions), max(s['timeline']['first_tool_call_utc'] for s in sessions)],
        'models': dict(Counter('%s / %s' % (s['agent']['model'], s['agent']['effort']) for s in sessions)),
        'kiro_cli_versions': dict(sorted(Counter(s['agent']['kiro_cli_version'] for s in sessions).items())),
        'audited_tool_calls': sum(s['audit']['rows'] for s in sessions), 'mcp_calls': sum(s['audit']['mcp_calls'] for s in sessions),
        'mcp_calls_by_name': dict(sorted(mcp.items())), 'scripts_and_file_writes': sum(s['audit']['scripts_and_file_writes'] for s in sessions),
        'scripts_and_file_writes_refused': sum(s['audit']['scripts_and_file_writes_refused'] for s in sessions),
        'guard_refusals': sum(s['audit']['guard_refusals'] for s in sessions),
        'sessions_with_guard_refusals': [s['session'] for s in sessions if s['audit']['guard_refusals']],
        'sessions_that_evaluated_a_candidate': len(withc), 'full_candidate_evaluations': sum(s['evaluations']['full_candidate_evaluations'] for s in sessions),
        'partial_probes': sum(s['evaluations']['partial_probes'] for s in sessions),
        'eligible_candidates': sum(c['eligible'] is True for c in cand), 'candidates_with_scores': len(cand),
        'recorded_scores_reproduced_from_rows': sum(c['recomputed_matches_recorded'] is True for c in cand),
        'every_evaluated_candidate_below_unchanged_app': sum(c['calibration_score_recorded'] < c['unchanged_app_score'] for c in cand),
        'sessions_with_two_or_more_candidates': len(multi),
        'error_fell_first_to_last': [s['session'] for s in multi if s['calibration']['last_vs_first']['error_fell']],
        'error_rose_first_to_last': [s['session'] for s in multi if not s['calibration']['last_vs_first']['error_fell']],
        'source_changed_between_first_and_last_candidate': sum(s['calibration']['last_vs_first']['source_changed_between_candidates'] for s in multi),
        'last_candidate_below_unchanged_app_sessions': [sum(s['calibration']['last_vs_unchanged_app']['last_candidate_below_unchanged_app'] for s in withc), len(withc)],
        'median_unchanged_over_last_candidate_ratio': {'web_panel': [statistics.median(ratios('web_panel')), len(ratios('web_panel'))],
                                                        'game': [statistics.median(ratios('game')), len(ratios('game'))]},
        'activity_between_first_result_and_next_full_evaluation': {
            'sessions': len(windows), 'sessions_with_at_least_one_script_or_file_write': sum(s['agent_activity']['to_next_full_evaluation']['scripts_and_file_writes'] > 0 for s in windows),
            'scripts_and_file_writes_min_max': [min(s['agent_activity']['to_next_full_evaluation']['scripts_and_file_writes'] for s in windows),
                                                max(s['agent_activity']['to_next_full_evaluation']['scripts_and_file_writes'] for s in windows)]},
        'sessions_with_heldout_scores': len(held), 'heldout_cases_passed': [sum(s['finalize']['heldout']['cases_passed'] for s in held), sum(s['finalize']['heldout']['cases_total'] for s in held)],
        'heldout_by_role_passed_total': roles,
        'heldout_candidate_below_unchanged_app_sessions': [sum(s['finalize']['heldout']['candidate_score'] < s['finalize']['heldout']['unchanged_app_score'] for s in held), len(held)],
        'heldout_cases_candidate_better_than_unchanged_app': [sum(s['finalize']['heldout']['cases_candidate_better'] for s in held), sum(s['finalize']['heldout']['cases_total'] for s in held)],
        'sessions_with_all_heldout_cases_passed': [s['session'] for s in held if s['finalize']['heldout']['cases_passed'] == s['finalize']['heldout']['cases_total']],
        'finalize_attempts': sum(len(s['finalize']['attempts']) for s in sessions),
        'verdicts': dict(sorted(Counter(s['verdict']['category'] for s in sessions).items())),
        'verdict_sessions': {cat: [s['session'] for s in sessions if s['verdict']['category'] == cat] for cat in sorted({s['verdict']['category'] for s in sessions})},
        'verdict_agrees_with_session_files': sum(s['verdict']['session_files_agree'] for s in sessions),
        'native_audit_per_logs': dict(sorted(Counter(s['native_audit_per_logs']['status'] for s in sessions).items())),
        'sessions_in_pinned_native_audit_range': sum(s['agent']['in_pinned_native_audit_range'] for s in sessions),
        'sessions_with_exactly_one_task_prompt_and_no_other_client_message': [
            sum(s['agent']['task_prompts_sent'] == 1 and set(s['agent']['other_client_messages']) <= set(SETUP_METHODS) for s in sessions), len(sessions)],
        'sessions_with_passive_activity_record': [sum(s['agent']['upstream_pause_notifications'] is not None for s in sessions), len(sessions)],
        'passive_record_no_retry_no_resume_no_deadline_extension_no_pause': sum(
            s['agent']['upstream_pause_notifications'] == 0 and not s['agent']['automatic_resume_sent'] and not s['agent']['prompt_retried'] and not s['agent']['deadline_extended']
            for s in sessions if s['agent']['upstream_pause_notifications'] is not None),
        'sessions_whose_measurement_records_report_opencv_5': [sum(s['opencv']['every_record_reports_opencv_5'] is True for s in sessions), len(sessions)],
        'sessions_without_measurement_records': [s['session'] for s in sessions if s['opencv']['every_record_reports_opencv_5'] is None],
        'heldout_job_scores_reproduced_from_rows': [sum(s['finalize']['heldout']['recomputed_job_score_matches'] is True for s in held), len(held)],
    }


# --------------------------------------------------------------------------- report

def f4(x):
    return 'n/a' if x is None else '%.4f' % x


def show(value, decimals):
    if isinstance(value, float):
        return ('%.' + str(decimals) + 'f') % value if decimals is not None else '%.6g' % value
    return json.dumps(value) if isinstance(value, list) else str(value)


def hhmm(t):
    return 'n/a' if t is None else t[:10]+' '+t[11:16]


def render(result):
    S = {s['session']: s for s in result['sessions']}
    T = result['totals']
    inv = result['inventory']
    flagged = lambda sid: (S[sid]['finalize']['heldout']['failed_cases_by_signature'].get('required_order_unobservable', 0), len(S[sid]['finalize']['heldout']['failed_cases']))
    v6w = S['v6p01']['finalize']['heldout']['failed_cases'][0]['failing_windows']
    ok = sum(c['agree'] for c in result['cross_checks'])
    lt, hc, act, med = (T['last_candidate_below_unchanged_app_sessions'], T['heldout_cases_passed'], T['activity_between_first_result_and_next_full_evaluation'],
                        T['median_unchanged_over_last_candidate_ratio'])
    out = ['# Agent sessions: post-hoc evidence table', '',
           'Read-only, post-hoc extraction from the %d logged Kiro CLI sessions: every folder under `%s` that has a launcher record (`kiro/launch.json`). The other %d folders '
           'there hold recordings, preparation or measurements without a model run and have no launcher record, so no model prompt was launched from them. No measurement was repeated and no model was run. Every number below is read '
           'from the recorded session files or recomputed from the recorded rows; a value that is not recorded is shown as n/a and `result.json` says why (`null_reasons`). '
           'Source files and their SHA-256 are listed per session in `result.json`. Times are UTC.' % (
               T['sessions_logged'], inv['sessions_root'], len(inv['without_tool_audit'])), '',
           '## Reading guide', '',
           '- "Error" is the recorded calibration score of a candidate (lower is better): the mean over cases of the case error, where a case error is the mean over its windows '
           '(panel) or the single recorded case error (game). The unchanged app is the `before` side of the same rows on the same formula. The recomputed scores reproduce the '
           'recorded ones to 1e-12 for all %d scored candidates (%d of %d match) and for %d of %d held-out job scores.' % (
               T['candidates_with_scores'], T['recorded_scores_reproduced_from_rows'], T['candidates_with_scores'],
               T['heldout_job_scores_reproduced_from_rows'][0], T['heldout_job_scores_reproduced_from_rows'][1]),
           '- Panel errors are in object-scale units (window position RMSE divided by the recorded object scale), game errors in body heights; they are not comparable across domains.',
           '- Calibration cases are the feedback the agent sees while it works. Held-out cases (known regression and unseen validation) are measured only after the candidate is sealed.',
           '']

    out += ['## 1. Headline numbers', '']
    out += [
        '- Sessions logged: %d (%s); first tool calls from %s to %s. Model / effort: %s. Kiro CLI versions: %s.' % (
            T['sessions_logged'], ', '.join('%s %d' % (k, n) for k, n in T['sessions_by_version'].items()), T['first_tool_call_utc_range'][0],
            T['first_tool_call_utc_range'][1], ', '.join(T['models']), ', '.join('%s x%d' % (k, n) for k, n in T['kiro_cli_versions'].items())),
        '- Domains: %s. No logged Kiro session covers the SVG domain.' % ', '.join('%s x%d' % (k, n) for k, n in T['sessions_by_domain'].items()),
        '- Audited tool calls: %d, of which %d are KiroClops MCP calls and %d are the agent\'s own scripts or file writes. Guard refusals: %d (in %s).' % (
            T['audited_tool_calls'], T['mcp_calls'], T['scripts_and_file_writes'], T['guard_refusals'], ', '.join(T['sessions_with_guard_refusals'])),
        '- In %d of %d sessions the client sent exactly one task prompt and no other message after the protocol set-up (no follow-up, resume or cancel message). Among the %d sessions whose launcher '
        'recorded passive activity, %d had no upstream pause, no automatic resume, no prompt retry and no deadline extension.' % (
            T['sessions_with_exactly_one_task_prompt_and_no_other_client_message'][0], T['sessions_with_exactly_one_task_prompt_and_no_other_client_message'][1],
            T['sessions_with_passive_activity_record'][0], T['passive_record_no_retry_no_resume_no_deadline_extension_no_pause']),
        '- %d of %d sessions evaluated at least one candidate: %d full evaluations (%d of the %d scored candidates were eligible) and %d partial probe.' % (
            T['sessions_that_evaluated_a_candidate'], T['sessions_logged'], T['full_candidate_evaluations'], T['eligible_candidates'], T['candidates_with_scores'], T['partial_probes']),
        '- Candidate error fell from the first to the last candidate in %d of %d sessions with two or more candidates (%s); it rose in %d (%s).' % (
            len(T['error_fell_first_to_last']), T['sessions_with_two_or_more_candidates'], ', '.join(T['error_fell_first_to_last']),
            len(T['error_rose_first_to_last']), ', '.join(T['error_rose_first_to_last'])),
        '- In all %d of those sessions the candidate source differs between the first and the last candidate.' % T['source_changed_between_first_and_last_candidate'],
        '- The last candidate beat the unchanged app on calibration error in %d of %d sessions with a candidate; so did every one of the %d evaluated candidates (%d of %d).' % (
            lt[0], lt[1], T['candidates_with_scores'], T['every_evaluated_candidate_below_unchanged_app'], T['candidates_with_scores']),
        '- Median factor by which the last candidate\'s calibration error is below the unchanged app\'s: web panel %.1fx (n=%d), game %.1fx (n=%d). '
        'The game factor is large because the unchanged app lacks the recorded behaviour; the two domains must not be pooled.' % (med['web_panel'][0], med['web_panel'][1], med['game'][0], med['game'][1]),
        '- Between the first candidate result and the next full evaluation the agent made at least one script or file-write call in %d of %d sessions (range %d to %d calls).' % (
            act['sessions_with_at_least_one_script_or_file_write'], act['sessions'], act['scripts_and_file_writes_min_max'][0], act['scripts_and_file_writes_min_max'][1]),
        '- %d sessions reached sealed held-out scoring: %d of %d held-out cases passed (%s). The candidate\'s mean held-out error was below the unchanged app\'s in %d of the %d '
        'sessions and on %d of %d held-out cases.' % (
            T['sessions_with_heldout_scores'], hc[0], hc[1], '; '.join('%s %d/%d' % (r, v[0], v[1]) for r, v in T['heldout_by_role_passed_total'].items()),
            T['heldout_candidate_below_unchanged_app_sessions'][0], T['heldout_candidate_below_unchanged_app_sessions'][1],
            T['heldout_cases_candidate_better_than_unchanged_app'][0], T['heldout_cases_candidate_better_than_unchanged_app'][1]),
        '- All held-out cases passed in %d of those %d sessions (%s).' % (len(T['sessions_with_all_heldout_cases_passed']), T['sessions_with_heldout_scores'],
                                                                          ', '.join(T['sessions_with_all_heldout_cases_passed'])),
        '- Recorded verdicts: %s. Sessions: %s.' % (', '.join('%s %d' % (k, n) for k, n in T['verdicts'].items()),
                                                      '; '.join('%s: %s' % (k, ', '.join(v)) for k, v in T['verdict_sessions'].items())),
        '- Native tool-audit certification as recorded in the logs: %s. %d of %d sessions ran on kiro-cli 2.27.x, the only range the pinned audit reader covers.' % (
            ', '.join('%s %d' % (k, n) for k, n in T['native_audit_per_logs'].items()), T['sessions_in_pinned_native_audit_range'], T['sessions_logged']),
        '- Every measurement record that carries an `opencv_version` field reports OpenCV 5.x (5.0.0; v6p01 also records package 5.0.0.93) in %d of %d sessions; %s has no measurement record.' % (
            T['sessions_whose_measurement_records_report_opencv_5'][0], T['sessions_whose_measurement_records_report_opencv_5'][1], ', '.join(T['sessions_without_measurement_records'])),
        '']

    out += ['## 2. Sessions and agent tooling', '',
            '| Session | Ver. | Domain | First tool call (UTC) | Kiro CLI | Audited calls (MCP) | Refused | Full evaluations + probes | Scripts / file writes |',
            '|---|---|---|---|---|---|---|---|---|']
    for s in result['sessions']:
        ev = s['evaluations']
        out.append('| %s | %s | %s (%s) | %s | %s | %d (%d) | %d | %d + %d | %d |' % (
            s['session'], s['version'], s['domain'], s['mechanic'], hhmm(s['timeline']['first_tool_call_utc']), s['agent']['kiro_cli_version'],
            s['audit']['rows'], s['audit']['mcp_calls'], s['audit']['guard_refusals'], ev['full_candidate_evaluations'], ev['partial_probes'],
            s['audit']['scripts_and_file_writes']))
    used = [n for n, c in T['mcp_calls_by_name'].items() if c]
    never = [n for n, c in T['mcp_calls_by_name'].items() if not c]
    refusals = [(s['session'], r) for s in result['sessions'] for r in s['audit']['refusal_details']]
    out += ['', 'Model / effort as recorded in each `kiro/launch.json`: %s; approval mode: %s. KiroClops MCP tools called at least once: %s; never called in any session: %s. '
            '"Audited calls" are the rows of `kiro/tool-audit.jsonl` (one per tool call, including one `tool_load` row). "Scripts / file writes" counts execute_bash, execute_pwsh, '
            'fs_write and str_replace calls. Refusals are guard decisions with `allow` false: %s.' % (
                ', '.join(T['models']), ', '.join(sorted({s['agent']['approval_mode'] for s in result['sessions']})), ', '.join(used), ', '.join(never),
                '; '.join('%s +%.0f s %s (%s)' % (sid, r['offset_seconds'], r['tool'], REFUSAL_TEXT[r['kind']]) for sid, r in refusals)), '']

    out += ['## 3. Calibration evolution (what the agent saw)', '',
            '| Session | Candidates in order: error (eligible; cases passed) | Unchanged app | First to last | Last vs unchanged app |',
            '|---|---|---|---|---|']
    for s in result['sessions']:
        if not s['candidates']:
            out.append('| %s | none evaluated | n/a | n/a | n/a |' % s['session'])
            continue
        parts = ['%s (%s; %d/%d)' % (f4(c['calibration_score_recorded']), 'eligible' if c['eligible'] else 'not eligible', c['cases_passed'], c['cases_total'])
                 for c in s['candidates']]
        lf = s['calibration']['last_vs_first']
        lu = s['calibration']['last_vs_unchanged_app']
        out.append('| %s | %s | %s | %s | %s |' % (
            s['session'], ' -> '.join(parts), f4(s['calibration']['unchanged_app_score']),
            'single candidate' if not lf else '%s (%+.1f%%)' % ('fell' if lf['error_fell'] else 'rose', 100*lf['relative_change']),
            '%.1fx lower (better on %d/%d cases)' % (lu['ratio_unchanged_over_last_candidate'], lu['cases_candidate_better'], lu['cases'])))
    out += ['']

    out += ['## 4. Descriptive: what the agent did after it saw the first candidate result', '',
            'Counts of audited calls strictly between the `finished_at` of the first full evaluation and the next full evaluation call. This is a descriptive signal that the agent kept '
            'working after the feedback arrived; it does not show that the feedback caused the change. The last column compares the two candidates window by window (panel) or case by case (game).', '',
            '| Session | Window (s) | Scripts + file writes in it | Shell scripts / file writes | Candidate source changed | Windows or cases improved / worsened / equal, first to last candidate |',
            '|---|---|---|---|---|---|']
    for s in result['sessions']:
        a = s['agent_activity']['to_next_full_evaluation']
        ch = s['calibration'].get('changes_first_to_last')
        lf = s['calibration'].get('last_vs_first')
        if a:
            out.append('| %s | %.0f | %d | %d / %d | %s | %d / %d / %d of %d %s |' % (
                s['session'], a['window_seconds'], a['scripts_and_file_writes'], a['shell_script_calls'], a['file_write_calls'],
                'yes' if lf['source_changed_between_candidates'] else 'no', ch['improved'], ch['worsened'], ch['unchanged'], ch['compared'], ch['unit']))
        else:
            fin = s['agent_activity']['to_finalize_call']
            out.append('| %s | no second full evaluation | n/a (until the finalize call: %s) | n/a | n/a | n/a |' % (
                s['session'], 'n/a' if not fin else str(fin['scripts_and_file_writes'])))
    out += ['']

    out += ['## 5. Sealed held-out outcome and recorded verdict', '',
            '| Session | Held-out passed / total | Held-out error: candidate vs unchanged app | Failure signatures | Recorded verdict | Native audit per logs |',
            '|---|---|---|---|---|---|']
    for s in result['sessions']:
        h = s['finalize']['heldout']
        v = s['verdict']
        if h:
            roles = '; '.join('%s %d/%d' % (r, x['passed'], x['total']) for r, x in h['by_role'].items())
            sig = ', '.join('%s x%d' % (k, n) for k, n in h['failed_cases_by_signature'].items()) or 'none'
            row = '%d/%d (%s) | %s vs %s | %s' % (h['cases_passed'], h['cases_total'], roles, f4(h['candidate_score']), f4(h['unchanged_app_score']), sig)
        else:
            att = s['finalize']['attempts']
            row = 'n/a (%s) | n/a | n/a' % ('no finalize attempt' if not att else 'finalize %s: %s' % (att[-1]['status'], att[-1]['error_code']))
        out.append('| %s | %s | %s | %s |' % (s['session'], row, v['category'], s['native_audit_per_logs']['status']))
    out += ['', 'Size of the sealed candidate relative to the baseline (`report/candidate.diff`; written only for sessions that sealed a candidate and built a report):', '',
            '| Session | Files changed | Lines added | Lines removed | Files |', '|---|---|---|---|---|']
    for s in result['sessions']:
        d = s['finalize']['sealed_candidate_diff']
        out.append('| %s | %s |' % (s['session'], 'n/a | n/a | n/a | n/a' if not d else '%d | %d | %d | %s' % (
            d['files_changed'], d['lines_added'], d['lines_removed'], ', '.join(d['file_names']))))
    out += ['']

    out += ['## 6. Recorded verdicts and their sources', '']
    for s in result['sessions']:
        v = s['verdict']
        refs = ', '.join('`%s` %s' % (r['file'], r['lines']) for r in v['recorded_in'])
        out.append('- **%s** (%s): %s. %s Source: %s. Derived from the session files: %s (%s).' % (
            s['session'], s['version'], v['category'], v['detail'], refs, v['derived_from_session_files'], 'agrees' if v['session_files_agree'] else 'DISAGREES'))
    out += ['']

    out += ['## 7. Cross-checks against the results logs', '',
            '%d of %d checks agree. Each row compares a number or identifier written in a results log (transcribed by the script author, in English) with the value read from the '
            'session files. Log files are identified by path and line range only.' % (ok, len(result['cross_checks'])), '',
            '| Id | Ver. | Session | Quantity | Log value | File value | Agree | Log source |', '|---|---|---|---|---|---|---|---|']
    for c in result['cross_checks']:
        out.append('| %s | %s | %s | %s | %s | %s | %s | %s %s |' % (c['id'], c['version'], c['session'], c['quantity'], show(c['log_value'], c['compared_at_decimals']),
                                                                    show(c['file_value'], c['compared_at_decimals']+2 if c['compared_at_decimals'] is not None else 6),
                                                                    'yes' if c['agree'] else '**NO**', c['log_file'], c['log_lines']))
    consistency = result['consistency_with_v6_single_session_analysis']
    out += ['', 'Consistency with the single-session analysis (`scripts/v6_agent_baseline_posthoc.py`): %s' % consistency['summary'], '']

    out += ['## 8. Places where a log and the files use different definitions', '']
    for d in result['explained_differences']:
        out.append('- **%s** (%s; %s): the log says %s; the files say %s. %s' % (d['id'], d['session'], d['log'], d['log_says'], d['files_say'], d['explanation']))
    bad = [c for c in result['cross_checks'] if not c['agree']]
    if bad:
        out += ['', '%d of %d cross-checks agree. The following do not:' % (ok, len(result['cross_checks'])), '']
        for c in bad:
            out.append('- **%s** (%s, %s; %s %s): %s. The log says %s; the files say %s. %s' % (
                c['id'], c['version'], c['session'], c['log_file'], c['log_lines'], c['quantity'], show(c['log_value'], c['compared_at_decimals']),
                show(c['file_value'], c['compared_at_decimals']+2 if c['compared_at_decimals'] is not None else 6), c['note'] or ''))
        out += ['', 'Every other number in the logs that was checked agrees with the files.', '']
    else:
        out += ['', 'No numeric contradiction was found: all %d cross-checks agree.' % len(result['cross_checks']), '']

    out += ['## 9. Caveats', '',
            '- These are %d single runs, not a controlled experiment: no repeated runs per recipe, no control arm without feedback, no randomisation, one model setting. '
            'The unchanged app is a no-agent reference on the same rows, not a competing agent. Nothing here shows that the feedback caused the error reduction.' % T['sessions_logged'],
            '- Feedback formats and recipes changed between versions. Acceptance profiles differ (%s), calibration suites differ in size (%s), thresholds and gates were revised after '
            'failures, and V6 added window diagnostics (offset, lag, trajectories) and the `inspect_window` tool, which the agent never used. A failure under an older rule is not a '
            'failure under a later one.' % (
                ', '.join(sorted({s['experiment']['acceptance_profile'] for s in result['sessions'] if s['experiment']['acceptance_profile']})),
                '; '.join('%s: %s' % (d, ', '.join('%s %d' % (s['session'], s['experiment']['calibration_cases_in_suite']) for s in result['sessions'] if s['domain'] == d))
                          for d in sorted({s['domain'] for s in result['sessions']}))),
            '- Several recorded failures (held-out or calibration) are recipe or gate faults according to the logs, not agent behaviour: required-event-order cases that are unobservable even in '
            'the reference (v3p01: %d of %d failures; v4p01: %d of %d), a speed gate that looked at a short frame window (v3g02: all %d failures) and a 50%% improvement gate near the '
            'measurement floor (v2g01, which ended with no eligible candidate). The single v6p01 failure carries the same `required_order_unobservable` label, but its failing '
            'post-release windows show candidate errors of %.2f-%.2f against %.3f-%.3f for the unchanged app, so it is counted as a failure of the candidate.' % (
                flagged('v3p01')[0], flagged('v3p01')[1], flagged('v4p01')[0], flagged('v4p01')[1], flagged('v3g02')[1],
                min(w['candidate'] for w in v6w), max(w['candidate'] for w in v6w), min(w['unchanged_app'] for w in v6w), max(w['unchanged_app'] for w in v6w)),
            '- Some runs did not end on the agent\'s own work: v2j03 (harness callback error at finalize), v5p02c (capture timing at finalize while an operating-system update was '
            'installing) and gj04 (operator interruption). gj05 reached its request deadline with zero candidates and the log leaves the cause undetermined. These rows keep their '
            'recorded verdicts; they are neither successes nor, except for gj05\'s missing candidate, evidence about the agent.',
            '- Native tool-audit certification covers kiro-cli 2.27.x only. v6p01 ran on 2.31.0 and is native-uncertified. Among the 2.27.x sessions the logs record clean passes for %s, '
            're-audit passes after reader updates (the earlier failure is retained) for %s, failures for %s, and no certification for the interrupted gj04 and gj05.' % (
                ', '.join(s['session'] for s in result['sessions'] if s['native_audit_per_logs']['status'] == 'pass'),
                ', '.join(s['session'] for s in result['sessions'] if s['native_audit_per_logs']['status'] == 'reaudit_pass'),
                ', '.join(s['session'] for s in result['sessions'] if s['native_audit_per_logs']['status'] == 'fail')),
            '- Only %d of %d sessions (%s) carries an accepted verdict, and that rests on a separate domain closure with independent reviews, not on the session report. '
            'The accepted count is not a success rate of the agent.' % (T['verdicts'].get('accepted', 0), T['sessions_logged'], ', '.join(T['verdict_sessions'].get('accepted', []))),
            '- Held-out "cases" are not independent: game repeats are deterministic copies of a scenario and panel repeats are separate recordings of the same recipe, so case counts overstate '
            'the number of independent trials.',
            '- Game and panel errors use different units and different row formats (per-case for games, per-window for panels); averaging or ranking across domains is not meaningful.',
            '- Scope: the table covers the %d sessions under the sessions root that have a tool-audit log; within them no session was left out (the %d other folders have no launcher record). '
            'Kiro runs of earlier phases are stored elsewhere under `artifacts/` and are not part of it; for example PHASE3A_STATUS.md (L19-L20) records the Phase 3A original cohort of 18 runs as 3 '
            'completed and 15 failed under the rules of that phase. This scope is a storage and format boundary, not a performance filter.' % (T['sessions_logged'], len(inv['without_tool_audit'])),
            '- The audit file records hook decisions; the launcher event counts and the application-call counts quoted in some logs are different quantities (section 8). %d audit files contain rows '
            'up to %.2f s out of order (parallel tool calls), which does not affect the windows used here.' % (
                sum(not s['audit']['rows_in_time_order'] for s in result['sessions']), max(s['audit']['largest_backward_step_seconds'] for s in result['sessions'])),
            '- Dates are UTC from the audit timestamps; the logs use local time (UTC+3).',
            '- "Scripts and file writes" counts the tools execute_bash, execute_pwsh, fs_write and str_replace as attempted calls (%d of the %d were refused by the guard); no other equivalent tool names occur '
            'in these audit files (the refused delete_file call is not counted). The windows start at the `finished_at` of the first full evaluation, so they show activity, not intent.' % (
                T['scripts_and_file_writes_refused'], T['scripts_and_file_writes']),
            '- OpenCV version: every measurement record carries its own `opencv_version`; the table reports what the records say and does not re-run any measurement.', '']

    out += ['## 10. Files read besides the session folders', '',
            'Results logs (read for verdicts and cross-checks; identified here by path and SHA-256 only) and the single-session analysis used for the consistency check:', '']
    for item in result['log_sources']:
        path = Path(item['path'])
        out.append('- `%s` %s' % (path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix(), item['sha256']))
    closure = S['v4g01']['verdict']['closure']
    out += ['- `%s/closure.json` %s' % (closure['folder'], closure['closure_sha256']), '- `%s/receipt.json` %s' % (closure['folder'], closure['receipt_sha256']), '']
    return '\n'.join(out)


# --------------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f'Keep prior evidence; choose a new folder: {args.output}')

    folders = sorted(p for p in SESSIONS_ROOT.iterdir() if p.is_dir())
    audited = sorted(p.name for p in folders if (p/'kiro/tool-audit.jsonl').exists())
    if audited != sorted(SESSIONS):
        raise SystemExit(f'Expected audited sessions {sorted(SESSIONS)}, found {audited}')
    inventory = {'sessions_root': SESSIONS_ROOT.as_posix(), 'folders': len(folders), 'with_tool_audit': audited,
                 'without_tool_audit': [p.name for p in folders if p.name not in audited],
                 'without_tool_audit_but_with_launcher_record': [p.name for p in folders if p.name not in audited and (p/'kiro/launch.json').exists()]}
    if inventory['without_tool_audit_but_with_launcher_record']:
        raise SystemExit(f"Sessions with a launcher record but no tool audit: {inventory['without_tool_audit_but_with_launcher_record']}")

    log_src = Sources()
    logs = {}
    for name in LOG_FILES:
        logs[name] = log_src.note(ROOT/name, 'results log (read for verdicts and cross-checks only)')

    S = {}
    for sid in SESSIONS:
        src = Sources()
        S[sid] = analyse(sid, src, logs)
        S[sid]['sources'] = src.listing()
        report = SESSIONS_ROOT/sid/'report/report.json'
        S[sid]['report_sha256'] = src.files[report.as_posix()]['sha256'] if report.as_posix() in src.files else None
    cross = checks(S, logs)
    for s in S.values():
        s.pop('report_sha256')

    baseline = json.loads(V6_BASELINE.read_bytes().decode('utf-8')) if V6_BASELINE.exists() else None
    consistency = {'file': V6_BASELINE.as_posix(), 'sha256': log_src.note(V6_BASELINE, 'single-session analysis (consistency check)') if baseline else None}
    if baseline:
        jobs = baseline['jobs']
        v6 = S['v6p01']
        pairs = [('candidate-01 error', jobs['candidate-01 (calibration)']['summary']['candidate_score'], v6['candidates'][0]['calibration_score_recorded']),
                 ('candidate-02 error', jobs['candidate-02 (calibration)']['summary']['candidate_score'], v6['candidates'][1]['calibration_score_recorded']),
                 ('unchanged app (calibration)', jobs['candidate-01 (calibration)']['summary']['unchanged_app_score'], v6['calibration']['unchanged_app_score']),
                 ('held-out candidate error', jobs['candidate-02 (held-out, after sealing)']['summary']['candidate_score'], v6['finalize']['heldout']['candidate_score']),
                 ('held-out unchanged app', jobs['candidate-02 (held-out, after sealing)']['summary']['unchanged_app_score'], v6['finalize']['heldout']['unchanged_app_score'])]
        for role in ('regression', 'validation'):
            ref = jobs['candidate-02 (held-out, after sealing)']['by_role'][role]
            mine = v6['finalize']['heldout']['summary_by_role'][role]
            pairs += [('held-out %s candidate error' % role, ref['candidate_score'], mine['candidate_score']),
                      ('held-out %s unchanged app' % role, ref['unchanged_app_score'], mine['unchanged_app_score'])]
        consistency['comparisons'] = [{'quantity': q, 'single_session_script': a, 'this_script': b, 'identical': abs(a-b) < EPS} for q, a, b in pairs]
        consistency['all_identical'] = all(c['identical'] for c in consistency['comparisons'])
        consistency['summary'] = ('%d of %d v6p01 quantities are identical to the output of that script (unchanged app %.4f, candidate-01 %.4f, candidate-02 %.4f, '
                                  'held-out %.4f vs %.4f).' % (sum(c['identical'] for c in consistency['comparisons']), len(consistency['comparisons']),
                                                                v6['calibration']['unchanged_app_score'], v6['candidates'][0]['calibration_score_recorded'],
                                                                v6['candidates'][1]['calibration_score_recorded'], v6['finalize']['heldout']['candidate_score'],
                                                                v6['finalize']['heldout']['unchanged_app_score']))
    else:
        consistency.update(all_identical=None, summary='the single-session analysis result was not found, so no comparison was made.')

    result = {'kind': 'AgentSessionsPosthoc', 'schema_version': 1,
              'label': 'post-hoc, read-only analysis of recorded Kiro CLI sessions; no measurement was repeated and no model was run',
              'sessions_root': SESSIONS_ROOT.as_posix(), 'inventory': inventory, 'sessions': [S[sid] for sid in SESSIONS], 'totals': totals(S),
              'cross_checks': cross, 'cross_checks_agree': sum(c['agree'] for c in cross), 'explained_differences': explained_differences(S),
              'consistency_with_v6_single_session_analysis': consistency,
              'definitions': {
                  'calibration_score': 'recorded job score = mean over cases of the case error; case error = mean over windows (panel) or the single recorded case error (game)',
                  'unchanged_app_score': 'the same formula applied to the before side of the same rows',
                  'scripts_and_file_writes': list(WORK_TOOLS), 'mcp_prefix': MCP_PREFIX,
                  'agent_activity_window': 'audited calls strictly between the finished_at of the first full evaluation and the next call of the stated kind',
                  'verdict_categories': ['accepted', 'failed', 'infrastructure_failure', 'not_finalized'],
                  'version_label': 'taken from the session id prefix (g/j/p = game/jump/panel runs before V2 are V1; v2..v6 = the epoch of the run; v5p02c is the V5b panel run) and the results logs',
                  'dates': 'UTC, from the audit timestamps (the results logs use local time, UTC+3)',
                  'null_semantics': 'null = not recorded, not applicable, or no error. Fields that are missing carry a reason under null_reasons of their session. Error fields '
                                    '(error_code, error_message, session_final.error) are null when the operation succeeded, source_changed_since_previous_candidate is null for a first '
                                    'candidate, and the note field of a cross-check is null when there is nothing to add; these carry no separate reason.'},
              'log_sources': log_src.listing()}
    args.output.mkdir(parents=True)
    with open(args.output/'result.json', 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(result, indent=1)+'\n')
    with open(args.output/'REPORT.md', 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(render(result)+'\n')

    for s in result['sessions']:
        cals = ' -> '.join(f4(c['calibration_score_recorded']) for c in s['candidates']) or 'no candidate'
        h = s['finalize']['heldout']
        print(f"{s['session']}: {s['audit']['rows']} calls ({s['audit']['mcp_calls']} MCP, {s['audit']['guard_refusals']} refused), "
              f"{s['evaluations']['full_candidate_evaluations']}+{s['evaluations']['partial_probes']} evaluations, error {cals} "
              f"(unchanged app {f4(s['calibration'].get('unchanged_app_score'))}), "
              f"held-out {'n/a' if not h else '%d/%d' % (h['cases_passed'], h['cases_total'])}, verdict {s['verdict']['category']}")
    t = result['totals']
    print(f"{t['sessions_logged']} sessions, {t['audited_tool_calls']} audited calls, {t['full_candidate_evaluations']} full evaluations, "
          f"error fell first->last in {len(t['error_fell_first_to_last'])}/{t['sessions_with_two_or_more_candidates']}, "
          f"held-out {t['heldout_cases_passed'][0]}/{t['heldout_cases_passed'][1]}; cross-checks agree {result['cross_checks_agree']}/{len(cross)}")


if __name__ == '__main__':
    main()
