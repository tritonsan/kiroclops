"""Fail-closed native diagnostics; disposable projects, no model calls."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from kiroclops.local_engine import guard_diagnostics as diagnostics, launcher
from kiroclops.local_engine.backend import _lock
from kiroclops.visual.schema import VisualError, read_json, write_json
from tests.test_local_hook_enforcement import fixture


class GuardDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory, self.state, self.outside = fixture(Path(self.temp.name))

    def rows(self):
        return [json.loads(p.read_text('utf8')) for p in sorted(
            (self.directory/'kiro/guard-diagnostics').glob('*.json'))]

    def canonical(self):
        path = self.directory/'kiro/tool-audit.jsonl'
        return [json.loads(s) for s in path.read_text('utf8').splitlines()] if path.exists() else []

    def subprocess_hook(self, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, '-m', 'kiroclops.local_engine.guard_diagnostics',
                               '--session', str(self.directory), '--guard'], input=raw,
                              capture_output=True, text=True, encoding='utf8', timeout=15,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_healthy_audit_bytes_unchanged_and_diagnostics_separate(self):
        payload = {'tool_name': 'mcp_kiroclops_get_job', 'tool_input': {'job_id': 'a'*32, 'wait_seconds': 10}}
        with patch.object(launcher.time, 'time', return_value=1000):
            decision = launcher.guard(self.directory, payload, audit_origin='pre_tool_use')
        expected = {'at':1000, 'arguments':payload['tool_input'], 'audit_origin':'pre_tool_use', **decision}
        encoded = (json.dumps(expected, ensure_ascii=False, allow_nan=False)+'\n').encode('utf8')
        actual = (self.directory/'kiro/tool-audit.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(actual).hexdigest(), hashlib.sha256(encoded).hexdigest())
        self.assertTrue(decision['allow'])
        rows = self.rows()
        self.assertEqual({'start','load_config','guard','decision_persist','return'}, {r['phase'] for r in rows})
        self.assertTrue(all('arguments' not in r for r in rows))
        self.assertEqual('allowed', rows[-1]['outcome'])
        self.assertTrue(all(r['diagnostic_persistence']['fsync'] is False for r in rows))

    def test_malformed_event_and_early_path_denials_have_real_audit(self):
        for payload in ([], {'tool_name':'read_file','tool_input':None},
                        {'tool_name':'read_file','tool_input':{'path':'~/secret'}}):
            self.assertFalse(launcher.guard(self.directory, payload)['allow'])
        records = self.canonical()
        self.assertEqual(3, len(records))
        self.assertFalse(any(r['allow'] for r in records))
        self.assertEqual([], records[0]['arguments'])
        self.assertIsNone(records[1]['arguments'])
        self.assertEqual({'path':'~/secret'}, records[2]['arguments'])

    def test_config_failure_denies_and_records_original_error(self):
        config = read_json(self.directory/'kiro/config.json')
        config['model'] = 'wrong-model'
        write_json(self.directory/'kiro/config.json', config)
        with self.assertRaises(VisualError) as caught:
            launcher.guard(self.directory, {'tool_name':'read_file','tool_input':{'path':'allowed.txt'}})
        self.assertEqual('model_changed', caught.exception.code)
        self.assertFalse(self.canonical()[0]['allow'])
        failures = [r for r in self.rows() if r['phase']=='failure']
        self.assertEqual('model_changed', failures[0]['error']['code'])

    def test_audit_lock_failure_has_independent_diagnostic(self):
        with patch('kiroclops.local_engine.backend._lock', side_effect=VisualError('job_locked','fixture audit lock')):
            with self.assertRaises(VisualError) as caught:
                launcher.guard(self.directory, {'tool_name':'mcp_kiroclops_get_job','tool_input':{}})
        self.assertEqual('job_locked', caught.exception.code)
        self.assertEqual([], self.canonical())
        failures = [r for r in self.rows() if r['phase']=='decision_persist' and r['outcome']=='failed']
        self.assertEqual('job_locked', failures[0]['error']['code'])

    def test_primary_audit_write_failure_keeps_original_and_no_allow(self):
        with patch.object(launcher, '_append_guard_audit', side_effect=OSError('canonical disk error')):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, 'stdin', io.StringIO('{"tool_name":"mcp_kiroclops_get_job","tool_input":{}}')),\
                 redirect_stdout(out), redirect_stderr(err):
                result = diagnostics.run_hook(self.directory, guard_callable=launcher.guard)
        self.assertEqual(2, result)
        self.assertEqual('canonical disk error', json.loads(out.getvalue())['message'])
        self.assertFalse(any(r.get('outcome')=='allowed' and r['phase']=='return' for r in self.rows()))
        self.assertEqual([], self.canonical())

    def test_diagnostic_write_failure_never_masks_primary_or_grants_access(self):
        with patch.object(diagnostics, '_write_receipt', side_effect=OSError('diagnostic disk error')):
            record = diagnostics.GuardDiagnostics(self.directory)
            with patch.object(launcher, 'load_config', side_effect=VisualError('primary_failure','primary message')):
                with self.assertRaises(VisualError) as caught:
                    launcher.guard(self.directory, {'tool_name':'read_file','tool_input':{}}, _diagnostics=record)
            result = record.finish()
        self.assertEqual('primary_failure', caught.exception.code)
        self.assertEqual('failed', result['status'])
        self.assertFalse(self.canonical()[0]['allow'])

    def test_diagnostic_io_hang_has_bounded_flush(self):
        release = threading.Event()
        def blocked(*_): release.wait(5)
        with patch.object(diagnostics, '_write_receipt', side_effect=blocked):
            record = diagnostics.GuardDiagnostics(self.directory)
            record.emit('return', outcome='denied', exit_code=2)
            started = time.monotonic()
            result = record.finish()
            elapsed = time.monotonic()-started
            release.set()
            record.worker.join(2)
        self.assertLess(elapsed, .5)
        self.assertEqual('pending', result['status'])
        self.assertTrue(record.worker.daemon)

    def test_error_text_is_bounded_redacted_and_single_line(self):
        result = diagnostics.error_record(RuntimeError('token=abc123 secret=def password=ghi\nBearer xyz\x00'+'x'*3000))
        self.assertLessEqual(len(result['message']), 512)
        for secret in ('abc123','def','ghi','xyz','\n','\x00'):
            self.assertNotIn(secret, result['message'])
        self.assertIn('[redacted]', result['message'])

    def test_new_bootstrap_bound_missing_or_changed_hash_denies(self):
        config_path = self.directory/'kiro/config.json'
        original = read_json(config_path)
        for bindings in ({}, {'guard_diagnostics.py':'0'*64}):
            write_json(config_path, {**original, 'guard_sources':bindings})
            with self.assertRaises(VisualError) as caught: launcher.load_config(self.directory)
            self.assertEqual('guard_source_changed', caught.exception.code)

    def test_subprocess_bad_config_and_malformed_json_leave_phase_receipts(self):
        result = self.subprocess_hook('not json')
        self.assertEqual(2, result.returncode, result.stderr)
        rows = self.rows()
        self.assertTrue(any(r['phase']=='start' for r in rows))
        self.assertTrue(any(r['phase']=='return' and r['error']['type']=='JSONDecodeError' for r in rows))
        self.assertTrue(any('input_event_sha256' in r for r in rows))
        config = read_json(self.directory/'kiro/config.json')
        write_json(self.directory/'kiro/config.json', {**config,'model':'wrong'})
        result = self.subprocess_hook({'tool_name':'read_file','tool_input':{'path':'allowed.txt'}})
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual('model_changed', json.loads(result.stdout)['code'])
        self.assertFalse(self.canonical()[0]['allow'])
        self.assertTrue(any(r['phase']=='import_launcher' and r['outcome']=='started' for r in self.rows()))

    def test_real_audit_lease_timeout_still_persists_separate_error(self):
        with _lock(self.directory/'kiro/tool-audit.lock'):
            result = self.subprocess_hook({'tool_name':'mcp_kiroclops_get_job','tool_input':{}})
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual([], self.canonical())
        self.assertTrue(any(r['phase']=='decision_persist' and r['outcome']=='failed' for r in self.rows()))

    def test_concurrent_phase_writes_never_overwrite_another_invocation(self):
        records = [diagnostics.GuardDiagnostics(self.directory) for _ in range(12)]
        for record in records:
            record.emit('guard', outcome='denied')
            record.emit('return', outcome='denied', exit_code=2)
        for record in records:
            record.finish()
            record.worker.join(2)
        rows = self.rows()
        self.assertEqual(36, len(rows))
        self.assertEqual(12, len({r['invocation_id'] for r in rows}))
        self.assertEqual(36, len({(r['invocation_id'],r['sequence']) for r in rows}))


if __name__=='__main__':
    unittest.main()
