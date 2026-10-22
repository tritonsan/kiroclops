"""Do not hide missing probes or confuse recorder failures with model outcomes."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from summarize_recorder_diagnostics import (
    FINISHED, VisualError, declared_cases, file_sha256, read_json, run,
    summarize_receipt, write_json,
)


def receipt(domain='svg', scenario='three-to-five-w1280-r1', policy='frozen'):
    return {'kind': 'ProspectiveCaptureTimingProbe', 'status': 'captured',
            'source_unchanged': True, 'acceptance_run': False, 'model_run': False,
            'failed_trial_replaced': False, 'deadlines_changed': False, 'retries': 0,
            'domain': domain, 'scenario_id': scenario, 'diagnostic_static_policy': policy,
            'timing': {'status': 'pass', 'p95_interval_s': .017, 'errors': []},
            'trace': {'events': [{'name': 'page_evaluate', 'duration_s': .012}],
                      'dropped_old_events': 0}}


class ClassificationTests(unittest.TestCase):
    def test_capture_error_is_not_motion_failure_or_success(self):
        value = receipt()
        value.update(status='failed', timing=None, error={'code': 'capture_timing_failure'})
        result = summarize_receipt(value)
        self.assertEqual(result['status'], 'capture_failure')
        self.assertIsNone(result['metrics_s']['p95_interval_s'])
        self.assertFalse(result['causality_established'])

    def test_successful_process_with_bad_timing_stays_invalid(self):
        value = receipt()
        value['timing'].update(status='fail', p95_interval_s=.081, errors=['p95_frame_interval_exceeded'])
        self.assertEqual(summarize_receipt(value)['status'], 'timing_failure')

    def test_no_timings_never_means_zero_error(self):
        value = receipt(); value['timing'] = None
        self.assertEqual(summarize_receipt(value)['status'], 'timing_unavailable')

    def test_changed_source_retry_or_deadline_rejected(self):
        for key, replacement in (('source_unchanged', False), ('retries', 1),
                                 ('deadlines_changed', True), ('model_run', True)):
            with self.subTest(key=key):
                value = receipt(); value[key] = replacement
                with self.assertRaises(VisualError): summarize_receipt(value)

    def test_nonfinite_and_negative_numbers_rejected(self):
        for invalid in (float('inf'), float('nan'), -.1, True):
            with self.subTest(invalid=invalid):
                value = receipt(); value['timing']['p95_interval_s'] = invalid
                with self.assertRaises(VisualError): summarize_receipt(value)


class BoundReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='KiroClops diagnostics café naïve ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.output = self.base/'summary'
        for directory, cases in declared_cases():
            folder = self.base/directory
            rows = []
            for name, domain, scenario, policy, mode in cases:
                path = folder/name/'timing-probe.json'
                write_json(path, receipt(domain, scenario, policy))
                row = {'status': 'recorded', 'owned_tree_closed': True,
                       'receipt_sha256': file_sha256(path)}
                row.update({'mode': mode} if mode is not None else {'policy': policy})
                rows.append(row)
                if mode is not None:
                    write_json(folder/(name+'-sink')/'progress-io.json',
                               {'mode': mode, 'causality_established': False, 'operations': []})
            write_json(folder/'pipeline.json', {'status': FINISHED, 'rows': rows,
                       'primary_outcomes_modified': False, 'thresholds_changed': False})
        self.folder = self.base/declared_cases()[0][0]
        self.pipeline_path = self.folder/'pipeline.json'
        self.first_receipt = self.folder/declared_cases()[0][1][0][0]/'timing-probe.json'

    def change_pipeline(self, mutate):
        p = read_json(self.pipeline_path); mutate(p); write_json(self.pipeline_path, p)

    def rejected(self):
        with self.assertRaises(VisualError): run(self.base, self.output)
        self.assertFalse(self.output.exists())

    def test_all14_and_unicode_path_are_bound_without_success_claim(self):
        result = run(self.base, self.output)
        self.assertEqual(len(result['rows']), 14)
        self.assertEqual(result['counts']['timing_pass'], 14)
        self.assertFalse(result['reliability_established'])
        self.assertFalse(result['production_patch_authorized_by_this_report'])

    def test_pending_pipeline_rejected(self):
        self.change_pipeline(lambda p: p.update(status='capturing_fixture_diagnostics'))
        self.rejected()

    def test_missing_attempt_rejected(self):
        self.change_pipeline(lambda p: p['rows'].pop())
        self.rejected()

    def test_missing_registered_receipt_rejected(self):
        self.first_receipt.unlink()
        self.rejected()

    def test_incomplete_attempt_retained_in_denominator(self):
        self.first_receipt.unlink()
        self.change_pipeline(lambda p: p['rows'][0].update(status='probe_incomplete'))
        result = run(self.base, self.output)
        self.assertEqual(len(result['rows']), 14)
        self.assertEqual(result['counts']['incomplete'], 1)
        self.assertEqual(result['counts']['timing_pass'], 13)

    def test_changed_receipt_rejected(self):
        v = read_json(self.first_receipt); v['timing']['p95_interval_s'] = .1
        write_json(self.first_receipt, v)
        self.rejected()

    def test_wrong_scenario_even_with_updated_hash_rejected(self):
        v = read_json(self.first_receipt); v['scenario_id'] = 'other'
        write_json(self.first_receipt, v)
        self.change_pipeline(lambda p: p['rows'][0].update(receipt_sha256=file_sha256(self.first_receipt)))
        self.rejected()

    def test_unconfirmed_process_cleanup_rejected(self):
        self.change_pipeline(lambda p: p['rows'][0].update(owned_tree_closed=False))
        self.rejected()

    def test_altered_durable_order_rejected(self):
        path = self.base/declared_cases()[2][0]/'pipeline.json'
        p = read_json(path); p['rows'][0]['mode'] = 'durable'; write_json(path, p)
        self.rejected()


if __name__ == '__main__':
    unittest.main()
