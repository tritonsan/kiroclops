"""Reference-only SVG recorder qualification, with predeclared scenarios."""
from copy import deepcopy
from pathlib import Path
import argparse
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.web_local.specs import scenarios,fixture_project,validate_scenario
from kiroclops.local_engine.session import source_hashes,copy_project,_lock
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.evaluation import _capture_web,_compare_web
from kiroclops.local_engine.media_session import _compare,LIMITS,VIEWPORT_ALIGNMENT
from scripts.validate_panel_recorder import implementation_hashes
from scripts.compare_svg_recorder_qualification import binding_for


def run(output,analysis_policy=None):
    root=Path(__file__).resolve().parents[1];output=Path(output).resolve()
    if output.exists():raise ValueError('Fresh output required; failed recordings are never retried in place')
    recipes=[s for split in ('calibration','validation') for s in scenarios('svg',split) if s['repeat']==1]
    if len(recipes)!=8:raise ValueError('Eight distinct SVG recipes required')
    for s in recipes:
        s.update(id='natural-svg-'+s['id'],split='qualification',frame_driver='natural_browser_raf_v1')
        if analysis_policy:s['analysis_policy']=analysis_policy
        validate_scenario(s)
    source=fixture_project('svg','reference');expected=source_hashes(source)
    implementation=implementation_hashes(root);output.mkdir(parents=True)
    write_json(output/'protocol.json',{'kind':'ProspectiveSVGReferenceRecorderQualification','created_at':time.time(),
        'scenarios':recipes,'repeats':2,'required_pairs':8,'limits':LIMITS,'source_hashes':expected,
        'implementation_hashes':implementation,'model_started':False,'source_edited':False,'retries':0,
        'scope':'Reference repeatability, not Kiro transfer or independent visual accuracy',
        'runner_sha256':file_sha256(__file__),
        'binding_helper_sha256':file_sha256(ROOT_BINDING_HELPER)})
    protocol_sha=file_sha256(output/'protocol.json');copy_project(source,output/'source');configure_environment()
    def check():
        if implementation_hashes(root)!=implementation or source_hashes(output/'source')!=expected:
            raise ValueError('Recorder/evaluator or reference source changed')
        if file_sha256(output/'protocol.json')!=protocol_sha:raise ValueError('Frozen protocol changed')
        if (file_sha256(__file__)!=read_json(output/'protocol.json')['runner_sha256'] or
                file_sha256(ROOT_BINDING_HELPER)!=read_json(output/'protocol.json')['binding_helper_sha256']):
            raise ValueError('Qualification runner changed')
    rows=[]
    for scenario in recipes:
        sid=scenario['id'];row={'scenario':scenario,'records':[],'errors':[],'pass':False}
        for repeat in (1,2):
            print(len(rows)+1,8,sid,repeat,flush=True)
            try:
                with _lock(root/'artifacts/local-engine/capture.lock',timeout=30):
                    record=_capture_web(output/'source',output/'captures'/sid/str(repeat),scenario,check,
                        lambda *args:None,lambda:False,'reference',
                        reference_selection=row['records'][0]['selection'] if row['records'] else None)
                if record['source_sha256']!=digest(expected):raise ValueError('Wrong recording source')
                row['records'].append(record);write_json(output/'records'/sid/(str(repeat)+'.json'),record)
            except Exception as exc:
                row['errors'].append({'repeat':repeat,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
        if not row['errors']:
            a,b=row['records']
            binding=binding_for(a,scenario)
            try:
                row['input_window_comparison']=_compare(a,a,b,binding,require_improvement=False)
                row['benchmark_comparison']=_compare_web(a,a,b,improvement=False)
                row['functional_pass']=all(r['functional']['status']=='pass' for r in row['records'])
                row['pass']=row['input_window_comparison']['pass'] and row['benchmark_comparison']['pass'] and row['functional_pass']
            except Exception as exc:
                row['errors'].append({'role':'comparison','code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
        write_json(output/'results'/(sid+'.json'),row)
        summary={'scenario_id':sid,'pass':row['pass'],'errors':row['errors'],
                 'input_window_pass':row.get('input_window_comparison',{}).get('pass'),
                 'benchmark_pass':row.get('benchmark_comparison',{}).get('pass')}
        rows.append(summary);write_json(output/'progress.json',{'completed':len(rows),'required':8,'rows':rows})
        print(summary,flush=True)
    check()
    result={'status':'pass' if all(r['pass'] for r in rows) else 'fail','passed':sum(r['pass'] for r in rows),
        'required':8,'rows':rows,'protocol_sha256':protocol_sha,'production_mode_promoted':False,'model_started':False,
        'independent_visual_review':'pending'}
    write_json(output/'result.json',result);return result


ROOT_BINDING_HELPER=Path(__file__).with_name('compare_svg_recorder_qualification.py')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--analysis-policy',choices=['observed_response_v2'])
    args=p.parse_args();result=run(args.output,args.analysis_policy);print({'status':result['status'],'passed':result['passed'],'required':8},flush=True)
