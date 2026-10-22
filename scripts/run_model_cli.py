"""Configure, preflight or run a fresh explicit-model Kiro CLI session."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.model_cli_v1 import (
    DEFAULT_MODEL, DEFAULT_EFFORT, KiroError, configure_model_cli, preflight_model_cli, run_model_cli,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--configure", action="store_true")
    actions.add_argument("--preflight", action="store_true", help="Native ACP handshake only; sends no prompt.")
    actions.add_argument("--run", action="store_true", help="Start the authorized paid model run.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--supplemental-instructions-file", type=Path,
                        help="Explicit additional cohort instructions, only with --configure.")
    args = parser.parse_args(argv)
    try:
        if args.supplemental_instructions_file and not args.configure:
            raise KiroError("invalid_input", "Supplemental instructions can only be bound while configuring a fresh session.")
        if args.configure:
            extra = (args.supplemental_instructions_file.read_text(encoding="utf-8")
                     if args.supplemental_instructions_file else None)
            config = configure_model_cli(args.session, model=args.model, effort=args.effort,
                                         supplemental_instructions=extra)
            result = {"status": "configured", "session": config["session_dir"], "model": config["model"],
                      "effort": config["effort"], "model_launcher": config["model_launcher"]}
        else:
            # Launch never silently substitutes CLI options for the bound profile.
            from kiroclops.integration.model_cli_v1 import load_model_config
            config = load_model_config(args.session)
            if (config["model"], config["effort"]) != (args.model, args.effort):
                raise KiroError("selection_conflict", "Command selection differs from the bound session profile.")
            if args.preflight:
                ready = preflight_model_cli(args.session)
                result = {"status": "ready", "model": config["model"], "effort": config["effort"],
                          "acp_preflight": ready["acp_preflight"], "cli_version": ready["cli_version"],
                          "kas_version": ready.get("kas_version")}
            else:
                result = run_model_cli(args.session, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"configured", "ready", "completed"} else 1
    except (KiroError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "code": getattr(exc, "code", "invalid_input"), "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
