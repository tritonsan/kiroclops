"""Plan or sequentially launch explicitly named, fresh KiroClops sessions.

The default is a read-only dry run. --run authorizes real model calls and may
consume credits. A finished batch is not evidence that an experiment passed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.common import ROOT
from kiroclops.integration.kiro import EFFORT, MODEL, MAX_SECONDS, run_kiro
from kiroclops.integration.session import prepare_session
from kiroclops.integration.storage import IntegrationError, identifier, read_json, utc_now, write_json


ENVIRONMENT_FAILURES = {
    "cli_missing", "runtime_missing", "engine_missing", "model_unavailable",
    "model_inventory_unavailable", "model_mismatch", "mode_mismatch", "effort_mismatch",
    "cleanup_failed",
}


def plan_batch(tools=None, baseline=None) -> list[dict]:
    tools, baseline = list(tools or []), list(baseline or [])
    if not tools and not baseline:
        raise IntegrationError("empty_batch", "Specify at least one --tools or --baseline session ID")
    if len(tools) > 3 or len(baseline) > 3:
        raise IntegrationError("batch_limit", "At most three named sessions per arm are allowed")
    base = ROOT.resolve() / "sessions"
    if base.resolve() != base:
        raise IntegrationError("invalid_session_path", "The sessions directory must not redirect outside the workspace")
    seen, plan = set(), []
    for mode, names in (("tools", tools), ("baseline", baseline)):
        for name in names:
            identifier(name)
            if name.casefold() in seen:
                raise IntegrationError("duplicate_session", "Every session ID must be unique across both arms")
            seen.add(name.casefold())
            path = base / name
            if os.path.lexists(path):
                raise IntegrationError("session_exists", f"Refusing to reuse or overwrite existing session: {path}")
            plan.append({"session_id": name, "mode": mode, "session_dir": str(path)})
    return plan


def _record_existing_evidence(item: dict) -> None:
    session = Path(item["session_dir"])
    launch_path = session / "kiro/launch.json"
    item["launch_path"] = str(launch_path) if launch_path.is_file() else None
    if launch_path.is_file() and "launch" not in item:
        try:
            item["launch"] = read_json(launch_path)
        except (OSError, ValueError) as exc:
            item["launch_read_error"] = str(exc)
    state_path = session / "session.json"
    if state_path.is_file():
        try:
            state = read_json(state_path)
            item["experiment"] = {"status": state.get("status"),
                                  "selected_candidate_id": state.get("selected_candidate_id"),
                                  "final": state.get("final")}
        except (OSError, ValueError) as exc:
            item["session_read_error"] = str(exc)


def run_batch(tools=None, baseline=None, *, execute=False, experiment_id='legacy-jump-v1') -> dict:
    # Validate the complete cohort before creating a session or starting a
    # preparation/import/model process. A conflict in the last slot stops all.
    if experiment_id not in {'legacy-jump-v1', 'ground-motion-v1'}:
        raise IntegrationError('unsupported_experiment', 'Unknown benchmark experiment')
    if experiment_id == 'ground-motion-v1' and baseline:
        raise IntegrationError('invalid_mode', 'Ground-motion has no baseline arm in this phase')
    plan = plan_batch(tools, baseline)
    if not execute:
        return {"status": "dry_run", "model": MODEL, "effort": EFFORT, "experiment_id": experiment_id,
                "max_seconds_per_session": MAX_SECONDS, "sessions": plan,
                "note": "No sessions or processes started. Add --run to authorize real model calls."}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    summary_path = ROOT / "artifacts" / f"benchmark-batch-{stamp}.json"
    summary = {"schema_version": 1, "status": "running", "started_at": utc_now(), "experiment_id": experiment_id,
               "model": MODEL, "effort": EFFORT, "max_seconds_per_session": MAX_SECONDS,
               "summary_path": str(summary_path),
               "sessions": [{**item, "status": "pending"} for item in plan],
               "note": "Batch completion is not experiment success. Read each session's recorded final results."}
    write_json(summary_path, summary)
    cancel = threading.Event()
    phase = {"value": "idle"}
    previous_handler = None

    def interrupt(_signum, _frame):
        cancel.set()
        # The launcher polls this Event and owns all CLI descendants. During
        # preparation, KeyboardInterrupt runs the import runner's own finally.
        if phase["value"] != "launch":
            raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.signal(signal.SIGINT, interrupt)
    try:
        for index, item in enumerate(summary["sessions"], 1):
            if cancel.is_set():
                summary["status"] = "interrupted"
                break
            item["started_at"] = utc_now()
            item["status"] = "preparing"
            phase["value"] = "prepare"
            write_json(summary_path, summary)
            print(f"[{index}/{len(plan)}] {item['mode']} {item['session_id']}: preparing", flush=True)
            try:
                extra = {'experiment_id': experiment_id} if experiment_id != 'legacy-jump-v1' else {}
                prepared = prepare_session(item["session_id"], mode=item["mode"], **extra)
                if Path(prepared).resolve() != Path(item["session_dir"]):
                    raise IntegrationError("invalid_session_path", "Preparation returned an unexpected session directory")
                if cancel.is_set():
                    raise KeyboardInterrupt
                phase["value"] = "launch"
                item["status"] = "running"
                write_json(summary_path, summary)
                print(f"[{index}/{len(plan)}] {item['mode']} {item['session_id']}: launching {MODEL}", flush=True)
                result = run_kiro(prepared, mode=item["mode"], timeout=MAX_SECONDS, cancel=cancel)
                item["launch"] = result
                item["status"] = result.get("status", "launch_failed")
                if cancel.is_set():
                    summary["status"] = "interrupted"
                elif item["status"] in ENVIRONMENT_FAILURES:
                    summary.update(status="stopped", stop_reason=item["status"])
            except KeyboardInterrupt:
                cancel.set()
                item.update(status="interrupted", error={"code": "cancelled", "message": "Operator interrupted the active session"})
                summary["status"] = "interrupted"
            except Exception as exc:
                code = getattr(exc, "code", "session_failed")
                item.update(status="failed", error={"code": code, "message": str(exc)})
                if code in ENVIRONMENT_FAILURES:
                    summary.update(status="stopped", stop_reason=code)
            finally:
                phase["value"] = "idle"
                item["finished_at"] = utc_now()
                _record_existing_evidence(item)
                write_json(summary_path, summary)
            print(f"[{index}/{len(plan)}] {item['session_id']}: {item['status']}", flush=True)
            if summary["status"] in {"interrupted", "stopped"}:
                break
        else:
            summary["status"] = "finished"
    except KeyboardInterrupt:
        cancel.set()
        summary["status"] = "interrupted"
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)
        for item in summary["sessions"]:
            if item["status"] == "pending":
                item.update(status="not_run", reason=summary["status"])
        summary["finished_at"] = utc_now()
        summary["attempted_count"] = sum("started_at" in item for item in summary["sessions"])
        write_json(summary_path, summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", nargs="+", default=[], metavar="ID", help="Fresh tools-arm session IDs (maximum three)")
    parser.add_argument("--baseline", nargs="+", default=[], metavar="ID", help="Fresh baseline-arm session IDs (maximum three)")
    parser.add_argument("--experiment", choices=('legacy-jump-v1','ground-motion-v1'), default='legacy-jump-v1')
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--run", action="store_true", help="Authorize sequential real Kiro runs; may consume model credits")
    execution.add_argument("--dry-run", action="store_true", help="Only validate and print the plan (default)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = run_batch(args.tools, args.baseline, execute=args.run, experiment_id=args.experiment)
    except (IntegrationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": {"code": getattr(exc, "code", "batch_error"),
                                                       "message": str(exc)}}, ensure_ascii=False))
        return 2
    if result["status"] == "dry_run":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({key: result[key] for key in ("status", "attempted_count", "summary_path")}, ensure_ascii=False))
    return 130 if result["status"] == "interrupted" else (1 if result["status"] == "stopped" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
