"""Prospective reference-only grid; all failures retained, no Kiro run or code edits."""
from pathlib import Path
from copy import deepcopy
import argparse,sys,time,shutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,file_sha256
from kiroclops.visual.package import load_package
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.session import source_hashes,copy_project,_lock
from kiroclops.local_engine.evaluation import _capture_web
from kiroclops.local_engine.media_session import _compare,LIMITS


def run(output):
    root=Path(__file__).resolve().parents[1];output=Path(output).resolve()
    prior=root/'artifacts/phase3a/natural-browser-raf-qualification-02'
    if read_json(prior/'result.json')['status']!='pass':raise ValueError('Initial repeatability gate failed')
    if output.exists():raise ValueError('Fresh output required; never retry failed cases in place')
    state=read_json(root/'artifacts/phase3a/panel-opus55-media-05/session.json')
    original=read_json(root/'artifacts/phase3a/panel-feedback-validation-01/protocol.json')
    source=root/'artifacts/phase3a/panel-feedback-validation-01/sources/reference';hashes=source_hashes(source)
    recipes=[original['calibration']]+[s for s in original['heldout'] if s['repeat']==1]
    assert len(recipes)==7
    for s in recipes:
        s.update(id='natural-'+s['id'],split='qualification',frame_driver='natural_browser_raf_v1',click_schedule='dispatch_at_due_v1')
    output.mkdir(parents=True)
    adapters={p.name:file_sha256(p) for p in (root/'kiroclops/web_local').glob('*.py')}
    write_json(output/'protocol.json',{'created_at':time.time(),'scenarios':recipes,'repeats':2,
        'required_pairs':7,'limits':LIMITS,'source_hashes':hashes,'adapter_hashes':adapters,
        'candidate_used':False,'retries':0,'first_gate_sha256':file_sha256(prior/'result.json')})
    copy_project(source,output/'source')
    shutil.copytree(root/'kiroclops/web_local',output/'adapter-source',ignore=shutil.ignore_patterns('__pycache__'))
    configure_environment();rows=[]
    def check():
        if source_hashes(output/'source')!=hashes:raise ValueError('Reference source changed')
        if adapters!={p.name:file_sha256(p) for p in (root/'kiroclops/web_local').glob('*.py')}:
            raise ValueError('Adapter changed')
    for scenario in recipes:
        sid=scenario['id'];records=[];errors=[]
        for i in range(2):
            print(sid,i+1,flush=True)
            try:
                with _lock(Path(state['capture_lock']),timeout=30):
                    record=_capture_web(output/'source',output/'captures'/sid/str(i+1),scenario,check,
                        lambda *a:None,lambda:False,'reference')
                write_json(output/'records'/sid/f'{i+1}.json',record);records.append(record)
            except Exception as exc:
                errors.append({'repeat':i+1,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
        row={'scenario':scenario,'errors':errors,'pass':False}
        if not errors:
            binding=read_json(Path(state['reference'])/'capture-binding.json');binding['scenario']=deepcopy(scenario)
            try:
                result=_compare(records[0],records[0],records[1],binding,require_improvement=False)
                row.update(comparison=result)
                row['pass']=result['pass'] and all(r['functional']['status']=='pass' for r in records)
                row['inputs']=[load_package(r['package'])['inputs'] for r in records]
            except Exception as exc:
                errors.append({'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
        write_json(output/'results'/(sid+'.json'),row)
        rows.append({'scenario':sid,'pass':row['pass'],'errors':errors})
        write_json(output/'progress.json',{'completed':len(rows),'required':7,'rows':rows})
    check();result={'status':'pass' if all(r['pass'] for r in rows) else 'fail','rows':rows,
        'passed':sum(r['pass'] for r in rows),'required':7,'limits_changed':False,'model_started':False,
        'scope':'same-reference repeatability, not target transfer or independent visual accuracy'}
    write_json(output/'result.json',result);print(result,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);run(parser.parse_args().output)
