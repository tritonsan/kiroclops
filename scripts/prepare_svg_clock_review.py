"""Freeze independent review tasks for every pair in a completed SVG qualification."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.local_engine.review import prepare_review_group, prepare_scope_review


def run(qualification, output):
    qualification, output = Path(qualification).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Preserve earlier evidence; use a new directory')
    result = read_json(qualification / 'result.json')
    if result['status'] != 'pass' or result['passed'] != result['required']:
        raise ValueError('Recorder repeatability must pass before visual qualification')
    items, inventory = [], []
    for pair in result['rows']:
        for repeat in (1, 2):
            path = qualification / 'records' / pair['scenario_id'] / f'{repeat}.json'
            record = read_json(path)
            name = f"{pair['scenario_id']}-repeat{repeat}"
            items.append({**record, 'role': f'reference-repeat{repeat}'})
            inventory.append({'id': name, 'record_id': f'record-{len(items)-1:03}',
                'record_path': str(path), 'record_sha256': file_sha256(path),
                'package': record['package'], 'package_sha256': record['observations']['package_sha256']})
    summary = prepare_review_group(items, output/'positions', tolerance_px=1.5)
    for record, row in zip(items, inventory):
        for scope in ('events', 'relationships'):
            receipt = prepare_scope_review(record['package'], record['observations'], record['events'],
                output/'records'/row['id']/scope, scope=scope)
            row[scope+'_tasks'] = receipt['tasks']
        print(row['id'], row['events_tasks'], row['relationships_tasks'], flush=True)
    write_json(output/'inventory.json', {'kind':'SVGClockIndependentReviewInventory',
        'qualification': str(qualification), 'qualification_result_sha256': file_sha256(qualification/'result.json'),
        'status':'pending', 'records':inventory, 'position_tasks':summary['control_points'],
        'reviewer':'not_assigned', 'model_started':False})
    print(summary, flush=True)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('qualification', type=Path); p.add_argument('--output', type=Path, required=True)
    a=p.parse_args(); run(a.qualification,a.output)
