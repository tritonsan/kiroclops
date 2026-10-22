"""Stage recognition of an actually prevented shell read outside the target.

Only the exact pre-handle rejection can recognize an external absolute cwd.
Executed calls keep the original workspace restriction. No guard is changed.
"""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import difflib
import hashlib
import importlib.util
import io
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.local_engine import review
from tests.test_local_native_described_pwsh import ARGS, CONTEXT, OPUS_IDENTITY, audit, call, decision, rejected_call
from prepare_rejected_delete_repair import RejectedDeleteTests

FUNCTIONS = ('_native_tool_name', '_native_arguments', '_hook_denied_no_output',
             '_native_pwsh_arguments', '_native_rejected_pwsh_call')


class RejectedExternalCwdTests(unittest.TestCase):
    def fixture(self, cwd=r'D:\workspace\private\preservation'):
        args = {**ARGS, 'cwd': cwd, 'timeout': None}
        return args, rejected_call(args), [{**decision(args), 'allow': False, 'reason': 'Outside boundary'}]

    def test_exact_rejection_is_denied_not_authorized(self):
        for cwd in (r'D:\workspace\private\preservation', r'D:\workspace\target\..\private',
                    r'E:\private', r'\\server\share\private'):
            args, events, rows = self.fixture(cwd)
            original = deepcopy((events, rows))
            result = audit(events, rows)
            self.assertEqual(result['status'], 'pass', result)
            self.assertFalse(result['calls'][0]['decision_allow'])
            self.assertTrue(result['calls'][0]['denial_proven'])
            self.assertEqual(result['calls'][0]['denial_class'], 'denied_no_output')
            self.assertEqual((events, rows), original)

    def test_executed_external_cwd_does_not_gain_recognition(self):
        args, _, rows = self.fixture()
        args['timeout'] = 120000
        for allowed in (False, True):
            rows = [{**decision(args), 'allow': allowed}]
            self.assertEqual(audit(call(args, identity=OPUS_IDENTITY), rows)['status'], 'fail')

    def test_missing_mismatched_or_allow_decision_cannot_pass(self):
        args, events, rows = self.fixture()
        variants = [[], [decision(args)], rows + deepcopy(rows),
                    [{**rows[0], 'arguments': {**args, 'cwd': args['cwd'].upper()}}],
                    [{**rows[0], 'arguments': {**args, 'command': 'different'}}],
                    [{**rows[0], 'tool': 'read_file'}]]
        for supplied in variants:
            self.assertEqual(audit(events, supplied)['status'], 'fail')
        self.assertEqual(audit(events[1:], rows)['status'], 'fail')

    def test_any_output_or_different_native_shape_rejects(self):
        changes = [('rawOutput', {}), ('rawOutput', {'message': 'private data'}),
                   ('content', []), ('status', 'completed'), ('title', 'Read File'),
                   ('toolCallId', OPUS_IDENTITY), ('_meta', {'kiro': {'toolOrigin': 'client'}})]
        for field, value in changes:
            _, events, rows = self.fixture()
            events[-1]['params']['update'][field] = value
            self.assertEqual(audit(events, rows)['status'], 'fail')

    def test_unverified_runtime_cannot_borrow_rejection_shape(self):
        _, events, rows = self.fixture()
        self.assertEqual(audit(events, rows, {**CONTEXT, 'kas_version': 'unknown'})['status'], 'fail')

    def test_invalid_external_arguments_remain_invalid(self):
        for cwd in ('relative', r'\root-relative', r'C:drive-relative', '', None, 42):
            args, events, rows = self.fixture(cwd)
            if cwd is None:  # Existing schema intentionally accepts omitted/null cwd.
                continue
            self.assertEqual(audit(events, rows)['status'], 'fail')
        for key, value in [('run_in_background', True), ('timeout', 0), ('extra', 'unknown')]:
            args, events, rows = self.fixture()
            events[-1]['params']['update']['rawInput'][key] = value
            rows[0]['arguments'][key] = value
            self.assertEqual(audit(events, rows)['status'], 'fail')


