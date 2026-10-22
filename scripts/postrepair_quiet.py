"""Wait until no other capture-heavy workload runs on this machine.

Used by the post-repair completion runners before each measured batch. Timing
gates in both recorders fail closed, so a concurrent browser, Godot or session
worker from another tool turns a measurement into an infrastructure failure
(and would disturb that tool's own measurement). Nothing is stopped here; the
runner only waits. Imports nothing from kiroclops.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

QUIET_POLICY = {'interval_s': 30, 'consecutive_clear_checks': 2, 'maximum_wait_s': 4*3600}
# (field, pattern) pairs; a process outside this runner's own tree matching any is a foreign workload.
HEAVY_PATTERNS = (
    ('name', r'^godot'),                                               # Godot editor or native capture
    ('path', r'[\\/]ms-playwright[\\/]'),                               # Playwright-owned Chromium
    ('command', r'\brun-driver\b'),                                     # Playwright driver
    ('command', r'kiroclops\.local_engine\.(launcher|session\b.*--worker)'),  # session launchers and job workers
    ('command', r'scripts[./\\]phase3[b-z]_'),                          # Phase 3B/3C batch runners (3C seen 2026-10-01)
    ('command', r'\bpytest\b|-m unittest\b'),                           # test suites (browser/Godot tests)
)
_COMPILED = tuple((field, re.compile(pattern, re.I)) for field, pattern in HEAVY_PATTERNS)


def foreign_workloads():
    script = ('Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,'
              'CommandLine | ConvertTo-Json -Compress')
    done = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True,
                          text=True, encoding='utf-8', errors='replace', timeout=180,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    rows = json.loads(done.stdout)
    parents = {row['ProcessId']: row['ParentProcessId'] for row in rows}

    def own(pid):
        seen = set()
        while pid and pid not in seen:
            if pid == os.getpid():
                return True
            seen.add(pid)
            pid = parents.get(pid)
        return False

    found = []
    for row in rows:
        fields = {'name': row.get('Name') or '', 'path': row.get('ExecutablePath') or '',
                  'command': row.get('CommandLine') or ''}
        if any(pattern.search(fields[field]) for field, pattern in _COMPILED) and not own(row['ProcessId']):
            found.append({'pid': row['ProcessId'], 'name': fields['name'],
                          'command': re.sub(r'\s+', ' ', fields['command'])[:240]})
    return found


class ConcurrencyMonitor:
    """Record foreign capture workloads seen while a long measured stage runs; never stops anything.

    Processes whose command line names one of ``own_paths`` belong to the stage
    even when an intermediate parent has exited.
    """

    def __init__(self, own_paths, interval_s=60):
        import threading
        self._own = [str(path).lower() for path in own_paths]
        self._interval = interval_s
        self._stop = threading.Event()
        self._sightings, self._checks, self._errors = {}, 0, []
        self._started = time.time()
        self._thread = threading.Thread(target=self._run, name='postrepair-concurrency', daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                found = foreign_workloads()
            except Exception as exc:  # A failed probe is recorded, never fatal.
                self._errors.append(f'{type(exc).__name__}: {exc}')
                continue
            self._checks += 1
            for item in found:
                if any(path in item['command'].lower() for path in self._own):
                    continue
                key = (item['pid'], item['command'])
                entry = self._sightings.setdefault(key, {**item, 'first_seen': time.time(), 'checks_seen': 0})
                entry['last_seen'] = time.time()
                entry['checks_seen'] += 1

    def close(self):
        self._stop.set()
        self._thread.join(timeout=300)
        return {'interval_s': self._interval, 'started_at': self._started, 'stopped_at': time.time(),
                'checks': self._checks, 'probe_errors': self._errors[-5:],
                'foreign_workloads_seen': list(self._sightings.values()),
                'clean': not self._sightings and not self._errors}


def quiet_gate(label, policy=QUIET_POLICY):
    """Block until two consecutive checks find no foreign workload; returns the receipt."""
    started, clear, checks, seen = time.time(), 0, 0, []
    while True:
        found = foreign_workloads()
        checks += 1
        if found:
            clear = 0
            seen = found
            if checks == 1 or checks % 10 == 0:
                names = ', '.join(f"{item['pid']} {item['name']}" for item in found[:4])
                print(f'{label}: waiting for {len(found)} foreign capture workload(s): {names}', flush=True)
        else:
            clear += 1
            if clear >= policy['consecutive_clear_checks']:
                return {'policy': dict(policy), 'patterns': [list(p) for p in HEAVY_PATTERNS], 'checks': checks,
                        'waited_s': round(time.time()-started, 1), 'last_foreign_seen': seen, 'clear_at': time.time()}
        if time.time()-started > policy['maximum_wait_s']:
            raise TimeoutError(f'{label}: foreign capture workloads still running after {policy["maximum_wait_s"]} s')
        time.sleep(policy['interval_s'])


if __name__ == '__main__':
    print(json.dumps(foreign_workloads(), indent=1))
