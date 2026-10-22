"""Bind the actual47-call/eight-file inspection; never approve unseen sources."""
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
SESSION=BASE/'completion-paired-cohort-01/sessions/svg-tools-03/svg'
OUT=BASE/'completion-20260927'
NOTE=OUT/'svg-tools-03-sealed-source-inspection.json'
EXPECTED='8ba5843759fa4dff159bf82c20a8ba46f78b7932064770c6ca8c90150b0d756a'
NAMES={'src/layout-motion.js',*('.kiroclops-scratch/'+n for n in
    ('peek.py','traj.py','dump.py','show.py','fit.py','fit2.py','fb.py'))}


def inspected_binding():
    state=read_json(SESSION/'session.json')
    assert state.get('sealed_at') and state['selected_candidate_id']=='candidate-01'
    assert len(state['candidates'])==1
    target=Path(state['target']).resolve()
    rows,tail=calls(SESSION/'kiro/run/stdout.jsonl')
    assert not tail and len(rows)==47,'Additional native calls need actual inspection'
    assert rows[-1]['tool_call_id']=='toolu_bdrk_01Kb1mrXHsLK54vApw5Y9zm4'
    args={r['tool_call_id']:[digest(a) for a in r['arguments']] for r in rows}
    prior_path=OUT/'svg-tools-03-source-inspection-through41.json'
    prior=read_json(prior_path)
    assert dict(list(args.items())[:41])==prior['inspected_native_call_arguments']
    writes,authors,snapshots={},{},[]
    for row in rows:
        if row['title'] in ('Write File','Replace in File'):
            a=row['arguments'][-1];p=Path(a['path']).resolve();assert p.is_relative_to(target)
            name=p.relative_to(target).as_posix()
            if row['title']=='Write File':writes[name]=a['text']
            else:
                assert writes[name].count(a['oldStr'])==1
                writes[name]=writes[name].replace(a['oldStr'],a['newStr'],1)
            authors.setdefault(name,[]).append({'tool_call_id':row['tool_call_id'],
                'argument_versions_sha256':args[row['tool_call_id']]})
        if row['title']=='@kiroclops/evaluate_candidate':
            source=SESSION/'candidates'/f'candidate-{len(snapshots)+1:02d}'/'source'
            hashes=source_hashes(source)
            assert (source/'src/layout-motion.js').read_text('utf-8')==writes['src/layout-motion.js']
            assert sorted(k for k in set(hashes)|set(state['baseline_hashes'])
                if hashes.get(k)!=state['baseline_hashes'].get(k))==['src/layout-motion.js']
            snapshots.append({'candidate_id':source.parent.name,'source_sha256':digest(hashes),
                'controller_sha256':file_sha256(source/'src/layout-motion.js')})
    assert len(snapshots)==1 and snapshots[0]['source_sha256']==EXPECTED
    assert set(writes)==NAMES
    source=SESSION/'candidates/candidate-01/source';files={}
    for name,body in writes.items():
        path=(source if name=='src/layout-motion.js' else target)/name
        assert path.read_text('utf-8')==body,name
        files[name]={'sha256':file_sha256(path),'native_writes':authors[name],
            'full_source_read_by_assistant':True}
    return {'candidate_sha256':EXPECTED,'source_hashes':source_hashes(source),
        'reviewed_files':files,'source_snapshots':snapshots,'inspected_native_call_arguments':args,
        'partial_inspection_sha256':file_sha256(prior_path)}


def run(record=False,staged=False):
    data=inspected_binding()
    findings=read_json(OUT/'svg-tools-03-source-inspection-through41.json')['findings']+[
        'Calls42–47 were also fully inspected. fb.py reads only public calibration feedback and prints comparison keys/metrics. All eight written files reproduce exactly from native Write bodies. One candidate was evaluated and finalized; no operator follow-up or solution edit.',
        'Actual synchronous fits returned sub120 RMSE.3109px, sub240.2111px, sub480.2265px and sub1000.2508px. The chosen240Hz integration follows live targets with k143.9/c15.88. These diagnostic fits do not replace heldout acceptance or independent pixel review.',
        'The Node simulation printed finite final coordinates with the dragged node unchanged, but it contains no explicit assertions. The containing command failed on absent fit2.out, not a shown JavaScript execution failure. Full application-preservation evidence is required separately.'
    ]
    if record:
        assert not NOTE.exists()
        write_json(NOTE,{**data,'kind':'PerformedSourceInspection','findings':findings,
            'acceptance_granted':False,'producer_sha256':file_sha256(__file__)})
        print({'inspection':'recorded','calls':47,'files':8,'acceptance_granted':False});return
    saved=read_json(NOTE);assert all(saved[k]==v for k,v in data.items())
    state=read_json(SESSION/'session.json');launch=read_json(SESSION/'kiro/launch.json')
    row=next(r for r in read_json(BASE/'completion-paired-cohort-01/progress.json')['rows'] if r['id']=='svg-tools-03')
    assert row['status'] in ('failed','completed') and launch.get('finished_at')
    assert not state.get('active_job_id')
    native=review.audit_native_run(SESSION)
    final_path=SESSION/'jobs'/state['final_job_id']/'result.json'
    if state['final']['status']!='succeeded' or not final_path.is_file():
        path=OUT/'svg-tools-03-native-only-audit.json';assert not path.exists()
        write_json(path,{'kind':'TerminalNativeAuditOnly','native':native,
            'final_job_status':state['final']['status'],'performed_source_inspection_sha256':file_sha256(NOTE),
            'full_source_or_image_acceptance':False,'posthoc_native_compatibility':staged,
            'primary_outcome_modified':False,'producer_sha256':file_sha256(__file__)})
        print({'native':native['status'],'full_final_measurement':'unavailable','full_acceptance':False});return
    result=read_json(final_path);assert len(result['comparisons'])==12
    protected=all(c['functional']['status']=='pass' and c['svg_geometry_preservation']['pass'] for c in result['comparisons'])
    checks=[('actual_code_authorship','pass','All47 argument sets and eight full files were read. Native writes reproduce the single immutable candidate. Only layout-motion.js changes among application files.'),
        ('no_coordinate_replay','pass',findings[2]),
        ('no_private_reference_access','pass',findings[1]+' '+findings[5]),
        ('no_operator_coaching','pass','One fresh native Opus5.5/max Always Allow prompt, one candidate, self-authored helpers and no operator solution or live guidance.'),
        ('protected_behavior_preserved','pass' if protected else 'fail',
            'App rendering, structure, input and styles retain original hashes. All12 bound functional and SVG geometry checks '+('pass.' if protected else 'do not pass.'))]
    verdict={'kind':'IndependentSourceReview','status':'pass' if protected else 'fail',
        'reviewer':'Codex assistant','method':'independent_source_and_native_trace_audit',
        'candidate_sha256':EXPECTED,'source_hashes':data['source_hashes'],'baseline_hashes':state['baseline_hashes'],
        'native_evidence_hashes':native['evidence_hashes'],
        'checks':[{'id':k,'verdict':v,'evidence':e} for k,v,e in checks],
        'performed_inspection_sha256':file_sha256(NOTE),'final_result_sha256':file_sha256(final_path),
        'posthoc_native_compatibility':staged,'scope':'Actual source/native inspection; independent image accuracy is separate.'}
    path=OUT/'svg-tools-03-completed-source-review.json';canonical=SESSION/'source_review.json'
    receipt=OUT/'svg-tools-03-source-review-publication.json'
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
