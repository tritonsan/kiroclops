"""Prepare, inspect, attest and attach prospective realistic pixel reviews."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.local_engine import realistic_review as review
from kiroclops.visual.schema import read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cal = commands.add_parser("calibration")
    cal.add_argument("--records", type=Path, required=True)
    cal.add_argument("--output", type=Path, required=True)
    cal.add_argument("--domain", choices=("panel", "web_panel", "svg"), default="panel")
    final = commands.add_parser("final")
    final.add_argument("--session", type=Path, required=True)
    final.add_argument("--output", type=Path, required=True)
    attest = commands.add_parser("attest")
    attest.add_argument("--output", type=Path, required=True)
    attest.add_argument("--inspection", type=Path, required=True, help="Actual viewed-sheet journal: list[{path,sha256}].")
    attest.add_argument("--reviewer", required=True)
    attest.add_argument("--accepted", action="store_true")
    attest.add_argument("--images-inspected", action="store_true")
    check = commands.add_parser("validate")
    check.add_argument("--output", type=Path, required=True)
    floors = commands.add_parser("floors")
    floors.add_argument("--output", type=Path, required=True)
    attach = commands.add_parser("attach")
    attach.add_argument("--output", type=Path, required=True)
    attach.add_argument("--session", type=Path, required=True)
    attach.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "calibration":
        value = review.prepare_calibration_review(read_json(args.records), args.output, domain=args.domain)
    elif args.command == "final":
        value = review.prepare_final_review(args.session, args.output)
    elif args.command == "attest":
        value = review.attest_review(args.output, reviewer=args.reviewer, inspected_sheets=read_json(args.inspection),
                                    accepted=args.accepted, images_inspected=args.images_inspected)
    elif args.command == "floors":
        value = review.calibration_review(args.output)
    elif args.command == "attach":
        value = review.attach_final_review(args.output, args.receipt, session_directory=args.session)
    else:
        result = review.validate_review(args.output)
        value = {"status": "pass", "phase": result["phase"], "numeric_status": result["numeric_status"],
                 "full_behavior_acceptance": False}
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
