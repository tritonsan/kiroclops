"""Resolve near-equal NCC peaks using the observed integer pixels.

SIMD implementations can reverse float32 maxima for two mathematically equal
windows. Exact integer sums identify that tie; row/column order resolves it.
This changes neither score/ambiguity thresholds nor missing-frame handling.
"""
from __future__ import annotations

import math
import numpy as np


def exact_ncc(patch, template):
    if patch.shape != template.shape or patch.dtype != np.uint8 or template.dtype != np.uint8:
        return None
    channels = 1 if template.ndim == 2 else template.shape[2]
    a = patch.reshape(-1, channels).astype(np.int64)
    b = template.reshape(-1, channels).astype(np.int64)
    count = len(a)
    sa, sb = a.sum(axis=0), b.sum(axis=0)
    numerator = int(count * np.sum(a*b) - np.sum(sa*sb))
    av = int(count * np.sum(a*a) - np.sum(sa*sa))
    bv = int(count * np.sum(b*b) - np.sum(sb*sb))
    if av <= 0 or bv <= 0:
        return None
    return max(-1., min(1., numerator / math.sqrt(av*bv)))


def resolve_peak(crop, template, response, px, py, score):
    if crop.dtype != np.uint8 or template.dtype != np.uint8 or score <= 0:
        return px, py, score
    candidates = np.argwhere(response >= score - .00001)
    # More than sixteen tied peaks remains an ambiguous native match; the
    # existing distinct-location margin gate rejects repeated identities.
    if not 1 < len(candidates) <= 16:
        return px, py, score
    h, w = template.shape[:2]
    ranked = []
    for y, x in candidates:
        refined = exact_ncc(crop[y:y+h, x:x+w], template)
        if refined is not None:
            ranked.append((-refined, int(y), int(x)))
    if not ranked:
        return px, py, score
    negative, y, x = min(ranked)
    return x, y, -negative
