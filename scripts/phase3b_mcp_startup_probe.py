"""Read-only stdio initialization/list-tools probe; no model or trial work."""
from pathlib import Path
import argparse
import json
import os
import queue
import subprocess
import threading
import time

from kiroclops.visual.schema import read_json, write_json, file_sha256


def run(session, output):
    session, output = Path(session).resolve(), Path(output)
    if output.exists():
        raise ValueError('Fresh diagnostic output required')
    config_path = session / 'target/.kiro/agents/kiroclops-local-tools.json'
    config = read_json(config_path)['mcpServers']['kiroclops']
    if read_json(session / 'session.json').get('active_job_id'):
        raise ValueError('Use an idle session; never disturb an active trial')
    output.mkdir(parents=True)
    results = []
    for repeat in range(3):
        started = time.monotonic()
        with (output / f'stderr-{repeat+1}.log').open('w', encoding='utf8') as errors:
            child = subprocess.Popen([config['command'], *config['args']],
                cwd=session / 'target', env={**os.environ, **config['env']},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding='utf8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            messages = queue.Queue()
            def reader():
                for line in child.stdout:
                    messages.put(line)
            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            def send(message):
                child.stdin.write(json.dumps({'jsonrpc': '2.0', **message}) + '\n')
                child.stdin.flush()
            def receive(identity, deadline):
                while time.monotonic() < deadline:
                    value = json.loads(messages.get(timeout=max(.01, deadline-time.monotonic())))
                    if value.get('id') == identity:
                        return value
                raise TimeoutError('Read-only handshake deadline')
            try:
                send({'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
                    'capabilities': {}, 'clientInfo': {'name': 'kiroclops-startup-diagnostic', 'version': '1'}}})
                initialized = receive(1, started+45)
                handshake = time.monotonic()-started
                send({'method': 'notifications/initialized'})
                send({'id': 2, 'method': 'tools/list', 'params': {}})
                tools = receive(2, started+50)
                names = sorted(t['name'] for t in tools.get('result', {}).get('tools', []))
                results.append({'repeat': repeat+1, 'handshake_seconds': handshake,
                                'list_seconds_from_start': time.monotonic()-started,
                                'server': initialized.get('result', {}).get('serverInfo'),
                                'tool_names': names, 'status': 'pass' if len(names)==7 else 'fail'})
            except Exception as exc:
                results.append({'repeat': repeat+1, 'status': 'fail',
                                'error_type': type(exc).__name__, 'elapsed_seconds': time.monotonic()-started})
            finally:
                child.stdin.close()
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.terminate(); child.wait(timeout=5)
                thread.join(timeout=2)
                child.stdout.close()
        write_json(output / 'result.json', {'config_sha256': file_sha256(config_path),
            'model_started': False, 'trial_tools_called': False, 'rows': results,
            'scope': 'Direct stdio handshake only; does not prove Kiro native MCP startup reliability.'})
        print(results[-1], flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--session', required=True)
    parser.add_argument('--output', required=True)
    args=parser.parse_args()
    run(args.session, args.output)
