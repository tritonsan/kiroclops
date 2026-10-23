#!/usr/bin/env python
"""Build data.js for the KiroClops session replay page (index.html / render.html).

The page replays one real, logged session: the V6 web-panel run `v6p01` (16 October
2026). Nothing is re-measured here. Every number in data.js is copied from the frozen
evidence files below, parsed from the Agentic Vision write-up, or derived from them by
plain arithmetic (sums, ratios, rounding), and each derivation is asserted against the
write-up before anything is written.

Sources (the repository copy is read when it exists, otherwise the engineering folder;
absolute paths are fine in a build tool, and the generated data contains no local paths):

* AGENTIC_VISION.md                       trace table (section 2), four-window evidence
                                          table, baseline table (section 3)
* agentic-trace-v6p01/trace.json          230 audited tool calls, five jobs, diff statistics
* agentic-trace-v6p01/agent-scratch/*     scripts the agent wrote (verbatim excerpts)
* agentic-trace-v6p01/candidate-*.diff    the agent's code changes (verbatim excerpts)
* agent-baseline-posthoc-01/result.json   unchanged app vs candidates (post-hoc recomputation)
* panel-v6p01/heldout-outcome.json        56 held-out cases, 55 passed
* video/heldout/data.js                   stored OpenCV 5 handle tracks of the failing case
* session measurements (optional)         lag_s of every window of the two evaluations;
                                          without the session folder the counts already in
                                          data.js are kept

Writes data.js beside this file and copies the two videos the page embeds. Run it after
any change to the evidence; it stops with an assertion if the page data and the write-up
disagree.

Usage:  python build_data.py        (Python 3.10+, standard library only)
Override the locations with KC_HACK_ROOT, KC_SESSION and KC_REPO_ROOT if needed.
"""
import collections
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get('KC_REPO_ROOT', HERE.parents[1]))
HACK = Path(os.environ.get('KC_HACK_ROOT', 'D:/Amazon Developer Hackathon'))
SESSION = Path(os.environ.get('KC_SESSION', 'C:/kc3d/s/v6p01'))

def first_existing(*paths):
    return next((p for p in paths if p.exists()), paths[0])


# The repository copy is preferred (the published evidence); the engineering folder is the fallback.
DOC = first_existing(REPO/'submission'/'AGENTIC_VISION.md', HACK/'submission'/'AGENTIC_VISION.md')
EVID = first_existing(REPO/'submission'/'evidence'/'agentic-trace-v6p01', HACK/'submission'/'evidence'/'agentic-trace-v6p01')
TRACE = EVID/'trace.json'
SCRATCH = EVID/'agent-scratch'
BASELINE = first_existing(REPO/'evidence'/'v6-agent-baseline'/'result.json',
                          HACK/'artifacts'/'phase4-v6'/'agent-baseline-posthoc-01'/'result.json')
HELDOUT = REPO/'evidence'/'panel-v6p01'/'heldout-outcome.json'
HELDOUT_TRACKS = HACK/'submission'/'video'/'heldout'/'data.js'  # optional: kept from the previous data.js if absent
VIDEOS = {
    'good-demo-v1.mp4': HACK/'submission'/'video'/'intro'/'good-demo-v1.mp4',
    'heldout-regression.mp4': HACK/'submission'/'video'/'scenes'/'heldout-regression.mp4',
}
OUT = HERE/'data.js'

# The eight steps of section 2 of the write-up. Times are parsed from its table and
# compared with trace.json; the wording on the page is written for the page.
STEPS = [
    dict(id='prepare', tool='prepare_experiment'),
    dict(id='candidate01', tool='evaluate_candidate'),
    dict(id='reads', tool=None),
    dict(id='codes', tool=None),
    dict(id='probe', tool='evaluate_candidate'),
    dict(id='cancel', tool='cancel_job'),
    dict(id='candidate02', tool='evaluate_candidate'),
    dict(id='finalize', tool='finalize_candidate'),
]
KEY_TOOLS = ('prepare_experiment', 'evaluate_candidate', 'cancel_job', 'finalize_candidate')

