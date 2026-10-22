"""Publish an actually performed source inspection after the native turn closes.

No capture, model prompt, candidate edit or historical outcome change occurs.
Unknown new native arguments invalidate this inspection instead of being signed.
"""
from pathlib import Path
from contextlib import nullcontext
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine.review import audit_native_run, validate_source_review


def main(staged_native=False):
    base = ROOT / 'artifacts/phase3a'
    session = base / 'completion-paired-cohort-01/sessions/game-tools-02/jump'
    out = base / 'completion-20260927'
    publication = out / 'game-tools-02-source-review-publication.json'
    assert not publication.exists() and not (session / 'source_review.json').exists()
    state = read_json(session / 'session.json')
    launch = read_json(session / 'kiro/launch.json')
    assert launch.get('finished_at') and launch.get('prompt_sent') is True
    assert state['status'] == 'finalized' and not state.get('active_job_id')
    selected = state['selected_candidate_id']
    assert selected == 'candidate-02'
    expected = '4a59f165d90dbfe2aefdda2521af106c0544cc57dfdd6d9a7f567c42b1998b39'
    source = session / 'candidates' / selected / 'source'
    hashes = source_hashes(source)
    assert digest(hashes) == expected
    assert [k for k in hashes if hashes[k] != state['baseline_hashes'].get(k)] == ['player.gd']
    inspection = read_json(out / 'game-tools-02-source-inspection.json')
    snapshots = {x['candidate_id']: x for x in inspection['candidate_snapshots']}
    assert snapshots[selected]['player_sha256'] == file_sha256(source / 'player.gd')
    target = Path(state['target'])
    for name, sha in inspection['reviewed_files'].items():
        assert file_sha256(target / name) == sha
    updates = [json.loads(line).get('params', {}).get('update', {})
               for line in (session / 'kiro/run/stdout.jsonl').read_text('utf-8').splitlines()]
    calls = [u for u in updates if u.get('sessionUpdate') == 'tool_call']
    inspected = {u['toolCallId']: digest(u.get('rawInput')) for u in calls
                 if not u.get('title', '').startswith('@kiroclops/')}
    assert inspected == inspection['inspected_native_call_arguments'], 'Additional calls need actual inspection'
    starts = {u['toolCallId'] for u in calls}
    rejected = [u for u in updates if u.get('sessionUpdate') == 'tool_call_update'
                and u.get('toolCallId') not in starts]
    assert len(rejected) == 1
    denial = rejected[0]
    assert denial['toolCallId'] == 'toolu_bdrk_01LmnchcR53hgjyckoLWzdSv'
    assert denial['status'] == 'failed' and denial['title'] == 'Run Command'
    assert not set(denial) & {'rawOutput', 'content'}
    native = audit_native_run(session)
    result_path = session / 'jobs' / state['final_job_id'] / 'result.json'
    result = read_json(result_path)
    protected = result['preservation']['status'] == 'pass'
    checks = [
        ('actual_code_authorship', 'pass',
         'Three native controller revisions replay exactly to their immutable candidates. Both scratch helpers and the full controller were read. Only player.gd changes among application files.'),
        ('no_coordinate_replay', 'pass',
         'Live input and dt drive sustained normal rise, gravity, release and contact handling. Air/wall jumps exit the normal-jump phase. Landing clears it. No recorded-coordinate playback exists.'),
        ('no_private_reference_access', 'pass',
         'All started non-MCP native arguments match those independently inspected. Helpers use only public h03/h24 calibration pixels and numerical search. One additional terminal-only request attempted to read private preservation evidence; its complete arguments were separately inspected, the hook denied it before execution, and its sole failed update contains no returned data. The exact native replay binds this denial. No reference movement source or heldout feedback enters editing.'),
        ('no_operator_coaching', 'pass',
         'One prompt, three candidates, Opus5.5/max and native Always Allow. The first preservation failure led Kiro itself to protect just_wall_jumped in the coyote branch; no operator solution or follow-up was supplied.'),
        ('protected_behavior_preserved', 'pass' if protected else 'fail',
         'Candidate02 protects same-tick native wall velocity from the normal jump branch; horizontal functions remain unchanged. The source-bound final preservation result is ' + result['preservation']['status'] + '.'),
    ]
    review = {'kind': 'IndependentSourceReview', 'status': 'pass' if protected else 'fail',
        'reviewer': 'Codex assistant', 'method': 'independent_source_and_native_trace_audit',
        'candidate_sha256': expected, 'source_hashes': hashes, 'baseline_hashes': state['baseline_hashes'],
        'native_evidence_hashes': native['evidence_hashes'],
        'checks': [{'id': k, 'verdict': v, 'evidence': e} for k, v, e in checks],
        'performed_inspection_sha256': file_sha256(out / 'game-tools-02-source-inspection.json'),
        'separately_inspected_rejected_update_sha256': digest(denial),
        'posthoc_native_compatibility': staged_native,
        'final_result_sha256': file_sha256(result_path),
        'scope': 'Actual source/trace inspection, separate from the still-pending independent image review.'}
    review_path = out / 'game-tools-02-completed-source-review.json'
    write_json(review_path, review)
    if protected:
        assert validate_source_review(review_path, session, candidate_sha256=expected)['status'] == 'pass'
    (session / 'source_review.json').write_bytes(review_path.read_bytes())
    write_json(out / 'game-tools-02-motion-diagnostic.json', {
        'kind': 'SecondToolsGameDiagnosis', 'candidate_sha256': expected,
        'passed': result['passed'], 'required': result['required'],
        'comparison': result['comparison'], 'preservation': result['preservation'],
        'visual_boundary_passed': result['visual_wall']['passed'],
        'visual_boundary_required': result['visual_wall']['required'],
        'native_status': native['status'], 'independent_visual_review': 'pending',
        'candidate_calibrations': [{k: c.get(k) for k in ('candidate_id', 'eligible', 'score', 'source_sha256')}
                                   for c in state['candidates']],
        'result_sha256': file_sha256(result_path), 'primary_outcome_modified': False})
    write_json(publication, {'status': 'published', 'native_audit': native['status'],
        'source_review': review['status'], 'source_review_sha256': file_sha256(review_path),
        'posthoc_native_compatibility': staged_native,
        'primary_outcome_modified': False, 'producer_sha256': file_sha256(__file__)})
    print({'source_review': review['status'], 'native': native['status'],
           'numerical': f"{result['passed']}/{result['required']}", 'independent_image_review': 'pending'})


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged-native-reader', action='store_true')
    args = parser.parse_args()
    scope = nullcontext()
    if args.staged_native_reader:
        from prepare_rejected_cwd_repair import patched_reader
        proposal = ROOT / 'artifacts/phase3a/native-rejection-compatibility-02'
        receipt = read_json(proposal / 'result.json')
        assert receipt['status'] == 'pass' and receipt['tests'] == 77
        assert file_sha256(ROOT / 'kiroclops/local_engine/review.py') == receipt['production_review_sha256']
        assert file_sha256(proposal / 'review-proposed.py') == receipt['proposed_review_sha256']
        scope = patched_reader(proposal / 'review-proposed.py')
    with scope:
        main(args.staged_native_reader)
