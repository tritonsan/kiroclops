"""Continue one explicit qualification into one reference pack, without retries.

No model is invoked, no failed evidence is replaced, and no independent
visual review is signed. Intermediate state remains on disk across a reconnect.
"""
from pathlib import Path
import sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'artifacts/phase3a/tools')]
from kiroclops.visual.schema import read_json,write_json,file_sha256,VisualError
from kiroclops.local_engine.qualification import analyzer_sources
from kiroclops.local_engine.web_reference import prepare_reference_pack
from kiroclops.web_local.specs import QUALIFIED_CAPTURE_POLICY
from kiroclops.local_engine.environment import configure_environment
import check_qualified_reference_observability as gate

def run(qualification,references,measurement,receipt):
    qualification,references,measurement,receipt=map(lambda p:Path(p).resolve(),(qualification,references,measurement,receipt))
    if receipt.exists() or references.exists() or measurement.exists():raise ValueError('Fresh destinations required')
    before=analyzer_sources();deadline=time.monotonic()+1800;state={'status':'waiting_for_qualification','model_started':False,'source_hashes':before}
    def save(**changes):
        state.update(changes);write_json(receipt,state)
    save()
    try:
        while True:
            result=read_json(qualification/'run-result.json')
            if result.get('finished_at'):break
            if time.monotonic()>=deadline:raise VisualError('qualification_wait_timeout','No terminal qualification receipt after30minutes')
            time.sleep(5)
        if result['status']!='classification_pass_review_pending':raise VisualError('qualification_failed','Qualification did not pass classification')
        if analyzer_sources()!=before:raise VisualError('source_changed','Sources changed while qualification ran')
        corpus=qualification/'corpus';paths=sorted((corpus/'analysis').rglob('analysis.json'));bad=[]
        if len(paths)!=16:raise VisualError('incomplete_corpus','Expected16 measured raw recordings')
        for path in paths:
            analysis=read_json(path);events=analysis['events']
            for scope,key in (('event','events'),('relationship','relations')):
                for index,item in enumerate(events.get(key,[])):
                    if item.get('timing_status')=='unresolved':bad.append({'analysis_path':str(path),'scope':scope,'index':index,'measurement':item})
        save(precision_checked_records=16,unresolved_timing=bad,qualification_result_sha256=file_sha256(qualification/'run-result.json'))
        if bad:raise VisualError('qualification_event_precision_failure',f'{len(bad)} unresolved measurements; model remains blocked')
        save(status='capturing_references');configure_environment()
        print('Qualified16-record precision gate passed; capture32 references',flush=True)
        pack=prepare_reference_pack(references,capture_policy=QUALIFIED_CAPTURE_POLICY)
        save(status='measuring_references',reference_pack=pack)
        code=gate.run(Path(pack['manifest']),pack['manifest_sha256'],measurement)
        if code:raise VisualError('reference_not_qualified','32-record reference gate failed')
        save(status='automated_gates_pass_independent_review_pending',reference_gate_sha256=file_sha256(measurement/'result.json'),independent_review_status='pending')
    except BaseException as exc:
        save(status='failed' if not isinstance(exc,KeyboardInterrupt) else 'interrupted',error={'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)},failed_evidence_preserved=True)
        raise
    finally:
        save(finished_at=time.time());print(state['status'],flush=True)
if __name__=='__main__':run(*sys.argv[1:])
