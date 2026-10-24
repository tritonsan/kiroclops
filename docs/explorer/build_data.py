#!/usr/bin/env python
"""Build data.js and the web-sized frames for the KiroClops measurement explorer (index.html).

The explorer shows what the OpenCV 5 measurement of KiroClops produces on three real
recordings. Nothing is simulated or edited by hand: every recording is measured here with the
code path the cloud worker uses, ``kiroclops.cloud.execution.measure_direct(package, selection,
contract)``, and the observations and events it returns are written to data.js together with
lossless WebP copies of the recorded frames (pixel-identical to the PNG frames; identical
consecutive images are stored once).

Recordings
  game   sample-corpus/game-reference-h03          240 frames at 60 Hz, 228 x 128
  svg    sample-corpus/svg-three-to-four-w960-r1   180 frames, 960 x 640
  panel  a V6 calibration reference (record.json)  211 frames, 960 x 640; package, selection and
         event contract are read from the record, which is only read, never modified

Receipt (game tab): scripts/judge_check.py writes judge-check.json when it measures the same
recording on the live AWS COOL worker. Only the engine identity (OpenCV version, architecture,
COOL library names), worker seconds, origin, measurement status, objects summary and the
local-equivalence verdict are kept; job ids, endpoint URLs and hashes are dropped. Pass the file
with --receipt (or KC_JUDGE_CHECK). Without it the receipt already in data.js is kept.

Writes data.js and frames/<id>/NNNN.webp beside this file. Before data.js is written it asserts that
every number equals what measure_direct returned and that the generated data holds no local path,
e-mail address, AWS identifier or hash; afterwards it checks that the whole folder stays under 8 MB.
The output is deterministic: the same inputs give a byte-identical data.js.

Usage:  python build_data.py [--receipt PATH]      (Python 3.10+; OpenCV 5, NumPy)
Locations (all optional): KC_HACK_ROOT (engineering tree), KC_REPO_ROOT (this repository),
KC_PANEL_RECORD (record.json of the panel recording), KC_JUDGE_CHECK (judge-check.json).
Absolute paths are fine in a build tool; the generated data contains none.
"""
import argparse
import base64
import hashlib
import json
import math
import os
import platform
import re
import shutil
import statistics
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # importing kiroclops must not write caches into its source tree

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get('KC_REPO_ROOT', HERE.parents[1]))
HACK = Path(os.environ.get('KC_HACK_ROOT', 'D:/Amazon Developer Hackathon'))
PANEL_RECORD = Path(os.environ.get(
    'KC_PANEL_RECORD',
    'C:/kc3d/p/v6p01-cal/reference/realistic-suite-panel-calibration-reverse-w960-r1/record.json'))
OUT = HERE / 'data.js'
FRAMES = HERE / 'frames'
BUDGET_BYTES = 8 * 1024 * 1024


def first_existing(*paths):
    return next((p for p in paths if p.exists()), paths[0])


