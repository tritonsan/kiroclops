"""Shared skip for tests that read local evidence (V6 plan WP4.3).

Evidence is what a fresh clone of the source tree does not carry: this
repository's ``artifacts/`` and ``reviews/`` folders and the frozen run folders
and code snapshots under ``C:/kc3d``. A test that reads such a path names it in
``requires_evidence``. It runs wherever every named path exists, and on a clean
clone it is skipped with the missing paths in the reason.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def evidence_path(path):
    """Absolute paths (``C:/kc3d/...``) as given; anything else relative to the repository."""
    path = Path(path)
    return path if path.is_absolute() else ROOT/path


def missing_evidence(*paths):
    return [str(path) for path in paths if not evidence_path(path).exists()]


def requires_evidence(*paths):
    """Skip the decorated test class or method unless every named evidence path exists."""
    absent = missing_evidence(*paths)
    return unittest.skipIf(bool(absent), 'Local evidence is not on this machine: '+', '.join(absent))


def skip_module_without(*paths):
    """For a module that reads evidence at import time: ``unittest`` discovery reports it as skipped."""
    absent = missing_evidence(*paths)
    if absent:
        raise unittest.SkipTest('Local evidence is not on this machine: '+', '.join(absent))
