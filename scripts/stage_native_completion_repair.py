"""Stage native completion versus failed local validation classification.

The cohort remains frozen. This proposal also contains the already tested
pre-handle denial compatibility fixes. Full handoff replay stays mandatory.
"""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import difflib
import importlib.util
import io
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256, VisualError
from kiroclops.local_engine import review, acceptance
from tests.test_local_postturn_native_audit import PostturnNativeAuditTests
from tests.test_acceptance_axes import AcceptanceAxesTests
from prepare_rejected_cwd_repair import FUNCTIONS as REJECTION_FUNCTIONS

NATIVE_FUNCTIONS = REJECTION_FUNCTIONS + ('_recorded_native_turn_completed',
    '_audit_postturn_finalization', 'audit_native_run')

PREDICATE = '''def _recorded_native_turn_completed(launch):
    """Lifecycle evidence only; a failed worker still requires full handoff audit."""
    if launch.get('status') == 'completed':
        return launch.get('native_turn_status') in (None, 'completed')
    handoff = launch.get('postturn_finalization') or {}
    cleanup = launch.get('terminal_cleanup') or {}
    request = cleanup.get('request') or {}
    return (launch.get('status') == 'finalization_failed'
            and launch.get('native_turn_status') == 'completed'
            and handoff.get('status') == 'terminal'
            and handoff.get('job_status') in ('failed', 'cancelled', 'interrupted')
            and cleanup.get('status') == 'pass'
            and request.get('preserved_finalization') is True)


'''


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patched_reader(folder):
    proposed = load_module('kiroclops.local_engine._completion_proposal', Path(folder)/'review-proposed.py')
    axes = load_module('kiroclops.local_engine._completion_axes', Path(folder)/'acceptance-proposed.py')
    stack = ExitStack()
    for name in NATIVE_FUNCTIONS:
        original = getattr(proposed, name)
        # Use the real module namespace just as the eventual file replacement
        # will. In particular audit_native_tool_outcomes must remain the real
        # injectable boundary, rather than a stale proposal-module reference.
        function = types.FunctionType(original.__code__, review.__dict__, name,
                                      original.__defaults__, original.__closure__)
        function.__kwdefaults__ = original.__kwdefaults__
        stack.enter_context(patch.object(review, name, function, create=True))
    # Imported references in existing tests execute their original function's
    # globals. Patch those dependencies and use the proposed function explicitly
    # for the new presentation tests below.
    stack.enter_context(patch.object(acceptance, 'summarize_acceptance', axes.summarize_acceptance))
    return stack


class FailedHandoffTests(PostturnNativeAuditTests):
    def failed(self):
        error = {'code': 'capture_timing_failure', 'message': 'gap'}
        self.job.update(status='failed', error=error)
        self.state['final'].update(status='failed', pass_=False)
        self.state['final'].pop('pass_')
        self.state['final'].update({'pass': False, 'error': error})
        self.launch.update(status='finalization_failed', native_turn_status='completed')
        self.launch['terminal_cleanup']['terminal_job_status'] = 'failed'
        self.launch['postturn_finalization']['job_status'] = 'failed'

    def test_failed_worker_does_not_erase_bound_end_turn(self):
        self.failed()
        result = self.audit()
        self.assertEqual('pass', result['status'])
        self.assertEqual('failed', result['job_status'])
        self.assertFalse(self.state['final']['pass'])

    def test_failed_worker_requires_every_handoff_binding(self):
        self.failed()
        original = deepcopy(self.launch)
        variants = [lambda x: x.pop('postturn_finalization'),
                    lambda x: x.update(native_turn_status='timeout'),
                    lambda x: x['terminal_cleanup'].update(status='unconfirmed'),
                    lambda x: x['terminal_cleanup']['request'].update(preserved_finalization=False),
                    lambda x: x['postturn_finalization'].update(source_sha256='other'),
                    lambda x: x['postturn_finalization'].update(job_status='succeeded'),
                    lambda x: x['response'].update(stopReason='cancelled')]
        for change in variants:
            self.launch = deepcopy(original)
            change(self.launch)
            with self.assertRaises((VisualError, KeyError)):
                self.audit()

    def test_failed_worker_error_must_match_applied_state(self):
        self.failed()
        self.state['final']['error'] = {'code': 'different'}
        with self.assertRaises(VisualError):
            self.audit()


class CompletionAxesTests(AcceptanceAxesTests):
    def result(self):
        return acceptance.summarize_acceptance(self.state, self.report, recordings=self.records)

    def failed(self):
        self.state['final'].update(status='failed', error={'code': 'capture_timing_failure'})
        self.report.update(overall_acceptance='fail', acceptance_details={'error': {'code': 'capture_timing_failure'}})
        self.report['kiro_run'].update(status='finalization_failed', native_turn_status='completed',
            postturn_finalization={'status': 'terminal', 'job_status': 'failed'},
            terminal_cleanup={'status': 'pass', 'request': {'preserved_finalization': True}})

    def test_completed_native_and_failed_capture_remain_distinct(self):
        self.failed()
        result = self.result()
        self.assertEqual('pass', result['axes']['model_completion']['status'])
        self.assertEqual('invalid', result['axes']['measurement_validity']['status'])
        self.assertEqual('not_evaluated', result['axes']['observed_behavior']['status'])
        self.assertFalse(result['summary']['certified_success'])

    def test_no_claim_for_missing_generation_handoff_or_cleanup(self):
        self.failed()
        original = deepcopy(self.report['kiro_run'])
        for change in (lambda x: x.pop('postturn_finalization'),
                       lambda x: x.update(native_turn_status='timeout'),
                       lambda x: x['terminal_cleanup'].update(status='unconfirmed'),
                       lambda x: x['stream_configuration'].update(generation_configuration_verified=False)):
            self.report['kiro_run'] = deepcopy(original)
            change(self.report['kiro_run'])
            self.assertEqual('incomplete', self.result()['axes']['model_completion']['status'])


