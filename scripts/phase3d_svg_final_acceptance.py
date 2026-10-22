"""Close unchanged SVG evidence without capture, model, or human attestation.

Run source-request only after an actual final raw image inspection has been
attached. A human independently supplies the source receipt; this command
never writes one. Keep every output outside the immutable input directories.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import suite_svg_final_acceptance as closure
from kiroclops.visual.schema import VisualError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('origins', 'historical-evidence'):
        command = commands.add_parser(name)
        command.add_argument('--initial', type=Path, required=True)
        command.add_argument('--output', type=Path, required=True)
        if name == 'historical-evidence':
            command.add_argument('--source-review', type=Path, required=True)
    request = commands.add_parser('source-request')
    request.add_argument('--initial', type=Path, required=True)
    request.add_argument('--visual-receipt', type=Path, required=True)
    request.add_argument('--origins', type=Path, required=True)
    request.add_argument('--historical-evidence', type=Path, required=True)
    request.add_argument('--functional-execution', type=Path, required=True)
    request.add_argument('--functional-witness', type=Path, required=True)
    request.add_argument('--output', type=Path, required=True)
    close = commands.add_parser('close')
    close.add_argument('--initial', type=Path, required=True)
    close.add_argument('--visual-receipt', type=Path, required=True)
    close.add_argument('--origins', type=Path, required=True)
    close.add_argument('--source-request', type=Path, required=True)
    close.add_argument('--source-receipt', type=Path, required=True)
    close.add_argument('--output', type=Path, required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'origins':
            result = closure.build_origin_bindings(args.initial, args.output)
        elif args.command == 'historical-evidence':
            result = closure.build_historical_bindings(args.initial, args.output, source_review=args.source_review)
        elif args.command == 'source-request':
            result = closure.prepare_source_review(args.initial, args.visual_receipt, args.origins, args.output,
                historical_evidence=args.historical_evidence, functional_execution=args.functional_execution,
                functional_witness=args.functional_witness)
        elif args.command == 'close':
            result = closure.close_acceptance(args.initial, args.visual_receipt, args.origins,
                                              args.source_request, args.source_receipt, args.output)
        else:
            result = closure.validate_acceptance(args.output)
    except (VisualError, OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'code': getattr(error, 'code', 'svg_closure_failed'),
            'message': str(error), 'full_behavior_acceptance': False, 'new_model_generation': False}))
        return 1
    print(json.dumps({'kind': result['kind'], 'status': result.get('status', 'evidence_declared'),
        'output': str(args.output), 'record_count': result.get('record_count'),
        'full_behavior_acceptance': result.get('full_behavior_acceptance', False), 'new_model_generation': False}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
