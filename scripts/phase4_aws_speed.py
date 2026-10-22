"""Paired speed measurement of the analysis service: standard OpenCV 5.0.0 wheel versus COOL on Graviton.

Phase 4 AWS addition after the V6 freeze. The same frozen V6 panel recordings
(``phase4_aws_v6_equivalence.recordings``: 16 calibration references plus one
Kiro candidate) are submitted ``--repeats`` times to every named deployment.
For each job the worker's own receipt (``delivery-v2.json``, ``timings``)
gives ``worker_wall_seconds``: the wall time from claiming the job to reading
the finished measurement, which includes the package download from S3 with its
integrity check, the sandboxed interpreter start, extraction and the complete
OpenCV 5 analysis, and excludes client upload, queueing, polling and instance
wake-up. Repeats carry a harmless probe tag in the
contract so the service's exact-reuse cache never answers; a cached delivery
(``origin.kind != fresh``) is recorded and excluded from the statistics. The
same recordings are also measured in-process on this machine
(``measure_direct``) as the local baseline.

    python scripts/phase4_aws_speed.py --deployment standard=artifacts/phase4-aws/speed-standard-service-01 \
        --deployment cool=artifacts/phase4-aws/live-cool-02 --repeats 3 --output artifacts/phase4-aws/speed-01

Nothing here changes a measurement result, a threshold or the service code.
"""
from __future__ import annotations

import argparse
import math
import platform
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.cloud.execution import measure_direct  # noqa: E402
from kiroclops.cloud.identity import engine_identity  # noqa: E402
from kiroclops.cloud.remote import RemoteAnalysisBackend  # noqa: E402
from kiroclops.visual.schema import digest, read_json, write_json  # noqa: E402
from scripts.phase4_aws_v6_equivalence import JOB_DEADLINE_S, recordings  # noqa: E402
from services.analysis_api.probe import configure  # noqa: E402

TERMINAL = ('succeeded', 'failed', 'interrupted', 'cancelled')


def _median(values):
    return statistics.median(values) if values else None


def _geomean(values):
    values = [v for v in values if v and v > 0]
    return math.exp(statistics.mean(math.log(v) for v in values)) if values else None


def measure_remote(label, deployment, items, repeats, output, log):
    config = configure(Path(deployment), 'owner')
    client_root = output/('client-'+label)
    backend = RemoteAnalysisBackend(client_root, config)
    rows = []
    for repeat in range(1, repeats+1):
        for item in items:
            # The exact-reuse cache keys on package, selection, contract, sources and engine, so a
            # harmless probe tag in the contract keeps every repeat a fresh computation. The
            # analysis reads only its known contract keys; the measurement is unchanged.
            contract = {**item['contract'], 'speed_probe': {'run': output.name, 'engine': label, 'repeat': repeat}}
            request = {'package_path': str(item['package']), 'selection': item['selection'], 'contract': contract,
                       'candidate_sha256': '3'*64, 'retention_class': 'reference'}
            started = time.monotonic()
            submitted = backend.submit(request, idempotency_key=uuid.uuid4().hex)
            deadline = time.monotonic()+JOB_DEADLINE_S
            while time.monotonic() < deadline:
                job = backend.get_job(submitted['job_id'], wait_seconds=2)
                if job['status'] in TERMINAL:
                    break
            else:
                raise RuntimeError(f'{label} {item["id"]}: service deadline exceeded')
            end_to_end = time.monotonic()-started
            row = {'engine': label, 'repeat': repeat, 'id': item['id'], 'job_id': job['job_id'], 'status': job['status'],
                   'end_to_end_seconds': round(end_to_end, 3)}
            delivery = client_root/'jobs'/job['job_id']/'delivery-v2.json'
            if job['status'] == 'succeeded' and delivery.is_file():
                receipt = read_json(delivery)['receipt']
                identity = receipt.get('engine', {}).get('identity', {})
                row.update(worker_wall_seconds=receipt.get('timings', {}).get('worker_wall_seconds'),
                           origin=receipt.get('origin', {}).get('kind'),
                           opencv_version=identity.get('opencv_version'), architecture=identity.get('architecture'),
                           threads=identity.get('threads'), measurement_sha256=receipt.get('measurement_sha256'))
            else:
                row['error'] = job.get('error')
            rows.append(row)
            write_json(output/f'partial-{label}.json', rows)
            log(f"{label} r{repeat} {item['id']}: {row['status']} worker {row.get('worker_wall_seconds')} s, "
                f"end-to-end {end_to_end:.1f} s, origin {row.get('origin')}")
    state = read_json(Path(deployment)/'state.json')
    return rows, {'deployment': str(deployment), 'stack': state.get('StackName') or state.get('stack'),
                  'engine': state.get('engine', 'standard'), 'ami': state.get('ami_name') or state.get('ami_id'),
                  'instance_type': state.get('InstanceType') or state.get('instance_type') or 'c8g.xlarge',
                  'api_url': state.get('ApiUrl')}


