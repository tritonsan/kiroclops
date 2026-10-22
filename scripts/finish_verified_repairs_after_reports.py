"""Apply pre-reviewed repairs once both frozen reports match expected evidence.

No trial outcome, threshold, candidate or frozen report is changed. An unexpected
report interpretation stops this coordinator before production modification.
"""
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256


def main():
    base=ROOT/'artifacts/phase3a'
    out=base/'completion-repair-application-pipeline-01'
    assert not out.exists()
    out.mkdir()
    scripts=[ROOT/'scripts/apply_verified_postcohort_repairs.py',
             ROOT/'scripts/verify_applied_postcohort_repairs.py']
    pinned={str(p):file_sha256(p) for p in [Path(__file__),*scripts]}
    state={'status':'waiting_for_frozen_reports','sources':pinned,'rows':[],
           'new_model_run':False,'primary_outcomes_modified':False}
    def save():write_json(out/'pipeline.json',state)
    save()
    report_pipeline=base/'completion-frozen-report-pipeline-01/pipeline.json'
    deadline=time.monotonic()+4*3600
    while True:
        current=read_json(report_pipeline)
        if current['status']=='reports_completed_review_required':break
        if current['status']=='failed_retained' or time.monotonic()>deadline:
            state['status']='not_applied_reports_incomplete';save();return 2
        time.sleep(15)
    assert all(file_sha256(p)==sha for p,sha in pinned.items())
    primary=base/'completion-original-cohort-report-01'
    posthoc=base/'completion-posthoc-compatibility-report-01'
    a,b=read_json(primary/'cohort-report.json'),read_json(posthoc/'result.json')
    assert len(a['rows'])==len(b['rows'])==18
    assert sum(r['status']=='completed' for r in a['rows'])==3
    assert sum(r['status']=='failed' for r in a['rows'])==15
    observed={r['id']+'/'+s['stage'] for r in b['rows'] for s in r['stages']
              if s.get('compatibility_replay',{}).get('overall_acceptance')=='pass'}
    expected={'game-tools-01/jump','game-tools-02/jump','game-tools-03/jump',
              'svg-tools-02/svg','svg-tools-03/svg','svg-baseline-02/svg'}
    assert observed==expected, ('Unexpected supplementary acceptance',sorted(observed))
    unrun=[s for r in b['rows'] for s in r['stages'] if s['stage']=='ground']
    assert len(unrun)==6 and all(s['status']=='not_observed' for s in unrun)
    state.update(status='reports_checked',report_check={
        'primary_completed':3,'primary_failed':15,'supplementary_passed_stages':sorted(observed),
        'unrun_game_stages':6,'primary_report_sha256':file_sha256(primary/'cohort-report.json'),
        'posthoc_report_sha256':file_sha256(posthoc/'result.json')})
    save();print(state['report_check'],flush=True)
    jobs=[('apply',scripts[0],['--primary',str(primary),'--posthoc',str(posthoc)]),
          ('regression',scripts[1],[])]
    for name,script,args in jobs:
        assert all(file_sha256(p)==sha for p,sha in pinned.items())
        row={'id':name,'status':'running','started_at':time.time()}
        state['status']=name+'_running';state['rows'].append(row);save()
        with (out/(name+'.log')).open('w',encoding='utf-8') as stream:
            result=subprocess.run([sys.executable,'-B','-X','utf8',str(script),*args],
                cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        row.update(status='completed' if result.returncode==0 else 'failed_retained',
                   exit_code=result.returncode,finished_at=time.time())
        save();print(name,row['status'],flush=True)
        if result.returncode:
            state['status']=name+'_failed_retained';save();return result.returncode
    state['status']='repairs_applied_and_regression_passed_review_required';save()
    return 0


if __name__=='__main__':raise SystemExit(main())
