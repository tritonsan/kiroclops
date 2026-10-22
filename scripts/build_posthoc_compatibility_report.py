"""Read frozen trials with explicitly staged native/browser compatibility fixes.

Run AFTER the primary portable report and independent reviews, BEFORE applying
production patches. No model, capture, candidate edit or primary outcome rewrite.
This supplementary interpretation never grants a completed cohort or replaces a
failed trial. It reuses captured numerical results, not newly optimized scores.
"""
from contextlib import ExitStack
from pathlib import Path
import argparse
import json
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256, VisualError

TERMINAL_ROWS = {'completed', 'failed', 'interrupted'}
TERMINAL_STAGES = TERMINAL_ROWS | {'not_run_predecessor_failed'}


def require_terminal(registry, progress, primary, registration_sha, progress_sha):
    if (progress.get('registration_sha256') != registration_sha
            or primary.get('registration_sha256') != registration_sha
            or primary.get('progress_sha256') != progress_sha):
        raise VisualError('wrong_report', 'Primary report and progress do not bind to this registration')
    rows = progress.get('rows', [])
    expected = registry.get('rows', [])
    identities = lambda values: [(r.get('id'), r.get('domain'), r.get('mode'), r.get('repeat'),
                                 [s.get('stage') for s in r.get('stages', [])]) for r in values]
    if len(rows) != 18 or identities(rows) != identities(expected) or identities(primary.get('rows', [])) != identities(rows):
        raise VisualError('wrong_report', 'All18 original trial identities and stages are required')
    statuses = lambda values: [(r.get('status'), [s.get('status') for s in r.get('stages', [])]) for r in values]
    if statuses(primary['rows']) != statuses(rows):
        raise VisualError('wrong_report', 'Primary report changed the recorded outcomes')
    if any(r.get('status') not in TERMINAL_ROWS or not r.get('stages')
           or any(s.get('status') not in TERMINAL_STAGES for s in r['stages']) for r in rows):
        raise VisualError('cohort_running', 'Finish every registered stage before compatibility replay')
    if progress.get('continuation_blocked'):
        raise VisualError('cohort_blocked', 'Resolve owned process cleanup before compatibility replay')


def unchanged(hashes):
    if any(file_sha256(p) != sha for p, sha in hashes.items()):
        raise VisualError('evidence_changed', 'Bound report, proposal or frozen source changed')


