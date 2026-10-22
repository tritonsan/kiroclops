"""Opt-in foreground-color centroids observed in each actual matched image.

This measures a fixed illustration's visible color mass, not its physical center.
NCC identity gates belong to the caller. No temporal state or inferred positions
are used here; a changed or unsuitable appearance produces an invalid sample.
"""
from __future__ import annotations

import math
import cv2
import numpy as np

from .schema import VisualError

METHOD = 'foreground_color_mass_v1'


def _mix_weights(pixels, foreground, backgrounds):
    values = pixels.astype(float)
    best = np.full(pixels.shape[:2], np.inf)
    weights = np.zeros(pixels.shape[:2], float)
    for background in backgrounds:
        delta = foreground-background
        alpha = np.sum((values-background)*delta, axis=2)/float(delta@delta)
        alpha = np.clip(alpha, 0., 1.)
        residual = np.max(abs(values-background-alpha[..., None]*delta), axis=2)
        use = residual < best
        best[use], weights[use] = residual[use], alpha[use]
    valid = best <= 3.
    return np.where(valid, weights, 0.), valid


def _moments(weights):
    mass = float(weights.sum())
    if mass < 9:
        return None
    yy, xx = np.indices(weights.shape, dtype=float)
    center = np.array([float(np.sum(weights*xx)/mass), float(np.sum(weights*yy)/mass)])
    dx, dy = xx-center[0], yy-center[1]
    covariance = np.array([[np.sum(weights*dx*dx), np.sum(weights*dx*dy)],
                           [np.sum(weights*dx*dy), np.sum(weights*dy*dy)]])/mass
    # Component topology is frozen from visible opaque support. A handle made
    # of six separate dots is supported; one connected component is not assumed.
    count, _, stats, _ = cv2.connectedComponentsWithStats((weights >= .5).astype(np.uint8), 8)
    components = sum(int(stats[i, cv2.CC_STAT_AREA]) >= 2 for i in range(1, count))
    border_mass = float(weights[0].sum()+weights[-1].sum()+weights[1:-1, 0].sum()+weights[1:-1, -1].sum())
    return {'mass': mass, 'center': center, 'eigenvalues': np.linalg.eigvalsh(covariance),
            'components': components, 'border_mass': border_mass}


def prepare_anchor(template, options):
    """Freeze a color mixture model from the selected PNG, never later frames."""
    if not isinstance(options, dict) or options.get('method') != METHOD:
        raise VisualError('invalid_selection', 'Unknown observed pixel-anchor method')
    rgb = options.get('foreground_rgb')
    if (not isinstance(rgb, (list, tuple)) or len(rgb) != 3
            or any(type(v) is not int or not 0 <= v <= 255 for v in rgb)):
        raise VisualError('invalid_selection', 'Foreground color requires three integer RGB values')
    foreground = np.array(rgb[::-1], float)
    opaque = np.max(abs(template.astype(float)-foreground), axis=2) <= 3
    if int(opaque.sum()) < 9:
        raise VisualError('insufficient_observability', 'Pixel anchor lacks opaque observed foreground support')
    colors, counts = np.unique(template.reshape(-1, 3), axis=0, return_counts=True)
    backgrounds = []
    # Freeze at most four independently visible background/ink colors. Colors
    # explained as anti-alias mixtures are not new palette entries.
    for index in np.argsort(-counts, kind='stable'):
        color = colors[index].astype(float)
        if counts[index] < max(2, math.ceil(template.shape[0]*template.shape[1]*.01)):
            continue
        if np.linalg.norm(color-foreground) < 32:
            continue
        if backgrounds:
            _, supported = _mix_weights(color[None, None, :], foreground, backgrounds)
            if bool(supported[0, 0]):
                continue
        backgrounds.append(color)
        if len(backgrounds) == 4:
            break
    if not backgrounds:
        raise VisualError('insufficient_observability', 'Pixel anchor has no separable observed background')
    weights, supported = _mix_weights(template, foreground, backgrounds)
    shape = _moments(weights)
    if (shape is None or shape['components'] == 0 or shape['border_mass'] > .1
            or float(supported.mean()) < .95):
        raise VisualError('insufficient_observability', 'Pixel anchor is clipped or lacks a supported fixed-color appearance')
    return {'foreground': foreground, 'backgrounds': backgrounds, 'shape': shape,
            'reference_quality': {'foreground_mass_px': shape['mass'],
                                  'centroid_offset_px': shape['center'].tolist(),
                                  'component_count': shape['components'],
                                  'supported_pixel_fraction': float(supported.mean()),
                                  'background_palette_rgb': [b[::-1].astype(int).tolist() for b in backgrounds]}}


def observe_anchor(image, left, top, width, height, reference):
    """Return an observed subpixel center or a reason, without filling failures."""
    x, y, w, h = (int(v) for v in (left, top, width, height))
    if x < 0 or y < 0 or x+w > image.shape[1] or y+h > image.shape[0]:
        return {'valid': False, 'reason': 'pixel_anchor_crop_clipped'}
    crop = image[y:y+h, x:x+w]
    weights, supported = _mix_weights(crop, reference['foreground'], reference['backgrounds'])
    shape = _moments(weights)
    if shape is None:
        return {'valid': False, 'reason': 'pixel_anchor_foreground_missing'}
    initial = reference['shape']
    mass_ratio = shape['mass']/initial['mass']
    shape_error = float(np.max(abs(shape['eigenvalues']-initial['eigenvalues'])/np.maximum(1., initial['eigenvalues'])))
    quality = {'foreground_mass_px': shape['mass'], 'mass_ratio': mass_ratio,
               'shape_relative_error': shape_error, 'component_count': shape['components'],
               'supported_pixel_fraction': float(supported.mean()), 'border_mass_px': shape['border_mass']}
    reason = ('pixel_anchor_crop_clipped' if shape['border_mass'] > .1 else
              'pixel_anchor_palette_mismatch' if float(supported.mean()) < .95 else
              'pixel_anchor_mass_changed' if abs(mass_ratio-1.) > .08 else
              'pixel_anchor_components_changed' if shape['components'] != initial['components'] else
              'pixel_anchor_shape_changed' if shape_error > .12 else None)
    return {'valid': reason is None, 'reason': reason,
            'x': float(x+shape['center'][0]) if reason is None else None,
            'y': float(y+shape['center'][1]) if reason is None else None,
            'pixel_anchor_quality': quality, 'pixel_anchor_method': METHOD,
            'pixel_anchor_accuracy_uncertainty_px': None}
