"""Launch a configured Kiro CLI experiment, or process its boundary hook."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.kiro import KiroError, guard_request, preflight_kiro, run_kiro


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--mode", choices=("tools", "baseline"), default="tools")
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--preflight", action="store_true", help="Verify live ACP workspace, agent, model and MCP; no prompt or model invocation.")
    parser.add_argument("--guard", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.guard:
            decision = guard_request(args.session, args.mode, json.load(sys.stdin))
            if not decision["allow"]:
                print(decision["reason"], file=sys.stderr)
                # Kiro PreToolUse interprets 2 as a blocking decision; 1 only
                # reports a hook error and must not be used for denials.
                return 2
            return 0
        if args.preflight:
            result = preflight_kiro(args.session, args.mode)
            print(json.dumps({"status": "ready", "model": result["config"]["model"],
                              "agent": result["config"]["agent_name"],
                              "acp_preflight": result["acp_preflight"],
                              "cli_version": result["cli_version"], "kas_version": result.get("kas_version")}, indent=2))
            return 0
        result = run_kiro(args.session, args.mode, prompt_path=args.prompt, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 1
    except (KiroError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "code": getattr(exc, "code", "invalid_input"), "error": str(exc)}), file=sys.stderr)
        return 2 if args.guard else 1


if __name__ == "__main__":
    raise SystemExit(main())
