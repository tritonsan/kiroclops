"""Separate raw-image inspection aid for the exact two original-level routes.

No position/velocity or landing flag from the engine selects a picture. Public
input neighborhoods, distinct image controls and image-only glyph candidates
support actual visual review. Functional/native checks remain separate.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import numpy as np

from scripts import phase3d_v2_game_supplemental as supplemental
from scripts import prepare_game_raw_review_aid as glyph
from kiroclops.local_engine import suite_game_demo, suite_raw_review
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.package import read_image
from kiroclops.visual.schema import VisualError,digest,file_sha256,read_json,safe_path,write_json

VERSION='unchanged-game-normal-raw-v1'


def _need(value,message,code='incomplete_visual_review'):
    if not value:raise VisualError(code,message)


def _code_sources():
    return {str(Path(m.__file__).resolve()):file_sha256(m.__file__)
        for m in (glyph,supplemental,suite_game_demo,suite_raw_review)} | {
        str(Path(__file__).resolve()):file_sha256(__file__)}


def prepare(supplemental_directory,output):
    directory,output=map(_ordinary,(supplemental_directory,output))
    _need(not output.exists(),'Preserve earlier normal visual reviews.','output_exists')
    _need(not output.is_relative_to(directory) and not directory.is_relative_to(output),'Review output overlaps producer.','unsafe_path')
    result=supplemental.verify(directory)
    _need(result['status']=='pass','Supplemental wall/normal checks have not passed.')
    source=result['source_sha256'];sources=_code_sources()
    normal=suite_game_demo.verify(directory/'normal-level',source_sha256=source)
    output.mkdir(parents=True)
    inventory={str(directory/'result.json'):file_sha256(directory/'result.json'),
        str(directory/'receipt.json'):file_sha256(directory/'receipt.json'),
        str(directory/'normal-level/result.json'):file_sha256(directory/'normal-level/result.json')}
    sheets,records=[],[]
    for route in normal['scenarios']:
        name=route['id'];run=_ordinary(route['raw_run']);manifest=read_json(run/'manifest.json')
        _need(len(manifest['frames'])==240,'The full fixed recording is required.')
        rows=[]
        # Complete raw pass first. Source telemetry/coordinates never enter the
        # glyph estimator or contact-frame selection.
        for index,frame in enumerate(manifest['frames']):
            path=run/'frames'/f'{index:06d}.png'
            sha=file_sha256(path)
            _need(sha==manifest['raw_frame_hashes'][path.name],'Normal source PNG changed.','source_changed')
            inventory[str(path)]=sha
            found=glyph.anchor(read_image(path))
            point=found[0] if len(found)==1 else {'status':'ambiguous' if found else 'not_detected','count':len(found)}
            rows.append({'index':index,'time_s':frame['time_s'],'image_sha256':sha,'point':point})
        reasons=defaultdict(set)
        def near(index,reason):
            for i in range(max(0,index-2),min(240,index+3)):reasons[i].add(reason)
        unique={}
        for row in rows:unique.setdefault(row['image_sha256'],row['index'])
        distinct=list(unique.values())
        _need(len(distinct)>=20,'At least20 distinct source images are required for each route.')
        for value in sorted({round(i) for i in np.linspace(0,len(distinct)-1,20)}):
            reasons[distinct[value]].add('distinct_raw_control')
        for event in route['scenario']['events']:
            near(event['tick'],'public_input:'+event['action']+(' press' if event['pressed'] else ' release'))
        # These are image-space candidates, never claimed engine landing/brake
        # events. Camera or glyph ambiguity can make the candidate unavailable.
        for i in range(1,239):
            section=rows[i-1:i+2]
            if all('y' in r['point'] for r in section):
                a,b,c=(r['point']['y'] for r in section)
                if abs(b-a)>=1 and abs(c-b)>=1 and (b-a)*(c-b)<0:near(i,'image_vertical_turn_candidate')
            if i>=36:
                section=rows[i-6:i+1]
                if all('x' in r['point'] for r in section):
                    points=[r['point'] for r in section]
                    if max(p['x'] for p in points)-min(p['x'] for p in points)<=1 \
                        and max(p['y'] for p in points)-min(p['y'] for p in points)<=1:
                        if rows[i-7]['point'].get('x')!=points[0]['x']:near(i,'image_rest_candidate')
        near(0,'clip_boundary');near(239,'clip_boundary')
        tiles=[];tile_ids={};selected_images=[]
        for i in sorted(reasons):
            row=rows[i];image=read_image(run/'frames'/f'{i:06d}.png')
            key=(row['image_sha256'],digest(row['point']))
            if key in tile_ids:
                selected_images.append(dict(row,reasons=sorted(reasons[i]),tile_index=tile_ids[key]))
                continue
            tile_ids[key]=len(tiles)
            selected_images.append(dict(row,reasons=sorted(reasons[i]),tile_index=tile_ids[key]))
            # Label/overlay the independently detected glyph only when unique.
            from kiroclops.local_engine.suite_generic_raw_review import _tile
            geometry={'points':{'character':row['point']},'objects':{},'connections':{}}
            tiles.append(_tile(image,{'index':i,'time_s':row['time_s']},name,geometry,'game'))
        for page,start in enumerate(range(0,len(tiles),12)):
            batch=tiles[start:start+12]
            batch.extend(np.full((230,600,3),24,np.uint8) for _ in range((-len(batch))%3))
            relative=f'sheets/{name}-{page:02}.png'
            suite_raw_review._save_image(output/relative,np.vstack([np.hstack(batch[i:i+3]) for i in range(0,len(batch),3)]))
            sheets.append({'path':relative,'sha256':file_sha256(output/relative)})
        for path in (run/'manifest.json',run/'recording.json',run/'capture_config.json',
                     Path(route['video']),Path(route['video']).with_suffix('.source.json')):
            inventory[str(path)]=file_sha256(path)
        records.append({'route':name,'mandatory':name=='normal-combined','raw_run':str(run),
            'source_sha256':source,'manifest_sha256':file_sha256(run/'manifest.json'),
            'video_sha256':route['video_sha256'],'frames':rows,'distinct_image_count':len(distinct),
            'selected_images':selected_images,'unique_tile_count':len(tiles),
            'dedup_key':['image_sha256','independent_glyph_proposal_sha256'],
            'public_input_timeline':route['scenario']['events'],'physics_landing_inferred':False,
            'functional_status':route['gameplay']['status']})
    _need(sources==_code_sources(),'Normal review code changed.','source_changed')
    for path,sha in inventory.items():_need(file_sha256(path)==sha,'Normal evidence changed.','source_changed')
    aid={'kind':'UnsignedNormalLevelRawReviewAid','version':VERSION,'status':'pending_visual_review',
        'numeric_status':'pass','unsupported':[],'source_sha256':source,'supplemental':str(directory),
        'supplemental_result_sha256':digest(result),'records':records,'sheets':sheets,
        'production_coordinates_used_for_proposals':False,'production_events_used_for_contact_selection':False,
        'visual_inspection_claimed':False,'full_behavior_acceptance':False,'historical_acceptance_changed':False,
        'scope':'Actual normal-level raster review; engine functional facts are separately verified, not inferred from glyph candidates.'}
    write_json(output/'result.json',aid)
    write_json(output/'evidence-receipt.json',{'kind':'UnsignedRawReviewEvidenceReceipt','status':'unsigned',
        'result_sha256':digest(aid),'source_files':inventory,'code_sources':sources,
        'generated_files':{p.relative_to(output).as_posix():file_sha256(p) for p in sorted(output.rglob('*')) if p.is_file()}})
    return aid


def validate(supplemental_directory,output):
    directory,output=map(_ordinary,(supplemental_directory,output))
    aid,receipt,attestation=(read_json(output/name) for name in ('result.json','evidence-receipt.json','attestation.json'))
    result=supplemental.verify(directory)
    _need(aid['kind']=='UnsignedNormalLevelRawReviewAid' and aid['version']==VERSION
        and aid['numeric_status']=='pass' and aid['supplemental_result_sha256']==digest(result)
        and aid['source_sha256']==result['source_sha256'] and receipt['result_sha256']==digest(aid)
        and attestation['status']=='pass' and attestation['accepted'] is True and attestation['images_inspected'] is True
        and attestation['result_sha256']==digest(aid) and attestation['inspected_sheets']==aid['sheets']
        and attestation['evidence_receipt_sha256']==file_sha256(output/'evidence-receipt.json'),
        'Exact actual normal-level image inspection is required.')
    _need(receipt['code_sources']==_code_sources(),'Normal raw review source changed.','stale_review')
    for path,sha in {**receipt['source_files'],**receipt['code_sources']}.items():
        _need(file_sha256(path)==sha,'Normal raw source changed.','stale_review')
    for name,sha in receipt['generated_files'].items():
        _need(file_sha256(safe_path(output,name))==sha,'Normal raw sheet changed.','stale_review')
    return {'kind':'VerifiedNormalLevelRawInspection','status':'pass','source_sha256':aid['source_sha256'],
        'directory':str(output),'attestation_sha256':file_sha256(output/'attestation.json'),
        'mandatory_route':'normal-combined','slope_route_is_diagnostic':True,'full_behavior_acceptance':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','validate'))
    parser.add_argument('--supplemental',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=(prepare if args.operation=='prepare' else validate)(args.supplemental,args.output)
    print({k:result.get(k) for k in ('kind','status','numeric_status','full_behavior_acceptance')})
    if 'sheets' in result:print('sheets',len(result['sheets']))


if __name__=='__main__':main()
