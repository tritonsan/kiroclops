"""Reproducible calibration benchmark, with profiling outside timing trials."""
from __future__ import annotations

import argparse
import cProfile
from pathlib import Path
import pstats
import statistics
import time

from kiroclops.visual.schema import digest, read_json, write_json, VisualError
from .corpus import freeze, verify_corpus
from .identity import engine_identity, analysis_sources


def measure(package, selection, contract):
    from kiroclops.visual.tracking import analyze_package
    from kiroclops.visual.events import extract_events
    start, cpu = time.perf_counter(), time.process_time()
    obs = analyze_package(package, selection, contract)
    tracked = time.perf_counter()
    events = extract_events(obs, selection, contract)
    end = time.perf_counter()
    return {'observations': obs, 'events': events}, {
        'direct_wall_s': end - start, 'tracking_with_validation_decode_s': tracked - start,
        'events_s': end - tracked, 'process_cpu_s': time.process_time() - cpu}


def summarize_profile(profile):
    stats = pstats.Stats(profile)
    rows = []
    for (file, line, function), (primitive, calls, exclusive, cumulative, _) in stats.stats.items():
        rows.append({'file': Path(file).name, 'line': line, 'function': function,
                     'calls': calls, 'exclusive_s': exclusive, 'inclusive_s': cumulative})
    categories = {}
    for label, names in {
        'package_validation_inclusive': {'load_package'},
        'png_read_decode_inclusive': {'read_image'},
        'png_decode_native': {'imdecode'},
        'hash_inclusive': {'file_sha256'},
        'tracking_inclusive': {'analyze_package'},
        'events_inclusive': {'extract_events'},
        'disk_read_native': {'read', 'read_bytes'},
    }.items():
        matching = [r for r in rows if any(r['function'] == n or (r['function'].startswith('<') and n in r['function']) for n in names)]
        categories[label] = {'exclusive_s': sum(r['exclusive_s'] for r in matching),
                             'inclusive_s': sum(r['inclusive_s'] for r in matching),
                             'calls': sum(r['calls'] for r in matching)}
    return {'categories': categories, 'top_exclusive': sorted(rows, key=lambda r: -r['exclusive_s'])[:40],
            'note': 'Inclusive categories overlap; do not sum. Profiling trials are excluded from benchmark timing.'}


