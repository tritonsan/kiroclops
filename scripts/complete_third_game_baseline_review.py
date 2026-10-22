"""Bind the actual five-file,62-call inspection; publish only when terminal."""
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
from show_native_calls import calls


def run(record_only=False, staged=False):
    base = ROOT/'artifacts/phase3a'
    session = base/'completion-paired-cohort-01/sessions/game-baseline-03/jump'
    out = base/'completion-20260927'
    state = read_json(session/'session.json')
    assert state['selected_candidate_id'] == 'candidate-02'
    target = Path(state['target']).resolve()
    log = session/'kiro/run/stdout.jsonl'
    rows, tail = calls(log)
    assert not tail and len(rows) == 62, 'Additional native arguments need actual inspection'
    assert rows[-1]['tool_call_id'] == 'run_command_toolu_bdrk_0171BeLET6W2jskYKuVnLtbJ'
    writes, authors, snapshots, denials = {}, {}, [], []
    for line in log.read_text('utf-8').splitlines():
        u = json.loads(line).get('params', {}).get('update', {})
        if u.get('toolCallId') == 'toolu_bdrk_01D2wHCSbpvDaUowNHBjdhbw':
            assert u['sessionUpdate'] == 'tool_call_update' and u['status'] == 'failed'
            assert not set(u) & {'rawOutput', 'content'}
            denials.append(u)
        if u.get('sessionUpdate') != 'tool_call':
            continue
        args = u.get('rawInput') or {}
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
        if u.get('toolCallId') in ('run_command_toolu_bdrk_01LLBx98h5AGMRD8urg9UfMC',
                                  'run_command_toolu_bdrk_01JcL46g7BsnhZKRUCYomwM6'):
            source = session/'candidates'/f'candidate-{len(snapshots)+1:02d}'/'source'
            hashes = source_hashes(source)
            assert (source/'player.gd').read_text('utf-8') == writes['player.gd']
            assert sorted(k for k in set(hashes)|set(state['baseline_hashes'])
                          if hashes.get(k) != state['baseline_hashes'].get(k)) == ['player.gd']
            snapshots.append({'candidate_id': source.parent.name, 'source_sha256': digest(hashes),
                              'player_sha256': file_sha256(source/'player.gd')})
    assert len(denials) == 1 and len(snapshots) == 2
    assert set(writes) == {'player.gd', *('.kiroclops-scratch/'+n for n in ('track.py','sim.py','compare.py','sweep.py'))}
    expected = '91468203096b759bb39ee1d61e3df62c652c88a00fdc844e9176af22a47b11c1'
    assert snapshots[1]['source_sha256'] == expected
    frozen = session/'candidates/candidate-02/source'
    files = []
    for name, text in writes.items():
        path = frozen/name if name == 'player.gd' else target/name
        assert path.read_text('utf-8') == text
        files.append({'name': name, 'sha256': file_sha256(path), 'native_writes': authors[name],
                      'full_source_read_by_reviewer': True})
    inspection = {'kind': 'ActualAssistantSourceInspection', 'status': 'source_read_full_acceptance_pending',
        'candidate_id': 'candidate-02', 'candidate_sha256': expected,
        'source_snapshots': snapshots, 'reviewed_files': files,
        'inspected_native_call_arguments': {r['tool_call_id']: [digest(a) for a in r['arguments']] for r in rows},
        'denied_update_sha256': digest(denials[0]), 'findings': [
            'All62 native argument sets, the full original and selected controller, all four scratch helpers and every intermediate native replacement were read. Native writes replay to both immutable candidate controllers and current scratch helpers. Only player.gd changes among application files.',
            'track.py uses public calibration/candidate PNG foreground masks and a pixel-derived floor, with NumPy/OpenCV. sim.py and sweep.py simulate their own held-rise model; compare.py compares candidate images. All use scale2 (the model explicitly chooses reference body8 and target16); registered reference body7 is a task-scale confound, not changed here.',
            'The live controller uses takeoff204px/s, held-rise168px/s, hold17/60s, and gravity1440px/s². Candidate02 additionally predicts downward contact with test_move, leaves1px on first touchdown and then restores falling speed. It uses live input and collisions, without a recorded trajectory, scenario-id lookup or reference source code.',
            'The coyote branch still calls start_normal_jump after handle_wall_jump without excluding just_wall_jumped. This can overwrite the wall response on the same tick. Air control, acceleration/friction, sprites, camera and collision shapes otherwise retain their original code/data; final functional preservation determines the protected-behavior verdict.',
            'One relative-public-evidence command was denied before execution and returned no data. Kiro retried using absolute paths inside the public evidence tree. No private reference physics, private oracle, preservation report or heldout motion data was accessed.',
            'No operator solution, candidate edit or follow-up prompt. One native Opus5.5/max Always Allow conversation; selected candidate02 is sealed before heldout validation. The initial Python simulation had an uninitialized local; Kiro fixed it itself.'
        ], 'primary_outcome_modified': False, 'producer_sha256': file_sha256(__file__)}
    inspected = out/'game-baseline-03-sealed-source-inspection.json'
    if record_only:
        assert not inspected.exists()
        write_json(inspected, inspection)
        print({'inspection': 'recorded', 'files': len(files), 'native_calls': len(rows)})
        return
    prior = read_json(inspected)
    for k in ('candidate_sha256','source_snapshots','reviewed_files','inspected_native_call_arguments','denied_update_sha256'):
        assert prior[k] == inspection[k]
    row = next(r for r in read_json(base/'completion-paired-cohort-01/progress.json')['rows'] if r['id']=='game-baseline-03')
    assert row['status'] in ('failed','completed')
    assert read_json(session/'kiro/launch.json').get('finished_at') and not state.get('active_job_id')
    native = review.audit_native_run(session)
    result_path = session/'jobs'/state['final_job_id']/'result.json'
    assert state['final']['status'] == 'succeeded'
    result = read_json(result_path)
    protected = result['preservation']['status'] == 'pass'
    notes = inspection['findings']
    checks = [
        ('actual_code_authorship','pass',notes[0]),
        ('no_coordinate_replay','pass',notes[2]),
        ('no_private_reference_access','pass',notes[1]+' '+notes[4]+' Complete native audit passed.'),
        ('no_operator_coaching','pass',notes[5]),
        ('protected_behavior_preserved','pass' if protected else 'fail',notes[3]+' Final preservation: '+result['preservation']['status']),
    ]
    verdict = {'kind':'IndependentSourceReview','status':'pass' if protected else 'fail',
        'reviewer':'Codex assistant','method':'independent_source_and_native_trace_audit',
        'candidate_sha256':expected,'source_hashes':source_hashes(frozen),'baseline_hashes':state['baseline_hashes'],
        'native_evidence_hashes':native['evidence_hashes'],
        'checks':[{'id':k,'verdict':v,'evidence':e} for k,v,e in checks],
        'performed_inspection_sha256':file_sha256(inspected),'final_result_sha256':file_sha256(result_path),
        'posthoc_native_compatibility':staged,'scope':'Actual source/native inspection only; independent image review remains separate.'}
    path = out/'game-baseline-03-completed-source-review.json'
    canonical = session/'source_review.json'
    receipt = out/'game-baseline-03-source-review-publication.json'
    assert not any(p.exists() for p in (path, canonical, receipt))
    write_json(path, verdict)
    if protected:
        assert review.validate_source_review(path, session, candidate_sha256=expected)['status']=='pass'
    canonical.write_bytes(path.read_bytes())
    write_json(receipt, {'status':'published','native_audit':native['status'],'source_review':verdict['status'],
        'source_review_sha256':file_sha256(path),'numeric_passed':result['passed'],'numeric_required':result['required'],
        'visual_boundary':{k:result['visual_wall'][k] for k in ('passed','required')},
        'posthoc_native_compatibility':staged,'primary_outcome_modified':False,
        'independent_visual_review':'pending','producer_sha256':file_sha256(__file__)})
    print({'native':native['status'],'source':verdict['status'],
           'numeric':f"{result['passed']}/{result['required']}",'visual':'pending'})


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--record-inspection', action='store_true')
    p.add_argument('--staged-native-reader', action='store_true')
    args = p.parse_args()
    scope = nullcontext()
    if args.staged_native_reader:
        from stage_native_completion_repair import patched_reader
        proposal = ROOT/'artifacts/phase3a/native-completion-compatibility-01'
        a = read_json(proposal/'result.json')
        b = read_json(proposal/'combined-regression-02.json')
        assert a['status'] == b['status'] == 'pass' and b['tests']==104
        for source,key in (('review','review'),('acceptance','acceptance')):
            assert file_sha256(ROOT/f'kiroclops/local_engine/{source}.py') == a[f'production_{key}_sha256']
            assert file_sha256(proposal/f'{source}-proposed.py') == a[f'proposed_{key}_sha256']
        assert file_sha256(ROOT/'scripts/stage_native_completion_repair.py') == b['runtime_adapter_sha256']
        scope = patched_reader(proposal)
    with scope:
        run(args.record_inspection, args.staged_native_reader)
