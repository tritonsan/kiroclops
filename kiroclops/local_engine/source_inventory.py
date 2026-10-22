"""Fresh full-byte inventories with one directory walk and no result cache.

Validate the root's ancestors once, then validate each encountered entry rather
than rechecking every ancestor for every PNG. Ignored directory entries are
checked for links/reparse points before pruning; their contents are excluded.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from kiroclops.visual.schema import VisualError


SOURCE_IGNORED = frozenset({'.git', '.godot', '.import', '__pycache__', '.kiro',
                          '.kiroclops-evidence', '.kiroclops-scratch'})
_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT = 0x400


def _reject_link(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE_POINT:
        raise VisualError('unsafe_path', 'Source inventories cannot use symbolic links or reparse points.')


def _identity(info):
    return info.st_dev, info.st_ino


def _content_state(info):
    return (_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _checked_root(value):
    absolute = Path(value).expanduser().absolute()
    ancestors = []
    for path in reversed((absolute, *absolute.parents)):
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise VisualError('invalid_project', 'Expected an existing project directory.') from None
        _reject_link(info)
        if not stat.S_ISDIR(info.st_mode):
            raise VisualError('invalid_project', 'Expected a project directory with ordinary ancestors.')
        ancestors.append((path, _identity(info)))
    # Ordinary ancestors ensure resolving cannot follow a link. Resolution is
    # done once for the root, never once per included file.
    return absolute.resolve(), ancestors


def _hash_regular(path, expected):
    """Hash every byte and reject replacement or modification during the read."""
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        _reject_link(before)
        if not stat.S_ISREG(before.st_mode) or _content_state(before) != _content_state(expected):
            raise VisualError('source_changed', 'Source file changed before its full byte read.')
        value = hashlib.sha256()
        for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b''):
            value.update(chunk)
        after = os.fstat(stream.fileno())
        if _content_state(after) != _content_state(before):
            raise VisualError('source_changed', 'Source file changed during its full byte read.')
    current = path.lstat()
    _reject_link(current)
    if not stat.S_ISREG(current.st_mode) or _content_state(current) != _content_state(before):
        raise VisualError('source_changed', 'Source file was replaced during its full byte read.')
    return value.hexdigest()


def full_byte_inventory(project, *, ignored=SOURCE_IGNORED, excluded_suffixes=('.pyc', '.pyo')):
    """Return the complete current relative-POSIX-name to SHA-256 map.

    The defaults have Session.source_hashes' included-file semantics. Pass
    ignored=(), excluded_suffixes=() for an inventory of every ordinary file.
    Each call rereads all included bytes, even if size and timestamps match a
    previous call. Links at excluded entries are rejected too. Excluded
    directory contents are deliberately neither enumerated nor claimed.
    """
    root, ancestors = _checked_root(project)
    ignored = frozenset(ignored)
    excluded_suffixes = tuple(excluded_suffixes)
    result = {}
    visited = []

    def walk(directory, relative, expected_identity):
        current = directory.lstat()
        _reject_link(current)
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != expected_identity:
            raise VisualError('source_changed', 'Source directory changed during inventory.')
        visited.append((directory, expected_identity))
        with os.scandir(directory) as stream:
            entries = sorted(stream, key=lambda item: item.name)
        folders = []
        for entry in entries:
            path = directory / entry.name
            # Windows DirEntry.stat uses cached find-data with st_ino/st_dev=0
            # on supported Python versions. One fresh lstat is required for
            # meaningful identity/replacement checks and reparse attributes.
            info = path.lstat()
            _reject_link(info)
            name = relative / entry.name
            if stat.S_ISDIR(info.st_mode):
                if entry.name not in ignored:
                    folders.append((path, name, _identity(info)))
            elif stat.S_ISREG(info.st_mode):
                if not entry.name.endswith(excluded_suffixes):
                    result[name.as_posix()] = _hash_regular(path, info)
            else:
                raise VisualError('unsafe_path', 'Source inventories require ordinary files and directories.')
        for path, name, identity in folders:
            walk(path, name, identity)

    walk(root, Path(), ancestors[-1][1])
    # Recheck owned directory identities once at the end. This catches an
    # ancestor replaced by a link during traversal without per-file ancestor
    # resolution. These are fresh stats, not persisted metadata cache entries.
    for path, identity in (*ancestors, *visited):
        current = path.lstat()
        _reject_link(current)
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != identity:
            raise VisualError('source_changed', 'Source directory was replaced during inventory.')
    return dict(sorted(result.items()))
