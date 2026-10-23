"""Summarise the six runs of ``phase4_aws_cool_kernels.py`` into summary.json and REPORT.md.

Post-hoc measurement after the V6 freeze. For every kernel and thread count it takes the
median over the three rounds of each engine's per-round median, and reports the ratio
standard/COOL (above 1 means COOL is faster). For the pipeline it does the same with the
per-recording wall time of ``measure_direct`` and reports the profiled time shares.

    python scripts/phase4_aws_cool_kernels_summary.py artifacts/phase4-aws/cool-kernels-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    runs_dir = args.folder/'runs'
    runs = {e: [json.loads((runs_dir/f'result-{e}-{r}.json').read_text(encoding='utf-8')) for r in (1, 2, 3)] for e in ('cool', 'stock')}
    env = {e: {k: rs[0]['environment'][k] for k in ('opencv_version', 'numpy_version', 'python', 'machine', 'cpu', 'build_information_sha256')}
           | {'build_lines': [l for l in rs[0]['environment']['build_lines'] if any(k in l for k in ('Baseline', 'Dispatched', 'HAL', 'Parallel'))],
              'engine_check': [r['engine_check'] for r in rs]} for e, rs in runs.items()}
    kernels = {}
    for threads in ('1', '4'):
        kernels[threads] = {}
        for name in runs['cool'][0]['kernels'][threads]:
            cool = statistics.median(r['kernels'][threads][name]['median_s'] for r in runs['cool'])
            stock = statistics.median(r['kernels'][threads][name]['median_s'] for r in runs['stock'])
            kernels[threads][name] = {'cool_ms': cool*1e3, 'standard_ms': stock*1e3, 'standard_over_cool': stock/cool}
    pipeline = []
    for i, item in enumerate(runs['cool'][0]['pipeline']):
        cool = statistics.median(r['pipeline'][i]['wall_median_s'] for r in runs['cool'])
        stock = statistics.median(r['pipeline'][i]['wall_median_s'] for r in runs['stock'])
        pipeline.append({'recording': item['name'], 'cool_s': cool, 'standard_s': stock, 'standard_over_cool': stock/cool,
                         'share_cool': runs['cool'][0]['pipeline'][i]['share'], 'share_standard': runs['stock'][0]['pipeline'][i]['share'],
                         'top_opencv_cool': runs['cool'][0]['pipeline'][i]['top_opencv_tottime_s'][:5],
                         'results_identical_within_engine': {e: len({r['pipeline'][i]['measurement_sha256'] for r in rs}) == 1 for e, rs in runs.items()}})
    summary = {'kind': 'CoolKernelBenchmarkSummary', 'schema_version': 1,
               'label': 'post-hoc measurement after the V6 freeze; same c8g.xlarge instance, three alternating rounds per engine',
               'instance': 'c8g.xlarge (Graviton4), image ami-033e481a24f94c8cb', 'environments': env, 'kernels': kernels, 'pipeline': pipeline,
               'runs_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(runs_dir.glob('result-*.json'))}}
    (args.folder/'summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    lines = ['# COOL against the standard wheel on one Graviton4 instance', '',
             f"Post-hoc measurement after the V6 freeze. One c8g.xlarge from `ami-033e481a24f94c8cb`; COOL: OpenCV {env['cool']['opencv_version']}, "
             f"NumPy {env['cool']['numpy_version']}; standard: `opencv-python` {env['stock']['opencv_version']}, NumPy {env['stock']['numpy_version']}; "
             f"Python {env['cool']['python']} for both. Three rounds per engine in alternating order; each value is the median over rounds of the "
             'median of seven timed batches. Ratio = standard / COOL (above 1: COOL faster).', '',
             '| kernel | COOL ms (4 threads) | standard ms (4 threads) | ratio, 4 threads | ratio, 1 thread |', '|---|---:|---:|---:|---:|']
    for name, row in kernels['4'].items():
        lines.append(f"| `{name}` | {row['cool_ms']:.3f} | {row['standard_ms']:.3f} | {row['standard_over_cool']:.2f} | {kernels['1'][name]['standard_over_cool']:.2f} |")
    lines += ['', '| recording (`measure_direct`, in process, 4 threads) | COOL s | standard s | ratio | OpenCV share (COOL) |', '|---|---:|---:|---:|---:|']
    for row in pipeline:
        lines.append(f"| {row['recording']} | {row['cool_s']:.3f} | {row['standard_s']:.3f} | {row['standard_over_cool']:.3f} | {row['share_cool']['opencv']:.0%} |")
    lines += ['', 'Build lines: COOL ' + '; '.join(env['cool']['build_lines']) + '. Standard ' + '; '.join(env['stock']['build_lines']) + '.', '']
    (args.folder/'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
