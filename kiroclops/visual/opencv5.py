"""OpenCV 5 runtime gate and capability inventory for measurement outputs.

KiroClops measures with OpenCV 5. The wheel pinned in ``requirements-local.lock``
(``opencv-python==5.0.0.93``) builds the 5.x module set (``calib core dnn
features flann geometry ... ptcloud stereo``); the ``features`` module carries
the learned local features (``ALIKED``, ``DISK``) and ``LightGlueMatcher`` that
``kiroclops.visual.features5`` uses, and none of those exist in 4.x. The gate
below refuses a 4.x runtime instead of silently measuring with different code,
and every ObservationSet records the runtime it was produced with.

The symbol inventory was taken by diffing ``dir(cv2)`` between 4.14.0 and 5.0.0
(``artifacts/phase4-v6/opencv-symbol-diff-01``).
"""
from __future__ import annotations

import re

import cv2

from .schema import VisualError

REQUIRED_MAJOR = 5
# Symbols this package relies on that the 5.0 wheel exposes and 4.14.0 does not.
OPENCV5_ONLY_SYMBOLS = ('ALIKED', 'LightGlueMatcher', 'DISK', 'ANNIndex', 'finiteMask')
# Classical symbols shared with 4.x that the tracker also uses (listed so the
# runtime record states what the measurement depends on, not to claim novelty).
SHARED_SYMBOLS = ('matchTemplate', 'minMaxLoc', 'integral', 'calcOpticalFlowPyrLK', 'goodFeaturesToTrack',
                  'estimateAffinePartial2D', 'USAC_MAGSAC', 'Canny', 'findContours', 'fitLine',
                  'connectedComponentsWithStats', 'phaseCorrelate')


def version_tuple():
    parts = re.findall(r'\d+', cv2.__version__)
    return tuple(int(p) for p in parts[:3])


def require_opencv5():
    """Return the OpenCV version string, or refuse a runtime that is not 5.x."""
    major = cv2.getVersionMajor() if hasattr(cv2, 'getVersionMajor') else version_tuple()[0]
    if major != REQUIRED_MAJOR:
        raise VisualError('unsupported_opencv',
                          f'OpenCV {REQUIRED_MAJOR}.x is required for measurement; found {cv2.__version__}')
    return cv2.__version__


def built_modules():
    """OpenCV modules compiled into the installed binary, from its build information."""
    info = cv2.getBuildInformation()
    match = re.search(r'To be built:\s+(.+)', info)
    return match.group(1).split() if match else []


def opencv_runtime():
    """Stable runtime record: version, wheel flags, built modules and 5-only symbols.

    Machine-dependent values (thread counts, CPU features, OpenCL devices) are
    deliberately excluded so the record is identical across replays.
    """
    wheel = {}
    version_module = getattr(cv2, 'version', None)
    for key in ('opencv_version', 'contrib', 'headless', 'rolling', 'ci_build'):
        value = getattr(version_module, key, None) if version_module is not None else None
        if isinstance(value, (str, bool, int)):
            wheel[key] = value
    return {
        'version': cv2.__version__,
        'major': version_tuple()[0] if version_tuple() else None,
        'wheel': wheel,
        'modules': built_modules(),
        'opencv5_symbols': {name: hasattr(cv2, name) for name in OPENCV5_ONLY_SYMBOLS},
        'shared_symbols_present': all(hasattr(cv2, name) for name in SHARED_SYMBOLS),
    }
