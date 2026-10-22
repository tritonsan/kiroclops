"""Transfer image/event facts only across byte-identical timed game sequences.

Different source/package identities never transfer source or motion acceptance.
Every PNG, timestamp, input, full selection and requested measurement must match.
Unlike approximate matching this may save repeated viewing of the same evidence.
"""
from copy import deepcopy
from pathlib import Path
import argparse
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest, VisualError
from kiroclops.visual.package import load_package
from kiroclops.local_engine.review import validate_independent_review, evaluate_scope_review
from reuse_exact_scope_reviews import load_kit


def sequence_key(record, manifest):
    if record['mechanic'] not in ('jump', 'ground', 'boundary'):
        raise VisualError('reuse_out_of_scope', 'Only fixed-cadence game sequences are supported')
    return digest({'role': record['role'], 'mechanic': record['mechanic'],
        'selection_sha256': record['selection_sha256'], 'image_size': manifest['image_size'],
        'inputs': manifest['inputs'],
        'frames': [{k: v for k, v in f.items() if k != 'path'} for f in manifest['frames']]})


def fact_key(request):
    value = deepcopy(request)
    for field in ('candidate_sha256', 'package_sha256', 'observations_sha256', 'events_sha256'):
        value.pop(field, None)
    return digest(value)


def run(prior, current, output):
    prior, current, output = map(lambda p: Path(p).resolve(), (prior, current, output))
    assert not output.exists() and prior != current
    assert all(not output.is_relative_to(p) for p in (prior, current))
    phase = ROOT/'artifacts/phase3a'
    progress = read_json(phase/'completion-paired-cohort-01/progress.json')
    assert len(progress['rows']) == 18 and all(r['status'] in ('completed', 'failed', 'interrupted') for r in progress['rows'])
    assert read_json(phase/'completion-durable-progress-diagnostics-01/pipeline.json')['status'] == 'diagnostics_finished_interpretation_required'
    old_prep, old = load_kit(prior)
    new_prep, new = load_kit(current)
    aggregate = prior/'independent_review.json'
    verified = validate_independent_review(aggregate, candidate_sha256=old_prep['candidate_sha256'])
    assert verified['preparation_sha256'] == file_sha256(prior/'preparation.json')
    certified = {str((aggregate.parent/e['directory']).resolve()): e
        for scope in ('events', 'relationships') for e in verified[scope+'_reviews']}
    bindings = {str(p): file_sha256(p) for p in (aggregate, prior/'preparation.json',
                current/'preparation.json', Path(__file__))}
    package_cache = {}

    def pixels(record):
        p = Path(record['package']).resolve()
        if str(p) not in package_cache:
            manifest = load_package(p)  # Rehash/decode every raw PNG, including non-task frames.
            bindings[str(p/'manifest.json')] = file_sha256(p/'manifest.json')
            package_cache[str(p)] = manifest
        m = package_cache[str(p)]
        assert m['package_sha256'] == record['package_sha256']
        return sequence_key(record, m)

    pool = {}
    for entry, folder, request, record in old:
        if str(folder.resolve()) not in certified:
            continue
        certificate = certified[str(folder.resolve())]
        annotation = read_json(folder/'annotation.json')
        saved = read_json(folder/'review-result.json')
        assert file_sha256(folder/'review-result.json') == certificate['result_sha256']
        assert digest(annotation) == saved['annotation_sha256']
        assert digest(request) == certificate['request_sha256']
        for name in ('request.json', 'annotation.json', 'review-result.json'):
            bindings[str(folder/name)] = file_sha256(folder/name)
        pool.setdefault((pixels(record), fact_key(request)), []).append((folder, request, annotation))
    reused, pending = [], []
    for entry, folder, request, record in new:
        matches = pool.get((pixels(record), fact_key(request)), [])
        if not matches:
            pending.append(entry['directory'])
            continue
        # IDs/task measurements/order and actual marks are exact, not merely pass labels.
        first = matches[0]
        if any(m[2]['checks'] != first[2]['checks'] for m in matches[1:]):
            pending.append(entry['directory'])
            continue
        old_folder, old_request, annotation = first
        copied = deepcopy(annotation)
        copied['request_sha256'] = digest(request)
        copied['reuse_provenance'] = {'kind': 'IdenticalTimedGamePixels',
            'prior_review_sha256': file_sha256(aggregate),
            'prior_annotation_sha256': file_sha256(old_folder/'annotation.json'),
            'prior_request_sha256': digest(old_request), 'current_request_sha256': digest(request),
            'full_sequence_sha256': pixels(record), 'all_requested_measurements_sha256': fact_key(request),
            'source_acceptance_transferred': False, 'candidate_acceptance_transferred': False,
            'new_visual_inspection_claimed': False,
            'scope': 'Previously inspected exact PNG sequence, clocks, inputs, full selection and requested facts. Source code is reviewed independently.'}
        checked = evaluate_scope_review(folder, copied, persist=False)
        assert checked['status'] == 'pass'
        dest = output/entry['directory']/'annotation.json'
        write_json(dest, copied)
        reused.append({'directory': entry['directory'], 'prior_directory': str(old_folder),
            'checks': len(checked['checks']), 'annotation_sha256': file_sha256(dest)})
    assert all(file_sha256(p) == sha for p, sha in bindings.items())
    summary = {'status': 'partial_annotations_only', 'reused': reused, 'pending': pending,
        'reused_count': len(reused), 'pending_count': len(pending),
        'new_visual_inspection_claimed': False, 'candidate_or_source_acceptance_granted': False,
        'input_bindings': bindings, 'policy': 'Exact complete raw sequence and request facts, no approximate match.'}
    write_json(output/'reuse-result.json', summary)
    return {k: summary[k] for k in ('status', 'reused_count', 'pending_count')}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.r = {'role': 'candidate', 'mechanic': 'jump', 'selection_sha256': 's'*64}
        self.m = {'image_size': [228,128], 'inputs': {'events': [{'time_s': .5, 'action': 'jump'}]},
                  'frames': [{'index': i, 'time_s': i/60, 'sha256': str(i)*64, 'path': f'{i}.png'} for i in range(3)]}
        self.q = {'scope': 'events', 'event_tolerance_s': .05, 'candidate_sha256': 'old',
                  'package_sha256': 'old', 'observations_sha256': 'old', 'events_sha256': 'old',
                  'tasks': [{'id': 'e0', 'measurement': {'type': 'stop', 'time_interval_s': [.5,.6]}, 'frames': []}]}

    def test_code_binding_is_separate(self):
        q = deepcopy(self.q)
        for k in ('candidate_sha256','package_sha256','observations_sha256','events_sha256'): q[k] = 'new'
        self.assertEqual(fact_key(self.q), fact_key(q))

    def test_any_raw_frame_change_rejects(self):
        m = deepcopy(self.m); m['frames'][-1]['sha256'] = 'f'*64
        self.assertNotEqual(sequence_key(self.r,self.m), sequence_key(self.r,m))

    def test_time_or_uncertainty_change_rejects(self):
        for key,val in (('time_s',.05),('uncertainty_s',.01)):
            m=deepcopy(self.m);m['frames'][1][key]=val
            self.assertNotEqual(sequence_key(self.r,self.m),sequence_key(self.r,m))

    def test_input_change_rejects(self):
        m=deepcopy(self.m);m['inputs']['events'][0]['time_s']=.6
        self.assertNotEqual(sequence_key(self.r,self.m),sequence_key(self.r,m))

    def test_selection_role_mechanic_change_rejects(self):
        for key,val in (('role','before'),('mechanic','boundary'),('selection_sha256','other')):
            r=deepcopy(self.r);r[key]=val
            self.assertNotEqual(sequence_key(self.r,self.m),sequence_key(r,self.m))

    def test_expected_fact_change_rejects(self):
        q=deepcopy(self.q);q['tasks'][0]['measurement']['time_interval_s']=[.6,.7]
        self.assertNotEqual(fact_key(self.q),fact_key(q))

    def test_tolerance_change_rejects(self):
        q=deepcopy(self.q);q['event_tolerance_s']=.1
        self.assertNotEqual(fact_key(self.q),fact_key(q))

    def test_web_rejected(self):
        r=deepcopy(self.r);r['mechanic']='svg'
        with self.assertRaises(VisualError):sequence_key(r,self.m)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test',action='store_true')
    for field in ('prior','current','output'):parser.add_argument('--'+field,type=Path)
    args=parser.parse_args()
    if args.self_test:
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(IdentityTests)
        raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
    assert args.prior and args.current and args.output
    print(run(args.prior,args.current,args.output))
