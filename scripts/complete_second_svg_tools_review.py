"""Publish an actual source inspection, with acceptance kept independently gated.

All40 native arguments and all four written source files were read by the
assistant. This script binds that inspection to bytes; it does not perform it.
"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine import review


def run(record_only=False):
    base = ROOT/'artifacts/phase3a'
    session = base/'completion-paired-cohort-01/sessions/svg-tools-02/svg'
    output = base/'completion-20260927'
    state = read_json(session/'session.json')
    assert state['selected_candidate_id'] == 'candidate-02'
    target = Path(state['target']).resolve()
    calls, writes, authors, snapshots = {}, {}, {}, []
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
            authors.setdefault(name, []).append({'tool_call_id': update['toolCallId'],
                                                'arguments_sha256': digest(args)})
        if update.get('title') == '@kiroclops/evaluate_candidate':
            source = session/'candidates'/f'candidate-{len(snapshots)+1:02d}'/'source'
            hashes = source_hashes(source)
            assert (source/'src/layout-motion.js').read_text('utf-8') == writes['src/layout-motion.js']
            assert sorted(k for k in set(hashes)|set(state['baseline_hashes'])
                          if hashes.get(k) != state['baseline_hashes'].get(k)) == ['src/layout-motion.js']
            snapshots.append({'candidate_id': source.parent.name, 'source_sha256': digest(hashes),
                              'controller_sha256': file_sha256(source/'src/layout-motion.js')})
    assert len(calls) == 40 and len(snapshots) == 2
    assert list(calls)[-1] == 'toolu_bdrk_01CBrMNBR8NZ9FYeCnFLwJ6b'
    assert set(writes) == {'src/layout-motion.js', '.kiroclops-scratch/extract.py',
                          '.kiroclops-scratch/fit.py', '.kiroclops-scratch/fit2.py'}
    files = []
    frozen = session/'candidates/candidate-02/source'
    for name, content in writes.items():
        path = frozen/name if name == 'src/layout-motion.js' else target/name
        assert path.read_text('utf-8') == content
        files.append({'name': name, 'sha256': file_sha256(path),
                      'native_writes': authors[name], 'full_source_read_by_reviewer': True})
    assert (target/'.kiroclops-scratch/lm.mjs').read_text('utf-8') == writes['src/layout-motion.js']
    inspection_path = output/'svg-tools-02-sealed-source-inspection.json'
    findings = [
        'All40 actual native argument sets and every Write/Replace source body read. Four written files and the numerical-check controller copy are bound to their actual bytes.',
        'extract.py reads public calibration image observations, subtracts the stationary a-node anchor and derives trajectories. fit.py compares spring integrations over observed frame intervals with NumPy grid/refinement searches. fit2.py compares analytical underdamped springs and rejects overdamped parameter proposals.',
        'The first numerical search timed out after300000ms. A same-identity Python child survived the shell timeout in read-only process samples; this is separately documented, not repaired during the trial and not evidence of model fallback.',
        'Candidate01 uses one semi-implicit step with stiffness120/damping13.75; candidate02 uses a live analytical spring with stiffness154/damping16.75, persistent velocity and actualdt. Both evaluation snapshots match the native authored controller.',
        'Only src/layout-motion.js changes in application sources. Dragged node remains at its pointer position with zero velocity; targets and current state are live. No recorded coordinates are played back.',
        'No operator code changes, live coaching, private reference code or heldout feedback observed during the editing turn. Numerical, functional and independent image outcomes remain separate.'
    ]
    inspection = {'kind': 'ActualAssistantSourceInspection',
        'status': 'source_read_full_acceptance_pending', 'candidate_id': 'candidate-02',
        'candidate_sha256': snapshots[-1]['source_sha256'], 'reviewed_files': files,
        'source_snapshots': snapshots, 'inspected_native_call_arguments': calls,
        'findings': findings, 'producer_sha256': file_sha256(__file__),
        'primary_outcome_modified': False}
    if record_only:
        assert not inspection_path.exists()
        write_json(inspection_path, inspection)
        print({'source_inspection': 'recorded', 'files': len(files), 'native_calls': len(calls),
               'candidate_sha256': snapshots[-1]['source_sha256'], 'full_acceptance': 'pending'})
        return
    prior = read_json(inspection_path)
    for key in ('candidate_sha256', 'reviewed_files', 'source_snapshots', 'inspected_native_call_arguments'):
        assert prior[key] == inspection[key]
    launch = read_json(session/'kiro/launch.json')
    row = next(r for r in read_json(base/'completion-paired-cohort-01/progress.json')['rows']
               if r['id'] == 'svg-tools-02')
    assert launch.get('finished_at') and not state.get('active_job_id')
    assert row['status'] in ('failed', 'completed')
    native = review.audit_native_run(session)
    result_path = session/'jobs'/state['final_job_id']/'result.json'
    assert state['final']['status'] == 'succeeded' and result_path.is_file(), 'No full final measurement; preserve source inspection only'
    result = read_json(result_path)
    assert len(result['comparisons']) == 12
    protected = all(c['functional']['status'] == 'pass' and c['svg_geometry_preservation']['pass']
                    for c in result['comparisons'])
    checks = [
        ('actual_code_authorship', 'pass', findings[0] + ' ' + findings[3]),
        ('no_coordinate_replay', 'pass', findings[4]),
        ('no_private_reference_access', 'pass', findings[1] + ' ' + findings[5]),
        ('no_operator_coaching', 'pass', 'One fresh native Opus5.5/max Always Allow conversation and one prepared prompt. Kiro authored both controllers, evaluated and finalized itself; no follow-up solution or replacement trial.'),
        ('protected_behavior_preserved', 'pass' if protected else 'fail', 'Application rendering/input/styles retain baseline hashes. Source-bound final functional and SVG geometry checks ' + ('all pass.' if protected else 'do not all pass.')),
    ]
    hashes = source_hashes(frozen)
    verdict = {'kind': 'IndependentSourceReview', 'status': 'pass' if protected else 'fail',
        'reviewer': 'Codex assistant', 'method': 'independent_source_and_native_trace_audit',
        'candidate_sha256': digest(hashes), 'source_hashes': hashes,
        'baseline_hashes': state['baseline_hashes'], 'native_evidence_hashes': native['evidence_hashes'],
        'checks': [{'id': k, 'verdict': v, 'evidence': e} for k, v, e in checks],
        'actual_source_inspection_sha256': file_sha256(inspection_path), 'source_snapshots': snapshots,
        'final_result_sha256': file_sha256(result_path),
        'scope': 'Actual assistant source and trace inspection; independent image accuracy is still pending.'}
    path = output/'svg-tools-02-completed-source-review.json'
    canonical = session/'source_review.json'
    receipt = output/'svg-tools-02-source-review-publication.json'
    assert not any(p.exists() for p in (path, canonical, receipt))
    write_json(path, verdict)
    if protected:
        assert review.validate_source_review(path, session, candidate_sha256=digest(hashes))['status'] == 'pass'
    canonical.write_bytes(path.read_bytes())
    write_json(receipt, {'status': 'published', 'native': native['status'],
        'source_review': verdict['status'], 'source_review_sha256': file_sha256(path),
        'numeric_passed': result['passed'], 'numeric_required': result['required'],
        'primary_outcome_modified': False, 'independent_visual_review': 'pending',
        'producer_sha256': file_sha256(__file__)})
    print({'native': native['status'], 'source': verdict['status'],
           'numeric': f"{result['passed']}/{result['required']}", 'visual': 'pending'})


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--record-inspection', action='store_true')
    run(p.parse_args().record_inspection)
