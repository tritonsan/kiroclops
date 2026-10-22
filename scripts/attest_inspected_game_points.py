"""Bind explicitly re-inspected raw game sheets to prior position marks.

Invoke only AFTER personally displaying every named aid sheet and correction
crop. This records that reviewer's attestation; it cannot itself inspect images.
It neither changes the original annotation nor grants any candidate acceptance.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from reuse_single_object_pixel_points import helper
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest


def run(prior, aid, output, actually_viewed):
    assert actually_viewed, 'Actual reviewer inspection is required'
    prior, aid, output = [Path(p).resolve() for p in (prior, aid, output)]
    assert not output.exists()
    bound = helper()
    identity_helper = bound._helper()
    request, records, files = identity_helper.load_group(prior)
    marks = prior/'annotation.json'
    annotation = read_json(marks)
    proposals = read_json(aid/'proposals.json')
    assert proposals['request_sha256'] == digest(request) == annotation['request_sha256']
    journal_path = aid/'visual-inspection-progress.json'
    viewed = {e['file']: e['sha256'] for e in read_json(journal_path)['viewed']}
    tasks = {(t['record_id'], t['object_id'], t['index']): t for t in request['tasks']}
    points = {(p['record_id'], p['object_id'], p['index']): p for p in annotation['points']}
    assert len(tasks) == len(request['tasks']) and len(points) == len(annotation['points'])
    assert set(tasks) == set(points)
    identities = {key: identity_helper.task_identity(tasks[key], records, prior, files) for key in points}
    correction_file = aid/'manual-corrections.json'
    corrections = ({p['raw_proposal_index']: p for p in read_json(correction_file)['points']}
                   if correction_file.exists() else {})
    proposed = {(p['record_id'], p['object_id'], p['index']): p for p in proposals['points']}
    evidence, coverage = {}, set()
    for sheet in proposals['sheets']:
        assert viewed[sheet['file']] == sheet['sha256'] == file_sha256(aid/sheet['file'])
        for region in sheet['bindings']:
            correction = corrections.get(region['raw_proposal_index'])
            name = correction['viewed_file'] if correction else sheet['file']
            path = aid/name
            assert viewed[name] == file_sha256(path)
            if name not in evidence:
                evidence[name] = {'id': name, 'kind': 'raw_sheet', 'viewed': True,
                    'path': str(path), 'sha256': file_sha256(path), 'points': []}
            rectangle = region['sheet_region']
            if correction:
                width, height = struct.unpack('>II', path.read_bytes()[16:24])
                rectangle = [8, 36, width-16, height-36]
            for task in region['tasks']:
                key = task['record_id'], task['object_id'], task['index']
                assert key not in coverage and key in points
                assert region['image_sha256'] == points[key]['image_sha256'] == tasks[key]['image_sha256']
                observed = correction or proposed[key]
                assert [observed['x'], observed['y']] == [points[key]['x'], points[key]['y']]
                if correction:
                    assert correction['image_sha256'] == region['image_sha256']
                evidence[name]['points'].append({
                    **{k: tasks[key][k] for k in ('record_id', 'object_id', 'index', 'image_sha256')},
                    'identity_sha256': digest(identities[key]), 'sheet_region': rectangle})
                coverage.add(key)
    assert coverage == set(points)
    def binding(p): return {'path': str(p), 'sha256': file_sha256(p)}
    receipt = {'schema_version': 1, 'kind': 'IndependentPositionInspectionBinding',
        'status': 'completed', 'scope': 'positions_only', 'method': 'actual_raw_image_inspection',
        'personally_viewed': True, 'external_human_review': False,
        'tracker_or_dom_values_used_as_truth': False, 'reviewer': annotation['reviewer'],
        'reviewed_at': datetime.now(timezone.utc).isoformat(),
        'request': binding(prior/'request.json'), 'marks': binding(marks),
        'request_sha256': digest(request), 'marks_sha256': digest(annotation),
        'original_inspection_journal': binding(journal_path), 'raw_aid': binding(aid/'proposals.json'),
        'evidence': list(evidence.values()), 'candidate_acceptance_granted': False,
        'inspection_note': 'Reviewer actually re-displayed every raw point sheet and any separately enlarged ambiguous head crops before invoking this attestation. Hat/head anchor checked consistently, including wall merges. Prior numeric marks retained unchanged; computer-assisted, not individual manual clicks.'}
    write_json(output, receipt)
    replay = bound.validate_inspection(output, prior, request, annotation, marks, tasks, identities, files)
    assert all(file_sha256(p) == sha for p, sha in files.items())
    print({k: replay[k] for k in ('kind', 'points', 'evidence_files')}, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('prior', 'aid', 'output'):
        parser.add_argument('--'+key, type=Path, required=True)
    parser.add_argument('--attest-actually-viewed', action='store_true', required=True)
    args = parser.parse_args()
    run(args.prior, args.aid, args.output, args.attest_actually_viewed)
