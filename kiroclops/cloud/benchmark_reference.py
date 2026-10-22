"""Bind a benchmark oracle to the same corpus and analysis code as its trial.

Historical outputs from a different tracker remain useful diagnostics. They
cannot decide cross-engine correctness after that tracker was changed.
"""
from pathlib import Path
from copy import deepcopy
import shutil

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json


def validate_reference(root, corpus, sources):
    root = Path(root)
    reference = read_json(root/'reference.json')
    expected = reference.get('reference_sha256')
    if expected != digest({k: v for k, v in reference.items() if k != 'reference_sha256'}):
        raise VisualError('reference_integrity_failure', 'Benchmark reference manifest changed')
    if reference.get('corpus_sha256') != corpus['corpus_sha256']:
        raise VisualError('reference_binding_mismatch', 'Benchmark reference uses another corpus')
    if reference.get('analysis_sources') != sources:
        raise VisualError('reference_binding_mismatch', 'Benchmark reference uses another analysis implementation')
    engine = reference.get('engine', {})
    if not engine.get('identity') or digest(engine['identity']) != engine.get('engine_sha256'):
        raise VisualError('reference_integrity_failure', 'Benchmark reference engine identity is missing or changed')
    items = reference.get('items', [])
    ids = [r['id'] for r in items]
    if len(ids) != len(set(ids)) or set(ids) != {r['id'] for r in corpus['items']}:
        raise VisualError('reference_binding_mismatch', 'Benchmark reference inventory differs from corpus')
    inventory = {r['id']: r for r in corpus['items']}
    for item in items:
        if item.get('package_sha256') != inventory[item['id']]['package_sha256']:
            raise VisualError('reference_binding_mismatch', 'Benchmark reference package binding differs')
        path = safe_path(root, item['measurement_path'])
        if file_sha256(path) != item['measurement_file_sha256']:
            raise VisualError('reference_integrity_failure', 'Benchmark reference measurement bytes changed')
        measurement = read_json(path)
        if digest(measurement) != item['measurement_sha256']:
            raise VisualError('reference_integrity_failure', 'Benchmark reference measurement digest differs')
        observations, events = measurement['observations'], measurement['events']
        if observations['package_sha256'] != item['package_sha256'] or events['observation_sha256'] != digest(observations):
            raise VisualError('reference_integrity_failure', 'Benchmark measurement links are invalid')
    return reference


def expected_measurement(root, reference, item_id):
    item = next(r for r in reference['items'] if r['id'] == item_id)
    return read_json(safe_path(root, item['measurement_path']))


def portable_reference(root, output, corpus, sources):
    """Copy only measurements and engine identity; keep local installation paths local."""
    root, output = Path(root), Path(output)
    reference = deepcopy(validate_reference(root, corpus, sources))
    if output.exists():
        raise VisualError('output_exists', 'Portable benchmark reference requires a new directory')
    reference['engine'] = {k: reference['engine'][k] for k in ('identity', 'engine_sha256')}
    reference['source_reference_sha256'] = reference.pop('reference_sha256')
    for item in reference['items']:
        target = safe_path(output, item['measurement_path'], must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(safe_path(root, item['measurement_path']), target)
    reference['reference_sha256'] = digest(reference)
    write_json(output/'reference.json', reference)
    return validate_reference(output, corpus, sources)
