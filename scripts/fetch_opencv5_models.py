"""Fetch and verify the OpenCV 5 feature models (ALIKED + LightGlue) used by kiroclops.visual.features5.

The files and their SHA-1 digests are the ones OpenCV's own regression tests
pin in ``opencv/opencv_extra`` ``testdata/dnn/download_models.py``. Each
download lands in a temporary file, is verified, and only then moved into the
models directory; a verified file is never re-downloaded. A manifest with
sizes, SHA-1/SHA-256 digests, sources and timestamps is written beside them.

    python scripts/fetch_opencv5_models.py            # into .tools/models
    python scripts/fetch_opencv5_models.py --dir PATH # or KIROCLOPS_MODELS_DIR
    python scripts/fetch_opencv5_models.py --check    # verify only, no network
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.visual.features5 import MODELS, file_sha1, model_status, models_dir


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def fetch(name, spec, directory):
    target = directory/spec['file']
    if target.is_file() and file_sha1(target) == spec['sha1']:
        return {'name': name, 'status': 'present', 'path': str(target)}
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, prefix=spec['file']+'.', suffix='.part', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with urllib.request.urlopen(spec['url'], timeout=120) as response, open(temporary, 'wb') as out:
            for block in iter(lambda: response.read(1 << 20), b''):
                out.write(block)
        actual = file_sha1(temporary)
        if actual != spec['sha1']:
            raise SystemExit(f'{name}: downloaded SHA-1 {actual} does not match the pinned {spec["sha1"]}; file discarded')
        if target.exists():
            target.unlink()
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {'name': name, 'status': 'downloaded', 'path': str(target)}


def write_manifest(directory):
    rows = {}
    for name, spec in MODELS.items():
        path = directory/spec['file']
        rows[name] = {'file': spec['file'], 'url': spec['url'], 'source': spec['source'], 'sha1': spec['sha1'],
                      'present': path.is_file(), 'size_bytes': path.stat().st_size if path.is_file() else None,
                      'sha256': _sha256(path) if path.is_file() else None,
                      'sha1_ok': path.is_file() and file_sha1(path) == spec['sha1']}
    manifest = {'kind': 'OpenCV5FeatureModels', 'schema_version': 1,
                'written_at': datetime.now(timezone.utc).isoformat(), 'directory': str(directory), 'models': rows}
    (directory/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dir', type=Path, default=None, help='models directory (default: KIROCLOPS_MODELS_DIR or .tools/models)')
    parser.add_argument('--check', action='store_true', help='verify the pinned files without any network access')
    args = parser.parse_args(argv)
    directory = args.dir or models_dir()
    if args.check:
        status = model_status(directory)
        print(json.dumps(status, indent=2))
        return 0 if all(item['sha1_ok'] for item in status.values()) else 1
    results = [fetch(name, spec, directory) for name, spec in MODELS.items()]
    manifest = write_manifest(directory)
    print(json.dumps({'results': results, 'manifest': str(directory/'manifest.json'),
                      'all_verified': all(m['sha1_ok'] for m in manifest['models'].values())}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
