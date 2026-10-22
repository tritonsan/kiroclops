"""Bind existing historical candidates; no capture, model or source write."""
from pathlib import Path
import argparse
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.session import Session, source_hashes, JOB_BINDING_FIELDS
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


def create(previous, output):
    previous, output = Path(previous), Path(output)
    if output.exists():
        raise VisualError('output_exists', 'Preserve every historical origin receipt.')
    binding = read_json(previous / 'start-binding.json')
    historical = Path(binding['original']).parent
    candidate = binding['historical_candidate']
    state = Session(historical)._load()  # Binding-only; never reconcile or submit.
    actual = next((c for c in state['candidates'] if c['candidate_id'] == candidate['candidate_id']), None)
    source = historical / 'candidates' / candidate['candidate_id'] / 'source'
    hashes = source_hashes(source)
    if (actual != candidate or actual['status'] != 'succeeded'
            or digest(hashes) != binding['parent_source_sha256']
            or source_hashes(binding['parent']) != hashes):
        raise VisualError('wrong_origin', 'Historical candidate or preparation parent changed.')
    job = historical / 'jobs' / candidate['job_id']
    job_state, result = read_json(job / 'job.json'), read_json(job / 'result.json')
    if (job_state['status'] != 'succeeded' or job_state['operation'] != 'evaluate'
            or job_state['source_hashes'] != hashes or job_state['result_sha256'] != digest(result)
            or read_json(job / 'binding.json')['sha256'] != digest({k: job_state[k] for k in JOB_BINDING_FIELDS})):
        raise VisualError('wrong_origin', 'Historical evaluation source/result binding changed.')
    files = {str(historical / name): file_sha256(historical / name) for name in ('session.json', 'binding.json')}
    files.update({str(job / name): file_sha256(job / name) for name in ('job.json', 'binding.json', 'result.json')})
    files.update({str(source / name): sha for name, sha in hashes.items()})
    value = {'kind': 'ExistingKiroCandidateOrigin', 'schema_version': 1,
        'historical_session': str(historical), 'historical_candidate_id': candidate['candidate_id'],
        'source_sha256': digest(hashes), 'source_hashes': hashes,
        'historical_binding_files': files, 'new_model_generation': False}
    write_json(output, value)
    return {'output': str(output), 'sha256': file_sha256(output), 'source_sha256': digest(hashes)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(create(args.previous, args.output))
