"""Bounded PNG package transport. Never extract arbitrary archive member paths."""
from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from kiroclops.visual.package import MAX_FRAMES, MAX_PIXELS, load_package
from .corpus import portable_manifest
from kiroclops.visual.png import dimensions as png_dimensions

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def validate_png(path, expected_size=None):
    path = Path(path)
    if path.stat().st_size > MAX_FRAME_BYTES:
        raise VisualError('unsupported_input', 'Encoded PNG exceeds the byte budget')
    with path.open('rb') as stream:
        size = png_dimensions(stream.read(33))
    if expected_size is not None and size != expected_size:
        raise VisualError('invalid_frame', 'PNG dimensions differ from the manifest')
    return size


def pack(source, output):
    source, output = Path(source).resolve(), Path(output)
    # Header checks must precede load_package(), which decodes the frames.
    original = read_json(safe_path(source, 'manifest.json'))
    for frame in original.get('frames', []):
        validate_png(safe_path(source, frame['path']), original.get('image_size'))
    original = load_package(source)
    manifest = portable_manifest(original)
    import json
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise VisualError('unsupported_input', 'Manifest exceeds the byte budget')
    if output.exists():
        raise VisualError('output_exists', 'Archive already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_STORED) as archive:
            def entry(name):
                info=zipfile.ZipInfo(name,(1980,1,1,0,0,0))
                info.compress_type=zipfile.ZIP_STORED
                info.create_system=3
                info.external_attr=(0o100444 << 16)
                return info
            archive.writestr(entry('manifest.json'), encoded)
            for frame in manifest['frames']:
                with safe_path(source,frame['path']).open('rb') as stream, archive.open(entry(frame['path']),'w') as dest:
                    for block in iter(lambda:stream.read(1024*1024),b''):dest.write(block)
                if archive.fp.tell() > MAX_ARCHIVE_BYTES:
                    raise VisualError('unsupported_input', 'Package exceeds upload budget')
        if output.stat().st_size>MAX_ARCHIVE_BYTES:
            raise VisualError('unsupported_input','Archive including its directory exceeds upload budget')
        # Hash every byte again, without decoding. The upload never contains
        # unlisted files, source code or native validation receipts.
        for frame in original['frames']:
            if file_sha256(safe_path(source, frame['path'])) != frame['sha256']:
                raise VisualError('source_changed', 'Frame changed while packing')
        if load_package(source, verify=False)['package_sha256'] != original['package_sha256']:
            raise VisualError('source_changed', 'Manifest changed while packing')
        return {'package_sha256': manifest['package_sha256'], 'original_package_sha256': original['package_sha256'],
                'archive_sha256': file_sha256(output), 'bytes': output.stat().st_size,
                'package_policy': 'manifest-listed PNGs only; provenance metadata removed'}
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def unpack(archive_path, output, expected_package_sha256):
    archive_path, output = Path(archive_path), Path(output).resolve()
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise VisualError('unsupported_input', 'Archive exceeds the byte budget')
    if output.exists():
        raise VisualError('output_exists', 'Unpack into a fresh private directory')
    import json
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FRAMES + 1 or len({e.filename for e in entries}) != len(entries):
            raise VisualError('invalid_archive', 'Duplicate or excessive archive entries')
        by_name = {e.filename: e for e in entries}
        if 'manifest.json' not in by_name or by_name['manifest.json'].file_size > MAX_MANIFEST_BYTES:
            raise VisualError('invalid_archive', 'Missing or oversized manifest')
        for entry in entries:
            safe_path(output, entry.filename, must_exist=False)
            if entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise VisualError('unsafe_path', 'Directories and symbolic links are not package assets')
            if entry.compress_type != zipfile.ZIP_STORED or entry.file_size != entry.compress_size:
                raise VisualError('invalid_archive', 'Only stored PNG packages are supported')
        if sum(e.file_size for e in entries) > MAX_ARCHIVE_BYTES:
            raise VisualError('unsupported_input', 'Expanded archive exceeds the byte budget')
        manifest = json.loads(archive.read('manifest.json'))
        if (manifest.get('kind') != 'VideoPackage' or manifest.get('schema_version') != 1
                or manifest.get('metadata') != {} or manifest.get('package_sha256') != expected_package_sha256
                or digest({k:v for k,v in manifest.items() if k != 'package_sha256'}) != expected_package_sha256):
            raise VisualError('package_integrity_failure', 'Invalid portable manifest binding')
        frames = manifest.get('frames', [])
        if not 2 <= len(frames) <= MAX_FRAMES:
            raise VisualError('invalid_archive', 'Invalid number of frames')
        allowed = ['manifest.json', *[f['path'] for f in frames]]
        if len(set(allowed)) != len(allowed) or set(allowed) != set(by_name):
            raise VisualError('invalid_archive', 'Archive must contain exactly the manifest assets')
        # Preflight *all* image headers before any decode or extraction.
        for frame in frames:
            entry = by_name[frame['path']]
            if entry.file_size > MAX_FRAME_BYTES:
                raise VisualError('unsupported_input', 'PNG exceeds the byte budget')
            with archive.open(entry) as stream:
                if png_dimensions(stream.read(33)) != manifest['image_size']:
                    raise VisualError('invalid_frame', 'PNG dimensions differ from the manifest')
        output.mkdir(parents=True)
        for frame in frames:
            # The validated manifest path, not an arbitrary archive entry,
            # controls every destination. Read-only input is set by the worker.
            target = safe_path(output, frame['path'], must_exist=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            actual = hashlib.sha256()
            with archive.open(frame['path']) as stream, target.open('xb') as dest:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    actual.update(block); dest.write(block)
            if actual.hexdigest() != frame['sha256']:
                raise VisualError('package_integrity_failure', 'Frame bytes differ')
        write_json(output / 'manifest.json', manifest)
    return load_package(output)
