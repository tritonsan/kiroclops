"""Best-effort native-hook receipts, independent of the canonical audit lease.

This module intentionally imports only the standard library. New hooks enter
here before importing the (larger) launcher. Receipts are atomic visibility,
not fsync durability: a daemon writer never delays a decision for disk I/O.
A process killed before Python/this module starts, or before a blocked disk
write finishes, cannot be diagnosed reliably from absent receipts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import uuid

FLUSH_SECONDS = .1
MAX_EVENTS = 24
MAX_MESSAGE = 512
MAX_INPUT_BYTES = 1024 * 1024


def bounded_text(value, limit=MAX_MESSAGE):
    text = str(value)[:max(4096, limit)]
    text = re.sub(r'(?i)(bearer\s+)[^\s,;]+', r'\1[redacted]', text)
    text = re.sub(r'''(?i)((?:password|token|secret|api[_-]?key)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
                  r'\1[redacted]', text)
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    return text[:limit]


def error_record(exc):
    return {'type': bounded_text(type(exc).__name__, 80),
            'code': bounded_text(getattr(exc, 'code', 'guard_failure'), 80),
            'message': bounded_text(exc)}


def _write_receipt(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    try:
        with temporary.open('x', encoding='utf8', newline='\n') as stream:
            json.dump(record, stream, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class GuardDiagnostics:
    """One bounded queue and daemon per invocation, with immutable phase files."""

    def __init__(self, directory, *, origin='pre_tool_use', tool_call_id=None):
        self.root = Path(directory) / 'kiro/guard-diagnostics'
        self.invocation_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.sequence = 0
        self.pending = queue.Queue(maxsize=MAX_EVENTS)
        self.errors = []
        self.persisted = []
        self.dropped = 0
        self.closed = False
        self.identity = {'schema': 1, 'invocation_id': self.invocation_id,
                         'pid': os.getpid(), 'audit_origin': bounded_text(origin, 80)}
        if tool_call_id is not None:
            self.identity['tool_call_id'] = bounded_text(tool_call_id, 160)
        self.worker = threading.Thread(target=self._work, daemon=True,
                                       name='kiro-guard-diagnostics')
        self.worker.start()
        self.emit('start', outcome='started')

    def emit(self, phase, **details):
        if self.closed:
            return
        self.sequence += 1
        record = {**self.identity, 'sequence': self.sequence, 'phase': phase,
                  'at': time.time(), 'elapsed_seconds': time.monotonic() - self.started,
                  # An existing JSON file means its atomic replacement succeeded.
                  # There is deliberately no assertion of power-loss durability.
                  'diagnostic_persistence': {'status': 'atomic_file_visible', 'fsync': False},
                  **details}
        try:
            self.pending.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def identify_payload(self, payload):
        if isinstance(payload, dict):
            tool = payload.get('tool_name', payload.get('toolName', ''))
            if isinstance(tool, str):
                self.identity['tool'] = bounded_text(tool, 160)
            event_ids = {key: bounded_text(payload[key], 160)
                         for key in ('tool_use_id', 'tool_call_id', 'toolCallId')
                         if isinstance(payload.get(key), str)}
            if event_ids:
                self.identity['event_ids'] = event_ids
        # Hash only: never put a tool's command/file contents in diagnostics.
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             default=lambda _: '<non-json>').encode('utf8')
        self.identity['payload_sha256'] = hashlib.sha256(encoded).hexdigest()

    def _work(self):
        while True:
            record = self.pending.get()
            try:
                if record is None:
                    return
                name = f'{self.invocation_id}-{record["sequence"]:02d}-{record["phase"]}.json'
                _write_receipt(self.root / name, record)
                self.persisted.append(record['sequence'])
            except Exception as exc:
                self.errors.append(error_record(exc))
            finally:
                self.pending.task_done()

    def finish(self):
        if not self.closed:
            self.closed = True
            try:
                self.pending.put_nowait(None)
            except queue.Full:
                self.dropped += 1
            self.worker.join(FLUSH_SECONDS)
        return {'invocation_id': self.invocation_id,
                'status': 'pending' if self.worker.is_alive() else 'failed' if self.errors or self.dropped else 'written',
                'persisted_sequences': list(self.persisted), 'errors': list(self.errors),
                'dropped': self.dropped, 'flush_bound_seconds': FLUSH_SECONDS}


def run_hook(directory, *, guard_callable=None, diagnostics=None):
    """The same native JSON / exit-2 protocol, with pre-import evidence."""
    diagnostic = diagnostics or GuardDiagnostics(directory)
    try:
        diagnostic.emit('payload', outcome='reading')
        raw = sys.stdin.read(MAX_INPUT_BYTES + 1)
        diagnostic.identity['input_event_sha256'] = hashlib.sha256(raw.encode('utf8')).hexdigest()
        if len(raw.encode('utf8')) > MAX_INPUT_BYTES:
            raise ValueError('Native hook payload exceeds the 1 MiB bound')
        payload = json.loads(raw)
        diagnostic.identify_payload(payload)
        if guard_callable is None:
            diagnostic.emit('import_launcher', outcome='started')
            from .launcher import guard as guard_callable
            diagnostic.emit('import_launcher', outcome='completed')
        decision = guard_callable(directory, payload, audit_origin='pre_tool_use', _diagnostics=diagnostic)
        code = 0 if decision['allow'] else 2
        print(json.dumps(decision))
        if code:
            print(decision.get('reason') or 'Local trial operation denied.', file=sys.stderr)
        diagnostic.emit('return', outcome='allowed' if code == 0 else 'denied', exit_code=code)
        return code
    except Exception as exc:
        # No exception path can authorize a call. Preserve native denial semantics.
        error = error_record(exc)
        print(json.dumps({'status': 'error', 'code': error['code'], 'message': error['message']}))
        print(error['message'] or 'Local trial guard failed.', file=sys.stderr)
        diagnostic.emit('return', outcome='error_denied', exit_code=2, error=error)
        return 2
    finally:
        result = diagnostic.finish()
        if result['status'] != 'written':
            # A second channel reports unavailable diagnostics even if the disk
            # cannot record its own failure. It never changes the tool decision.
            print('Guard diagnostic persistence: ' + json.dumps(result), file=sys.stderr)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--guard', action='store_true')
    args = parser.parse_args()
    return run_hook(args.session)


if __name__ == '__main__':
    raise SystemExit(main())
