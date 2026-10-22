"""Code and binary identities, independent of request and candidate identity."""
from __future__ import annotations

import hashlib
import platform
from pathlib import Path
import sys

from kiroclops.visual.schema import digest, file_sha256, VisualError


def analysis_sources(root=None):
    root = Path(root or Path(__file__).resolve().parents[2])
    # Full-analysis cache only. This closure deliberately does not claim a
    # per-stage dependency graph suitable for partial reuse.
    paths = sorted((root / 'kiroclops/visual').glob('*.py'))
    if not paths:
        raise VisualError('missing_analysis_sources', 'No visual analysis sources found')
    return {p.relative_to(root).as_posix(): file_sha256(p) for p in paths}


def service_sources(root=None):
    root = Path(root or Path(__file__).resolve().parents[2])
    sources = analysis_sources(root)
    for rel in ('kiroclops/__init__.py', 'kiroclops/local_engine/worker.py',
                'kiroclops/cloud/identity.py', 'kiroclops/cloud/results.py'):
        sources[rel] = file_sha256(root / rel)
    return dict(sorted(sources.items()))


def engine_identity(threads=None):
    import cv2
    import numpy as np
    if threads is not None:
        if type(threads) is not int or threads < 1:
            raise VisualError('invalid_threads', 'Use a positive explicit thread count')
        cv2.setNumThreads(threads)
    location = Path(cv2.__file__).resolve()
    libraries = sorted(p for p in location.parent.rglob('*')
                       if p.is_file() and (p.suffix.lower() in ('.pyd', '.dll', '.so') or '.so.' in p.name))
    if location.suffix.lower() in ('.pyd', '.so') and location not in libraries:
        libraries.append(location)
    if not libraries:
        raise VisualError('missing_engine_identity', 'Cannot identify the OpenCV native module')
    builds = cv2.getBuildInformation()
    # Record dependent shared libraries actually mapped in a Linux process,
    # including optimized libraries outside the Python wheel directory.
    mapped = {}
    maps = Path('/proc/self/maps')
    if maps.exists():
        for line in maps.read_text().splitlines():
            bits = line.split(maxsplit=5)
            if len(bits) == 6 and bits[5].startswith('/'):
                p = Path(bits[5])
                if p.is_file() and any(s in p.name.lower() for s in ('opencv', 'cool')):
                    mapped[p.name] = file_sha256(p)
    value = {'schema_version': 1, 'opencv_version': cv2.__version__,
             'numpy_version': np.__version__, 'python_version': platform.python_version(),
             'architecture': platform.machine(), 'system': platform.system(),
             'build_information_sha256': hashlib.sha256(builds.encode()).hexdigest(),
             'threads': cv2.getNumThreads(), 'opencl_enabled': bool(cv2.ocl.useOpenCL()),
             'optimized': bool(cv2.useOptimized()),
             'native_libraries': {p.relative_to(location.parent).as_posix(): file_sha256(p) for p in libraries},
             'mapped_libraries': mapped}
    return {'identity': value, 'engine_sha256': digest(value),
            'installation': {'python': sys.executable, 'cv2_module': str(location),
                             'native_library_paths': [str(p) for p in libraries]},
            'build_information': builds}


def computation_key(package_sha256, selection, contract, sources, engine):
    if not sources or not engine.get('engine_sha256') or digest(engine['identity']) != engine['engine_sha256']:
        raise VisualError('missing_engine_identity', 'Versioned code and engine identity are required')
    return digest({'schema_version': 2, 'operation': 'analyze', 'package_sha256': package_sha256,
                   'selection': selection, 'contract': contract, 'analysis_sources': sources,
                   'engine_sha256': engine['engine_sha256']})
