"""Bind the actually inspected59 calls/11 files, separately from image acceptance."""
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
SESSION=BASE/'completion-paired-cohort-01/sessions/svg-baseline-03/svg'
OUT=BASE/'completion-20260927'
NOTE=OUT/'svg-baseline-03-sealed-source-inspection.json'
EXPECTED='3f83c59d3ddf1d6fa9b0f602a27d47e3c01019062d38d3451b8e38c96ae0ed91'
NAMES={'src/layout-motion.js',*('.kiroclops-scratch/'+n for n in
    ('track.py','show.py','fit.py','fit2.py','fit3.py','fit3b.py',
     'compare.py','compare2.py','simcheck.py','difffit.py'))}


def inspected_binding():
    state=read_json(SESSION/'session.json')
    assert state.get('sealed_at') and state['selected_candidate_id']=='candidate-02'
    assert len(state['candidates'])==2
    target=Path(state['target']).resolve()
    rows,tail=calls(SESSION/'kiro/run/stdout.jsonl')
    assert not tail and len(rows)==59,'Additional calls need actual inspection'
    assert rows[-1]['tool_call_id']=='run_command_toolu_bdrk_01ABivHWNHziLKj8g2HzpxvC'
    args={r['tool_call_id']:[digest(a) for a in r['arguments']] for r in rows}
    prior_path=OUT/'svg-baseline-03-source-inspection-through52.json'
    prior=read_json(prior_path)
    assert dict(list(args.items())[:52])==prior['inspected_native_call_arguments']
    writes,authors,snapshots={},{},[]
    for i,row in enumerate(rows,1):
        a=row['arguments'][-1] if row['arguments'] else {}
        if row['title'] in ('Write File','Replace in File'):
            path=Path(a['path']).resolve();assert path.is_relative_to(target)
            name=path.relative_to(target).as_posix()
            if row['title']=='Write File':writes[name]=a['text']
            else:
                assert writes[name].count(a['oldStr'])==1
                writes[name]=writes[name].replace(a['oldStr'],a['newStr'],1)
            authors.setdefault(name,[]).append({'tool_call_id':row['tool_call_id'],
                'argument_versions_sha256':args[row['tool_call_id']]})
        if i==37:
            # This specific command and the complete resulting helper were read.
            # Bind the argument to the prior actual inspection; don't execute it.
            assert row['tool_call_id']=='run_command_toolu_bdrk_012mtcW33N3HqsHfpJXNxMKr'
            body=writes['.kiroclops-scratch/fit3.py']
            old="for kind in ['semi', 'expl', 'verlet', 'impl']:\n    for sub in [1, 2, 4, 8]:"
            assert body.count(old)==body.count('for i in range(0, 40):')==1
            name='.kiroclops-scratch/fit3b.py'
            writes[name]=body.replace(old,"for kind in ['semi']:\n    for sub in [3, 4, 5, 6, 10, 16]:").replace(
                'for i in range(0, 40):','for i in range(0, 0):')+'\n'
            authors[name]=[{'tool_call_id':row['tool_call_id'],
                'argument_versions_sha256':args[row['tool_call_id']],
                'method':'Two inspected PowerShell replacements plus Set-Content trailing newline; normalized text replay.'}]
        if i in (36,51):
            source=SESSION/'candidates'/f'candidate-{len(snapshots)+1:02d}'/'source'
            hashes=source_hashes(source)
            assert (source/'src/layout-motion.js').read_text('utf-8')==writes['src/layout-motion.js']
            assert sorted(k for k in set(hashes)|set(state['baseline_hashes'])
                if hashes.get(k)!=state['baseline_hashes'].get(k))==['src/layout-motion.js']
            snapshots.append({'candidate_id':source.parent.name,'source_sha256':digest(hashes),
                'controller_sha256':file_sha256(source/'src/layout-motion.js')})
    assert snapshots==prior['source_snapshots'] and snapshots[-1]['source_sha256']==EXPECTED
    assert set(writes)==NAMES
    source=SESSION/'candidates/candidate-02/source';files={}
    for name,body in writes.items():
        path=(source if name=='src/layout-motion.js' else target)/name
        assert path.read_text('utf-8')==body,name
        assert file_sha256(path)==prior['full_written_files_read'][name]
        files[name]={'sha256':file_sha256(path),'native_writes':authors[name],
            'full_source_read_by_assistant':True}
    return {'candidate_sha256':EXPECTED,'source_hashes':source_hashes(source),
        'reviewed_files':files,'source_snapshots':snapshots,'inspected_native_call_arguments':args,
        'partial_inspection_sha256':file_sha256(prior_path)}


