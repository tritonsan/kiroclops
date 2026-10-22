from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.phase3d_v2_failed_replay_preparation import MESSAGE, run
from kiroclops.local_engine.realistic_policy import input_history_check, replay_scenario
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


class FailedReplayPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.previous, self.failed, self.output = self.root / 'p' / 'v2p02', self.root / 'p' / 'v2p03', self.root / 'a' / 'closure'
        events = [{'time_s': .4, 'action': 'click', 'selector': '#open-panel'},
                  {'time_s': .8, 'action': 'drag_start', 'selector': '#panel-handle'},
                  {'time_s': .9, 'action': 'drag_move', 'fraction': 1.}, {'time_s': 1., 'action': 'drag_end'}]
        self.cases = [{'case_id': f'case-{i:02}', 'role': 'regression' if i <= 12 else 'validation',
            'scenario': {'id': f'case-{i:02}', 'domain': 'panel', 'viewport': [1280, 720], 'duration_s': 3.8,
                         'events': deepcopy(events)}} for i in range(1, 25)]
        self.sources = {'index.html': 'b' * 64}
        self.source_sha = digest(self.sources)
        write_json(self.previous / 'calibration.json', {'stage': 'panel'})
        write_json(self.previous / 'recipes.json', self.cases)
        write_json(self.previous / 'start-binding.json', {'stage': 'panel', 'recipes_sha256': digest(self.cases),
                                                         'parent_source_sha256': self.source_sha})
        write_json(self.failed / 'failure.json', {'code': 'input_history_mismatch', 'message': MESSAGE,
                                                'model_started': False, 'previous_attempts_preserved': True})
        write_json(self.failed / 'progress.json', {'phase': 'analysis_tracking', 'model_started': False})
        for case in self.cases[:7]:
            for role in ('reference', 'original', 'before'):
                write_json(self.failed / role / case['case_id'] / 'record.json',
                           {'scenario': case['scenario'], 'measurement_status': 'ok', 'source_sha256': self.source_sha})
        scenario = self.cases[7]['scenario']
        actual_events = [{**e, 'client_x': 100., 'client_y': 100.} for e in deepcopy(scenario['events'])]
        ref = {'inputs': {'provenance': 'recorder_verified', 'events': actual_events},
               'objects': [{'frames': [{'time_s': 0.}, {'time_s': .016}, {'time_s': .032}]}]}
        record = {'scenario': scenario, 'measurement_status': 'ok', 'source_sha256': self.source_sha, 'observations': ref}
        for role in ('reference', 'original'):
            write_json(self.failed / role / 'case-08' / 'record.json', record)
        self.case_root = self.failed / 'before' / 'case-08'
        execution, proof = replay_scenario(scenario, ref)
        attempts = []
        for index in (1, 2):
            directory = self.case_root / f'attempt-{index:02}'
            observed = deepcopy(ref)
            observed['inputs']['events'][-1]['time_s'] += .04
            history = input_history_check(ref, observed)
            actual = {**deepcopy(record), 'observations': observed, 'execution_scenario': execution, 'input_replay': proof}
            receipt = {'attempt': index, 'directory': str(directory), 'record': actual,
                       'input_history': history, 'status': 'input_history_mismatch'}
            write_json(directory / 'capture-attempt.json', receipt)
            write_json(directory / 'capture-start.json', {'scenario': execution, 'source_hashes': self.sources, 'adapter_hashes': {}})
            attempts.append({'attempt': index, 'directory': str(directory), 'receipt_sha256': digest(receipt),
                             'status': 'input_history_mismatch', 'input_history': history})
        write_json(self.case_root / 'capture-replay.json', {'status': 'input_history_mismatch', 'attempts': attempts,
                                                           'selected_attempt': None, 'duration_rescaled': False})

    def test_recomputes_exact_two_mismatches_and_never_accepts_behavior(self):
        value = run(self.failed, self.previous, self.output)
        self.assertEqual((value['completed_case_count'], value['missing_case_count']), (7, 17))
        self.assertEqual(len(value['completed_records']), 23)
        self.assertEqual(value['behavior_status'], 'not_evaluated')
        self.assertFalse(value['model_started'])
        self.assertFalse(value['full_behavior_acceptance'])
        diag = read_json(self.output / 'diagnostic.json')
        self.assertTrue(diag['comparison_recomputed_from_actual_input_metadata'])
        self.assertFalse(diag['raw_png_decoded_or_hashed'])
        self.assertEqual(diag['attempt_count'], 2)

    def test_fresh_output_is_append_only(self):
        run(self.failed, self.previous, self.output)
        before = (self.output / 'receipt.json').read_bytes()
        with self.assertRaises(VisualError):
            run(self.failed, self.previous, self.output)
        self.assertEqual((self.output / 'receipt.json').read_bytes(), before)

    def test_rejects_missing_second_attempt(self):
        replay = read_json(self.case_root / 'capture-replay.json')
        replay['attempts'].pop()
        write_json(self.case_root / 'capture-replay.json', replay)
        with self.assertRaises(VisualError):
            run(self.failed, self.previous, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_tampered_actual_input_even_when_receipt_rehashed(self):
        path = self.case_root / 'attempt-02' / 'capture-attempt.json'
        receipt = read_json(path)
        receipt['record']['observations']['inputs']['events'][-1]['time_s'] += .02
        write_json(path, receipt)
        replay = read_json(self.case_root / 'capture-replay.json')
        replay['attempts'][1]['receipt_sha256'] = digest(receipt)
        write_json(self.case_root / 'capture-replay.json', replay)
        with self.assertRaises(VisualError):
            run(self.failed, self.previous, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_existing_session(self):
        (self.root / 's' / 'v2p03').mkdir(parents=True)
        with self.assertRaises(VisualError):
            run(self.failed, self.previous, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
