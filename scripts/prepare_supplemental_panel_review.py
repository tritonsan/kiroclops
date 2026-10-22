"""Freeze review tasks for all fresh supplemental pixels; never signs a review."""
from pathlib import Path
import argparse
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.review import prepare_review_group, prepare_scope_review
from scripts.propose_review_pixels import propose


def run(validation, output):
    validation, output = Path(validation).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Fresh review output required')
    result = read_json(validation / 'validation.json')
    if result['status'] != 'pass' or result['passed'] != 12:
        raise ValueError('Expected completed 12-case supplemental validation')
    records = []
    for row in result['results']:
        for role in ('reference', 'before', 'candidate'):
            path = validation / 'records' / row['scenario_id'] / (role + '.json')
            record = read_json(path)
            records.append({**record, 'role': role, 'id': row['scenario_id'] + '-' + role,
                            'record_path': str(path), 'record_sha256': file_sha256(path)})
    output.mkdir(parents=True)
    write_json(output / 'inventory.json', {'status': 'pending', 'candidate_sha256': result['candidate_source_sha256'],
        'validation_sha256': file_sha256(validation / 'validation.json'),
        'records': [{k:r[k] for k in ('id','role','record_path','record_sha256','package')} for r in records],
        'required_observations': [[r['observations']['package_sha256'], digest(r['observations'])] for r in records]})
    # Positions sample each role/object across the full suite, including every
    # input boundary. Events are reviewed separately in their full sequences.
    print('prepare positions', flush=True)
    position = prepare_review_group([{**r, 'events': {}} for r in records], output / 'positions',
                                   tolerance_px=1.5, candidate_sha256=result['candidate_source_sha256'])
    print(position, flush=True)
    print('propose raw positions (unsigned)', flush=True)
    print(propose(output / 'positions', output / 'position-proposals'), flush=True)
    for index, record in enumerate(records):
        print(index+1, len(records), record['id'], flush=True)
        for scope in ('events', 'relationships'):
            prepare_scope_review(record['package'], record['observations'], record['events'],
                output / 'scopes' / record['id'] / scope, scope=scope,
                candidate_sha256=result['candidate_source_sha256'])
    write_json(output / 'preparation.json', {'status':'prepared_not_reviewed','records':len(records),
        'position_points':position['control_points'],'candidate_sha256':result['candidate_source_sha256']})


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--validation',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.validation,a.output)
