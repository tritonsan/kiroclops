"""Host quietness sample and capture priority receipts (V6 plan WP2.2)."""
import os
import unittest

from kiroclops.web_local import host_quiet


class HostQuietTests(unittest.TestCase):
    def test_sample_reports_overshoot_statistics(self):
        report = host_quiet.host_quiet_report(seconds=.2, tick_s=.01)
        self.assertEqual(report['kind'], 'HostQuietSample')
        self.assertGreater(report['samples'], 5)
        self.assertGreaterEqual(report['max_overshoot_s'], report['p99_overshoot_s'])
        self.assertGreaterEqual(report['p99_overshoot_s'], report['median_overshoot_s'])
        self.assertIsInstance(report['spikes'], int)
        self.assertEqual(report['spike_s'], host_quiet.SPIKE_S)

    def test_quiet_decision_counts_spikes_only(self):
        self.assertTrue(host_quiet.quiet_enough({'spikes': 0}))
        self.assertFalse(host_quiet.quiet_enough({'spikes': 1}))
        self.assertTrue(host_quiet.quiet_enough({'spikes': 2}, max_spikes=2))
        self.assertFalse(host_quiet.quiet_enough(None))

    def test_descendants_follow_the_process_tree(self):
        table = [(1, 0, 'System'), (10, 1, 'python.exe'), (11, 10, 'chrome.exe'), (12, 11, 'chrome.exe'),
                 (13, 11, 'crashpad.exe'), (20, 1, 'chrome.exe'), (14, 12, 'headless_shell.exe')]
        found = host_quiet.descendant_pids(10, table=table)
        self.assertEqual(sorted(found), [(11, 'chrome.exe'), (12, 'chrome.exe'), (14, 'headless_shell.exe')])

    @unittest.skipUnless(os.name == 'nt', 'priority classes are Windows-only')
    def test_priority_is_raised_and_restored_on_windows(self):
        receipt = host_quiet.raise_capture_priority('above_normal')
        try:
            self.assertEqual(receipt['status'], 'applied')
            own = receipt['processes'][0]
            self.assertEqual(own['pid'], os.getpid())
            self.assertEqual(own['status'], 'set')
            self.assertEqual(own['class'], host_quiet.PRIORITY_CLASSES['above_normal'])
        finally:
            restored = host_quiet.restore_priorities(receipt)
        self.assertEqual(restored[0], {'pid': os.getpid(), 'status': 'restored'})
        with self.assertRaises(ValueError):
            host_quiet.raise_capture_priority('realtime')

    @unittest.skipIf(os.name == 'nt', 'non-Windows behaviour')
    def test_priority_is_reported_unsupported_elsewhere(self):
        receipt = host_quiet.raise_capture_priority('above_normal')
        self.assertEqual(receipt['status'], 'unsupported')
        self.assertEqual(host_quiet.restore_priorities(receipt), [])


if __name__ == '__main__':
    unittest.main()
