"""OpenCV 5 learned local features: ALIKED keypoints matched by LightGlue (V6 plan WP1.4).

The fixed-template tracker (``tracking.py``) measures with normalised
cross-correlation, which is exact for rigid, same-scale renders but blind to
scale and rotation and silent about its own confidence. OpenCV 5's ``features``
module ships learned local features that 4.x does not have: ``cv2.ALIKED``
(sub-pixel keypoints with 128-D descriptors) and ``cv2.LightGlueMatcher``
(context-aware matching). This module uses them for two things:

* ``locate`` finds a selected patch in a later frame through a robust
  similarity transform (``cv2.estimateAffinePartial2D``, RANSAC; the 5.0 wheel's
  partial-affine estimator accepts RANSAC/LMEDS only),
  which reports scale and rotation as well as position;
* the same call serves as an independent cross-check of the template tracker.

Model weights are not shipped in the repository. They are pinned to the exact
files OpenCV's own regression tests download (``opencv/opencv_extra``,
``testdata/dnn/download_models.py``) and fetched by
``scripts/fetch_opencv5_models.py`` into ``.tools/models`` (or
``KIROCLOPS_MODELS_DIR``). Nothing here runs without both files verified.

OpenCV 5.0.0 workarounds (evidence in ``reviews/phase4-v6/v6-results.md``):

* The LightGlue net keeps buffers sized by its first inference. A later call
  with more keypoints can end the process (0xC0000409, 0xC0000005), and
  results depend on earlier calls. Every matcher therefore starts with one
  inference at the largest possible shapes (``WARMUP_KEYPOINTS``, the ALIKED
  top-1k limit); after it, results depend only on the call itself.
* The matcher raises an integer division by zero when the network finds no
  match at all: over 1,617 keypoint-count pairs it never returned zero
  matches, and its 96 failures cluster where neighbouring pairs return one or
  two. ``locate`` reports such a frame as not located (``matcher_engine_failure``).

The classic DNN engine cannot load ALIKED, and ``OPENCV_FORCE_DNN_ENGINE`` is
read once per process. LightGlue therefore also runs in a helper process
(``IsolatedLightGlue``), so a failure that still ends a process costs only
that call; the next call starts a fresh, warmed-up helper.
"""
from __future__ import annotations

import hashlib
import math
import os
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

from .schema import VisualError

ROOT = Path(__file__).resolve().parents[2]
MODELS = {
    'aliked': {
        'file': 'aliked-n16rot-top1k-640.onnx',
        'sha1': '41faa7bf5d7eb68a2851471ba03aa20c9db30e4c',
        'url': 'https://raw.githubusercontent.com/YangGuanyuhan/lightglue_opencv_project/main/model/aliked-n16rot-top1k-640.onnx',
        'source': 'opencv/opencv_extra testdata/dnn/download_models.py: "ALIKED (ONNX)"'},
    'lightglue': {
        'file': 'aliked_lightglue.onnx',
        'sha1': '02723aa521990e57fe33d90b67977590c460e351',
        'url': 'https://raw.githubusercontent.com/YangGuanyuhan/lightglue_opencv_project/main/model/aliked_lightglue.onnx',
        'source': 'opencv/opencv_extra testdata/dnn/download_models.py: "ALIKED LightGlue (ONNX)"'},
}
DEFAULT_INPUT_SIZE = (640, 640)
MIN_MATCHES = 8
MIN_INLIER_RATIO = .5
REPROJECTION_PX = 2.
ENGINE_FAILURE = 'Unknown C++ exception from OpenCV code'
WARMUP_KEYPOINTS = 1000  # ALIKED 'top1k' returns at most this many keypoints per image


class MatcherEngineFailure(RuntimeError):
    """LightGlue failed inside OpenCV 5.0's DNN graph engine (see the module docstring)."""


