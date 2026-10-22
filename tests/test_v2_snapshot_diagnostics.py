"""The V2 post-hoc panel diagnostics stay verified in their own frozen V2 code epoch.

Both diagnostics transform the frozen V2 review reader. Since V3b integrates
their corrections into the live reader, they run against the verified V2
snapshot copy (C:/kc3d/code/v2-final), using that snapshot's own test files.
"""
import os
import subprocess
import sys
import unittest

from scripts import phase3d_v3_ground_parent as bridge
from tests.evidence import requires_evidence

MODULES = ("tests.test_phase3d_v2_panel_subframe_window_rule",
           "tests.test_phase3d_v2_panel_event_identity_diagnostic")


class V2SnapshotDiagnosticTests(unittest.TestCase):
    @requires_evidence('artifacts/phase3d-v3/v2-source-epoch-01', 'C:/kc3d/code/v2-final')
    def test_v2_diagnostics_pass_against_the_frozen_v2_reader(self):
        bridge.verify_snapshot()
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        done = subprocess.run([sys.executable, "-B", "-m", "unittest", *MODULES], cwd=str(bridge.SNAPSHOT),
                              env=environment, capture_output=True, text=True, timeout=900)
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        self.assertRegex(done.stderr, r"Ran [1-9]\d* tests")
        self.assertEqual("OK", done.stderr.strip().splitlines()[-1])
        # The run must leave every snapshot byte unchanged.
        bridge.verify_snapshot()


if __name__ == "__main__":
    unittest.main()
