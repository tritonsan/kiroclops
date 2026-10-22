"""Prepare a calibration-only game pixel aid before private pool capture."""
from pathlib import Path
import argparse
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine.suite_evidence import _source_expectation
from kiroclops.local_engine.suite_generic_raw_review import review_records
from kiroclops.visual.schema import VisualError, digest, read_json


def prepare(previous, output):
    previous = Path(previous)
    cal = read_json(previous / 'calibration.json')
    binding = read_json(previous / 'start-binding.json')
    if cal['stage'] not in ('jump', 'ground') or cal.get('model_started') is not False:
        raise VisualError('invalid_suite', 'Only pre-model game calibration may be reviewed here.')
    expected = {}
    for role, key in (('original', 'original'), ('before', 'parent')):
        source = Path(binding[key])
        hashes = source_hashes(source)
        if digest(hashes) != binding[key + '_source_sha256']:
            raise VisualError('source_changed', 'The game calibration source changed.')
        expected[role] = _source_expectation(source, hashes, 'game')
    rows = []
    for item in cal['records']:
        role = item['source_role']
        row = {'id': role + '-' + item['case_id'], 'case_id': item['case_id'],
            'domain': 'game', 'role': role, 'reference': role == 'reference', 'record': item['record']}
        if role != 'reference':
            row.update(expected_source=expected[role], source_project=binding['original' if role == 'original' else 'parent'])
        rows.append(row)
    value = review_records(rows, output)
    return {'numeric_status': value['numeric_status'], 'sheet_count': len(value['sheets']),
        'failures': value['failures'], 'output': str(output), 'model_started': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.previous, args.output))
