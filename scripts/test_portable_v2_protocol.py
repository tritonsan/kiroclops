"""Run all local tests and bind their evidence to v2 and all 81 preserved sources.

This runner does not qualify a cohort or invoke Kiro/Godot. Native boundaries in
the unit/contract tests are mocked; native qualification remains a separate step.
"""
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.portable_v2.specs import protocol_sources, verify_legacy
from kiroclops.integration.storage import sha256, write_json


def test_sources():
    return {p.relative_to(ROOT).as_posix(): sha256(p)
            for p in sorted((ROOT / 'tests').rglob('*.py'))}


def main():
    output = ROOT / 'artifacts/phase2c-v2/setup' / ('software-tests-' + str(time.time_ns()))
    output.mkdir(parents=True)
    before, legacy, sources = protocol_sources(), verify_legacy(), test_sources()
    if len(legacy) != 81:
        raise RuntimeError('Expected the original 61 sources plus the frozen 20 portable sources')
    runner = Path(__file__).resolve()
    runner_relative = runner.relative_to(ROOT).as_posix()
    runner_hash = sha256(runner)
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    with (output / 'tests.log').open('x', encoding='utf-8') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    unchanged = (before == protocol_sources() and legacy == verify_legacy()
                 and sources == test_sources() and runner_hash == sha256(runner))
    log_relative = (output / 'tests.log').relative_to(ROOT).as_posix()
    record = {
        'schema_version': 2, 'runner': 'portable-v2-full-test-suite-v1',
        'status': 'pass' if result.wasSuccessful() and unchanged else 'fail',
        'tests_run': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': len(result.skipped),
        'sources_unchanged': unchanged, 'protocol_sources': before,
        'legacy_sources_count': len(legacy), 'legacy_sources': legacy,
        'test_sources': sources,
        'evidence_files': {**sources, runner_relative: runner_hash,
                           log_relative: sha256(output / 'tests.log')},
    }
    write_json(output / 'result.json', record)
    print(str(output / 'result.json'), flush=True)
    print({key: record[key] for key in ('status', 'tests_run', 'failures', 'errors', 'skipped')}, flush=True)
    return 0 if record['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
