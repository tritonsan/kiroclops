import sys

import cv2
import numpy as np


def keypoints(na, nb):
    # Seeded random ALIKED-shaped input: L2-normalised 128-D descriptors, image sizes 200x150 and 300x250.
    rng = np.random.default_rng(na*100003 + nb)
    desc0 = rng.normal(size=(na, 128)).astype(np.float32)
    desc0 /= np.linalg.norm(desc0, axis=1, keepdims=True) + 1e-9
    desc1 = rng.normal(size=(nb, 128)).astype(np.float32)
    desc1 /= np.linalg.norm(desc1, axis=1, keepdims=True) + 1e-9
    kpts0 = rng.uniform(0, 200, size=(na, 2)).astype(np.float32)
    kpts1 = rng.uniform(0, 300, size=(nb, 2)).astype(np.float32)
    return kpts0, desc0, kpts1, desc1


matcher = cv2.LightGlueMatcher_create(sys.argv[1], 0.0)
for na, nb in ((8, 8), (50, 200)):
    kpts0, desc0, kpts1, desc1 = keypoints(na, nb)
    matcher.setPairInfo(kpts0, kpts1, (200, 150), (300, 250))
    matches = matcher.match(desc0, desc1)
    print(f'{na}x{nb}: {len(matches)} matches, distances {[round(m.distance, 8) for m in matches[:2]]}', flush=True)
