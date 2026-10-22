import json, math, sys
from fit3 import simulate, PARENT, huber
from sim import span

src = sys.argv[1] if len(sys.argv) > 1 else 'before_track.json'
data = json.load(open(src))
ref = {c['id']: c for c in json.load(open('ref.json'))}

def series(c):
    rows = [r for r in c['rows'] if r[1] and r[2]]
    bx0, by0 = rows[0][1]; sx0, sy0 = rows[0][2]
    P = [(t, b[0] - bx0) for t, b, s in rows]
    T = [(t, b[1] - (by0 - 24.0)) for t, b, s in rows]
    C = [(t, s[1] - (sy0 - 40.0)) for t, b, s in rows]
    return P, T, C

prm = dict(PARENT)
grid = [x / 1000.0 for x in range(-24, 12)]
allL = []
for c in data:
    cc = {'id': c['id'], 'viewport': ref[c['id']]['viewport'], 'clicks': c['clicks']}
    P, T, C = series(c)
    times = [p[0] for p in P]
    ev = [t for _, t in c['clicks']] + [99]
    lats = [-0.002] * len(c['clicks'])
    for j in range(len(c['clicks'])):
        best = None
        for g in grid:
            lats[j] = g
            sim = simulate(cc, prm, times, lats)
            L = 0
            for i, t in enumerate(times):
                if t < ev[j] - 0.03 or t >= ev[j + 1] + 0.05: continue
                L += huber(sim[i][0] - P[i][1]) + 8 * huber(sim[i][1] - T[i][1], 0.5) + 8 * huber(sim[i][2] - C[i][1], 0.5)
            if best is None or L < best[0]: best = (L, g)
        lats[j] = best[1]
    sim = simulate(cc, prm, times, lats)
    e = [sim[i][0] - P[i][1] for i, t in enumerate(times) if t >= ev[0] - 0.03]
    s = sorted(abs(x) for x in e)
    allL += lats
    print(c['id'][-34:], [round(l * 1000, 1) for l in lats], 'P rms %.2f p90 %.2f' % (math.sqrt(sum(x * x for x in e) / len(e)), s[int(0.9 * len(s))]))
print('mean lat ms', 1000 * sum(allL) / len(allL), 'min', 1000 * min(allL), 'max', 1000 * max(allL))
