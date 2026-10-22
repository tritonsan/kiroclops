"""The existing seven MCP tool contracts bound to a portable session."""
from __future__ import annotations
import argparse
from typing import Any
from mcp.server import MCPServer
from kiroclops.integration.storage import IntegrationError
from .session import Session


def create_server(directory):
    session = Session(directory)
    server = MCPServer("kiroclops", version="0.4.0", instructions=(
        "Measure pixels; author the real Heart controller yourself; evaluate calibration; "
        "seal the best eligible immutable candidate once, then validate. No tool writes motion code."))
    def guarded(function, *args):
        try:
            return function(*args)
        except (IntegrationError, OSError, ValueError) as exc:
            return {"status": "error", "error": {"code": getattr(exc, "code", "invalid_request"), "message": str(exc)}}

    @server.tool()
    def inspect_session() -> dict[str, Any]:
        """Inspect the current stage, permitted scope and remaining budget."""
        return guarded(session.inspect)

    @server.tool()
    def prepare_experiment() -> dict[str, Any]:
        """Prepare calibration reference/profile and unchanged target recordings; returns a job ID."""
        return guarded(session.submit, "prepare")

    @server.tool()
    def evaluate_candidate() -> dict[str, Any]:
        """Snapshot your source and evaluate calibration and preservation; consumes one of three attempts."""
        return guarded(session.submit, "evaluate")

    @server.tool()
    def finalize_candidate(candidate_id: str | None = None) -> dict[str, Any]:
        """Seal the eligible candidate with lowest calibration error and reveal held-out validation."""
        return guarded(session.submit, "finalize", candidate_id)

    @server.tool()
    def get_job(job_id: str, wait_seconds: int = 10) -> dict[str, Any]:
        """Wait up to twenty seconds for owned progress or a terminal result."""
        return guarded(session.get_job, job_id, wait_seconds)

    @server.tool()
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel this session's job and its owned game processes."""
        return guarded(session.cancel_job, job_id)

    @server.tool()
    def build_report() -> dict[str, Any]:
        """Package current-stage evidence and separate numerical/independent-review statuses."""
        return guarded(session.submit, "report")

    return server, session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    server, session = create_server(args.session)
    try:
        server.run(transport="stdio")
    finally:
        session.close()


if __name__ == "__main__":
    main()
