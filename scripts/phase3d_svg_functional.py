"""Separate bounded SVG functional oracle; never a model or pixel measurement.

Request and inspect are read-only with respect to browser execution. Run starts
one fresh owned child/browser under the common lease. No command writes the
mandatory independent witness or grants full behavior acceptance.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.local_engine import suite_svg_functional as functional
from kiroclops.visual.schema import VisualError, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    request = commands.add_parser('request')
    request.add_argument('--initial', type=Path, required=True)
    request.add_argument('--output', type=Path, required=True)
    request.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    request.add_argument('--browser-path', type=Path)
    for name in ('run', '_worker'):
        command = commands.add_parser(name)
        command.add_argument('--request', type=Path, required=True)
        command.add_argument('--output', type=Path, required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'request':
            result = functional.prepare_request(args.initial, args.output, runtime_root=args.root, browser_path=args.browser_path)
        elif args.command == 'run':
            result = functional.run(args.request, args.output)
        elif args.command == '_worker':
            result = functional.worker(args.request, args.output, sys.stdin.readline().strip(),
                read_authorization=lambda: json.loads(sys.stdin.readline()))
            print(json.dumps({'worker_result_sha256': digest(result), 'challenge_sha256': result['challenge_sha256']}))
            return 0 if result['status'] == 'succeeded' else 1
        else:
            result = functional.inspect_run(args.output)
    except (VisualError, OSError, KeyError, TypeError, ValueError, TimeoutError) as error:
        print(json.dumps({'status': 'failed', 'code': getattr(error, 'code', 'svg_functional_failed'),
            'message': str(error), 'functional_qualified': False, 'full_behavior_acceptance': False, 'new_model_generation': False}))
        return 1
    print(json.dumps({k: result[k] for k in ('kind', 'status', 'functional_qualified', 'full_behavior_acceptance', 'new_model_generation') if k in result}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
