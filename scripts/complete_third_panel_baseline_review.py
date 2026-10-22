"""Bind the eight files and43 calls actually inspected for this single trial."""
from contextlib import nullcontext
from pathlib import Path
import argparse
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine import review
from show_native_calls import calls

BASE=ROOT/'artifacts/phase3a'
SESSION=BASE/'completion-paired-cohort-01/sessions/panel-baseline-03/panel'
OUT=BASE/'completion-20260927'
NOTE=OUT/'panel-baseline-03-sealed-source-inspection.json'
EXPECTED='680018e4bcf8c3f3a91b3fac8ea9eb824422f0fc4ced2a77beaad7f4e38f1ea0'
NAMES={'src/panel-motion.js',*('.kiroclops-scratch/'+n for n in
    ('track.py','show.py','fit.py','sim.mjs','resid.py','compare.py','phase.py'))}
FINDINGS=[
    'All43 native argument sets, the complete selected controller, seven helpers and the tracking replacement were read. Write/Replace bodies reproduce the eight final files exactly. Only src/panel-motion.js changes among application sources.',
    'The controller advances three analytic damped springs from live position, velocity, goal and dt. Panel k159.9/c17.52, title229.3/20.82 and content196.5/18.61 were fitted from public pixels. Opening delays.080/.160 and closing delays.120/.040 split a frame at the actual target switch. Retargets retain velocity; drag yields horizontal ownership to unchanged app.js. No coordinate table or scenario playback.',
    'track.py uses alpha-weighted RGB segmentation of public calibration PNGs, refined to the dominant high-alpha patch. show.py subtracts the visible target initial offsets. fit.py implements its own NumPy simplex and damped response; resid.py imports that inspected fit and checks residuals. compare.py and phase.py independently compare public candidate PNGs and input/frame timing. No reference source, private oracle or heldout feedback was consumed.',
    'Call16 combined scratch-directory creation with neutral polling and was denied before execution. Call17 polls normally. Kiro recovered without operator guidance. One fresh Opus5.5/max native Always Allow prompt, one submitted candidate, no operator solution edit or follow-up.',
    'sim.mjs checks finite values during a nominal open/close loop including one zero-dt step and prints an interruption end state. Its drag-like comment does not correspond to a real dragging=true test; it also starts initially open without the browser warmup. Therefore it is not treated as full interaction evidence. Selected-candidate bound functional checks are required separately.'
]


def binding():
    state=read_json(SESSION/'session.json')
    assert state.get('sealed_at') and state['selected_candidate_id']=='candidate-01'
    target=Path(state['target']).resolve()
    rows,tail=calls(SESSION/'kiro/run/stdout.jsonl')
    assert not tail and len(rows)==43,'New native calls need actual inspection'
    assert rows[-1]['tool_call_id']=='run_command_toolu_bdrk_01691X8TuHxhXv4apMaDbbF1'
    args={r['tool_call_id']:[digest(a) for a in r['arguments']] for r in rows}
    prior=read_json(OUT/'panel-baseline-03-source-inspection-through27.json')
    assert dict(list(args.items())[:27])==prior['inspected_native_call_arguments']
    source=SESSION/'candidates/candidate-01/source'
    hashes=source_hashes(source); assert digest(hashes)==EXPECTED
    assert sorted(k for k in set(hashes)|set(state['baseline_hashes'])
        if hashes.get(k)!=state['baseline_hashes'].get(k))==['src/panel-motion.js']
    writes,authors={},{}
    for row in rows:
        if row['title'] not in ('Write File','Replace in File'):continue
        a=row['arguments'][-1];p=Path(a['path']).resolve();assert p.is_relative_to(target)
        name=p.relative_to(target).as_posix()
        if row['title']=='Write File':writes[name]=a['text']
        else:
            assert writes[name].count(a['oldStr'])==1
            writes[name]=writes[name].replace(a['oldStr'],a['newStr'],1)
        authors.setdefault(name,[]).append({'tool_call_id':row['tool_call_id'],
            'argument_versions_sha256':args[row['tool_call_id']]})
    assert set(writes)==NAMES
    files={}
    for name,body in writes.items():
        path=(source if name in hashes else target)/name
        assert path.read_text('utf-8')==body,name
        files[name]={'sha256':file_sha256(path),'native_writes':authors[name],
            'full_source_read_by_assistant':True}
    return {'candidate_sha256':EXPECTED,'source_hashes':hashes,'reviewed_files':files,
        'inspected_native_call_arguments':args}


