import json
from pathlib import Path
import tempfile
import unittest

from scripts.phase3d_v2_failed_preparation import MESSAGE, run
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


class FailedPreparationClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous = self.root / 'p' / 'v2s05'
        self.failed = self.root / 'p' / 'v2s07'
        self.output = self.root / 'a' / 'closure'
        self.cases = [{'case_id': f'case-{i:02}', 'role': 'regression' if i <= 12 else 'validation',
                       'scenario': {'id': f'case-{i:02}'}} for i in range(1, 25)]
        write_json(self.previous / 'calibration.json', {'stage': 'svg'})
        write_json(self.previous / 'recipes.json', self.cases)
        write_json(self.previous / 'start-binding.json', {'stage': 'svg', 'recipes_sha256': digest(self.cases)})
        write_json(self.failed / 'failure.json', {'code': 'capture_timing_failure', 'message': MESSAGE,
                                                'model_started': False, 'previous_attempts_preserved': True})
        write_json(self.failed / 'progress.json', {'phase': 'case-19: input 11/11', 'model_started': False})
        for case in self.cases[:18]:
            for role in ('reference', 'original', 'before'):
                write_json(self.failed / role / case['case_id'] / 'record.json',
                           {'measurement_status': 'ok', 'scenario': case['scenario'], 'source_sha256': 'a' * 64})
        self.attempt = self.failed / 'reference' / 'case-19' / 'attempt-01'
        write_json(self.attempt / 'capture-error.json', {'code': 'capture_timing_failure', 'message': MESSAGE})
        write_json(self.attempt / 'capture-start.json', {'scenario': self.cases[18]['scenario'], 'variant': 'reference',
                                                       'source_hashes': {}, 'adapter_hashes': {}})
        for name in ('received-evidence.json', 'frame-receipts.json', 'clock-runtime.json', 'progress-runtime.json',
                     'transport-context.json', 'transport-order.json', 'stream-baseline.json', 'preflight.json'):
            write_json(self.attempt / 'private' / name, {})

    def test_closes_failure_without_model_or_behavior_acceptance(self):
        result = run(self.failed, self.previous, self.output)
        self.assertEqual((result['completed_case_count'], result['missing_case_count']), (18, 6))
        self.assertEqual(len(result['completed_records']), 54)
        self.assertEqual(result['measurement_status'], 'fail')
        self.assertEqual(result['behavior_status'], 'not_evaluated')
        self.assertFalse(result['model_started'])
        self.assertFalse(result['full_behavior_acceptance'])
        receipt = read_json(self.output / 'receipt.json')
        self.assertEqual(receipt['closure_sha256'], digest(result))
        self.assertEqual(set(receipt['output_file_hashes']), {'closure.json', 'evidence-bindings.json'})
        self.assertFalse((self.root / 's' / 'v2s07').exists())

    def test_preserves_append_only_output(self):
        run(self.failed, self.previous, self.output)
        before = (self.output / 'closure.json').read_bytes()
        with self.assertRaises(VisualError):
            run(self.failed, self.previous, self.output)
        self.assertEqual((self.output / 'closure.json').read_bytes(), before)

    def test_rejects_completed_or_started_session(self):
        (self.root / 's' / 'v2s07').mkdir(parents=True)
        with self.assertRaises(VisualError):
            run(self.failed, self.previous, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_wrong_failure_or_completion_count(self):
        failure = read_json(self.failed / 'failure.json')
        failure['code'] = 'input_history_mismatch'
        write_json(self.failed / 'failure.json', failure)
        with self.assertRaises(VisualError):
            run(self.failed, self.previous, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_completed_role_cannot_be_hidden(self):
        (self.failed / 'before' / 'case-01' / 'record.json').unlink()
        with self.assertRaises(VisualError):
            run(self.failed, self.previous, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
