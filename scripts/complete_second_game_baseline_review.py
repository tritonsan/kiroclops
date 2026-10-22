"""Close the performed source review and retain actual failed acceptance axes."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine.review import audit_native_run


def main():
    base = ROOT/'artifacts/phase3a'
    session = base/'completion-paired-cohort-01/sessions/game-baseline-02/jump'
    out = base/'completion-20260927'
    publication = out/'game-baseline-02-source-review-publication.json'
    assert not publication.exists() and not (session/'source_review.json').exists()
    progress = read_json(base/'completion-paired-cohort-01/progress.json')
    row = next(r for r in progress['rows'] if r['id']=='game-baseline-02')
    assert row['status']=='failed' and row['stages'][0]['status']=='failed'
    state = read_json(session/'session.json')
    assert state['status']=='finalized' and not state.get('active_job_id')
    assert state['selected_candidate_id']=='candidate-01'
    target = Path(state['target']).resolve()
    source = session/'candidates/candidate-01/source'
    hashes = source_hashes(source)
    selected = '37a9b597aeb6122b648e94432fed996c41c61acee1d833fb27e90bf13f7e4cdd'
    assert digest(hashes)==selected
    assert [k for k in hashes if hashes[k]!=state['baseline_hashes'].get(k)]==['player.gd']
    inspected = read_json(out/'game-baseline-02-preliminary-helper-inspection.json')['reviewed_files']
    writes = {}
    for line in (session/'kiro/run/stdout.jsonl').read_text('utf-8').splitlines():
        update = json.loads(line).get('params',{}).get('update',{})
        if update.get('sessionUpdate')!='tool_call' or update.get('title')!='Write File':
            continue
        args = update['rawInput'];path=Path(args['path']).resolve()
        assert path.is_relative_to(target)
        name=path.relative_to(target).as_posix()
        actual=source/name if name in hashes else target/name
        assert actual.read_text('utf-8')==args['text']
        if name!='player.gd':
            assert file_sha256(actual)==inspected[name]
        writes[name]={'sha256':file_sha256(actual),'tool_call_id':update['toolCallId'],
                      'arguments_sha256':digest(args),'full_source_read_by_assistant':True}
    assert set(writes)==set(inspected)|{'player.gd'}
    native=audit_native_run(session)
    final_path=session/'jobs'/state['final_job_id']/'result.json'
    result=read_json(final_path)
    assert result['passed']==0 and result['required']==12
    assert result['preservation']['status']=='fail' and result['preservation']['failed_cases']==2
    checks=[
        ('actual_code_authorship','pass','All four native Write File bodies match the fully read player.gd and three scratch helpers. Only player.gd changes among application files. No operator candidate edit.'),
        ('no_coordinate_replay','pass','The full controller integrates input-dependent held ascent and gravity with live velocities and floor/ceiling state. No clip, sample table or recorded-coordinate playback. Normal jump speed168px/s, gravity1440px/s² and hold0.28s came through the model-authored code.'),
        ('no_private_reference_access','pass','All native execute arguments and read/search paths were inspected. Helpers segment public calibration/candidate images and compare local tracks. No reference physics source, private oracle or heldout feedback access was found. The terminal native audit binds all outcomes.'),
        ('no_operator_coaching','pass','One initial prompt, one candidate, Claude Opus5.5/max and native Always Allow. No follow-up operator message or solution patch.'),
        ('protected_behavior_preserved','fail','The source calls handle_wall_jump before handle_jump. The live coyote branch does not exclude just_wall_jumped; start_normal_jump overwrites the wall vertical response on the same tick. Final preservation fails wall-coyote-overlap-left and right, steps0..4. Eight visual boundary checks passing do not override the functional failure.'),
    ]
    review={'kind':'IndependentSourceReview','status':'fail','reviewer':'Codex assistant',
        'method':'independent_source_and_native_trace_audit','candidate_sha256':selected,
        'source_hashes':hashes,'baseline_hashes':state['baseline_hashes'],
        'native_evidence_hashes':native['evidence_hashes'],
        'checks':[{'id':k,'verdict':v,'evidence':e} for k,v,e in checks],
        'reviewed_files':writes,'final_result_sha256':file_sha256(final_path),
        'scope':'Actual assistant inspection; source authorship and native audit pass, protected behavior fails. Full independent image review remains pending.'}
    review_path=out/'game-baseline-02-completed-source-review.json'
    write_json(review_path,review)
    (session/'source_review.json').write_bytes(review_path.read_bytes())
    numerical=result['comparison']
    diagnostic={'kind':'SecondBaselineGameDiagnosis','candidate_sha256':selected,
        'passed':0,'required':12,'mean_rmse_body':numerical['mean_rmse_body'],
        'scenario_checks':numerical['scenario_checks'],'preservation':result['preservation'],
        'visual_boundary_passed':result['visual_wall']['passed'],
        'visual_boundary_required':result['visual_wall']['required'],
        'native_status':native['status'],'independent_visual_review':'pending',
        'source_scale_note':'The baseline helper explicitly selects an8px unsquashed crate; the registered fixed annotation is7px. Task-scale ambiguity limits attribution of pure tool benefit.',
        'result_sha256':file_sha256(final_path),'primary_outcome_modified':False}
    write_json(out/'game-baseline-02-motion-diagnostic.json',diagnostic)
    write_json(publication,{'status':'published_failed_source_review','native_audit':'pass',
        'source_authorship':'pass','protected_behavior':'fail','reviewed_source_files':4,
        'source_review_sha256':file_sha256(review_path),'primary_outcome_modified':False,
        'producer_sha256':file_sha256(__file__)})
    print({'native':'pass','source_authorship':'pass','preservation':'fail','numeric':'0/12',
           'independent_image_review':'pending'})


if __name__=='__main__':
    main()
