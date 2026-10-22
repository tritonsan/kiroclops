"""Bind the already performed full source inspection to terminal native receipts.

This closes a specific assistant review; it cannot approve an uninspected trial.
It does not alter numerical outcomes or claim independent image accuracy.
"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine.review import audit_native_run, validate_source_review


def main():
    base = ROOT / 'artifacts/phase3a'
    session = base / 'completion-paired-cohort-01/sessions/svg-baseline-01/svg'
    notes_path = base / 'completion-20260927/svg-baseline-01-sealed-source-notes.json'
    notes = read_json(notes_path)
    state = read_json(session / 'session.json')
    assert state['status'] == 'finalized' and not state.get('active_job_id')
    assert state['selected_candidate_id'] == 'candidate-02'
    launch = read_json(session / 'kiro/launch.json')
    progress = read_json(base / 'completion-paired-cohort-01/progress.json')
    row = next(r for r in progress['rows'] if r['id'] == 'svg-baseline-01')
    assert launch.get('finished_at') and row['status'] == 'failed'
    assert row['stages'][0].get('postturn_report') and (session / 'report/report.json').is_file()
    frozen = session / 'candidates/candidate-02/source'
    target = Path(state['target']).resolve()
    hashes = source_hashes(frozen)
    assert digest(hashes) == notes['candidate_sha256']
    changed = sorted(k for k in set(hashes) | set(state['baseline_hashes'])
                     if hashes.get(k) != state['baseline_hashes'].get(k))
    assert changed == ['src/layout-motion.js']
    writes = {}
    authors = {}
    for line in (session / 'kiro/run/stdout.jsonl').read_text('utf-8').splitlines():
        update = json.loads(line).get('params', {}).get('update', {})
        if update.get('sessionUpdate') != 'tool_call':
            continue
        args = update.get('rawInput') or {}
        if update.get('title') not in ('Write File', 'Replace in File'):
            continue
        path = Path(args['path']).resolve()
        assert path.is_relative_to(target)
        name = path.relative_to(target).as_posix()
        if update['title'] == 'Write File':
            writes[name] = args['text']
        else:
            assert name in writes and writes[name].count(args['oldStr']) == 1
            writes[name] = writes[name].replace(args['oldStr'], args['newStr'], 1)
        authors.setdefault(name, []).append({'tool_call_id': update['toolCallId'],
                                             'arguments_sha256': digest(args)})
    reviewed = {r['name']: r for r in notes['reviewed_files']}
    assert set(writes) == set(reviewed) and len(reviewed) == 10
    for name, content in writes.items():
        path = frozen / name if name in hashes else target / name
        assert path.read_text('utf-8') == content
        assert file_sha256(path) == reviewed[name]['sha256']
        assert reviewed[name]['full_source_read_by_reviewer'] is True
        assert authors[name] == reviewed[name]['native_writes']
    native = audit_native_run(session)
    result_path = session / 'jobs' / state['final_job_id'] / 'result.json'
    result = read_json(result_path)
    assert result['passed'] == 8 and result['required'] == 12
    assert len(result['comparisons']) == 12
    assert all(c['functional']['status'] == 'pass' and c['svg_geometry_preservation']['pass']
               for c in result['comparisons'])
    checks = [
        ('actual_code_authorship', 'The controller and all nine scratch helpers were fully read before closure. All native Write/Replace operations replay exactly to these ten reviewed files. Only src/layout-motion.js differs from application baseline. No operator solution edit.'),
        ('no_coordinate_replay', 'The full controller advances live node states and targets using dt, four spring integration substeps, and geometric separation. Dragged nodes retain pointer position with zero velocity. No recorded trajectory table or clip lookup is used.'),
        ('no_private_reference_access', 'All helper bodies, replacement diffs and actual Execute command arguments were inspected. Extract/fit/compare consume public calibration and candidate PNGs/manifests or locally derived tracks. sim.mjs imports the edited controller but was not invoked. No reference source, private oracle, heldout feedback, remote execution or dynamic eval/exec was found.'),
        ('no_operator_coaching', 'The terminal native audit binds one fresh conversation, one prepared prompt, Claude Opus5.5/max and native Always Allow. The model autonomously retried the initial failed preparation in the same conversation. No follow-up operator prompt, candidate correction or replacement trial was supplied.'),
        ('protected_behavior_preserved', 'All12 final functional and geometry checks pass; app.js, rendering and styles retain baseline hashes. Requested motion fidelity remains8/12, with four f-node initial-motion failures. This source verdict does not certify trajectory accuracy or independent image measurement.'),
    ]
    review = {'kind': 'IndependentSourceReview', 'status': 'pass',
        'reviewer': 'Codex assistant', 'method': 'independent_source_and_native_trace_audit',
        'candidate_sha256': notes['candidate_sha256'], 'source_hashes': hashes,
        'baseline_hashes': state['baseline_hashes'], 'native_evidence_hashes': native['evidence_hashes'],
        'checks': [{'id': name, 'verdict': 'pass', 'evidence': evidence} for name, evidence in checks],
        'supplemental_source_notes_sha256': file_sha256(notes_path),
        'final_result_sha256': file_sha256(result_path),
        'scope': 'Actual assistant source/trace inspection; not external human attestation or motion/visual acceptance.'}
    output = base / 'completion-20260927/svg-baseline-01-completed-source-review.json'
    publication = base / 'completion-20260927/svg-baseline-01-source-review-publication.json'
    canonical = session / 'source_review.json'
    assert not any(p.exists() for p in (output, publication, canonical))
    write_json(output, review)
    validated = validate_source_review(output, session, candidate_sha256=notes['candidate_sha256'])
    canonical.write_bytes(output.read_bytes())
    assert file_sha256(canonical) == file_sha256(output)
    write_json(publication, {'status': 'published', 'source_review_sha256': file_sha256(output),
        'native_status': validated['native_validation']['status'],
        'reviewed_source_files': 10, 'candidate_sha256': notes['candidate_sha256'],
        'numerical_passed': 8, 'numerical_required': 12, 'functional_passed': 12,
        'independent_visual_review': 'pending', 'primary_outcome_modified': False,
        'producer_sha256': file_sha256(__file__)})
    print({'source_native_review': 'pass', 'numerical_result': '8/12',
           'independent_visual_review': 'pending', 'primary_outcome_modified': False})


if __name__ == '__main__':
    main()
