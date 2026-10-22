"""Describe the frozen COOL equivalence failures without changing acceptance."""
import argparse
from collections import Counter
import math
from pathlib import Path

from kiroclops.cloud.equivalence import POLICY
from kiroclops.visual.schema import read_json, write_json


def differences(left, right, path=''):
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted((set(left) | set(right)) - set(POLICY['provenance_fields_excluded'])):
            if key not in left or key not in right:
                yield path+'/'+key, left.get(key), right.get(key)
            else:
                yield from differences(left[key], right[key], path+'/'+key)
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            yield path+'/length', len(left), len(right)
        for index, (a, b) in enumerate(zip(left, right)):
            yield from differences(a, b, path+'/'+str(index))
    else:
        numeric = (type(left) in (int, float) and type(right) in (int, float))
        same = (math.isclose(left, right,
                            abs_tol=POLICY['numeric_absolute_tolerance'],
                            rel_tol=POLICY['numeric_relative_tolerance'])
                if numeric else type(left) is type(right) and left == right)
        if not same:
            yield path, left, right


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--probe', type=Path, required=True)
    args = parser.parse_args()
    results = args.probe/'download/cool-validation'
    run = read_json(results/'result.json')
    rows = []
    for item in run['rows']:
        old = read_json(args.corpus/item['id']/'historical.json')
        new = read_json(results/item['id']/'measurement.json')
        fields = {key: old[key] for key in ('observations', 'events') if key in old}
        changes = list(differences(fields, {key: new[key] for key in fields}))
        coordinates = [p for p, _, _ in changes if p.startswith('/observations/objects/')
                       and '/frames/' in p and p.rsplit('/', 1)[-1] in ('x', 'y')]
        validity = [p for p, _, _ in changes if p.startswith('/observations/')
                    and p.rsplit('/', 1)[-1] in ('valid', 'reason')]
        row = {'id': item['id'], 'domain': item['domain'],
               'registered_equivalence_pass': item['valid'],
               'total_different_fields': len(changes),
               'differing_field_counts': dict(Counter(p.rsplit('/', 1)[-1] for p, _, _ in changes)),
               'coordinate_difference_count': len(coordinates),
               'validity_difference_count': len(validity),
               'examples': [{'path': p, 'left': a, 'right': b} for p, a, b in changes[:30]]}
        rows.append(row)
    write_json(args.probe/'diagnostics.json', {
        'kind': 'CoolEquivalenceDiagnostics', 'schema_version': 1,
        'status': run['status'], 'acceptance_policy_unchanged': True,
        'not_an_independent_visual_correctness_review': True,
        'causality_limit': 'OpenCV base, native optimization and NumPy all differ; this run cannot isolate their effects.',
        'rows': rows})
    print({row['id']: row['differing_field_counts'] for row in rows})


if __name__ == '__main__':
    main()
