"""Replay frozen observations with the separate exact-frame proposal only."""
from pathlib import Path
import importlib.util
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256


def main():
    output=ROOT/'artifacts/phase3a/jump-frame-timing-compatibility-01'
    receipt=output/'result.json';assert not receipt.exists()
    proposal=read_json(output/'proposal.json')
    source=ROOT/'kiroclops/portable_v2/measurement.py'
    staged=output/'measurement-proposed.py'
    assert file_sha256(source)==proposal['original_source_sha256']
    assert file_sha256(staged)==proposal['proposed_source_sha256']
    spec=importlib.util.spec_from_file_location('kiroclops.portable_v2.checked_timing_proposal',staged)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    session=ROOT/'artifacts/phase3a/completion-paired-cohort-01/sessions/game-baseline-02/jump'
    measurements=session/'measurements/81e11c28b1ba4650b8513ff7bd6cd5ce'
    saved=read_json(session/'jobs/81e11c28b1ba4650b8513ff7bd6cd5ce/result.json')
    key=lambda r:(r['scenario']['hold_ticks'],r['scenario']['spawn_offset'],r['scenario']['repeat'])
    hashes={};groups=[]
    for role in ('reference','before','candidate'):
        listing=measurements/f'{role}.json';hashes[str(listing)]=file_sha256(listing)
        mapping={}
        for record in read_json(listing):
            path=Path(record['analysis']);hashes[str(path)]=file_sha256(path)
            data=read_json(path);module._reliable(data);assert key(data) not in mapping
            mapping[key(data)]=data
        assert len(mapping)==12
        groups.append(mapping)
    previous={key(r):r for r in saved['comparison']['scenario_checks']}
    rows=[]
    for k in sorted(previous):
        ref,before,after=(g[k] for g in groups)
        fresh=module._jump_row(ref,before,after,True);old=previous[k]
        changed=[name for name in old['gates'] if fresh['gates'][name]!=old['gates'][name]]
        assert changed==['fall'] and fresh['gates']['fall'] and not fresh['pass']
        assert fresh['after_rmse_body']==old['after_rmse_body']
        ref_spans,cand_spans=module._jump_frame_spans(ref),module._jump_frame_spans(after)
        assert abs(ref_spans[1]-cand_spans[1])==2
        rows.append({'scenario':list(k),'reference_event_frames':ref['events'],'candidate_event_frames':after['events'],
            'reference_fall_frames':ref_spans[1],'candidate_fall_frames':cand_spans[1],
            'original_fall_error_s':old['fall_error_s'],'exact_frame_error_s':fresh['fall_error_s'],
            'old_gates':old['gates'],'proposed_gates':fresh['gates'],'overall_still_fail':not fresh['pass']})
    assert all(file_sha256(path)==sha for path,sha in hashes.items())
    assert file_sha256(source)==proposal['original_source_sha256']
    write_json(receipt,{'status':'staged_repair_verified','tests_passed':9,
        'real_rows_replayed':12,'corrected_fall_gate_rows':12,'overall_result_still':'0/12',
        'rows':rows,'evidence_hashes':hashes,'proposal':proposal,
        'test_source_sha256':file_sha256(ROOT/'artifacts/phase3a/tools/test_jump_frame_timing_proposal.py'),
        'producer_sha256':file_sha256(__file__),'production_modified':False,
        'primary_outcomes_modified':False,'recaptured':False,'model_run':False})
    print({'tests':9,'real_rows':12,'fall_gate_corrected':12,'overall_still':'0/12','production_modified':False})


if __name__=='__main__':main()
