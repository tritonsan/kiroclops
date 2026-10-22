"""Tiny real evidence fixtures and injected evaluators; no apps or models."""
import asyncio
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import stage_closure, suite_web_v2
from kiroclops.local_engine.backend import _atomic
from kiroclops.local_engine.execution_policy import REVISED_ID, resolve_policy
from kiroclops.local_engine.realistic_policy import input_history_check, replay_scenario
from kiroclops.local_engine.session import Session, create_session, run_session_job, source_hashes
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


class StageMeasurementClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fatal stage café naïve ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = self.root / 'baseline'
        (self.baseline / 'src').mkdir(parents=True)
        (self.baseline / 'src/panel-motion.js').write_text('existing motion', encoding='utf8')
        (self.baseline / 'index.html').write_text('<main>fixture</main>', encoding='utf8')
        self.reference = self.root / 'reference'
        scenario = next(row for row in suite_web_v2.scenarios('panel', 'calibration') if row['recipe'] == 'open-close')
        self.scenario = scenario
        self.image = self.root / 'frame.png'
        cv2.imencode('.png', np.zeros((3, 3, 3), np.uint8))[1].tofile(self.image)
        inputs = {'provenance': 'recorder_verified', 'events': [{**event, 'client_x': 100., 'client_y': 100.}
            for event in scenario['events']]}
        package = create_package(self.reference / 'package', [self.image] * 3, [0., .05, .1], inputs)
        self.observed = {'package_sha256': package['package_sha256'], 'inputs': inputs,
            'objects': [{'id': 'panel', 'frames': [{'time_s': timestamp} for timestamp in (0., .05, .1)]}]}
        self.case = {'case_id': scenario['case_id'], 'role': 'calibration', 'stage': 'panel', 'scenario': scenario,
            'package_sha256': package['package_sha256'], 'timeline_sha256': digest(inputs),
            'record': {'observations': self.observed}}
        hashes = source_hashes(self.baseline)
        execution = resolve_policy(REVISED_ID, 'panel')
        self.suite = {'kind': 'ReferenceMediaSuite', 'schema_version': 2, 'stage': 'panel', 'cases': [self.case],
            'execution_policy': execution, 'original_source_hashes': hashes, 'authoring_parent_source_hashes': hashes,
            'source_epoch': 'fixture-source-epoch', 'file_hashes': {}}
        write_json(self.reference / 'suite.json', self.suite)
        write_json(self.reference / 'suite-binding.json', {'suite_sha256': digest(self.suite)})
        self.directory = self.root / 'session'
        create_session(self.directory, 'web_panel', self.baseline, self.reference, 'Fixture stage', experiment={
            'reference_mode': 'media_suite', 'stage': 'panel', 'suite_sha256': digest(self.suite),
            'calibration': [scenario], 'capture_lock': str(self.root / 'capture.lock')}, execution_policy=execution)
        self.session = Session(self.directory)

    def submit(self, operation, result=None, failure=None):
        with patch.object(Session, '_spawn'):
            submitted = self.session.submit(operation)
        def runner(directory, job, *args):
            if failure is not None:
                self.capture_proof(job, 'original' if operation == 'prepare' else 'candidate')
                raise VisualError(failure, 'injected evaluator failure')
            return result or {}
        run_session_job(self.directory, submitted['job_id'], runner=runner)
        return self.session.get_job(submitted['job_id'])

    def capture_proof(self, job, role):
        root = self.directory / 'measurements' / job['job_id'] / role / self.case['case_id']
        execution, replay = replay_scenario(self.scenario, self.observed)
        attempts = []
        for index in (1, 2):
            destination = root / f'attempt-{index:02d}'
            inputs = deepcopy(self.observed['inputs'])
            inputs['events'][1]['time_s'] += .11
            package = create_package(destination / 'package', [self.image] * 3, [0., .05, .1], inputs,
                {'fixture_actual_capture': index})
            observed = {**deepcopy(self.observed), 'inputs': inputs, 'package_sha256': package['package_sha256']}
            history = input_history_check(self.observed, observed)
            record = {'package': str(destination / 'package'), 'observations': observed,
                'scenario': self.scenario, 'execution_scenario': execution, 'input_replay': replay,
                'source_sha256': job['source_sha256']}
            receipt = {'attempt': index, 'directory': str(destination), 'record': record,
                'input_history': history, 'status': 'input_history_mismatch'}
            write_json(destination / 'capture-attempt.json', receipt)
            attempts.append({'attempt': index, 'directory': str(destination), 'receipt_sha256': digest(receipt),
                'status': 'input_history_mismatch', 'input_history': history})
        write_json(root / 'capture-replay.json', {'status': 'input_history_mismatch', 'attempts': attempts,
                   'selected_attempt': None, 'duration_rescaled': False})
        self.proof_root = root

    def fail_candidate(self):
        self.assertEqual('succeeded', self.submit('prepare')['status'])
        job = self.submit('evaluate', failure='input_history_mismatch')
        self.assertEqual('failed', job['status'])
        self.assertEqual('input_history_mismatch', job['error']['code'])
        return job

    def assert_closed(self, operation):
        before = deepcopy(self.session._load())
        with patch.object(Session, '_spawn') as spawn:
            with self.assertRaises(VisualError) as error:
                self.session.submit(operation)
        self.assertEqual('stage_measurement_insufficient', error.exception.code)
        self.assertEqual(before['candidates'], self.session._load()['candidates'])
        self.assertEqual(before['prepare_job_id'], self.session._load()['prepare_job_id'])
        spawn.assert_not_called()

    def test_two_real_mismatches_close_submission_without_new_budget_attempt(self):
        failed = self.fail_candidate()
        closure = stage_closure.inspect_closure(self.directory, self.session._load())
        self.assertEqual('measurement_insufficient', closure['status'])
        self.assertEqual('verified', closure['evidence_status'])
        self.assertEqual(failed['job_id'], closure['fatal_job_id'])
        self.assertEqual([1, 2], [attempt['attempt'] for attempt in closure['capture']['attempts']])
        self.assertFalse(closure['behavior_failure_inferred'])
        self.assertFalse(closure['automatic_restart'])
        for operation in ('prepare', 'evaluate', 'finalize'):
            self.assert_closed(operation)

    def test_inspect_reports_closed_measurement_and_retains_original_error(self):
        failed = self.fail_candidate()
        with patch('kiroclops.local_engine.media_suite.inspect_suite', return_value={}), \
             patch('kiroclops.local_engine.calibration_planning.inspect_advice', return_value={}):
            inspected = self.session.inspect()
        self.assertEqual('measurement_insufficient', inspected['status'])
        self.assertEqual(0, inspected['budget']['remaining_candidates'])
        self.assertEqual(1, inspected['budget']['used_candidates'])
        self.assertEqual(3, inspected['budget']['max_candidates'])
        self.assertEqual(failed['job_id'], inspected['measurement_closure']['fatal_job_id'])
        self.assertEqual('input_history_mismatch', self.session.get_job(failed['job_id'])['error']['code'])

    def test_preparation_failure_also_closes_before_model_or_candidates(self):
        self.submit('prepare', failure='input_history_mismatch')
        for operation in ('prepare', 'evaluate', 'finalize'):
            self.assert_closed(operation)
        self.assertEqual([], self.session._load()['candidates'])

    def test_invalid_or_single_attempt_proof_is_honest_fail_closed(self):
        self.fail_candidate()
        path = self.proof_root / 'capture-replay.json'
        value = read_json(path)
        value['attempts'] = value['attempts'][:1]
        write_json(path, value)
        closure = stage_closure.inspect_closure(self.directory, self.session._load())
        self.assertEqual('invalid', closure['evidence_status'])
        self.assertEqual('fatal_input_history_evidence_incomplete', closure['reason'])
        self.assertNotIn('capture', closure)
        self.assert_closed('evaluate')

    def test_missing_receipt_and_wrong_source_cannot_claim_verified_failure(self):
        self.fail_candidate()
        receipt = self.proof_root / 'attempt-01/capture-attempt.json'
        value = read_json(receipt)
        value['record']['source_sha256'] = '0' * 64
        write_json(receipt, value)
        closure = stage_closure.inspect_closure(self.directory, self.session._load())
        self.assertEqual('invalid', closure['evidence_status'])
        self.assert_closed('finalize')
        receipt.unlink()
        closure = stage_closure.inspect_closure(self.directory, self.session._load())
        self.assertEqual('invalid', closure['evidence_status'])
        self.assert_closed('evaluate')

    def test_queued_worker_is_blocked_even_if_submit_guard_was_bypassed(self):
        self.fail_candidate()
        with patch.object(stage_closure, 'guard_operation'), patch.object(Session, '_spawn'):
            queued = self.session.submit('evaluate')
        called = []
        run_session_job(self.directory, queued['job_id'], runner=lambda *args: called.append('unexpected capture'))
        failed = self.session.get_job(queued['job_id'])
        self.assertEqual('failed', failed['status'])
        self.assertEqual('stage_measurement_insufficient', failed['error']['code'])
        self.assertEqual([], called)

    def test_report_read_and_cancel_remain_available(self):
        failed = self.fail_candidate()
        self.assertEqual('failed', self.session.get_job(failed['job_id'])['status'])
        self.assertEqual('failed', self.session.cancel_job(failed['job_id'])['status'])
        with patch.object(Session, '_spawn'):
            submitted = self.session.submit('report')
        run_session_job(self.directory, submitted['job_id'], runner=lambda *args: {'status': 'report-built'})
        report = self.session.get_job(submitted['job_id'])
        self.assertEqual('succeeded', report['status'])
        self.assertEqual('report-built', report['result']['status'])

    def test_comparison_failure_class_and_other_execution_errors_do_not_close(self):
        self.submit('prepare')
        failed_quality = self.submit('evaluate', {'eligible': False, 'score': .2,
            'comparisons': [{'failure_class': 'input_history_mismatch'}]})
        self.assertEqual('succeeded', failed_quality['status'])
        self.assertIsNone(stage_closure.inspect_closure(self.directory, self.session._load()))
        failed_execution = self.submit('evaluate', failure='fixture_compilation_error')
        self.assertEqual('failed', failed_execution['status'])
        self.assertIsNone(stage_closure.inspect_closure(self.directory, self.session._load()))

    def test_legacy_session_and_other_session_are_unchanged(self):
        self.fail_candidate()
        legacy = self.root / 'legacy'
        create_session(legacy, 'web_panel', self.baseline, self.reference, 'Legacy fixture',
                       experiment={'capture_lock': str(self.root / 'capture.lock')})
        self.assertIsNone(stage_closure.inspect_closure(legacy, Session(legacy)._load()))
        other = self.root / 'other'
        create_session(other, 'web_panel', self.baseline, self.reference, 'Another revised fixture',
            experiment=self.session._load()['experiment'], execution_policy=resolve_policy(REVISED_ID, 'panel'))
        self.assertIsNone(stage_closure.inspect_closure(other, Session(other)._load()))
        with patch.object(Session, '_spawn'):
            queued = Session(other).submit('prepare')
        self.assertEqual('queued', queued['status'])

    def test_cli_and_mcp_cannot_allocate_another_candidate(self):
        self.fail_candidate()
        from kiroclops.local_engine.mcp import create_server
        server, _ = create_server(self.directory)
        for tool in ('prepare_experiment', 'evaluate_candidate', 'finalize_candidate'):
            result = asyncio.run(server.call_tool(tool, {}))
            self.assertEqual('stage_measurement_insufficient', result.structured_content['error']['code'])
        from kiroclops.local_engine.__main__ import main
        for operation in ('prepare', 'evaluate', 'finalize'):
            output = io.StringIO()
            with patch('kiroclops.local_engine.environment.configure_environment'), contextlib.redirect_stdout(output):
                code = main(['trial', operation, '--session', str(self.directory), '--async'])
            self.assertEqual(1, code)
            self.assertEqual('stage_measurement_insufficient', json.loads(output.getvalue())['code'])
        self.assertEqual(1, len(self.session._load()['candidates']))


if __name__ == '__main__':
    unittest.main()
