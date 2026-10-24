"""cv2.LightGlueMatcher (OpenCV 5.0.0): results depend on earlier calls, a later call with more
keypoints can end the process, and some inputs raise "Unknown C++ exception from OpenCV code".

Each sequence runs in a fresh process with one matcher. Inputs are seeded random keypoints and
L2-normalised 128-D descriptors (the shape ALIKED produces); image sizes 200x150 and 300x250.

    python lightglue_repro.py path/to/aliked_lightglue.onnx
"""
import subprocess
import sys

import cv2
import numpy as np

SEQUENCES = [
    ['8x8', '50x200'],             # small first call, then a larger one
    ['8x8', '1000x1000'],
    ['1000x1000', '8x8', '50x200'],  # same calls after a first call at the largest size
    ['8x8'],
    ['33x2', '8x8'],               # same 8x8 call after a different first call
    ['2x64'],
    ['1000x1000', '2x64'],
]


def keypoints(na, nb):
    rng = np.random.default_rng(na*100003+nb)
    da = rng.normal(size=(na, 128)).astype(np.float32)
    da /= np.linalg.norm(da, axis=1, keepdims=True)+1e-9
    db = rng.normal(size=(nb, 128)).astype(np.float32)
    db /= np.linalg.norm(db, axis=1, keepdims=True)+1e-9
    pa = rng.uniform(0, 200, size=(na, 2)).astype(np.float32)
    pb = rng.uniform(0, 300, size=(nb, 2)).astype(np.float32)
    return pa, da, pb, db


def run(model, steps):
    matcher = cv2.LightGlueMatcher_create(model, 0.0)
    for step in steps:
        na, nb = map(int, step.split('x'))
        pa, da, pb, db = keypoints(na, nb)
        matcher.setPairInfo(pa, pb, (200, 150), (300, 250))
        try:
            matches = matcher.match(da, db)
            print(f'  {step}: {len(matches)} matches, first distances {[repr(m.distance) for m in matches[:2]]}', flush=True)
        except cv2.error as error:
            print(f'  {step}: cv2.error: {str(error).strip()}', flush=True)
        finally:
            matcher.clearPairInfo()


def main():
    model = sys.argv[1]
    if len(sys.argv) > 2:
        run(model, sys.argv[2:])
        return
    print('OpenCV', cv2.__version__, '| NumPy', np.__version__, '| Python', sys.version.split()[0], '|', sys.platform, flush=True)
    for steps in SEQUENCES:
        result = subprocess.run([sys.executable, __file__, model, *steps], capture_output=True, text=True, timeout=600)
        print(' -> '.join(steps), f'| exit code {result.returncode} ({result.returncode & 0xffffffff:#x})', flush=True)
        print(result.stdout.rstrip(), flush=True)
        if result.returncode and result.stderr.strip():
            print('  stderr:', result.stderr.strip()[-400:], flush=True)


if __name__ == '__main__':
    main()
