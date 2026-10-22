"""Eight Kiro tools sharing the local authoring-session implementation."""
from __future__ import annotations

import argparse
from typing import Any

from mcp.server import MCPServer
from kiroclops.visual.schema import VisualError
from .job_feedback import compact_inspection
from .session import Session

# The MCP client configured by the launcher times out a call after 30 s, so a
# get_job wait longer than this would surface as a transport failure.
MAX_WAIT_SECONDS = 25


def create_server(directory, *, reference_draft=False):
    if reference_draft:
        from .reference_draft import ReferenceDraft
        session=ReferenceDraft(directory)
    else:session = Session(directory)
    server = MCPServer("kiroclops", version="0.6.0", instructions=(
        "Inspect the selected domain, prepare reference and baseline, then edit real target code yourself. "
        "Evaluate only calibration, preserve protected behavior, and seal the best eligible candidate. "
        "A reference draft reports missing examples before authoring. A frozen multi-clip suite reveals held-out validation after sealing; a supplied single clip remains partial without unseen-input evidence. "
        "KiroClops measures and runs applications; it never writes the motion implementation."))

    def guarded(function, *args):
        try:
            return function(*args)
        except VisualError as exc:
            return {"status": "error", "error": {"code": exc.code, "message": str(exc)}}
        except OSError as exc:
            # V6 plan WP3.3: an I/O failure is not a malformed request.
            return {"status": "error", "error": {"code": getattr(exc, "code", "io_error"), "message": str(exc), "type": type(exc).__name__}}
        except (ValueError, KeyError) as exc:
            return {"status": "error", "error": {"code": getattr(exc, "code", "invalid_request"), "message": str(exc), "type": type(exc).__name__}}

    def inspect():
        # Kiro offloads outputs above ~30,000 characters outside the target and
        # shows only a preview, so a long inspection is shortened here and kept
        # complete inside the target workspace.
        value = session.inspect()
        target = value.get("target") if isinstance(value, dict) and not reference_draft else None
        return compact_inspection(value, target) if target else value

    @server.tool()
    def inspect_session() -> dict[str, Any]:
        """Inspect the current domain, permitted source edits and remaining attempt/time budget."""
        return guarded(inspect)

    @server.tool()
    def prepare_experiment() -> dict[str, Any]:
        """Prepare calibration reference and unchanged baseline; returns an asynchronous job ID."""
        return guarded(session.submit, "prepare")

    @server.tool()
    def evaluate_candidate(case_ids: list[str] | None = None) -> dict[str, Any]:
        """Freeze current target sources and run calibration; every full attempt consumes a candidate. With case_ids, re-measure only those calibration cases as a partial probe: faster, never eligible for finalization, at most three probes."""
        if case_ids is None or reference_draft:
            # A reference draft has no probes; it reports why it cannot evaluate.
            return guarded(session.submit, "evaluate")
        return guarded(session.submit, "evaluate", None, case_ids)

    def window_inspection(job_id, case_id, object_id, window_id):
        if not hasattr(session, "inspect_window"):
            raise VisualError("unsupported_operation", "A reference draft has no evaluation to inspect.")
        return session.inspect_window(job_id, case_id, object_id, window_id)

    if not reference_draft:
        # A reference draft has nothing to inspect and keeps the frozen seven tools
        # (kiroclops/local_engine/mcp_tools.py).
        @server.tool()
        def inspect_window(job_id: str, case_id: str, object_id: str, window_id: str | None = None) -> dict[str, Any]:
            """Write reference, candidate and difference PNG strips plus an error-over-time plot for one measured object of a completed calibration evaluation into .kiroclops-evidence/inspect; returns the file paths and a numeric summary."""
            return guarded(window_inspection, job_id, case_id, object_id, window_id)

    @server.tool()
    def finalize_candidate(candidate_id: str | None = None) -> dict[str, Any]:
        """Seal the eligible candidate and verify it. Registered trials reveal holdout; single-video sessions report partial calibration only."""
        return guarded(session.submit, "finalize", candidate_id)

    @server.tool()
    def get_job(job_id: str, wait_seconds: int = 10) -> dict[str, Any]:
        """Get durable job progress or result; optionally wait, at most 25 seconds per call."""
        bounded = min(max(0, wait_seconds), MAX_WAIT_SECONDS) if isinstance(wait_seconds, (int, float)) else wait_seconds
        return guarded(session.get_job_feedback, job_id, bounded)

    @server.tool()
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel the selected session job and only its owned application processes."""
        return guarded(session.cancel_job, job_id)

    @server.tool()
    def build_report() -> dict[str, Any]:
        """Build local media, source differences and separately stated numerical/manual-review results."""
        return guarded(session.submit, "report")

    return server, session


def main():
    from .environment import configure_environment
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    destination=parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--session")
    destination.add_argument("--reference-draft")
    args = parser.parse_args()
    server, session = create_server(args.reference_draft or args.session,reference_draft=bool(args.reference_draft))
    try:
        server.run(transport="stdio")
    finally:
        session.close()


if __name__ == "__main__":
    main()
