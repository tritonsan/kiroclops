"""Reuse actually reviewed scopes only when the complete evidence is identical.

This is an artifact-only aid. It does not invent marks, run a model, or certify
an experiment. Final kit checks still replay every copied annotation.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest, safe_path, VisualError
from kiroclops.local_engine.review import validate_independent_review, evaluate_scope_review


def semantic_request(request):
    """Only the target-code binding may differ; pixel and scope fields may not."""
    result = deepcopy(request)
    result.pop('candidate_sha256', None)
    return result


def may_reuse(old_request, new_request, old_record, new_record):
    fields = ('mechanic', 'role', 'package_sha256', 'observations_sha256',
              'events_sha256', 'selection_sha256')
    if any(not isinstance(r.get(k), str) or not r[k] for r in (old_record, new_record) for k in fields):
        return False
    if any(old_record[k] != new_record[k] for k in fields):
        return False
    if new_record.get('role') == 'candidate' and (
            old_request.get('candidate_sha256') != new_request.get('candidate_sha256')):
        return False
    return semantic_request(old_request) == semantic_request(new_request)


def load_kit(root):
    root = Path(root).resolve()
    prep = read_json(root / 'preparation.json')
    if digest(prep) != read_json(root / 'preparation-binding.json')['sha256']:
        raise VisualError('stale_review', 'Review preparation binding changed')
    records = {r['id']: r for r in prep['fingerprint']['records']}
    scopes = []
    for entry in prep['requests']:
        if entry['scope'] not in ('events', 'relationships'):
            continue
        directory = safe_path(root, entry['directory'], must_exist=False)
        request = read_json(directory / 'request.json')
        if digest(request) != entry['request_sha256']:
            raise VisualError('stale_review', 'Scope request changed')
        record = records[entry['record_id']]
        if any(request.get(k) != record.get(k) for k in (
                'package_sha256', 'observations_sha256', 'events_sha256')):
            raise VisualError('stale_review', 'Scope and originating recording differ')
        scopes.append((entry, directory, request, record))
    return prep, scopes


def run(prior, current, output):
    prior, current, output = map(lambda p: Path(p).resolve(), (prior, current, output))
    if output.exists() or any(output == root or output.is_relative_to(root)
                              for root in (prior, current)):
        raise VisualError('output_exists', 'Use fresh annotation output outside both frozen kits')
    old_prep, old = load_kit(prior)
    new_prep, new = load_kit(current)
    aggregate_path = prior / 'independent_review.json'
    verified = validate_independent_review(aggregate_path,
                                          candidate_sha256=old_prep['candidate_sha256'])
    if verified.get('preparation_sha256') != file_sha256(prior / 'preparation.json'):
        raise VisualError('stale_review', 'Prior aggregate does not bind these record roles and selections')
    original_bindings = {str(p): file_sha256(p) for p in (
        aggregate_path, prior / 'preparation.json', current / 'preparation.json')}
    certified_directories = {
        str((aggregate_path.parent / e['directory']).resolve()): e
        for scope in ('events', 'relationships') for e in verified[scope + '_reviews']}
    output.mkdir(parents=True)
    reused, pending = [], []
    for entry, directory, request, record in new:
        matches = [(e, d, q, r) for e, d, q, r in old
                   if str(d.resolve()) in certified_directories
                   and may_reuse(q, request, r, record)]
        if not matches:
            pending.append(entry['directory'])
            continue
        # Multiple occurrences are safe only when their actual independent
        # judgements are byte-semantically identical, not merely PASS labels.
        choices = []
        for old_entry, old_dir, old_request, _ in matches:
            annotation = read_json(old_dir / 'annotation.json')
            certificate = certified_directories[str(old_dir.resolve())]
            saved = read_json(old_dir / 'review-result.json')
            if (file_sha256(old_dir / 'review-result.json') != certificate['result_sha256']
                    or digest(old_request) != certificate['request_sha256']
                    or digest(annotation) != saved.get('annotation_sha256')):
                raise VisualError('stale_review', 'Prior annotation changed after independent validation')
            for name in ('request.json', 'annotation.json', 'review-result.json'):
                original_bindings[str(old_dir / name)] = file_sha256(old_dir / name)
            choices.append((old_dir, old_request, annotation))
        marks = [dict(a, request_sha256='') for _, _, a in choices]
        if any(v != marks[0] for v in marks[1:]):
            pending.append(entry['directory'])
            continue
        old_dir, old_request, annotation = choices[0]
        reused_annotation = deepcopy(annotation)
        reused_annotation['request_sha256'] = digest(request)
        reused_annotation['reuse_provenance'] = {
            'kind': 'ExactCompleteScopeReuse',
            'prior_review_sha256': file_sha256(aggregate_path),
            'prior_annotation_sha256': file_sha256(old_dir / 'annotation.json'),
            'prior_request_sha256': digest(old_request),
            'prior_preparation_sha256': file_sha256(prior / 'preparation.json'),
            'current_preparation_sha256': file_sha256(current / 'preparation.json'),
            'role': record['role'], 'selection_sha256': record['selection_sha256'],
            'new_manual_inspection_claimed': False,
            'scope': 'Exact previously reviewed pixels, observations, events, selection, role and tolerances.'}
        checked = evaluate_scope_review(directory, reused_annotation, persist=False)
        if checked['status'] != 'pass':
            raise VisualError('incomplete_review', 'An exact-reuse scope did not replay')
        destination = safe_path(output, entry['directory'] + '/annotation.json', must_exist=False)
        write_json(destination, reused_annotation)
        reused.append({'directory': entry['directory'], 'prior_directory': str(old_dir),
                       'annotation_sha256': file_sha256(destination),
                       'scope_checks': len(checked['checks']), 'role': record['role']})
    if any(file_sha256(p) != sha for p, sha in original_bindings.items()):
        raise VisualError('evidence_changed', 'Frozen review inputs changed during reuse')
    result = {'status': 'partial_annotations_only', 'reused': reused, 'pending': pending,
              'reused_count': len(reused), 'pending_count': len(pending),
              'position_marks_created': False, 'current_experiment_certified': False,
              'source_sha256': file_sha256(__file__), 'input_bindings': original_bindings}
    write_json(output / 'reuse-result.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prior', required=True, type=Path)
    p.add_argument('--current', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    result = run(a.prior, a.current, a.output)
    print({k: result[k] for k in ('status', 'reused_count', 'pending_count')})
