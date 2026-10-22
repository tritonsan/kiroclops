"""Publish an already checked visual receipt without rewriting trial outcomes."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes


def run(review, receipt):
    review, receipt = Path(review).resolve(), Path(receipt).resolve()
    assert not receipt.exists()
    p = read_json(review/'preparation.json')
    assert digest(p) == read_json(review/'preparation-binding.json')['sha256']
    checked = read_json(review/'check-result.json')
    assert checked['visual_status'] == 'pass'
    aggregate = review/'independent_review.json'
    assert checked['visual_receipt_sha256'] == file_sha256(aggregate)
    value = read_json(aggregate)
    assert value['preparation_sha256'] == file_sha256(review/'preparation.json')
    assert value['candidate_sha256'] == checked['candidate_sha256'] == p['candidate_sha256']
    assert value['required_observations'] == p['required_observations']
    bound = {}
    for scope in ('position', 'events', 'relationships'):
        for item in value[scope+'_reviews']:
            folder = Path(item['directory'])
            if not folder.is_absolute(): folder = review/folder
            folder.resolve().relative_to(review)
            result = read_json(folder/'review-result.json')
            assert file_sha256(folder/'review-result.json') == item['result_sha256']
            assert digest(read_json(folder/'request.json')) == item['request_sha256'] == result['request_sha256']
            assert digest(read_json(folder/'annotation.json')) == result['annotation_sha256']
            assert result['status'] == 'pass'
            relative = folder.relative_to(review).as_posix()
            assert relative not in bound, 'Duplicate scope receipt'
            bound[relative] = item['result_sha256']
    expected = {Path(e['directory']).as_posix() for e in p['requests']}
    assert expected and len(expected) == len(p['requests']), 'Empty or duplicate preparation scopes'
    assert set(bound) == expected
    session = Path(p['session'])
    state = read_json(session/'session.json')
    assert not state.get('active_job_id') and state['final']['status'] == 'succeeded'
    candidate = next(c for c in state['candidates'] if c['candidate_id'] == state['selected_candidate_id'])
    assert candidate['source_sha256'] == p['candidate_sha256']
    assert digest(source_hashes(session/'candidates'/candidate['candidate_id']/'source')) == p['candidate_sha256']
    canonical = session/'independent_review.json'
    assert not canonical.exists(), 'Never overwrite earlier review evidence'
    canonical.write_bytes(aggregate.read_bytes())
    assert file_sha256(canonical) == file_sha256(aggregate)
    result = {'kind': 'CheckedVisualReviewPublication', 'status': 'published',
        'review': str(review), 'canonical_path': str(canonical),
        'candidate_sha256': p['candidate_sha256'], 'visual_status': 'pass',
        'source_native_status': checked['source_native_status'],
        'visual_receipt_sha256': file_sha256(canonical),
        'check_result_sha256': file_sha256(review/'check-result.json'),
        'scopes': len(bound), 'external_human_review': False,
        'primary_outcomes_modified': False, 'motion_acceptance_granted': False}
    write_json(receipt, result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path)
    args = parser.parse_args()
    print(run(args.review, args.receipt), flush=True)
