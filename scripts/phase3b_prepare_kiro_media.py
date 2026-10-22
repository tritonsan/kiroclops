"""Prepare a fresh cloud-backed CLI integration task from bound calibration pixels.

No solution is copied and no model is called here. These are service integration
scenarios, not replacements for the original 18-run comparative cohort.
"""
import argparse
import os
from pathlib import Path
from kiroclops.visual.schema import read_json,write_json,file_sha256
from kiroclops.local_engine.intake import prepare_intake,resume_selection
from kiroclops.local_engine.media_session import binding_preset,create_media_session

ROOT=Path(__file__).resolve().parents[1]
SOURCES={
    'game':('artifacts/phase3a/game-media-smoke-01/intake','vendor/heart'),
    'panel':('artifacts/phase3a/panel-opus55-media-01-intake','fixtures/phase3a-web/target'),
    'svg':('artifacts/phase3a/svg-opus55-media-02-intake','fixtures/phase3a-svg/target'),
}


def prepare(domain,output,service_config):
    output=Path(output).resolve();service_config=Path(service_config).resolve()
    if output.exists():raise ValueError('Fresh integration directory required')
    descriptor=read_json(service_config)
    prior,target=(ROOT/p for p in SOURCES[domain])
    source=read_json(prior/'intake.json');selection=read_json(prior/'selection.json')
    previous=os.environ.pop('KIROCLOPS_ANALYSIS_BACKEND',None)
    try:
        # Local intake UI and object proposal still require local OpenCV. The
        # actual model preparation/candidate measurements select AWS below.
        prepare_intake(prior/'package',target,source['request'],output/'intake')
        resume_selection(output/'intake',selection)
    finally:
        if previous is not None:os.environ['KIROCLOPS_ANALYSIS_BACKEND']=previous
    mapping={obj['id']:obj['id'] for obj in selection['objects']}
    options={'game_stage':'jump','body_height':7} if domain=='game' else {'initial_node_count':3} if domain=='svg' else {}
    binding=binding_preset(output/'intake',object_map=mapping,**options)
    # Keep this run's descriptor immutable. Encrypted, time-limited credential
    # contents remain outside the model's workspace.
    write_json(output/'service.json',descriptor)
    os.environ['KIROCLOPS_ANALYSIS_BACKEND']='remote'
    os.environ['KIROCLOPS_CLOUD_CONFIG']=str(output/'service.json')
    result=create_media_session(output/'intake',output/'session',binding)
    write_json(output/'prepared.json',{**result,'purpose':'AWS service integration; calibration-only',
        'previous_intake':str(prior.relative_to(ROOT)),'selection_sha256':file_sha256(prior/'selection.json'),
        'fresh_target':str(target.relative_to(ROOT)),'solution_patch_supplied':False,
        'model':'claude-opus-5.5','effort':'max','authoring_budget_seconds':1800,
        'comparative_claim':False,'external_user':False})
    print({'status':result['status'],'domain':domain,'session':result['session'],'model_started':False})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--domain',choices=SOURCES,required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--service-config',required=True)
    args=parser.parse_args();prepare(args.domain,args.output,args.service_config)
