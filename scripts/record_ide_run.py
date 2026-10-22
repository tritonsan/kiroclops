"""Record screenshot-backed IDE start and native-export completion; no model run."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.integration.ide_run import finish_ide_run, start_ide_run
from kiroclops.integration.kiro import KiroError
from kiroclops.integration.storage import IntegrationError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="action", required=True)
    start = modes.add_parser("start", help="Record observed UI settings immediately before sending the prepared prompt")
    start.add_argument("--session", required=True)
    start.add_argument("--screenshot", action="append", required=True, help="Existing session-owned UI image; repeat as needed")
    start.add_argument("--agent", required=True)
    start.add_argument("--model", required=True)
    start.add_argument("--effort", required=True)
    finish = modes.add_parser("finish", help="Re-audit the final native ZIP and bind elapsed time to its completed turn")
    finish.add_argument("--session", required=True)
    finish.add_argument("--export", required=True, help="Native ZIP already saved inside this session")
    finish.add_argument("--screenshot", action="append", help="Optional preserved post-run UI screenshot; requires explicit --agent, --model and --effort")
    finish.add_argument("--agent")
    finish.add_argument("--model")
    finish.add_argument("--effort")
    args = parser.parse_args(argv)
    try:
        if args.action == "start":
            result = start_ide_run(args.session, screenshots=args.screenshot,
                                   agent=args.agent, model=args.model, effort=args.effort)
        else:
            observed = {}
            if any(value is not None for value in (args.screenshot, args.agent, args.model, args.effort)):
                observed = {"screenshots": args.screenshot, "agent": args.agent, "model": args.model, "effort": args.effort}
            result = finish_ide_run(args.session, export=args.export, **observed)
    except (IntegrationError, KiroError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps({key: result[key] for key in
                     ("status", "surface", "transport", "started_at", "strict_configuration_verified")}
                    | {key: result[key] for key in ("finished_at", "elapsed_seconds") if key in result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
