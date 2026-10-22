"""Track bookmark (cyan) and star (yellow) colour-mass centroids in packaged frames.
Usage: python track.py <package-root> <out.json>
"""
import json, os, sys
import numpy as np
from PIL import Image

root = sys.argv[1]
out = sys.argv[2]
res = []
for case in sorted(os.listdir(root)):
    pdir = os.path.join(root, case)
    mf = os.path.join(pdir, 'manifest.json')
    if not os.path.exists(mf): continue
    m = json.load(open(mf))
    clicks = [(e['selector'], e['time_s']) for e in m['inputs']['events']]
    rows = []
    for f in m['frames']:
        im = np.asarray(Image.open(os.path.join(pdir, f['path'])).convert('RGB')).astype(np.int16)
        # restrict to the track band (y 300..600)
        band = im[300:620]
        def centroid(rgb, tol=40):
            d = np.abs(band - np.array(rgb, dtype=np.int16)).sum(axis=2)
            w = np.clip(1.0 - d / float(tol), 0, 1)
            s = w.sum()
            if s < 20: return None
            ys, xs = np.mgrid[0:band.shape[0], 0:band.shape[1]]
            return float((w * xs).sum() / s), float((w * ys).sum() / s + 300)
        b = centroid((77, 208, 225))
        s = centroid((255, 209, 102))
        rows.append((f['time_s'], b, s))
    res.append({'id': case, 'clicks': clicks, 'rows': rows})
    print(case, len(rows), clicks)
json.dump(res, open(out, 'w'))
