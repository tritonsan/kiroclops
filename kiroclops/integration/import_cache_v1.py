"""Seed fresh Godot 4 projects with verified, unchanged texture import products.

This is an asset cache, never an editor-readiness or capture-acceptance cache.
The caller MUST still run the normal editor import on the disposable project.
No engine process is started here. No source, import sidecar, script/UID cache,
editor state, recording, or controller is copied from a cache entry.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import uuid

from .storage import editable, file_lock, project_files, project_hashes, sha256, utc_now

SCHEMA = 1
TEXTURE_SUFFIXES = {'.png', '.svg', '.jpg', '.jpeg', '.webp', '.bmp', '.tga'}
METHOD = 'godot4-texture-seed-v1'


class ImportCacheError(ValueError):
    pass


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise ImportCacheError('Import cache operation cancelled')


def _path(value, *, exists=True, directory=False):
    path = Path(os.path.abspath(value))
    # Resolve only after rejecting links, including linked ancestors on Windows.
    for part in (*reversed(path.parents), path):
        if not part.exists() and not part.is_symlink():
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ImportCacheError(f'Linked/reparse path is not allowed: {part}')
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ImportCacheError(f'Shared hard-linked file is not allowed: {part}')
    if exists and not (path.is_dir() if directory else path.is_file()):
        raise ImportCacheError(f'Expected {"directory" if directory else "file"}: {path}')
    return path


def _relative(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ImportCacheError('Invalid cache-relative path')
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in {'.', '..'} for p in path.parts) or str(path) != value:
        raise ImportCacheError('Invalid cache-relative path')
    return value


def _inside(root, relative, *, exists=True):
    return _path(root / _relative(relative), exists=exists)


def _separate(a, b):
    if a.is_relative_to(b) or b.is_relative_to(a):
        raise ImportCacheError('Cache and project must be separate directories')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _read_json(path):
    try:
        return json.loads(_path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ImportCacheError(f'Invalid cache manifest: {exc}') from exc


def _current(project, expected):
    actual = project_hashes(project)
    if not expected or actual != expected:
        raise ImportCacheError('Project differs from the supplied full source fingerprint')
    return actual


def _quoted(text, name):
    values = re.findall(r'^' + re.escape(name) + r'="([^"\r\n]+)"\s*$', text, re.M)
    if len(values) != 1:
        raise ImportCacheError(f'Unsupported or ambiguous import field: {name}')
    return values[0]


def _texture_inputs(project, *, products, cancel=None):
    assets, files = {}, {}
    for relative, sidecar in project_files(project, include_uid=True):
        _check(cancel)
        if not relative.endswith('.import'):
            continue
        sidecar = _path(sidecar)
        text = sidecar.read_text(encoding='utf-8')
        importer = re.findall(r'^importer="([^"\r\n]+)"\s*$', text, re.M)
        if importer != ['texture']:
            continue  # Other assets are imported by the normal editor process.
        source = relative[:-len('.import')]
        if Path(source).suffix.lower() not in TEXTURE_SUFFIXES:
            raise ImportCacheError('Unsupported texture source type')
        if re.search(r'^valid=false\s*$', text, re.M):
            raise ImportCacheError('Import sidecar reports an invalid resource')
        if _quoted(text, 'source_file') != 'res://' + source:
            raise ImportCacheError('Import source path differs from its sidecar')
        product = _quoted(text, 'path')
        if not product.startswith('res://.godot/imported/'):
            raise ImportCacheError('Import product is not in .godot/imported')
        product = _relative(product[len('res://'):])
        if PurePosixPath(product).parent != PurePosixPath('.godot/imported') or not product.endswith('.ctex'):
            raise ImportCacheError('Only direct texture products may be cached')
        destinations = re.findall(r'^dest_files=(\[[^\r\n]*\])\s*$', text, re.M)
        try:
            if len(destinations) != 1 or json.loads(destinations[0]) != ['res://' + product]:
                raise ImportCacheError('Only single-product textures are supported')
        except json.JSONDecodeError as exc:
            raise ImportCacheError('Malformed texture destination list') from exc
        source_path = _inside(project, source)
        md5_path = str(PurePosixPath(product).with_suffix('.md5'))
        assets[source] = {'source_sha256': sha256(source_path), 'sidecar': relative,
                          'sidecar_sha256': sha256(sidecar), 'product': product, 'md5': md5_path}
        if products:
            compiled, checksum = _inside(project, product), _inside(project, md5_path)
            if not compiled.stat().st_size:
                raise ImportCacheError('Empty imported texture')
            md5_text = checksum.read_text(encoding='utf-8')
            # Godot 4.5.1 verifies these same source/destination MD5 values.
            if (_quoted(md5_text, 'source_md5') != hashlib.md5(source_path.read_bytes()).hexdigest()
                    or _quoted(md5_text, 'dest_md5') != hashlib.md5(compiled.read_bytes()).hexdigest()):
                raise ImportCacheError('Imported texture is stale or corrupted')
            for path, file in ((product, compiled), (md5_path, checksum)):
                if path in files:
                    raise ImportCacheError('Multiple assets claim the same cached product')
                files[path] = sha256(file)
    if not assets:
        raise ImportCacheError('No supported imported textures were found')
    return assets, files


def _inputs(project, engine, expected, rendering_method, *, products, cancel=None):
    if rendering_method != 'gl_compatibility':
        raise ImportCacheError('This cache version supports gl_compatibility only')
    actual = _current(project, expected)
    assets, files = _texture_inputs(project, products=products, cancel=cancel)
    result = {'engine_sha256': sha256(_path(engine)), 'rendering_method': rendering_method,
              'protected_source_hashes': {k: v for k, v in actual.items() if not editable(k)},
              'assets': assets}
    return result, files


def _validate_entry(entry):
    entry = _path(entry, directory=True)
    manifest = _read_json(entry / 'manifest.json')
    if (manifest.get('schema_version') != SCHEMA or manifest.get('method') != METHOD
            or manifest.get('requires_editor_import') is not True
            or manifest.get('implementation_sha256') != sha256(Path(__file__))
            or manifest.get('cache_key') != _digest(manifest.get('inputs'))
            or entry.name != manifest.get('cache_key')):
        raise ImportCacheError('Cache identity or schema mismatch')
    assets = manifest['inputs'].get('assets', {})
    expected_names = {name for asset in assets.values() for name in (asset['product'], asset['md5'])}
    if not assets or set(manifest.get('files', {})) != expected_names:
        raise ImportCacheError('Cache product inventory differs from its asset inputs')
    for name, digest in manifest['files'].items():
        if PurePosixPath(_relative(name)).parent != PurePosixPath('.godot/imported') or Path(name).suffix not in {'.ctex', '.md5'}:
            raise ImportCacheError('Forbidden cache product')
        if sha256(_inside(entry / 'payload', name)) != digest:
            raise ImportCacheError('Cache payload checksum mismatch')
    actual = set()
    for base, dirs, names in os.walk(entry / 'payload', followlinks=False):
        for name in dirs:
            _path(Path(base) / name, directory=True)
        for name in names:
            p = _path(Path(base) / name)
            actual.add(p.relative_to(entry / 'payload').as_posix())
    if actual != expected_names:
        raise ImportCacheError('Unregistered file in cache payload')
    return manifest


def inspect_import_inputs(project, *, engine, expected_source_hashes,
                          rendering_method='gl_compatibility', cancel=None):
    """Read-only eligibility inspection; never copies or runs an engine."""
    started = time.perf_counter()
    project = _path(project, directory=True)
    inputs, files = _inputs(project, engine, expected_source_hashes, rendering_method, products=True, cancel=cancel)
    return {'cache_key': _digest(inputs), 'asset_count': len(inputs['assets']), 'product_count': len(files),
            'product_bytes': sum((project / p).stat().st_size for p in files),
            'inspection_seconds': time.perf_counter() - started, 'requires_editor_import': True}


def publish_import_cache(project, cache_root, *, engine, expected_source_hashes,
                         rendering_method='gl_compatibility', cancel=None):
    """Publish immutable verified texture products from an already imported copy."""
    started = time.perf_counter()
    project = _path(project, directory=True)
    cache_root = _path(cache_root, exists=False, directory=True)
    _separate(project, cache_root)
    inputs, files = _inputs(project, engine, expected_source_hashes, rendering_method, products=True, cancel=cancel)
    key = _digest(inputs)
    cache_root.mkdir(parents=True, exist_ok=True)
    entry = cache_root / key
    lock_path = _path(cache_root / (key + '.lock'), exists=False)
    with file_lock(lock_path):
        if entry.exists():
            existing = _validate_entry(entry)
            if existing['inputs'] != inputs or existing['files'] != files:
                raise ImportCacheError('Existing cache entry does not reproduce these imported assets')
        else:
            stage = cache_root / ('.staging-' + uuid.uuid4().hex)
            stage.mkdir()
            for relative, digest in sorted(files.items()):
                _check(cancel)
                source = _inside(project, relative)
                destination = stage / 'payload' / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if sha256(source) != digest or sha256(destination) != digest:
                    raise ImportCacheError('Asset changed during cache publication; staging evidence retained')
            _check(cancel)
            again, again_files = _inputs(project, engine, expected_source_hashes, rendering_method, products=True, cancel=cancel)
            if again != inputs or again_files != files:
                raise ImportCacheError('Inputs changed during cache publication; staging evidence retained')
            manifest = {'schema_version': SCHEMA, 'method': METHOD, 'cache_key': key, 'inputs': inputs,
                        'files': files, 'requires_editor_import': True, 'created_at': utc_now(),
                        'producer': {'project': str(project), 'full_source_hashes': expected_source_hashes},
                        'implementation_sha256': sha256(Path(__file__))}
            (stage / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
            os.rename(stage, entry)
        manifest = _validate_entry(entry)
    _current(project, expected_source_hashes)
    return {'status': 'published', 'cache_dir': str(entry), 'cache_key': key,
            'manifest_sha256': sha256(entry / 'manifest.json'), 'asset_count': len(inputs['assets']),
            'product_count': len(files), 'elapsed_seconds': time.perf_counter() - started,
            'requires_editor_import': True, 'capture_acceptance': False}


def seed_import_cache(cache_dir, project, *, engine, expected_source_hashes, expected_manifest_sha256,
                      rendering_method='gl_compatibility', cancel=None):
    """Copy asset products into a fresh project; caller still runs editor import."""
    started = time.perf_counter()
    entry, project = _path(cache_dir, directory=True), _path(project, directory=True)
    _separate(entry, project)
    _check(cancel)
    manifest = _validate_entry(entry)
    manifest_sha = sha256(entry / 'manifest.json')
    if manifest_sha != expected_manifest_sha256:
        raise ImportCacheError('Cache manifest differs from its caller-held fingerprint')
    inputs, _ = _inputs(project, engine, expected_source_hashes, rendering_method, products=False, cancel=cancel)
    if inputs != manifest['inputs']:
        raise ImportCacheError('Engine, protected source, asset, or import settings changed')
    if (project / '.godot').exists() or (project / '.godot').is_symlink():
        raise ImportCacheError('Cache seeding requires a fresh project without .godot state')
    stage = project / ('.kiroclops-import-stage-' + uuid.uuid4().hex)
    stage.mkdir()
    try:
        for relative, digest in sorted(manifest['files'].items()):
            _check(cancel)
            source = _inside(entry / 'payload', relative)
            destination = stage / Path(relative).relative_to('.godot')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if sha256(source) != digest or sha256(destination) != digest:
                raise ImportCacheError('Cache changed during seeding')
        if sha256(entry / 'manifest.json') != manifest_sha:
            raise ImportCacheError('Cache manifest changed during seeding')
        # The temporary directory contains no source files; remove it from the
        # project before checking the exact full source fingerprint.
        _check(cancel)
        os.rename(stage, project / '.godot')
        current, products = _inputs(project, engine, expected_source_hashes, rendering_method, products=True, cancel=cancel)
        if current != inputs or products != manifest['files']:
            raise ImportCacheError('Seeded assets or sources changed')
        _validate_entry(entry)
    except Exception:
        # Keep any partial copy as diagnostic evidence; callers must discard the
        # disposable project after failure and must not start its engine.
        raise
    return {'status': 'seeded', 'cache_dir': str(entry), 'cache_key': manifest['cache_key'],
            'manifest_sha256': manifest_sha, 'project': str(project),
            'full_source_hashes': expected_source_hashes, 'asset_count': len(inputs['assets']),
            'product_count': len(manifest['files']), 'elapsed_seconds': time.perf_counter() - started,
            'requires_editor_import': True, 'capture_acceptance': False}
