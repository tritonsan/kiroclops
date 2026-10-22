import json, math, sys, os
cases = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ref.json')))

def spring(x0, v0, goal, dt, k, c):
    if dt <= 0: return x0, v0
    d0 = x0 - goal
    w0 = math.sqrt(k); z = c / (2 * w0)
    if z < 1 - 1e-6:
        a = z * w0; wd = w0 * math.sqrt(1 - z * z)
        e = math.exp(-a * dt); cs = math.cos(wd * dt); sn = math.sin(wd * dt)
        d = e * (d0 * cs + ((v0 + a * d0) / wd) * sn)
        v = e * (v0 * cs - ((a * v0 + w0 * w0 * d0) / wd) * sn)
    elif z > 1 + 1e-6:
        r = w0 * math.sqrt(z * z - 1); r1 = -z * w0 + r; r2 = -z * w0 - r
        c1 = (v0 - r2 * d0) / (r1 - r2); c2 = d0 - c1
        e1 = math.exp(r1 * dt); e2 = math.exp(r2 * dt)
        d = c1 * e1 + c2 * e2; v = c1 * r1 * e1 + c2 * r2 * e2
    else:
        e = math.exp(-w0 * dt); b = v0 + w0 * d0
        d = e * (d0 + b * dt); v = e * (v0 - w0 * b * dt)
    return goal + d, v

def ref_series(c):
    o = c['objs']
    h = o['handle']; b = o['bookmark']; s = o['star']
    x0 = 66.5 if c['viewport'][0] < 1100 else 82.5
    P = [(t, x - x0) for t, x, y in h]
    T = [(t, y - 406.45454999432087) for t, x, y in b]
    C = [(t, y - 496.56) for t, x, y in s]
    return P, T, C

def span(c):
    w = c['viewport'][0]
    if w >= 1100: tw = w - 80 - 2
    else: tw = w - 48 - 2
    return tw - 260 - 76 - 12

if __name__ == '__main__':
    for c in cases:
        P, T, C = ref_series(c)
        print(c['id'][-38:], 'span', span(c), 'clicks', [round(t, 3) for _, t in c['clicks']])
        # print a compact table every ~2nd frame during motion
        rows = []
        for i in range(len(P)):
            t = P[i][0]
            if t < c['clicks'][0][1] - 0.05 or t > c['clicks'][-1][1] + 1.0: continue
            rows.append('%.3f:%.1f/%.1f/%.1f' % (t, P[i][1], T[i][1], C[i][1]))
        print(' '.join(rows))
        if len(sys.argv) > 1: break