# Verbatim excerpts: (file, first line, last line), 1-based, inclusive.
SCRIPT_EXCERPTS = {
    'windows.py': (15, 22),
    'track.py': (22, 31),
    'latfit.py': (26, 36),
}
DIFF_EXCERPTS = {
    'input-clock.js': ('candidate-02-src_motion_input-clock.js.diff', 4, 12),
    'panel-motion.js': ('candidate-02-src_panel-motion.js.diff', 74, 82),
    'candidate-01': ('candidate-01-src_panel-motion.js.diff', 7, 11),
}

FORBIDDEN = [
    (r'(?<![A-Za-z])[A-Za-z]:[\\/]', 'drive path'), (r'[\\/](?:Users|home)[\\/]', 'user folder'),
    (r'[\w.+-]+@[\w-]+\.[A-Za-z]{2,}', 'e-mail address'),
    (r'[\u00e7\u011f\u0131\u0130\u00f6\u015f\u00fc\u00c7\u011e\u00d6\u015e\u00dc]', 'Turkish letter'),
]


def read_text(path):
    return Path(path).read_text(encoding='utf-8').replace('\r\n', '\n')


def number_or_range(cell):
    parts = [float(p) for p in re.split(r'\s*[\u2013-]\s*', cell.strip())]
    return parts[0], (parts[1] if len(parts) > 1 else None)


def parse_trace_table(md):
    rows = []
    for line in md.split('\n'):
        m = re.match(r'^\|\s*(\d+(?:\s*\u2013\s*\d+)?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$', line)
        if m:
            t0, t1 = number_or_range(m.group(1))
            rows.append(dict(t0=t0, t1=t1, step=m.group(2), what=m.group(3)))
    return rows


def parse_window_table(md):
    pattern = re.compile(r'^\|\s*(?P<case>[^|,]+?),\s*(?P<obj>\w+),\s*(?P<win>motion-\d+):\s*'
                         r'(?P<e1>[\d.]+)\s*/\s*(?P<l1>[\d.]+)\s*\((?P<r1>[\d.]+)\)\s*\|\s*'
                         r'(?P<e2>[\d.]+)\s*/\s*(?P<l2>[\d.]+)\s*\((?P<r2>[\d.]+)\)\s*\|\s*$')
    out = []
    for line in md.split('\n'):
        m = pattern.match(line)
        if m:
            g = m.groupdict()
            row = dict(case=g['case'], object=g['obj'], window=g['win'])
            for tag, e, l, r in (('c01', 'e1', 'l1', 'r1'), ('c02', 'e2', 'l2', 'r2')):
                err, lim, ratio = float(g[e]), float(g[l]), float(g[r])
                assert abs(round(err/lim, 2) - ratio) < 0.0051, (line, err, lim, ratio)
                row[tag] = dict(error=err, limit=lim, ratio=ratio)
            out.append(row)
    return out


def new_file_lines(diff_text):
    """Line number in the new file of every diff line (None for removed and header lines)."""
    numbers, n = [], None
    for line in diff_text.split('\n'):
        m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)', line)
        if m:
            n = int(m.group(1))
            numbers.append(None)
        elif n is None or line.startswith(('---', '+++', '-')):
            numbers.append(None)
        else:
            numbers.append(n)
            n += 1
    return numbers


def excerpt(path, first, last, strip_plus=False):
    """Lines first..last (1-based, inclusive) of a file. For a diff with strip_plus the added
    lines are returned as code and numbered by their line in the new file."""
    text = read_text(path)
    lines = text.split('\n')[first-1:last]
    out = dict(first=first, last=last)
    if strip_plus:
        assert all(l.startswith('+') for l in lines), (path, first, last)
        lines = [l[1:] for l in lines]
        numbers = new_file_lines(text)[first-1:last]
        assert None not in numbers and numbers == list(range(numbers[0], numbers[0] + len(numbers)))
        out.update(first=numbers[0], last=numbers[-1])
    assert 5 <= len(lines) <= 12, (path, first, last, len(lines))
    out['text'] = '\n'.join(lines)
    return out


