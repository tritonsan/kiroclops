import json, sys
path = sys.argv[1]
d = json.load(open(path))
r = d['result']
print('score', r.get('score'), 'eligible', r.get('eligible'))
for c in r['comparisons']:
    cid = c['case_id'][-34:]
    pc = c['pixel_comparison']
    opc = c.get('original_pixel_comparison') or {}
    orig = {}
    for w in (opc.get('object_windows') or []):
        orig[(w['reference_id'], w['segment_index'], tuple(w['input_timing']['requested_window_s']) if w.get('input_timing') else None)] = w
    owins = opc.get('object_windows') or []
    line = []
    for i, w in enumerate(pc['object_windows']):
        ow = owins[i] if i < len(owins) else None
        o = w['input_timing']['alignment']['ordinal'] if w.get('input_timing') else '?'
        line.append('%s%s e%.3f/%.3f lag%+.2f@%.3f%s' % (
            w['reference_id'][0], o, w['position_rmse'], w['limits']['rmse_limit'], w['lag_s'], w['lag_position_rmse'],
            (' (o%.3f lag%+.2f)' % (ow['position_rmse'], ow['lag_s'])) if ow else ''))
    print(cid, 'score %.4f' % c['score'])
    print('   ', ' | '.join(line))
