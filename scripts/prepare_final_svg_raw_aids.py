"""Unsigned raw-pixel aid bridge for a frozen final SVG review kit."""
from pathlib import Path
import argparse
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'artifacts/phase3a/tools')]
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest


def run(review,output):
    review,output=Path(review).resolve(),Path(output).resolve()
    assert not output.exists()
    prepared=read_json(review/'preparation.json')
    assert digest(prepared)==read_json(review/'preparation-binding.json')['sha256']
    entries={e['directory']:e for e in prepared['requests']}
    positions=read_json(review/'positions/svg/request.json')
    assert digest(positions)==entries['positions/svg']['request_sha256']
    write_json(output/'positions/request.json',positions)
    originals=prepared['fingerprint']['records']
    assert len(originals)==len(positions['records'])
    inventory=[]
    for original,requested in zip(originals,positions['records']):
        assert original['mechanic']=='svg'
        assert (original['package_sha256'],original['observations_sha256']) == (
            requested['package_sha256'],requested['observations_sha256'])
        analysis=Path(original['analysis_path'])
        assert file_sha256(analysis)==prepared['fingerprint']['files'][str(analysis)]
        data=read_json(analysis)
        obs=data.get('observations')
        if obs is None:
            path=analysis.parent/'observations.json'
            assert file_sha256(path)==prepared['fingerprint']['files'][str(path)]
            obs=read_json(path)
        assert digest(obs)==original['observations_sha256']
        assert digest(obs['selection'])==original['selection_sha256']
        record_path=output/'inputs'/original['id']/'record.json'
        # Only declared selection/lifetime metadata goes into the aid. It never
        # receives tracker coordinates or event values as pixel-marking input.
        write_json(record_path,{'selection':obs['selection']})
        inventory.append({'id':original['id'],'record_id':requested['id'],
            'role':original['role'],'package':original['package'],
            'package_sha256':original['package_sha256'],'record_path':str(record_path),
            'record_sha256':file_sha256(record_path)})
        for scope in ('events','relationships'):
            relative=f"records/{original['id']}/{scope}"
            request=read_json(review/relative/'request.json')
            assert digest(request)==entries[relative]['request_sha256']
            write_json(output/relative/'request.json',request)
    write_json(output/'inventory.json',{'kind':'UnsignedSelectedSVGBridge',
        'status':'pending_independent_review','records':inventory,
        'source_preparation_sha256':file_sha256(review/'preparation.json'),
        'candidate_sha256':prepared['candidate_sha256'],
        'review_kit':str(review),'producer_sha256':file_sha256(__file__)})
    import raw_svg_holdout_review_aid
    import compare_svg_clock_review
    import svg_lifetime_review_boards
    import svg_clock_event_boards
    raw_svg_holdout_review_aid.run(output)
    compare_svg_clock_review.run(output)
    svg_lifetime_review_boards.run(output)
    svg_clock_event_boards.run(output)
    write_json(output/'unsigned-ready.json',{'status':'unsigned',
        'new_visual_inspection_claimed':False,'acceptance_granted':False,
        'inventory_sha256':file_sha256(output/'inventory.json')})
    return {'status':'unsigned','records':len(inventory),'output':str(output)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--review',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(run(a.review,a.output),flush=True)
