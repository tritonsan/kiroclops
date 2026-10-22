"""Record explicit independent source review of a sealed ground-motion candidate."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.source_review import verify_source_review, write_source_review
from kiroclops.integration.storage import IntegrationError, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--review', type=Path,
        help='JSON with candidate_id, source, reviewer, checks and files from an independent static diff review')
    action.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    try:
        if args.review:
            decisions = read_json(args.review)
            if not isinstance(decisions, dict) or set(decisions) != {
                'candidate_id', 'source', 'reviewer', 'checks', 'files'
            }:
                raise ValueError('Review JSON needs exactly candidate_id, source, reviewer, checks and files')
            write_source_review(args.session, **decisions)
        result = verify_source_review(args.session)
    except (IntegrationError, OSError, ValueError, TypeError) as exc:
        result = {'status': 'fail', 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
