"""Bind the actually inspected four files and43 native calls to selected code.

Does not modify the candidate, final scores or original trial status. A failed
recording has no complete source/application verdict and stays incomplete.
"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine import review


def run():
    base = ROOT/'artifacts/phase3a'
    session = base/'completion-paired-cohort-01/sessions/panel-tools-02/panel'
    output = base/'completion-20260927'
    state = read_json(session/'session.json')
    launch = read_json(session/'kiro/launch.json')
    assert launch.get('finished_at') and launch.get('prompt_sent') is True
    assert not state.get('active_job_id') and state['selected_candidate_id'] == 'candidate-01'
    row = next(r for r in read_json(base/'completion-paired-cohort-01/progress.json')['rows']
               if r['id'] == 'panel-tools-02')
    assert row['status'] in ('failed', 'completed')
    prior = read_json(output/'panel-tools-02-source-inspection-through42.json')
    target = Path(state['target']).resolve()
    calls, writes, snapshots = {}, {}, []
    for line in (session/'kiro/run/stdout.jsonl').read_text('utf-8').splitlines():
        update = json.loads(line).get('params', {}).get('update', {})
        if update.get('sessionUpdate') != 'tool_call':
            continue
        args = update.get('rawInput')
        calls[update['toolCallId']] = digest(args)
        if update.get('title') in ('Write File', 'Replace in File'):
            path = Path(args['path']).resolve()
            assert path.is_relative_to(target)
            name = path.relative_to(target).as_posix()
            if update['title'] == 'Write File':
                writes[name] = args['text']
            else:
                assert writes[name].count(args['oldStr']) == 1
                writes[name] = writes[name].replace(args['oldStr'], args['newStr'], 1)
        if update.get('title') == '@kiroclops/evaluate_candidate':
            source = session/'candidates'/f'candidate-{len(snapshots)+1:02d}'/'source'
            hashes = source_hashes(source)
            assert (source/'src/panel-motion.js').read_text('utf-8') == writes['src/panel-motion.js']
            assert sorted(k for k in set(hashes)|set(state['baseline_hashes'])
                          if hashes.get(k) != state['baseline_hashes'].get(k)) == ['src/panel-motion.js']
            snapshots.append({'candidate_id': source.parent.name, 'source_sha256': digest(hashes),
                              'controller_sha256': file_sha256(source/'src/panel-motion.js')})
    assert len(calls) == 43 and len(snapshots) == 2
    assert dict(list(calls.items())[:42]) == prior['inspected_native_call_arguments']
    assert list(calls)[-1] == 'toolu_bdrk_01NZ2ix4hdiyytLfcdjmGwCp'
    assert set(writes) == set(prior['reviewed_files'])
    for name, entry in prior['reviewed_files'].items():
        path = session/'candidates/candidate-02/source'/name if name == 'src/panel-motion.js' else target/name
        assert path.read_text('utf-8') == writes[name]
        assert file_sha256(path) == entry['sha256']
    expected = '3a699c283714afde7a0f5b5104255817cbf1aa3a66c1761141dfa6950c631405'
    assert snapshots[0]['source_sha256'] == expected
    selected_source = session/'candidates/candidate-01/source'
    source_hash = source_hashes(selected_source)
    native = review.audit_native_run(session)
    result_path = session/'jobs'/state['final_job_id']/'result.json'
    assert state['final']['status'] == 'succeeded' and result_path.is_file(), 'No complete final recording; retain performed source inspection without inventing acceptance'
    result = read_json(result_path)
    assert len(result['comparisons']) == 12
    protected = all(c['functional']['status'] == 'pass' for c in result['comparisons'])
    checks = [
        ('actual_code_authorship', 'pass', 'The selected full controller and all three scratch helpers were independently read. Native writes and both replacements replay to each of the two immutable evaluation snapshots. Only src/panel-motion.js changes among application files. Candidate01 was selected by its lower calibration score.'),
        ('no_coordinate_replay', 'pass', 'Live analytic damped springs advance panel, title and content from current positions, velocities, actual dt and requested mode. Delay crossings split the step; current pointer owns x during drag. No sample table, clip path or recorded position playback.'),
        ('no_private_reference_access', 'pass', 'All43 native argument sets were inspected. extract.py consumes public calibration observations; show.py and the complete NumPy Nelder-Mead fit.py consume the derived local series. Feedback inspection reads published candidate calibration results only. No private physics source, DOM oracle or heldout feedback entered editing.'),
        ('no_operator_coaching', 'pass', 'One fresh Opus5.5/max native Always Allow conversation. Kiro corrected initial-mode setup and compared a damping adjustment itself. No operator solution edits or follow-up prompt.'),
        ('protected_behavior_preserved', 'pass' if protected else 'fail', 'Rendering, styles and input code retain baseline hashes. The source-bound final functional checks ' + ('all pass.' if protected else 'do not all pass.')),
    ]
    verdict = {'kind': 'IndependentSourceReview', 'status': 'pass' if protected else 'fail',
        'reviewer': 'Codex assistant', 'method': 'independent_source_and_native_trace_audit',
        'candidate_sha256': expected, 'source_hashes': source_hash,
        'baseline_hashes': state['baseline_hashes'], 'native_evidence_hashes': native['evidence_hashes'],
        'checks': [{'id': k, 'verdict': v, 'evidence': e} for k, v, e in checks],
        'actual_source_inspection_sha256': file_sha256(output/'panel-tools-02-source-inspection-through42.json'),
        'source_snapshots': snapshots, 'final_result_sha256': file_sha256(result_path),
        'scope': 'Actual assistant source and trace inspection; independent image accuracy is still pending.'}
    path = output/'panel-tools-02-completed-source-review.json'
    canonical = session/'source_review.json'
    publication = output/'panel-tools-02-source-review-publication.json'
    assert not any(p.exists() for p in (path, canonical, publication))
    write_json(path, verdict)
    if protected:
        assert review.validate_source_review(path, session, candidate_sha256=expected)['status'] == 'pass'
    canonical.write_bytes(path.read_bytes())
    write_json(publication, {'status': 'published', 'native': native['status'],
        'source_review': verdict['status'], 'source_review_sha256': file_sha256(path),
        'numeric_passed': result['passed'], 'numeric_required': result['required'],
        'primary_outcome_modified': False, 'independent_visual_review': 'pending',
        'producer_sha256': file_sha256(__file__)})
    print({'native': native['status'], 'source': verdict['status'],
           'numeric': f"{result['passed']}/{result['required']}", 'visual': 'pending'})


if __name__ == '__main__':
    run()
