"""Measure V6 panel recordings on the AWS analysis service and locally, and compare them.

Phase 4 AWS addition, after the V6 freeze. It reads the frozen V6 calibration
preparation (``C:/kc3d/p/v6p01-cal``) and one Kiro candidate recording of the
V6 session (``C:/kc3d/s/v6p01``) without changing them. For every recording the
same package, selection and suite event contract are measured

* remotely, through the deployed service (S3 upload, SQS queue, Graviton worker), and
* locally, in-process with ``measure_direct`` (the same V6 code),

and compared with the service's cross-engine policy (``kiroclops.cloud.equivalence``,
engine-equivalence-v1). Movement thresholds are not involved. The worker's
analysis sources are compared with the local ``kiroclops/visual`` hashes.

    python scripts/phase4_aws_v6_equivalence.py --deployment artifacts/phase4-aws/v6-service-01 \
        --output artifacts/phase4-aws/v6-equivalence-01
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.cloud.equivalence import compare  # noqa: E402
from kiroclops.cloud.execution import measure_direct  # noqa: E402
from kiroclops.cloud.identity import analysis_sources, engine_identity  # noqa: E402
from kiroclops.cloud.remote import RemoteAnalysisBackend  # noqa: E402
from kiroclops.visual.schema import file_sha256, read_json, write_json  # noqa: E402
from services.analysis_api.probe import configure  # noqa: E402

CALIBRATION = Path('C:/kc3d/p/v6p01-cal')
SESSION = Path('C:/kc3d/s/v6p01')
# Kiro's second candidate in the V6 session, recorded on the reverse calibration case.
CANDIDATE_JOB = '6dbff94f'
CANDIDATE_CASE = 'realistic-suite-panel-calibration-reverse-w960-r1'
JOB_DEADLINE_S = 300


def recordings():
    rows = []
    for case in sorted(p for p in (CALIBRATION/'reference').iterdir() if p.is_dir()):
        record = read_json(case/'record.json')
        rows.append({'id': 'reference/'+case.name, 'package': Path(record['package']),
                     'selection': record['selection'], 'contract': record['suite_event_contract']})
    job = next(p for p in (SESSION/'jobs').iterdir() if p.name.startswith(CANDIDATE_JOB))
    result = read_json(job/'result.json')
    media = next(m for m in result['candidate_media'] if m['scenario_id'] == CANDIDATE_CASE)
    reference = next(r for r in rows if r['id'] == 'reference/'+CANDIDATE_CASE)
    # The candidate is measured with the reference selection, as in the session's evaluation.
    rows.append({'id': 'kiro-candidate-02/'+CANDIDATE_CASE, 'package': Path(media['package']),
                 'selection': reference['selection'], 'contract': reference['contract']})
    return rows


def run(deployment, output):
    deployment, output = Path(deployment), Path(output)
    if output.exists():
        raise ValueError('Use a fresh result directory')
    config = configure(deployment, 'owner')
    output.mkdir(parents=True)
    (output/'jobs').mkdir()
    backend = RemoteAnalysisBackend(output/'client', config)
    local_engine = engine_identity(4)
    # The worker reports the visual sources plus the runner files it executes; each must equal the local file.
    remote_sources = config.get('analysis_sources') or {}
    local_sources = {**analysis_sources(ROOT), **{k: file_sha256(ROOT/k) for k in remote_sources}}
    rows = []
    for item in recordings():
        request = {'package_path': str(item['package']), 'selection': item['selection'], 'contract': item['contract'],
                   'candidate_sha256': '3'*64, 'retention_class': 'reference'}
        started = time.monotonic()
        submitted = backend.submit(request, idempotency_key=uuid.uuid4().hex)
        deadline = time.monotonic()+JOB_DEADLINE_S
        while time.monotonic() < deadline:
            job = backend.get_job(submitted['job_id'], wait_seconds=2)
            if job['status'] in ('succeeded', 'failed', 'interrupted', 'cancelled'):
                break
        else:
            raise RuntimeError(item['id']+': service deadline exceeded')
        remote_seconds = time.monotonic()-started
        write_json(output/'jobs'/(item['id'].replace('/', '__')+'.json'), job)
        row = {'id': item['id'], 'job_id': job['job_id'], 'status': job['status'], 'end_to_end_seconds': round(remote_seconds, 3)}
        if job['status'] == 'succeeded':
            started = time.monotonic()
            expected = measure_direct(item['package'], item['selection'], item['contract'])
            row['local_seconds'] = round(time.monotonic()-started, 3)
            row['equivalence'] = compare(expected, {k: job['result'][k] for k in ('observations', 'events')})
            row['remote_opencv_version'] = job['result']['observations'].get('opencv_version')
        else:
            row['error'] = job.get('error')
        rows.append(row)
        write_json(output/'partial.json', rows)
        verdict = 'equivalent' if row.get('equivalence', {}).get('equivalent') else 'NOT equivalent'
        print(f"{item['id']}: {job['status']}, {verdict}, {remote_seconds:.1f} s", flush=True)
    passed = sum(1 for r in rows if r.get('equivalence', {}).get('equivalent'))
    result = {'kind': 'V6AwsServiceEquivalence', 'checked_at': datetime.now(timezone.utc).isoformat(),
              'deployment': read_json(deployment/'state.json'),
              'status': 'pass' if passed == len(rows) and local_sources == remote_sources else 'fail',
              'equivalent': passed, 'recordings': len(rows),
              'exact': sum(1 for r in rows if r.get('equivalence', {}).get('exact')),
              'analysis_sources_match': local_sources == remote_sources,
              'local_engine': local_engine['identity'], 'remote_engine_sha256': config.get('engine_sha256'),
              'rows': rows,
              'scope': 'Cross-engine measurement equivalence of frozen V6 recordings. No acceptance, '
                       'movement-threshold or Kiro claim. Phase 4 AWS addition after the V6 freeze.'}
    write_json(output/'result.json', result)
    print(f"{result['status']}: {passed}/{len(rows)} equivalent, analysis sources match: {result['analysis_sources_match']}", flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    run(args.deployment, args.output)
