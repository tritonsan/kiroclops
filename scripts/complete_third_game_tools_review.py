"""Bind the actually read two files and51 native arguments; close review later.

The optional staged native reader is explicitly post-hoc. Original outcomes,
capture evidence and candidate code are never rewritten.
"""
from contextlib import nullcontext
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine import review
from show_native_calls import calls as native_calls


def run(record_only=False, staged=False):
    base = ROOT/'artifacts/phase3a'
    session = base/'completion-paired-cohort-01/sessions/game-tools-03/jump'
    output = base/'completion-20260927'
    state = read_json(session/'session.json')
    assert state['selected_candidate_id'] == 'candidate-02'
    target = Path(state['target']).resolve()
    log = session/'kiro/run/stdout.jsonl'
    rows, incomplete = native_calls(log)
    assert not incomplete and len(rows) == 51, 'Additional native arguments need actual inspection'
    assert rows[-1]['tool_call_id'] == 'toolu_bdrk_01XiB9nydAJKkocwdtZjt173'
    argument_hashes = {r['tool_call_id']: [digest(a) for a in r['arguments']] for r in rows}
    writes, authors, snapshots, denied = {}, {}, [], []
    for line in log.read_text('utf-8').splitlines():
        u = json.loads(line).get('params', {}).get('update', {})
        if u.get('toolCallId') == 'toolu_bdrk_01GYeWKKooiH8qgAxTFiJqaF':
            assert u['sessionUpdate'] == 'tool_call_update' and u['status'] == 'failed'
            assert not set(u) & {'rawOutput', 'content'}
            denied.append(u)
        if u.get('sessionUpdate') != 'tool_call':
            continue
        args = u.get('rawInput')
        if u.get('title') in ('Write File', 'Replace in File'):
            path = Path(args['path']).resolve()
            assert path.is_relative_to(target)
            name = path.relative_to(target).as_posix()
            if u['title'] == 'Write File':
                writes[name] = args['text']
            else:
                assert writes[name].count(args['oldStr']) == 1
                writes[name] = writes[name].replace(args['oldStr'], args['newStr'], 1)
            authors.setdefault(name, []).append({'tool_call_id': u['toolCallId'], 'arguments_sha256': digest(args)})
        if u.get('title') == '@kiroclops/evaluate_candidate':
            source = session/'candidates'/f'candidate-{len(snapshots)+1:02d}'/'source'
            hashes = source_hashes(source)
            assert (source/'player.gd').read_text('utf-8') == writes['player.gd']
            assert sorted(k for k in set(hashes)|set(state['baseline_hashes'])
                          if hashes.get(k) != state['baseline_hashes'].get(k)) == ['player.gd']
            snapshots.append({'candidate_id': source.parent.name, 'source_sha256': digest(hashes),
                              'player_sha256': file_sha256(source/'player.gd')})
    assert len(denied) == 1 and len(snapshots) == 2
    assert set(writes) == {'player.gd', '.kiroclops-scratch/track.py'}
    expected = '4c2b89bcedcd10644736a2468a61b17fe8745b941a0343effce8088a0f27a3a2'
    assert snapshots[1]['source_sha256'] == expected
    frozen = session/'candidates/candidate-02/source'
    files = []
    for name, content in writes.items():
        path = frozen/name if name == 'player.gd' else target/name
        assert path.read_text('utf-8') == content
        files.append({'name': name, 'sha256': file_sha256(path), 'native_writes': authors[name],
                      'full_source_read_by_reviewer': True})
    inspection_path = output/'game-tools-03-sealed-source-inspection.json'
    inspection = {'kind': 'ActualAssistantSourceInspection', 'status': 'source_read_full_acceptance_pending',
        'candidate_id': 'candidate-02', 'candidate_sha256': expected,
        'source_snapshots': snapshots, 'reviewed_files': files,
        'inspected_native_call_arguments': argument_hashes,
        'separately_inspected_denied_update_sha256': digest(denied[0]),
        'findings': [
            'All51 native argument sets and both full controller versions read. The complete initial and revised PIL pixel-tracking helper were read. Native writes replay to both immutable controller snapshots and the current helper.',
            'The profile units, v0,gravity and hold ticks were confirmed present in the actual native tool output. The controller uses body16, measured speed12.447667529 body/s,gravity103.977288855 body/s² and17/60s sustained rise. Actual live input and physics ticks govern release; no recorded coordinate table or scenario playback.',
            'Wall and air jumps end the normal-jump state and retain the inherited movement-data/gravity code. The candidate02 coyote branch preserves native wall velocity when just_wall_jumped; horizontal functions, sprite, camera, scene and collisions remain unchanged.',
            'The helper consumes public calibration PNGs, first using frame0 difference and then foreground bounding boxes above an image-derived floor. It imports PIL and uses no native coordinates or private physics constants.',
            'One request attempted private preservation evidence outside target; it was denied before execution, with no content/rawOutput returned. Source review acceptance additionally requires a complete exact native replay of that denial. No reference movement source or heldout feedback was consumed.',
            'No operator solution or follow-up prompt. An incidental operator filename scan during initial preparation is disclosed separately; it was not a source edit or model coaching.'
        ], 'producer_sha256': file_sha256(__file__), 'primary_outcome_modified': False}
    if record_only:
        assert not inspection_path.exists()
        write_json(inspection_path, inspection)
        print({'source_inspection': 'recorded', 'files': len(files), 'native_calls': len(rows),
               'candidate_sha256': expected, 'full_acceptance': 'pending'})
        return
    prior = read_json(inspection_path)
    for key in ('candidate_sha256', 'source_snapshots', 'reviewed_files', 'inspected_native_call_arguments',
                'separately_inspected_denied_update_sha256'):
        assert prior[key] == inspection[key]
    launch = read_json(session/'kiro/launch.json')
    row = next(r for r in read_json(base/'completion-paired-cohort-01/progress.json')['rows'] if r['id'] == 'game-tools-03')
    assert launch.get('finished_at') and not state.get('active_job_id')
    assert row['status'] in ('failed', 'completed')
    native = review.audit_native_run(session)
    result_path = session/'jobs'/state['final_job_id']/'result.json'
    assert state['final']['status'] == 'succeeded' and result_path.is_file()
    result = read_json(result_path)
    protected = result['preservation']['status'] == 'pass'
    notes = inspection['findings']
    checks = [
        ('actual_code_authorship', 'pass', notes[0] + ' Only player.gd changes among application files.'),
        ('no_coordinate_replay', 'pass', notes[1]),
        ('no_private_reference_access', 'pass', notes[3] + ' ' + notes[4] + ' The full native replay binds the denial.'),
        ('no_operator_coaching', 'pass', notes[5] + ' One Opus5.5/max Always Allow conversation; Kiro independently responded to the first preservation failure and sealed candidate02.'),
        ('protected_behavior_preserved', 'pass' if protected else 'fail', notes[2] + ' Source-bound final preservation status: '+result['preservation']['status']),
    ]
    hashes = source_hashes(frozen)
    verdict = {'kind': 'IndependentSourceReview', 'status': 'pass' if protected else 'fail',
        'reviewer': 'Codex assistant', 'method': 'independent_source_and_native_trace_audit',
        'candidate_sha256': expected, 'source_hashes': hashes, 'baseline_hashes': state['baseline_hashes'],
        'native_evidence_hashes': native['evidence_hashes'],
        'checks': [{'id': k, 'verdict': v, 'evidence': e} for k, v, e in checks],
        'performed_inspection_sha256': file_sha256(inspection_path),
        'posthoc_native_compatibility': staged, 'final_result_sha256': file_sha256(result_path),
        'scope': 'Actual source/native inspection only; independent image review remains separate.'}
    path = output/'game-tools-03-completed-source-review.json'
    canonical = session/'source_review.json'
    receipt = output/'game-tools-03-source-review-publication.json'
    assert not any(p.exists() for p in (path, canonical, receipt))
    write_json(path, verdict)
    if protected:
        assert review.validate_source_review(path, session, candidate_sha256=expected)['status'] == 'pass'
    canonical.write_bytes(path.read_bytes())
    write_json(receipt, {'status': 'published', 'native_audit': native['status'],
        'source_review': verdict['status'], 'source_review_sha256': file_sha256(path),
        'numeric_passed': result['passed'], 'numeric_required': result['required'],
        'visual_boundary': {k: result['visual_wall'][k] for k in ('passed', 'required')},
        'posthoc_native_compatibility': staged, 'primary_outcome_modified': False,
        'independent_visual_review': 'pending', 'producer_sha256': file_sha256(__file__)})
    print({'native': native['status'], 'source': verdict['status'],
           'numeric': f"{result['passed']}/{result['required']}", 'visual': 'pending'})


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--record-inspection', action='store_true')
    p.add_argument('--staged-native-reader', action='store_true')
    args = p.parse_args()
    scope = nullcontext()
    if args.staged_native_reader:
        from stage_native_completion_repair import patched_reader
        proposal = ROOT/'artifacts/phase3a/native-completion-compatibility-01'
        r = read_json(proposal/'result.json')
        tests = read_json(proposal/'combined-regression-02.json')
        assert r['status'] == tests['status'] == 'pass' and tests['tests'] == 104
        assert file_sha256(ROOT/'kiroclops/local_engine/review.py') == r['production_review_sha256']
        assert file_sha256(proposal/'review-proposed.py') == r['proposed_review_sha256']
        assert file_sha256(ROOT/'scripts/stage_native_completion_repair.py') == tests['runtime_adapter_sha256']
        scope = patched_reader(proposal)
    with scope:
        run(args.record_inspection, args.staged_native_reader)
