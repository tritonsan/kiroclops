"""Capture a supplemental wall-launch probe on a finished sealed candidate."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.gameplay_review import review_wall_behavior, verify_wall_review
from kiroclops.integration.storage import IntegrationError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--capture", action="store_true", help="Explicitly run the additional real Godot capture; default only verifies existing evidence")
    args = parser.parse_args(argv)
    try:
        if args.capture:
            review_wall_behavior(args.session)
        result = verify_wall_review(args.session)
    except (IntegrationError, OSError, ValueError) as exc:
        result = {"status": "fail", "error_code": getattr(exc, "code", type(exc).__name__), "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
