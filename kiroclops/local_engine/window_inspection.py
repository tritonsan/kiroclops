"""Visual window inspection for the authoring agent (V6 plan WP3.2).

Numeric feedback tells the agent that a window failed; it does not show what
the failure looks like. This module renders, for one measured object of one
calibration case, three PNG rows (reference crops, candidate crops and their
absolute difference) sampled across the window, plus an error-over-time plot
of the candidate's position against the interpolated reference. Everything is
written inside the target workspace under ``.kiroclops-evidence/inspect`` so
the agent can open it with its ordinary image reader, and the returned text is
short. Only completed calibration evaluations are inspectable; held-out
results never pass through here.

Times and positions follow the comparison row of the chosen window: times are
relative to the input that window is aligned to in each recording
(``input_timing``), samples after the next input are not interpolated, and an
anchor-relative window is plotted as object minus anchor, the quantity it was
scored on. A visible lag in the plot is therefore a real timing difference.
The image rows always show absolute crops around the object.
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from kiroclops.visual.comparison import relative_object
from kiroclops.visual.package import load_package, frame_paths, read_image
from kiroclops.visual.schema import VisualError, read_json, write_json

SAMPLES = 6
PAD_PX = 12
TILE_MAX_PX = 160
GUTTER_PX = 6
PLOT_SIZE = (760, 280)
LAG_SEARCH_FRAMES = 6
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _observations(row):
    observations = row.get('observations')
    if observations is None:
        analysis = read_json(Path(row['analysis']))
        observations = analysis.get('observations', analysis)
    return observations


def _object(row, object_id):
    observations = _observations(row)
    obj = next((o for o in observations.get('objects', []) if o.get('id') == object_id), None)
    if obj is None:
        raise VisualError('unknown_object', f'Object {object_id} is not measured in this case')
    return observations, obj


def _window_frame(chosen, reference_row, candidate_row):
    """Each recording's origin and next-input bound for the chosen window, as the comparison resolved them."""
    timing = (chosen or {}).get('input_timing') or {}
    origins = (timing.get('reference_origin_s'), timing.get('candidate_origin_s'))
    if all(isinstance(v, (int, float)) and math.isfinite(v) for v in origins):
        stops = (list(timing.get('next_input_after_origin_s') or [])+[None, None])[:2]
        return ([float(v) for v in origins], [float(v) if isinstance(v, (int, float)) else None for v in stops],
                'window_input_timing')
    # Rows without per-window input timing: each recording's first input.
    return ([float((row.get('selection') or {}).get('input_origin_s', 0.) or 0.) for row in (reference_row, candidate_row)],
            [None, None], 'selection_first_input')


def _scored_frames(observations, obj, anchor_id, origin, stop):
    """Valid frames in the coordinate space the window was scored in, up to the next input."""
    frames = obj['frames']
    if anchor_id is not None:
        anchor = next((o for o in observations.get('objects', []) if o.get('id') == anchor_id), None)
        if anchor is None:
            raise VisualError('missing_relative_anchor', f'Anchor {anchor_id} is not measured in this case')
        frames = relative_object(obj, anchor)['frames']
    valid = [f for f in frames if f.get('valid') and all(isinstance(f.get(k), (int, float)) for k in ('x', 'y'))
             and (stop is None or f['time_s']-origin < stop-1e-9)]
    if not valid:
        raise VisualError('insufficient_observability', f'Object {obj.get("id")} has no valid frames')
    return valid


def _series(valid, origin):
    times = np.array([f['time_s']-origin for f in valid], float)
    return times, np.array([f['x'] for f in valid], float), np.array([f['y'] for f in valid], float)


def _nearest(valid, origin, t_rel):
    return min(valid, key=lambda f: abs(f['time_s']-origin-t_rel))


def _crop_tile(image, frame, box_size, tile_size, pad=PAD_PX):
    width, height = box_size
    cx, cy = float(frame['x']), float(frame['y'])
    left = int(round(cx-width/2-pad))
    top = int(round(cy-height/2-pad))
    right, bottom = left+int(width)+2*pad, top+int(height)+2*pad
    canvas = np.full((bottom-top, right-left, 3), 40, np.uint8)
    sl, st = max(0, left), max(0, top)
    sr, sb = min(image.shape[1], right), min(image.shape[0], bottom)
    if sr > sl and sb > st:
        canvas[st-top:sb-top, sl-left:sr-left] = image[st:sb, sl:sr]
    return cv2.resize(canvas, tile_size, interpolation=cv2.INTER_NEAREST if canvas.shape[0] < tile_size[1] else cv2.INTER_AREA)


