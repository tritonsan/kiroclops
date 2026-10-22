"""V4 final game raw review logistics: deduplicated batches, a fixed random sample, verdict merging.

Post-hoc review logistics (10 October 2026), not a review. This tool judges no image.
It groups pages, writes the lists that real reviewers open, fixes the Sonnet re-check
sample before any verdict exists, and merges the verdict files the reviewers wrote.

Grouping (user decision: identical pages are inspected once):

- zoom pages are grouped by exact bytes (sha256, re-hashed here);
- contact sheets are grouped by identical pixels outside the per-tile label strip.
  The label names the record; the frame images, markers and boxes are all below it.

The two kinds are grouped separately: one zoom page can mirror sheets whose frames
differ outside the ±18 px crop.

First pass: Haiku 5.5 opens the representative image of every group. Second pass:
Sonnet 5.5 re-opens every group Haiku did not call ok, plus the fixed random sample.
Where Sonnet looked, its verdict is the final one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from kiroclops.visual.package import read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json

# suite_generic_raw_review._tile: 230-px tiles, label text above y=27, frame images from y=27.
TILE_HEIGHT, LABEL_HEIGHT = 230, 27
KINDS = ('zoom', 'sheet')
VERDICTS = ('ok', 'anomaly', 'uncertain')
SAMPLE_FRACTION = .10


def _need(condition, message, code='invalid_visual_plan'):
    if not condition:
        raise VisualError(code, message)


def content_sha256(path):
    image = read_image(path)
    kept = np.ascontiguousarray(image[(np.arange(image.shape[0]) % TILE_HEIGHT) >= LABEL_HEIGHT])
    return hashlib.sha256(repr(kept.shape).encode() + kept.tobytes()).hexdigest()


def _rows(prefix, found, image):
    rows = []
    for key, members in sorted(found.items(), key=lambda item: min(item[1])):
        members = sorted(members)
        rows.append({'group': f'{prefix}{len(rows):04}', 'key_sha256': key, 'image': image(members[0]), 'members': members})
    return rows


def groups(review, zoom):
    review, zoom = Path(review).resolve(), Path(zoom).resolve()
    result, manifest = read_json(review / 'result.json'), read_json(zoom / 'manifest.json')
    _need(manifest.get('kind') == 'MagnifiedRawReviewPages'
          and manifest.get('review_result_sha256') == file_sha256(review / 'result.json'),
          'The zoom pages belong to another review.')
    sheets = {row['path']: row['sha256'] for row in result['sheets']}
    _need(sorted(page['mirrors_sheet'] for page in manifest['pages']) == sorted(sheets),
          'Every contact sheet must be mirrored by exactly one zoom page.')
    pages, contents = {}, {}
    for page in manifest['pages']:
        _need(file_sha256(zoom / page['path']) == page['sha256'], 'Zoom page bytes changed: ' + page['path'])
        pages.setdefault(page['sha256'], []).append(page['path'])
    for path, sha in sheets.items():
        _need(file_sha256(review / path) == sha, 'Sheet bytes changed: ' + path)
        contents.setdefault(content_sha256(review / path), []).append(path)
    return result, manifest, {'zoom': _rows('z', pages, lambda p: (zoom / p).as_posix()),
                              'sheet': _rows('s', contents, lambda p: (review / p).as_posix())}


def _write_batches(output, prefix, rows, size):
    batches = [rows[i:i + size] for i in range(0, len(rows), size)]
    for number, batch in enumerate(batches, 1):
        (output / f'{prefix}-{number:02}.txt').write_text(''.join(f"{row['group']} | {row['image']}\n" for row in batch),
                                                         encoding='utf-8')
    return [[row['group'] for row in batch] for batch in batches]


def plan(review, zoom, output, *, batch_images=30):
    output = Path(output)
    _need(not output.exists(), 'Use a fresh plan directory.', 'output_exists')
    result, manifest, found = groups(review, zoom)
    _need(result.get('numeric_status') == 'pass' and not result.get('failures'), 'Plan only a numerically passing review.')
    seed = digest(found)
    names = [row['group'] for kind in KINDS for row in found[kind]]
    sample = sorted(random.Random(seed).sample(names, max(1, round(SAMPLE_FRACTION * len(names)))))
    output.mkdir(parents=True)
    (output / 'verdicts').mkdir()
    value = {'kind': 'V4FinalGameVisualReviewPlan', 'schema_version': 1,
             'review': Path(review).resolve().as_posix(), 'zoom': Path(zoom).resolve().as_posix(),
             'review_result_sha256': file_sha256(Path(review) / 'result.json'),
             'zoom_manifest_sha256': file_sha256(Path(zoom) / 'manifest.json'),
             'sheets': len(result['sheets']), 'zoom_pages': len(manifest['pages']), 'groups': found,
             'grouping': {'zoom': 'identical file sha256',
                          'sheet': f'identical pixels outside the label strip (y mod {TILE_HEIGHT} < {LABEL_HEIGHT})'},
             'sample': {'fraction': SAMPLE_FRACTION, 'seed_sha256': seed, 'groups': sample},
             'first_pass': {kind: _write_batches(output, f'haiku-{kind}', found[kind], batch_images) for kind in KINDS},
             'images_judged_by_this_tool': False}
    write_json(output / 'plan.json', value)
    return {'sheets': value['sheets'], 'zoom_pages': value['zoom_pages'],
            'groups': {kind: len(found[kind]) for kind in KINDS},
            'haiku_batches': {kind: len(value['first_pass'][kind]) for kind in KINDS}, 'sample': len(sample)}


def _verdicts(output, prefix, batches):
    """One verdict file per batch list, covering exactly its groups in order."""
    merged = {}
    for kind in KINDS:
        for number, names in enumerate(batches[kind], 1):
            batch = f'{prefix}-{kind}-{number:02}'
            path = Path(output) / 'verdicts' / f'{batch}.json'
            _need(path.exists(), 'Missing verdict file: ' + path.name, 'incomplete_visual_review')
            value = read_json(path)
            rows = value.get('groups') if isinstance(value, dict) else None
            _need(isinstance(rows, list) and [row.get('group') for row in rows] == names,
                  f'{batch}: the verdicts must cover exactly the batch groups in order.', 'incomplete_visual_review')
            for row in rows or []:
                _need(row.get('verdict') in VERDICTS and isinstance(row.get('note', ''), str), f'{batch}: invalid verdict row.')
                merged[row['group']] = {'batch': batch, 'verdict': row['verdict'], 'note': row.get('note', ''),
                                        'cells': row.get('cells')}
    return merged


def second(output, *, batch_images=15):
    """After every Haiku verdict: Sonnet batches for each non-ok group plus the fixed sample."""
    output = Path(output)
    value = read_json(output / 'plan.json')
    _need('second_pass' not in value, 'The second pass is already planned.')
    haiku = _verdicts(output, 'haiku', value['first_pass'])
    flagged = [row['group'] for kind in KINDS for row in value['groups'][kind] if haiku[row['group']]['verdict'] != 'ok']
    chosen = set(flagged) | set(value['sample']['groups'])
    value['second_pass'] = {'flagged_by_haiku': flagged, 'sample': value['sample']['groups'],
                            'batches': {kind: _write_batches(output, f'sonnet-{kind}',
                                                             [row for row in value['groups'][kind] if row['group'] in chosen],
                                                             batch_images) for kind in KINDS}}
    write_json(output / 'plan.json', value)
    return {'flagged': len(flagged), 'sample': len(value['sample']['groups']), 'second_pass_groups': len(chosen),
            'sonnet_batches': {kind: len(value['second_pass']['batches'][kind]) for kind in KINDS}}


def merge(output):
    output = Path(output)
    value = read_json(output / 'plan.json')
    _need('second_pass' in value, 'Plan the second pass first.')
    haiku = _verdicts(output, 'haiku', value['first_pass'])
    sonnet = _verdicts(output, 'sonnet', value['second_pass']['batches'])
    sample, flagged = value['sample']['groups'], value['second_pass']['flagged_by_haiku']
    agree = [name for name in sample if haiku[name]['verdict'] == sonnet[name]['verdict']]
    rows = [row for kind in KINDS for row in value['groups'][kind]]
    final = {row['group']: sonnet.get(row['group']) or haiku[row['group']] for row in rows}
    count = lambda found: {v: sum(1 for row in found.values() if row['verdict'] == v) for v in VERDICTS}
    summary = {'kind': 'V4FinalGameVisualReviewSummary', 'schema_version': 1, 'plan_sha256': file_sha256(output / 'plan.json'),
               'groups': {kind: len(value['groups'][kind]) for kind in KINDS},
               'covered': {kind: sum(len(row['members']) for row in value['groups'][kind]) for kind in KINDS},
               'haiku_counts': count(haiku),
               'second_pass': {'groups': len(sonnet), 'flagged_by_haiku': len(flagged),
                               'flagged_confirmed': sorted(n for n in flagged if sonnet[n]['verdict'] != 'ok'),
                               'flagged_cleared': sorted(n for n in flagged if sonnet[n]['verdict'] == 'ok')},
               'sample_agreement': {'groups': len(sample), 'agree': len(agree), 'rate': len(agree) / len(sample),
                                    'disagree': {n: {'haiku': haiku[n], 'sonnet': sonnet[n]}
                                                 for n in sorted(set(sample) - set(agree))}},
               'final_counts': count(final),
               'final_non_ok': {name: row for name, row in sorted(final.items()) if row['verdict'] != 'ok'},
               'verdict_files': {p.name: file_sha256(p) for p in sorted((output / 'verdicts').glob('*.json'))},
               'images_judged_by_this_tool': False}
    write_json(output / 'summary.json', summary)
    return {key: summary[key] for key in ('groups', 'covered', 'haiku_counts', 'final_counts')} | {
        'flagged': len(flagged), 'sample_agreement': summary['sample_agreement']['rate']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest='operation', required=True)
    one = sub.add_parser('plan')
    one.add_argument('--review', type=Path, required=True)
    one.add_argument('--zoom', type=Path, required=True)
    one.add_argument('--output', type=Path, required=True)
    one.add_argument('--batch-images', type=int, default=30)
    two = sub.add_parser('second')
    two.add_argument('--output', type=Path, required=True)
    two.add_argument('--batch-images', type=int, default=15)
    three = sub.add_parser('merge')
    three.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'plan':
            result = plan(args.review, args.zoom, args.output, batch_images=args.batch_images)
        elif args.operation == 'second':
            result = second(args.output, batch_images=args.batch_images)
        else:
            result = merge(args.output)
    except VisualError as error:
        print(json.dumps({'status': 'error', 'code': error.code, 'message': str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
