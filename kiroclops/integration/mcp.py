"""Session-bound local stdio tools shared by Kiro IDE and CLI."""
from __future__ import annotations

import argparse
from typing import Any
from mcp.server import MCPServer

from .session import Session
from .storage import IntegrationError


def create_server(directory):
    session=Session(directory)
    server=MCPServer('kiroclops',version='0.2.0',
                     instructions='Measure pixels, let Kiro edit the real controller, evaluate calibration, then seal once for held-out validation. No tool writes motion code.')

    def guarded(function,*args,**kwargs):
        try:
            return function(*args,**kwargs)
        except (IntegrationError,OSError,ValueError) as exc:
            return {'status':'error','error':{'code':getattr(exc,'code','invalid_request'),'message':str(exc)}}

    @server.tool()
    def inspect_session() -> dict[str, Any]:
        """Inspect this session, readiness, allowed source scope and remaining budget."""
        return guarded(session.inspect)

    @server.tool()
    def prepare_experiment() -> dict[str, Any]:
        """Start fresh reference pixel analysis/profile and baseline captures; returns a job ID."""
        return guarded(session.submit,'prepare')

    @server.tool()
    def evaluate_candidate() -> dict[str, Any]:
        """Snapshot your controller, evaluate this session's locked calibration and preservation checks. Uses one of three attempts."""
        return guarded(session.submit,'evaluate')

    @server.tool()
    def finalize_candidate(candidate_id: str | None = None) -> dict[str, Any]:
        """Seal the best eligible calibration candidate and evaluate held-out motion. Further tuning is forbidden."""
        return guarded(session.submit,'finalize',candidate_id)

    @server.tool()
    def get_job(job_id: str, wait_seconds: int = 10) -> dict[str, Any]:
        """Wait up to 20 seconds for progress/completion. Repeat while status is queued/running."""
        return guarded(session.get_job,job_id,wait_seconds)

    @server.tool()
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel an owned job and its game processes; incomplete captures cannot become valid results."""
        return guarded(session.cancel_job,job_id)

    @server.tool()
    def build_report() -> dict[str, Any]:
        """Package a local HTML report with videos, measurements, attempt diffs and session history."""
        return guarded(session.submit,'report')

    return server,session


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',required=True)
    args=parser.parse_args()
    server,session=create_server(args.session)
    from .power_lifetime import start_launcher_watch
    stop = None
    try:
        stop = start_launcher_watch(session)
        server.run(transport='stdio')
    finally:
        if stop is not None:
            stop.set()
        session.close()


if __name__=='__main__':
    main()