def diff_stat(path):
    added = removed = 0
    for line in read_text(path).split('\n'):
        if line.startswith('+') and not line.startswith('+++'):
            added += 1
        elif line.startswith('-') and not line.startswith('---'):
            removed += 1
    return added, removed


def short_detail(tool, detail):
    if isinstance(detail, list):
        detail = ' '.join(detail[1:] if detail and detail[0] == tool else detail)
    detail = (detail or '').strip()
    m = re.match(r"^\{'tool_ids': \['([^']+)'\]\}$", detail)
    if m:
        detail = 'tools ' + m.group(1)
    m = re.match(r'^case_ids \[(.*)\]', detail)
    if m:
        detail = f"partial probe, {m.group(1).count('realistic-')} cases"
    if len(detail) >= 88:
        detail = detail.rstrip() + ' \u2026'
    return detail


def probe_case(case_id):
    m = re.match(r'realistic-(v\d)-suite-panel-calibration-(.+)-(w\d+)-(r\d+)$', case_id)
    assert m, case_id
    return dict(recipe=m.group(1), family=m.group(2), width=int(m.group(3)[1:]), repeat=m.group(4))


def lag_counts(job8, previous):
    """Histogram of the measured lag (ms) over every window of an evaluation."""
    folder = next((p for p in (SESSION/'measurements').glob(job8 + '*') if p.is_dir()), None) if (SESSION/'measurements').is_dir() else None
    if folder is None or not (folder/'comparison.json').is_file():
        return previous
    counts = collections.Counter()
    for case in json.loads((folder/'comparison.json').read_text(encoding='utf-8')):
        for w in case['pixel_comparison']['object_windows']:
            counts[str(int(round(w['lag_s']*1000)))] += 1
    return dict(sorted(counts.items(), key=lambda kv: int(kv[0])), n=sum(counts.values()))


def drag_series(path):
    text = read_text(path)
    data = json.loads(text[text.index('{'):text.rindex('}')+1])
    keep = lambda t: -0.70 <= t <= 0.95
    series = {}
    for role, name in (('reference', 'reference'), ('original', 'unchanged'), ('candidate', 'candidate')):
        series[name] = [[round(t, 3), round(x, 1)] for t, x, _y in data['roles'][role]['tracks']['handle'] if x is not None and keep(t)]
    return dict(size=data['size'], window_s=[round(v, 3) for v in data['window_s']], series=series)