def measure_local(items, repeats, log):
    rows = []
    for repeat in range(1, repeats+1):
        for item in items:
            started = time.perf_counter()
            result = measure_direct(item['package'], item['selection'], item['contract'])
            seconds = time.perf_counter()-started
            rows.append({'engine': 'local', 'repeat': repeat, 'id': item['id'], 'status': 'succeeded',
                         'worker_wall_seconds': round(seconds, 3), 'origin': 'fresh',
                         'measurement_sha256': digest({k: result[k] for k in ('observations', 'events') if k in result})})
            log(f"local r{repeat} {item['id']}: {seconds:.2f} s")
    return rows


def summarize(rows, hourly):
    """Per-engine medians, per-recording medians and paired ratios against the first remote engine."""
    engines = []
    for row in rows:
        if row['engine'] not in engines:
            engines.append(row['engine'])
    fresh = [r for r in rows if r.get('status') == 'succeeded' and r.get('origin') == 'fresh' and r.get('worker_wall_seconds')]
    per_engine, per_recording = {}, {}
    for engine in engines:
        values = [r['worker_wall_seconds'] for r in fresh if r['engine'] == engine]
        e2e = [r['end_to_end_seconds'] for r in rows if r['engine'] == engine and 'end_to_end_seconds' in r]
        per_engine[engine] = {'jobs': len(values), 'median_seconds': _median(values), 'min_seconds': min(values) if values else None,
                              'max_seconds': max(values) if values else None, 'mean_seconds': statistics.mean(values) if values else None,
                              'median_end_to_end_seconds': _median(e2e),
                              'cached_or_failed': sum(1 for r in rows if r['engine'] == engine and r not in fresh)}
        rate = hourly.get(engine)
        if rate is not None and per_engine[engine]['median_seconds']:
            per_engine[engine]['usd_per_hour'] = rate
            per_engine[engine]['usd_per_analysis_compute'] = rate/3600*per_engine[engine]['median_seconds']
    ids = []
    for row in rows:
        if row['id'] not in ids:
            ids.append(row['id'])
    for item in ids:
        per_recording[item] = {engine: _median([r['worker_wall_seconds'] for r in fresh if r['engine'] == engine and r['id'] == item])
                               for engine in engines}
    ratios = {}
    remote = [e for e in engines if e != 'local']
    if len(remote) >= 2:
        base, other = remote[0], remote[1]
        paired = [(per_recording[i][other]/per_recording[i][base]) for i in ids
                  if per_recording[i].get(base) and per_recording[i].get(other)]
        ratios = {'baseline': base, 'engine': other, 'paired_recordings': len(paired),
                  'median_ratio': _median(paired), 'geometric_mean_ratio': _geomean(paired),
                  'min_ratio': min(paired) if paired else None, 'max_ratio': max(paired) if paired else None,
                  'speedup_percent_median': (1-_median(paired))*100 if paired else None}
    return {'engines': per_engine, 'per_recording': per_recording, 'paired': ratios}


