"""Two prospectively declared fixture probes for the newly observed SVG gap.

Wait for the original eight probes; no model/candidate/replacement trial.
Original cohort and original diagnostic declaration stay unchanged.
"""
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json,write_json,file_sha256
from prepare_review_between_trials import start_owned_review,stop_owned_review


def main():
    base=ROOT/'artifacts/phase3a'
    output=base/'completion-final-svg-recorder-diagnostics-01'
    assert not output.exists();output.mkdir()
    prerequisite=base/'completion-postcohort-recorder-diagnostics-01/pipeline.json'
    producer=ROOT/'scripts/diagnose_capture_stalls.py'
    diagnosis=base/'completion-20260927/svg-baseline-03-capture-diagnostic.json'
    registration=base/'completion-paired-cohort-01/registration.json'
    proposal=base/'static-sampling-compatibility-01/result.json'
    bindings={str(p):file_sha256(p) for p in (Path(__file__),producer,diagnosis,registration,proposal)}
    state={'kind':'AdditionalDeclaredRecorderDiagnostic','status':'waiting_for_original_eight_probes',
        'created_at':time.time(),'scenario_id':'three-to-five-w1280-r1','domain':'svg',
        'policies':['frozen','earlier-real-png'],'rows':[],'input_hashes':bindings,
        'reason':'Newly observed182.700ms gap in unchanged BEFORE source of final original SVG trial.',
        'model_run':False,'candidate_source_used':False,'acceptance_run':False,
        'thresholds_changed':False,'primary_outcomes_modified':False,'retries':0,
        'scope':'One attempt per policy on the fixture target after original diagnostics. No statistical reliability or old-trial repair claim.'}
    def save(**values):
        state.update(values);write_json(output/'pipeline.json',state)
    save();deadline=time.monotonic()+24*3600
    while True:
        prior=read_json(prerequisite)
        if prior['status']=='diagnostics_finished_interpretation_required':break
        if prior['status'] in ('wait_expired_no_capture','not_run_predecessor_incomplete'):
            save(status='not_run_predecessor_incomplete',finished_at=time.time());return 2
        if time.monotonic()>=deadline:
            save(status='wait_expired_no_capture',finished_at=time.time());return 2
        time.sleep(15)
    assert all(file_sha256(p)==sha for p,sha in bindings.items())
    save(status='capturing_additional_fixture_diagnostics',prerequisite_sha256=file_sha256(prerequisite))
    for policy in state['policies']:
        folder=output/policy
        row={'policy':policy,'status':'running','started_at':time.time()}
        state['rows'].append(row);save()
        command=[sys.executable,'-B','-X','utf8',str(producer),'--domain','svg',
            '--scenario',state['scenario_id'],'--static-policy',policy,'--output',str(folder)]
        with (output/(policy+'.log')).open('w',encoding='utf-8') as stream:
            child,owner=start_owned_review(command,stream);limit=time.monotonic()+300
            try:
                while child.poll() is None and time.monotonic()<limit:time.sleep(1)
            finally:stop_owned_review(child,owner)
        receipt=folder/'timing-probe.json'
        row.update(status='recorded' if receipt.exists() else 'probe_incomplete',
            exit_code=child.returncode,finished_at=time.time(),owned_tree_closed=True)
        if receipt.exists():
            value=read_json(receipt);row.update(timing=value.get('timing'),error=value.get('error'),
                receipt_sha256=file_sha256(receipt),source_unchanged=value['source_unchanged'])
        save();print(policy,row['status'],flush=True)
    save(status='diagnostics_finished_interpretation_required',finished_at=time.time());return 0


if __name__=='__main__':raise SystemExit(main())
