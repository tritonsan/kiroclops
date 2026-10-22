"""Twelve prospectively fixed SVG holdouts of one sealed autonomous candidate.

All 36 recordings finish before pixel analysis starts. Each scenario is
attempted once; old results and the selected source are never rewritten.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import argparse
import re
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.visual.package import load_package
from kiroclops.local_engine.session import source_hashes,copy_project,_lock
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.evaluation import _capture_web,_measure_record
from kiroclops.local_engine.media_session import _compare,LIMITS
from kiroclops.local_engine.review import audit_native_run
from kiroclops.web_local.specs import validate_scenario,SVG_CAPTURE_POLICY
from scripts.validate_panel_recorder import implementation_hashes
from scripts.compare_svg_recorder_qualification import binding_for

def run(session,protocol_path,reference_project,output):
    session,protocol_path,reference_project,output=map(lambda p:Path(p).resolve(),(session,protocol_path,reference_project,output))
    state=read_json(session/'session.json');protocol=read_json(protocol_path);prep=read_json(session/'operator-preparation.json')
    if output.exists():raise ValueError('Use a fresh output; no replacement retries')
    if output.is_relative_to(session):raise ValueError('Preserve the sealed session separately')
    if state['status']!='finalized' or not state['selected_candidate_id'] or state['active_job_id']:
        raise ValueError('Require idle, sealed final candidate')
    if (file_sha256(protocol_path)!=prep['protocol_sha256'] or protocol['created_at']>=state['started_at']
            or protocol['limits']!=LIMITS):raise ValueError('Protocol was not fixed before the model')
    impl=implementation_hashes(ROOT)
    helpers={name:file_sha256(ROOT/name) for name in protocol['holdout_helper_hashes']}
    if impl!=protocol['implementation_hashes'] or helpers!=protocol['holdout_helper_hashes']:
        raise ValueError('Prospective evaluator implementation changed')
    recipes=protocol['heldout']
    if len(recipes)!=12 or len({s['id'] for s in recipes})!=12:raise ValueError('Exactly twelve distinct holdouts required')
    for s in recipes:
        validate_scenario(s)
        if (s['domain']!='svg' or s['split']!='validation' or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',s['id'])
                or any(s.get(k)!=v for k,v in SVG_CAPTURE_POLICY.items())):raise ValueError('Wrong validation policy')
    native=audit_native_run(session)
    if native['status']!='pass':raise ValueError('Native authorship/configuration audit must pass first')
    selected=next(c for c in state['candidates'] if c['candidate_id']==state['selected_candidate_id'])
    sources={'reference':reference_project,'before':Path(state['baseline']),
        'candidate':session/'candidates'/state['selected_candidate_id']/'source'}
    expected={role:source_hashes(path) for role,path in sources.items()}
    if any(output.is_relative_to(p.resolve()) for p in sources.values()):raise ValueError('Output must be outside source projects')
    manifest=load_package(Path(state['reference'])/'package')
    if (digest(expected['candidate'])!=selected['source_sha256'] or expected['before']!=state['baseline_hashes']
            or expected['reference']!=manifest['metadata']['source_hashes']):raise ValueError('Bound project source changed')
    snapshot={p.relative_to(session).as_posix():file_sha256(p) for p in session.rglob('*') if p.is_file()}
    output.mkdir(parents=True);configure_environment()
    for role,path in sources.items():copy_project(path,output/'sources'/role)
    write_json(output/'binding.json',{'session_sha256':file_sha256(session/'session.json'),'protocol_sha256':prep['protocol_sha256'],
        'candidate_sha256':selected['source_sha256'],'source_hashes':expected,'original_files':snapshot,
        'native_evidence_hashes':native['evidence_hashes'],'began_at':time.time(),'retries':0,'candidate_edits':False})
    write_json(output/'native-audit.json',native);write_json(output/'protocol.json',protocol)
    def check():
        if (file_sha256(protocol_path)!=prep['protocol_sha256'] or implementation_hashes(ROOT)!=impl
                or any(file_sha256(ROOT/name)!=sha for name,sha in helpers.items())):raise ValueError('Frozen protocol/evaluator changed')
        for role,hashes in expected.items():
            if source_hashes(output/'sources'/role)!=hashes:raise ValueError('Frozen source changed')
    rows=[];completed=0
    for s in recipes:
        row={'scenario':s,'records':{},'errors':[],'pass':False};sid=s['id']
        for role in sources:
            print('capture',completed+1,36,sid,role,flush=True)
            try:
                with _lock(Path(state['capture_lock']),timeout=30):
                    record=_capture_web(output/'sources'/role,output/'captures'/sid/role,s,check,lambda *a:None,lambda:False,role,analyze=False)
                if record['source_sha256']!=digest(expected[role]):raise ValueError('Wrong source receipt')
                row['records'][role]=record;write_json(output/'raw-records'/sid/(role+'.json'),record)
            except Exception as exc:row['errors'].append({'role':role,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
            completed+=1;write_json(output/'progress.json',{'phase':'capture','completed':completed,'required':36})
        rows.append(row)
    def analyze(index,row):
        sid=row['scenario']['id'];measured={}
        if not row['errors']:
            try:
                for role,record in row['records'].items():
                    print('analyze',sid,role,flush=True)
                    value=_measure_record(record,output/'captures'/sid/role/'measurement',check,lambda *a:None,
                        measured['reference']['selection'] if measured else None)
                    measured[role]=value;write_json(output/'records'/sid/(role+'.json'),value)
                row['records']=measured
                comparison=_compare(measured['reference'],measured['before'],measured['candidate'],binding_for(measured['reference'],row['scenario']),require_improvement=True)
                row.update(comparison=comparison,functional_pass=all(x['functional']['status']=='pass' for x in measured.values()))
                row['pass']=bool(comparison['pass'] and row['functional_pass'])
            except Exception as exc:row['errors'].append({'role':'analysis','code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
        write_json(output/'results'/(sid+'.json'),row)
        result={'scenario_id':sid,'pass':row['pass'],'errors':row['errors'],'score':row.get('comparison',{}).get('score'),
            'improvement':row.get('comparison',{}).get('improvement_fraction')}
        print(result,flush=True);return index,result
    done={}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(analyze,i,row) for i,row in enumerate(rows)]):
            i,result=future.result();done[i]=result
            write_json(output/'progress.json',{'phase':'analysis','completed':len(done),'required':12,'results':[done[i] for i in sorted(done)]})
    check()
    if {p.relative_to(session).as_posix():file_sha256(p) for p in session.rglob('*') if p.is_file()}!=snapshot:
        raise ValueError('Original session changed during validation')
    results=[done[i] for i in range(12)]
    result={'status':'pass' if all(r['pass'] for r in results) else 'fail','passed':sum(r['pass'] for r in results),'required':12,
        'results':results,'candidate_source_sha256':selected['source_sha256'],'original_files_unchanged':len(snapshot),
        'protocol_sha256':prep['protocol_sha256'],'source_review':'pending','independent_visual_review':'pending',
        'previous_session_rewritten':False,'benchmark_cohort_trial':False}
    write_json(output/'validation.json',result);return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('session','protocol','reference-project','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();r=run(a.session,a.protocol,a.reference_project,a.output);print({'passed':r['passed'],'required':12},flush=True)
