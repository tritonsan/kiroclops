"""Compact read-only status for the three registered Heart v2 chains."""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


for directory in sorted((ROOT / "sessions").glob("heart-heart-v2-01-*-*")):
    state = read(directory / "session.json")
    jobs = [read(p) for p in sorted((directory / "jobs").glob("job-*.json"))]
    launch = read(directory / "kiro/launch.json")
    final = state.get("final") or {}
    row = {"session": directory.name, "status": state.get("status"),
           "launch": launch.get("status"), "candidates": len(state.get("candidates", [])),
           "selected": state.get("selected_candidate_id"),
           "numerical": final.get("numerical_status"),
           "integrity": final.get("automatic_integrity_status")}
    if state.get("started_at") and not state.get("sealed_at"):
        started = datetime.fromisoformat(state["started_at"].replace("Z", "+00:00"))
        row["elapsed_s"] = round((datetime.now(timezone.utc) - started).total_seconds())
    active = next((j for j in jobs if j.get("job_id") == state.get("active_job_id")), None)
    if active:
        row["job"] = {k: active.get(k) for k in ("job_id", "action", "status", "progress", "error")}
    row["candidate_results"] = [
        {"id": j.get("candidate_id"), "job": j.get("status"),
         "eligible": (j.get("result") or {}).get("eligible"),
         "error": j.get("error")}
        for j in jobs if j.get("action") == "evaluate"]
    print(json.dumps(row, ensure_ascii=False))
