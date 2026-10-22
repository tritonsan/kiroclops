import math, sys, json
from sim import cases, spring, ref_series, span

KEYS = ['pk', 'pc', 'tk', 'tc', 'tod', 'tcd', 'ck', 'cc', 'cod', 'ccd']
PARENT = dict(pk=159, pc=17.5, tk=229, tc=20.8, tod=0.08, tcd=0.12, ck=198, cc=18.7, cod=0.16, ccd=0.04)

def simulate(c, prm, times, lats):
    S = span(c)
    evs = [(t + lats[j], sel == '#open-panel') for j, (sel, t) in enumerate(c['clicks'])]
    out = []
    st = dict(x=0.0, v=0.0, ty=24.0, tv=0.0, cy=40.0, cv=0.0, open=False, el=1e9)
    def adv(dt):
        if dt <= 0: return
        o = st['open']
        st['x'], st['v'] = spring(st['x'], st['v'], S if o else 0.0, dt, prm['pk'], prm['pc'])
        hold = max(0.0, prm['tod' if o else 'tcd'] - st['el'])
        if hold < dt: st['ty'], st['tv'] = spring(st['ty'], st['tv'], 0.0 if o else 24.0, dt - hold, prm['tk'], prm['tc'])
        hold = max(0.0, prm['cod' if o else 'ccd'] - st['el'])
        if hold < dt: st['cy'], st['cv'] = spring(st['cy'], st['cv'], 0.0 if o else 40.0, dt - hold, prm['ck'], prm['cc'])
        st['el'] += dt
    tcur = 0.0; ei = 0
    for T in times:
        while ei < len(evs) and evs[ei][0] <= T:
            adv(evs[ei][0] - tcur); tcur = evs[ei][0]
            if evs[ei][1] != st['open']:
                st['open'] = evs[ei][1]; st['el'] = 0.0
            ei += 1
        adv(T - tcur); tcur = T
        out.append((st['x'], st['ty'], st['cy']))
    return out

def huber(e, d=3.0):
    a = abs(e)
    return e * e if a < d else 2 * d * a - d * d

W = (1.0, 8.0, 8.0)  # weight title/content errors higher (smaller travel)

def case_loss(c, prm, lats, upto=None, frm=None):
    P, T, C = ref_series(c)
    times = [p[0] for p in P]
    sim = simulate(c, prm, times, lats)
    L = 0.0
    for i, t in enumerate(times):
        if t < c['clicks'][0][1] - 0.03: continue
        if upto is not None and t >= upto: continue
        if frm is not None and t < frm: continue
        L += W[0] * huber(sim[i][0] - P[i][1]) + W[1] * huber(sim[i][1] - T[i][1], 0.5) + W[2] * huber(sim[i][2] - C[i][1], 0.5)
    return L

def fit_lats(prm, lats_all):
    grid = [x / 1000.0 for x in range(-20, 9)]
    for ci, c in enumerate(cases):
        lats = lats_all[ci]
        ev = [t for _, t in c['clicks']] + [99]
        for j in range(len(c['clicks'])):
            best = None
            for g in grid:
                lats[j] = g
                L = case_loss(c, prm, lats, upto=ev[j + 1] + 0.05, frm=ev[j] - 0.03)
                if best is None or L < best[0]: best = (L, g)
            lats[j] = best[1]
    return lats_all

def total(prm, lats_all):
    return sum(case_loss(c, prm, lats_all[ci]) for ci, c in enumerate(cases))

def fit_params(prm, lats_all, keys, rounds=3):
    steps = {k: (abs(prm[k]) * 0.04 if not k.endswith('d') else 0.008) for k in keys}
    cur = total(prm, lats_all)
    for r in range(rounds):
        for k in keys:
            improved = True
            while improved:
                improved = False
                for sgn in (1, -1):
                    old = prm[k]; prm[k] = old + sgn * steps[k]
                    L = total(prm, lats_all)
                    if L < cur - 1e-9:
                        cur = L; improved = True; break
                    prm[k] = old
            steps[k] *= 0.5
    return prm, cur

if __name__ == '__main__':
    prm = dict(PARENT)
    lats_all = [[-0.002] * len(c['clicks']) for c in cases]
    print('init', total(prm, lats_all))
    lats_all = fit_lats(prm, lats_all)
    base = total(prm, lats_all)
    print('parent with fitted lats', base)
    for it in range(2):
        prm, L = fit_params(prm, lats_all, KEYS, rounds=3)
        print('iter', it, 'params', {k: round(prm[k], 4) for k in KEYS}, 'loss', L)
        lats_all = fit_lats(prm, lats_all)
        print('  after lat refit', total(prm, lats_all))
    # parent again with the final lats
    print('parent w/ final lats', total(dict(PARENT), lats_all))
    json.dump({'prm': prm, 'lats': lats_all}, open('fit3_out.json', 'w'))
    for ci, c in enumerate(cases):
        print(c['id'][-34:], [round(l * 1000, 1) for l in lats_all[ci]])
