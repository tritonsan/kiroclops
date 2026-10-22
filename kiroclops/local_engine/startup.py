"""Bounded MCP readiness checks and recovery before any model prompt."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from kiroclops.integration.kiro import KiroError
from .mcp_tools import ACTIVE_MCP_TOOLS
from kiroclops.visual.schema import write_json


def probe_stdio(server, output, cwd, *, timeout=30):
    """Initialize/list tools only; never execute a trial or call a model."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    messages = queue.Queue()
    with (output / 'stderr.log').open('w', encoding='utf-8') as errors:
        child = subprocess.Popen([server['command'], *server['args']], cwd=cwd,
            env={**os.environ, **server.get('env', {})}, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def drain():
            for line in child.stdout:
                messages.put(line)
            messages.put(None)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        def send(value):
            child.stdin.write(json.dumps({'jsonrpc':'2.0', **value})+'\n')
            child.stdin.flush()
        def receive(identity):
            deadline = started + timeout
            while time.monotonic() < deadline:
                try:
                    line = messages.get(timeout=max(.01, deadline-time.monotonic()))
                except queue.Empty:
                    raise KiroError('mcp_startup_failed', 'Direct MCP initialization timed out')
                if line is None:
                    raise KiroError('mcp_startup_failed', 'MCP exited during initialization')
                event = json.loads(line)
                if event.get('id') == identity:
                    if 'error' in event:
                        raise KiroError('mcp_startup_failed', 'MCP initialization returned a protocol error')
                    return event['result']
            raise KiroError('mcp_startup_failed', 'Direct MCP initialization timed out')
        result = {'prompt_sent':False, 'trial_tools_called':False}
        try:
            send({'id':1, 'method':'initialize', 'params':{'protocolVersion':'2025-11-25',
                'capabilities':{}, 'clientInfo':{'name':'kiroclops-startup-check', 'version':'1'}}})
            receive(1)
            send({'method':'notifications/initialized'})
            send({'id':2, 'method':'tools/list', 'params':{}})
            tools = sorted(t['name'] for t in receive(2).get('tools', []))
            if tools != sorted(ACTIVE_MCP_TOOLS):
                raise KiroError('unexpected_tools', 'Direct MCP registry differs from the expected session tools')
            result.update(status='pass', tool_names=tools)
        except BaseException as exc:
            result.update(status='fail', code=getattr(exc, 'code', type(exc).__name__))
            raise
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
            reader.join(timeout=2)
            child.stdout.close()
            result.update(seconds=time.monotonic()-started, process_exit_verified=child.poll() is not None)
            write_json(output / 'result.json', result)
    return result


def connect_native(ready, log_dir, client_factory, *, max_attempts=2):
    """Retry transport startup only. Never retry a prompt, model, or candidate."""
    log_dir = Path(log_dir).resolve()
    if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
        raise ValueError('At most two startup attempts are permitted')
    journal = log_dir.parent / (log_dir.name+'-startup-attempts.json')
    attempts = []
    for number in range(1, max_attempts+1):
        started = time.monotonic()
        client = None
        try:
            client = client_factory(ready, log_dir, 120, None)
            handshake = client.handshake()
            if client.prompt_sent or client.guard.generation_started:
                raise KiroError('premature_generation', 'Startup must not generate model output')
            attempts.append({'attempt':number, 'status':'pass', 'seconds':time.monotonic()-started,
                             'prompt_sent':False, 'log_dir':str(log_dir)})
            write_json(journal, {'attempts':attempts})
            return client, handshake, attempts
        except BaseException as exc:
            prompted = bool(client and (client.prompt_sent or client.guard.generation_started))
            errors = client.close() if client else []
            code = getattr(exc, 'code', type(exc).__name__)
            retry = (isinstance(exc, KiroError) and code in ('mcp_startup_failed', 'timeout', 'cli_failed')
                     and not prompted and not errors and number < max_attempts)
            archive = log_dir.parent / f'{log_dir.name}-startup-failed-{number:02}'
            if log_dir.exists():
                # Both resolved paths are explicit children of this session's
                # log directory. Preserve the whole failed connection journal.
                if archive.parent != log_dir.parent or archive.exists():
                    raise KiroError('startup_archive_conflict', 'Cannot preserve failed startup journal') from exc
                log_dir.rename(archive)
            attempts.append({'attempt':number, 'status':'fail', 'code':code,
                'seconds':time.monotonic()-started, 'prompt_sent':prompted,
                'cleanup_errors':errors, 'retry':retry, 'log_dir':str(archive)})
            write_json(journal, {'attempts':attempts})
            if not retry:
                raise
    raise AssertionError('Unreachable startup state')
