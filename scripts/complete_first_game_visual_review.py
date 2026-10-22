"""Assemble completed independent marks; original native audit still applies."""
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'artifacts/phase3a/tools')]
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from prepare_local_final_review import check


def main():
    base=ROOT/'artifacts/phase3a'
    review=base/'completion-game-tools-01-jump-review-01'
    scopes=base/'completion-game-tools-01-jump-scopes-reviewed-02'
    summary=read_json(scopes/'scope-summary.json')
    assert summary['status']=='pass' and summary['records']==62
    dest=review/'completed-annotations/records'
    assert not dest.exists() and not (review/'completed-source-review.json').exists()
    shutil.copytree(scopes/'records',dest)
    source=read_json(review/'unsigned-source-review.json')
    source.update(status='pass',reviewer='Codex assistant',external_human_review=False,
        native_gate_note='Manual source findings pass. The original production native auditor remains failing on the documented rejected-delete compatibility issue; this receipt cannot override that gate.',
        compatibility_diagnostic_sha256=file_sha256(base/'completion-20260927/game-tools-01-rejected-delete-proof.json'))
    evidence={
        'actual_code_authorship': 'Native fs_write wrote target/player.gd; native str_replace applied the wall/coyote priority correction. Both writes appear in the bound 42-call stream. The selected candidate02 player.gd and full diff from baseline were read independently; only player.gd changed among GDScript sources.',
        'no_coordinate_replay': 'The inspected source integrates velocity/gravity and caps held input by elapsed physics ticks. It contains no recorded position trajectory, frame table, reference file access, or playback path. Current user input controls jump sustain/release.',
        'no_private_reference_access': 'The bound read/write trace was inspected. The attempted private preservation-report shell read and scratch-file delete were denied before execution, with empty failed terminals. Installed-tool-source-backed diagnostic replay binds all 42 outcomes. The production audit compatibility failure is separately retained, not hidden.',
        'no_operator_coaching': 'Exactly one prepared native session/prompt; the configuration/stream records Opus5.5 max with native session Always Allow. The operator supplied no solution patch or follow-up prompt. Candidate01 failed preservation, and Kiro authored the correction before candidate02 evaluation.',
        'protected_behavior_preserved': 'The full player.gd diff leaves ground and airborne horizontal acceleration/friction paths unchanged. Wall jump and air jump end the new normal-jump phase; just_wall_jumped has priority over coyote/ground start on the same tick. Floor contact ends the phase and ceiling contact ends sustain. Selected candidate preservation and all12 heldout numerical comparisons pass; raw wall sequences visibly show midair redirection and return to ground. Visual proximity is not treated as engine collision proof.'}
    for item in source['checks']:
        item.update(verdict='pass',evidence=evidence[item['id']])
    write_json(review/'completed-source-review.json',source)
    result=check(review,review/'completed-annotations',review/'completed-source-review.json')
    assert result['visual_status']=='pass'
    assert result['source_native_status']=='fail' # documented frozen auditor, no bypass
    receipt={'status':'visual_pass_native_compatibility_blocked','records':62,
        'position_controls':1006,'sequence_event_relationship_checks':488,
        'visual_review_sha256':result['visual_receipt_sha256'],
        'check_result_sha256':file_sha256(review/'check-result.json'),
        'manual_source_review_sha256':file_sha256(review/'completed-source-review.json'),
        'primary_cohort_outcome_modified':False,'session_modified':False,
        'external_human_review':False,
        'scope':'Actual assistant inspection aided by raw glyph/geometry readings; not third-party or manual-click-per-point attestation.'}
    write_json(review/'review-summary.json',receipt)
    print(receipt,flush=True)


if __name__=='__main__':main()
