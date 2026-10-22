"""Compare unsigned raw-pixel proposals with claims, without granting review.

This triage report directs actual image inspection. It is deliberately not an
IndependentScopeReview and cannot satisfy any acceptance gate.
"""
import argparse
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import digest, read_json, write_json


def differences(wanted, actual, tolerance, prefix=''):
    if isinstance(wanted, dict):
        if not isinstance(actual, dict) or set(wanted) != set(actual):
            return [{'field': prefix, 'reason': 'keys_differ'}]
        return [d for k, v in wanted.items() for d in differences(
            v, actual[k], .02 if k == 'intersection_over_union' else
            tolerance if k == 'lag_interval_s' else 2., k)]
    if isinstance(wanted, list):
        if not isinstance(actual, list) or len(wanted) != len(actual):
            return [{'field': prefix, 'reason': 'length_differs'}]
        return [d for i, (a, b) in enumerate(zip(wanted, actual))
                for d in differences(a, b, tolerance, f'{prefix}[{i}]')]
    good = (actual == wanted if isinstance(wanted, (bool, str)) else
            isinstance(actual, (float, int)) and abs(actual - wanted) <= tolerance)
    return [] if good else [{'field': prefix, 'claim': wanted,
                             'raw_proposal': actual, 'tolerance': tolerance}]


def run(review, drafts, output):
    review, drafts, output = map(Path, (review, drafts, output))
    if output.exists():
        raise ValueError('Preserve prior diagnostic; choose a new output')
    keys = ('distance_px', 'endpoint_distances_px', 'visually_attached',
            'visually_touching', 'anchor_distance_px', 'edge_distance_px',
            'overlap_area_px2', 'intersection_over_union', 'visually_overlapping',
            'dx_px', 'dy_px', 'lag_interval_s', 'order')
    rows, totals = [], Counter()
    for record in read_json(review / 'inventory.json')['records']:
        for scope in ('events', 'relationships'):
            request = read_json(review / 'scopes' / record['id'] / scope / 'request.json')
            draft = read_json(drafts / record['id'] / (scope + '.json'))
            assert draft['request_sha256'] == digest(request)
            marks = {v['id']: v for v in draft['checks']}
            for task in request['tasks']:
                mark = marks[task['id']]
                assert mark['frame_sha256s'] == [v['sha256'] for v in task['frames']]
                value = task['measurement']
                expected = (value['time_interval_s'] if scope == 'events' else
                            {k: value[k] for k in keys if k in value})
                delta = differences(expected, mark['independent_observation'], request['event_tolerance_s'])
                totals[scope] += 1
                if delta:
                    totals[scope + '_disagreements'] += 1
                    rows.append({'record': record['id'], 'scope': scope, 'id': task['id'],
                                 'type': mark['type'], 'object_ids': mark['object_ids'],
                                 'frame_indices': mark['frame_indices'], 'differences': delta})
    result = {'kind': 'UnsignedScopeDisagreementDiagnostic', 'status': 'not_reviewed',
              'acceptance': 'pending', 'counts': dict(totals), 'disagreements': rows}
    write_json(output, result)
    print(dict(totals), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--review', type=Path, required=True)
    p.add_argument('--drafts', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    run(a.review, a.drafts, a.output)
