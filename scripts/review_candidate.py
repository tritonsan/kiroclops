"""Recheck a sealed candidate using explicit, independently observed controls."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.integration.review import review_session
from kiroclops.integration.storage import IntegrationError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path, help="Finished session directory")
    parser.add_argument("--controls", type=Path, help="JSON list of explicit candidate run points/events; optional")
    parser.add_argument("--reuse-controls-from", type=Path,
                        help="Approved finished session: reuse verified direct observations only when raw PNGs match exactly")
    parser.add_argument("--source", choices=("human", "assistant_visual"), default="human",
                        help="Who directly observed the supplied controls (default: human)")
    args = parser.parse_args(argv)
    try:
        result = review_session(args.session, args.controls, source=args.source,
                                reuse_controls_from=args.reuse_controls_from)
    except (IntegrationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": {"code": getattr(exc, "code", "review_error"),
                                                       "message": str(exc)}}, ensure_ascii=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0  # The JSON distinguishes a completed review from an accepted result.


if __name__ == "__main__":
    raise SystemExit(main())
