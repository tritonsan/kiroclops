"""Build the two frozen reports after all available final reviews are published.

No model, capture, production edit, retry or primary outcome replacement.
"""
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json,write_json,file_sha256
from prepare_review_between_trials import start_owned_review,stop_owned_review


def main():
    base=ROOT/'artifacts/phase3a';cohort=base/'completion-paired-cohort-01'
    out=base/'completion-frozen-report-pipeline-01';assert not out.exists();out.mkdir()
    registry=cohort/'registration.json';progress=cohort/'progress.json'
    sources=[Path(__file__),registry,progress,ROOT/'scripts/build_local_cohort_report.py',
             ROOT/'scripts/build_posthoc_compatibility_report.py']
    hashes={str(p):file_sha256(p) for p in sources}
    state={'status':'waiting_for_final_visual_reviews','rows':[],'inputs':hashes,
           'new_model_run':False,'production_modified':False,'primary_outcomes_modified':False}
    def save():write_json(out/'pipeline.json',state)
    save();deadline=time.monotonic()+24*3600
    while True:
        assert all(file_sha256(p)==sha for p,sha in hashes.items()),'Frozen input changed'
        rows=read_json(progress)['rows'];assert len(rows)==18
        finals=[]
        for row in rows:
            assert row['status'] in ('completed','failed','interrupted')
            for stage in row['stages']:
                session=Path(stage['session'])
                if not (session/'session.json').exists():continue
                s=read_json(session/'session.json');assert not s.get('active_job_id')
                if (s.get('final') or {}).get('status')=='succeeded':
                    finals.append(session/'independent_review.json')
        assert len(finals)==12
        state['published_reviews']=sum(p.is_file() for p in finals);save()
        if all(p.is_file() for p in finals):break
        assert time.monotonic()<deadline,'Reviews did not finish within the declared wait'
        time.sleep(20)
    state['review_hashes']={str(p):file_sha256(p) for p in finals};save()
    primary=base/'completion-original-cohort-report-01'
    posthoc=base/'completion-posthoc-compatibility-report-01'
    jobs=[('primary',ROOT/'scripts/build_local_cohort_report.py',
           ['--registration',str(registry),'--output',str(primary)]),
          ('posthoc',ROOT/'scripts/build_posthoc_compatibility_report.py',
           ['--registration',str(registry),'--primary-report',str(primary),'--output',str(posthoc)])]
    for name,script,args in jobs:
        assert all(file_sha256(p)==sha for p,sha in state['review_hashes'].items())
        row={'id':name,'status':'running','started_at':time.time()}
        state['status']='building_'+name;state['rows'].append(row);save()
        with (out/(name+'.log')).open('w',encoding='utf-8') as stream:
            child,owner=start_owned_review([sys.executable,'-B','-X','utf8',str(script),*args],stream)
            limit=time.monotonic()+4*3600
            try:
                while child.poll() is None and time.monotonic()<limit:time.sleep(2)
            finally:stop_owned_review(child,owner)
        row.update(status='completed' if child.returncode==0 else 'failed',
            exit_code=child.returncode,finished_at=time.time(),owned_process_tree_closed=True)
        save();print(name,row['status'],flush=True)
        if child.returncode:
            state['status']='failed_retained';save();return 2
    state.update(status='reports_completed_review_required',primary=str(primary),posthoc=str(posthoc))
    save();return 0


if __name__=='__main__':raise SystemExit(main())