def local_benchmark(corpus_dir, output, *, repeats=5, threads=4, worker=True, profile=True, code_variant='baseline'):
    corpus_dir, output = Path(corpus_dir).resolve(), Path(output).resolve()
    if output.exists():
        raise VisualError('output_exists', 'Use a fresh benchmark result directory')
    if repeats < 1:
        raise VisualError('invalid_repeats', 'At least one timed trial is required')
    corpus = verify_corpus(corpus_dir)
    sources = analysis_sources()
    if code_variant == 'baseline' and sources != corpus['analysis_sources']:
        raise VisualError('source_changed', 'Run the frozen baseline with its registered analysis code')
    engine = engine_identity(threads)
    output.mkdir(parents=True)
    write_json(output / 'engine.json', engine)
    record = {'schema_version': 1, 'kind': 'LocalCalibrationBenchmark', 'status': 'running',
              'corpus_sha256': corpus['corpus_sha256'], 'analysis_sources': sources,
              'engine_sha256': engine['engine_sha256'], 'repeats': repeats, 'rows': [],
              'code_variant': code_variant, 'corpus_analysis_sources': corpus['analysis_sources'],
              'scope': {'direct': 'analyze_package + extract_events, includes their validation and image decoding',
                        'local_end_to_end': 'LocalAnalysisBackend.submit through verified result delivery; snapshot, process launch, polling and JSON included',
                        'local_end_to_end_repeats': 1 if worker else 0,
                        'excluded': 'capture, corpus creation, environment install, engine hashing, profiling'}}
    write_json(output / 'result.json', record)
    for item in corpus['items']:
        parent = corpus_dir / item['id']
        package = corpus_dir / item['package']
        selection, contract = read_json(parent / 'selection.json'), read_json(parent / 'contract.json')
        historical = read_json(parent / 'historical.json')
        result, warmup = measure(package, selection, contract)
        expected_sha = digest(result)
        row = {'id': item['id'], 'domain': item['domain'], 'package_sha256': item['package_sha256'],
               'warmup_s': warmup['direct_wall_s'], 'historical_observations_match': result['observations'] == historical['observations'],
               'historical_events_match': result['events'] == historical['events'] if 'events' in historical else None,
               'measurement_status': result['observations'].get('measurement_status'),
               'measurement_sha256': expected_sha, 'timings': []}
        write_json(output / item['id'] / 'measurement.json', result)
        for i in range(repeats):
            current, timing = measure(package, selection, contract)
            row['timings'].append({'repeat': i + 1, **timing, 'same_measurement': digest(current) == expected_sha})
        if worker:
            from kiroclops.local_engine.backend import LocalAnalysisBackend
            backend = LocalAnalysisBackend(output / item['id'] / 'worker-store', timeout_seconds=300)
            start, cpu = time.perf_counter(), time.process_time()
            submitted = backend.submit({'operation': 'analyze', 'package_path': str(package),
                                        'selection': selection, 'contract': contract},
                                       idempotency_key='baseline-' + item['id'])
            submit_done = time.perf_counter()
            while True:
                job = backend.get_job(submitted['job_id'], wait_seconds=1)
                if job['status'] in ('succeeded', 'failed', 'cancelled', 'interrupted'):
                    break
                if time.perf_counter() - start > 330:
                    backend.cancel_job(submitted['job_id'])
                    raise VisualError('benchmark_timeout', 'Worker exceeded benchmark budget')
            end = time.perf_counter()
            if job['status'] != 'succeeded':
                raise VisualError('benchmark_worker_failed', str(job.get('error')))
            measured = {k: job['result'][k] for k in ('observations', 'events')}
            row['worker'] = {'job_id': job['job_id'], 'wall_s': end - start,
                             'submit_snapshot_s': submit_done - start, 'poll_delivery_s': end - submit_done,
                             'process_parent_cpu_s': time.process_time() - cpu,
                             'worker_lifetime_s': job['finished_at'] - job['started_at'],
                             'same_measurement': digest(measured) == expected_sha,
                             'thread_policy': 'Unmodified production worker defaults; separate from explicit direct thread setting'}
        if profile:
            profiler = cProfile.Profile()
            profiler.enable()
            profiled, _ = measure(package, selection, contract)
            profiler.disable()
            profiler.dump_stats(str(output / item['id'] / 'analysis.prof'))
            write_json(output / item['id'] / 'profile.json', summarize_profile(profiler))
            row['profile_same_measurement'] = digest(profiled) == expected_sha
        row['direct_median_s'] = statistics.median(t['direct_wall_s'] for t in row['timings'])
        row['valid'] = (row['historical_observations_match'] and row['historical_events_match'] is not False
                        and all(t['same_measurement'] for t in row['timings'])
                        and row.get('worker', {}).get('same_measurement', True)
                        and row.get('profile_same_measurement', True))
        record['rows'].append(row)
        write_json(output / 'result.json', record)
        print(f"{item['id']}: {row['direct_median_s']:.3f}s direct, integrity={row['valid']}", flush=True)
    verify_corpus(corpus_dir)
    record['sources_unchanged'] = sources == analysis_sources()
    record['status'] = 'pass' if record['sources_unchanged'] and all(r['valid'] for r in record['rows']) else 'failed'
    write_json(output / 'result.json', record)
    lines = ['# Local baseline measurement', '', f"Status: **{record['status']}**. 12 calibration packages; each has one warm-up and {repeats} timing repeats.", '',
             '| Package | Direct median s | Local end-to-end s (single repeat) | Result binding |', '| --- | ---: | ---: | --- |']
    for row in record['rows']:
        lines.append(f"| {row['id']} | {row['direct_median_s']:.3f} | {row.get('worker',{}).get('wall_s',0):.3f} | {row['valid']} |")
    lines += ['', 'Profiling times are not included in the timing repeats; nested totals cannot be summed.',
              'End-to-end time excludes capture/setup. Worker times include the process, copying and result delivery.',
              'This result is not a COOL measurement. The earlier SVG ROI optimization is already in the baseline code.']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    f = sub.add_parser('freeze'); f.add_argument('--root', type=Path, default=Path.cwd()); f.add_argument('--output', type=Path, required=True)
    b = sub.add_parser('local'); b.add_argument('--corpus', type=Path, required=True); b.add_argument('--output', type=Path, required=True)
    b.add_argument('--repeats', type=int, default=5); b.add_argument('--threads', type=int, default=4)
    b.add_argument('--no-worker', action='store_true'); b.add_argument('--no-profile', action='store_true')
    b.add_argument('--code-variant', choices=['baseline', 'local-optimization'], default='baseline')
    args = parser.parse_args()
    if args.command == 'freeze':
        result = freeze(args.root, args.output)
        print(result['corpus_sha256'])
        return 0
    try:
        return 0 if local_benchmark(args.corpus, args.output, repeats=args.repeats, threads=args.threads,
                                    worker=not args.no_worker, profile=not args.no_profile,
                                    code_variant=args.code_variant)['status'] == 'pass' else 1
    except BaseException as exc:
        if args.output.exists():
            write_json(args.output / 'failure.json', {'status':'interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',
                'code':getattr(exc,'code',type(exc).__name__), 'message':str(exc),
                'partial_results_are_not_complete':True})
        raise


if __name__ == '__main__':
    raise SystemExit(main())
