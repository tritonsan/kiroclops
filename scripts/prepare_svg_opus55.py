"""Prepare a fresh, prospectively bound SVG media trial; never starts the model."""
from pathlib import Path
from copy import deepcopy
import argparse
import time
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.visual.package import load_package
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.intake import prepare_intake,resume_selection
from kiroclops.local_engine.media_session import binding_preset,create_media_session,LIMITS
from kiroclops.local_engine.session import source_hashes
from kiroclops.web_local.specs import fixture_project,scenarios,validate_scenario
from scripts.validate_panel_recorder import implementation_hashes
from kiroclops.local_engine.review import validate_independent_review


def run(qualification,review,output):
    qualification,review,output=map(lambda p:Path(p).resolve(),(qualification,review,output))
    if output.exists():raise ValueError('Never overwrite or restart a model trial')
    receipt=read_json(review/'review-summary.json')
    if receipt.get('status')!='pass' or not all(receipt.get(k) for k in ('position_pass','event_pass','relationship_pass','visual_inspection_complete')):
        raise ValueError('Independent visual qualification must finish first')
    if receipt['qualification_result_sha256']!=file_sha256(qualification/'result.json'):
        raise ValueError('Visual qualification refers to other recordings')
    if receipt['independent_review_sha256']!=file_sha256(review/'independent-review.json'):
        raise ValueError('Independent review binding changed')
    validate_independent_review(review/'independent-review.json')
    # The first defined calibration recipe/repeat is fixed independently of scores.
    record=read_json(qualification/'records/observed-svg-three-to-four-w960-r1/1.json')
    source=fixture_project('svg','target');original=source_hashes(source)
    heldout=[]
    for scenario in scenarios('svg','validation'):
        s=deepcopy(scenario);s.update(frame_driver='natural_browser_raf_v1',analysis_policy='observed_response_v2',visual_frame_clock='svg_raf_tag_v1')
        validate_scenario(s);heldout.append(s)
    protocol={'kind':'ProspectiveSVGMediaAndExternalHoldout','created_at':time.time(),
        'model':'claude-opus-5.5','effort':'max','approval':'native_session_allow_always',
        'max_candidates':3,'model_authoring_seconds':1800,'calibration_recipe':'three-to-four-w960-r1',
        'reference_record_sha256':file_sha256(qualification/'records/observed-svg-three-to-four-w960-r1/1.json'),
        'reference_package_sha256':record['observations']['package_sha256'],
        'review_summary_sha256':file_sha256(review/'review-summary.json'),
        'heldout':heldout,'limits':LIMITS,'baseline_hashes':original,'implementation_hashes':implementation_hashes(ROOT),
        'holdout_helper_hashes':{name:file_sha256(ROOT/name) for name in ('scripts/validate_sealed_svg.py','scripts/compare_svg_recorder_qualification.py','scripts/validate_panel_recorder.py')},
        'human_solution_or_live_guidance':False,'automatic_retries':0,'benchmark_cohort_trial':False,
        'scope':'One real autonomous media trial plus 12 prospectively bound external holdouts; not the 18-trial paired cohort.'}
    intake=output.with_name(output.name+'-intake');protocol_path=output.with_name(output.name+'-protocol.json')
    if protocol_path.exists():raise ValueError('Preserve earlier protocol')
    write_json(protocol_path,protocol);configure_environment()
    request=('Match the supplied diagram video: how the nodes move when a node is added. '
        'Preserve node identity, selection, real add/remove and pointer interactions, visible connector attachment, '
        'responsive layout and no final node overlap. Use the observed motion to change the actual layout '
        'controller, and validate with KiroClops. Do not replay recorded coordinates or read reference code.')
    prepare_intake(record['package'],source,request,intake)
    manifest=load_package(intake/'package');selection=deepcopy(record['selection'])
    selection.update(kind='ObjectSelection',package_sha256=manifest['package_sha256'])
    for obj in selection['objects']:
        obj['template_frame_sha256']=manifest['frames'][obj['frame_index']]['sha256']
        obj['body_height_px']=float(obj['roi'][3]);obj['body_height_source']='selected_raw_image_roi'
    resume_selection(intake,selection)
    binding=binding_preset(intake,object_map={o['id']:o['id'] for o in selection['objects']},initial_node_count=3)
    result=create_media_session(intake,output,binding)
    if source_hashes(source)!=original:raise ValueError('Original target changed during setup')
    write_json(output/'operator-preparation.json',{'kind':'ProspectiveOpus55SVGTask','created_at':time.time(),
        'model':'claude-opus-5.5','effort':'max','protocol_path':str(protocol_path),'protocol_sha256':file_sha256(protocol_path),
        'qualification_review_sha256':file_sha256(review/'review-summary.json'),
        'reference_choice':'first defined calibration recipe and first repeat; fixed before model',
        'reference_implementation_exposed':False,'old_candidate_exposed':False,'target_edited_by_operator':False,
        'human_solution_or_live_guidance':False,'model_started':False,'benchmark_trial':False,
        'heldout_status':'external_protocol_sealed_before_model','automatic_retries':0})
    print(result['session'],flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('qualification','review','output'):p.add_argument('--'+key,type=Path,required=True)
    a=p.parse_args();run(a.qualification,a.review,a.output)
