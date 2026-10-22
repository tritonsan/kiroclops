"""Run the opt-in headless browser test that the cohort's test suite skipped.

tests/test_web_local.py CaptureBoundaryTests.test_headless_real_frame_clock_and_private_oracle
runs only with KIROCLOPS_BROWSER_TESTS=1. It runs once against each isolated runtime: the
exact cohort code archive (cohort-code) and the post-repair web runtime (web-code). Each run
follows the web readiness rule first (quiet machine, SSD-served Playwright driver, two clean
warm-up captures). The test file is unchanged since 2026-09-25; only the imported kiroclops
package differs. Records go to postrepair-completion-01/optin-browser-test/<runtime>.json.

Usage (from the repository root, with the .venv-local interpreter):
  python scripts/run_postrepair_optin_test.py --runtime cohort-code
  python scripts/run_postrepair_optin_test.py --runtime web-code
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import run_postrepair_completion as web  # binds kiroclops to the web runtime for the readiness rule

MAIN = Path(__file__).resolve().parents[1]
TEST_FILE = MAIN/'tests/test_web_local.py'
TEST_ID = 'CaptureBoundaryTests.test_headless_real_frame_clock_and_private_oracle'
# cohort-code: the verified NVMe copy the ground stages use; web-code: where the web trials imported from.
RUNTIMES = {'cohort-code': Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))
                           / 'kc3a-runtime/postrepair-sessions/cohort-code',
            'web-code': web.WEB_RUNTIME}
OUTPUT = web.STUDY/'optin-browser-test'
CHILD = r'''
import importlib.util, json, sys, unittest
sys.path.insert(0, sys.argv[1])
import kiroclops
spec = importlib.util.spec_from_file_location('test_web_local', sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[3], module)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print('RESULT ' + json.dumps({'kiroclops': kiroclops.__file__, 'tests_run': result.testsRun,
    'failures': [t.id() + ': ' + m[-1500:] for t, m in result.failures],
    'errors': [t.id() + ': ' + m[-1500:] for t, m in result.errors],
    'skipped': [t.id() + ': ' + r for t, r in result.skipped]}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', choices=tuple(RUNTIMES), required=True)
    args = parser.parse_args()
    root = RUNTIMES[args.runtime]
    path = OUTPUT/f'{args.runtime}.json'
    if path.exists():
        raise SystemExit(f'{path} exists; each runtime is tested once')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    readiness = web._ready(f'optin-{args.runtime}', ('panel',), web.RUNTIME_LOCK)
    env = dict(os.environ, KIROCLOPS_BROWSER_TESTS='1', PYTHONPATH='')
    started = time.time()
    done = subprocess.run([sys.executable, '-B', '-c', CHILD, str(root), str(TEST_FILE), TEST_ID],
                          capture_output=True, text=True, encoding='utf-8', errors='replace', env=env,
                          cwd=str(root), timeout=900)
    line = next((l for l in reversed(done.stdout.splitlines()) if l.startswith('RESULT ')), None)
    outcome = json.loads(line[7:]) if line else {'tests_run': 0, 'failures': [], 'skipped': [],
                                                 'errors': ['no result line: ' + done.stderr[-2000:]]}
    status = ('pass' if outcome['tests_run'] == 1 and not outcome['failures'] and not outcome['errors']
              and not outcome['skipped'] else 'fail')
    record = {'kind': 'PostRepairOptInBrowserTest', 'runtime': args.runtime, 'runtime_root': str(root),
              'test_file': str(TEST_FILE), 'test_file_sha256': web.file_sha256(TEST_FILE), 'test_id': TEST_ID,
              'environment': {'KIROCLOPS_BROWSER_TESTS': '1', 'PLAYWRIGHT_NODEJS_PATH': os.environ.get('PLAYWRIGHT_NODEJS_PATH')},
              'readiness': readiness, 'started_at': started, 'finished_at': time.time(), 'returncode': done.returncode,
              'status': status, **outcome, 'stderr_tail': done.stderr[-4000:]}
    path.write_text(json.dumps(record, indent=1) + '\n', encoding='utf-8')
    print({'runtime': args.runtime, 'status': status, 'kiroclops': outcome.get('kiroclops')}, flush=True)
    return 0 if status == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
