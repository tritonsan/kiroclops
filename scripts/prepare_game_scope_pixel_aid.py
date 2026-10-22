"""Unsigned sequence/geometry review from raw game PNGs; no tracked coordinates.

Deduplicates only identical complete pixel/time sequences. Event candidates are
computed afresh from the visible hat/head glyph, not copied from measured times.
Every board still needs actual inspection before any annotation is accepted.
"""
from pathlib import Path
import argparse
import sys
import math
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
import cv2
import numpy as np
from prepare_game_raw_review_aid import anchor
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.visual.package import read_image, load_package


def motion(points, times):
    p = np.array([[v['x'],v['y']] for v in points])
    axis = int(np.ptp(p, axis=0).argmax())
    start = next(i for i in range(1,len(p)) if np.linalg.norm(p[i]-p[0]) > 1.5)
    events = [{'type':'motion_start','indices':[start-1,start]}]
    values = p[:,axis]
    direction = 1 if values[start] > values[0] else -1
    extreme = start
    reversals = []
    for i in range(start+1,len(p)):
        delta = (values[i]-values[extreme])*direction
        if delta >= 0:
            extreme = i
        elif delta <= -1.5:
            first = next(j for j in range(extreme+1,i+1) if (values[j]-values[extreme])*direction < 0)
            reversals.append({'type':'reversal','indices':[first-1,first], 'confirmed_index':i})
            direction *= -1
            extreme = i
    events += reversals
    # Independently choose earliest sustained final bounded pose; no prediction
    # of physical zero velocity. A one-frame bracket difference is retained.
    after = max([start] + [e['confirmed_index'] for e in reversals])
    stable = next(i for i in range(after,len(p)-12)
                  if np.linalg.norm(p[i:]-p[i],axis=1).max() <= 1.5)
    for kind in ('settled','stop'):
        events.append({'type':kind,'indices':[stable-1,stable], 'confirmed_index':stable+12})
    return ('x' if axis == 0 else 'y'), events