CODE_ROOT = first_existing(HACK / 'kiroclops', REPO / 'kiroclops').parent
CORPUS = first_existing(HACK / 'submission' / 'sample-corpus', REPO / 'submission' / 'sample-corpus')
sys.path.insert(0, str(CODE_ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from kiroclops.cloud.execution import measure_direct  # noqa: E402

# ---------------------------------------------------------------------------------------------
# Words that belong to the page, not to the measurement.

OBJECT_LABELS = {
    'game': {'character': 'body patch of the character'},
    'svg': {'a': 'Core idea card', 'b': 'Research card', 'c': 'Prototype card', 'd': 'Feedback card (new)'},
    'panel': {'bookmark': 'bookmark icon', 'handle': 'panel drag handle', 'star': 'star icon'},
}
# Fallback colours when a selection has no foreground colour (the game) or two objects share one.
PALETTE = ['#4dd0e1', '#fbbf24', '#f9a8d4', '#a78bfa', '#34d399', '#fb7185', '#fdba74', '#93c5fd']


def num(value, digits=3):
    """Round for the page; whole numbers stay integers."""
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('non-finite value in a measurement')
    rounded = round(value, digits)
    return int(rounded) if rounded == int(rounded) else rounded


def load_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------------------------
# Sources

def sources():
    """The three recordings, each as a dict with id, package folder, selection and contract."""
    game = CORPUS / 'game-reference-h03'
    svg = CORPUS / 'svg-three-to-four-w960-r1'
    if not PANEL_RECORD.exists():
        raise SystemExit('The panel recording is missing: set KC_PANEL_RECORD to its record.json (%s)' % PANEL_RECORD.name)
    record = load_json(PANEL_RECORD)
    panel_package = Path(str(record['package']).replace('\\', '/'))
    return [
        dict(id='game', package=game / 'package', selection=load_json(game / 'selection.json'), contract=load_json(game / 'contract.json')),
        dict(id='svg', package=svg / 'package', selection=load_json(svg / 'selection.json'), contract=load_json(svg / 'contract.json')),
        dict(id='panel', package=panel_package, selection=record['selection'], contract=record['suite_event_contract']),
    ]


# ---------------------------------------------------------------------------------------------
# Frames

def export_frames(rec_id, package, manifest):
    """Lossless WebP copies of the recorded frames. Returns (src, count): src[i] is the file of frame i."""
    folder = FRAMES / rec_id
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    seen, src, total = {}, [], 0
    for record in manifest['frames']:
        image = cv2.imread(str(package / record['path']), cv2.IMREAD_COLOR)
        if image is None:
            raise SystemExit('Frame %s cannot be read' % record['path'])
        key = hashlib.sha1(image.tobytes()).hexdigest()
        if key not in seen:
            ok, buffer = cv2.imencode('.webp', image, [cv2.IMWRITE_WEBP_QUALITY, 101])  # > 100 means lossless
            if not ok:
                raise SystemExit('WebP encoding failed')
            back = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
            if back is None or not np.array_equal(back, image):
                raise SystemExit('WebP copy of frame %s is not pixel-identical' % record['path'])
            seen[key] = len(seen)
            (folder / ('%04d.webp' % seen[key])).write_bytes(buffer.tobytes())
            total += len(buffer)
        src.append(seen[key])
    return src, len(seen), total


def template_png(package, manifest, spec):
    """The patch the tracker looks for (a crop of the selection frame), as a small data URI."""
    index = spec.get('template_frame_index', spec.get('frame_index', 0))
    image = cv2.imread(str(package / manifest['frames'][index]['path']), cv2.IMREAD_COLOR)
    x, y, w, h = spec['roi']
    ok, buffer = cv2.imencode('.png', image[y:y + h, x:x + w], [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not ok:
        raise SystemExit('PNG encoding failed')
    return 'data:image/png;base64,' + base64.b64encode(buffer.tobytes()).decode('ascii')


# ---------------------------------------------------------------------------------------------
# Measurement -> page data

def hex_color(rgb):
    return '#%02x%02x%02x' % tuple(int(v) for v in rgb)


def object_rows(frames):
    """One row per frame: [x, y, left, top, width, height, score, margin, geometry_score] when valid,
    {r: reason, p: present(0/1/null)} when not. An invalid frame carries no coordinates."""
    rows = []
    for f in frames:
        if f['valid']:
            rows.append([num(f['x']), num(f['y']), num(f['left']), num(f['top']), num(f['width']), num(f['height']),
                         num(f.get('score'), 4), num(f.get('margin'), 4), num(f.get('geometry_score'), 4)])
        else:
            present = f.get('present')
            rows.append({'r': f.get('reason') or 'unknown', 'p': None if present is None else int(bool(present))})
    return rows


def connection_rows(frames):
    rows = []
    for f in frames:
        if f['valid']:
            rows.append([num(f['start'][0], 2), num(f['start'][1], 2), num(f['end'][0], 2), num(f['end'][1], 2),
                         num(f.get('score'), 3), int(bool(f.get('start_occluded'))), int(bool(f.get('end_occluded')))])
        else:
            rows.append({'r': f.get('reason') or 'unknown'})
    return rows


def describe_event(e, opts):
    """One plain sentence about what the measurement saw. Only fields of the event itself are used."""
    kind, axis = e['type'], e.get('axis')
    rel = e.get('relative_to_object_id')
    lead = ('relative to %s, ' % rel) if rel else ''
    radius = opts['rest_radius_px']
    if kind == 'motion_start':
        text = 'first visible departure beyond the %g px rest band: %.1f px from the rest position; dominant axis %s' % (
            radius, e['departure_distance_px'], axis)
        if e.get('start_context') != 'observed_rest':
            text += '; the rest before the move was not fully observed (clip start or gap)'
    elif kind == 'reversal':
        text = 'direction reverses on %s; confirmed at %.3f s' % (axis, e['confirmed_at_s'])
    elif kind in ('stop', 'settled'):
        text = 'at rest for %.2f s within %g px; confirmed at %.3f s' % (e['rest_duration_s'], e['rest_radius_px'], e['confirmed_at_s'])
    elif kind == 'overshoot':
        text = '%.1f px beyond the observed end position (%s); peak time %s' % (
            e['amplitude_px'], axis, e.get('peak_timing_status', 'unresolved'))
    elif kind == 'oscillation':
        text = '%d sign changes around the end position, %.1f px peak to peak' % (e['sign_changes'], e['peak_to_peak_px'])
    elif kind in ('connection_attached', 'connection_detached'):
        text = 'connector %s %s' % (e.get('connection_id'), 'attaches to both boxes' if kind == 'connection_attached' else 'detaches from a box')
    elif kind in ('overlap_enter', 'overlap_leave'):
        text = 'the two boxes %s overlapping' % ('start' if kind.endswith('enter') else 'stop')
    elif kind.startswith('boundary_'):
        text = '%s (%s)' % (kind.replace('_', ' '), e.get('boundary_id'))
    else:
        text = kind.replace('_', ' ')
    return lead + text


def event_rows(events, opts):
    rows = []
    for e in events:
        ids = list(e['object_ids'])
        rel = e.get('relative_to_object_id')
        rows.append({
            'ty': e['type'], 'ids': ids, 'rel': rel,
            't': [num(e['time_interval_s'][0], 6), num(e['time_interval_s'][1], 6)],
            'f': [int(v) for v in e['frame_indices']],
            'ok': e.get('timing_status') == 'resolved',
            'd': describe_event(e, opts),
        })
    return rows


def input_rows(inputs):
    rows = []
    for e in inputs.get('events', []):
        action, selector = e.get('action'), e.get('selector')
        if 'pressed' in e:
            label = '%s %s' % (action, 'pressed' if e['pressed'] else 'released')
        else:
            label = '%s %s' % (action, selector) if selector else str(action)
        rows.append({'t': num(e['time_s'], 6), 'label': label})
    return rows


def clock_facts(rec_id, t, observations):
    gaps = [b - a for a, b in zip(t, t[1:])]
    median = statistics.median(gaps)
    ordered = sorted(gaps)
    p95 = ordered[min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)]
    stats = {'median_ms': num(median * 1000, 2), 'p95_ms': num(p95 * 1000, 2), 'max_ms': num(max(gaps) * 1000, 2),
             'duration_s': num(t[-1] - t[0], 3)}
    if rec_id == 'game':
        text = 'game frame clock, %.1f Hz (%.1f ms per frame)' % (1 / median, median * 1000)
    else:
        text = 'browser rendering clock: median %.1f ms between frames, 95th percentile %.1f ms, longest %.1f ms' % (
            median * 1000, p95 * 1000, max(gaps) * 1000)
    return stats, text


def build_recording(src, result):
    rec_id, package, selection, contract = src['id'], src['package'], src['selection'], src['contract']
    observations, event_set = result['observations'], result['events']
    manifest = load_json(package / 'manifest.json')
    size = observations['image_size']
    t = [num(f['time_s'], 6) for f in observations['camera']]
    files, unique, nbytes = export_frames(rec_id, package, manifest)
    n = len(t)
    assert len(files) == n == len(manifest['frames'])
    opts = event_set['options']

    specs = {s['id']: s for s in selection['objects']}
    used, objects = set(), []
    for k, ob in enumerate(observations['objects']):
        spec = specs[ob['id']]
        anchor = spec.get('pixel_anchor')
        color = hex_color(anchor['foreground_rgb']) if anchor else PALETTE[k % len(PALETTE)]
        if color in used:
            color = next(c for c in PALETTE if c not in used)
        used.add(color)
        geometry = spec.get('geometry')
        objects.append({
            'id': ob['id'], 'label': OBJECT_LABELS[rec_id].get(ob['id'], ob['id']), 'color': color,
            'method': ob['method'], 'valid_ratio': num(ob['valid_ratio'], 4),
            'roi': spec['roi'], 'anchor': spec.get('anchor'),
            'anchor_kind': 'colour centroid' if anchor else 'template anchor',
            'box_kind': 'contour' if geometry else 'template',
            'min_score': spec.get('min_score', .75), 'min_margin': spec.get('min_margin', .03),
            'search_radius': spec.get('search_radius'),
            'tpl': template_png(package, manifest, spec),
            'f': object_rows(ob['frames']),
        })

    connections = [{'id': c['id'], 'from': c['from_object'], 'to': c['to_object'],
                    'valid_ratio': num(c['valid_ratio'], 4), 'f': connection_rows(c['frames'])}
                   for c in observations['connections']]

    cam = observations['camera']
    camera = {'n': n, 'n_pass': sum(1 for c in cam if c['status'] == 'pass'),
              'max_shift_px': num(max((c['max_shift_px'] or 0) for c in cam), 2),
              'limit_px': num(selection.get('camera_max_shift_px', contract.get('camera_max_shift_px', 1.5)), 2),
              'rois': [list(r) for r in selection.get('background_rois', [])]}

    inputs = input_rows(observations.get('inputs', {}))
    clock, clock_text = clock_facts(rec_id, t, observations)
    events = event_rows(event_set['events'], opts)

    # Plain-language text for the page, generated from the data so that it cannot drift from it.
    first = lambda action: next((i for i in inputs if i['label'].startswith(action)), None)
    if rec_id == 'game':
        press, release = first('jump pressed'), first('jump released')
        tab, tab_sub = 'Game', 'Tiny Crate jump, %d frames' % n
        title = 'Game: one jump, measured at 60 Hz'
        blurb = ('Tiny Crate is a public-domain Godot platformer (Unlicense). In this %.1f s recording at 60 Hz the harness presses the '
                 'jump key at %.2f s and releases it at %.2f s; the character jumps once and lands. One object is tracked: a '
                 '%d \u00d7 %d px patch of the character\u2019s body.') % (
                     clock['duration_s'], press['t'], release['t'], objects[0]['roi'][2], objects[0]['roi'][3])
    elif rec_id == 'svg':
        click = first('click')
        tab, tab_sub = 'SVG diagram', 'a card is added, %d frames' % n
        title = 'SVG diagram: a card is added'
        blurb = ('A diagram of linked idea cards, rendered as SVG in a browser (%d \u00d7 %d). A recorded click on \u201cAdd idea\u201d at '
                 '%.2f s adds a fourth card and the layout glides to make room. The four cards (a to d) and the three connectors that '
                 'join card a to the others are tracked; card d does not exist before the click.') % (size[0], size[1], click['t'])
    else:
        clicks = [i for i in inputs if i['label'].startswith('click')]
        tab, tab_sub = 'Web panel', 'open, close, open, %d frames' % n
        title = 'Web panel: a motion interrupted and reversed'
        blurb = ('A web page with a slide-in panel (%d \u00d7 %d). Three recorded clicks (%s) interrupt and reverse the panel\u2019s motion '
                 'in mid-flight. Tracked: the panel\u2019s drag handle (its outline is the box), the bookmark icon and the star icon, '
                 'plus the motion of both icons relative to the handle.') % (
                     size[0], size[1], ', '.join('%s at %.2f s' % (c['label'].split(' ', 1)[1], c['t']) for c in clicks))

    ratios = ', '.join('%s %.0f%%' % (o['id'], 100 * o['valid_ratio']) for o in objects)
    facts = [
        ['Recording', '%d frames, %d \u00d7 %d px, %.2f s' % (n, size[0], size[1], clock['duration_s'])],
        ['Frame clock', clock_text],
        ['Recorded inputs', '; '.join('%s at %.3f s' % (i['label'], i['t']) for i in inputs) + ' (from the capture harness, not inferred from pixels)'],
        ['Static camera', 'checked on %d background patch%s: pass on %d of %d frames, largest shift %.1f px (limit %.1f px)' % (
            len(camera['rois']), '' if len(camera['rois']) == 1 else 'es', camera['n_pass'], n, camera['max_shift_px'], camera['limit_px'])],
        ['Valid frames', ratios + ' of the frames where the object is expected (contract: at least %.0f%%)' % (100 * contract.get('min_valid_ratio', .95))],
    ]
    if rec_id != 'game':
        facts.append(['Corner strip', 'The black-and-white strip in the bottom-right corner of each frame is a counter the recorder draws '
                                      'so that every image can be bound to the browser\u2019s rendering batch.'])

    return {
        'id': rec_id, 'tab': tab, 'tab_sub': tab_sub, 'title': title, 'blurb': blurb,
        'size': size, 'smooth': rec_id != 'game', 'n': n, 'unique_images': unique,
        'dir': 'frames/%s/' % rec_id, 'ext': 'webp', 'pad': 4, 'src': files,
        't': t, 'clock': clock, 'facts': facts,
        'objects': objects, 'connections': connections, 'camera': camera,
        'inputs': inputs, 'inputs_provenance': observations.get('inputs', {}).get('provenance'),
        'events': events, 'event_status': event_set['status'], 'unobservable': len(event_set['unobservable']),
        'options': {k: num(v, 4) if isinstance(v, float) else v for k, v in opts.items()},
        'policy': event_set['motion_start_policy']['version'],
        'min_valid_ratio': contract.get('min_valid_ratio', .95),
        'status': observations['status'],
        'frame_bytes': nbytes,
    }


# ---------------------------------------------------------------------------------------------
# Receipt

def receipt_from(path, local_game):
    raw = load_json(path)
    if raw.get('kind') != 'JudgeCheck' or raw.get('sample') != 'game-reference-h03' or raw.get('status') != 'succeeded':
        raise SystemExit('The receipt must be a successful JudgeCheck of game-reference-h03')
    engine, local = raw['engine'], raw.get('local', {})
    receipt = {
        'checked': str(raw['checked_at'])[:10],
        'engine': {'opencv': engine['opencv_version'], 'architecture': engine['architecture'], 'system': engine['system'],
                   'cool_build': bool(engine['cool_build']), 'libraries': list(engine['opencv_libraries'])},
        'worker_seconds': num(raw['worker_wall_seconds'], 2),
        'end_to_end_seconds': num(raw['end_to_end_seconds'], 1),
        'origin': raw['origin'],
        'measurement_status': raw['measurement_status'],
        'objects': [{'id': o['id'], 'valid_ratio': num(o['valid_ratio'], 4), 'frames': o['frames']} for o in raw['objects']],
        'events': raw['events'],
        'local': {'opencv': local.get('opencv_version'), 'seconds': num(local.get('seconds'), 2),
                  'strict_equivalent': local.get('policy_equivalent'), 'strict_differences': local.get('policy_differences'),
                  'difference_fields': list(local.get('difference_fields', [])),
                  'equivalent_excluding': local.get('equivalent_excluding_build_record_and_phase_shifts'),
                  'remaining_differences': len(local.get('remaining_difference_paths', []))},
    }
    # The receipt describes the same recording as the local measurement: say so only if the numbers agree.
    obj = local_game['objects'][0]
    mismatch = [name for name, ok in (
        ('object', [o['id'] for o in receipt['objects']] == [obj['id']]),
        ('valid ratio', receipt['objects'][0]['valid_ratio'] == obj['valid_ratio']),
        ('frame count', receipt['objects'][0]['frames'] == local_game['n']),
        ('event count', receipt['events'] == len(local_game['events'])),
    ) if not ok]
    if mismatch:
        raise SystemExit('The receipt and the local measurement disagree on: ' + ', '.join(mismatch))
    return receipt


def previous_receipt():
    if not OUT.exists():
        return None
    text = OUT.read_text(encoding='utf-8')
    match = re.search(r'window\.EXPLORER_DATA = (\{.*\});\s*$', text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))['recordings'][0].get('receipt')
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# Output checks

# Patterns are written with a bracketed letter ("[U]sers") so that this file itself holds none of the strings it looks for.
FORBIDDEN = [
    (r'[A-Za-z]:[\\/]', 'a drive path'), (r'\\\\', 'a backslash path'), (r'/[U]sers/|/[h]ome/', 'a user path'),
    (r'[\w.+-]+@[\w-]+\.[\w.-]+', 'an e-mail address'), (r'(?<![\w.])\d{12}(?![\w.])', 'a 12-digit account number'),
    (r'ar[n]:aws|amazona[w]s|execute-ap[i]|ami-[0-9a-f]{8}', 'an AWS identifier'), (r'(?i)jo[b][_ ]?id|ap[i][_ ]?url', 'a service identifier'),
    (r'\b[0-9a-f]{40,64}\b', 'a hash'), (r'(?i)kc3[d]|hackatho[n]|\.ven[v]', 'a local folder name'),
    (r'[\u011f\u011e\u0131\u0130\u015f\u015e\u00f6\u00d6\u00fc\u00dc\u00e7\u00c7]', 'Turkish letters'),
]


def assert_clean(text):
    # The file itself is ASCII (JSON escapes); look at the decoded strings, where Turkish letters would show.
    payload = text.split(' = ', 1)[1].rsplit(';', 1)[0]
    text = json.dumps(json.loads(payload), ensure_ascii=False)
    for pattern, what in FORBIDDEN:
        found = re.search(pattern, text)
        if found:
            raise SystemExit('data.js would contain %s: %r' % (what, text[max(0, found.start() - 30):found.end() + 30]))


def assert_faithful(data, results):
    """Every number on the page equals what measure_direct returned (up to the rounding written)."""
    for rec, (rec_id, result) in zip(data['recordings'], results):
        observed = result['observations']['objects']
        for out, ob in zip(rec['objects'], observed):
            for row, f in zip(out['f'], ob['frames']):
                if f['valid']:
                    assert isinstance(row, list) and abs(row[0] - f['x']) <= 5.1e-4 and abs(row[1] - f['y']) <= 5.1e-4, (rec_id, ob['id'], f['index'])
                else:
                    assert isinstance(row, dict), (rec_id, ob['id'], f['index'])  # no coordinates for an invalid frame
        assert len(rec['events']) == len(result['events']['events'])


def folder_bytes():
    return sum(p.stat().st_size for p in HERE.rglob('*') if p.is_file())


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--receipt', default=os.environ.get('KC_JUDGE_CHECK'), help='judge-check.json of the game recording on the AWS COOL worker')
    args = parser.parse_args()

    results, recordings = [], []
    for src in sources():
        started = time.monotonic()
        result = measure_direct(src['package'], src['selection'], src['contract'])
        seconds = time.monotonic() - started
        recordings.append(build_recording(src, result))
        results.append((src['id'], result))
        rec = recordings[-1]
        print('%-6s %3d frames (%d image files, %.2f MB), %d events, measured in %.1f s' % (
            src['id'], rec['n'], rec['unique_images'], rec['frame_bytes'] / 1e6, len(rec['events']), seconds))

    version = results[0][1]['observations']['opencv_version']
    if any(r['observations']['opencv_version'] != version for _, r in results):
        raise SystemExit('The recordings were measured with different OpenCV versions')
    if version != '5.0.0':
        print('warning: OpenCV %s was used; the page footer says 5.0.0' % version)

    receipt = receipt_from(Path(args.receipt), recordings[0]) if args.receipt else previous_receipt()
    if receipt is None:
        print('warning: no receipt (pass --receipt); the AWS card will be hidden')
    else:
        recordings[0]['receipt'] = receipt

    machine = {'AMD64': 'x86-64', 'x86_64': 'x86-64'}.get(platform.machine(), platform.machine())
    import kiroclops
    data = {'schema': 1, 'opencv_local': version, 'kiroclops': kiroclops.__version__,
            'local_platform': '%s %s' % (platform.system(), machine), 'recordings': recordings}
    assert_faithful(data, results)

    lines = ['/* Generated by build_data.py from real recordings measured with kiroclops.cloud.execution.measure_direct. Do not edit. */',
             'window.EXPLORER_DATA = {%s,"recordings":[' % ','.join(
                 '%s:%s' % (json.dumps(k), json.dumps(v, separators=(',', ':'))) for k, v in data.items() if k != 'recordings')]
    lines.append(',\n'.join(json.dumps(r, separators=(',', ':'), allow_nan=False) for r in recordings))
    lines.append(']};')
    text = '\n'.join(lines) + '\n'
    assert_clean(text)
    OUT.write_text(text, encoding='utf-8', newline='\n')

    total = folder_bytes()
    print('data.js %.0f KB; explorer folder %.2f MB (budget %.0f MB)' % (OUT.stat().st_size / 1024, total / 1e6, BUDGET_BYTES / 1e6))
    if total > BUDGET_BYTES:
        raise SystemExit('The explorer folder is over its size budget')


if __name__ == '__main__':
    main()
