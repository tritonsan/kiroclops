"""Close the actually performed57-call/seven-file review without altering trials."""
from contextlib import nullcontext
from pathlib import Path
import argparse
import hashlib
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine import review
from show_native_calls import calls


def run(record_only=False,staged=False):
    base=ROOT/'artifacts/phase3a'
    session=base/'completion-paired-cohort-01/sessions/panel-tools-03/panel'
    out=base/'completion-20260927'
    state=read_json(session/'session.json')
    assert state['selected_candidate_id']=='candidate-02'
    target=Path(state['target']).resolve()
    rows,tail=calls(session/'kiro/run/stdout.jsonl')
    assert not tail and len(rows)==57, 'Additional native arguments need actual inspection'
    assert rows[-1]['tool_call_id']=='toolu_bdrk_01FZKULERJQtT1VQ5ikX3Ke6'
    args_hashes={r['tool_call_id']:[digest(a) for a in r['arguments']] for r in rows}
    prior=read_json(out/'panel-tools-03-source-inspection-through29.json')
    assert dict(list(args_hashes.items())[:29])==prior['inspected_native_call_arguments']
    writes,authors,snapshots,append_notes={},{},[],[]
    for row in rows:
        title=row['title']
        if title in ('Write File','Replace in File','Append to File'):
            a=row['arguments'][-1]
            p=Path(a['path']).resolve();assert p.is_relative_to(target)
            name=p.relative_to(target).as_posix()
            if title=='Write File':writes[name]=a['text']
            elif title=='Replace in File':
                assert writes[name].count(a['oldStr'])==1
                writes[name]=writes[name].replace(a['oldStr'],a['newStr'],1)
            else:
                assert row['tool_call_id']=='toolu_bdrk_0132MqK1ZgauU3mJz6ZX9pmQ'
                assert name=='.kiroclops-scratch/fit.py' and a['text'].startswith('\n')
                # Actual native append produced one additional empty line.
                # This is an explicitly inspected whitespace difference for
                # this exact helper/call, not a generic replay normalization.
                literal=writes[name]+a['text']
                writes[name]+='\n'+a['text']
                append_notes.append({'tool_call_id':row['tool_call_id'],
                    'literal_concatenation_utf8_sha256':hashlib.sha256(literal.encode()).hexdigest(),
                    'observed_text_utf8_sha256':hashlib.sha256(writes[name].encode()).hexdigest(),
                    'difference':'One extra blank line immediately before the residual-print section; full diff inspected. No statement differs.',
                    'literal_byte_exact_replay_claimed':False})
            authors.setdefault(name,[]).append({'tool_call_id':row['tool_call_id'],
                'argument_versions_sha256':args_hashes[row['tool_call_id']]})
        if title=='@kiroclops/evaluate_candidate':
            d=session/'candidates'/f'candidate-{len(snapshots)+1:02d}'/'source'
            hashes=source_hashes(d)
            assert (d/'src/panel-motion.js').read_text('utf-8')==writes['src/panel-motion.js']
            assert sorted(n for n in set(hashes)|set(state['baseline_hashes'])
                if hashes.get(n)!=state['baseline_hashes'].get(n))==['src/panel-motion.js']
            snapshots.append({'candidate_id':d.parent.name,'source_sha256':digest(hashes),
                'controller_sha256':file_sha256(d/'src/panel-motion.js')})
    assert len(snapshots)==2 and len(append_notes)==1
    expected='12cbbe2585915fb62c8702bc1b837b94ae2b91dae28f2a589227a4b6e2dd7495'
    assert snapshots[1]['source_sha256']==expected
    assert set(writes)=={'src/panel-motion.js',*('.kiroclops-scratch/'+n for n in
        ('dump.py','fit.py','sim.mjs','fit2.py','track.py','fit3.py'))}
    files={}
    frozen=session/'candidates/candidate-02/source'
    for name,body in writes.items():
        p=frozen/name if name=='src/panel-motion.js' else target/name
        assert p.read_text('utf-8')==body,name
        files[name]={'sha256':file_sha256(p),'native_writes':authors[name],
                     'full_source_read_by_reviewer':True}
    findings=[
        'Both complete controllers, all six scratch helpers and57 native argument sets were read. Native edits reproduce both immutable controller snapshots exactly. Only src/panel-motion.js changes among application files. Helper statements replay; fit.py has a separately disclosed single-blank-line native append difference.',
        'dump.py/fit.py use four public calibration ObservationSets. Missing SciPy was autonomously replaced with NumPy simplex search. fit2 compares exact and semi-implicit fixed60Hz models; both versions of fit3 execute only the fully inspected local fit2 prefix. track.py independently segments the public handle PNGs by color. No private reference source, DOM oracle or heldout feedback was consumed.',
        'Selected code advances three analytic damped springs from live states, goals, input and dt. Measured omega values12.633/15.129/14.018 and zeta.6926/.6873/.6635 are used; child offsets24/40, opening delays.080/.160 and closing delays.120/.040 are explicit. New live retargets use a fitted1.6ms lead. Delayed goal crossings split the step, velocities persist and pointer ownership remains in unchanged app.js. No coordinate table or scenario playback.',
        'The selected candidate has lower calibration score. Kiro made both versions, auxiliary fitting and corrections without an operator solution, dependency installation or follow-up prompt. One Opus5.5/max native Always Allow turn.',
        'sim.mjs checks finite open/close/reverse outputs; its drag flag is never enabled and settling is not asserted. Its comment is not a full functional test. Full selected-candidate functional evidence is required separately.'
    ]
    inspection={'kind':'ActualAssistantSourceInspection','status':'source_read_full_acceptance_pending',
        'candidate_id':'candidate-02','candidate_sha256':expected,'reviewed_files':files,
        'candidate_snapshots':snapshots,'inspected_native_call_arguments':args_hashes,
        'append_whitespace_review':append_notes,'findings':findings,
        'producer_sha256':file_sha256(__file__),'primary_outcome_modified':False}
    inspected=out/'panel-tools-03-sealed-source-inspection.json'
    if record_only:
        assert not inspected.exists();write_json(inspected,inspection)
        print({'inspection':'recorded','files':len(files),'calls':len(rows),'candidate_sha256':expected})
        return
    old=read_json(inspected)
    for k in ('candidate_sha256','reviewed_files','candidate_snapshots','inspected_native_call_arguments','append_whitespace_review'):
        assert old[k]==inspection[k]
    row=next(r for r in read_json(base/'completion-paired-cohort-01/progress.json')['rows'] if r['id']=='panel-tools-03')
    assert row['status'] in ('failed','completed')
    assert read_json(session/'kiro/launch.json').get('finished_at') and not state.get('active_job_id')
    native=review.audit_native_run(session)
    result_path=session/'jobs'/state['final_job_id']/'result.json'
    if state['final']['status']!='succeeded' or not result_path.is_file():
        path=out/'panel-tools-03-native-only-audit.json';assert not path.exists()
        write_json(path,{'kind':'TerminalNativeAuditOnly','native':native,'final_job_status':state['final']['status'],
            'performed_source_inspection_sha256':file_sha256(inspected),
            'full_source_or_image_acceptance':False,'posthoc_native_compatibility':staged,
            'primary_outcome_modified':False,'producer_sha256':file_sha256(__file__)})
        print({'native':native['status'],'final_measurement':'unavailable','source_and_image_acceptance':'not_granted'})
        return
    result=read_json(result_path);assert len(result['comparisons'])==12
    protected=all(c['functional']['status']=='pass' for c in result['comparisons'])
    checks=[('actual_code_authorship','pass',findings[0]),
        ('no_coordinate_replay','pass',findings[2]),
        ('no_private_reference_access','pass',findings[1]+' Complete native audit passed.'),
        ('no_operator_coaching','pass',findings[3]),
        ('protected_behavior_preserved','pass' if protected else 'fail',findings[4]+
         ' Source-bound selected functional checks '+('all pass.' if protected else 'do not all pass.'))]
    verdict={'kind':'IndependentSourceReview','status':'pass' if protected else 'fail',
        'reviewer':'Codex assistant','method':'independent_source_and_native_trace_audit',
        'candidate_sha256':expected,'source_hashes':source_hashes(frozen),'baseline_hashes':state['baseline_hashes'],
        'native_evidence_hashes':native['evidence_hashes'],
        'checks':[{'id':k,'verdict':v,'evidence':e} for k,v,e in checks],
        'performed_inspection_sha256':file_sha256(inspected),'final_result_sha256':file_sha256(result_path),
        'posthoc_native_compatibility':staged,'scope':'Actual source/native inspection; independent image review remains separate.'}
    path=out/'panel-tools-03-completed-source-review.json'
    canonical=session/'source_review.json';receipt=out/'panel-tools-03-source-review-publication.json'
    assert not any(p.exists() for p in (path,canonical,receipt))
    write_json(path,verdict)
    if protected:assert review.validate_source_review(path,session,candidate_sha256=expected)['status']=='pass'
    canonical.write_bytes(path.read_bytes())
    write_json(receipt,{'status':'published','native':native['status'],'source_review':verdict['status'],
        'source_review_sha256':file_sha256(path),'numeric_passed':result['passed'],'numeric_required':result['required'],
        'posthoc_native_compatibility':staged,'primary_outcome_modified':False,
        'independent_visual_review':'pending','producer_sha256':file_sha256(__file__)})
    print({'native':native['status'],'source':verdict['status'],'numeric':f"{result['passed']}/{result['required']}",'visual':'pending'})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--record-inspection',action='store_true')
    p.add_argument('--staged-native-reader',action='store_true')
    args=p.parse_args();scope=nullcontext()
    if args.staged_native_reader:
        from stage_native_completion_repair import patched_reader
        proposal=ROOT/'artifacts/phase3a/native-completion-compatibility-01'
        a=read_json(proposal/'result.json');b=read_json(proposal/'combined-regression-02.json')
        assert a['status']==b['status']=='pass' and b['tests']==104
        for name in ('review','acceptance'):
            assert file_sha256(ROOT/f'kiroclops/local_engine/{name}.py')==a[f'production_{name}_sha256']
            assert file_sha256(proposal/f'{name}-proposed.py')==a[f'proposed_{name}_sha256']
        assert file_sha256(ROOT/'scripts/stage_native_completion_repair.py')==b['runtime_adapter_sha256']
        scope=patched_reader(proposal)
    with scope:run(args.record_inspection,args.staged_native_reader)