def main():
    doc = read_text(DOC)
    trace = json.loads(TRACE.read_text(encoding='utf-8'))
    baseline = json.loads(BASELINE.read_text(encoding='utf-8'))
    heldout = json.loads(HELDOUT.read_text(encoding='utf-8'))
    previous = {}
    if OUT.is_file():
        text = OUT.read_text(encoding='utf-8')
        try:
            previous = json.loads(text[text.index('=')+1:].strip().rstrip(';'))
        except ValueError:
            previous = {}

    # --- the audit log ---------------------------------------------------------------
    timeline = trace['timeline']
    assert trace['audit_rows'] == len(timeline) == 230 == sum(trace['tool_counts'].values())
    assert all(r['allowed'] for r in timeline)
    tools = sorted(trace['tool_counts'], key=lambda k: -trace['tool_counts'][k])
    rows = [[r['t_s'], tools.index(r['tool']), short_detail(r['tool'], r['detail'])] for r in timeline]

    jobs = {}
    for j in trace['jobs']:
        jobs[j['job_id']] = dict(id=j['job_id'], op=j['operation'], candidate=j['candidate_id'], status=j['status'],
                                 partial=j['partial'], start=j['created_s'], dur=j['duration_s'], eligible=j.get('eligible'),
                                 score=j.get('score'), cases=j.get('cases'), passed=j.get('cases_passed'), error=j.get('error'))
    by = {(j['op'], j['candidate']): j for j in jobs.values()}
    prepare, c01, c02 = by[('prepare', None)], by[('evaluate', 'candidate-01')], by[('evaluate', 'candidate-02')]
    probe, final = by[('evaluate', 'probe-01')], by[('finalize', 'candidate-02')]
    assert final['id'] == 'd19d9b76' and probe['partial'] and probe['status'] == 'cancelled'

    # --- steps: times from the write-up, checked against the log ------------------------
    table = parse_trace_table(doc)
    assert len(table) == len(STEPS) == 8, [r['step'] for r in table]
    key_rows = [r for r in timeline if r['tool'] in KEY_TOOLS]
    key_times = [r['t_s'] for r in key_rows]
    assert [r['tool'] for r in key_rows] == ['prepare_experiment', 'evaluate_candidate', 'evaluate_candidate',
                                             'cancel_job', 'evaluate_candidate', 'finalize_candidate']
    expected = [key_times[0], key_times[1], None, None, key_times[2], key_times[3], key_times[4], key_times[5]]
    steps = []
    for i, (row, spec) in enumerate(zip(table, STEPS)):
        if expected[i] is not None:
            assert abs(row['t0'] - expected[i]) <= 1.0, (spec['id'], row['t0'], expected[i])
        steps.append(dict(spec, n=i+1, t0=row['t0'], t1=row['t1'], doc=row['what'],
                          at=expected[i] if expected[i] is not None else row['t0']))
    for i in (2, 3):  # interval steps: the first and last logged call inside the quoted range
        inside = [r for r in timeline if steps[i]['t0'] - 1 <= r['t_s'] <= steps[i]['t1'] + 1]
        steps[i]['calls'] = dict(collections.Counter(r['tool'] for r in inside))
        steps[i]['span_s'] = round(steps[i]['t1'] - steps[i]['t0'])
        assert inside and abs(inside[0]['t_s'] - steps[i]['t0']) <= 1.0 and abs(inside[-1]['t_s'] - steps[i]['t1']) <= 1.0
    assert abs(c01['start'] - steps[1]['t0']) < 1.5 and abs(c02['start'] - steps[6]['t0']) < 1.5
    assert round(prepare['dur']) == 426 and round(final['dur']) == 2260 and round(probe['dur']) == 151

    # --- scores and comparisons --------------------------------------------------------
    jb = baseline['jobs']
    s01, s02 = jb['candidate-01 (calibration)']['summary'], jb['candidate-02 (calibration)']['summary']
    sh = jb['candidate-02 (held-out, after sealing)']['summary']
    assert s01['recomputed_job_score_matches'] and s02['recomputed_job_score_matches']
    assert abs(s01['candidate_score'] - c01['score']) < 1e-12 and abs(s02['candidate_score'] - c02['score']) < 1e-12
    assert abs(sh['candidate_score'] - final['score']) < 1e-12
    scores = dict(
        calibration=dict(unchanged=s02['unchanged_app_score'], c01=c01['score'], c02=c02['score'], cases=16,
                         better=dict(c01=s01['cases_candidate_better'], c02=s02['cases_candidate_better'])),
        heldout=dict(unchanged=sh['unchanged_app_score'], sealed=final['score'], cases=sh['cases'],
                     better=sh['cases_candidate_better']))
    assert s01['unchanged_app_score'] == s02['unchanged_app_score']
    for text in ('0.0502', '0.0244', '0.0067', '0.0500', '0.0121', '15 of 16', '16 of 16', '53 of 56', '3.6\u00d7'):
        assert text in doc, text
    for key, places in (('unchanged', 4), ('c01', 4), ('c02', 4)):
        assert f"{scores['calibration'][key]:.4f}" in doc
    assert f"{scores['heldout']['unchanged']:.4f}" in doc and f"{scores['heldout']['sealed']:.4f}" in doc
    assert f"{c01['score']/c02['score']:.1f}\u00d7" == '3.6\u00d7'

    windows = parse_window_table(doc)
    assert len(windows) == 4
    assert max(w['c01']['ratio'] for w in windows) == 0.85 and max(w['c02']['ratio'] for w in windows) == 0.43

    lag = dict(c01=lag_counts('ef4d46e4', previous.get('lag', {}).get('c01')),
               c02=lag_counts('6dbff94f', previous.get('lag', {}).get('c02')))
    n_windows = int(re.search(r'(\d+) windows', steps[1]['doc']).group(1))
    for tag in ('c01', 'c02'):
        assert lag[tag] is None or lag[tag]['n'] == n_windows, (tag, lag[tag], n_windows)

    # When the agent's scripts were written and when they ran on candidate 01's own data.
    on_c01 = dict(zip(('windows.py', 'track.py', 'latfit.py'), ('ef4d46e4', 'candidate-01', 'c01_track')))
    scripts = {}
    for name, needle in on_c01.items():
        written = [r['t_s'] for r in timeline if r['tool'] == 'fs_write' and str(r['detail']).startswith(name + ' (')]
        runs = [r['t_s'] for r in timeline if r['tool'] == 'execute_pwsh' and name in str(r['detail']) and needle in str(r['detail'])]
        assert written and runs, name
        scripts[name] = dict(written=min(written), on_c01=min(runs))
    assert scripts['windows.py']['written'] > c01['start'] + c01['dur'] and scripts['track.py']['written'] < c01['start'] + c01['dur']
    prepare_end = prepare['start'] + prepare['dur']
    prepare_polls = sum(1 for r in timeline if r['tool'] == 'get_job' and prepare['id'] in str(r['detail']))
    prepare_reads = sum(1 for r in timeline if r['tool'] == 'read_file' and r['t_s'] < prepare_end)
    assert (prepare_polls, prepare_reads) == (21, 5), (prepare_polls, prepare_reads)
    draft = [r['t_s'] for r in timeline if r['tool'] == 'fs_write' and str(r['detail']).startswith('input-clock.js (')]
    assert len(draft) == 2 and draft[0] < steps[3]['t0'] <= draft[1] + 1
    scripts['input-clock.js'] = dict(draft=min(draft), rewritten=max(draft))
    lead = re.search(r'^\+const RESPONSE_LEAD = ([\d.]+);', read_text(EVID/'candidate-02-src_panel-motion.js.diff'), re.M)
    response_lead_ms = round(float(lead.group(1)) * 1000, 3)

    # --- held-out outcome --------------------------------------------------------------
    assert heldout['cases'] == 56 and heldout['passed'] == 55 and heldout['session'] == 'v6p01'
    failed = [r for r in heldout['rows'] if not r['pass']]
    assert len(failed) == 1 and 'drag-release' in failed[0]['case_id'] and 'w1440' in failed[0]['case_id']
    before = {w['object']: w['before'] for w in final_failing(trace)}
    fail_windows = []
    for w in sorted(failed[0]['failing_windows'], key=lambda w: w['object']):
        fail_windows.append(dict(object=w['object'], window=w['window'], error=w['position_rmse'], limit=w['rmse_limit'],
                                 before=round(before[w['object']], 4)))
    assert ('0.78' in doc and '0.86' in doc and '0.05' in doc and '0.076' in doc and '0.005' in doc and '0.006' in doc)
    held = dict(cases=heldout['cases'], passed=heldout['passed'],
                roles={k: [v['passed'], v['cases']] for k, v in heldout['by_role'].items()},
                failed=dict(family='drag-release', width=1440, role=failed[0]['role'], windows=fail_windows),
                drag=drag_series(HELDOUT_TRACKS) if HELDOUT_TRACKS.is_file() else previous.get('heldout', {}).get('drag'))
    assert held['drag'], 'the stored handle tracks need submission/video/heldout/data.js or an existing data.js'

    # --- verbatim excerpts ---------------------------------------------------------------
    code = {name: dict(excerpt(SCRATCH/name, a, b), file=name, where='agent-scratch') for name, (a, b) in SCRIPT_EXCERPTS.items()}
    diffs = {}
    for name, (fname, a, b) in DIFF_EXCERPTS.items():
        diffs[name] = dict(excerpt(EVID/fname, a, b, strip_plus=(name != 'candidate-01')), file=fname)
    stats = {}
    for cand, files in trace['candidate_diffs'].items():
        stats[cand] = []
        for f in files:
            diff_name = f"{cand}-{f['file'].replace('/', '_')}.diff"
            a, r = diff_stat(EVID/diff_name)
            assert (a, r) == (f['added'], f['removed']), (diff_name, a, r, f)
            stats[cand].append(dict(file=f['file'], added=a, removed=r, diff=diff_name))
    assert stats['candidate-01'][0]['added'] == 3 and stats['candidate-02'][0]['added'] == 86

    probe_ids = next(j for j in trace['jobs'] if j['job_id'] == probe['id'])['case_ids']
    probe_cases = [probe_case(c) for c in probe_ids]

    data = dict(
        meta=dict(session='v6p01', date='2026-10-16', kiro='Kiro CLI 2.31.0', calls=trace['audit_rows'], windows=n_windows, response_lead_ms=response_lead_ms, prepare_polls=prepare_polls, prepare_reads=prepare_reads,
                  span_s=timeline[-1]['t_s'], audit_sha256=trace['source']['audit_sha256'],
                  repo='https://github.com/tritonsan/kiroclops', branch='main'),
        tools=[dict(id=t, count=trace['tool_counts'][t]) for t in tools],
        timeline=rows, steps=steps, jobs=sorted(jobs.values(), key=lambda j: j['start']),
        windows=windows, lag=lag, scripts=scripts, scores=scores, heldout=held, code=code, diffs=diffs, diff_stats=stats,
        probe=dict(job=probe['id'], dur=probe['dur'], cases=probe_cases, status=probe['status'], error=probe['error']),
        sources=['submission/AGENTIC_VISION.md', 'submission/evidence/agentic-trace-v6p01/trace.json',
                 'evidence/v6-agent-baseline/result.json', 'evidence/panel-v6p01/heldout-outcome.json'],
    )
    plain = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    for pattern, what in FORBIDDEN:
        hit = re.search(pattern, plain)
        assert not hit, f'{what} in generated data: {plain[max(0, hit.start()-40):hit.end()+40]!r}'
    for folder in (HACK, SESSION, REPO, HERE):  # none of the source folders may leak into the page data
        for variant in (folder.as_posix(), str(folder), str(folder).replace(chr(92), chr(92) * 2)):
            assert variant not in plain, f'source folder in generated data: {variant}'
    odd = sorted({c for c in plain if ord(c) > 126 and c not in '…–−×·→'})
    assert not odd, f'unexpected non-ASCII characters in generated data: {odd}'
    text = json.dumps(data, separators=(',', ':'), ensure_ascii=True)
    with open(OUT, 'w', encoding='utf-8', newline='\n') as fh:  # LF on every platform
        fh.write('/* Generated by build_data.py from the frozen evidence of session v6p01. Do not edit. */\n'
                 'window.REPLAY_DATA = ' + text + ';\n')
    print(f'wrote {OUT.name}: {OUT.stat().st_size} bytes, {len(rows)} tool calls, {len(steps)} steps, '
          f'{len(windows)} windows, lag {json.dumps(lag)}')
    for name, src in VIDEOS.items():
        dst = HERE/name
        if src.is_file() and (not dst.is_file() or dst.stat().st_size != src.stat().st_size):
            shutil.copyfile(src, dst)
        assert dst.is_file(), f'{name} is missing: copy it from {src.parent.name}/'
        print(f'video {name}: {dst.stat().st_size} bytes')


def final_failing(trace):
    return next(j for j in trace['jobs'] if j['job_id'] == 'd19d9b76')['failing_windows']


if __name__ == '__main__':
    sys.exit(main())
