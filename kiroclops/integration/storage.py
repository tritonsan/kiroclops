"""Atomic session records, bounded paths and project fingerprints."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from kiroclops.common import ROOT
from .runner import _CACHE_DIRS, RunnerError, source_hashes

SCHEMA_VERSION = 2
IGNORED_DIRS = frozenset(_CACHE_DIRS)
PLAYER_PREFIX = 'source/characters/player/'


class IntegrationError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _retry_permission(operation):
    """Windows readers/scanners can briefly hold an atomic-update destination."""
    for attempt in range(20):
        try:
            return operation()
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.025)


def read_json(path):
    text = _retry_permission(lambda: Path(path).read_text(encoding='utf-8-sig'))
    return json.loads(text)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        _retry_permission(lambda: os.replace(temp, path))
    finally:
        _retry_permission(lambda: temp.unlink(missing_ok=True))
    return path


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}', value):
        raise IntegrationError('invalid_identifier', 'Expected a bounded session/job/candidate identifier')
    return value


def session_path(value):
    path = Path(value).resolve()
    base = (ROOT / 'sessions').resolve()
    if path.parent != base:
        raise IntegrationError('invalid_session_path', 'Session must be an immediate child of the project sessions directory')
    identifier(path.name)
    return path


def project_files(project, *, include_uid=False):
    project = Path(project).resolve()
    for path in sorted(project.rglob('*')):
        rel = path.relative_to(project)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        if path.is_symlink() or (hasattr(path.stat(), 'st_file_attributes') and path.stat().st_file_attributes & 0x400):
            raise IntegrationError('unsafe_path', f'Reparse points are not allowed in experiment sources: {rel}')
        if not path.is_file() or path.name.endswith(('.log', '.tmp')):
            continue
        # Godot creates UID sidecars for new helper scripts on import. The
        # actual scripts and their containing scenes are fingerprinted.
        if path.suffix == '.uid' and not include_uid:
            continue
        yield rel.as_posix(), path


def project_hashes(project):
    # Validate reparse points with the snapshot copier's stricter Windows
    # checks, then use the exact same fingerprint policy as capture manifests.
    # Copying still preserves .uid/.import sidecars for Godot; fingerprinting
    # excludes those generated files, logs and runtime/editor caches.
    for _ in project_files(project, include_uid=True):
        pass
    try:
        return source_hashes(project)
    except RunnerError as exc:
        raise IntegrationError(exc.code, str(exc)) from exc


def editable(relative):
    return relative.startswith(PLAYER_PREFIX) and relative.endswith('.gd') and '..' not in Path(relative).parts


def assert_allowed_changes(project, baseline_hashes):
    current = project_hashes(project)
    protected = [name for name in set(current) | set(baseline_hashes)
                 if current.get(name) != baseline_hashes.get(name) and not editable(name)]
    deleted = [name for name in baseline_hashes if name not in current]
    if protected or deleted:
        raise IntegrationError('experiment_integrity', f'Protected/deleted files changed: {sorted(set(protected + deleted))}')
    return current


def copy_project(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise IntegrationError('output_exists', f'Refusing to replace project snapshot: {destination}')
    # Enumerate and validate before copying so junctions cannot escape sources.
    files = list(project_files(source, include_uid=True))
    destination.mkdir(parents=True)
    for rel, path in files:
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return destination


@contextmanager
def file_lock(path, timeout=10):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    handle = None
    while True:
        try:
            handle = path.open('a+b')
            handle.seek(0)
            # Windows byte-range locks also deny reads of the locked byte.
            # Initialization must share the bounded acquisition retry path.
            if not handle.read(1):
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                handle = None
            if time.monotonic() - start >= timeout:
                raise IntegrationError('session_busy', 'Another process owns the session lock')
            time.sleep(.05)
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def process_identity(pid):
    """Return a live process's creation identity, excluding retained exit handles."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.restype = wintypes.HANDLE
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        # QUERY_LIMITED_INFORMATION | SYNCHRONIZE. A retained Popen handle can
        # keep an exited process queryable; creation time alone is not liveness.
        handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        try:
            if api.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT = still running
                return None
            if not api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            if api.WaitForSingleObject(handle, 0) != 258:
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            api.CloseHandle(handle)
    try:
        return Path(f'/proc/{pid}/stat').read_text().split()[21]
    except OSError:
        return None
