"""Record an already-stopped native IDE turn after its original deadline."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.ide_timeout import record_ide_timeout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--screenshot', required=True, help='Saved session-owned stopped-UI screenshot')
    parser.add_argument('--observed-stopped-at', required=True, help='Actual observed stop time, ISO8601 with timezone')
    parser.add_argument('--operator', required=True)
    parser.add_argument('--observation', required=True, help='Explicit description of the actual pending-turn UI cancellation')
    parser.add_argument('--export', help='Optional existing session-owned ZIP; opaque hash only')
    args = parser.parse_args(argv)
    try:
        result = record_ide_timeout(args.session, screenshot=args.screenshot, observed_stopped_at=args.observed_stopped_at,
                                    operator=args.operator, observation=args.observation, export=args.export)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'error', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}))
        return 2
    print(json.dumps({k: result[k] for k in ('status', 'basis', 'observed_stopped_at', 'elapsed_seconds', 'acceptance_credit')}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