def load_proposal(path):
    spec = importlib.util.spec_from_file_location('kiroclops.local_engine._rejection_proposal', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patched_reader(path):
    module = load_proposal(path)
    stack = ExitStack()
    for name in FUNCTIONS:
        stack.enter_context(patch.object(review, name, getattr(module, name)))
    return stack


def main():
    output = ROOT / 'artifacts/phase3a/native-rejection-compatibility-02'
    assert not output.exists(), 'Preserve earlier repair receipts'
    previous = ROOT / 'artifacts/phase3a/native-delete-compatibility-01'
    prior = read_json(previous / 'result.json')
    original_path = Path(review.__file__)
    assert prior['status'] == 'pass' and prior['tests'] == 71
    assert file_sha256(original_path) == prior['production_review_sha256']
    source = previous / 'review-proposed.py'
    assert file_sha256(source) == prior['proposed_review_sha256']
    original = original_path.read_text('utf-8')
    revised = source.read_text('utf-8')
    changes = {
        'def _native_pwsh_arguments(args,*,target_dir,allow_background=False):':
        'def _native_pwsh_arguments(args,*,target_dir,allow_background=False,allow_external_cwd=False):',
        'if ntpath.commonpath((root,cwd))!=root:return False':
        'if not allow_external_cwd and ntpath.commonpath((root,cwd))!=root:return False',
        "return _native_pwsh_arguments(_native_arguments('execute_pwsh',update['rawInput']),target_dir=target_dir)":
        "# A prevented request may name an external directory. This recognizes\n"
        "    # its identity, never permission: the caller still requires a matched\n"
        "    # deny, a completed hook and the exact output-free rejection envelope.\n"
        "    return _native_pwsh_arguments(_native_arguments('execute_pwsh',update['rawInput']),\n"
        "                                  target_dir=target_dir,allow_external_cwd=True)"}
    for old, new in changes.items():
        assert revised.count(old) == 1
        revised = revised.replace(old, new)
    output.mkdir(parents=True)
    proposed = output / 'review-proposed.py'
    proposed.write_text(revised, 'utf-8')
    (output / 'review.patch').write_text(''.join(difflib.unified_diff(original.splitlines(True),
        revised.splitlines(True), fromfile='kiroclops/local_engine/review.py',
        tofile='kiroclops/local_engine/review.py')), 'utf-8')
    proof_path = ROOT / 'artifacts/phase3a/panel-opus55-native-audit-02/source-proof.json'
    proof = read_json(proof_path)
    installed = Path(proof['source']).read_bytes()
    assert hashlib.sha256(installed).hexdigest() == proof['source_sha256']
    for name in ('prehandle_rejection', 'rejected_static_title', 'argument_null_to_undefined'):
        fragment = proof['fragments'][name]
        data = fragment['text'].encode('utf-8')
        assert hashlib.sha256(data).hexdigest() == fragment['sha256']
        assert installed[fragment['byte_offset']:fragment['byte_offset'] + len(data)] == data
    suite = unittest.TestSuite()
    for name in ('tests.test_local_native_tool_outcomes', 'tests.test_local_native_described_pwsh',
                 'tests.test_local_postturn_native_audit', 'tests.test_local_historical_audit_config'):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RejectedDeleteTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RejectedExternalCwdTests))
    stream = io.StringIO()
    with patched_reader(proposed):
        result = unittest.TextTestRunner(stream=stream).run(suite)
        actual = review.audit_native_run(ROOT / 'artifacts/phase3a/completion-paired-cohort-01/sessions/game-tools-02/jump')
    (output / 'tests.txt').write_text(stream.getvalue(), 'utf-8')
    write_json(output / 'actual-native-replay.json', actual)
    receipt = {'status': 'pass' if result.wasSuccessful() else 'fail', 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'production_review_sha256': file_sha256(original_path), 'proposed_review_sha256': file_sha256(proposed),
        'prior_delete_proposal_sha256': file_sha256(source), 'builder_sha256': file_sha256(__file__),
        'installed_source_sha256': proof['source_sha256'], 'inspected_source_proof_sha256': file_sha256(proof_path),
        'actual_native_replay_sha256': file_sha256(output / 'actual-native-replay.json'),
        'actual_native_status': actual['status'], 'actual_native_calls': actual['tool_outcomes']['application_call_count'],
        'guard_permissions_changed': False, 'production_modified': False, 'primary_outcomes_modified': False,
        'scope': 'Identity recognition for an independently bound, output-free, pre-handle denial only. Executed external cwd calls remain rejected by this auditor.'}
    write_json(output / 'result.json', receipt)
    print({k: receipt[k] for k in ('status', 'tests', 'failures', 'errors', 'actual_native_status', 'actual_native_calls', 'production_modified')})
    assert result.wasSuccessful(), stream.getvalue()


if __name__ == '__main__':
    main()
