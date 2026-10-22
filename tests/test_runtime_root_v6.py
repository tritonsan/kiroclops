"""V6 plan WP4.4: the short runtime root is a machine setting (KIROCLOPS_RUNTIME_ROOT), C:/kc3d only by default."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kiroclops.local_engine import reliability, suite_game, suite_svg_functional

ROOT = Path(__file__).resolve().parents[1]
PROBE = 'from kiroclops.local_engine import reliability; print(reliability.DEFAULT_RUNTIME_ROOT.as_posix())'


def default_root(**environment):
    env = {key: value for key, value in os.environ.items() if key != 'KIROCLOPS_RUNTIME_ROOT'}
    env.update(environment)
    run = subprocess.run([sys.executable, '-B', '-c', PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    if run.returncode:
        raise AssertionError(run.stderr[-2000:])
    return run.stdout.strip().splitlines()[-1]


class Stop(Exception):
    pass


class RuntimeRootTests(unittest.TestCase):
    def test_environment_variable_moves_the_default_root(self):
        self.assertEqual('C:/kc3d', default_root())
        self.assertEqual('D:/elsewhere/kiroclops-runtime', default_root(KIROCLOPS_RUNTIME_ROOT='D:/elsewhere/kiroclops-runtime'))

    def test_svg_functional_requests_and_game_runtimes_follow_the_setting(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(reliability, 'DEFAULT_RUNTIME_ROOT', Path(temp)):
            seen = []
            context = (Path(temp)/'acceptance', {'suite': Path(temp)/'suite', 'authoring_parent': Path(temp)/'parent'}, {})

            def doctor(root):
                seen.append(Path(root))
                raise Stop()
            with mock.patch.object(suite_svg_functional, '_context', return_value=context), \
                    mock.patch.object(suite_svg_functional, 'runtime_doctor', side_effect=doctor), self.assertRaises(Stop):
                suite_svg_functional.prepare_request(Path(temp)/'acceptance', Path(temp)/'request')
            self.assertEqual([Path(temp)], seen)
            directory = suite_game._short_runtime()
            self.assertEqual(Path(temp)/'runtime/game', directory.parent)


if __name__ == '__main__':
    unittest.main()
