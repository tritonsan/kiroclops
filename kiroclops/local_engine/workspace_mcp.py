"""One KiroClops MCP server for the Kiro IDE that sets up its own session for the opened project.

Post-freeze addition (25 October 2026); no recorded result depends on it. The launcher
prepares a session and its Kiro controls before a CLI run. In the IDE the project is
already open, so this server adds one tool, ``start_session(workspace, request)``, and
then offers the same eight session tools as ``mcp.py``:

* the session is created from a snapshot of the opened project (its baseline) with the
  matching registered experiment, and the reference is the matching fixture reference;
* Kiro edits the opened project itself; before every evaluation only the files in the
  session's editable scope are copied into KiroClops's own sealed copy, so a change to a
  protected file can never enter a measurement (it is reported instead);
* nothing else is written into the opened project: exported evidence (frames, inspections,
  window strips) stays in the session's own copy, and the tools return its paths. The Kiro
  IDE snapshots files that appear in the project during a turn, and its snapshot of a folder
  created there by another process failed with a malformed path (25 October 2026).

Supported projects are copies of this repository's panel and SVG fixture applications, and
(post-freeze, 26 October 2026) a copy of the Godot game project at its ground stage: Kiro edits
``player.gd``, the reference is the prerecorded Tiny calibration set, and the original Heart project
(the jump stage's starting point) is the jump denominator that ground preservation measures against.
The CLI launcher's pre-tool-use guard and native audit do not run inside the IDE.

    python -m kiroclops.local_engine.workspace_mcp
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from kiroclops.common import ROOT
from kiroclops.visual.schema import VisualError
from .job_feedback import compact_inspection
from .session import Session, create_session, editable, source_hashes

MAX_WAIT_SECONDS = 25
DOMAINS = {'src/panel-motion.js': ('web_panel', 'panel'), 'src/layout-motion.js': ('svg', 'svg'),
           'player.gd': ('game', 'ground')}
FINISHED = {'finished', 'closed_failed', 'setup_failed'}
SCRATCH = 'kiroclops-scratch/'  # Kiro's own helper scripts: never measured, never reported as changes


def sessions_root():
    configured = os.environ.get('KIROCLOPS_SESSIONS')
    root = Path(configured) if configured else Path(os.environ.get('LOCALAPPDATA', Path.home()))/'KiroClops'/'sessions'
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def project_root(path):
    """The opened project folder, also when Kiro passes a folder inside it (such as its src folder)."""
    for candidate in [path, *path.parents][:3]:
        if any((candidate/entry).is_file() for entry in DOMAINS):
            return candidate
    return path


def detect_domain(workspace):
    """The project kind from its editable entry point: (session domain, fixture domain)."""
    found = [kinds for entry, kinds in DOMAINS.items() if (workspace/entry).is_file()]
    if len(found) != 1:
        raise VisualError('unsupported_project', 'KiroClops can set up the panel, SVG or Godot game demo application: the '
                          'opened folder needs exactly one of src/panel-motion.js, src/layout-motion.js or player.gd.')
    return found[0]


class WorkspaceSessions:
    """The session of the opened project, created on request and remembered per project folder."""

    def __init__(self, root=None):
        self.root = Path(root).resolve() if root else sessions_root()
        self.registry_path = self.root/'registry.json'
        self.workspace = None
        self.session = None

    def _registry(self):
        return json.loads(self.registry_path.read_text(encoding='utf-8')) if self.registry_path.exists() else {}

    def start(self, workspace, request):
        from kiroclops.web_local.specs import fixture_project, scenarios
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise VisualError('missing_workspace', 'Pass the absolute path of the opened project folder.')
        workspace = project_root(workspace)
        if workspace.is_relative_to(self.root) or self.root.is_relative_to(workspace):
            raise VisualError('unsafe_path', 'The project folder and the KiroClops session store must be separate.')
        domain, fixture = detect_domain(workspace)
        registry = self._registry()
        known = registry.get(str(workspace))
        if known and Path(known).is_dir() and Session(known).inspect().get('status') not in FINISHED:
            self._bind(workspace, known)
            return {'status': 'resumed', **self._summary()}
        directory = self.root/f"{fixture}-{time.strftime('%Y%m%d-%H%M%S')}"
        if domain == 'game':
            # The registered ground stage of the local game sessions (cohort.py), with this copy as its baseline.
            reference = ROOT/'config/phase2c-v2'
            experiment = {'stage': 'ground', 'original_baseline': str(self._game_original()), 'experiment_id': 'local-heart-ground'}
        else:
            reference = fixture_project(fixture, 'reference')
            experiment = {'calibration': scenarios(fixture, 'calibration'), 'validation': scenarios(fixture, 'validation')}
        create_session(directory, domain, workspace, reference, request, mode='tools', experiment=experiment)
        registry[str(workspace)] = str(directory)
        self.registry_path.write_text(json.dumps(registry, indent=1), encoding='utf-8')
        self._bind(workspace, directory)
        return {'status': 'created', **self._summary()}

    def _game_original(self):
        """The original Heart project: KIROCLOPS_GAME_ORIGINAL, or game/heart-original beside the session store."""
        configured = os.environ.get('KIROCLOPS_GAME_ORIGINAL')
        original = Path(configured) if configured else self.root.parent/'game'/'heart-original'
        if not (original/'player.gd').is_file():
            raise VisualError('missing_original_baseline', 'The ground stage needs the original Heart project '
                              '(set KIROCLOPS_GAME_ORIGINAL or place it in game/heart-original beside the session store).')
        return original.resolve()

    def _bind(self, workspace, directory):
        if self.session is not None:
            self.session.close()
        self.workspace, self.session = workspace, Session(directory)

    def _summary(self):
        value = self.session.inspect()
        return {'session': str(self.session.directory), 'workspace': str(self.workspace), 'domain': value.get('domain'),
                'editable_scope': value.get('editable_scope'), 'budget': value.get('budget'),
                'note': 'Edit the files of this workspace. Before each evaluation KiroClops copies the editable ones '
                        'into its own sealed copy; changes to any other file are not measured.'}

    def require(self):
        if self.session is None:
            raise VisualError('no_session', 'Call start_session(workspace, request) first.')
        return self.session

    @property
    def target(self):
        return Path(self.require().inspect()['target'])

    def sync(self):
        """Copy the editable files of the workspace into the session target; report other differences."""
        session = self.require()
        state = session.inspect()
        domain, target = state['domain'], Path(state['target'])
        current, copied = source_hashes(self.workspace), source_hashes(target)
        changed = []
        for relative, digest in current.items():
            if editable(domain, relative) and copied.get(relative) != digest:
                (target/relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.workspace/relative, target/relative)
                changed.append(relative)
        removed = [relative for relative in copied if editable(domain, relative) and relative not in current]
        for relative in removed:
            (target/relative).unlink()
        baseline = json.loads((session.directory/'session.json').read_text(encoding='utf-8'))['baseline_hashes']
        ignored = sorted(r for r in set(baseline) | set(current) if not editable(domain, r) and not r.startswith(SCRATCH)
                         and baseline.get(r) != current.get(r))
        return {'copied_into_measurement': sorted(changed), 'removed_from_measurement': sorted(removed),
                'not_measured_protected_changes': ignored}


def create_server(root=None):
    sessions = WorkspaceSessions(root)
    server = MCPServer('kiroclops', version='0.7.0', instructions=(
        'First call start_session with the absolute path of the opened project folder and the user request. '
        'Then inspect the session, prepare reference and baseline, and edit the project code yourself. '
        'Evaluate only calibration, seal the best eligible candidate, and report the recorded result. '
        'KiroClops measures and runs applications; it never writes the motion implementation.'))

    def guarded(function, *args):
        try:
            return function(*args)
        except VisualError as exc:
            return {'status': 'error', 'error': {'code': exc.code, 'message': str(exc)}}
        except OSError as exc:
            return {'status': 'error', 'error': {'code': getattr(exc, 'code', 'io_error'), 'message': str(exc), 'type': type(exc).__name__}}
        except (ValueError, KeyError) as exc:
            return {'status': 'error', 'error': {'code': getattr(exc, 'code', 'invalid_request'), 'message': str(exc), 'type': type(exc).__name__}}

    @server.tool()
    def start_session(workspace: str, request: str) -> dict[str, Any]:
        """Set up (or resume) the KiroClops session for the opened project folder; pass its absolute path and the user's request."""
        return guarded(sessions.start, workspace, request)

    @server.tool()
    def inspect_session() -> dict[str, Any]:
        """Inspect the current domain, permitted source edits and remaining attempt/time budget."""
        return guarded(lambda: compact_inspection(sessions.require().inspect(), sessions.target))

    @server.tool()
    def prepare_experiment() -> dict[str, Any]:
        """Prepare calibration reference and unchanged baseline; returns an asynchronous job ID."""
        return guarded(lambda: sessions.require().submit('prepare'))

    @server.tool()
    def evaluate_candidate(case_ids: list[str] | None = None) -> dict[str, Any]:
        """Copy the workspace's editable files into the measurement, then run calibration; every full attempt consumes a candidate. With case_ids, re-measure only those calibration cases as a partial probe that is never eligible for finalization."""
        def evaluate():
            synced = sessions.sync()
            result = sessions.require().submit('evaluate') if case_ids is None else sessions.require().submit('evaluate', None, case_ids)
            return {**result, 'workspace_sync': synced} if isinstance(result, dict) else result
        return guarded(evaluate)

    @server.tool()
    def inspect_window(job_id: str, case_id: str, object_id: str, window_id: str | None = None) -> dict[str, Any]:
        """Write reference, candidate and difference PNG strips plus an error-over-time plot for one measured object of a completed calibration evaluation into the session copy; returns the file paths and a numeric summary."""
        return guarded(lambda: sessions.require().inspect_window(job_id, case_id, object_id, window_id))

    @server.tool()
    def finalize_candidate(candidate_id: str | None = None) -> dict[str, Any]:
        """Seal the eligible candidate and verify it; registered trials reveal the held-out cases only now."""
        return guarded(lambda: sessions.require().submit('finalize', candidate_id))

    @server.tool()
    def get_job(job_id: str, wait_seconds: int = 10) -> dict[str, Any]:
        """Get durable job progress or result; optionally wait, at most 25 seconds per call."""
        bounded = min(max(0, wait_seconds), MAX_WAIT_SECONDS) if isinstance(wait_seconds, (int, float)) else wait_seconds
        return guarded(lambda: sessions.require().get_job_feedback(job_id, bounded))

    @server.tool()
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel the selected session job and only its owned application processes."""
        return guarded(lambda: sessions.require().cancel_job(job_id))

    @server.tool()
    def build_report() -> dict[str, Any]:
        """Build local media, source differences and separately stated numerical results."""
        return guarded(lambda: sessions.require().submit('report'))

    return server, sessions


def main():
    from .environment import configure_environment
    configure_environment()
    server, sessions = create_server()
    try:
        server.run(transport='stdio')
    finally:
        if sessions.session is not None:
            sessions.session.close()


if __name__ == '__main__':
    main()
