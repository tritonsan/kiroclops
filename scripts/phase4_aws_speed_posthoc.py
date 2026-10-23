"""Post-hoc: bootstrap confidence intervals for the COOL against standard-wheel timings of speed-01.

Reads the frozen ``artifacts/phase4-aws/speed-01/report.json`` (17 recordings, three fresh
jobs per engine, the receipt's ``worker_wall_seconds``) and reports, with 95% percentile
bootstrap intervals (10,000 resamples, fixed seed):

* the paired per-recording ratio COOL/standard (median over repeats per recording), resampling
  recordings: its median and geometric mean;
* the ratio of the job-level medians, resampling jobs within each engine;
* the same paired ratio for the local x86 engine against the Graviton4 standard wheel.

    python scripts/phase4_aws_speed_posthoc.py --output artifacts/phase4-aws/speed-01-posthoc-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'artifacts'/'phase4-aws'/'speed-01'/'report.json'
RESAMPLES = 10_000
SEED = 20261023


def interval(values):
    ordered = sorted(values)
    return [ordered[int(.025*len(ordered))], ordered[int(.975*len(ordered))-1]]


def geomean(values):
    return math.exp(statistics.mean(math.log(v) for v in values))


def paired(per, a, b, rng):
    ids = sorted(i for i in per if a in per[i] and b in per[i])
    ratios = [per[i][a]/per[i][b] for i in ids]
    medians, geos = [], []
    for _ in range(RESAMPLES):
        sample = [ratios[rng.randrange(len(ratios))] for _ in ratios]
        medians.append(statistics.median(sample))
        geos.append(geomean(sample))
    return {'recordings': len(ids), 'median_ratio': statistics.median(ratios), 'median_ratio_ci95': interval(medians),
            'geometric_mean_ratio': geomean(ratios), 'geometric_mean_ratio_ci95': interval(geos),
            'min_ratio': min(ratios), 'max_ratio': max(ratios)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f'Keep prior evidence; choose a new folder: {args.output}')
    report = json.loads(SOURCE.read_text(encoding='utf-8'))
    rng = random.Random(SEED)
    jobs = {}
    per = {}
    for row in report['rows']:
        seconds = row.get('worker_wall_seconds') if row['engine'] != 'local' else (row.get('worker_wall_seconds') or row.get('seconds'))
        if row.get('status', 'succeeded') != 'succeeded' or not isinstance(seconds, (int, float)):
            continue
        jobs.setdefault(row['engine'], []).append(seconds)
        per.setdefault(row['id'], {}).setdefault(row['engine'], []).append(seconds)
    per = {i: {e: statistics.median(v) for e, v in engines.items()} for i, engines in per.items()}
    medians = []
    for _ in range(RESAMPLES):
        cool = [jobs['cool'][rng.randrange(len(jobs['cool']))] for _ in jobs['cool']]
        standard = [jobs['standard'][rng.randrange(len(jobs['standard']))] for _ in jobs['standard']]
        medians.append(statistics.median(cool)/statistics.median(standard))
    result = {'kind': 'CoolSpeedPosthoc', 'schema_version': 1, 'source': str(SOURCE.relative_to(ROOT)).replace('\\', '/'),
              'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(), 'resamples': RESAMPLES, 'seed': SEED,
              'label': 'post-hoc analysis of recorded timings; no job was repeated',
              'jobs': {e: {'count': len(v), 'median_s': statistics.median(v)} for e, v in jobs.items()},
              'job_median_ratio_cool_over_standard': statistics.median(jobs['cool'])/statistics.median(jobs['standard']),
              'job_median_ratio_ci95': interval(medians),
              'paired_cool_over_standard': paired(per, 'cool', 'standard', rng),
              'paired_local_x86_over_graviton_standard': paired(per, 'local', 'standard', rng) if 'local' in jobs else None}
    args.output.mkdir(parents=True)
    (args.output/'result.json').write_text(json.dumps(result, indent=1), encoding='utf-8')
    p = result['paired_cool_over_standard']
    print(json.dumps({'jobs': result['jobs'], 'job_median_ratio': round(result['job_median_ratio_cool_over_standard'], 4),
                      'job_median_ratio_ci95': [round(v, 4) for v in result['job_median_ratio_ci95']],
                      'paired_median': round(p['median_ratio'], 4), 'paired_median_ci95': [round(v, 4) for v in p['median_ratio_ci95']],
                      'paired_geomean': round(p['geometric_mean_ratio'], 4), 'paired_geomean_ci95': [round(v, 4) for v in p['geometric_mean_ratio_ci95']]}, indent=1))


if __name__ == '__main__':
    main()
