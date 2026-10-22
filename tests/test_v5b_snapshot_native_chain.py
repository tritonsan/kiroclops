"""The pinned 2.27 native chain still holds for the frozen V5b sources (see tests/frozen_native_chain.py).

The V5b snapshot (``C:/kc3d/code/v5b``) holds the sources only. The immutable pinned
evidence it reads (the staged 2.27 proofs, the launcher-extension receipt and the
2.27.1 release identity) stays in this repository and is copied beside a throwaway
copy of the snapshot; the snapshot itself is never written.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from kiroclops.visual.schema import file_sha256
from tests.evidence import requires_evidence

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = Path(os.environ.get('KIROCLOPS_V5B_SNAPSHOT', 'C:/kc3d/code/v5b'))
EPOCH_TREE = 'e89679ab0d3cffd401a99608802ab1fdfdde50e905d59b2dc0b9f00c9d7cd369'
EVIDENCE = ('reviews/staged-native227v3', 'artifacts/phase3d/launcher-execution-v2b',
            'artifacts/phase3d-v2/native-2271-release-identity-01')
MODULES = ('tests.test_launcher_policy_proof', 'tests.test_native_2271_release_identity',
           'tests.test_phase3d_v3_ground_closure', 'tests.test_phase3d_v5_panel_closure',
           'tests.test_phase3d_v5b_panel_closure')


@unittest.skipUnless((SNAPSHOT / 'epoch.json').is_file(), 'The frozen V5b snapshot is not on this machine.')
class V5bSnapshotNativeChainTests(unittest.TestCase):
    def test_snapshot_is_the_frozen_v5b_epoch(self):
        epoch = json.loads((SNAPSHOT / 'epoch.json').read_text(encoding='utf-8'))
        self.assertEqual(EPOCH_TREE, epoch['source_tree_sha256'])
        self.assertEqual(996, len(epoch['source_files']))
        for relative, sha in epoch['source_files'].items():
            self.assertEqual(sha, file_sha256(SNAPSHOT / relative), relative)

    @requires_evidence('reviews/staged-native227v3', 'artifacts/phase3d/launcher-execution-v2b', 'artifacts/phase3d-v2/native-2271-release-identity-01')
    def test_pinned_chain_tests_pass_on_the_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / 'v5b'
            shutil.copytree(SNAPSHOT, work)
            for relative in EVIDENCE:
                shutil.copytree(ROOT / relative, work / relative)
            run = subprocess.run([sys.executable, '-B', '-m', 'unittest', *MODULES], cwd=work, capture_output=True,
                                 text=True, encoding='utf-8', errors='replace', timeout=1800,
                                 env={**os.environ, 'PYTHONPATH': str(work)})
        self.assertEqual(0, run.returncode, run.stderr[-6000:])
        self.assertTrue(run.stderr.rstrip().endswith('OK') or 'OK (' in run.stderr.splitlines()[-1], run.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