def markdown(report):
    s = report['summary']
    lines = [f"# Analysis-service speed: {', '.join(report['deployments'])} and local ({report['created_at']})", '',
             f"Recordings: {report['recordings']} frozen V6 panel recordings, {report['repeats']} repeats per remote engine, "
             f"{report['local_repeats']} local repeats. Worker time = `worker_wall_seconds` from each delivery receipt "
             "(claim to finished measurement: S3 package download and integrity check, sandbox start, extraction, full OpenCV 5 analysis); client upload, queue, polling and wake-up excluded.", '',
             '| engine | OpenCV | jobs | median s | min s | max s | median end-to-end s | USD/h | USD per analysis (compute) |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for engine, row in s['engines'].items():
        version = report['engine_versions'].get(engine, '-')
        fmt = lambda v, n=3: '-' if v is None else f'{v:.{n}f}'
        lines.append(f"| {engine} | {version} | {row['jobs']} | {fmt(row['median_seconds'])} | {fmt(row['min_seconds'])} | {fmt(row['max_seconds'])} | "
                     f"{fmt(row['median_end_to_end_seconds'], 1)} | {fmt(row.get('usd_per_hour'), 4)} | {fmt(row.get('usd_per_analysis_compute'), 5)} |")
    if s['paired']:
        p = s['paired']
        lines += ['', f"Paired per-recording ratio {p['engine']}/{p['baseline']} over {p['paired_recordings']} recordings: "
                      f"median {p['median_ratio']:.3f}, geometric mean {p['geometric_mean_ratio']:.3f}, range {p['min_ratio']:.3f}-{p['max_ratio']:.3f} "
                      f"(median speed-up {p['speedup_percent_median']:+.1f}%).", '']
    lines += ['| recording | ' + ' | '.join(f'{e} median s' for e in s['engines']) + ' |', '|---|' + '---:|'*len(s['engines'])]
    for item, row in s['per_recording'].items():
        lines.append(f"| {item} | " + ' | '.join('-' if row.get(e) is None else f'{row[e]:.3f}' for e in s['engines']) + ' |')
    lines += ['', 'Scope: wall-time of the complete KiroClops measurement per recording on one c8g.xlarge worker with 4 threads. '
              'Not a kernel micro-benchmark; Python, I/O and sandbox start are part of every number. Prices are on-demand list prices '
              'for the instance only (the COOL Marketplace fee is listed separately when known). No measurement result, threshold or service code was changed.']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--deployment', action='append', required=True, help='label=path, e.g. standard=artifacts/phase4-aws/speed-standard-service-01')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--local-repeats', type=int, default=3)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--hourly', action='append', default=[], help='label=USD per hour on-demand, e.g. standard=0.1595')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output
    if output.exists():
        raise SystemExit(f'Use a fresh result directory: {output}')
    output.mkdir(parents=True)
    log_path = output/'run.log'

    def log(text):
        line = f"{datetime.now().strftime('%H:%M:%S')} {text}"
        print(line, flush=True)
        with log_path.open('a', encoding='utf-8') as handle:
            handle.write(line+'\n')
    items = recordings()
    if args.limit:
        items = items[:args.limit]
    hourly = {k: float(v) for k, v in (h.split('=', 1) for h in args.hourly)}
    rows, deployments, versions = [], [], {}
    for spec in args.deployment:
        label, path = spec.split('=', 1)
        remote_rows, info = measure_remote(label, path, items, args.repeats, output, log)
        rows += remote_rows
        deployments.append(label)
        versions[label] = next((r.get('opencv_version') for r in remote_rows if r.get('opencv_version')), None)
        info['label'] = label
        write_json(output/f'deployment-{label}.json', info)
    if args.local_repeats:
        rows += measure_local(items, args.local_repeats, log)
        local = engine_identity(4)['identity']
        versions['local'] = f"{local.get('opencv_version')} ({local.get('architecture')}, {platform.system()})"
    report = {'kind': 'AnalysisServiceSpeed', 'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'deployments': deployments, 'engine_versions': versions, 'recordings': len(items), 'repeats': args.repeats,
              'local_repeats': args.local_repeats, 'hourly_usd': hourly, 'rows': rows, 'summary': summarize(rows, hourly),
              'scope': 'Wall-time per complete recording measurement; excludes upload, queue, polling, wake-up. No result, threshold or service code change.'}
    report['report_sha256'] = digest({k: v for k, v in report.items()})
    write_json(output/'report.json', report)
    (output/'REPORT.md').write_text(markdown(report), encoding='utf-8')
    log('done: ' + ', '.join(f"{e} median {v['median_seconds']}" for e, v in report['summary']['engines'].items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
