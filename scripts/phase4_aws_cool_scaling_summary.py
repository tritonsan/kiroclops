"""Summarise ``phase4_aws_cool_scaling.py`` runs of several instance types into summary.json and REPORT.md.

Post-hoc measurement after the V6 freeze. For every instance type and engine it takes the
median over rounds of each round's value: kernel medians by image size and thread count,
``measure_direct`` wall times by thread count, and the throughput of the whole instance
(one single-threaded process per vCPU). Cost per 1,000 analyses is the hourly price divided
by that throughput: on-demand Linux prices in us-east-1 from the AWS Price List API
(effective 1 October 2026) and the COOL software fee from its Marketplace listing.
Compute only: the service's per-job overhead (upload, integrity checks, sandbox start) is
not part of these numbers.

    python scripts/phase4_aws_cool_scaling_summary.py artifacts/phase4-aws/cool-scaling-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

ON_DEMAND_USD_PER_HOUR = {'c8g.xlarge': .15952, 'c8g.4xlarge': .63808, 'c8i.xlarge': .18742, 'c8a.xlarge': .21554}
COOL_FEE_USD_PER_HOUR = {'c8g.xlarge': .01, 'c8g.4xlarge': .04}
KEY_KERNELS = ('connectedComponentsWithStats_mask', 'matchTemplate_ccoeff_normed_full_frame_64px', 'phaseCorrelate_crop',
               'goodFeaturesToTrack_frame', 'gaussianBlur_5x5_frame', 'imdecode_png_frame', 'calcOpticalFlowPyrLK_100_points',
               'matchTemplate_ccoeff_normed_200px_window_48px')


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def load(folder):
    runs = {}
    for path in sorted((folder/'runs').glob('result-*.json')):
        run = json.loads(path.read_text(encoding='utf-8'))
        runs.setdefault(run['engine'], []).append(run)
    return runs


def instance_summary(folder):
    instance_type = folder.name
    runs = load(folder)
    if not runs:
        return None
    first = next(iter(runs.values()))[0]
    env = first['environment']
    value = {'instance_type': instance_type, 'cpu': env['lscpu'].get('Model name') or env['lscpu'].get('Vendor ID'),
             'vcpus': env['cpu_count'], 'threads_per_core': env['lscpu'].get('Thread(s) per core'),
             'cpu_features': env['cpu_features'], 'architecture': env['machine'],
             'ec2_usd_per_hour': ON_DEMAND_USD_PER_HOUR.get(instance_type), 'engines': {}}
    for engine, rs in sorted(runs.items()):
        env = rs[0]['environment']
        fee = COOL_FEE_USD_PER_HOUR.get(instance_type, 0.) if engine == 'cool' else 0.
        price = (value['ec2_usd_per_hour'] or 0.)+fee
        kernels = {}
        for size in rs[0]['kernels']:
            kernels[size] = {}
            for threads in rs[0]['kernels'][size]:
                kernels[size][threads] = {name: median(r['kernels'][size][threads][name]['median_s'] for r in rs)
                                          for name in rs[0]['kernels'][size][threads]}
        pipeline = {}
        for row in rs[0]['pipeline']:
            walls = [next(x['wall_median_s'] for x in r['pipeline'] if x['threads'] == row['threads'] and x['name'] == row['name']) for r in rs]
            pipeline.setdefault(str(row['threads']), {})[row['name']] = median(walls)
        throughput = median(r['throughput']['analyses_per_hour'] for r in rs)
        sequential = {threads: 3600/statistics.mean(times.values()) for threads, times in pipeline.items()}
        value['engines'][engine] = {
            'opencv_version': env['opencv_version'], 'numpy_version': env['numpy_version'], 'rounds': len(rs),
            'engine_check': all(r['engine_check'] for r in rs), 'usd_per_hour': price,
            'kernels_s': kernels, 'pipeline_s': pipeline,
            'throughput_analyses_per_hour': throughput,
            'throughput_runs': [round(r['throughput']['analyses_per_hour'], 1) for r in rs],
            'throughput_range': [min(r['throughput']['analyses_per_hour'] for r in rs), max(r['throughput']['analyses_per_hour'] for r in rs)],
            'latest_start_after_signal_s': max(r['throughput']['latest_start_after_signal_s'] for r in rs),
            'sequential_analyses_per_hour_by_threads': sequential,
            'usd_per_1000_analyses': price/throughput*1000 if throughput and price else None,
            'measurement_hashes_per_recording': {row['name']: sorted({h for r in rs for x in r['pipeline'] if x['name'] == row['name']
                                                                      for h in x['measurement_sha256']}) for row in rs[0]['pipeline']}}
    if {'cool', 'stock'} <= set(value['engines']):
        cool, stock = value['engines']['cool'], value['engines']['stock']
        value['standard_over_cool'] = {
            'kernels': {size: {threads: {name: stock['kernels_s'][size][threads][name]/cool['kernels_s'][size][threads][name]
                                         for name in cool['kernels_s'][size][threads]}
                               for threads in cool['kernels_s'][size]} for size in cool['kernels_s']},
            'pipeline': {threads: {name: stock['pipeline_s'][threads][name]/cool['pipeline_s'][threads][name] for name in cool['pipeline_s'][threads]}
                         for threads in cool['pipeline_s']},
            'throughput': cool['throughput_analyses_per_hour']/stock['throughput_analyses_per_hour']}
    value['runs_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((folder/'runs').glob('result-*.json'))}
    return value


def fmt(x, digits=2):
    return '—' if x is None else f'{x:.{digits}f}'


def report(summary):
    rows = [i for i in summary['instances'] if i]
    lines = ['# COOL scaling: image size, threads, throughput and cost per analysis', '',
             summary['label']+'. '+summary['method'], '',
             '## Instances', '', '| instance | CPU | vCPUs | threads per core | engine | OpenCV | rounds | engine check | USD/h |',
             '|---|---|---:|---:|---|---|---:|---|---:|']
    for i in rows:
        for engine, e in i['engines'].items():
            lines.append(f"| {i['instance_type']} | {i['cpu']} | {i['vcpus']} | {i['threads_per_core']} | {engine} | {e['opencv_version']} | "
                         f"{e['rounds']} | {'pass' if e['engine_check'] else 'FAIL'} | {fmt(e['usd_per_hour'], 4)} |")
    lines += ['', '## Throughput and cost (compute only)', '',
              'One single-threaded process per vCPU, each measuring the panel, game and SVG recordings in turn; median over rounds.', '',
              '| instance | engine | analyses per hour, median (range of rounds) | USD per 1,000 analyses | one analysis at a time, all vCPUs as threads: analyses per hour |',
              '|---|---|---:|---:|---:|']
    for i in rows:
        for engine, e in i['engines'].items():
            top = max(e['sequential_analyses_per_hour_by_threads'], key=int)
            low, high = e['throughput_range']
            lines.append(f"| {i['instance_type']} | {engine} | {e['throughput_analyses_per_hour']:.0f} ({low:.0f}–{high:.0f}) | {fmt(e['usd_per_1000_analyses'], 3)} | "
                         f"{e['sequential_analyses_per_hour_by_threads'][top]:.0f} ({top} threads) |")
    for i in rows:
        if 'standard_over_cool' not in i:
            continue
        ratio = i['standard_over_cool']
        lines += ['', f"## COOL against the standard wheel on {i['instance_type']} (ratio = standard time / COOL time)", '',
                  '| kernel | '+' | '.join(f'{size}, {t} thr' for size in ratio['kernels'] for t in ratio['kernels'][size])+' |',
                  '|---|'+'---:|'*sum(len(ratio['kernels'][s]) for s in ratio['kernels'])]
        names = list(ratio['kernels']['native'][next(iter(ratio['kernels']['native']))])
        for name in names:
            cells = [fmt(ratio['kernels'][size][t].get(name)) for size in ratio['kernels'] for t in ratio['kernels'][size]]
            lines.append(f'| `{name}` | '+' | '.join(cells)+' |')
        lines += ['', '| recording (`measure_direct`) | '+' | '.join(f'{t} threads' for t in ratio['pipeline'])+' |',
                  '|---|'+'---:|'*len(ratio['pipeline'])]
        for name in ratio['pipeline'][next(iter(ratio['pipeline']))]:
            lines.append(f'| {name} | '+' | '.join(fmt(ratio['pipeline'][t][name], 3) for t in ratio['pipeline'])+' |')
        cool_range, stock_range = i['engines']['cool']['throughput_range'], i['engines']['stock']['throughput_range']
        overlap = cool_range[0] <= stock_range[1] and stock_range[0] <= cool_range[1]
        lines.append(f"\nWhole-instance throughput, COOL over standard (medians): {ratio['throughput']:.3f}; the per-round ranges "
                     f"{'overlap, so the difference is not resolved' if overlap else 'do not overlap'}.")
    lines += ['', 'On a 4-vCPU instance the slowest of the four processes sets the wall time, so a round is sensitive to any other '
              'activity on the machine. Read the throughput together with its range and with the one-thread `measure_direct` times, '
              'which measure the same work one process at a time.']
    lines += ['', '## Thread scaling of single kernels (time at 1 thread / time at n threads, native frames)', '']
    for i in rows:
        for engine, e in i['engines'].items():
            native = e['kernels_s']['native']
            counts = sorted(native, key=int)
            lines += [f"**{i['instance_type']}, {engine}**", '', '| kernel | '+' | '.join(f'{t}' for t in counts)+' |', '|---|'+'---:|'*len(counts)]
            for name in KEY_KERNELS:
                if name in native['1']:
                    lines.append(f'| `{name}` | '+' | '.join(fmt(native['1'][name]/native[t][name]) for t in counts)+' |')
            lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    instances = [instance_summary(f) for f in sorted(args.folder.iterdir()) if f.is_dir() and (f/'runs').exists()]
    summary = {'kind': 'CoolScalingSummary', 'schema_version': 1,
               'label': 'Post-hoc measurement after the V6 freeze; changes no recorded result',
               'method': ('Each instance ran three rounds per engine in alternating order on identical inputs (input.zip '
                          'SHA-256 in each state.json). Kernels: median of seven timed batches; 1080p and 2160p frames are '
                          'tiled from the 960 x 640 panel frames. Prices: AWS Price List API, us-east-1 on-demand Linux, '
                          'effective 2026-10-01; COOL fee from its Marketplace listing.'),
               'instances': instances}
    (args.folder/'summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    text = report(summary)
    (args.folder/'REPORT.md').write_text(text+'\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
