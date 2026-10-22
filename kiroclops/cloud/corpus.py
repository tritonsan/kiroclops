"""Freeze an explicit calibration inventory; never discover frames recursively."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import shutil

from kiroclops.visual.package import load_package
from kiroclops.visual.schema import digest, file_sha256, read_json, safe_path, write_json, VisualError
from .identity import analysis_sources


def approved_inventory(root):
    root = Path(root).resolve()
    evidence = root / 'artifacts/phase3a/completion-reference-observability-02/result.json'
    rows = []
    for rec in read_json(evidence)['records']:
        if rec['split'] != 'calibration':
            continue
        parent = Path(rec['analysis_path']).parent
        requests = sorted((parent / 'jobs/jobs').glob('*/request.json'))
        if len(requests) != 1:
            raise VisualError('ambiguous_source', 'Expected one frozen calibration analysis')
        req = read_json(requests[0])
        rows.append({'id': rec['domain'] + '-' + rec['scenario_id'], 'domain': rec['domain'],
                     'split': 'calibration', 'package': rec['package'],
                     'selection_path': str(parent / 'selection.json'), 'contract': req['contract'],
                     'expected_path': rec['analysis_path'], 'expected_kind': 'AnalysisResult',
                     'registration': str(evidence), 'job_directory': str(requests[0].parent)})
    prepared = root / 'artifacts/phase3a/completion-paired-cohort-01/sessions/game-tools-01/jump/prepared-private.json'
    game = read_json(prepared)
    for side in ('reference', 'before'):
        annotation = root / ('config/tiny-annotation.json' if side == 'reference' else 'config/phase2c-v2/heart-jump-annotation.json')
        for rec in read_json(game[side]):
            scenario = rec['scenario']
            if scenario['split'] != 'calibration' or scenario['repeat'] != 1 or scenario['hold_ticks'] not in (3, 24):
                continue
            parent = Path(rec['package']).parent
            rows.append({'id': f'game-{side}-{scenario["id"]}', 'domain': 'game', 'split': 'calibration',
                         'package': rec['package'], 'selection_path': str(parent / 'selection.json'),
                         'contract': {'min_valid_ratio': .95, 'require_static_camera': True,
                                      'camera_max_shift_px': read_json(annotation).get('camera_max_shift_px', 1.)},
                         'expected_path': str(parent / 'observations.json'), 'expected_kind': 'ObservationSet',
                         'registration': game[side], 'scenario_binding': rec['scenario_binding']})
    return sorted(rows, key=lambda r: r['id'])


def portable_manifest(original):
    value = deepcopy(original)
    # Metadata is provenance only, not measurement input. Never transport local
    # paths, native-state records or unlisted files. Retain the original binding
    # in a local-only receipt, not inside the worker's package.
    value['metadata'] = {}
    value.pop('package_sha256', None)
    value['package_sha256'] = digest(value)
    return value


def rebind(value, old_sha, new_sha):
    if isinstance(value, dict):
        return {k: rebind(v, old_sha, new_sha) for k, v in value.items()}
    if isinstance(value, list):
        return [rebind(v, old_sha, new_sha) for v in value]
    return new_sha if value == old_sha else value


def freeze(root, output, rows=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    rows = approved_inventory(root) if rows is None else rows
    if Counter(r['domain'] for r in rows) != Counter(game=4, panel=4, svg=4):
        raise VisualError('invalid_corpus', 'Expected four calibration packages in each domain')
    if len({r['id'] for r in rows}) != 12 or any(r.get('split') != 'calibration' for r in rows):
        raise VisualError('heldout_leakage', 'Only twelve distinct calibration records are allowed')
    if output.exists():
        raise VisualError('output_exists', 'Freeze into a fresh directory')
    sources = analysis_sources(root)
    output.mkdir(parents=True)
    bindings, items = [], []
    for row in rows:
        source = Path(row['package'])
        original = load_package(source)
        manifest = portable_manifest(original)
        original_sha, package_sha = original['package_sha256'], manifest['package_sha256']
        target = safe_path(output, row['id'], must_exist=False)
        package = target / 'package'
        for frame in original['frames']:
            dst = safe_path(package, frame['path'], must_exist=False)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(source, frame['path']), dst)
        write_json(package / 'manifest.json', manifest)
        selection = rebind(read_json(row['selection_path']), original_sha, package_sha)
        expected = read_json(row['expected_path'])
        if row['expected_kind'] == 'AnalysisResult':
            expected = {k: expected[k] for k in ('observations', 'events')}
        else:
            expected = {'observations': expected}
        original_observation_sha = digest(expected['observations'])
        expected = rebind(expected, original_sha, package_sha)
        if 'events' in expected and 'observation_sha256' in expected['events']:
            if expected['events']['observation_sha256'] != original_observation_sha:
                raise VisualError('result_binding_mismatch', 'Historical events reference another observation set')
            expected['events']['observation_sha256'] = digest(expected['observations'])
        for name, value in [('selection', selection), ('contract', row['contract']), ('historical', expected)]:
            write_json(target / (name + '.json'), value)
        if load_package(source)['package_sha256'] != original_sha or load_package(package)['package_sha256'] != package_sha:
            raise VisualError('source_changed', 'A source or its frozen copy changed')
        item = {'id': row['id'], 'domain': row['domain'], 'split': 'calibration',
                'package': row['id'] + '/package', 'package_sha256': package_sha,
                'selection_sha256': digest(selection), 'contract_sha256': digest(row['contract']),
                'historical_sha256': digest(expected), 'frame_count': len(manifest['frames']),
                'image_size': manifest['image_size'],
                'bytes': sum(safe_path(package, f['path']).stat().st_size for f in manifest['frames'])}
        items.append(item)
        bindings.append({**row, 'source_package_sha256': original_sha,
                         'portable_package_sha256': package_sha,
                         'selection_file_sha256': file_sha256(row['selection_path']),
                         'expected_file_sha256': file_sha256(row['expected_path']),
                         'registration_sha256': file_sha256(row['registration'])})
    if analysis_sources(root) != sources:
        raise VisualError('source_changed', 'Analysis code changed during corpus freeze')
    for rel in [*sources, 'kiroclops/__init__.py', 'kiroclops/local_engine/__init__.py',
                'kiroclops/local_engine/backend.py', 'kiroclops/local_engine/worker.py']:
        dst = output / 'source' / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, dst)
    manifest = {'kind': 'CalibrationBenchmark', 'schema_version': 1, 'items': items,
                'analysis_sources': sources, 'analysis_sources_sha256': digest(sources),
                'selection_policy': 'Registered calibration only; first native repeat for reference/target, all four web calibration clips',
                'package_policy': 'PNG bytes/times/inputs unchanged; provenance metadata removed; hash bindings rebased',
                'index_for_recommendations': False}
    manifest['corpus_sha256'] = digest(manifest)
    write_json(output / 'manifest.json', manifest)
    # This file stays local, outside the portable corpus.
    write_json(output.parent / (output.name + '-sources.local.json'), bindings)
    return manifest


def verify_corpus(root):
    root = Path(root)
    corpus = read_json(root / 'manifest.json')
    if digest({k: v for k, v in corpus.items() if k != 'corpus_sha256'}) != corpus['corpus_sha256']:
        raise VisualError('corpus_integrity_failure', 'Corpus manifest changed')
    for row in corpus['items']:
        if row.get('split') != 'calibration':
            raise VisualError('heldout_leakage', 'Benchmark may not use held-out records')
        if load_package(root / row['package'])['package_sha256'] != row['package_sha256']:
            raise VisualError('corpus_integrity_failure', 'Package binding changed')
        for name in ('selection', 'contract', 'historical'):
            if digest(read_json(safe_path(root, row['id'] + '/' + name + '.json'))) != row[name + '_sha256']:
                raise VisualError('corpus_integrity_failure', name + ' changed')
    return corpus
