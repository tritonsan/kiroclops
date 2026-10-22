"""Independent raw-pixel inspection and hash-bound operator observations.

Contact sheets read PNGs only. They do not read motion analyses or engine traces.
An observation is supplied by a human or visual reviewer, never by the tracker.
"""
from __future__ import annotations
import math
from pathlib import Path
import cv2
import numpy as np
from kiroclops.integration.storage import read_json, write_json, sha256, IntegrationError


def contact_sheet(run, frames, output, crop=(0, 0, 320, 180), scale=4, columns=5):
    run, output = Path(run), Path(output)
    x, y, w, h = crop
    tile_w, tile_h = w*scale+40, h*scale+48
    sheet = np.full((math.ceil(len(frames)/columns)*tile_h, columns*tile_w, 3), 235, np.uint8)
    hashes = {}
    for n, index in enumerate(frames):
        path = run/'frames'/f'{index:06d}.png'
        raw = cv2.imread(str(path))
        if raw is None or x < 0 or y < 0 or raw.shape[1] < x+w or raw.shape[0] < y+h:
            raise IntegrationError('invalid_frame', f'Cannot inspect {path}')
        hashes[str(index)] = sha256(path)
        tile_x, tile_y = (n%columns)*tile_w, (n//columns)*tile_h
        zoom = cv2.resize(raw[y:y+h,x:x+w], (w*scale,h*scale), interpolation=cv2.INTER_NEAREST)
        sheet[tile_y+25:tile_y+25+h*scale,tile_x+35:tile_x+35+w*scale] = zoom
        cv2.putText(sheet, f'frame {index} x={x}', (tile_x+3,tile_y+16), cv2.FONT_HERSHEY_SIMPLEX,.4,(0,0,0),1)
        for row in range(0,h,4):
            yy = tile_y+25+row*scale
            cv2.putText(sheet,str(y+row),(tile_x+1,yy+5),cv2.FONT_HERSHEY_SIMPLEX,.3,(0,0,0),1)
            cv2.line(sheet,(tile_x+31,yy),(tile_x+34,yy),(0,0,0),1)
        if w>=40:
            for col in range(0,w,10):
                xx=tile_x+35+col*scale; yy=tile_y+25+h*scale
                cv2.line(sheet,(xx,yy),(xx,yy+3),(0,0,0),1)
                cv2.putText(sheet,str(x+col),(xx-7,yy+14),cv2.FONT_HERSHEY_SIMPLEX,.3,(0,0,0),1)
    output.parent.mkdir(parents=True,exist_ok=True)
    if not cv2.imwrite(str(output),sheet):
        raise IntegrationError('image_write_failure',str(output))
    write_json(output.with_suffix('.json'),{'run':str(run.resolve()),'frames':hashes,'crop':list(crop),
        'scale':scale,'image_sha256':sha256(output),'method':'Nearest-neighbor raw PNG sheet; no analysis or telemetry input'})
    return output


def validate_observations(analysis_paths, observations, *, minimum=20, require_events=False, required_categories=()):
    """Replay observations against only matching raw PNG hashes and exact analyses."""
    reports = [read_json(p) for p in analysis_paths]
    lookup = {}
    for report in reports:
        run = Path(report['run_dir']) if 'run_dir' in report else None
        if run is None:
            # Every analysis provenance binds its input directory.
            run = Path(report.get('provenance',{}).get('run_dir', report.get('run_id','')))
        lookup[str(run.resolve())] = report
    points, seen, distinct, categories, errors = [], set(), set(), set(), []
    for point in observations.get('points',[]):
        run = Path(point['run']).resolve()
        report = lookup.get(str(run))
        if not report or point.get('source') not in ('human','assistant_visual'):
            errors.append('Unknown run or nonmanual point'); continue
        index = point['frame_index']; path = run/'frames'/f'{index:06d}.png'
        key=(str(run),index)
        if key in seen or not path.is_file() or point.get('frame_sha256') != sha256(path):
            errors.append('Duplicate point or changed PNG'); continue
        seen.add(key)
        frame = report['frames'][index]
        if type(point.get('x')) not in (int,float) or type(point.get('y')) not in (int,float) or not all(math.isfinite(point[k]) for k in ('x','y')):
            errors.append('Non-finite manual coordinate'); continue
        error=math.hypot(frame['x']-point['x'],frame['y']-point['y']) if frame['valid'] else None
        distinct.add(point['frame_sha256'])
        if error is None or not math.isfinite(error):
            errors.append('Observed frame not measurable')
        points.append({'run':str(run),'frame_index':index,'error_px':error,'frame_sha256':point['frame_sha256']})
        verified_categories=set()
        for phase in report.get('phases',[]):
            if phase['start']<=index<phase['end']:
                if phase['name']=='coast': verified_categories.add('brake')
                if phase['name']=='reversal': verified_categories.add('reverse')
        flips=[i for i in range(1,len(report['frames'])) if report['frames'][i].get('mirrored')!=report['frames'][i-1].get('mirrored')]
        if any(abs(index-i)<=2 for i in flips) or frame.get('mirrored'): verified_categories.add('flip')
        supplied=set(point.get('categories',[]))
        if not supplied<=verified_categories: errors.append('Manual category differs from observed phase/facing')
        categories.update(supplied)
    events=[]
    for control in observations.get('events',[]):
        run=Path(control['run']).resolve(); report=lookup.get(str(run))
        if not report or control.get('source') not in ('human','assistant_visual'):
            errors.append('Unknown event run or source'); continue
        bound=control.get('frame_hashes',{})
        needed={str(i) for k in ('takeoff_frame','apex_frame','landing_frame') for i in range(max(0,control[k]-2),min(240,control[k]+3))}
        if not needed<=bound.keys() or any(sha256(run/'frames'/f'{int(i):06d}.png')!=h for i,h in bound.items()):
            errors.append('Event observation missing immutable raw frame binding'); continue
        difference={k:abs(report['events'][k]-control[k]) for k in ('takeoff_frame','apex_frame','landing_frame')}
        events.append({'run':str(run),'errors_frames':difference,'pass':all(v<=2 for v in difference.values())})
    vals=[p['error_px'] for p in points if p['error_px'] is not None]
    median=float(np.median(vals)) if vals else None
    heights={r['body_height_px'] for r in reports}
    if len(heights)!=1: errors.append('Manual group must have one body scale')
    tolerance=max(1.,.05*next(iter(heights))) if heights else 1.
    passed=(not errors and len(distinct)>=minimum and median is not None and median<=tolerance
            and (not require_events or bool(events)) and all(e['pass'] for e in events)
            and set(required_categories)<=categories)
    return {'status':'pass' if passed else 'fail','point_count':len(seen),'distinct_pixel_points':len(distinct),'minimum':minimum,
            'median_error_px':median,'tolerance_px':tolerance,'points':points,'events':events,
            'categories':sorted(categories),'required_categories':list(required_categories),'errors':errors,
            'analysis_files':{str(Path(p).resolve()):sha256(p) for p in analysis_paths}}
