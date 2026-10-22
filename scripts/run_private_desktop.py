"""Opt-in standalone private desktop process launch. No existing session wiring."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.private_desktop_v1 import PrivateDesktopError, run_private_desktop


def main(argv=None):
    parser = argparse.ArgumentParser(description=(
        "Windows 10+ direct executable on a private non-input desktop. Requires explicit environment JSON. "
        "Blocks child processes; does not prove GPU rendering/timing and is not a security sandbox."))
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory; parent must exist.")
    parser.add_argument("--env-json", required=True, type=Path, help="Object of string environment values, including SystemRoot; not printed.")
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="After --: absolute direct .exe and arguments; no shell.")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        environment = json.loads(args.env_json.read_text(encoding="utf-8"))
        result = run_private_desktop(command, cwd=args.cwd, env=environment,
                                     output_dir=args.output, timeout=args.timeout)
        print(json.dumps({"status": result["status"], "exit_code": result["exit_code"],
                          "receipt": str(args.output.absolute() / "process.json")}, ensure_ascii=False))
        return 0 if result["status"] == "completed" else 124 if result["status"] == "timeout" else 130 if result["status"] == "cancelled" else 1
    except (PrivateDesktopError, ValueError, OSError) as error:
        print(json.dumps({"status": "error", "code": getattr(error, "code", "invalid_request"),
                          "message": str(error), "receipt": str(error.receipt_path) if getattr(error, "receipt_path", None) else None}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
