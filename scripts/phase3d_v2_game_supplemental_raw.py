"""Independent raw review of retained jump pixels and fresh supplemental walls.

This is an explicit mixed-epoch, unchanged-source matrix, not a succeeded
historical Session. All original roles and event/source identities are retained.
Only identical pixels with identical raw proposals share a contact-sheet tile.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import sys

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import numpy as np

from kiroclops.local_engine import evidence, media_suite, suite_evidence, suite_final_raw_review as final
from kiroclops.local_engine import suite_generic_raw_review as raw
from kiroclops.local_engine.session import _ordinary, source_hashes
from kiroclops.visual.package import frame_paths, load_package, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts import phase3d_v2_game_supplemental as supplemental

VERSION='mixed-epoch-game-supplemental-raw-v1'
ROLES=('reference','original','before','selected')


def _need(value,message,code='incomplete_review'):
    if not value:raise VisualError(code,message)


def _code_sources():
    return final._code_sources() | {str(Path(__file__).resolve()):file_sha256(__file__),
        str(Path(supplemental.__file__).resolve()):file_sha256(supplemental.__file__)}


def _context(supplemental_directory):
    directory=_ordinary(supplemental_directory)
    result=supplemental.verify(directory)
    _need(result['status']=='pass','Missing wall or normal checks have not passed.')
    plan=read_json(directory/'plan.json')
    session=_ordinary(plan['session']);state=read_json(session/'session.json')
    suite_directory=_ordinary(plan['suite'])
    suite=media_suite.load_suite(suite_directory,verify_analysis_code=False)
    chosen,layout=suite_evidence._matrix(suite,state)
    _need(chosen and chosen['candidate_id']==plan['candidate_id']
          and chosen['source_sha256']==plan['source_sha256'] and state['status']=='finalization_failed',
          'Do not relabel the historical failed Session.')
    original=safe_path(suite_directory,suite['original'],must_exist=False)
    projects={'original':original,'before':_ordinary(state['baseline']),'selected':_ordinary(plan['source'])}
    hashes={'original':suite['original_source_hashes'],'before':suite['authoring_parent_source_hashes'],
        'selected':plan['source_hashes']}
    sources={role:suite_evidence._source_expectation(project,hashes[role],'game')
        for role,project in projects.items()}
    rows=[]
    for job,capture_role,cases in layout:
        role='selected' if capture_role in ('candidate','final') else capture_role
        for record,case in suite_evidence._records(session/'measurements'/job/(capture_role+'.json'),cases,complete=True):
            row=final._row(record,case,role,len(rows),source=sources.get(role),project=projects.get(role),capture_role=capture_role)
            row['producer_job_id']=job
            row['source_epoch_kind']='historical_retained_pixels'
            rows.append(row)
    boundary=read_json(directory/'visual-boundary.json')
    from kiroclops.local_engine.visual_boundary_v4 import cases as boundary_cases
    actual=read_json(boundary['records_path'])
    _need(len(actual)==8 and [r['scenario'] for r in actual]==boundary_cases(),'All eight wall cases are required.')
    for record in actual:
        case={'case_id':record['scenario']['id'],'stage':'boundary','role':'regression','purpose':'preservation'}
        row=final._row(record,case,'boundary_selected',len(rows),source=sources['selected'],
            project=projects['selected'],capture_role='boundary')
        row.update(domain='game',boundary=True,full_sealed_source_sha256=plan['source_sha256'],
            source_epoch_kind='fresh_supplemental_capture')
        rows.append(row)
    context={'kind':'UnchangedGameMixedEpochSupplementalRawMatrix','version':VERSION,'domain':'game','stage':'jump',
        'supplemental':str(directory),'session':str(session),'suite':str(suite_directory),'suite_sha256':digest(suite),
        'sealed_source':str(projects['selected']),'sealed_source_hashes':plan['source_hashes'],
        'sealed_source_sha256':plan['source_sha256'],'selected_candidate_id':plan['candidate_id'],
        'original':str(original),'original_source_hashes':hashes['original'],
        'authoring_parent':str(projects['before']),'authoring_parent_source_hashes':hashes['before'],
        'cases':deepcopy(suite['cases']),'expected_roles':list(ROLES),'event_contract':suite.get('event_contract'),
        'historical_source_epoch':suite['source_epoch'],
        'current_capture_epoch_sha256':result['current_producer_epoch_sha256'],
        'supplemental_result_sha256':digest(result),'heldout_status':suite['heldout_status'],
        'retained_numerical_pass':True,'historical_final_status':'failed',
        'new_model_generation_claimed_by_aid':False,'full_behavior_acceptance':False}
    return context,rows


def _after_raw(context,rows,inventory):
    """Bind each actual producer only after the full image-only extraction pass."""
    directory=_ordinary(context['supplemental'])
    result=supplemental.verify(directory)
    _need(digest(result)==context['supplemental_result_sha256'],'Supplemental result changed.')
    plan=read_json(directory/'plan.json')
    for path,sha in plan['consumed_files'].items():
        _need(file_sha256(path)==sha,'Historical producer evidence changed.','source_changed')
        inventory[path]=sha
    for name in ('plan.json','result.json','receipt.json','producer-epoch.json','visual-boundary.json'):
        final._file(directory/name,inventory)
    for name in ('sealed_source','authoring_parent','original'):
        final._source(context[name],context[name+'_hashes' if name=='sealed_source' else name+'_source_hashes'],inventory)
    suite=media_suite.load_suite(context['suite'],verify_analysis_code=False)
    _need(digest(suite)==context['suite_sha256'],'Historical suite changed.')
    required=set()
    for row in rows:
        if row.get('boundary'):continue
        case=next(c for c in context['cases'] if c['case_id']==row['case_id'])
        record=row['record'];job=row['producer_job_id'];role=row['capture_role']
        suite_evidence._origin(Path(context['session']),job,role,record,case)
        if row['role']=='reference':
            frozen=media_suite.record_from_suite(context['suite'],case)
            value=suite_evidence._binding(record,case,suite,reference_package=case['package_sha256'])
            _need(value==suite_evidence._binding(frozen,case,suite,reference_package=case['package_sha256'])
                and record['raw_run']==frozen['raw_run'],'Historical reference pixels changed.')
        else:
            value=suite_evidence._binding(record,case,suite,expected_source=row['expected_source'],
                expected_job_id=job if role in ('candidate','final') else None)
        required.add(value)
    from kiroclops.local_engine.suite_boundary import bindings
    required.update(bindings(directory/'visual-boundary.json',candidate_sha256=context['sealed_source_sha256'],
        candidate_id=context['selected_candidate_id']))
    boundary=read_json(directory/'visual-boundary.json')
    for name,sha in boundary['file_hashes'].items():
        path=safe_path(Path(boundary['native_root']),name)
        _need(file_sha256(path)==sha,'Fresh wall producer evidence changed.')
        inventory[str(path)]=sha
    actual={(load_package(row['record']['package'],verify='hashes')['package_sha256'],
             digest(evidence.observations(row['record']))) for row in rows}
    _need(actual==required,'The mixed-epoch matrix omits or replaces an immutable observation.')
    return {'required_observations':sorted(required),'record_count':len(rows),
        'source_binding_status':'pass','source_code_review_status':'pending',
        'native_generation_kind':'retained_single_native_generation_reaudited',
        'historical_final_status':'failed','full_behavior_acceptance':False}


def _proposal_identity(row):
    return digest({k:row.get(k) for k in ('points','objects','connections','wall')})


def _compact_sheets(bundles,event_results,jump_results,output):
    """Same critical-frame policy, fewer tiles; every occurrence remains bound."""
    selected,reasons,pool=defaultdict(set),defaultdict(set),defaultdict(dict)
    by_id={b['record']['id']:b for b in bundles}
    def near(name,indices,reason):
        for index in indices:
            for value in (index-1,index,index+1):
                if 0<=value<len(by_id[name]['rows']):
                    selected[name].add(value);reasons[name,value].add(reason)
    for bundle in bundles:
        name=bundle['record']['id']
        for row in bundle['rows']:
            for oid in bundle['mapping']:
                if not any(a<=row['time_s']<=b for a,b in bundle['active'][oid]):continue
                if 'x' in row['points'][oid]:
                    pool[bundle['record']['role'],oid].setdefault(row['image_sha256'],
                        {'record_id':name,'role':bundle['record']['role'],'object_id':oid,
                         'index':row['index'],'image_sha256':row['image_sha256']})
                else:near(name,[row['index']],'raw_ambiguity')
        near(name,[0,len(bundle['rows'])-1],'clip_boundary')
        times=[row['time_s'] for row in bundle['rows']]
        for event in bundle['manifest']['inputs']['events']:
            near(name,[min(range(len(times)),key=lambda i:abs(times[i]-event['time_s']))],'public_input')
    for result in event_results:
        for event in result['raw_events']:
            near(result['record_id'],event['indices']+event.get('support_indices',[]),'independent_raw_event')
    for result in jump_results:
        if result:near(result['record_id'],list(result['raw'].values()),'independent_jump_event')
    controls=[]
    for entries in pool.values():
        values=list(entries.values())
        for index in sorted({round(i) for i in np.linspace(0,len(values)-1,min(20,len(values)))}):
            entry=values[index];controls.append(entry);near(entry['record_id'],[entry['index']],'distinct_raw_control')
    unique={};occurrences=[]
    for bundle in bundles:
        name=bundle['record']['id'];paths=frame_paths(bundle['record']['package'],bundle['manifest'])
        for index in sorted(selected[name]):
            row=bundle['rows'][index];key=(row['image_sha256'],_proposal_identity(row))
            if key not in unique:
                unique[key]={'bundle':bundle,'index':index,'path':paths[index],'tile_index':len(unique),'occurrences':[]}
            occurrence={'record_id':name,'role':bundle['record']['role'],'index':index,'time_s':row['time_s'],
                'image_sha256':row['image_sha256'],'raw_proposal_sha256':key[1],
                'reasons':sorted(reasons[name,index]),'tile_index':unique[key]['tile_index']}
            unique[key]['occurrences'].append(occurrence);occurrences.append(occurrence)
    tiles=[]
    for value in unique.values():
        image=read_image(value['path']);bundle=value['bundle'];index=value['index']
        frame=bundle['manifest']['frames'][index]
        _need(file_sha256(value['path'])==frame['sha256'],'Contact source changed.','source_changed')
        label=f"tile{value['tile_index']:03} {bundle['record']['role']} +{len(value['occurrences'])-1} exact repeats"
        tiles.append(raw._tile(image,frame,label,bundle['rows'][index],'game'))
    sheets=[]
    for page,start in enumerate(range(0,len(tiles),12)):
        batch=tiles[start:start+12]
        batch.extend(np.full((230,600,3),24,np.uint8) for _ in range((-len(batch))%3))
        relative=f'sheets/compact-{page:03}.png'
        raw.common._save_image(output/relative,np.vstack([np.hstack(batch[i:i+3]) for i in range(0,len(batch),3)]))
        sheets.append({'path':relative,'sha256':file_sha256(output/relative)})
    contact={'kind':'FrozenRawContactSelection','policy':'control_and_event_v1_exact_pixel_proposal_dedup',
        'controls':controls,'selected_images':occurrences,'minimum_distinct_control_images_per_object_role':20,
        'unique_tile_count':len(unique),'selected_occurrence_count':len(occurrences),
        'dedup_key':['image_sha256','raw_proposal_sha256'],'production_coordinates_or_events_used':False,
        'all_frames_extracted_hashed_and_numerically_checked':True}
    write_json(output/'contact-selection.json',contact)
    return sheets,contact


def prepare(supplemental_directory,output):
    context,rows=_context(supplemental_directory)
    output=_ordinary(output)
    _need(not output.exists(),'Preserve every prior raw aid.','output_exists')
    normalized=[raw.common._record(row,i) for i,row in enumerate(rows)]
    expected={(role,case['case_id']) for case in context['cases'] for role in ROLES}
    _need({(r['role'],r['case_id']) for r in normalized if not r.get('boundary')}==expected
        and len(normalized)==len(expected)+8,'The exact120 retained plus8 fresh records are required.')
    for row in normalized:
        _need(not output.is_relative_to(row['package']) and not row['package'].is_relative_to(output),'Raw output overlaps input.','unsafe_path')
    for name in ('suite','sealed_source','authoring_parent','original','session','supplemental'):
        _need(not output.is_relative_to(_ordinary(context[name])),'Raw aid cannot write inside a producer.','unsafe_path')
    contract=raw._public_contract(context['event_contract'],'game')
    sources,inventory=_code_sources(),{}
    output.mkdir(parents=True);write_json(output/'matrix.json',context)
    bundles=[]
    for row in normalized:
        bundle=raw._extract(row,output,inventory)
        if row.get('boundary'):final._extract_boundary(bundle,output,inventory)
        bundles.append(bundle)
    write_json(output/'extraction-complete.json',{'all_raw_images_marked_before_production_comparison':True,
        'production_coordinates_or_events_used':False,
        'raw_point_files':{b['record']['id']:file_sha256(output/'raw'/b['record']['id']/'points.json') for b in bundles}})
    producer=_after_raw(context,rows,inventory)
    failures,bindings,events,jumps,boundaries=[],[],[],[],[]
    for bundle in bundles:
        if bundle['record'].get('boundary'):
            failures.extend(final._scope_boundary(bundle,inventory,contract))
            view={**bundle,'event_set':{**bundle['event_set'],
                'events':[e for e in bundle['event_set']['events'] if not e['type'].startswith('boundary_')],
                'relations':[r for r in bundle['event_set']['relations'] if r['type']!='boundary_distance']}}
            compared,errors=raw._compare_events(view,contract)
            boundary,other=final._boundary_events(bundle,contract)
            boundaries.append(boundary);compared['raw_events'].extend(boundary.get('raw_events',[]));failures.extend(other)
            jump=None
        else:
            failures.extend(raw._scope(bundle,inventory,contract))
            compared,errors=raw._compare_events(bundle,contract)
            jump,other=raw._jump(bundle);failures.extend(other)
        events.append(compared);jumps.append(jump);failures.extend(errors)
        write_json(output/'measurements'/bundle['record']['id']/'measurement.json',
            {'observations':bundle['observations'],'events':bundle['event_set']})
        bindings.append({'record_id':bundle['record']['id'],'case_id':bundle['record']['case_id'],
            'role':bundle['record']['role'],'capture_role':bundle['record']['capture_role'],'stage':bundle['record']['stage'],
            'package_sha256':bundle['manifest']['package_sha256'],'observations_sha256':digest(bundle['observations']),
            'events_sha256':digest(bundle['event_set']),'source_sha256':bundle['record'].get('source_sha256'),
            'full_sealed_source_sha256':bundle['record'].get('full_sealed_source_sha256'),'object_map':bundle['mapping'],
            'public_active_intervals_s':bundle['active']})
    positions,errors=raw._positions(bundles);failures.extend(errors)
    sheets,contact=_compact_sheets(bundles,events,jumps,output)
    _need(sources==_code_sources(),'Review producer changed.','source_changed')
    for path,sha in inventory.items():_need(file_sha256(path)==sha,'Raw or producer bytes changed.','source_changed')
    result={'kind':'UnsignedMixedEpochGameSupplementalRawAid','version':VERSION,'schema_version':1,
        'status':'pending_visual_review','numeric_status':'pass' if not failures else 'fail','domain':'game',
        'matrix_sha256':digest(context),'records':bindings,'positions':positions,'events':events,'jumps':jumps,
        'boundaries':boundaries,'sheets':sheets,'failures':failures,'unsupported':[],
        'producer_binding':producer,'contact_selection_sha256':digest(contact),'public_event_contract':contract,
        'source_code_review_status':'pending','full_behavior_acceptance':False,'visual_inspection_claimed':False,
        'production_coordinates_used_for_extraction':False,'production_events_used_for_contact_selection':False,
        'new_model_generation':False,'historical_acceptance_changed':False,'historical_final_status':'failed'}
    write_json(output/'result.json',result)
    write_json(output/'evidence-receipt.json',{'kind':'UnsignedRawReviewEvidenceReceipt','status':'unsigned',
        'result_sha256':digest(result),'source_files':inventory,'code_sources':sources,
        'generated_files':{p.relative_to(output).as_posix():file_sha256(p) for p in sorted(output.rglob('*')) if p.is_file()},
        'matrix_sha256':digest(context),'measurement_bindings_sha256':digest(bindings),
        'contact_selection_sha256':digest(contact),'public_event_contract_sha256':digest(contract)})
    return result


def validate(supplemental_directory,output):
    context,rows=_context(supplemental_directory)
    output=_ordinary(output)
    result,receipt,attestation=(read_json(output/name) for name in ('result.json','evidence-receipt.json','attestation.json'))
    _need(result.get('kind')=='UnsignedMixedEpochGameSupplementalRawAid' and result.get('version')==VERSION
        and result['numeric_status']=='pass' and not result['failures'] and not result['unsupported']
        and result['matrix_sha256']==digest(context)==receipt['matrix_sha256']
        and receipt['result_sha256']==digest(result)
        and result['full_behavior_acceptance'] is False and result['historical_final_status']=='failed'
        and attestation['status']=='pass' and attestation['images_inspected'] is True and attestation['accepted'] is True
        and attestation['result_sha256']==digest(result) and attestation['inspected_sheets']==result['sheets']
        and attestation['evidence_receipt_sha256']==file_sha256(output/'evidence-receipt.json'),
        'The exact actual whole-matrix visual inspection is required.')
    _need(receipt['code_sources']==_code_sources(),'Raw review code changed.','stale_review')
    for path,sha in {**receipt['source_files'],**receipt['code_sources']}.items():
        _need(file_sha256(path)==sha,'Bound raw evidence changed.','stale_review')
    for name,sha in receipt['generated_files'].items():
        _need(file_sha256(safe_path(output,name))==sha,'Generated marks or sheets changed.','stale_review')
    _need(result['records']==final._current_record_bindings(rows),'A role/case/source/event binding changed.')
    producer=_after_raw(context,rows,{})
    _need(producer==result['producer_binding'],'Producer proof changed.')
    return {'kind':'VerifiedMixedEpochGameSupplementalRawReview','status':'pass','directory':str(output),
        'source_sha256':context['sealed_source_sha256'],'matrix_sha256':digest(context),
        'required_observations':producer['required_observations'],'record_count':len(rows),
        'attestation_sha256':file_sha256(output/'attestation.json'),'full_behavior_acceptance':False,
        'source_code_review_status':'pending','historical_final_status':'failed'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','validate'))
    parser.add_argument('--supplemental',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=(prepare if args.operation=='prepare' else validate)(args.supplemental,args.output)
    print({k:result.get(k) for k in ('kind','status','numeric_status','full_behavior_acceptance')})
    if 'sheets' in result:print('sheets',len(result['sheets']),'failures',len(result['failures']))


if __name__=='__main__':main()