def run(record=False,staged=False):
    data=binding()
    if record:
        assert not NOTE.exists()
        write_json(NOTE,{**data,'kind':'PerformedSourceInspection','findings':FINDINGS,
            'acceptance_granted':False,'producer_sha256':file_sha256(__file__)})
        print({'inspection':'recorded','calls':43,'files':8,'acceptance_granted':False});return
    saved=read_json(NOTE)
    assert all(saved[k]==v for k,v in data.items())
    state=read_json(SESSION/'session.json');launch=read_json(SESSION/'kiro/launch.json')
    row=next(r for r in read_json(BASE/'completion-paired-cohort-01/progress.json')['rows']
        if r['id']=='panel-baseline-03')
    assert row['status'] in ('failed','completed') and launch.get('finished_at')
    assert not state.get('active_job_id')
    native=review.audit_native_run(SESSION)
    final_path=SESSION/'jobs'/state['final_job_id']/'result.json'
    if state['final']['status']!='succeeded' or not final_path.is_file():
        path=OUT/'panel-baseline-03-native-only-audit.json';assert not path.exists()
        write_json(path,{'kind':'TerminalNativeAuditOnly','native':native,
            'final_job_status':state['final']['status'],'performed_source_inspection_sha256':file_sha256(NOTE),
            'full_source_or_image_acceptance':False,'posthoc_native_compatibility':staged,
            'primary_outcome_modified':False,'producer_sha256':file_sha256(__file__)})
        print({'native':native['status'],'final_measurement':'unavailable','full_acceptance':False});return
    result=read_json(final_path);assert len(result['comparisons'])==12
    protected=all(c['functional']['status']=='pass' for c in result['comparisons'])
    checks=[('actual_code_authorship','pass',FINDINGS[0]),
        ('no_coordinate_replay','pass',FINDINGS[1]),
        ('no_private_reference_access','pass',FINDINGS[2]+' Complete native audit passed.'),
        ('no_operator_coaching','pass',FINDINGS[3]),
        ('protected_behavior_preserved','pass' if protected else 'fail',FINDINGS[4]+
            ' All selected-candidate functional checks '+('pass.' if protected else 'do not pass.'))]
    verdict={'kind':'IndependentSourceReview','status':'pass' if protected else 'fail',
        'reviewer':'Codex assistant','method':'independent_source_and_native_trace_audit',
        'candidate_sha256':EXPECTED,'source_hashes':data['source_hashes'],
        'baseline_hashes':state['baseline_hashes'],'native_evidence_hashes':native['evidence_hashes'],
        'checks':[{'id':k,'verdict':v,'evidence':e} for k,v,e in checks],
        'performed_inspection_sha256':file_sha256(NOTE),'final_result_sha256':file_sha256(final_path),
        'posthoc_native_compatibility':staged,
        'scope':'Actual full source/native review; numerical fidelity and independent image review are separate.'}
    path=OUT/'panel-baseline-03-completed-source-review.json';canonical=SESSION/'source_review.json'
    receipt=OUT/'panel-baseline-03-source-review-publication.json'
    assert not any(p.exists() for p in (path,canonical,receipt))
    write_json(path,verdict)
    if protected:assert review.validate_source_review(path,SESSION,candidate_sha256=EXPECTED)['status']=='pass'
    canonical.write_bytes(path.read_bytes())
    write_json(receipt,{'status':'published','native':native['status'],'source_review':verdict['status'],
        'source_review_sha256':file_sha256(path),'numeric_passed':result['passed'],'numeric_required':result['required'],
        'posthoc_native_compatibility':staged,'primary_outcome_modified':False,'independent_visual_review':'pending',
        'producer_sha256':file_sha256(__file__)})
    print({'native':native['status'],'source':verdict['status'],'numeric':f"{result['passed']}/{result['required']}",'visual':'pending'})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--record-inspection',action='store_true')
    p.add_argument('--staged-native-reader',action='store_true');a=p.parse_args();scope=nullcontext()
    if a.staged_native_reader:
        from stage_native_completion_repair import patched_reader
        proposal=BASE/'native-completion-compatibility-01'
        original=read_json(proposal/'result.json');combined=read_json(proposal/'combined-regression-02.json')
        assert original['status']==combined['status']=='pass' and combined['tests']==104
        for name in ('review','acceptance'):
            assert file_sha256(ROOT/f'kiroclops/local_engine/{name}.py')==original[f'production_{name}_sha256']
            assert file_sha256(proposal/f'{name}-proposed.py')==original[f'proposed_{name}_sha256']
        assert file_sha256(ROOT/'scripts/stage_native_completion_repair.py')==combined['runtime_adapter_sha256']
        scope=patched_reader(proposal)
    with scope:run(a.record_inspection,a.staged_native_reader)
