"""Host quietness sampling and capture-process priority (V6 plan WP2.2).

Every browser capture command carries a fixed 120 ms deadline and a single
miss fails the whole recording. Between 13 and 15 October four long captures
died on isolated host stalls (183-268 ms) while the machine was otherwise idle.
This module adds two cheap, fully recorded mitigations:

* a one-second scheduling sample before each capture that refuses to start a
  recording on a host that is already stalling (the candidate is not spent);
* an elevated priority class for the capture process and its Chromium
  children (browser, GPU and renderer processes) during the owned capture,
  restored afterwards.

Neither changes a frame-timing threshold: a capture that still misses a
deadline still fails, and both receipts are written next to the capture.
"""
from __future__ import annotations

import ctypes
import os
import statistics
import time

SAMPLE_S = 1.0
TICK_S = .016
SPIKE_S = .1
MAX_SPIKES = 0

PRIORITY_CLASSES = {'normal': 0x20, 'above_normal': 0x8000, 'high': 0x80}
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
CHROMIUM_NAMES = ('chrome', 'chromium', 'headless')


def host_quiet_report(seconds=SAMPLE_S, tick_s=TICK_S, spike_s=SPIKE_S):
    """Sample scheduler overshoot for ``seconds``; a stall shows as a long sleep."""
    overshoots = []
    deadline = time.perf_counter()+seconds
    while True:
        due = time.perf_counter()+tick_s
        time.sleep(tick_s)
        overshoots.append(max(0., time.perf_counter()-due))
        if time.perf_counter() >= deadline:
            break
    ranked = sorted(overshoots)
    p99 = ranked[min(len(ranked)-1, int(len(ranked)*.99))]
    return {'kind': 'HostQuietSample', 'schema_version': 1, 'seconds': seconds, 'tick_s': tick_s,
            'samples': len(overshoots), 'max_overshoot_s': max(overshoots), 'p99_overshoot_s': p99,
            'median_overshoot_s': statistics.median(overshoots), 'spike_s': spike_s,
            'spikes': sum(o >= spike_s for o in overshoots)}


def quiet_enough(report, max_spikes=MAX_SPIKES):
    return isinstance(report, dict) and report.get('spikes', max_spikes+1) <= max_spikes


def _kernel():
    if os.name != 'nt':
        return None
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel.GetPriorityClass.restype = ctypes.c_uint32
    kernel.GetPriorityClass.argtypes = (ctypes.c_void_p,)
    kernel.SetPriorityClass.restype = ctypes.c_int
    kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.CreateToolhelp32Snapshot.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
    return kernel


class _ProcessEntry(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_uint32), ('cntUsage', ctypes.c_uint32), ('th32ProcessID', ctypes.c_uint32),
                ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', ctypes.c_uint32), ('cntThreads', ctypes.c_uint32),
                ('th32ParentProcessID', ctypes.c_uint32), ('pcPriClassBase', ctypes.c_long), ('dwFlags', ctypes.c_uint32),
                ('szExeFile', ctypes.c_wchar*260)]


def process_table():
    """(pid, parent pid, executable name) for every process, Windows only."""
    kernel = _kernel()
    if kernel is None:
        return []
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == INVALID_HANDLE_VALUE:
        return []
    rows = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        kernel.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry))
        kernel.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry))
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return rows


def descendant_pids(root_pid, names=CHROMIUM_NAMES, table=None):
    """Transitive children of ``root_pid`` whose executable name contains one of ``names``."""
    rows = process_table() if table is None else table
    children = {}
    for pid, parent, exe in rows:
        children.setdefault(parent, []).append((pid, exe))
    found, queue = [], [root_pid]
    seen = {root_pid}
    while queue:
        current = queue.pop()
        for pid, exe in children.get(current, []):
            if pid in seen:
                continue
            seen.add(pid)
            queue.append(pid)
            if any(name in exe.lower() for name in names):
                found.append((pid, exe))
    return found


def set_priority_class(pid, level):
    kernel = _kernel()
    if kernel is None:
        return {'pid': pid, 'status': 'unsupported'}
    wanted = PRIORITY_CLASSES[level]
    handle = kernel.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, 0, pid)
    if not handle:
        return {'pid': pid, 'status': 'open_failed', 'error': ctypes.get_last_error()}
    try:
        previous = kernel.GetPriorityClass(handle)
        ok = bool(kernel.SetPriorityClass(handle, wanted))
        return {'pid': pid, 'status': 'set' if ok else 'set_failed', 'previous_class': previous, 'class': wanted}
    finally:
        kernel.CloseHandle(handle)


def raise_capture_priority(level='above_normal', names=CHROMIUM_NAMES):
    """Raise this process and its Chromium descendants; return a restorable receipt."""
    if level not in PRIORITY_CLASSES:
        raise ValueError('Unknown priority level')
    if os.name != 'nt':
        return {'kind': 'CapturePriority', 'status': 'unsupported', 'level': level, 'processes': []}
    processes = [set_priority_class(os.getpid(), level)]
    for pid, exe in descendant_pids(os.getpid(), names):
        processes.append({**set_priority_class(pid, level), 'exe': exe})
    return {'kind': 'CapturePriority', 'status': 'applied', 'level': level, 'processes': processes}


def restore_priorities(receipt):
    """Put every process the receipt changed back to its previous class."""
    kernel = _kernel()
    if kernel is None or not isinstance(receipt, dict):
        return []
    restored = []
    for item in receipt.get('processes', []):
        if item.get('status') != 'set' or not item.get('previous_class'):
            continue
        handle = kernel.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, 0, item['pid'])
        if not handle:
            restored.append({'pid': item['pid'], 'status': 'gone'})
            continue
        try:
            ok = bool(kernel.SetPriorityClass(handle, item['previous_class']))
            restored.append({'pid': item['pid'], 'status': 'restored' if ok else 'restore_failed'})
        finally:
            kernel.CloseHandle(handle)
    return restored
