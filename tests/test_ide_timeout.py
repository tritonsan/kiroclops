"""Operator timeout bookkeeping with synthetic files and an explicit fake clock."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import ide_evidence, ide_timeout as timeout, storage
from scripts import record_ide_timeout as cli


class IDETimeoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'sessions/ide-test'
        self.now = datetime(2026, 9, 19, 10, 30, tzinfo=timezone.utc)
        self.started = self.now - timedelta(minutes=31)
        self.stopped = self.now - timedelta(seconds=10)
        for name in ('baseline', 'target'):
            root = self.directory / name
            root.mkdir(parents=True)
            (root / 'project.godot').write_bytes(b'[application]\n')
            (root / 'player.gd').write_bytes(b'extends Node\n')
        self.prompt = self.directory / 'target/.kiro/kiroclops-prompt.md'
        self.prompt.parent.mkdir()
        self.prompt.write_bytes(b'prepared explicit task\r\n')
        self.marker = self.prompt.with_name('kiroclops-session.json')
        self.marker.write_bytes(b'{"generated":"configuration"}\n')
        (self.directory / 'ide').mkdir()
        self.before = self.directory / 'ide/settings.png'
        self.before.write_bytes(b'synthetic starting UI image')
        self.screenshot = self.directory / 'ide/stopped.png'
        self.screenshot.write_bytes(b'synthetic stopped UI image')
        for name in ('.ide-run.lock', '.session.lock'):
            (self.directory / name).write_bytes(b'0')
        self.launch = {'schema_version': 2, 'surface': 'ide', 'transport': 'ide_gui', 'mode': 'tools',
            'session_id': 'ide-test', 'status': 'running', 'started_at': self.started.isoformat(),
            'deadline_at': self.started.timestamp()+1800, 'prompt_path': str(self.prompt),
            'prompt_sha256': storage.sha256(self.prompt), 'configuration_path': str(self.marker),
            'configuration_sha256': storage.sha256(self.marker),
            'ui_observed': {'evidence': [{'path': 'ide/settings.png', 'sha256': storage.sha256(self.before)}]},
            'model_requested': 'claude-opus-5', 'effort_requested': 'high', 'strict_configuration_verified': False}
        for name in ('kiro/launch.json', 'kiro/launch-start.json'):
            storage.write_json(self.directory / name, self.launch)
        self.state = {'session_id': 'ide-test', 'mode': 'tools', 'status': 'new',
            'started_at': self.launch['started_at'], 'deadline_at': self.launch['deadline_at'],
            'baseline': str(self.directory/'baseline'), 'target': str(self.directory/'target'),
            'baseline_hashes': storage.project_hashes(self.directory/'baseline'), 'candidates': [],
            'selected_candidate_id': None, 'active_job_id': None, 'final': None}
        storage.write_json(self.directory/'session.json', self.state)
        for module in (storage, ide_evidence):
            mocked = patch.object(module, 'ROOT', self.root)
            mocked.start(); self.addCleanup(mocked.stop)
        mocked = patch.object(timeout, '_now', return_value=self.now)
        mocked.start(); self.addCleanup(mocked.stop)
        mocked = patch.object(timeout.ide_run, '_config', side_effect=lambda _: ({}, self.prompt, storage.sha256(self.prompt)))
        mocked.start(); self.addCleanup(mocked.stop)

    def snapshot(self):
        return {p.relative_to(self.directory).as_posix(): p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}

    def record(self, **overrides):
        values = {'screenshot': 'ide/stopped.png', 'observed_stopped_at': self.stopped.isoformat(),
                  'operator': 'test operator', 'observation': 'Pending GUI turn cancelled after the deadline.'}
        return timeout.record_ide_timeout(self.directory, **{**values, **overrides})

    def assert_rejected_unchanged(self, **overrides):
        before = self.snapshot()
        with self.assertRaises((RuntimeError, OSError, ValueError)):
            self.record(**overrides)
        self.assertEqual(self.snapshot(), before)

    def test_expired_stop_is_one_way_and_preserves_clock_start_and_all_source_data(self):
        before = self.snapshot()
        receipt = self.record()
        self.assertEqual(receipt['status'], 'timeout')
        self.assertEqual(receipt['elapsed_seconds'], 1850)
        self.assertFalse(receipt['automatic_process_timeout_verified'])
        self.assertFalse(receipt['end_turn_attested'])
        self.assertFalse(receipt['acceptance_credit'])
        self.assertEqual(receipt['historical_effort_status'], 'unverified')
        state = storage.read_json(self.directory/'session.json')
        self.assertEqual(state, {**self.state, 'status': 'closed_failed'})
        launch = storage.read_json(self.directory/'kiro/launch.json')
        self.assertEqual(launch['status'], 'timeout')
        self.assertNotIn('prompt_result', launch)
        self.assertEqual(launch['operator_stop_receipt']['sha256'], storage.sha256(self.directory/timeout.RECEIPT))
        for name, raw in before.items():
            if name not in {'session.json', 'kiro/launch.json'}:
                self.assertEqual((self.directory/name).read_bytes(), raw)
        self.assert_rejected_unchanged()

    def test_actual_clock_and_observed_stop_must_both_be_after_original_deadline(self):
        with patch.object(timeout, '_now', return_value=self.started+timedelta(seconds=1799)):
            self.assert_rejected_unchanged()
        for when in (self.started+timedelta(seconds=1799), self.now+timedelta(seconds=1)):
            self.assert_rejected_unchanged(observed_stopped_at=when.isoformat())
        self.assert_rejected_unchanged(observed_stopped_at='2026-09-19T10:29:00')

    def test_missing_outside_startup_or_hardlinked_ui_evidence_is_rejected_without_writes(self):
        for image in ('ide/missing.png', str(self.root/'outside.png'), 'ide/settings.png'):
            self.assert_rejected_unchanged(screenshot=image)
        linked = self.directory/'ide/shared.png'
        linked.hardlink_to(self.screenshot)
        self.assert_rejected_unchanged(screenshot='ide/shared.png')

    def test_changed_start_launch_and_terminal_launch_are_never_overwritten(self):
        path = self.directory/'kiro/launch.json'
        original = path.read_bytes()
        path.write_bytes(original+b' ')
        self.assert_rejected_unchanged()
        for status in ('completed', 'timeout', 'turn_stopped'):
            for name in ('kiro/launch.json', 'kiro/launch-start.json'):
                storage.write_json(self.directory/name, {**self.launch, 'status': status})
            self.assert_rejected_unchanged()

    def test_active_pointer_and_orphan_nonterminal_jobs_require_existing_recovery(self):
        statepath = self.directory/'session.json'
        storage.write_json(statepath, {**self.state, 'active_job_id': 'job-live'})
        self.assert_rejected_unchanged()
        storage.write_json(statepath, self.state)
        for status in ('queued', 'running', 'unknown'):
            storage.write_json(self.directory/'jobs/job-live.json',
                {'session_id': 'ide-test', 'job_id': 'job-live', 'status': status})
            self.assert_rejected_unchanged()

    def test_recovered_jobs_candidates_and_final_results_are_preserved(self):
        candidate = {'candidate_id': 'candidate-01', 'status': 'failed', 'error': {'code': 'interrupted'}}
        state = {**self.state, 'status': 'closed_failed', 'candidates': [candidate],
                 'selected_candidate_id': 'candidate-01', 'final': {'numerical_status': 'pass', 'measurement_status': 'pending'}}
        storage.write_json(self.directory/'session.json', state)
        storage.write_json(self.directory/'jobs/job-old.json',
            {'session_id': 'ide-test', 'job_id': 'job-old', 'status': 'interrupted'})
        old_job = (self.directory/'jobs/job-old.json').read_bytes()
        receipt = self.record()
        self.assertEqual(storage.read_json(self.directory/'session.json'), state)
        self.assertEqual((self.directory/'jobs/job-old.json').read_bytes(), old_job)
        self.assertIn('jobs/job-old.json', receipt['job_hashes'])
        self.assertFalse(receipt['acceptance_credit'])

    def test_changed_clock_prompt_configuration_start_ui_or_baseline_rejects_read_only(self):
        for path in (self.prompt, self.marker, self.before, self.directory/'baseline/player.gd'):
            original = path.read_bytes()
            path.write_bytes(original+b'changed')
            self.assert_rejected_unchanged()
            path.write_bytes(original)
        storage.write_json(self.directory/'session.json', {**self.state, 'deadline_at': self.state['deadline_at']+1})
        self.assert_rejected_unchanged()

    def test_optional_export_is_only_opaque_bytes_without_native_completion_claim(self):
        export = self.directory/'ide/partial.zip'
        export.write_bytes(b'opaque deliberately unparsed ZIP fixture')
        result = self.record(export='ide/partial.zip')
        self.assertEqual(result['opaque_export']['sha256'], storage.sha256(export))
        self.assertFalse(result['end_turn_attested'])
        self.assertFalse((self.directory/'ide/evidence.json').exists())
        self.assertFalse((self.directory/'ide/finish-receipt.json').exists())

    def test_completed_receipt_missing_locks_and_absent_operator_observation_reject(self):
        self.assert_rejected_unchanged(operator='')
        self.assert_rejected_unchanged(observation=' ')
        lock = self.directory/'.ide-run.lock'
        lock.unlink()
        self.assert_rejected_unchanged()
        lock.write_bytes(b'0')
        storage.write_json(self.directory/'ide/finish-receipt.json', {'status': 'completed'})
        self.assert_rejected_unchanged()

    def test_partial_metadata_write_rolls_back_bytes_without_resetting_clock(self):
        before = self.snapshot()
        original = timeout.write_json
        def fail_state(path, value):
            if Path(path).name == 'session.json':
                raise KeyboardInterrupt()
            return original(path, value)
        with patch.object(timeout, 'write_json', side_effect=fail_state):
            with self.assertRaises(KeyboardInterrupt):
                self.record()
        self.assertEqual(self.snapshot(), before)

    def test_source_context_change_during_recording_refuses_commit(self):
        context = timeout._validate(self.directory, 'ide/stopped.png', self.stopped.isoformat())
        other = deepcopy(context)
        other['sources']['target']['player.gd'] = 'changed'
        with patch.object(timeout, '_validate', side_effect=[context, other]):
            self.assert_rejected_unchanged()

    def test_cli_requires_explicit_observation_and_reports_nonacceptance(self):
        args = ['--session', str(self.directory), '--screenshot', 'ide/stopped.png',
                '--observed-stopped-at', self.stopped.isoformat(), '--operator', 'test', '--observation', 'Cancelled pending GUI turn.']
        with patch('sys.stdout', new_callable=StringIO) as stdout:
            self.assertEqual(cli.main(args), 0)
        self.assertIn('"acceptance_credit": false', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
