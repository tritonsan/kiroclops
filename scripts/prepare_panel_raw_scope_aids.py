"""Prepare unsigned raw-image review aids for supplemental panel recordings.

The adapter supplies frozen identities and review scope to previously audited
pixel-only helpers. It never signs a review or substitutes tracked coordinates.
"""
from pathlib import Path
import argparse
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'artifacts/phase3a/tools'))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest


def load_helper(name):
    path = ROOT / 'artifacts/phase3a/tools' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(review, output):
    review, output = Path(review).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Fresh output required; preserve unsigned aids too')
    inventory = read_json(review / 'inventory.json')
    output.mkdir(parents=True)
    bridge = output / 'scope-adapter'
    write_json(bridge / 'panel/positions/request.json', read_json(review / 'positions/request.json'))
    records = []
    for entry in inventory['records']:
        if file_sha256(entry['record_path']) != entry['record_sha256']:
            raise ValueError('Supplemental recording changed')
        record = read_json(entry['record_path'])
        name = entry['id']
        folder = bridge / 'inputs' / name
        write_json(folder / 'analysis.json', {'observations': record['observations']})
        write_json(folder / 'selection.json', record['selection'])
        records.append({'id': name, 'domain': 'panel', 'role': entry['role'],
                        'package': entry['package'],
                        'package_sha256': record['observations']['package_sha256'],
                        'observations_sha256': digest(record['observations']),
                        'analysis_path': str(folder / 'analysis.json'),
                        'selection_sha256': digest(record['selection'])})
        for scope in ('events', 'relationships'):
            request = read_json(review / 'scopes' / name / scope / 'request.json')
            write_json(bridge / 'panel/records' / name / scope / 'request.json', request)
    write_json(bridge / 'preparation.json', {'records': records,
               'original_inventory_sha256': file_sha256(review / 'inventory.json'),
               'status': 'adapter_only_not_reviewed'})
    raw = load_helper('review_panel_raw09').run(bridge, output / 'raw')
    scopes = load_helper('prepare_panel_scope_raw10_segments').run(bridge, output / 'raw', output / 'drafts')
    result = {'status': 'unsigned_not_reviewed', 'raw': raw,
              'unresolved': sum(r['unresolved'] for r in scopes), 'records': scopes,
              'original_inventory_sha256': file_sha256(review / 'inventory.json')}
    write_json(output / 'preparation.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--review', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = run(a.review, a.output)
    print({'status': result['status'], 'unresolved': result['unresolved']}, flush=True)