def _lightglue_pairs(matcher, points_a, desc_a, size_a, points_b, desc_b, size_b):
    """Index pairs (a, b, distance) from one in-process LightGlue matcher; pair context is set per call."""
    matcher.setPairInfo(np.ascontiguousarray(points_a, np.float32), np.ascontiguousarray(points_b, np.float32),
                        (int(size_a[0]), int(size_a[1])), (int(size_b[0]), int(size_b[1])))
    try:
        matches = matcher.match(np.ascontiguousarray(desc_a, np.float32), np.ascontiguousarray(desc_b, np.float32))
    except cv2.error as error:
        if ENGINE_FAILURE not in str(error):
            raise
        raise MatcherEngineFailure(str(error)) from error
    finally:
        matcher.clearPairInfo()
    return [(int(m.queryIdx), int(m.trainIdx), float(m.distance)) for m in matches]


def _send(stream, value):
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(len(data).to_bytes(8, 'little'))
    stream.write(data)
    stream.flush()


def _receive(stream):
    head = stream.read(8)
    if len(head) < 8:
        return None
    size = int.from_bytes(head, 'little')
    data = stream.read(size)
    return pickle.loads(data) if len(data) == size else None


def _warm_up(matcher):
    """First inference at the largest shapes, so no later call outgrows the net's buffers."""
    rng = np.random.default_rng(0)
    points = rng.uniform(0, 640, (WARMUP_KEYPOINTS, 2)).astype(np.float32)
    descriptors = rng.normal(size=(WARMUP_KEYPOINTS, 128)).astype(np.float32)
    descriptors /= np.linalg.norm(descriptors, axis=1, keepdims=True)
    # Identical sets always match, so the warm-up itself cannot meet the empty-result failure.
    return _lightglue_pairs(matcher, points, descriptors, (640, 640), points, descriptors, (640, 640))