def replace_once(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old, new, 1)


def main():
    output = ROOT/'artifacts/phase3a/native-completion-compatibility-01'
    assert not output.exists()
    previous = ROOT/'artifacts/phase3a/native-rejection-compatibility-02'
    prior = read_json(previous/'result.json')
    assert prior['status'] == 'pass' and prior['tests'] == 77
    assert file_sha256(review.__file__) == prior['production_review_sha256']
    assert file_sha256(previous/'review-proposed.py') == prior['proposed_review_sha256']
    original = Path(review.__file__).read_text('utf-8')
    source = (previous/'review-proposed.py').read_text('utf-8')
    source = replace_once(source, 'def _audit_postturn_finalization(directory, launch):',
        PREDICATE + 'def _audit_postturn_finalization(directory, launch):')
    source = replace_once(source, "    if receipt is None:return {'status':'not_applicable'}",
        "    if receipt is None:\n        if launch.get('status') == 'finalization_failed':\n"
        "            raise VisualError('invalid_native_evidence', 'Failed finalization has no handoff receipt')\n"
        "        return {'status':'not_applicable'}")
    source = replace_once(source, "need(launch.get('status')=='completed' and launch.get('response',{}).get('stopReason')=='end_turn'",
        "need(_recorded_native_turn_completed(launch) and launch.get('response',{}).get('stopReason')=='end_turn'")
    source = replace_once(source, "need(launch.get('status')=='completed' and launch.get('prompt_sent') is True and",
        "need(_recorded_native_turn_completed(launch) and launch.get('prompt_sent') is True and")
    old_axes = Path(acceptance.__file__).read_text('utf-8')
    new_axes = replace_once(old_axes, 'def summarize_acceptance(state, report, *, recordings=()):',
        PREDICATE + 'def summarize_acceptance(state, report, *, recordings=()):')
    new_axes = replace_once(new_axes, "completed = (run.get('status') == 'completed' and response.get('stopReason') == 'end_turn'",
        "completed = (_recorded_native_turn_completed(run) and response.get('stopReason') == 'end_turn'")
    new_axes = replace_once(new_axes, "{'launch_status': run.get('status'), 'stop_reason': response.get('stopReason'),",
        "{'launch_status': run.get('status'), 'native_turn_status': run.get('native_turn_status'),\n"
        "         'postturn_job_status': (run.get('postturn_finalization') or {}).get('job_status'),\n"
        "         'stop_reason': response.get('stopReason'),")
    output.mkdir()
    for name, before, after in [('review', original, source), ('acceptance', old_axes, new_axes)]:
        (output/(name+'-proposed.py')).write_text(after, encoding='utf-8')
        (output/(name+'.patch')).write_text(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
            fromfile='kiroclops/local_engine/'+name+'.py', tofile='kiroclops/local_engine/'+name+'.py')), encoding='utf-8')
    log = io.StringIO()
    with patched_reader(output):
        suite = unittest.TestSuite()
        for cls in (FailedHandoffTests, CompletionAxesTests):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
        tested = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (output/'tests.txt').write_text(log.getvalue(), encoding='utf-8')
        assert tested.wasSuccessful(), log.getvalue()
        actual = []
        for trial, stage in [('panel-tools-01','panel'), ('svg-tools-01','svg'), ('panel-baseline-02','panel')]:
            session = ROOT/'artifacts/phase3a/completion-paired-cohort-01/sessions'/trial/stage
            result = review.audit_native_run(session)
            write_json(output/(trial+'-native-replay.json'), result)
            state = read_json(session/'session.json')
            launch = read_json(session/'kiro/launch.json')
            interpreted = acceptance.summarize_acceptance(state, {'kiro_run': launch,
                'overall_acceptance': 'fail', 'acceptance_details': {'error': state['final']['error']},
                'native_run_status': result['status']})
            assert interpreted['axes']['model_completion']['status'] == 'pass'
            assert interpreted['axes']['measurement_validity']['status'] == 'invalid'
            assert not interpreted['summary']['certified_success']
            actual.append({'trial': trial, 'native_status': result['status'],
                'calls': result['tool_outcomes']['application_call_count'],
                'failed_worker_remains_failed': result['postturn_finalization']['job_status'] == 'failed',
                'acceptance_axes': interpreted,
                'native_replay_sha256': file_sha256(output/(trial+'-native-replay.json'))})
    write_json(output/'actual-replays.json', actual)
    assert file_sha256(review.__file__) == prior['production_review_sha256']
    write_json(output/'result.json', {'status': 'pass', 'tests': tested.testsRun,
        'failures': len(tested.failures), 'errors': len(tested.errors), 'skipped': len(tested.skipped),
        'production_review_sha256': file_sha256(review.__file__),
        'production_acceptance_sha256': file_sha256(acceptance.__file__),
        'proposed_review_sha256': file_sha256(output/'review-proposed.py'),
        'proposed_acceptance_sha256': file_sha256(output/'acceptance-proposed.py'),
        'actual_replays_sha256': file_sha256(output/'actual-replays.json'),
        'actual_terminal_failures_replayed': len(actual), 'builder_sha256': file_sha256(__file__),
        'production_modified': False, 'primary_outcomes_modified': False,
        'scope': 'Completed native transport is separate from a failed source-bound local validation worker; no motion or aggregate success is created.'})
    print({'tests': tested.testsRun, 'actual_native_replays_pass': len(actual),
           'recording_failures_retained': len(actual), 'production_modified': False})


if __name__ == '__main__':
    main()
