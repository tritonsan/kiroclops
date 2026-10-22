"""Bind inspected baseline pixels; retain its motion and preservation failure."""
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'artifacts/phase3a/tools')]
from kiroclops.visual.schema import read_json,write_json,file_sha256
from prepare_local_final_review import check


def main():
    base=ROOT/'artifacts/phase3a'
    review=base/'completion-game-baseline-01-jump-review-01'
    scopes=base/'completion-game-baseline-01-jump-scopes-reviewed-01'
    summary=read_json(scopes/'scope-summary.json')
    assert summary['status']=='pass' and summary['records']==44
    dest=review/'completed-annotations/records'
    assert not dest.exists() and not (review/'completed-source-review.json').exists()
    shutil.copytree(scopes/'records',dest)
    source=read_json(review/'unsigned-source-review.json')
    source.update(status='fail',reviewer='Codex assistant',external_human_review=False,
        actual_model='claude-opus-5.5',actual_effort='max',
        note='Source request contains a historical Sol label. Actual bound launch/configuration proves Opus5.5 max. This review retains the observed protected-behavior failure.')
    evidence={
        'actual_code_authorship': 'Read complete player.gd diff and native tool trace. Calls36/37 write player.gd and add hazard-phase reset. Only player.gd differs among GDScript source files. Scratch tracking/fitting helpers are independently authored native writes, not an operator patch.',
        'no_coordinate_replay': 'Source integrates input-dependent held ascent and gravity; it contains no position table or frame playback. It uses viewport-height scaling180/128, which explains the mismatch with body-normalized acceptance. Real input changes the trajectory.',
        'no_private_reference_access': 'Inspected all52 hook decisions and native tool call metadata. Reads are target source/configuration or published calibration media. Scratch helpers measure those images. One compound polling/hash shell command was denied. Production native audit binds the full stream; no private reference source or heldout feedback access is established.',
        'no_operator_coaching': 'Exactly one initial native prompt. Bound launch proves Opus5.5 max, native Always Allow, no silent model substitution. Two candidates submitted autonomously; the model selected candidate01. No operator code edit or follow-up coaching was supplied.',
        'protected_behavior_preserved': 'FAIL. Read source calls handle_wall_jump before handle_jump. A live coyote timer lets handle_jump call start_normal_jump even when just_wall_jumped is true, overwriting wall-jump vertical response on that same tick. Frozen preservation results fail wall-coyote-overlap-left and right. Visible ordinary wall bounce and landing do not override this functional failure.'}
    for item in source['checks']:
        item.update(verdict='fail' if item['id']=='protected_behavior_preserved' else 'pass',evidence=evidence[item['id']])
    write_json(review/'completed-source-review.json',source)
    result=check(review,review/'completed-annotations',review/'completed-source-review.json')
    assert result['visual_status']=='pass' and result['source_native_status']=='fail'
    receipt={'status':'measurement_verified_motion_and_preservation_failed','records':44,
        'position_controls':743,'sequence_event_relationship_checks':394,
        'visual_review_sha256':result['visual_receipt_sha256'],
        'check_result_sha256':file_sha256(review/'check-result.json'),
        'manual_source_review_sha256':file_sha256(review/'completed-source-review.json'),
        'heldout_passed':0,'heldout_required':12,'preservation':'fail',
        'primary_cohort_outcome_modified':False,'session_modified':False,
        'external_human_review':False,'normalization_confound':True,
        'scope':'Assistant inspection with raw glyph/geometry aid. Measurement reliability passes; behavior success does not.'}
    write_json(review/'review-summary.json',receipt)
    print(receipt,flush=True)


if __name__=='__main__':main()
