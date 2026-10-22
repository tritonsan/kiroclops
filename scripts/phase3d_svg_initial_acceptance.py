"""Register/measure existing SVG source, then explicitly declare any repair.

No command launches a model, installs an editor hook, or changes SVG sources.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

# Support both ``python -m scripts...`` and the explicit script file without
# relying on an operator's PYTHONPATH or installing the workspace package.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.backend import _lock
from kiroclops.local_engine.reliability import prepare_runtime
from kiroclops.local_engine import suite_initial_acceptance as initial
from kiroclops.visual.schema import VisualError, write_json


def run(output, *, runtime_root=Path('C:/kc3d'), browser_path=None):
    """Own the common native-capture lease and restore the caller environment."""
    initial.load_plan(output)
    runtime = prepare_runtime(runtime_root, browser_path=browser_path)
    previous = {key: os.environ.get(key) for key in (*runtime['environment'], 'KIROCLOPS_ANALYSIS_BACKEND')}
    os.environ.update(runtime['environment'])
    os.environ['KIROCLOPS_ANALYSIS_BACKEND'] = 'local'

    def progress(phase, completed=0, total=1):
        print(phase, flush=True)
        write_json(Path(output) / 'progress.json', {'phase': phase, 'completed': completed, 'total': total,
            'model_started': False, 'new_model_generation': False})

    try:
        with _lock(Path(runtime['capture_lock']), timeout=60):
            return initial.run_initial_acceptance(output, progress=progress)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    register = commands.add_parser('register')
    register.add_argument('--suite', type=Path, required=True)
    register.add_argument('--parent', type=Path, required=True)
    register.add_argument('--output', type=Path, required=True)
    register.add_argument('--historical-session', type=Path, required=True)
    register.add_argument('--historical-candidate', required=True)
    register.add_argument('--max-seconds', type=int, default=None)
    measure = commands.add_parser('run')
    measure.add_argument('--output', type=Path, required=True)
    measure.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    measure.add_argument('--browser-path', type=Path)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--output', type=Path, required=True)
    repair = commands.add_parser('preregister-repair')
    repair.add_argument('--initial', type=Path, required=True)
    repair.add_argument('--output', type=Path, required=True)
    repair.add_argument('--suite', type=Path)
    repair.add_argument('--parent', type=Path)
    repair.add_argument('--exposure-attestation', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'register':
            result = initial.register_initial_acceptance(args.suite, args.parent, args.output,
                historical_session=args.historical_session, historical_candidate_id=args.historical_candidate,
                max_measurement_seconds=args.max_seconds)
        elif args.command == 'run':
            result = run(args.output, runtime_root=args.root, browser_path=args.browser_path)
        elif args.command == 'inspect':
            result = initial.load_result(args.output)
        else:
            result = initial.preregister_repair(args.initial, args.output, repair_suite_directory=args.suite,
                authoring_parent=args.parent, exposure_attestation=args.exposure_attestation)
    except (VisualError, OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'code': getattr(error, 'code', 'initial_acceptance_failed'), 'message': str(error)}))
        return 1
    # Detailed evidence stays in immutable local files, not a terminal dump.
    summary = {key: result[key] for key in ('kind', 'status', 'groups', 'numerical_pass', 'behavior_failure_observed',
        'repair_preregistration_allowed', 'full_behavior_acceptance', 'reference_status', 'heldout_status',
        'execution_ready', 'model_started', 'new_model_generation') if key in result}
    summary['output'] = str(args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if result.get('status') == 'measurement_failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
