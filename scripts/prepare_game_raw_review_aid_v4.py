"""V4 independent game anchor: the frozen anchor plus the mirrored (left-facing) Tiny Crate hat.

The frozen anchor (``scripts/prepare_game_raw_review_aid.py``) recognizes the
Tiny Crate pink hat only when the sprite faces right. The hat is five top pixels
with six below, the lower row extending to the right. When the character turns
left the sprite is mirrored and the lower row extends to the left, so the frozen
reader finds no point in any left-facing frame.

The game mirrors the sprite in place: in raw frames the character's 8-px box does
not move when it turns, and the left-facing sprite is the right-facing one flipped
about the box centre. The frozen right-facing point (x + 1, y + 2), for a top row
starting at x, is always box left + 3. The mirrored hat's top row starts at box
left + 1, so the same box position is (x + 2, y + 2). The point therefore marks
the same character position whichever way the sprite faces.

(Attempt ``v4g01-final-raw-03`` used (x + 3, y + 2), the mirrored hat corner. That
corner is box left + 4, so every turn moved the point by 1 px, and each left start
from rest showed a false raw reversal. It is preserved as a failed attempt.)

It is defined from the raw pixels alone; no production coordinate is read. Heart
frames and right-facing Tiny frames are delegated to the frozen reader unchanged.

Used only by the V4 final raw review (``scripts/phase3d_v3_ground_raw.py`` with a
V4 chain). The calibration reviews keep the frozen reader.
"""
from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from scripts.prepare_game_raw_review_aid import anchor as frozen_anchor

PINK = np.array([168, 119, 255], dtype=np.uint8)
MIRRORED_FEATURE = 'pink_hat_top_right'


def mirrored_tiny(image):
    """Mirrored hat: five top pixels, six below extending one pixel to the left."""
    pink = np.all(image == PINK, axis=2)
    height, width = pink.shape
    found = []
    for y in range(height - 1):
        for x in np.flatnonzero(pink[y]):
            if x < 1 or x + 5 >= width or pink[y, x - 1] or pink[y, x + 5]:
                continue
            if pink[y, x:x + 5].all() and pink[y + 1, x - 1:x + 5].all() and not pink[y + 1, x + 5]:
                found.append({'x': float(x + 2), 'y': float(y + 2), 'feature': MIRRORED_FEATURE, 'feature_xy': [int(x), y]})
    return found


def anchor(image):
    found = frozen_anchor(image)
    height, width = image.shape[:2]
    if (width, height) == (228, 128) and not found:
        return mirrored_tiny(image)
    return found
