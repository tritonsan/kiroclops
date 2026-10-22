"""Run a predeclared panel validation on a sealed Kiro candidate, without edits.

The protocol hash must have been bound before the model was started. Results
are an external supplement; this never rewrites a historical session verdict.
"""
from pathlib import Path
from copy import deepcopy
import argparse
import re
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.visual.package import load_package
from kiroclops.local_engine.session import source_hashes,copy_project,_lock
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.evaluation import _capture_web
from kiroclops.local_engine.media_session import _compare,LIMITS
from kiroclops.local_engine.review import audit_native_run
from kiroclops.web_local.specs import validate_scenario


def run(session,protocol_path,reference_project,output):
    session=Path(session).resolve();output=Path(output).resolve()
    state=read_json(session/'session.json');protocol=read_json(protocol_path)
    preparation=read_json(session/'operator-preparation.json')
    if output.exists():raise ValueError('Use a fresh output; never repeat failed validation in place')
    if output.is_relative_to(session):raise ValueError('Keep validation output outside the original session')
    if state['status']!='finalized' or not state['selected_candidate_id'] or state['active_job_id']:
        raise ValueError('Only an idle sealed final candidate can be validated')
    if (file_sha256(protocol_path)!=preparation.get('protocol_sha256') or
            protocol['created_at']>=state['started_at'] or protocol['limits']!=LIMITS):
        raise ValueError('Held-out protocol was not bound before the model, or limits changed')
    recipes=protocol['heldout']
    if len(recipes)!=12 or len({s['id'] for s in recipes})!=12:
        raise ValueError('This protocol requires twelve distinct panel validation scenarios')
    for scenario in recipes:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',scenario['id']):raise ValueError('Scenario IDs must be path-free')
        validate_scenario(scenario)
        if scenario['domain']!='panel' or scenario['split']!='validation':raise ValueError('Wrong validation scope')
    native=audit_native_run(session)
    if native['status']!='pass':raise ValueError('Native code authorship trace must pass before validation')
    candidate=session/'candidates'/state['selected_candidate_id']/'source'
    selected=next(c for c in state['candidates'] if c['candidate_id']==state['selected_candidate_id'])
    sources={'reference':Path(reference_project).resolve(),'before':Path(state['baseline']),'candidate':candidate}
    if any(output.is_relative_to(path.resolve()) for path in sources.values()):
        raise ValueError('Keep validation output outside source projects')
    expected={k:source_hashes(p) for k,p in sources.items()}
    if digest(expected['candidate'])!=selected['source_sha256']:raise ValueError('Candidate source changed')
    manifest=load_package(Path(state['reference'])/'package')
    if expected['reference']!=manifest['metadata']['source_hashes']:raise ValueError('Reference source differs from supplied pixels')
    if expected['before']!=state['baseline_hashes']:raise ValueError('Baseline changed')
    snapshot={p.relative_to(session).as_posix():file_sha256(p) for p in session.rglob('*') if p.is_file()}
    output.mkdir(parents=True)
    write_json(output/'protocol.json',protocol)
    write_json(output/'binding.json',{'session_sha256':file_sha256(session/'session.json'),
        'protocol_sha256':file_sha256(protocol_path),'candidate_sha256':selected['source_sha256'],
        'source_hashes':expected,'original_files':snapshot,'native_evidence_hashes':native['evidence_hashes'],
        'began_at':time.time(),'retries':0,'candidate_edits':False})
    for role,path in sources.items():copy_project(path,output/'sources'/role)
    configure_environment()
    def check():
        if file_sha256(protocol_path)!=preparation['protocol_sha256']:raise ValueError('Protocol changed')
        for role,hashes in expected.items():
            if source_hashes(output/'sources'/role)!=hashes:raise ValueError('Frozen validation source changed')
    rows=[]
    for scenario in recipes:
        row={'scenario':scenario,'records':{},'errors':[]};sid=scenario['id']
        for role in ('reference','before','candidate'):
            print(sid,role,flush=True)
            try:
                # Use the same host capture lease as normal MCP jobs.
                with _lock(Path(state['capture_lock']),timeout=30):
                    check();ref=row['records'].get('reference')
                    record=_capture_web(output/'sources'/role,output/'captures'/sid/role,scenario,
                        check,lambda *args:None,lambda:False,role,
                        reference_selection=ref['selection'] if ref else None)
                row['records'][role]=record;write_json(output/'records'/sid/(role+'.json'),record)
            except Exception as exc:
                row['errors'].append({'role':role,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
        if not row['errors']:
            try:
                binding=read_json(Path(state['reference'])/'capture-binding.json');binding['scenario']=deepcopy(scenario)
                r=row['records'];comparison=_compare(r['reference'],r['before'],r['candidate'],binding,require_improvement=True)
                row.update(comparison=comparison,functional_pass=all(x['functional']['status']=='pass' for x in r.values()))
                row['pass']=comparison['pass'] and row['functional_pass']
            except Exception as exc:
                row['errors'].append({'role':'comparison','code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
        row.setdefault('pass',False);write_json(output/'results'/(sid+'.json'),row)
        rows.append({'scenario_id':sid,'pass':row['pass'],'errors':row['errors'],
            'score':row.get('comparison',{}).get('score'),'improvement':row.get('comparison',{}).get('improvement_fraction')})
        write_json(output/'progress.json',{'completed':len(rows),'total':12,'results':rows})
    check()
    actual={p.relative_to(session).as_posix():file_sha256(p) for p in session.rglob('*') if p.is_file()}
    if actual!=snapshot:raise ValueError('Original sealed session changed during validation')
    result={'status':'pass' if all(r['pass'] for r in rows) else 'fail','required':12,
        'passed':sum(r['pass'] for r in rows),'results':rows,'candidate_source_sha256':selected['source_sha256'],
        'original_files_unchanged':len(snapshot),'protocol_sha256':preparation['protocol_sha256'],
        'source_review':'pending','independent_visual_review':'pending','previous_session_rewritten':False}
    write_json(output/'validation.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('session','protocol','reference-project','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    result=run(args.session,args.protocol,args.reference_project,args.output)
    print({'passed':result['passed'],'required':result['required']})
