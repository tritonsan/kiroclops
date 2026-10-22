"""Publication preserves checked evidence and rejects stale or incomplete receipts."""
import tempfile
import unittest
from pathlib import Path
from publish_completed_visual_review import run, read_json, write_json, file_sha256, digest, source_hashes


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kiroclops publication café naïve ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.review, self.session = self.root/'review', self.root/'session'
        source = self.session/'candidates/candidate-01/source'
        source.mkdir(parents=True)
        (source/'controller.js').write_text('const speed = 1;', encoding='utf-8')
        self.sha = digest(source_hashes(source))
        write_json(self.session/'session.json', {'active_job_id': None, 'final': {'status': 'succeeded'},
            'selected_candidate_id': 'candidate-01', 'candidates': [
                {'candidate_id': 'candidate-01', 'source_sha256': self.sha}]})
        preparation = {'session': str(self.session), 'candidate_sha256': self.sha,
            'required_observations': 20, 'requests': []}
        aggregate = {'candidate_sha256': self.sha, 'required_observations': 20}
        for scope in ('position', 'events', 'relationships'):
            relative = f'records/record-0000/{scope}'
            preparation['requests'].append({'directory': relative})
            folder = self.review/relative
            request, annotation = {'scope': scope}, {'scope': scope, 'observed': 1}
            write_json(folder/'request.json', request)
            write_json(folder/'annotation.json', annotation)
            result = {'status': 'pass', 'request_sha256': digest(request),
                'annotation_sha256': digest(annotation)}
            write_json(folder/'review-result.json', result)
            aggregate[scope+'_reviews'] = [{'directory': str(folder),
                'result_sha256': file_sha256(folder/'review-result.json'),
                'request_sha256': digest(request)}]
        write_json(self.review/'preparation.json', preparation)
        write_json(self.review/'preparation-binding.json', {'sha256': digest(preparation)})
        aggregate['preparation_sha256'] = file_sha256(self.review/'preparation.json')
        write_json(self.review/'independent_review.json', aggregate)
        self.checked = {'visual_status': 'pass', 'source_native_status': 'pass',
            'candidate_sha256': self.sha,
            'visual_receipt_sha256': file_sha256(self.review/'independent_review.json')}
        write_json(self.review/'check-result.json', self.checked)
        self.receipt = self.root/'publication.json'

    def rejected(self):
        with self.assertRaises(AssertionError):
            run(self.review, self.receipt)
        self.assertFalse(self.receipt.exists())
        self.assertFalse((self.session/'independent_review.json').exists())

    def test_windows_path_scope_normalization_and_byte_exact_publication(self):
        result = run(self.review, self.receipt)
        self.assertEqual(result['scopes'], 3)
        self.assertFalse(result['motion_acceptance_granted'])
        self.assertEqual((self.review/'independent_review.json').read_bytes(),
            (self.session/'independent_review.json').read_bytes())

    def test_source_changed_after_check_rejects(self):
        (self.session/'candidates/candidate-01/source/controller.js').write_text('const speed = 2;')
        self.rejected()

    def test_annotation_changed_after_check_rejects(self):
        write_json(self.review/'records/record-0000/events/annotation.json', {'changed': True})
        self.rejected()

    def test_unfinished_visual_check_rejects(self):
        self.checked['visual_status'] = 'pending'
        write_json(self.review/'check-result.json', self.checked)
        self.rejected()

    def test_missing_scope_rejects_even_if_aggregate_rehashed(self):
        path = self.review/'independent_review.json'
        data = read_json(path)
        data['events_reviews'] = []
        write_json(path, data)
        self.checked['visual_receipt_sha256'] = file_sha256(path)
        write_json(self.review/'check-result.json', self.checked)
        self.rejected()

    def test_duplicate_scope_rejects(self):
        path = self.review/'independent_review.json'
        data = read_json(path)
        data['events_reviews'] *= 2
        write_json(path, data)
        self.checked['visual_receipt_sha256'] = file_sha256(path)
        write_json(self.review/'check-result.json', self.checked)
        self.rejected()

    def test_existing_canonical_evidence_preserved(self):
        path = self.session/'independent_review.json'
        path.write_bytes(b'old evidence')
        with self.assertRaises(AssertionError):
            run(self.review, self.receipt)
        self.assertEqual(path.read_bytes(), b'old evidence')


if __name__ == '__main__':
    unittest.main(verbosity=2)