def run(registration, primary_report, output):
    registration, primary_report, output = map(lambda p: Path(p).resolve(),
                                               (registration, primary_report, output))
    if output.exists():
        raise VisualError('output_exists', 'Use a new supplementary report destination')
    progress_path = registration.with_name('progress.json')
    primary_path = primary_report/'cohort-report.json'
    primary_manifest = primary_report/'package-manifest.json'
    registry, progress, primary = map(read_json, (registration, progress_path, primary_path))
    inputs = {str(p): file_sha256(p) for p in (registration, progress_path, primary_path, primary_manifest)}
    require_terminal(registry, progress, primary, inputs[str(registration)], inputs[str(progress_path)])
    manifest = read_json(primary_manifest)
    if manifest.get('files', {}).get('cohort-report.json') != inputs[str(primary_path)]:
        raise VisualError('wrong_report', 'Primary package does not bind its report data')
    from kiroclops.local_engine import cohort, evidence, review, acceptance
    from stage_native_completion_repair import patched_reader
    from stage_browser_evidence_policy_fix import staged_function
    from build_local_cohort_report import replay_stage_acceptance, terminal_validation_diagnostic, portable
    if cohort.protocol_sources() != registry['source_hashes']:
        raise VisualError('source_changed', 'Replay this compatibility interpretation before production changes')
    proposals = ROOT/'artifacts/phase3a'
    native = proposals/'native-completion-compatibility-01'
    browser = proposals/'browser-evidence-policy-compatibility-01'
    a, combined, b = (read_json(native/'result.json'), read_json(native/'combined-regression-02.json'),
                       read_json(browser/'result.json'))
    if any(v.get('status') != 'pass' or v.get('failures') != 0 or v.get('errors') != 0 for v in (a, combined, b)):
        raise VisualError('unverified_proposal', 'All staged compatibility regression suites must pass')
    expected = {
        Path(review.__file__): a['production_review_sha256'],
        Path(acceptance.__file__): a['production_acceptance_sha256'],
        Path(evidence.__file__): b['production_evidence_sha256'],
        native/'review-proposed.py': a['proposed_review_sha256'],
        native/'acceptance-proposed.py': a['proposed_acceptance_sha256'],
        browser/'evidence-proposed.py': b['proposed_evidence_sha256'],
        ROOT/'scripts/stage_native_completion_repair.py': combined['runtime_adapter_sha256'],
        ROOT/'scripts/stage_browser_evidence_policy_fix.py': b['proposal_builder_sha256'],
    }
    unchanged(expected)
    inputs.update({str(p): sha for p, sha in expected.items()})
    for p in (native/'result.json', native/'combined-regression-02.json', browser/'result.json', Path(__file__)):
        inputs[str(p)] = file_sha256(p)
    rows = []
    with ExitStack() as stack:
        stack.enter_context(patched_reader(native))
        stack.enter_context(patch.object(evidence, 'required_observation_bindings', staged_function(browser/'evidence-proposed.py')))
        for original in progress['rows']:
            row = {k: original[k] for k in ('id', 'domain', 'mode', 'repeat')}
            row['primary_status'] = original['status']
            row['stages'] = []
            for stage in original['stages']:
                item = {'stage': stage['stage'], 'primary_status': stage['status']}
                session = Path(stage['session'])
                if stage['status'] == 'not_run_predecessor_failed' or not (session/'session.json').exists():
                    item.update(status='not_observed', reason='Original unrun stage retained; no synthetic continuation')
                else:
                    state = read_json(session/'session.json')
                    if state.get('active_job_id'):
                        raise VisualError('cohort_running', 'A terminal session still owns active work')
                    selected = next((c for c in state.get('candidates', [])
                                     if c['candidate_id'] == state.get('selected_candidate_id')), {})
                    item['candidate_sha256'] = selected.get('source_sha256')
                    item['compatibility_replay'] = replay_stage_acceptance(session, state, selected)
                    item['terminal_validation'] = terminal_validation_diagnostic(session, state, selected)
                row['stages'].append(item)
            rows.append(row)
            print(original['id'], 'replayed; primary status retained', flush=True)
    unchanged(inputs)
    if cohort.protocol_sources() != registry['source_hashes']:
        raise VisualError('source_changed', 'Production changed during supplementary replay')
    value = {'kind': 'ExplicitPosthocCompatibilityInterpretation', 'status': 'completed',
        'registration_sha256': inputs[str(registration)], 'progress_sha256': inputs[str(progress_path)],
        'primary_report_sha256': inputs[str(primary_path)], 'rows': rows,
        'interpretation': 'Staged native lifecycle/denied-call compatibility and registered browser-policy reader only. Captured numerical scores and original trial outcomes are retained. No new model/capture, no failed-trial replacement, no unrun game continuation, no population benefit claim.',
        'timing_rounding_repair_applied_here': False, 'static_capture_repair_applied_here': False,
        'production_modified': False, 'primary_outcomes_modified': False,
        'inputs': inputs, 'tests': {'combined_native': combined['tests'], 'browser_policy': b['tests']}}
    output.mkdir(parents=True)
    write_json(output/'result.json', value)
    lines = ['# KiroClops — separate compatibility review', '',
             'This report does not change the old trial results. The numerical measurement, model record, source and independent image review are shown separately. Stages that were not run are not counted as completed.', '',
             '| Run / stage | Old result | Numerical | Native | Source | Image | Coverage | Separate review |',
             '|---|---|---|---|---|---|---|---|']
    for row in rows:
        for stage in row['stages']:
            replay = stage.get('compatibility_replay', {})
            numeric = replay.get('captured_numerical_pass')
            cells = [row['id']+'/'+stage['stage'], stage['primary_status'],
                     'pass' if numeric is True else 'fail' if numeric is False else 'no measurement',
                     *[replay.get(k, 'not done') for k in ('native_run_status', 'source_review_status',
                         'independent_review_status', 'review_coverage_status', 'overall_acceptance')]]
            lines.append('| '+' | '.join(cells)+' |')
    lines += ['', 'Timing rounding and recorder changes were not applied here. The missing motion results of failed recordings were not estimated.', '',
              '[Linked result data](result.json)', '']
    (output/'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
    write_json(output/'portable-summary.json', portable({k: v for k, v in value.items() if k != 'inputs'}))
    print({'status': 'completed', 'trials': len(rows), 'primary_outcomes_modified': False})


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registration', type=Path, required=True)
    p.add_argument('--primary-report', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    run(args.registration, args.primary_report, args.output)
