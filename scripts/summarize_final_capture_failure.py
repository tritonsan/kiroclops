"""Bind a completed trial's unavailable result to its actual failed capture."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--trial', required=True)
    p.add_argument('--stage', required=True)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    base = ROOT / 'artifacts/phase3a/completion-paired-cohort-01'
    row = next(r for r in read_json(base/'progress.json')['rows'] if r['id'] == args.trial)
    assert row['status'] == 'failed'
    s = base/'sessions'/args.trial/args.stage
    assert s.resolve().is_relative_to((base/'sessions').resolve())
    st = read_json(s/'session.json'); launch = read_json(s/'kiro/launch.json')
    assert not st.get('active_job_id') and launch.get('finished_at')
    job_path = s/'jobs'/st['final_job_id']/'job.json'; job = read_json(job_path)
    assert job['status'] == 'failed' and job['error']['code'] == 'capture_timing_failure'
    assert not job_path.with_name('result.json').exists()
    captures = sorted((s/'measurements'/st['final_job_id']).glob('*/*/capture-result.json'))
    failures = []
    for path in captures:
        value = read_json(path)
        if value['timing']['status'] != 'pass':
            failures.append({'capture':path.relative_to(s).as_posix(),
                'capture_result_sha256':file_sha256(path),'timing':value['timing'],
                'functional_status':value['functional']['status']})
    assert failures
    native_path = ROOT/'artifacts/phase3a/completion-20260927'/f'{args.trial}-native-only-audit.json'
    native = read_json(native_path)
    assert native['native']['status'] == 'pass' and not native['full_source_or_image_acceptance']
    out = {'kind':'ReadOnlyCaptureFailureDiagnosis','trial':args.trial,
        'classification':job['error']['code'],'job_error':job['error'],
        'capture_result_count':len(captures),'failed_captures':failures,
        'native_turn_completed':launch['native_turn_status']=='completed',
        'model':launch['model'],'effort':launch['effort'],
        'native_audit_sha256':file_sha256(native_path),
        'full_final_motion_result_available':False,'primary_outcome_modified':False,
        'evidence_hashes':{str(path.relative_to(ROOT)):file_sha256(path) for path in
            (job_path,s/'session.json',s/'kiro/launch.json')},
        'producer_sha256':file_sha256(__file__),
        'root_cause':'Capture timing limit exceeded. Underlying scheduler/browser cause is not established; missing final measurement cannot be replaced by calibration success.'}
    assert not args.output.exists()
    write_json(args.output,out)
    print({'trial':args.trial,'captures':len(captures),'failures':len(failures),
        'native':'pass','full_final_motion_result':'unavailable'})


if __name__ == '__main__':
    main()
