"""POST-HOC: repeat a finished AWS-vs-local comparison without the V6 OpenCV runtime record.

Phase 4 AWS, after the V6 freeze, written after the COOL results were seen. The
cross-engine policy ``engine-equivalence-v1`` (``kiroclops.cloud.equivalence``) dates
from Phase 3B and treats only ``opencv_version``, ``observation_sha256`` and
``event_sha256`` as provenance. V6 (WP5.2) added ``observations.opencv_runtime``
(version, module list, wheel flags), which describes the engine and therefore differs
between engines. The original ``result.json`` is never changed. For every row this
script recomputes the local measurement, reproduces the original comparison, compares
again without ``observations.opencv_runtime`` (and, as a separate level, also without
the report-only ``camera[*].phase_shifts_px`` diagnostic), and reports the largest
numeric difference and the worker's own wall time from the delivery receipt.

    python scripts/phase4_aws_runtime_excluded_check.py --kind v6 --result-dir artifacts/phase4-aws/v6-cool-equivalence-02
    python scripts/phase4_aws_runtime_excluded_check.py --kind probe --result-dir artifacts/phase4-aws/v6-cool-probe-02
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.cloud.equivalence import POLICY, compare  # noqa: E402
from kiroclops.cloud.execution import measure_direct  # noqa: E402
from kiroclops.cloud.identity import engine_identity  # noqa: E402
from kiroclops.visual.schema import read_json, write_json  # noqa: E402
from scripts.phase4_aws_v6_equivalence import recordings  # noqa: E402

CORPUS = ROOT/'artifacts/phase3b/calibration-corpus-02'
PROVENANCE = {'opencv_version', 'observation_sha256', 'event_sha256'}


def without(value, *, phase_shifts=False):
    value = deepcopy(value)
    value['observations'].pop('opencv_runtime', None)
    if phase_shifts:
        for row in value['observations'].get('camera', []):
            row.pop('phase_shifts_px', None)
    return value


def numeric_spread(left, right):
    """Largest absolute difference over numeric leaves present on both sides, outside provenance fields."""
    stats = {'compared': 0, 'identical': 0, 'max_abs': 0.0, 'max_path': None}

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in a.keys() & b.keys():
                if key not in PROVENANCE:
                    walk(a[key], b[key], path+'/'+str(key))
        elif isinstance(a, list) and isinstance(b, list):
            for index, (x, y) in enumerate(zip(a, b)):
                walk(x, y, path+'/'+str(index))
        elif (isinstance(a, (int, float)) and isinstance(b, (int, float))
              and not isinstance(a, bool) and not isinstance(b, bool)):
            stats['compared'] += 1
            if a == b:
                stats['identical'] += 1
            elif math.isfinite(a) and math.isfinite(b) and abs(a-b) > stats['max_abs']:
                stats['max_abs'], stats['max_path'] = abs(a-b), path
    walk(left, right, '')
    return stats


def cases(kind, result_dir, rows):
    if kind == 'v6':
        known = {item['id']: item for item in recordings()}
        for row in rows:
            item = known[row['id']]
            job = read_json(result_dir/'jobs'/(row['id'].replace('/', '__')+'.json'))
            yield row, item['package'], item['selection'], item['contract'], job
    else:
        for row in rows:
            folder = CORPUS/row['id']
            yield (row, folder/'package', read_json(folder/'selection.json'), read_json(folder/'contract.json'),
                   read_json(result_dir/(row['id']+'-job.json')))


def worker_seconds(result_dir, job_id):
    delivery = result_dir/'client'/'jobs'/job_id/'delivery-v2.json'
    return read_json(delivery)['receipt']['timings']['worker_wall_seconds'] if delivery.exists() else None


def run(kind, result_dir):
    result_dir = Path(result_dir)
    original = read_json(result_dir/'result.json')
    engine_identity(4)
    rows = []
    for row, package, selection, contract, job in cases(kind, result_dir, original['rows']):
        if job['status'] != 'succeeded':
            rows.append({'id': row['id'], 'status': job['status']})
            continue
        local = measure_direct(package, selection, contract)
        remote = {k: job['result'][k] for k in ('observations', 'events')}
        full = compare(local, remote)
        level_1 = compare(without(local), without(remote))
        level_2 = compare(without(local, phase_shifts=True), without(remote, phase_shifts=True))
        rows.append({'id': row['id'], 'status': job['status'],
                     'original_reproduced': (full['equivalent'], len(full['differences'])) ==
                                            (row['equivalence']['equivalent'], len(row['equivalence']['differences'])),
                     'full_policy': {'equivalent': full['equivalent'], 'differences': len(full['differences']),
                                     'paths_outside_runtime': [d['path'] for d in full['differences']
                                                               if not d['path'].startswith('/observations/opencv_runtime')]},
                     'runtime_excluded': {'equivalent': level_1['equivalent'], 'exact': level_1['exact'],
                                          'differences': level_1['differences']},
                     'runtime_and_phase_shifts_excluded': {'equivalent': level_2['equivalent'], 'exact': level_2['exact'],
                                                           'differences': len(level_2['differences'])},
                     'numeric_spread_runtime_excluded': numeric_spread(without(local), without(remote)),
                     'remote_opencv_version': remote['observations'].get('opencv_version'),
                     'local_opencv_version': local['observations'].get('opencv_version'),
                     'worker_wall_seconds': worker_seconds(result_dir, row['job_id'])})
        print(row['id'], 'runtime-excluded equivalent:', level_1['equivalent'], flush=True)
    measured = [r for r in rows if 'runtime_excluded' in r]
    summary = {'kind': 'PostHocRuntimeExcludedEquivalence',
               'label': 'POST-HOC: written after the results were seen; the original result.json is unchanged',
               'checked_at': datetime.now(timezone.utc).isoformat(), 'source': str(result_dir), 'policy': POLICY,
               'original': {'status': original['status'], 'equivalent': sum(1 for r in original['rows'] if r.get('equivalence', {}).get('equivalent'))},
               'rows_measured': len(measured), 'original_reproduced': sum(r['original_reproduced'] for r in measured),
               'runtime_excluded_equivalent': sum(r['runtime_excluded']['equivalent'] for r in measured),
               'runtime_and_phase_shifts_excluded_equivalent': sum(r['runtime_and_phase_shifts_excluded']['equivalent'] for r in measured),
               'largest_numeric_difference': max((r['numeric_spread_runtime_excluded']['max_abs'] for r in measured), default=None),
               'rows': rows}
    write_json(result_dir/'posthoc-runtime-excluded.json', summary)
    print({k: summary[k] for k in ('rows_measured', 'original_reproduced', 'runtime_excluded_equivalent',
                                   'runtime_and_phase_shifts_excluded_equivalent', 'largest_numeric_difference')}, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--kind', choices=['v6', 'probe'], required=True)
    parser.add_argument('--result-dir', required=True)
    args = parser.parse_args()
    run(args.kind, args.result_dir)
