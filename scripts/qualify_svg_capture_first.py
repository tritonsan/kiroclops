"""Freeze eight reference pairs; capture serially, then analyze in two workers.

Separating recording from OpenCV work keeps analysis load off the real-time
browser path. Each recipe/repeat is attempted once; failures remain failures.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import argparse
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.web_local.specs import scenarios,fixture_project,validate_scenario
from kiroclops.local_engine.session import source_hashes,copy_project,_lock
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.evaluation import _capture_web,_measure_record,_compare_web
from kiroclops.local_engine.media_session import _compare,LIMITS
from scripts.validate_panel_recorder import implementation_hashes
from scripts.compare_svg_recorder_qualification import binding_for


def run(output, visual_clock=False):
    output=Path(output).resolve()
    if output.exists():raise ValueError('Fresh output required; no retry or substitution')
    recipes=[deepcopy(s) for split in ('calibration','validation') for s in scenarios('svg',split) if s['repeat']==1]
    if len(recipes)!=8:raise ValueError('Exactly eight recipes required')
    for s in recipes:
        s.update(id='observed-svg-'+s['id'],split='qualification',frame_driver='natural_browser_raf_v1',analysis_policy='observed_response_v2')
        if visual_clock:s['visual_frame_clock']='svg_raf_tag_v1'
        validate_scenario(s)
    source=fixture_project('svg','reference');expected=source_hashes(source);implementation=implementation_hashes(ROOT)
    runner_hash=file_sha256(__file__);helper=ROOT/'scripts/compare_svg_recorder_qualification.py';helper_hash=file_sha256(helper)
    output.mkdir(parents=True);configure_environment();copy_project(source,output/'source')
    # Retain the actual implementation as well as hashes for later reproduction.
    for name,sha in implementation.items():
        path=output/'implementation'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
        if file_sha256(path)!=sha:raise ValueError('Implementation changed while freezing')
    (output/'implementation/runner.py').write_bytes(Path(__file__).read_bytes())
    (output/'implementation/binding.py').write_bytes(helper.read_bytes())
    write_json(output/'protocol.json',{'kind':'ProspectiveSVGReferenceRecorderQualification','created_at':time.time(),
        'scenarios':recipes,'repeats':2,'required_pairs':8,'limits':LIMITS,'source_hashes':expected,
        'implementation_hashes':implementation,'runner_sha256':runner_hash,'binding_helper_sha256':helper_hash,
        'model_started':False,'source_edited':False,'retries':0,'serial_captures':True,'analysis_workers':2,
        'scope':'Reference repeatability only; not a Kiro run or independent visual accuracy acceptance'})
    protocol_sha=file_sha256(output/'protocol.json')
    def check():
        if implementation_hashes(ROOT)!=implementation or source_hashes(output/'source')!=expected:
            raise ValueError('Frozen implementation or source changed')
        if file_sha256(__file__)!=runner_hash or file_sha256(helper)!=helper_hash or file_sha256(output/'protocol.json')!=protocol_sha:
            raise ValueError('Frozen runner or protocol changed')
    pairs=[];count=0
    for s in recipes:
        row={'scenario':s,'records':[],'errors':[],'pass':False}
        for repeat in (1,2):
            print('capture',count+1,16,s['id'],repeat,flush=True)
            try:
                with _lock(ROOT/'artifacts/local-engine/capture.lock',timeout=30):
                    record=_capture_web(output/'source',output/'captures'/s['id']/str(repeat),s,check,lambda *a:None,lambda:False,'reference',analyze=False)
                if record['source_sha256']!=digest(expected):raise ValueError('Wrong source')
                row['records'].append(record);write_json(output/'raw-records'/s['id']/(str(repeat)+'.json'),record)
            except Exception as exc:row['errors'].append({'repeat':repeat,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
            count+=1;write_json(output/'progress.json',{'phase':'capture','completed':count,'required':16})
        pairs.append(row)
    def analyze(index,row):
        sid=row['scenario']['id']
        if not row['errors']:
            try:
                measured=[]
                for repeat,record in enumerate(row['records'],1):
                    print('analyze',sid,repeat,flush=True)
                    value=_measure_record(record,output/'captures'/sid/str(repeat)/'measurement',check,lambda *a:None,
                                          measured[0]['selection'] if measured else None)
                    measured.append(value);write_json(output/'records'/sid/(str(repeat)+'.json'),value)
                row['records']=measured;a,b=measured
                row['input_window_comparison']=_compare(a,a,b,binding_for(a,row['scenario']),require_improvement=False)
                row['benchmark_comparison']=_compare_web(a,a,b,improvement=False)
                row['functional_pass']=all(r['functional']['status']=='pass' for r in measured)
                row['pass']=bool(row['input_window_comparison']['pass'] and row['benchmark_comparison']['pass'] and row['functional_pass'])
            except Exception as exc:row['errors'].append({'role':'analysis','code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
        write_json(output/'results'/(sid+'.json'),row)
        summary={'scenario_id':sid,'pass':row['pass'],'errors':row['errors'],
                 'input_window_pass':row.get('input_window_comparison',{}).get('pass'),
                 'benchmark_pass':row.get('benchmark_comparison',{}).get('pass')}
        print(summary,flush=True);return index,summary
    done={}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(analyze,i,row) for i,row in enumerate(pairs)]
        for future in as_completed(futures):
            index,value=future.result();done[index]=value
            write_json(output/'progress.json',{'phase':'analysis','completed':len(done),'required':8,'rows':[done[i] for i in sorted(done)]})
    check();rows=[done[i] for i in range(8)]
    result={'status':'pass' if all(r['pass'] for r in rows) else 'fail','passed':sum(r['pass'] for r in rows),
            'required':8,'rows':rows,'protocol_sha256':protocol_sha,'production_mode_promoted':False,
            'model_started':False,'independent_visual_review':'pending'}
    write_json(output/'result.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--visual-clock',action='store_true')
    args=p.parse_args();result=run(args.output,args.visual_clock);print({'status':result['status'],'passed':result['passed']},flush=True)