def wall_line(image, initial):
    occupied = np.any(image != image[0,0], axis=2)
    columns = np.flatnonzero(occupied[:160].sum(axis=0) >= 60)
    if not len(columns):
        return None
    assert columns[-1]-columns[0] < 25, 'More than one wall or ambiguous geometry'
    first, last = int(columns[0]), int(columns[-1])
    x = last if initial['x'] > last else first-1
    top = int(np.where(occupied[:,(first+last)//2])[0][0])
    return {'x':float(x),'top':float(top-1),'bottom':159.,
            'method':'Independent raw long-column silhouette; image-edge center convention.'}


def separation(point, wall):
    # Declared reviewed head patch is 8x6; this is visible patch proximity,
    # not full-body contact and not an engine collision test.
    left, right = point['x']-3.5, point['x']+4.5
    top, bottom = point['y']-4., point['y']+2.
    dx = max(left-wall['x'],wall['x']-right,0.)
    dy = max(wall['top']-bottom,top-wall['bottom'],0.)
    distance = math.hypot(dx,dy)
    return {'distance_px':distance,'visually_touching':distance<=12.}


def run(review, output):
    review, output = Path(review).resolve(), Path(output).resolve()
    assert not output.exists()
    output.mkdir(parents=True)
    (output/'helper-source.py').write_bytes(Path(__file__).read_bytes())
    (output/'glyph-helper-source.py').write_bytes((ROOT/'scripts/prepare_game_raw_review_aid.py').read_bytes())
    groups, cache = {}, {}
    for folder in sorted((review/'records').iterdir()):
        source = folder/'events/images'
        manifest = load_package(source)
        key = digest([(f['sha256'],f['time_s']) for f in manifest['frames']])
        if key in groups:
            groups[key]['records'].append(folder.name)
            continue
        rows = []
        for frame in manifest['frames']:
            sha = frame['sha256']
            if sha not in cache:
                raw = source/frame['path']
                assert file_sha256(raw) == sha
                found = anchor(read_image(raw))
                if len(found) != 1:
                    raise ValueError(f'Ambiguous raw glyph: {folder.name} frame {frame["index"]}: {len(found)}')
                cache[sha] = found[0]
            rows.append({**cache[sha],'index':frame['index'],'time_s':frame['time_s'],
                         'image_sha256':sha})
        axis, events = motion(rows,[r['time_s'] for r in rows])
        first = read_image(source/manifest['frames'][0]['path'])
        wall = wall_line(first,rows[0]) if first.shape[1] == 320 else None
        distances = [separation(p,wall) for p in rows] if wall else []
        if wall:
            for i in range(1,len(rows)):
                old, new = distances[i-1]['visually_touching'], distances[i]['visually_touching']
                if old != new:
                    events.append({'type':'boundary_enter' if new else 'boundary_leave','indices':[i-1,i]})
        for event in events:
            event['time_interval_s'] = [rows[i]['time_s'] for i in event['indices']]
        # All event neighbors, observed rest supports and regular whole-clip
        # context are displayed using a fixed crop for the complete sequence.
        indexes = {0,15,29,30,60,90,120,180,239}
        for event in events:
            lo,hi = event['indices']
            indexes.update(range(max(0,lo-2),min(len(rows),hi+3)))
            if event['type'] in ('stop','settled'):
                indexes.update(range(lo,min(len(rows),event['confirmed_index']+1)))
            if 'confirmed_index' in event:indexes.add(event['confirmed_index'])
        indexes = sorted(indexes)
        # Keep x fixed as the character moves. Include wall edge and floor.
        xs = [p['x'] for p in rows]
        left = max(0,int(min(xs))-12)
        right = min(first.shape[1],int(max(xs))+15)
        if wall:
            left=min(left,max(0,int(wall['x'])-7));right=max(right,min(first.shape[1],int(wall['x'])+8))
        crop_top = max(0,int(min(p['y'] for p in rows))-12)
        crop_bottom = first.shape[0]
        scale = 2
        tw = max(95,(right-left)*scale+10)
        th = (crop_bottom-crop_top)*scale+39
        cols = max(1,min(10,1400//tw))
        board = np.full((math.ceil(len(indexes)/cols)*th,cols*tw,3),28,np.uint8)
        bindings=[]
        for j,i in enumerate(indexes):
            f = manifest['frames'][i]
            raw = read_image(source/f['path'])
            crop = cv2.resize(raw[crop_top:crop_bottom,left:right],None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
            bx,by=j%cols*tw,j//cols*th
            board[by+34:by+34+crop.shape[0],bx+4:bx+4+crop.shape[1]]=crop
            for line,text in enumerate((f'f{i} {rows[i]["time_s"]:.3f}s',f'{rows[i]["x"]:g},{rows[i]["y"]:g}')):
                cv2.putText(board,text,(bx+3,by+13+15*line),cv2.FONT_HERSHEY_SIMPLEX,.37,(240,240,240),1)
            bindings.append({'index':i,'image_sha256':f['sha256'],
                             'region':[bx+4,by+34,crop.shape[1],crop.shape[0]]})
        name=f'sequence-{len(groups):02}.png'
        cv2.imencode('.png',board)[1].tofile(output/name)
        groups[key] = {'records':[folder.name], 'sequence_sha256':key,
            'raw_source':str(source), 'axis':axis, 'points':rows, 'events':events,
            'wall':wall,'distances':distances,
            'sheet':{'file':name,'sha256':file_sha256(output/name),'frames':bindings,
                     'crop':[left,crop_top,right-left,crop_bottom-crop_top]}}
        print(folder.name,axis,len(events),len(indexes),'raw images',flush=True)
    result = {'status':'unsigned_needs_actual_visual_review','groups':list(groups.values()),
        'unique_sequences':len(groups),'raw_unique_images':len(cache),
        'raw_point_source':'Original PNG pixels only; no production position/speed/event times.',
        'acceptance':False,'helper_sha256':file_sha256(__file__)}
    write_json(output/'proposals.json',result)
    print('Unsigned sequence aids complete',len(groups),flush=True)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('review',type=Path)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.review,a.output)
