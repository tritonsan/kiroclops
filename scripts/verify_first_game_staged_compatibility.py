"""Separate post-hoc replay with the source-backed native schema repair.

Never modifies production, the experiment, its registration or primary outcome.
The independently completed visual/source receipts are required, not inferred.
"""
from pathlib import Path
import importlib.util
import sys
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.local_engine import review
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest


def main():
    b=ROOT/'artifacts/phase3a'
    kit=b/'completion-game-tools-01-jump-review-01'
    session=b/'completion-paired-cohort-01/sessions/game-tools-01/jump'
    proposed=b/'native-delete-compatibility-01/review-proposed.py'
    out=b/'completion-game-tools-01-posthoc-compatibility-01'
    assert not out.exists()
    tests=read_json(proposed.with_name('result.json'))
    assert tests['status']=='pass' and tests['tests']==71
    assert file_sha256(review.__file__)==tests['production_review_sha256']
    assert file_sha256(proposed)==tests['proposed_review_sha256']
    original={str(p):file_sha256(p) for p in (
        b/'completion-paired-cohort-01/registration.json',session/'session.json',
        kit/'independent_review.json',kit/'completed-source-review.json',Path(review.__file__))}
    state=read_json(session/'session.json')
    candidate=next(c for c in state['candidates'] if c['candidate_id']==state['selected_candidate_id'])
    selected=candidate['source_sha256']
    visual=review.validate_independent_review(kit/'independent_review.json',candidate_sha256=selected)
    spec=importlib.util.spec_from_file_location('kiroclops.local_engine._review_staged',proposed)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with patch.object(review,'_native_tool_name',module._native_tool_name), \
         patch.object(review,'_native_arguments',module._native_arguments), \
         patch.object(review,'_hook_denied_no_output',module._hook_denied_no_output):
        source=review.validate_source_review(kit/'completed-source-review.json',session,candidate_sha256=selected)
    final=read_json(session/'jobs'/state['final_job_id']/'result.json')
    assert final['pass'] and final['passed']==final['required']==12
    assert final['preservation']['status']=='pass'
    assert all(file_sha256(p)==sha for p,sha in original.items())
    result={'status':'posthoc_engineering_acceptance_pass',
        'primary_preregistered_outcome':'failed_native_compatibility',
        'primary_outcome_modified':False,'production_modified':False,
        'new_model_run':False,'operator_solution_edit':False,
        'candidate_sha256':selected,'numerical':{'passed':12,'required':12,
            'mean_rmse_body':final['comparison']['mean_rmse_body']},
        'visual':{'status':visual['status'],'records':62,'positions':1006,'scope_checks':488,
                  'receipt_sha256':file_sha256(kit/'independent_review.json')},
        'source_native':{'status':'pass','receipt_sha256':file_sha256(kit/'completed-source-review.json'),
                         'native_validation':source['native_validation']},
        'original_production_sha256':tests['production_review_sha256'],
        'proposed_production_sha256':tests['proposed_review_sha256'],
        'repair_test_receipt_sha256':file_sha256(proposed.with_name('result.json')),
        'input_bindings':original,
        'scope':'Explicit post-hoc installed-CLI compatibility replay; not a replacement successful trial. Actual assistant image/source inspection, not independent third-party attestation.'}
    write_json(out/'result.json',result)
    (out/'REPORT.md').write_text('# First tool-assisted game — separate compatibility verification\n\n'
        'The preregistered run is preserved as failed. When the Delete File/targetFile mapping, verified against '
        'the installed CLI source code, is applied in a separate process, the native audit and source review of '
        'the same evidence pass. The production code did not change, the model '
        'was not re-run and the candidate code was not touched.\n\n'
        '- Numerical transfer: 12/12; mean error 0.0732 body.\n'
        '- Preservation checks passed.\n'
        '- Independent assistant review: 62 recordings, 1,006 positions, 488 event/relationship checks.\n'
        '- Real native recording: 42/42 tool calls bound; blocked operations were not executed.\n'
        '- Fix: 71 tests; corrupted-evidence samples are rejected.\n\n'
        '**This additional engineering verification does not increase the primary success count of the 18-trial group.** '
        'The horizontal continuation stage did not run in this first run. There is no full three-domain acceptance yet.\n', 'utf-8')
    print({'status':result['status'],'primary_outcome_modified':False,
           'native_tools':len(source['native_validation']['tool_outcomes']['calls'])},flush=True)


if __name__=='__main__':main()
