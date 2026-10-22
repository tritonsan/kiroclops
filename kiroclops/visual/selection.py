"""Pixel-only object proposals. Proposals require confirmation, not semantic trust."""
from __future__ import annotations

import cv2
import numpy as np

from .schema import VisualError
from .package import load_package, frame_paths, read_image


def propose_objects(package_dir, *, max_objects=6, check=lambda: None):
    """Suggest moving regions from sampled frame differences and visible outlines.

    No tracker, DOM, telemetry or model labels participate. The caller must
    confirm the objects; a proposal is never automatically an accepted selection.
    """
    if type(max_objects) is not int or not 1 <= max_objects <= 32:
        raise VisualError('invalid_selection', 'max_objects must be between 1 and 32')
    manifest = load_package(package_dir, verify=True)
    paths = frame_paths(package_dir, manifest)
    if not paths:
        raise VisualError('unsupported_input', 'No video frames')
    base = read_image(paths[0])
    if base is None:
        raise VisualError('invalid_frame', 'Cannot decode first frame')
    gray = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    activity = np.zeros_like(gray)
    for index in sorted(set(np.linspace(0, len(paths)-1, min(12, len(paths)), dtype=int))):
        check()
        current = cv2.cvtColor(read_image(paths[index]), cv2.COLOR_BGR2GRAY)
        if current is None or current.shape != gray.shape:
            raise VisualError('invalid_frame', 'Unreadable or resized frame')
        activity = np.maximum(activity, cv2.absdiff(current, gray))
    moving = np.uint8(activity >= 18)*255
    moving = cv2.morphologyEx(moving, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(moving, 8)
    proposals = []
    for x, y, w, h, area in stats[1:count]:
        if area >= 9 and w >= 3 and h >= 3 and w*h < .85*width*height:
            proposals.append({'roi': [int(x), int(y), int(w), int(h)],
                              'basis': 'motion_envelope', 'activity_pixels': int(area),
                              'frame_index': 0,
                              'warning': 'Envelope may include several objects or their travel; select a stable visible object within it.'})
    # Stationary graph nodes and a panel's frame can be useful candidates too.
    contours, _ = cv2.findContours(cv2.Canny(gray, 60, 160), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w >= 8 and h >= 8 and 40 <= w*h < .8*width*height:
            proposals.append({'roi': [x, y, w, h], 'basis': 'visible_outline',
                              'activity_pixels': int(np.count_nonzero(moving[y:y+h, x:x+w])),
                              'frame_index': 0})
    proposals.sort(key=lambda p: (p['activity_pixels'], p['roi'][2]*p['roi'][3]), reverse=True)
    distinct = []
    for proposal in proposals:
        x, y, w, h = proposal['roi']
        duplicate = False
        for other in distinct:
            a, b, c, d = other['roi']
            overlap = max(0, min(x+w, a+c)-max(x, a))*max(0, min(y+h, b+d)-max(y, b))
            if overlap/max(1, w*h+c*d-overlap) > .8:
                duplicate = True
                break
        if not duplicate:
            proposal['id'] = f'proposal-{len(distinct)+1:02}'
            distinct.append(proposal)
        if len(distinct) == max_objects:
            break
    return {'schema_version': 1, 'kind': 'ObjectProposals', 'status': 'needs_confirmation',
            'image_size': [width, height], 'proposals': distinct,
            'limitations': ['Pixel regions have no guaranteed semantic identity.',
                           'Motion envelopes are not final tracking templates.',
                           'Occluded or textureless selections can be rejected.']}
