"""Predeclared engine equivalence, separate from exact reuse identity."""
from __future__ import annotations

import math
from kiroclops.visual.schema import digest

POLICY = {
    'id': 'engine-equivalence-v1',
    'numeric_absolute_tolerance': 0.00005,
    'numeric_relative_tolerance': 0.000001,
    'maximum_recorded_differences': 30,
    'exact_fields': 'All categorical, validity, object, event and frame identities',
    'provenance_fields_excluded': ['opencv_version', 'observation_sha256', 'event_sha256'],
    'note': 'This is a cross-engine numerical check, never an exact-cache equivalence rule. Movement acceptance thresholds are unchanged.'}


def compare(left, right):
    differences = []
    def visit(a, b, path):
        if len(differences) >= POLICY['maximum_recorded_differences']:
            return
        if isinstance(a, bool) or isinstance(b, bool):
            same = type(a) is type(b) and a == b
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            same = math.isclose(a, b, abs_tol=POLICY['numeric_absolute_tolerance'], rel_tol=POLICY['numeric_relative_tolerance'])
        elif isinstance(a, dict) and isinstance(b, dict):
            keys = (set(a) | set(b)) - set(POLICY['provenance_fields_excluded'])
            for key in sorted(keys):
                if key not in a or key not in b:
                    differences.append({'path': path + '/' + key, 'reason': 'missing_field'})
                else:
                    visit(a[key], b[key], path + '/' + key)
            return
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                differences.append({'path': path, 'reason': 'different_length'})
            for i, (x, y) in enumerate(zip(a, b)):
                visit(x, y, path + '/' + str(i))
            return
        else:
            same = type(a) is type(b) and a == b
        if not same:
            differences.append({'path': path, 'left': a, 'right': b})
    visit(left, right, '')
    return {'policy': POLICY, 'policy_sha256': digest(POLICY),
            'exact': left == right, 'equivalent': not differences, 'differences': differences}
