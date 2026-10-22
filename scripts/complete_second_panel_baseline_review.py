"""Close this particular completed source inspection; never approve unseen edits.

--record-inspection binds the nine files and 61 native calls actually read by
the assistant. The default command requires that exact record and a terminal
trial before publishing the independent source review. Pixel review is separate.
"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine.review import audit_native_run, validate_source_review

BASE = ROOT / 'artifacts/phase3a'
SESSION = BASE / 'completion-paired-cohort-01/sessions/panel-baseline-02/panel'
OUT = BASE / 'completion-20260927'
NOTE = OUT / 'panel-baseline-02-sealed-source-inspection.json'
NAMES = {'src/panel-motion.js'} | {
    '.kiroclops-scratch/' + name for name in
    ('extract.py', 'show.py', 'crop.py', 'debug.py', 'fit.py', 'sim.mjs', 'compare.py', 'disc.py')}


def inspect_binding():
    state = read_json(SESSION / 'session.json')
    assert state.get('sealed_at') and state['selected_candidate_id'] == 'candidate-02'
    source = SESSION / 'candidates/candidate-02/source'
    target = Path(state['target']).resolve()
    hashes = source_hashes(source)
    assert sorted(k for k in set(hashes) | set(state['baseline_hashes'])
                  if hashes.get(k) != state['baseline_hashes'].get(k)) == ['src/panel-motion.js']
    writes, calls, authors = {}, {}, {}
    for line in (SESSION / 'kiro/run/stdout.jsonl').read_text('utf-8').splitlines():
        update = json.loads(line).get('params', {}).get('update', {})
        if update.get('sessionUpdate') not in ('tool_call', 'tool_call_update'):
            continue
        identity = update.get('toolCallId')
        if not isinstance(identity, str) or not identity:
            continue
        if update.get('rawInput') is not None:
            calls.setdefault(identity, [])
            arg_hash = digest(update['rawInput'])
            if arg_hash not in calls[identity]:
                calls[identity].append(arg_hash)
        if update.get('sessionUpdate') != 'tool_call' or update.get('title') not in ('Write File', 'Replace in File'):
            continue
        args = update['rawInput']
        path = Path(args['path']).resolve()
        assert path.is_relative_to(target)
        name = path.relative_to(target).as_posix()
        if update['title'] == 'Write File':
            writes[name] = args['text']
        else:
            assert name in writes and writes[name].count(args['oldStr']) == 1
            writes[name] = writes[name].replace(args['oldStr'], args['newStr'], 1)
        authors.setdefault(name, []).append({'tool_call_id': identity, 'arguments_sha256': digest(args)})
    assert len(calls) == 61 and set(writes) == NAMES
    files = {}
    for name, content in writes.items():
        path = source / name if name in hashes else target / name
        assert path.read_text('utf-8') == content
        files[name] = {'sha256': file_sha256(path), 'native_writes': authors[name]}
    return {'candidate_sha256': digest(hashes), 'source_hashes': hashes,
            'reviewed_files': files, 'inspected_native_call_arguments': calls}


def main(record=False):
    binding = inspect_binding()
    if record:
        assert not NOTE.exists()
        write_json(NOTE, {**binding, 'kind': 'PerformedSourceInspection',
            'full_source_read_by_assistant': True, 'native_arguments_read_through_call': 61,
            'acceptance_granted': False,
            'scope': 'All nine final files and native argument sets actually read. Initial and updated extraction/fitting bodies were inspected as the model revised them.'})
        print({'inspection_recorded': True, 'files': 9, 'calls': 61, 'acceptance_granted': False})
        return
    saved = read_json(NOTE)
    assert all(saved[k] == value for k, value in binding.items()), 'Uninspected source or call change'
    state = read_json(SESSION / 'session.json')
    launch = read_json(SESSION / 'kiro/launch.json')
    row = next(r for r in read_json(BASE/'completion-paired-cohort-01/progress.json')['rows']
               if r['id'] == 'panel-baseline-02')
    assert row['status'] in ('completed', 'failed')
    assert launch.get('finished_at') and launch.get('prompt_sent') is True
    assert state['status'] == 'finalized' and not state.get('active_job_id')
    native = audit_native_run(SESSION)
    final_path = SESSION / 'jobs' / state['final_job_id'] / 'result.json'
    result = read_json(final_path)
    assert len(result['comparisons']) == 12
    protected = all(c['functional']['status'] == 'pass' for c in result['comparisons'])
    checks = [
        ('actual_code_authorship', 'pass', 'All eight helpers and the full selected controller were read. Every Write/Replace replays exactly; only src/panel-motion.js differs among application sources.'),
        ('no_coordinate_replay', 'pass', 'Three analytic damped springs integrate actual state, input, target and dt. Delay crossings split the integration step; pointer drag preserves actual x with zero velocity. No trajectory lookup or reference clip replay.'),
        ('no_private_reference_access', 'pass', 'All61 native argument sets were inspected. Helpers read public calibration/candidate PNGs, their manifests, locally extracted tracks and the edited controller. Crop/debug scripts inspect pixels; fit.py implements its own Nelder-Mead after SciPy was unavailable. One inline Python crop request was denied before execution, then rewritten as an allowed scratch script. No private source or heldout feedback was consumed while editing.'),
        ('no_operator_coaching', 'pass', 'One fresh Opus5.5/max native Always Allow conversation, two candidate submissions. Kiro diagnosed its own tracking and spring-fit error. No operator solution edits or follow-up prompt.'),
        ('protected_behavior_preserved', 'pass' if protected else 'fail', 'Rendering, CSS and input-dispatch source hashes remain unchanged; all12 bound functional checks ' + ('pass.' if protected else 'do not pass.')),
    ]
    review = {'kind': 'IndependentSourceReview', 'status': 'pass' if protected else 'fail',
        'reviewer': 'Codex assistant', 'method': 'independent_source_and_native_trace_audit',
        'candidate_sha256': binding['candidate_sha256'], 'source_hashes': binding['source_hashes'],
        'baseline_hashes': state['baseline_hashes'], 'native_evidence_hashes': native['evidence_hashes'],
        'checks': [{'id': k, 'verdict': v, 'evidence': e} for k, v, e in checks],
        'performed_inspection_sha256': file_sha256(NOTE), 'final_result_sha256': file_sha256(final_path),
        'scope': 'Actual assistant source/trace review. Numerical fidelity and independent image accuracy are separate.'}
    output = OUT / 'panel-baseline-02-completed-source-review.json'
    canonical = SESSION / 'source_review.json'
    publication = OUT / 'panel-baseline-02-source-review-publication.json'
    assert not any(p.exists() for p in (output, canonical, publication))
    write_json(output, review)
    if protected:
        assert validate_source_review(output, SESSION, candidate_sha256=binding['candidate_sha256'])['status'] == 'pass'
    canonical.write_bytes(output.read_bytes())
    write_json(publication, {'status': 'published', 'source_review': review['status'],
        'source_review_sha256': file_sha256(output), 'native_status': native['status'],
        'numerical_passed': result['passed'], 'numerical_required': result['required'],
        'independent_visual_review': 'pending', 'primary_outcome_modified': False,
        'producer_sha256': file_sha256(__file__)})
    print({'source_review': review['status'], 'native': native['status'],
           'numeric': f"{result['passed']}/{result['required']}", 'independent_visual_review': 'pending'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-inspection', action='store_true')
    main(parser.parse_args().record_inspection)