def _helper_main(model_path, score_threshold):
    """Helper process loop: length-prefixed pickles on stdin/stdout, one match per request."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    matcher = cv2.LightGlueMatcher_create(model_path, float(score_threshold))
    _warm_up(matcher)
    _send(stdout, ('ready', None))
    while (request := _receive(stdin)) is not None:
        try:
            reply = ('ok', _lightglue_pairs(matcher, *request))
        except MatcherEngineFailure as error:
            reply = ('engine_failure', str(error))
        except cv2.error as error:
            reply = ('error', str(error))
        _send(stdout, reply)


class IsolatedLightGlue:
    """LightGlue in a helper process, so the OpenCV 5.0 engine failure cannot end the measurement."""

    def __init__(self, model_path, score_threshold=0.):
        self.command = [sys.executable, '-B', '-m', 'kiroclops.visual.features5', '--lightglue-helper',
                        str(model_path), repr(float(score_threshold))]
        self.process, self.log, self.restarts = None, None, 0

    def _start(self):
        log = tempfile.TemporaryFile()
        process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, cwd=str(ROOT),
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        ready = _receive(process.stdout)
        if ready != ('ready', None):
            process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
            log.seek(0)
            detail = log.read()[-2000:].decode('utf-8', 'replace')
            log.close()
            raise VisualError('models_missing', 'The LightGlue helper process did not start: '+detail)
        self.process, self.log = process, log

    def match(self, points_a, desc_a, size_a, points_b, desc_b, size_b):
        if max(len(points_a), len(points_b)) > WARMUP_KEYPOINTS:
            raise VisualError('unsupported_input', f'LightGlue is warmed up for at most {WARMUP_KEYPOINTS} keypoints per image')
        if self.process is None:
            self._start()
        try:
            _send(self.process.stdin, (points_a, desc_a, size_a, points_b, desc_b, size_b))
            reply = _receive(self.process.stdout)
        except OSError:
            reply = None
        if reply is None:
            code = self.process.wait()
            self.close()
            self.restarts += 1
            raise MatcherEngineFailure(f'LightGlue helper process ended (exit code {code})')
        kind, value = reply
        if kind == 'engine_failure':
            raise MatcherEngineFailure(value)
        if kind == 'error':
            raise cv2.error(value)
        return value

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self.log.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass


def models_dir():
    configured = os.environ.get('KIROCLOPS_MODELS_DIR')
    return Path(configured) if configured else ROOT/'.tools'/'models'


def file_sha1(path):
    digest = hashlib.sha1()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def model_status(directory=None):
    """Presence and integrity of every pinned model file; never downloads."""
    directory = Path(directory) if directory else models_dir()
    status = {}
    for name, spec in MODELS.items():
        path = directory/spec['file']
        present = path.is_file()
        status[name] = {'path': str(path), 'present': present,
                        'sha1_ok': present and file_sha1(path) == spec['sha1'], 'expected_sha1': spec['sha1']}
    return status


def models_available(directory=None):
    return all(item['sha1_ok'] for item in model_status(directory).values())


def require_models(directory=None):
    status = model_status(directory)
    missing = [name for name, item in status.items() if not item['sha1_ok']]
    if missing:
        raise VisualError('models_missing',
                          'OpenCV 5 feature models are absent or unverified (' + ', '.join(missing) +
                          '); run scripts/fetch_opencv5_models.py or set KIROCLOPS_MODELS_DIR')
    return {name: Path(item['path']) for name, item in status.items()}


class FeatureEngine:
    """ALIKED detector plus LightGlue matcher (in its helper process) loaded once per measurement."""

    def __init__(self, directory=None, *, input_size=DEFAULT_INPUT_SIZE, score_threshold=0.):
        paths = require_models(directory)
        params = cv2.ALIKED_Params()
        params.inputSize = tuple(int(v) for v in input_size)
        self.detector = cv2.ALIKED_create(str(paths['aliked']), params)
        self.matcher = IsolatedLightGlue(paths['lightglue'], score_threshold)
        self.input_size = tuple(int(v) for v in input_size)
        self.model_sha1 = {name: MODELS[name]['sha1'] for name in MODELS}

    def describe(self, image):
        """Keypoints as an (N,2) float32 array plus (N,128) descriptors, or empty arrays."""
        keypoints, descriptors = self.detector.detectAndCompute(image, None)
        if not keypoints or descriptors is None:
            return np.zeros((0, 2), np.float32), np.zeros((0, 128), np.float32)
        points = np.float32([k.pt for k in keypoints]).reshape(-1, 2)
        return points, np.asarray(descriptors, np.float32)

    def match(self, points_a, desc_a, size_a, points_b, desc_b, size_b):
        """Index pairs (a, b, distance) from LightGlue; raises ``MatcherEngineFailure`` for the 5.0 engine failure."""
        if len(points_a) < 2 or len(points_b) < 2:
            return []
        return self.matcher.match(np.ascontiguousarray(points_a, np.float32), np.ascontiguousarray(desc_a, np.float32),
                                  (int(size_a[0]), int(size_a[1])), np.ascontiguousarray(points_b, np.float32),
                                  np.ascontiguousarray(desc_b, np.float32), (int(size_b[0]), int(size_b[1])))

    def close(self):
        self.matcher.close()


def _crop(image, left, top, width, height):
    height_px, width_px = image.shape[:2]
    left, top = max(0, int(left)), max(0, int(top))
    right, bottom = min(width_px, int(left+width)), min(height_px, int(top+height))
    return np.ascontiguousarray(image[top:bottom, left:right]), (left, top)


def locate(engine, template_image, roi, anchor, image, *, center=None, search_radius=None,
           min_matches=MIN_MATCHES, min_inlier_ratio=MIN_INLIER_RATIO, reprojection_px=REPROJECTION_PX):
    """Find the selected ROI of ``template_image`` inside ``image`` by a robust similarity.

    Keypoints come from a context window around the ROI (the patch alone is
    too small for a 640-px network input) and from a search window around
    ``center`` in the new frame; only template keypoints inside the ROI vote.
    The result is a measurement record in native pixels or an explicit reason.
    """
    x, y, w, h = (int(v) for v in roi)
    margin = max(32, w, h)
    template_crop, (tx0, ty0) = _crop(template_image, x-margin, y-margin, w+2*margin, h+2*margin)
    center = (x+anchor[0], y+anchor[1]) if center is None else center
    half = max(64, int((search_radius or max(w, h))+max(w, h)))
    frame_crop, (fx0, fy0) = _crop(image, center[0]-half, center[1]-half, 2*half, 2*half)
    result = {'valid': False, 'method': 'features_aliked_lightglue_v1', 'x': None, 'y': None, 'left': None, 'top': None,
              'width': None, 'height': None, 'scale': None, 'rotation_deg': None, 'matches': 0, 'inliers': 0,
              'inlier_ratio': None, 'template_keypoints': 0, 'frame_keypoints': 0, 'reason': None}
    if template_crop.size == 0 or frame_crop.size == 0:
        result['reason'] = 'empty_window'
        return result
    points_a, desc_a = engine.describe(template_crop)
    inside = np.zeros(len(points_a), bool)
    if len(points_a):
        gx, gy = points_a[:, 0]+tx0, points_a[:, 1]+ty0
        inside = (gx >= x) & (gx < x+w) & (gy >= y) & (gy < y+h)
    points_a, desc_a = points_a[inside], desc_a[inside]
    points_b, desc_b = engine.describe(frame_crop)
    result['template_keypoints'], result['frame_keypoints'] = int(len(points_a)), int(len(points_b))
    if len(points_a) < min_matches:
        result['reason'] = 'too_few_template_keypoints'
        return result
    try:
        pairs = engine.match(points_a, desc_a, template_crop.shape[1::-1], points_b, desc_b, frame_crop.shape[1::-1])
    except MatcherEngineFailure:
        result['reason'] = 'matcher_engine_failure'
        return result
    result['matches'] = len(pairs)
    if len(pairs) < min_matches:
        result['reason'] = 'too_few_matches'
        return result
    source = np.float32([[points_a[a][0]+tx0, points_a[a][1]+ty0] for a, _, _ in pairs])
    target = np.float32([[points_b[b][0]+fx0, points_b[b][1]+fy0] for _, b, _ in pairs])
    matrix, inliers = cv2.estimateAffinePartial2D(source, target, method=cv2.RANSAC,
                                                  ransacReprojThreshold=float(reprojection_px), maxIters=2000, confidence=.999)
    count = int(inliers.sum()) if inliers is not None else 0
    result['inliers'] = count
    result['inlier_ratio'] = count/len(pairs) if pairs else None
    if matrix is None or not np.isfinite(matrix).all() or count < min_matches or result['inlier_ratio'] < min_inlier_ratio:
        result['reason'] = 'no_consistent_similarity'
        return result
    corners = np.float32([[x, y, 1], [x+w, y, 1], [x+w, y+h, 1], [x, y+h, 1]])@matrix.T
    point = matrix@np.float64([x+anchor[0], y+anchor[1], 1.])
    lo, hi = corners.min(axis=0), corners.max(axis=0)
    result.update(valid=True, x=float(point[0]), y=float(point[1]), left=float(lo[0]), top=float(lo[1]),
                  width=float(hi[0]-lo[0]), height=float(hi[1]-lo[1]),
                  scale=float(math.hypot(matrix[0, 0], matrix[1, 0])),
                  rotation_deg=float(math.degrees(math.atan2(matrix[1, 0], matrix[0, 0]))),
                  transform=[[float(v) for v in row] for row in matrix])
    return result


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--lightglue-helper':
        raise SystemExit('usage: python -m kiroclops.visual.features5 --lightglue-helper MODEL SCORE_THRESHOLD')
    _helper_main(sys.argv[2], float(sys.argv[3]))
