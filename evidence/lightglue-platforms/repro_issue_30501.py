import sys

import cv2
import numpy as np

# Seeded random ALIKED-shaped input: 2 query and 64 train keypoints, L2-normalised 128-D descriptors.
rng = np.random.default_rng(2*100003 + 64)
desc0 = rng.normal(size=(2, 128)).astype(np.float32)
desc0 /= np.linalg.norm(desc0, axis=1, keepdims=True) + 1e-9
desc1 = rng.normal(size=(64, 128)).astype(np.float32)
desc1 /= np.linalg.norm(desc1, axis=1, keepdims=True) + 1e-9
kpts0 = rng.uniform(0, 200, size=(2, 2)).astype(np.float32)
kpts1 = rng.uniform(0, 300, size=(64, 2)).astype(np.float32)

matcher = cv2.LightGlueMatcher_create(sys.argv[1], 0.0)
matcher.setPairInfo(kpts0, kpts1, (200, 150), (300, 250))
print('matching', flush=True)
print(len(matcher.match(desc0, desc1)), 'matches')