def run(record=False,staged=False):
    data=inspected_binding()
    findings=read_json(OUT/'svg-baseline-03-source-inspection-through33.json')['findings']+read_json(
        OUT/'svg-baseline-03-source-inspection-through52.json')['findings']+[
        'Calls53–59 were fully read. Kiro reused its own public-image comparison helpers, selected candidate02 and submitted finalization. No later code edit or operator follow-up.',
        'Call57 was a pre-tool-use denied combined public-image diagnostic, reason Outside the registered experiment boundary. Its native terminal has no rawOutput/content and it did not execute. Call58 retries only the allowed local helper. This denial is preserved, not evidence of a private read.',
        'The second candidate own frame-index comparison had one apparent44.92px maximum outlier; three other clips had maximum<=.322px. These model-authored diagnostics are not the timestamp-based independent final metric. No original recording or threshold was changed.'
    ]
    if record:
        assert not NOTE.exists()
        write_json(NOTE,{**data,'kind':'PerformedSourceInspection','findings':findings,
            'acceptance_granted':False,'producer_sha256':file_sha256(__file__)})
        print({'inspection':'recorded','calls':59,'files':11,'acceptance_granted':False});return
    saved=read_json(NOTE);assert all(saved[k]==v for k,v in data.items())
    state=read_json(SESSION/'session.json');launch=read_json(SESSION/'kiro/launch.json')
    row=next(r for r in read_json(BASE/'completion-paired-cohort-01/progress.json')['rows'] if r['id']=='svg-baseline-03')
    assert row['status'] in ('failed','completed') and launch.get('finished_at')
    assert not state.get('active_job_id')
    native=review.audit_native_run(SESSION)
    final_path=SESSION/'jobs'/state['final_job_id']/'result.json'
    if state['final']['status']!='succeeded' or not final_path.is_file():
        path=OUT/'svg-baseline-03-native-only-audit.json';assert not path.exists()
        write_json(path,{'kind':'TerminalNativeAuditOnly','native':native,
            'final_job_status':state['final']['status'],'performed_source_inspection_sha256':file_sha256(NOTE),
            'full_source_or_image_acceptance':False,'posthoc_native_compatibility':staged,
            'primary_outcome_modified':False,'producer_sha256':file_sha256(__file__)})
        print({'native':native['status'],'full_final_measurement':'unavailable','full_acceptance':False});return
    result=read_json(final_path);assert len(result['comparisons'])==12
    protected=all(c['functional']['status']=='pass' and c['svg_geometry_preservation']['pass'] for c in result['comparisons'])
    checks=[('actual_code_authorship','pass','All59 argument sets and11 full files were inspected. Two immutable candidate controllers match native writes. The shell-created fit3b helper reproduces by its inspected transformation. Only layout-motion.js changes among application files.'),
        ('no_coordinate_replay','pass',findings[7]),
        ('no_private_reference_access','pass','Helpers use public reference/before/candidate calibration PNGs, manifests, own derived tracks and target source. Full native argument audit found no private physics or heldout access during editing. Denied call57 never executed.'),
        ('no_operator_coaching','pass','One fresh native Opus5.5/max Always Allow prompt, two candidates, self-authored helpers and no operator solution or live guidance.'),
        ('protected_behavior_preserved','pass' if protected else 'fail',
            'Application rendering, structure, input and styles retain original hashes. All12 bound functional and SVG geometry checks '+('pass.' if protected else 'do not pass.'))]
    verdict={'kind':'IndependentSourceReview','status':'pass' if protected else 'fail',
        'reviewer':'Codex assistant','method':'independent_source_and_native_trace_audit',
        'candidate_sha256':EXPECTED,'source_hashes':data['source_hashes'],'baseline_hashes':state['baseline_hashes'],
        'native_evidence_hashes':native['evidence_hashes'],
        'checks':[{'id':k,'verdict':v,'evidence':e} for k,v,e in checks],
        'performed_inspection_sha256':file_sha256(NOTE),'final_result_sha256':file_sha256(final_path),
        'posthoc_native_compatibility':staged,'scope':'Actual source/native inspection; independent image accuracy is separate.'}
    path=OUT/'svg-baseline-03-completed-source-review.json';canonical=SESSION/'source_review.json'
    receipt=OUT/'svg-baseline-03-source-review-publication.json'
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
