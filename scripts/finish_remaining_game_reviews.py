"""Finish already inspected game kits; preserve all original trial outcomes."""
from pathlib import Path
import importlib.util
import json
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json,write_json,file_sha256
from bind_viewed_game_scope_aid import run as bind_scopes
from publish_completed_visual_review import run as publish


def run():
    base=ROOT/'artifacts/phase3a'
    output=base/'completion-remaining-game-review-checks-01'
    assert not output.exists()
    output.mkdir()
    state={'status':'running','rows':[],'primary_outcomes_modified':False}
    def save():write_json(output/'pipeline.json',state)
    path=base/'tools/prepare_local_final_review.py'
    spec=importlib.util.spec_from_file_location('_frozen_game_checker',path)
    checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
    for arm,n in [('baseline','02'),('baseline','03'),('tools','02'),('tools','03')]:
        name=f'game-{arm}-{n}'
        row={'id':name,'status':'binding_or_waiting','started_at':time.time()}
        state['rows'].append(row);save()
        review=base/f'completion-postcohort-reviews-01/{name}-jump'
        prep=read_json(review/'preparation.json')
        if arm=='baseline':
            source=base/f'completion-{name}-scopes-reviewed-01'
            if not source.exists():
                bind_scopes(review,base/f'completion-remaining-game-scopes-02/{name}/scopes',source)
            assert read_json(source/'scope-summary.json')['status']=='pass'
        else:
            source=base/f'completion-{name}-exact-scope-reuse-01'
            deadline=time.monotonic()+4*3600
            while not (source/'reuse-result.json').is_file():
                assert time.monotonic()<deadline,'Exact scope reuse did not finish'
                time.sleep(10)
            reused=read_json(source/'reuse-result.json')
            assert reused['pending_count']==0 and reused['reused_count']>0
        for entry in prep['requests']:
            target=review/'completed-annotations'/entry['directory']/'annotation.json'
            if entry['scope']=='position':
                assert target.is_file()
                continue
            src=source/entry['directory']/'annotation.json'
            assert src.is_file() and not target.exists()
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(src.read_bytes())
            assert file_sha256(src)==file_sha256(target)
        row['status']='checking_frozen_kit';save()
        checked=checker.check(review,review/'completed-annotations',Path(prep['session'])/'source_review.json')
        row.update(visual_status=checked['visual_status'],source_native_status=checked['source_native_status'])
        save()
        assert checked['visual_status']=='pass',name
        receipt=base/f'completion-20260927/{name}-visual-review-publication.json'
        publication=publish(review,receipt)
        row.update(status='published',scopes=publication['scopes'],finished_at=time.time(),receipt_sha256=file_sha256(receipt))
        save();print(name,row['visual_status'],row['source_native_status'],flush=True)
    state['status']='finished';save()


if __name__=='__main__':run()
