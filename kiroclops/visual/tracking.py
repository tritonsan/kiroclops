"""Fixed-template, optical-flow and contour observations from immutable pixels.

Coordinates are native image pixels; x/y are selected anchors. Invalid frames
never contain predicted coordinates. Predictions are only private search hints.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path

import cv2
import numpy as np

from .schema import VisualError
from .package import load_package, frame_paths, read_image
from .opencv5 import require_opencv5, opencv_runtime


def _number(value, name, minimum=0, maximum=float('inf')):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise VisualError('invalid_selection', f'{name} must be finite in [{minimum}, {maximum}]')
    return float(value)


def _roi(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(v) is not int for v in value):
        raise VisualError('invalid_selection', 'ROI must contain four integer pixel values')
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) < 3 or x+w > size[0] or y+h > size[1]:
        raise VisualError('invalid_selection', 'ROI is outside the native image or too small')
    return x, y, w, h


def _parabolic_peak(response, px, py, method):
    """Sub-pixel peak offset from the 3x3 NCC neighbourhood (opt-in ``parabolic_v1``).

    A separable parabola through the integer peak and its two axis neighbours
    gives the vertex offset, clamped to half a pixel. The integer search origin
    never moves. Negative curvature confirms a genuine maximum; a border peak,
    a colour-masked neighbour or a flat neighbourhood reports a zero offset
    with its status, so the integer result stands unchanged.
    """
    height, width = response.shape[:2]
    info = {'method': method, 'dx': 0., 'dy': 0., 'curvature_x': None, 'curvature_y': None, 'status': 'interior'}
    if not (0 < px < width-1 and 0 < py < height-1):
        info['status'] = 'border_peak'
        return info

    def axis(a, b, c):
        values = [float(a), float(b), float(c)]
        if not all(math.isfinite(v) and v > -1 for v in values):
            return 0., None
        denominator = values[0]-2*values[1]+values[2]
        if denominator >= 0:
            return 0., denominator
        return max(-.5, min(.5, .5*(values[0]-values[2])/denominator)), denominator

    info['dx'], info['curvature_x'] = axis(response[py, px-1], response[py, px], response[py, px+1])
    info['dy'], info['curvature_y'] = axis(response[py-1, px], response[py, px], response[py+1, px])
    if info['curvature_x'] is None or info['curvature_y'] is None:
        info['status'] = 'masked_neighbour'
    elif info['curvature_x'] >= 0 or info['curvature_y'] >= 0:
        info['status'] = 'flat_neighbourhood'
    return info


UPSAMPLE_MAX_FACTOR = 10
UPSAMPLE_BUDGET_PX = 2e5


def _upsample_factor(width, height):
    """Largest integer factor (2..10) whose upsampled template stays near the pixel budget."""
    return max(2, min(UPSAMPLE_MAX_FACTOR, int(math.sqrt(UPSAMPLE_BUDGET_PX/max(1, width*height)))))


def _upsampled_peak(image, template, gx, gy, factor=None):
    """Sub-pixel peak by matching symmetrically upsampled pixels (opt-in ``upsampled_v1``).

    The template and the integer-peak window (one pixel of margin) are both
    enlarged by the same integer factor with the same bicubic kernel
    (``cv2.resize``), then scored with the same normalised cross-correlation as
    the integer search. Because both sides pass through one fixed kernel, the
    peak is not pulled toward integer offsets the way candidate-dependent
    resampling or a three-point parabola on the integer response is. A parabola
    on the upsampled response refines below one upsampled pixel. A window
    touching the image border keeps the integer result.
    """
    th, tw = template.shape[:2]
    gx, gy = int(round(gx)), int(round(gy))
    factor = _upsample_factor(tw, th) if factor is None else int(factor)
    info = {'method': 'upsampled_v1', 'dx': 0., 'dy': 0., 'factor': factor, 'status': 'interior', 'score_refined': None}
    if gx < 1 or gy < 1 or gx+tw+1 > image.shape[1] or gy+th+1 > image.shape[0]:
        info['status'] = 'border_peak'
        return info
    window = np.ascontiguousarray(image[gy-1:gy+th+1, gx-1:gx+tw+1])
    big_window = cv2.resize(window, ((tw+2)*factor, (th+2)*factor), interpolation=cv2.INTER_CUBIC)
    big_template = cv2.resize(template, (tw*factor, th*factor), interpolation=cv2.INTER_CUBIC)
    response = np.nan_to_num(cv2.matchTemplate(big_window, big_template, cv2.TM_CCOEFF_NORMED), nan=-1, posinf=-1, neginf=-1)
    _, score, _, (px, py) = cv2.minMaxLoc(response)
    fine = _parabolic_peak(response, px, py, 'parabolic_v1')
    dx, dy = (px+fine['dx'])/factor-1, (py+fine['dy'])/factor-1
    if abs(dx) > .5 or abs(dy) > .5:
        info['status'] = 'clamped_to_half_pixel'
    info['dx'], info['dy'] = float(max(-.5, min(.5, dx))), float(max(-.5, min(.5, dy)))
    info['score_refined'] = float(score)
    info['fine_status'] = fine['status']
    return info


def _ncc(image, template, origin, radius, max_color_rmse=None, refine=None):
    height, width = image.shape[:2]
    th, tw = template.shape[:2]
    x, y = origin
    left, top = max(0, math.floor(x-radius)), max(0, math.floor(y-radius))
    right, bottom = min(width, math.ceil(x+tw+radius)), min(height, math.ceil(y+th+radius))
    crop = image[top:bottom, left:right]
    if crop.shape[0] < th or crop.shape[1] < tw:
        return None
    response = np.nan_to_num(cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED), nan=-1, posinf=-1, neginf=-1)
    if max_color_rmse is not None:
        maximum=_number(max_color_rmse,'max_color_rmse',0,255)
        # NCC intentionally ignores channel offsets. A fixed patch's observed
        # mean color preserves colored identity without penalizing ordinary
        # subpixel rasterization as raw per-pixel differences would.
        integral=cv2.integral(crop)
        means=(integral[th:,tw:]-integral[:-th,tw:]-integral[th:,:-tw]+integral[:-th,:-tw])/(th*tw)
        color_rmse=np.sqrt(np.mean((means-template.mean(axis=(0,1)))**2,axis=2))
        response[color_rmse>maximum]=-1
    _, score, _, (px, py) = cv2.minMaxLoc(response)
    from .ncc_peak import resolve_peak
    px, py, score = resolve_peak(crop, template, response, px, py, score)
    remaining = response.copy()
    suppression = max(1, min(tw, th)//3)
    remaining[max(0, py-suppression):py+suppression+1, max(0, px-suppression):px+suppression+1] = -1
    result = (float(left+px), float(top+py), float(score), float(score-float(remaining.max())))
    if refine is None:
        return result
    if refine == 'upsampled_v1':
        return result+(_upsampled_peak(image, template, left+px, top+py),)
    return result+(_parabolic_peak(response, px, py, refine),)


def _blank(record, reason):
    return {'index': record['index'], 'time_s': record['time_s'], 'valid': False,
            'x': None, 'y': None, 'left': None, 'top': None, 'width': None, 'height': None,
            'score': None, 'reason': reason, 'present': None}


def _intervals(value, records, start):
    if value is None:
        return [[records[start]['time_s'], records[-1]['time_s']]]
    if not isinstance(value, list) or not value:
        raise VisualError('invalid_selection', 'active_intervals_s must be a nonempty list')
    previous = -float('inf')
    result=[]
    for interval in value:
        if not isinstance(interval, (list,tuple)) or len(interval)!=2:
            raise VisualError('invalid_selection', 'Each active interval requires start and end')
        a,b=[_number(v,'active interval',-60,records[-1]['time_s']+60) for v in interval]
        if b<a or a<=previous:
            raise VisualError('invalid_selection','Active intervals must be ordered and disjoint')
        if not any(a<=r['time_s']<=b for r in records):
            raise VisualError('invalid_selection','Active interval contains no recorded frame')
        previous=b;result.append([a,b])
    return result


def _active(time, intervals):
    return any(a-1e-9<=time<=b+1e-9 for a,b in intervals)


def _template_bank(template, anchor, spec, convention='pixel_index'):
    """Mirrored/quarter-turned template variants with the anchor mapped alongside.

    ``pixel_index`` anchors name a pixel (column ``ax`` mirrors to ``w-1-ax``);
    this is the V1 rule every explicit selection anchor uses. ``continuous``
    anchors are coordinates inside the patch (``ax`` mirrors to ``w-ax``), which
    keeps the default centre ``w/2`` on the same physical point in every
    variant. Unmirrored, unrotated results are identical under both.
    """
    rotations = spec.get('rotations', spec.get('template_rotations', [0]))
    if not isinstance(rotations, list) or not rotations or any(type(v) is not int or v not in (0,90,180,270) for v in rotations):
        raise VisualError('invalid_selection', 'Template rotations must be fixed quarter turns')
    bank=[]
    height,width=template.shape[:2]
    far=0 if convention=='continuous' else 1
    for mirrored in ([False,True] if spec.get('horizontal_mirror',False) else [False]):
        base=np.ascontiguousarray(template[:,::-1] if mirrored else template)
        ax,ay=(width-far-anchor[0],anchor[1]) if mirrored else anchor
        offsets={0:(ax,ay),90:(ay,width-far-ax),180:(width-far-ax,height-far-ay),270:(height-far-ay,ax)}
        for angle in dict.fromkeys(rotations):
            bank.append((np.ascontiguousarray(np.rot90(base,angle//90)),offsets[angle],mirrored,angle))
    return bank


def _search_radius(base_radius, elapsed_s, median_interval_s, image_size):
    """Expand pixel search for an observed long frame interval, never a guess.

    The selection radius describes an ordinary interval in this recording.
    Only adjacent presentation timestamps scale it; failed matches do not
    accumulate a growing reacquisition window. Pixel score, identity color
    and ambiguity gates still decide whether a location was observed.
    """
    scale = max(1., elapsed_s/median_interval_s) if median_interval_s else 1.
    return min(float(max(image_size)*2), base_radius*scale), scale


def _template_match(image, bank, position, radius, max_color_rmse, *, whole_image=False, strict_bank_uniqueness=False, refine=None):
    """Return the best observed identity and its strongest distinct-location rival.

    A global retry changes only the search extent. It uses the original fixed
    template bank, color gate and location-uniqueness calculation unchanged.
    ``refine`` adds the opt-in sub-pixel peak offset as a trailing element.
    """
    candidates=[]
    for patch,offset,mirrored,rotation in bank:
        origin=(0,0) if whole_image else (position[0]-offset[0],position[1]-offset[1])
        matched=_ncc(image,patch,origin,radius,max_color_rmse,refine=refine)
        if matched:
            left,top,score,margin=matched[:4]
            candidates.append((score,margin,left,top,offset,patch.shape[1],patch.shape[0],mirrored,rotation,
                               matched[4] if refine is not None else None))
    if not candidates:
        return None
    candidates.sort(key=lambda item:item[0],reverse=True)
    score,margin,left,top,offset,width,height,mirrored,rotation,peak=candidates[0]
    rival=score-margin
    for other in candidates[1:]:
        if math.hypot(other[2]+other[4][0]-left-offset[0],other[3]+other[4][1]-top-offset[1])>max(1,min(width,height)//3):
            rival=max(rival,other[0])
        if strict_bank_uniqueness:
            # Another variant may prefer this same anchor while also matching
            # a distinct remote object. Its runner-up cannot be discarded.
            # Including it is conservative when nearby variant peaks overlap.
            rival=max(rival,other[0]-other[1])
    return score,score-rival,left,top,offset,width,height,mirrored,rotation,peak


def _geometry(left, top, width, height, anchor):
    return {'x': float(left+anchor[0]), 'y': float(top+anchor[1]),
            'left': float(left), 'top': float(top), 'width': float(width), 'height': float(height)}


def _reference_geometry_options(image, roi, options):
    """Freeze appearance from the selected pixels, never a domain color or box."""
    result = dict(options)
    segmentation = result.get('segmentation', 'edges')
    if segmentation not in ('edges', 'reference_fill'):
        raise VisualError('invalid_selection', 'Unknown visible contour segmentation')
    if segmentation == 'reference_fill':
        x, y, w, h = roi
        pixels = image[y:y+h, x:x+w].reshape(-1, 3)
        colors, counts = np.unique(pixels, axis=0, return_counts=True)
        peak = int(counts.argmax())
        if counts[peak]/len(pixels) < .25:
            raise VisualError('insufficient_observability', 'Selected geometry has no dominant visible fill')
        result['_observed_fill_bgr'] = colors[peak].astype(np.int16)
        palette_count = result.get('fill_palette_count', 1)
        if type(palette_count) is not int or not 1 <= palette_count <= 3:
            raise VisualError('invalid_selection', 'Reference fill palette must contain one to three observed colors')
        if palette_count > 1:
            indexes=np.argsort(counts)[-palette_count:]
            if any(counts[i]/len(pixels)<.1 for i in indexes):
                raise VisualError('insufficient_observability','Each selected fill color needs visible reference support')
            result['_observed_fill_palette_bgr']=colors[indexes].astype(np.int16)
        result['fill_tolerance'] = _number(result.get('fill_tolerance', 5), 'fill_tolerance', 0, 32)
        # A fill contour is inside a painted outline. Learn its surrounding
        # background from the actual reference annulus, not a domain palette.
        pad=4;left=max(0,x-pad);top=max(0,y-pad)
        right=min(image.shape[1],x+w+pad);bottom=min(image.shape[0],y+h+pad)
        surround=image[top:bottom,left:right]
        ring=np.ones(surround.shape[:2],bool);ring[y-top:y-top+h,x-left:x-left+w]=False
        values,counts=np.unique(surround[ring],axis=0,return_counts=True)
        if not len(values):raise VisualError('insufficient_observability','No visible surrounding background')
        result['_observed_background_bgr']=values[int(counts.argmax())].astype(float)
        if result.get('outline_mode') == 'reference_color':
            if min(x,y)<1 or x+w>=image.shape[1] or y+h>=image.shape[0]:
                raise VisualError('insufficient_observability','Visible outline requires an unclipped reference annulus')
            xa,xb=x+max(1,w//5),x+w-max(1,w//5)
            ya,yb=y+max(1,h//5),y+h-max(1,h//5)
            sides=np.concatenate((image[y-1,xa:xb],image[y+h,xa:xb],image[ya:yb,x-1],image[ya:yb,x+w]))
            border_colors,border_counts=np.unique(sides,axis=0,return_counts=True)
            border_index=int(border_counts.argmax())
            if border_counts[border_index]/len(sides)<.5:
                raise VisualError('insufficient_observability','No independently visible common outline color')
            result['_observed_outline_bgr']=border_colors[border_index].astype(float)
            result['outline_color_tolerance']=_number(result.get('outline_color_tolerance',24),'outline_color_tolerance',0,32)
    return result


def _visible_outline_bounds(image, box, background, outline=None, outline_tolerance=24):
    """Include actually painted sides; a thin connecting line is insufficient.

    The central sixty percent of each side avoids rounded corners. A side can
    extend only through a run of observed pixels, never through a blank margin.
    """
    x,y,w,h=box;height,width=image.shape[:2]
    xa,xb=x+max(1,w//5),x+w-max(1,w//5)
    ya,yb=y+max(1,h//5),y+h-max(1,h//5)
    def supported(pixels,inside=None):
        if outline is not None:
            # Subpixel browser strokes blend with the adjacent fill. Observe
            # that actual contrast, rather than demanding an opaque border
            # color or extending through its darker drop shadow.
            base=inside.astype(float);delta=outline-base;paint=pixels.astype(float)-base
            squared=np.sum(delta*delta,axis=-1);strength=np.sum(paint*delta,axis=-1)/np.maximum(1,squared)
            residual=np.max(abs(paint-strength[:,None]*delta),axis=-1)
            visible=(strength>=.12)&(strength<=1.25)&(np.max(abs(paint),axis=-1)>=10)&(residual<=outline_tolerance)
            return bool(len(pixels) and np.mean(visible)>=.75)
        return bool(len(pixels) and np.mean(np.max(abs(pixels.astype(float)-background),axis=-1)>10)>=.75)
    left,right,top,bottom=x,x+w,y,y+h
    for offset in range(1,5):
        column=x-offset
        if column<0 or not supported(image[ya:yb,column],image[ya:yb,min(width-1,x+2)]):break
        left=column
    for offset in range(4):
        column=x+w+offset
        if column>=width or not supported(image[ya:yb,column],image[ya:yb,max(0,x+w-3)]):break
        right=column+1
    for offset in range(1,5):
        row=y-offset
        if row<0 or not supported(image[row,xa:xb],image[min(height-1,y+2),xa:xb]):break
        top=row
    for offset in range(4):
        row=y+h+offset
        if row>=height or not supported(image[row,xa:xb],image[max(0,y+h-3),xa:xb]):break
        bottom=row+1
    return left,top,right-left,bottom-top


def _contours(gray, origin, base_size, radius, options, image=None):
    h, w = gray.shape
    x, y = origin
    bw, bh = base_size
    pad = int(radius)
    left, top = max(0, int(x)-pad), max(0, int(y)-pad)
    right, bottom = min(w, int(x+bw)+pad), min(h, int(y+bh)+pad)
    crop = gray[top:bottom, left:right]
    if crop.size == 0:
        return []
    if options.get('segmentation') == 'reference_fill':
        if image is None or '_observed_fill_bgr' not in options:
            raise VisualError('invalid_selection', 'Reference fill must be learned from a selected image')
        fill = options['_observed_fill_bgr']
        tolerance = options['fill_tolerance']
        mask=np.zeros(crop.shape,np.uint8)
        for color in options.get('_observed_fill_palette_bgr',[fill]):
            mask |= cv2.inRange(image[top:bottom, left:right],
                               np.clip(color-tolerance, 0, 255).astype(np.uint8),
                               np.clip(color+tolerance, 0, 255).astype(np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    else:
        edges = cv2.Canny(crop, options.get('canny_low', 50), options.get('canny_high', 150))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    min_area = options.get('min_area_ratio', .2)*bw*bh
    max_area = options.get('max_area_ratio', 5.)*bw*bh
    for contour in contours:
        bx, by, cw, ch = cv2.boundingRect(contour)
        area = abs(cv2.contourArea(contour))
        if min(cw, ch) < 3 or not min_area <= cw*ch <= max_area:
            continue
        rectangularity = area/max(1, (cw-1)*(ch-1))
        if rectangularity < options.get('min_rectangularity', .8):
            continue
        gx, gy = left+bx, top+by
        if options.get('segmentation')=='reference_fill':
            gx,gy,cw,ch=_visible_outline_bounds(image,(gx,gy,cw,ch),options['_observed_background_bgr'],
                options.get('_observed_outline_bgr'),options.get('outline_color_tolerance',24))
        distance = math.hypot(gx+cw/2-(x+bw/2), gy+ch/2-(y+bh/2))
        shape_change = abs(math.log(cw/bw))+abs(math.log(ch/bh))
        score = min(1., rectangularity)*math.exp(-distance/max(1, radius)-.15*shape_change)
        proposal = (gx, gy, cw, ch, score)
        # Canny commonly returns the two sides of the same one-pixel outline.
        if not any(max(abs(a-b) for a, b in zip(proposal[:4], old[:4])) <= 2 for old in candidates):
            candidates.append(proposal)
    return sorted(candidates, key=lambda item: item[4], reverse=True)


LINE_ENDPOINT_METHODS = {'extreme_v1': 'canny_connected_visible_support',
                         'axis_band_v2': 'canny_axis_band_visible_support_v2',
                         'axis_band_v3_ring_aware': 'canny_axis_band_ring_aware_visible_end_v3'}
AXIS_BAND_METHODS = ('axis_band_v2', 'axis_band_v3_ring_aware')
# The tracker always passes the colour frame and so measures with the V5 rule.
# A caller with only the grey frame keeps the V3 rule; asking for the V5 rule
# without the colour frame is an error, never a silent fallback.
DEFAULT_ENDPOINT_METHOD = 'axis_band_v3_ring_aware'
GRAY_ONLY_ENDPOINT_METHOD = 'axis_band_v2'
NODE_HALO_PX = 3
# V5 ring-aware rule (PHASE3D_V5_PLAN.md 4.2). A browser focus ring is painted
# achromatic (R=G=B) just outside a node box; strokes, cards, borders and the
# canvas are tinted. Ring pixels near a box never terminate a visible stroke.
RING_ZONE_PX = 6
RING_MAX_CHROMA = 6
OCCLUSION_WALK_PX = 10
BACKGROUND_MAX_DIFF = 12


def _achromatic(image):
    pixels = image.astype(np.int16)
    return (np.max(pixels, axis=2)-np.min(pixels, axis=2)) <= RING_MAX_CHROMA


def _ring_mask(image, frames, crop):
    """Achromatic pixels within RING_ZONE_PX of any valid box, grown by one pixel, in crop coordinates."""
    crop_x, crop_y, crop_right, crop_bottom = crop
    zone = np.zeros((crop_bottom-crop_y, crop_right-crop_x), bool)
    for frame in frames.values():
        if not frame.get('valid'):
            continue
        x, y, w, h = [int(round(frame[k])) for k in ('left', 'top', 'width', 'height')]
        y0, y1 = max(crop_y, y-RING_ZONE_PX), min(crop_bottom, y+h+RING_ZONE_PX)
        x0, x1 = max(crop_x, x-RING_ZONE_PX), min(crop_right, x+w+RING_ZONE_PX)
        if y1 > y0 and x1 > x0:
            zone[y0-crop_y:y1-crop_y, x0-crop_x:x1-crop_x] = True
    ring = zone & _achromatic(image[crop_y:crop_bottom, crop_x:crop_right])
    return cv2.dilate(ring.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1).astype(bool)


def _background(image, crop, across, corridor, frames):
    """Median colour of the corridor rim away from every box, or None when too little is visible."""
    crop_x, crop_y, crop_right, crop_bottom = crop
    rim = (across >= max(1., corridor-2.)) & (across <= corridor)
    for frame in frames.values():
        if not frame.get('valid'):
            continue
        x, y, w, h = [int(round(frame[k])) for k in ('left', 'top', 'width', 'height')]
        y0, y1 = max(crop_y, y-RING_ZONE_PX), min(crop_bottom, y+h+RING_ZONE_PX)
        x0, x1 = max(crop_x, x-RING_ZONE_PX), min(crop_right, x+w+RING_ZONE_PX)
        if y1 > y0 and x1 > x0:
            rim[y0-crop_y:y1-crop_y, x0-crop_x:x1-crop_x] = False
    pixels = image[crop_y:crop_bottom, crop_x:crop_right][rim]
    return np.median(pixels, axis=0) if len(pixels) >= 10 else None


def _stroke_chroma(image, crop, region, frames, background):
    """Median chroma of the corridor's non-background pixels away from every box, or None when none are seen.

    A ring is separable from the stroke by chroma only when the stroke itself is
    tinted; an achromatic connector keeps the V3 endpoint rule.
    """
    if background is None:
        return None
    crop_x, crop_y, crop_right, crop_bottom = crop
    away = region.copy()
    for frame in frames.values():
        if not frame.get('valid'):
            continue
        x, y, w, h = [int(round(frame[k])) for k in ('left', 'top', 'width', 'height')]
        margin = RING_ZONE_PX+2
        y0, y1 = max(crop_y, y-margin), min(crop_bottom, y+h+margin)
        x0, x1 = max(crop_x, x-margin), min(crop_right, x+w+margin)
        if y1 > y0 and x1 > x0:
            away[y0-crop_y:y1-crop_y, x0-crop_x:x1-crop_x] = False
    pixels = image[crop_y:crop_bottom, crop_x:crop_right].astype(np.int16)
    foreground = away & (np.max(np.abs(pixels-background.astype(np.int16)), axis=2) > 2*BACKGROUND_MAX_DIFF)
    selected = pixels[foreground]
    if len(selected) < 10:
        return None
    return float(np.median(np.max(selected, axis=1)-np.min(selected, axis=1)))


def _occluded(image, point, toward, frame, background):
    """True when an achromatic occluder, and no background, fills the axis gap from a visible end to its box.

    A background pixel on the way is a real gap. Reaching the box without having
    crossed an achromatic pixel needs no flag: that end is judged by its distance.
    """
    if background is None or not frame.get('valid'):
        return False
    left, top = frame['left'], frame['top']
    right, bottom = left+frame['width'], top+frame['height']
    occluder = False
    for step in np.arange(.5, OCCLUSION_WALK_PX+1e-9, .5):
        x, y = point[0]+toward[0]*step, point[1]+toward[1]*step
        if left <= x < right and top <= y < bottom:
            return occluder
        xi, yi = int(math.floor(x)), int(math.floor(y))
        if not (0 <= xi < image.shape[1] and 0 <= yi < image.shape[0]):
            return False
        pixel = image[yi, xi].astype(float)
        if float(np.max(np.abs(pixel-background))) <= BACKGROUND_MAX_DIFF:
            return False
        if float(pixel.max()-pixel.min()) <= RING_MAX_CHROMA:
            occluder = True
    return False


def _axis_band_endpoints(points, halo, direction):
    """V2 endpoints: the stroke axis at the furthest visible extent of its band.

    Node outlines and focus rings lie just outside the masked boxes and are joined
    to the stroke by the grouping dilation. They may still terminate the visible
    stroke, but they must not tilt its axis or pull an endpoint along a border.
    The axis is fitted away from node borders; the across-stroke position is that
    axis (the visible stroke centre), and the along-stroke position is the extreme
    projection of support within the stroke band. Canny can start the two stroke
    sides on different rows, so averaging only the extreme pixels is biased.
    """
    interior = points[~halo]
    if len(interior) < 6:
        return None
    vx, vy, x0, y0 = cv2.fitLine(interior.astype(np.float32), cv2.DIST_L2, 0, .01, .01).reshape(-1)
    fitted = np.array([vx, vy], float)
    if abs(float(np.dot(fitted, direction))) < .97:
        return None
    if np.dot(fitted, direction) < 0:
        fitted *= -1
    origin = np.array([x0, y0], float)
    offset = points-origin
    lateral = np.abs(offset[:, 0]*fitted[1]-offset[:, 1]*fitted[0])
    width = max(1.5, float(np.percentile(lateral[~halo], 95))+1.)
    band = points[lateral <= width]
    projected = (band-origin) @ fitted
    # fitLine's origin lies on the fitted axis; both ends are placed on that axis.
    return origin+fitted*float(projected.min()), origin+fitted*float(projected.max())


def _line_connection(gray, spec, frames, *, edge_cache=None, endpoint_method=None, image=None):
    """Visible straight segments only. Endpoints are measured, never snapped.

    ``axis_band_v3_ring_aware`` (V5; the default when the colour frame is given)
    removes achromatic focus-ring pixels near the boxes before grouping and flags
    an end whose whole gap to its box is covered by such an occluder
    (``start_occluded``/``end_occluded``). ``axis_band_v2`` is the V3 rule and the
    default for grey-only callers; ``extreme_v1`` is the historical rule. Both are
    kept for identity checks of old evidence.
    """
    method = (endpoint_method or spec.get('endpoint_method')
              or (DEFAULT_ENDPOINT_METHOD if image is not None else GRAY_ONLY_ENDPOINT_METHOD))
    if method not in LINE_ENDPOINT_METHODS:
        raise VisualError('invalid_selection', 'Unsupported connection endpoint method.')
    ring_aware = method == 'axis_band_v3_ring_aware'
    if ring_aware and (image is None or image.ndim != 3 or image.shape[:2] != gray.shape[:2]):
        raise VisualError('invalid_selection', 'The ring-aware endpoint rule needs the colour frame.')
    a, b = frames[spec['from_object']], frames[spec['to_object']]
    if not a['valid'] or not b['valid']:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'node_unobservable'}
    pa = np.array([a['left']+a['width']/2, a['top']+a['height']/2], float)
    pb = np.array([b['left']+b['width']/2, b['top']+b['height']/2], float)
    direction = pb-pa
    length = float(np.linalg.norm(direction))
    if length < 5:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'ambiguous_node_geometry'}
    direction /= length
    thresholds = (spec.get('canny_low', 50), spec.get('canny_high', 150))
    # The cache belongs to ONE decoded frame, never an object or an analysis.
    # Canny still sees the whole original image: cropping before Canny would
    # change edge support at the crop boundary. Every link masks its own copy.
    if edge_cache is None:
        full_edges = cv2.Canny(gray, *thresholds)
    else:
        if thresholds not in edge_cache:
            edge_cache[thresholds] = cv2.Canny(gray, *thresholds)
        full_edges = edge_cache[thresholds]
    corridor = _number(spec.get('corridor_px', 8), 'connection corridor', 1, 100)
    # Every corridor pixel is within corridor pixels of the segment's axis-
    # aligned bounds. Eight further pixels retain dilation (1px) and ALL real
    # outside-support pixels queried around measured endpoints (up to 4px).
    # Round the origin down to an even pixel so connected-component 2x2 block
    # ordering has the same global parity as the full-image kernel.
    end = pa+direction*length
    pad = math.ceil(corridor)+8
    crop_x = max(0, (math.floor(min(pa[0], end[0]))-pad)//2*2)
    crop_y = max(0, (math.floor(min(pa[1], end[1]))-pad)//2*2)
    crop_right = min(gray.shape[1], (math.ceil(max(pa[0], end[0]))+pad+2)//2*2)
    crop_bottom = min(gray.shape[0], (math.ceil(max(pa[1], end[1]))+pad+2)//2*2)
    if crop_right <= crop_x or crop_bottom <= crop_y:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'visible_connection_not_found'}
    edges = full_edges[crop_y:crop_bottom, crop_x:crop_right].copy()
    yy, xx = np.indices(edges.shape)
    xx += crop_x
    yy += crop_y
    along = (xx-pa[0])*direction[0]+(yy-pa[1])*direction[1]
    across = abs((xx-pa[0])*direction[1]-(yy-pa[1])*direction[0])
    region = (across <= corridor) & (along >= 0) & (along <= length)
    for frame in frames.values():
        if not frame.get('valid'):
            continue
        x, y, w, h = [int(round(frame[k])) for k in ('left', 'top', 'width', 'height')]
        # Normalize original global Python slices before intersecting the crop.
        # This retains their exact semantics even for partly off-image boxes.
        y0,y1,_=slice(max(0,y),y+h).indices(gray.shape[0])
        x0,x1,_=slice(max(0,x),x+w).indices(gray.shape[1])
        edges[max(0,y0-crop_y):max(0,y1-crop_y),max(0,x0-crop_x):max(0,x1-crop_x)] = 0
    separable, background = False, None
    if ring_aware:
        crop = (crop_x, crop_y, crop_right, crop_bottom)
        background = _background(image, crop, across, corridor, frames)
        chroma = _stroke_chroma(image, crop, (across <= corridor) & (along >= 0) & (along <= length), frames, background)
        separable = chroma is not None and chroma > 2*RING_MAX_CHROMA
        if separable:
            edges[_ring_mask(image, frames, crop)] = 0
    # Preserve actual support outside the search region. Cutting a continuing
    # stroke at the corridor boundary does not reveal a physical endpoint.
    outside_support = (edges > 0) & ~region
    for frame in frames.values():
        if not frame.get('valid'):
            continue
        x, y, w, h = [int(round(frame[k])) for k in ('left', 'top', 'width', 'height')]
        # Canny/three-pixel grouping can leave a node-outline halo. It is an
        # occluding border, not evidence that a connector continues outside
        # the corridor. This mask is diagnostic only; endpoints are unchanged.
        y0,y1,_=slice(max(0,y-3),y+h+3).indices(gray.shape[0])
        x0,x1,_=slice(max(0,x-3),x+w+3).indices(gray.shape[1])
        outside_support[max(0,y0-crop_y):max(0,y1-crop_y),max(0,x0-crop_x):max(0,x1-crop_x)] = False
    edges[~region] = 0
    # Canny's two sides of a three-pixel stroke are one visible connection.
    # Group nearby edge support, but measure endpoints from original edge
    # pixels only. Hough segments alone can stop halfway along a sound line.
    # V2 groups both Canny sides of an antialiased three-pixel stroke, which can
    # lie four pixels apart; the historical V1 kernel is kept byte-compatible.
    kernel = 5 if method in AXIS_BAND_METHODS else 3
    support = cv2.dilate(edges, np.ones((kernel, kernel), np.uint8), iterations=1)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(support, connectivity=8)
    if count <= 1:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'visible_connection_not_found'}
    candidates = []
    for label in range(1, count):
        left, top, width, height, area = stats[label]
        if area < 6:
            continue
        local_labels=labels[top:top+height,left:left+width]
        local_edges=edges[top:top+height,left:left+width]
        ys, xs = np.nonzero((local_labels == label) & (local_edges > 0))
        if len(xs) < 6:
            continue
        xs=xs+left+crop_x;ys=ys+top+crop_y
        points = np.column_stack((xs, ys)).astype(float)
        if method in AXIS_BAND_METHODS:
            halo = np.zeros(len(points), bool)
            for frame in frames.values():
                if not frame.get('valid'):
                    continue
                x, y, w, h = [int(round(frame[k])) for k in ('left', 'top', 'width', 'height')]
                halo |= ((points[:, 0] >= x-NODE_HALO_PX) & (points[:, 0] < x+w+NODE_HALO_PX)
                         & (points[:, 1] >= y-NODE_HALO_PX) & (points[:, 1] < y+h+NODE_HALO_PX))
            ends = _axis_band_endpoints(points, halo, direction)
            if ends is None:
                continue
            p, q = ends
        else:
            vx, vy, _, _ = cv2.fitLine(points.astype(np.float32), cv2.DIST_L2, 0, .01, .01).reshape(-1)
            fitted = np.array([vx, vy], float)
            if abs(float(np.dot(fitted, direction))) < .97:
                continue
            if np.dot(fitted, direction) < 0:
                fitted *= -1
            projected = points @ fitted
            # Averaging the observed opposing stroke edges locates its visible
            # center, without extending it toward any node boundary.
            p = points[projected <= projected.min()+1e-5].mean(axis=0)
            q = points[projected >= projected.max()-1e-5].mean(axis=0)
        size = float(np.linalg.norm(q-p))
        if size < 6:
            continue
        clipped = False
        for endpoint in (p, q):
            ex, ey = endpoint
            x0, x1 = max(0, int(ex)-3), min(gray.shape[1], int(ex)+5)
            y0, y1 = max(0, int(ey)-3), min(gray.shape[0], int(ey)+5)
            oy, ox = np.nonzero(outside_support[y0-crop_y:y1-crop_y, x0-crop_x:x1-crop_x])
            if len(ox) and np.any((ox+x0-ex)**2+(oy+y0-ey)**2 <= 9):
                clipped = True
                break
        midpoint = (p+q)/2
        lateral = abs(float((midpoint[0]-pa[0])*direction[1]-(midpoint[1]-pa[1])*direction[0]))
        candidates.append((size-.5*lateral, p.tolist(), q.tolist(), size, clipped))
    if not candidates:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'unsupported_connection_geometry'}
    candidates.sort(key=lambda row: row[0], reverse=True)
    best = candidates[0]
    if best[4]:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'endpoint_clipped_by_search_region'}
    if any(abs(other[0]-best[0]) < 1 and
           min(np.linalg.norm(np.array(other[1])-best[1]), np.linalg.norm(np.array(other[2])-best[2])) > 4
           for other in candidates[1:]):
        return {'valid': False, 'start': None, 'end': None, 'reason': 'ambiguous_visible_connection'}
    measured = {'valid': True, 'start': best[1], 'end': best[2], 'score': min(1., best[3]/length),
                'method': LINE_ENDPOINT_METHODS[method], 'reason': None}
    if ring_aware:
        measured['ring_separable'] = separable
        measured['start_occluded'] = separable and _occluded(image, np.array(best[1]), -direction, a, background)
        measured['end_occluded'] = separable and _occluded(image, np.array(best[2]), direction, b, background)
    return measured


def analyze_package(package_dir, selection, contract=None, check=lambda: None, progress=lambda *args: None):
    """Analyze confirmed object ROIs without domain identities or frame-rate assumptions.

    Selection options: template min_score/min_margin/search_radius; flow
    max_fb_error_px/min_features/min_inlier_ratio; contour min_rectangularity,
    min/max_area_ratio. Connections currently support method='line' only.
    V6 options: ``anchor_convention`` (``pixel_index`` for explicit anchors,
    ``continuous`` for the default centre) and the opt-in template
    ``peak_refinement`` (``upsampled_v1`` or ``parabolic_v1``: sub-pixel x/y;
    the integer search origin is kept).
    """
    contract = dict(contract or {})
    check()
    require_opencv5()
    # Verify every byte and PNG dimension first. Every frame is decoded by the
    # tracker below, so a second full decoding pass adds no integrity evidence.
    manifest = load_package(package_dir, verify='hashes')
    paths = frame_paths(package_dir, manifest)
    records = manifest.get('frames', [])
    size = manifest.get('image_size')
    if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
        raise VisualError('invalid_package', 'Native image_size must be [width,height]')
    if not records or len(records) != len(paths):
        raise VisualError('invalid_package', 'Frame records and paths differ')
    previous = -float('inf')
    for i, record in enumerate(records):
        if record.get('index') != i or type(record.get('time_s')) not in (int, float) or not math.isfinite(record['time_s']) or record['time_s'] <= previous:
            raise VisualError('capture_timing_failure', 'Strictly increasing finite presentation times required')
        previous = record['time_s']
    median_interval = statistics.median(b['time_s']-a['time_s'] for a,b in zip(records,records[1:])) if len(records)>1 else None
    if not isinstance(selection, dict) or selection.get('schema_version') != 1:
        raise VisualError('invalid_selection', 'Selection schema_version must be 1')
    specs = selection.get('objects')
    if not isinstance(specs, list) or not 1 <= len(specs) <= 32:
        raise VisualError('invalid_selection', 'Select between one and 32 objects')
    if any(not isinstance(s,dict) for s in specs):
        raise VisualError('invalid_selection', 'Each selected object must be an object')
    ids = [s.get('id') for s in specs]
    if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
        raise VisualError('invalid_selection', 'Object IDs must be nonempty and unique')

    def read(index):
        check()
        image = read_image(paths[index])
        if image is None or list(image.shape[1::-1]) != size:
            raise VisualError('invalid_frame', f'Frame {index} cannot be decoded at its declared dimensions')
        return image

    states = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise VisualError('invalid_selection', 'Each object must be an object record')
        x, y, w, h = _roi(spec.get('roi'), size)
        index = spec.get('frame_index', 0)
        if type(index) is not int or not 0 <= index < len(paths):
            raise VisualError('invalid_selection', 'Template frame outside video')
        intervals=_intervals(spec.get('active_intervals_s'),records,index)
        geometry=spec.get('geometry')
        anchor = spec.get('anchor', [w/2, h/2])
        if not isinstance(anchor, (list, tuple)) or len(anchor) != 2:
            raise VisualError('invalid_selection', 'Anchor must be a two-dimensional pixel offset')
        anchor = (_number(anchor[0], 'anchor x', 0, w), _number(anchor[1], 'anchor y', 0, h))
        convention = spec.get('anchor_convention', 'pixel_index' if 'anchor' in spec else 'continuous')
        if convention not in ('pixel_index', 'continuous'):
            raise VisualError('invalid_selection', 'anchor_convention must be pixel_index or continuous')
        method = spec.get('method', 'template')
        if method not in ('template', 'flow', 'contour', 'features'):
            raise VisualError('unsupported_input', f'Unsupported tracking method {method}')
        refinement = spec.get('peak_refinement')
        if refinement is not None and refinement not in ('parabolic_v1', 'upsampled_v1'):
            raise VisualError('invalid_selection', 'peak_refinement supports parabolic_v1 or upsampled_v1 only')
        if refinement is not None and method != 'template':
            raise VisualError('unsupported_input', 'peak_refinement requires fixed-template tracking')
        # V6 plan WP1.4: OpenCV 5 learned features, as a method or as a cross-check.
        if method == 'features' and (refinement is not None or 'pixel_anchor' in spec
                                     or spec.get('reacquisition_policy', 'local_only_v1') != 'local_only_v1'):
            raise VisualError('unsupported_input', 'features tracking takes no peak refinement, pixel anchor or reacquisition policy')
        cross_check = spec.get('cross_check')
        if cross_check not in (None, 'features_v1'):
            raise VisualError('invalid_selection', 'cross_check supports features_v1 only')
        if cross_check is not None and method != 'template':
            raise VisualError('unsupported_input', 'cross_check requires fixed-template tracking')
        reacquisition_policy=spec.get('reacquisition_policy','local_only_v1')
        if reacquisition_policy not in ('local_only_v1','full_image_unique_v1') or (method!='template' and reacquisition_policy!='local_only_v1'):
            raise VisualError('invalid_selection','Unknown or unsupported template reacquisition policy')
        if geometry is not None:
            if not isinstance(geometry,dict) or geometry.get('method')!='contour':
                raise VisualError('invalid_selection','Separate visible geometry must use contour extraction')
            geometry_roi=_roi(geometry.get('roi'),size)
        else:
            geometry_roi=None
        template_source=None
        if spec.get('template_package') is not None:
            raise VisualError('unsupported_input','External template paths are not portable; select a frame inside this package')
        template_index=spec.get('template_frame_index',index)
        if type(template_index) is not int or not 0<=template_index<len(paths):
            raise VisualError('invalid_selection','Template frame outside this package')
        template_image=read(template_index)
        geometry_options = _reference_geometry_options(template_image, geometry_roi, geometry) if geometry_roi else None
        template_source={'package_sha256':manifest['package_sha256'],'frame_index':template_index,'frame_sha256':records[template_index]['sha256']}
        template = template_image[y:y+h, x:x+w].copy()
        pixel_anchor = None
        if 'pixel_anchor' in spec:
            if method != 'template':
                raise VisualError('unsupported_input', 'Observed color-mass anchors require fixed-template identity tracking')
            from .pixel_anchor import prepare_anchor
            pixel_anchor = prepare_anchor(template, spec['pixel_anchor'])
        if method != 'contour' and float(np.max(np.std(template.astype(float), axis=(0, 1)))) < 2:
            raise VisualError('insufficient_observability', f'{spec["id"]}: selected patch has no spatial texture')
        gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        points = None
        if method == 'flow':
            points = cv2.goodFeaturesToTrack(gray, maxCorners=80, qualityLevel=.01, minDistance=3, blockSize=3)
            if points is None or len(points) < spec.get('min_features', 4):
                raise VisualError('insufficient_observability', f'{spec["id"]}: not enough independent flow features')
            points = points+np.float32([[[x, y]]])
        if method=='flow' and template_index!=index:
            raise VisualError('unsupported_input','Flow must begin at its observed selection frame')
        states.append({'spec': spec, 'template': template, 'bank': _template_bank(template,anchor,spec,convention), 'template_source':template_source,
                       'anchor': anchor, 'anchor_convention': convention, 'refinement': refinement,
                       'cross_check': cross_check, 'template_image': template_image if (method == 'features' or cross_check) else None,
                       'position':(x+anchor[0],y+anchor[1]),'origin': (x, y),
                       'size': (w, h), 'initial_roi': (x, y, w, h), 'start': index, 'points': points,
                       'intervals':intervals,'geometry_roi':geometry_roi,'geometry_options':geometry_options,
                       'previous_gray': None, 'transform': np.eye(3), 'lost': False, 'frames': [],
                       'pixel_anchor': pixel_anchor})
    first = read(0)
    backgrounds = []
    for box in selection.get('background_rois', []):
        x, y, w, h = _roi(box, size)
        patch = first[y:y+h, x:x+w].copy()
        if float(np.max(np.std(patch.astype(float), axis=(0, 1)))) < 2:
            raise VisualError('insufficient_observability', 'Background patch is textureless')
        backgrounds.append((patch, (x, y)))
    connections = selection.get('connections', [])
    if not isinstance(connections, list):
        raise VisualError('invalid_selection', 'Connections must be a list')
    connection_ids = set()
    for spec in connections:
        if not isinstance(spec, dict) or not isinstance(spec.get('id'), str) or spec['id'] in connection_ids or spec.get('from_object') not in ids or spec.get('to_object') not in ids or spec.get('from_object') == spec.get('to_object'):
            raise VisualError('invalid_selection', 'Connection must bind two distinct selected objects and a unique ID')
        if spec.get('method', 'line') not in ('line','polyline'):
            raise VisualError('unsupported_input', 'Supported visible connections are straight lines and selected-color non-branching polylines')
        connection_ids.add(spec['id'])
    connection_outputs = [{**{k: s[k] for k in ('id', 'from_object', 'to_object')}, 'frames': []} for s in connections]
    camera = []
    # V6 plan WP1.5: a sub-pixel camera shift report by phase correlation of each
    # background patch against its first-frame pixels. Reported only; the camera
    # gate stays the integer NCC rule. A cropped patch does not shift circularly,
    # so the estimate leans toward whole pixels (about 0.15 px on windowed 128-px
    # synthetic patches) and grows with smaller or busier patches.
    phase_references = []
    for patch, _ in backgrounds:
        reference = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32)
        phase_references.append((reference, cv2.createHanningWindow(reference.shape[1::-1], cv2.CV_32F)))
    features_engine = None  # Loaded once per measurement, only when a selection asks for it.

    def features():
        nonlocal features_engine
        if features_engine is None:
            from . import features5
            features_engine = features5.FeatureEngine()
        return features_engine

    for index, record in enumerate(records):
        image = read(index)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        camera_ok = True
        shifts = []
        for patch, origin in backgrounds:
            matched = _ncc(image, patch, origin, 6)
            shift = math.hypot(matched[0]-origin[0], matched[1]-origin[1]) if matched else float('inf')
            shifts.append(shift)
            camera_ok &= bool(matched and matched[2] >= .85 and shift <= contract.get('camera_max_shift_px', 1.5))
        phase_shifts = []
        for (patch, (bx, by)), (reference, window) in zip(backgrounds, phase_references):
            observed = gray[by:by+patch.shape[0], bx:bx+patch.shape[1]].astype(np.float32)
            (dx, dy), response = cv2.phaseCorrelate(reference, observed, window)
            phase_shifts.append([round(float(dx), 4), round(float(dy), 4), round(float(response), 4)])
        camera.append({'index': index, 'time_s': record['time_s'], 'status': 'pass' if backgrounds and camera_ok else 'fail' if backgrounds else 'unverified',
                       'max_shift_px': max(shifts) if shifts and all(math.isfinite(s) for s in shifts) else None,
                       **({'phase_shifts_px': phase_shifts} if phase_shifts else {})})
        current = {}
        for state in states:
            spec = state['spec']; method = spec.get('method', 'template')
            frame = _blank(record, 'before_selection_frame')
            active=_active(record['time_s'],state['intervals'])
            if index >= state['start'] and active:
                frame = _blank(record, 'tracking_failure')
                base_radius = _number(spec.get('search_radius', max(16, .8*max(state['size']))), 'search_radius', 1, max(size)*2)
                elapsed = record['time_s']-records[index-1]['time_s'] if index>state['start'] else 0.
                radius,time_scale = _search_radius(base_radius,elapsed,median_interval,size)
                frame.update(search_radius_px=radius,search_time_scale=time_scale)
                if method == 'template':
                    minimum_score=_number(spec.get('min_score', .75), 'min_score', -1, 1)
                    minimum_margin=_number(spec.get('min_margin', .03), 'min_margin', 0, 2)
                    reacquire=spec.get('reacquisition_policy','local_only_v1')=='full_image_unique_v1'
                    matched=_template_match(image,state['bank'],state['position'],radius,spec.get('max_color_rmse'),
                                            strict_bank_uniqueness=reacquire,refine=state['refinement'])
                    if reacquire and (not matched or matched[0]<minimum_score or matched[1]<minimum_margin):
                        # One bounded retry over this recorded image; no template
                        # update, predicted location, interpolation or gap fill.
                        check()
                        frame.update(template_search='full_image_reacquisition',reacquisition_policy='full_image_unique_v1',
                                     local_score=matched[0] if matched else None,
                                     local_margin=matched[1] if matched else None)
                        matched=_template_match(image,state['bank'],state['position'],max(size)*2,
                                                spec.get('max_color_rmse'),whole_image=True,strict_bank_uniqueness=True,
                                                refine=state['refinement'])
                    if matched:
                        score,margin,left,top,offset,width,height,mirrored,rotation,peak=matched
                        frame.update(score=score, margin=margin)
                        if score >= minimum_score and margin >= minimum_margin:
                            state['origin'] = (left, top)
                            state['position']=(left+offset[0],top+offset[1])
                            # The integer peak stays the search origin; only the
                            # reported coordinates carry the opt-in sub-pixel offset.
                            dx,dy=(peak['dx'],peak['dy']) if peak else (0.,0.)
                            frame.update(valid=True, reason=None,mirrored=mirrored,rotation_deg=rotation, **_geometry(left+dx, top+dy, width,height,offset))
                            if peak:frame['peak_refinement']=peak
                            if state['pixel_anchor'] is not None:
                                from .pixel_anchor import observe_anchor
                                refined=observe_anchor(image,left,top,width,height,state['pixel_anchor'])
                                if refined['valid']:
                                    frame.update(refined)
                                else:
                                    frame=_blank(record,refined['reason']) | {k:v for k,v in refined.items() if k.startswith('pixel_anchor_')}
                            if state['cross_check'] == 'features_v1' and frame['valid']:
                                # Independent learned-feature location of the same patch; reported, never gating.
                                from . import features5
                                located = features5.locate(features(), state['template_image'], state['initial_roi'], state['anchor'], image,
                                                           center=(frame['x'], frame['y']), search_radius=radius)
                                check_row = {k: located.get(k) for k in ('valid', 'x', 'y', 'scale', 'rotation_deg', 'matches', 'inliers', 'inlier_ratio', 'reason')}
                                if located['valid']:
                                    check_row['consistency_px'] = math.hypot(located['x']-frame['x'], located['y']-frame['y'])
                                frame['features_cross_check'] = check_row
                elif method == 'features':
                    from . import features5
                    located = features5.locate(features(), state['template_image'], state['initial_roi'], state['anchor'], image,
                                               center=state['position'], search_radius=radius,
                                               min_matches=spec.get('min_matches', features5.MIN_MATCHES),
                                               min_inlier_ratio=spec.get('min_inlier_ratio', features5.MIN_INLIER_RATIO))
                    frame.update(feature_matches=located['matches'], feature_inliers=located['inliers'], inlier_ratio=located['inlier_ratio'])
                    if located['valid'] and spec.get('min_scale', .5) <= located['scale'] <= spec.get('max_scale', 2.):
                        state['position'] = (located['x'], located['y'])
                        frame.update(valid=True, reason=None, score=located['inlier_ratio'], x=located['x'], y=located['y'],
                                     left=located['left'], top=located['top'], width=located['width'], height=located['height'],
                                     scale=located['scale'], rotation_deg=located['rotation_deg'], features_method=located['method'])
                    else:
                        frame['reason'] = located.get('reason') or 'features_scale_out_of_range'
                elif method == 'contour':
                    found = _contours(gray, state['origin'], state['size'], radius, spec)
                    if found:
                        left, top, width, height, score = found[0]
                        margin = score-(found[1][4] if len(found) > 1 else 0.)
                        frame.update(score=score, margin=margin)
                        if score >= spec.get('min_score', .35) and margin >= spec.get('min_margin', .02):
                            iw, ih = state['initial_roi'][2:]
                            offset = (state['anchor'][0]*width/iw, state['anchor'][1]*height/ih)
                            state['origin'], state['size'] = (left, top), (width, height)
                            frame.update(valid=True, reason=None, **_geometry(left, top, width, height, offset))
                elif not state['lost']:
                    if index == state['start']:
                        frame.update(valid=True, score=1., reason=None, **_geometry(*state['origin'], *state['size'], state['anchor']))
                    else:
                        old = state['points']
                        forward, status, _ = cv2.calcOpticalFlowPyrLK(state['previous_gray'], gray, old, None, winSize=(21, 21), maxLevel=3)
                        if forward is not None:
                            backward, reverse_status, _ = cv2.calcOpticalFlowPyrLK(gray, state['previous_gray'], forward, None, winSize=(21, 21), maxLevel=3)
                            if backward is not None:
                                fb = np.linalg.norm(backward-old, axis=2).reshape(-1)
                                good = status.reshape(-1).astype(bool) & reverse_status.reshape(-1).astype(bool) & (fb <= spec.get('max_fb_error_px', 1.))
                                good &= np.isfinite(forward).all(axis=(1, 2))
                                if int(good.sum()) >= spec.get('min_features', 4):
                                    matrix, inliers = cv2.estimateAffinePartial2D(old[good], forward[good], method=cv2.RANSAC, ransacReprojThreshold=spec.get('ransac_error_px', 1.5), maxIters=500, confidence=.99)
                                    ratio = float(inliers.mean()) if inliers is not None else 0.
                                    if matrix is not None and ratio >= spec.get('min_inlier_ratio', .75) and np.isfinite(matrix).all():
                                        transform = np.vstack([matrix, [0, 0, 1]])@state['transform']
                                        x, y, w, h = state['initial_roi']
                                        vertices = np.array([[x,y,1],[x+w,y,1],[x+w,y+h,1],[x,y+h,1]], float)@transform.T
                                        transformed_anchor = transform@np.array([x+state['anchor'][0], y+state['anchor'][1], 1])
                                        lo, hi = vertices[:,:2].min(axis=0), vertices[:,:2].max(axis=0)
                                        scale = math.hypot(transform[0,0], transform[1,0])
                                        if .2 <= scale <= 5 and lo[0] >= 0 and lo[1] >= 0 and hi[0] <= size[0] and hi[1] <= size[1]:
                                            frame.update(valid=True, reason=None, score=ratio, x=float(transformed_anchor[0]), y=float(transformed_anchor[1]), left=float(lo[0]), top=float(lo[1]), width=float(hi[0]-lo[0]), height=float(hi[1]-lo[1]), forward_backward_error_px=float(np.median(fb[good])), inlier_ratio=ratio)
                                            state['transform'] = transform
                                            keep = good.copy(); keep[good] &= inliers.reshape(-1).astype(bool)
                                            state['points'] = forward[keep].reshape(-1,1,2)
                    if not frame['valid']:
                        state['lost'] = True  # No hidden reacquisition over an unobserved gap.
                    state['previous_gray'] = gray
                else:
                    frame['reason'] = 'flow_identity_lost'
                if frame['valid'] and state['geometry_roi'] is not None:
                    geometry=state['geometry_options'];gx,gy,gw,gh=state['geometry_roi']
                    ix,iy,_,_=state['initial_roi']
                    if state['pixel_anchor'] is not None:
                        # Artwork centroid and integer template bank anchors are
                        # separate observed quantities. Only matching-origin
                        # displacement seeds the independent outer contour.
                        predicted=(gx+state['origin'][0]-ix,gy+state['origin'][1]-iy)
                    else:
                        predicted=(gx+frame['x']-ix-state['anchor'][0],gy+frame['y']-iy-state['anchor'][1])
                    candidates=_contours(gray,predicted,(gw,gh),geometry.get('search_radius',12),geometry,image)
                    if candidates:
                        gl,gt,gw,gh,gs=candidates[0]
                        gm=gs-(candidates[1][4] if len(candidates)>1 else 0.)
                        if gs>=geometry.get('min_score',.4) and gm>=geometry.get('min_margin',.02):
                            frame.update(left=float(gl),top=float(gt),width=float(gw),height=float(gh),geometry_score=float(gs),geometry_method='contour')
                        else:
                            frame=_blank(record,'ambiguous_visible_boundary')
                    else:
                        frame=_blank(record,'visible_boundary_not_found')
                if frame['valid']:frame['present']=True
            elif not active:
                frame=_blank(record,'outside_expected_lifecycle')
                # An inactive interval is a reference contract, not evidence of
                # absence. Search the whole image for the original fixed patch.
                # Intermediate confidence remains unknown, never guessed absent.
                if method=='template':
                    matches=[_ncc(image,p,(0,0),max(size)*2,spec.get('max_color_rmse')) for p,*_ in state['bank']]
                    score=max((m[2] for m in matches if m),default=-1.)
                    frame['presence_score']=score
                    frame['present']=True if score>=spec.get('min_score',.75) else False if score<spec.get('absence_max_score',.45) else None
                if method=='flow':state['lost']=True
            if not camera_ok or (contract.get('require_static_camera', False) and not backgrounds):
                reason = 'camera_motion' if backgrounds else 'camera_unverified'
                frame = _blank(record, reason)
            frame['expected_active']=active
            state['frames'].append(frame)
            current[spec['id']] = frame
        line_edge_cache = {}
        for spec, output in zip(connections, connection_outputs):
            active=current[spec['from_object']]['expected_active'] and current[spec['to_object']]['expected_active']
            if active and spec.get('method','line')=='polyline':
                from .polyline import visible_polyline
                measured=visible_polyline(image,spec,current)
            else:
                measured=_line_connection(gray,spec,current,edge_cache=line_edge_cache,image=image) if active else {'valid':False,'start':None,'end':None,'reason':'outside_expected_lifecycle'}
            output['frames'].append({'index': index, 'time_s': record['time_s'],'expected_active':active, **measured})
        progress('tracking', index+1, len(records))
    objects = []
    errors = []
    minimum = _number(contract.get('min_valid_ratio', .95), 'min_valid_ratio', 0, 1)
    for state in states:
        observed = [f for f in state['frames'] if f['expected_active']]
        ratio = sum(f['valid'] for f in observed)/len(observed)
        item = {'id': state['spec']['id'], 'method': state['spec'].get('method', 'template'), 'frames': state['frames'],
                'valid_ratio': ratio, 'active_intervals_s':state['intervals'], 'selection_frame': state['start'], 'template_source':state['template_source'], 'status': 'ok' if ratio >= minimum else 'rejected'}
        if state['pixel_anchor'] is not None:
            item['pixel_anchor']={'method':state['spec']['pixel_anchor']['method'],
                                  'semantics':'centroid_of_observed_foreground_color_mass',
                                  'foreground_rgb':state['spec']['pixel_anchor']['foreground_rgb'],
                                  'reference_quality':state['pixel_anchor']['reference_quality'],
                                  'accuracy_uncertainty_px':None,
                                  'accuracy_claim':'Subpixel coordinates require independent accuracy validation; no presentation or physical-center inference.'}
        objects.append(item)
        if ratio < minimum:
            errors.append({'code': 'tracking_failure', 'object_id': item['id'], 'message': 'Object valid-frame ratio below contract minimum'})
    for item in connection_outputs:
        active=[f for f in item['frames'] if f['expected_active']]
        item['valid_ratio'] = sum(f['valid'] for f in active)/len(active) if active else 0.
        if item['valid_ratio'] < minimum:
            errors.append({'code': 'insufficient_observability', 'connection_id': item['id'], 'message': 'Visible connection is missing or ambiguous'})
    package_hash = manifest.get('content_sha256') or manifest.get('package_sha256') or hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {'schema_version': 1, 'kind': 'ObservationSet', 'status': 'rejected' if errors else 'ok',
            'measurement_status':'fail' if errors else 'pass',
            'image_size': size, 'package_sha256': package_hash, 'objects': objects,
            'connections': connection_outputs, 'camera': camera, 'inputs': manifest.get('inputs', {'provenance': 'unknown', 'events': []}),
            'selection': selection, 'errors': errors, 'opencv_version': cv2.__version__,
            'opencv_runtime': opencv_runtime(),
            'search_timing': {'policy':'adjacent_presentation_interval_scaled_radius',
                              'median_interval_s':median_interval,'predicted_positions_emitted':False},
            'coordinate_system': 'native_image_pixels', 'measurement_source': 'pixels_only',
            'warnings': [] if backgrounds else ['Static camera has not been independently observed.'],
            'limitations': ['No hidden-state or input inference.', 'Flow identities are not silently reacquired after failure.',
                            'Connection extraction currently accepts visible straight segments only.']}