def _label(image, text, origin=(4, 14), scale=.42):
    cv2.putText(image, text, origin, FONT, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(image, text, origin, FONT, scale, (255, 255, 255), 1, cv2.LINE_AA)


def _strip(reference_tiles, candidate_tiles, diff_tiles, header):
    tile_h, tile_w = reference_tiles[0].shape[:2]
    rows = [reference_tiles, candidate_tiles, diff_tiles]
    width = len(reference_tiles)*(tile_w+GUTTER_PX)+GUTTER_PX
    height = 3*(tile_h+GUTTER_PX)+GUTTER_PX+26
    strip = np.full((height, width, 3), 24, np.uint8)
    _label(strip, header, (6, 17), .5)
    for r, tiles in enumerate(rows):
        for c, tile in enumerate(tiles):
            y = 26+GUTTER_PX+r*(tile_h+GUTTER_PX)
            x = GUTTER_PX+c*(tile_w+GUTTER_PX)
            strip[y:y+tile_h, x:x+tile_w] = tile
    return strip


def _plot(times, dx, dy, magnitude, limit_px, t0, t1, title):
    width, height = PLOT_SIZE
    canvas = np.full((height, width, 3), 255, np.uint8)
    left, right, top, bottom = 56, width-16, 30, height-34
    ymax = max(float(np.max(np.abs(np.concatenate([dx, dy, magnitude])))), limit_px or 0., 1e-6)*1.15
    ymin = -ymax

    def px(t, v):
        x = left+(t-t0)/(t1-t0 if t1 > t0 else 1.)*(right-left)
        y = bottom-(v-ymin)/(ymax-ymin)*(bottom-top)
        return int(round(x)), int(round(y))

    cv2.rectangle(canvas, (left, top), (right, bottom), (200, 200, 200), 1)
    zero_y = px(t0, 0.)[1]
    cv2.line(canvas, (left, zero_y), (right, zero_y), (170, 170, 170), 1)
    if limit_px:
        for sign in (1, -1):
            y = px(t0, sign*limit_px)[1]
            for x in range(left, right, 12):
                cv2.line(canvas, (x, y), (min(right, x+6), y), (90, 90, 220), 1)
    for values, colour in ((magnitude, (0, 0, 0)), (dx, (200, 90, 0)), (dy, (0, 60, 220))):
        points = np.array([px(t, v) for t, v in zip(times, values)], np.int32).reshape(-1, 1, 2)
        if len(points) > 1:
            cv2.polylines(canvas, [points], False, colour, 2, cv2.LINE_AA)
        else:
            cv2.circle(canvas, tuple(points[0, 0]), 3, colour, -1)
    cv2.putText(canvas, title, (left, 20), FONT, .5, (0, 0, 0), 1, cv2.LINE_AA)
    cv2.putText(canvas, f'{t0:.3f}s', (left, bottom+18), FONT, .4, (0, 0, 0), 1, cv2.LINE_AA)
    cv2.putText(canvas, f'{t1:.3f}s', (right-56, bottom+18), FONT, .4, (0, 0, 0), 1, cv2.LINE_AA)
    cv2.putText(canvas, f'{ymax:+.1f}px', (4, top+8), FONT, .4, (0, 0, 0), 1, cv2.LINE_AA)
    cv2.putText(canvas, f'{ymin:+.1f}px', (4, bottom), FONT, .4, (0, 0, 0), 1, cv2.LINE_AA)
    legend = 'black |error|  orange dx (candidate-reference)  blue dy' + ('  dashed: window limit' if limit_px else '')
    cv2.putText(canvas, legend, (left, height-8), FONT, .4, (0, 0, 0), 1, cv2.LINE_AA)
    return canvas


def build_window_inspection(reference_row, candidate_row, comparison_row, object_id, window_id, output_dir, *,
                            case_id='', job_id='', samples=SAMPLES):
    """Write strip.png, error.png and summary.json for one object window; return the summary."""
    ref_observations, ref_obj = _object(reference_row, object_id)
    cand_observations, cand_obj = _object(candidate_row, object_id)
    windows = [w for w in ((comparison_row or {}).get('pixel_comparison') or {}).get('object_windows', [])
               if w.get('reference_id') == object_id]
    chosen = next((w for w in windows if window_id is None or w.get('window_id') == window_id), None)
    if window_id is not None and chosen is None:
        raise VisualError('unknown_window', f'Window {window_id} is not measured for {object_id}')
    (ref_origin, cand_origin), (ref_stop, cand_stop), alignment = _window_frame(chosen, reference_row, candidate_row)
    ref_anchor = (chosen or {}).get('relative_to_reference')
    cand_anchor = (chosen or {}).get('relative_to_candidate')
    ref_valid = _scored_frames(ref_observations, ref_obj, ref_anchor, ref_origin, ref_stop)
    cand_valid = _scored_frames(cand_observations, cand_obj, cand_anchor, cand_origin, cand_stop)
    # Crops are cut around the absolute position of the same frame.
    ref_image_frames = {f['index']: f for f in ref_obj['frames']}
    cand_image_frames = {f['index']: f for f in cand_obj['frames']}
    ref_t, ref_x, ref_y = _series(ref_valid, ref_origin)
    if chosen and isinstance(chosen.get('time_window_s'), (list, tuple)) and len(chosen['time_window_s']) == 2:
        t0, t1 = (float(v) for v in chosen['time_window_s'])
    else:
        t0, t1 = float(ref_t[0]), float(ref_t[-1])
    rows = []
    for frame in cand_valid:
        t = frame['time_s']-cand_origin
        if t < t0-1e-9 or t > t1+1e-9 or t < ref_t[0] or t > ref_t[-1]:
            continue
        rows.append((t, frame['x']-float(np.interp(t, ref_t, ref_x)), frame['y']-float(np.interp(t, ref_t, ref_y))))
    if not rows:
        raise VisualError('insufficient_observability', 'No candidate frame overlaps the reference inside this window')
    times = np.array([r[0] for r in rows]); dx = np.array([r[1] for r in rows]); dy = np.array([r[2] for r in rows])
    magnitude = np.hypot(dx, dy)
    worst = int(np.argmax(magnitude))
    interval = float(np.median(np.diff(times))) if len(times) > 1 else None
    lag = {'lag_s': 0., 'rmse_px_at_lag': float(math.sqrt(np.mean(magnitude**2)))}
    if interval:
        for k in range(-LAG_SEARCH_FRAMES, LAG_SEARCH_FRAMES+1):
            shifted = times+k*interval
            inside = (shifted >= ref_t[0]) & (shifted <= ref_t[-1])
            if inside.sum() < max(3, len(times)//2):
                continue
            ex = np.array([frame['x'] for frame in cand_valid if t0-1e-9 <= frame['time_s']-cand_origin <= t1+1e-9 and ref_t[0] <= frame['time_s']-cand_origin <= ref_t[-1]])[inside]-np.interp(shifted[inside], ref_t, ref_x)
            ey = np.array([frame['y'] for frame in cand_valid if t0-1e-9 <= frame['time_s']-cand_origin <= t1+1e-9 and ref_t[0] <= frame['time_s']-cand_origin <= ref_t[-1]])[inside]-np.interp(shifted[inside], ref_t, ref_y)
            rmse = float(math.sqrt(np.mean(ex**2+ey**2)))
            if rmse < lag['rmse_px_at_lag']-1e-9:
                lag = {'lag_s': float(k*interval), 'rmse_px_at_lag': rmse}
    scale_px = chosen.get('scale_px') if chosen else None
    limits = (chosen or {}).get('limits') or {}
    limit_px = limits['rmse_limit']*scale_px if isinstance(limits.get('rmse_limit'), (int, float)) and isinstance(scale_px, (int, float)) else None

    reference_package = load_package(reference_row['package'])
    candidate_package = load_package(candidate_row['package'])
    ref_paths = frame_paths(reference_row['package'], reference_package)
    cand_paths = frame_paths(candidate_row['package'], candidate_package)
    box_w = max(8., float(np.median([ref_image_frames[f['index']].get('width') or 8 for f in ref_valid])))
    box_h = max(8., float(np.median([ref_image_frames[f['index']].get('height') or 8 for f in ref_valid])))
    scale = min(1., TILE_MAX_PX/max(box_w+2*PAD_PX, box_h+2*PAD_PX))
    tile = (max(24, int(round((box_w+2*PAD_PX)*scale))), max(24, int(round((box_h+2*PAD_PX)*scale))))
    reference_tiles, candidate_tiles, diff_tiles, sampled = [], [], [], []
    for t in np.linspace(t0, t1, samples):
        rf, cf = _nearest(ref_valid, ref_origin, t), _nearest(cand_valid, cand_origin, t)
        ref_tile = _crop_tile(read_image(ref_paths[rf['index']]), ref_image_frames[rf['index']], (box_w, box_h), tile)
        cand_tile = _crop_tile(read_image(cand_paths[cf['index']]), cand_image_frames[cf['index']], (box_w, box_h), tile)
        diff = cv2.applyColorMap(cv2.cvtColor(cv2.absdiff(ref_tile, cand_tile), cv2.COLOR_BGR2GRAY), cv2.COLORMAP_INFERNO)
        _label(ref_tile, f'ref {rf["time_s"]-ref_origin:.3f}s')
        _label(cand_tile, f'cand {cf["time_s"]-cand_origin:.3f}s')
        _label(diff, f'dx {cf["x"]-float(np.interp(cf["time_s"]-cand_origin, ref_t, ref_x)):+.1f} dy {cf["y"]-float(np.interp(cf["time_s"]-cand_origin, ref_t, ref_y)):+.1f}')
        reference_tiles.append(ref_tile); candidate_tiles.append(cand_tile); diff_tiles.append(diff)
        sampled.append({'t_s': float(t), 'reference_frame': rf['index'], 'candidate_frame': cf['index']})
    header = f'{case_id} {object_id} {chosen.get("window_id") if chosen else "all"}: rows = reference / candidate / |difference|'
    strip = _strip(reference_tiles, candidate_tiles, diff_tiles, header)
    plot = _plot(times, dx, dy, magnitude, limit_px, t0, t1,
                 f'{object_id} candidate - reference position (px) over the window'
                 + (f', relative to {ref_anchor}' if ref_anchor is not None else ''))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_dir/'strip.png'), strip)
    cv2.imwrite(str(output_dir/'error.png'), plot)
    summary = {
        'kind': 'WindowInspection', 'schema_version': 2, 'job_id': job_id, 'case_id': case_id, 'object_id': object_id,
        'window_id': chosen.get('window_id') if chosen else None, 'time_window_s': [t0, t1],
        'alignment': {'source': alignment, 'reference_origin_s': ref_origin, 'candidate_origin_s': cand_origin},
        'coordinate_space': 'relative_to_anchor' if ref_anchor is not None else 'image',
        'relative_to_reference': ref_anchor, 'relative_to_candidate': cand_anchor,
        'frames_compared': int(len(times)), 'rmse_px': float(math.sqrt(np.mean(magnitude**2))),
        'max_error_px': float(magnitude[worst]), 'max_error_at_s': float(times[worst]),
        'mean_dx_px': float(np.mean(dx)), 'mean_dy_px': float(np.mean(dy)),
        'best_time_shift': lag, 'window_limit_px': limit_px, 'scale_px': scale_px,
        'scored_rmse_px': (chosen['position_rmse']*scale_px if chosen and isinstance(chosen.get('position_rmse'), (int, float))
                           and isinstance(scale_px, (int, float)) else None),
        'strip_png': str(output_dir/'strip.png'), 'error_png': str(output_dir/'error.png'),
        'samples': sampled,
        'reading': ('Positive dx means the candidate sits right of the reference, positive dy below it. '
                    'For an anchor-relative window dx and dy are object-minus-anchor offsets, as scored; the image rows show absolute crops. '
                    'rmse_px is sampled at candidate frames; scored_rmse_px is the window RMSE the comparison integrated and gated. '
                    'A best_time_shift far from zero with a much lower rmse_px_at_lag means the motion is right but early/late. '
                    'Open strip.png (reference / candidate / difference rows) and error.png with your image reader.')}
    write_json(output_dir/'summary.json', summary)
    return summary
