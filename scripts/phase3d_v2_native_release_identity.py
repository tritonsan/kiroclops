"""Exact static release identity for recorded Kiro CLI 2.27.1 native runs.

Kiro CLI 2.27.1 extracted its own KAS 0.66.22 directory. Every installed file,
including the native output producer ``acp-server.js`` and the native modules,
is byte-identical to the 2.27.0 installation the source-pinned 2.27 output
policies were reviewed against, except the version label and publisher hash of
one preserved ``@types/node`` license manifest. This versioned consumer lets
the unchanged canonical native audit read an exact 2.27.1 / KAS 0.66.22 /
launcher 628ec724 run with those 2.27.0 rules, only while both installed trees,
the pinned 2.27.0 receipts and the canonical reader still match the saved proof.

The resolver is process-local: ``review.py`` and every hash-bound core file stay
byte-identical (the 3D V2 repair closure binds them). It never authorizes a
tool or waives an envelope, guard decision, edit checkpoint or time budget,
aliases another release, or rewrites a historical receipt. The CLI host binary
is not compared: the 2.27.0 binary is no longer installed, so every recorded
transport event must still pass the exact 2.27.0 envelope rules.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import launcher_policy_proof as base
from kiroclops.local_engine import review
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = 'artifacts/phase3d-v2/native-2271-release-identity-01'
PROOF = EVIDENCE + '/source-proof.json'
MANIFEST = EVIDENCE + '/tree-manifest.json'
PROVENANCE = EVIDENCE + '/provenance.json'
RED = EVIDENCE + '/red-canonical.json'
BASE_OUTPUT_PROOF = 'reviews/staged-native227v3/source-proof-v3.json'
REVIEW = 'kiroclops/local_engine/review.py'
LAUNCHER_POLICY_PROOF = 'kiroclops/local_engine/launcher_policy_proof.py'
# The exact canonical reader this identity was reviewed with; any reader change
# requires a new reviewed identity version rather than silent reuse.
READER_PINS = {
    REVIEW: '86c1be95c0140d63cc37d72e984ff6446161384dcb7b658c93f11fd45c789a1f',
    LAUNCHER_POLICY_PROOF: '467d076393dce29f146406b98f5609a14e1b7ae19f6572554e1d568d739619be',
    base.OUTPUT: '2a566e3fc132b95716e28c956fd361c390776005a223d387b4ccb28cb8941c8e',
    BASE_OUTPUT_PROOF: base.SOURCE_PINS[BASE_OUTPUT_PROOF],
}
BASE_RELEASE = 'kiro-cli-chat 2.27.0'
RELEASE = 'kiro-cli-chat 2.27.1'
KAS_VERSION = '0.66.22'
BASE_DIRECTORY = '2.27.0-2943e5ce601c8f80b0b4240176f70606c25c931dfc0d9c74c98f5e4e054d5475'
RELEASE_DIRECTORY = '2.27.1-6b7b66ac6aa10b16f11db633ea4cb6e60591f7e6b04df438718020685de1fb67'
PRODUCER = 'node_modules/@kiro/agent/dist/server/acp-server.js'
PACKAGE = 'node_modules/@kiro/agent/package.json'
LICENSE_MANIFEST = 'node_modules/_preserved-licenses/@types/node/package.json'
LICENSE_CHANGE = {'package': '@types/node', 'changed_keys': ['typesPublisherContentHash', 'version'],
                  'base_version': '26.6.3', 'release_version': '26.6.4'}
REFERENCE_TOKEN = b'preserved-licenses'
BASE_CONTEXT = {'cli_version': BASE_RELEASE, 'kas_version': KAS_VERSION,
                'launcher_sha256': base.ACTIVE_LAUNCHER_SHA256}
RELEASE_CONTEXT = {**BASE_CONTEXT, 'cli_version': RELEASE}
# Canonical entry points where a recorded launch context reaches the 2.27
# policy lookup. Inner calls already receive the mapped context.
ENTRY_POINTS = ('_builtin_policy_227', 'audit_native_tool_outcomes')
NATIVE_FILES = ('kiro/launch.json', 'kiro/config.json', 'kiro/run/stdout.jsonl',
                'kiro/run/outbound.jsonl', 'kiro/tool-audit.jsonl')


def _need(value, message, code='release_identity_unproven'):
    if not value:
        raise VisualError(code, message)


def _redirected(path):
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0)
                                              & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _tree(directory):
    """Relative POSIX path -> SHA-256 of every regular file, plus files naming the token."""
    directory = Path(directory)
    _need(directory.is_dir() and not _redirected(directory), f'Installed KAS directory is missing or redirected: {directory}')
    files, references = {}, []
    for current, dirs, names in os.walk(directory):
        dirs.sort()
        for name in dirs:
            _need(not _redirected(Path(current) / name), 'Installed KAS tree contains a directory link or junction.')
        for name in sorted(names):
            path = Path(current) / name
            _need(not _redirected(path) and path.is_file(), 'Installed KAS tree contains a link or non-regular file.')
            data = path.read_bytes()
            relative = path.relative_to(directory).as_posix()
            files[relative] = hashlib.sha256(data).hexdigest()
            if REFERENCE_TOKEN in data:
                references.append(relative)
    _need(files, 'Installed KAS directory is empty.')
    return dict(sorted(files.items())), sorted(references)


def _license_change(base_dir, release_dir):
    """The one differing file may change only its version label and publisher hash."""
    old, new = (read_json(Path(directory) / LICENSE_MANIFEST) for directory in (base_dir, release_dir))
    _need(isinstance(old, dict) and isinstance(new, dict) and set(old) == set(new),
          'The preserved license manifest changed its structure.')
    changed = sorted(key for key in old if old[key] != new[key])
    _need(old.get('name') == new.get('name') == LICENSE_CHANGE['package'] and changed == LICENSE_CHANGE['changed_keys']
          and (old['version'], new['version']) == (LICENSE_CHANGE['base_version'], LICENSE_CHANGE['release_version']),
          'The preserved license manifest changed beyond its version label.')
    return dict(LICENSE_CHANGE)


def _pinned_reader(root):
    """Exact canonical reader and pinned 2.27.0 receipts; re-verified, never rewritten."""
    for relative, expected in READER_PINS.items():
        _need(file_sha256(root / relative) == expected, 'Pinned canonical reader or 2.27.0 receipt changed: ' + relative)
    v3 = read_json(root / BASE_OUTPUT_PROOF)
    _need(v3.get('status') == 'pass' and v3.get('policy_version') == 3 and v3.get('cli_version') == BASE_RELEASE
          and v3.get('kas_version') == KAS_VERSION and v3.get('source_sha256') == base.KAS_SOURCE_SHA256,
          'Pinned 2.27.0 output proof differs.')
    _need(base.verify_extension(dict(BASE_CONTEXT), root=root) is not None,
          'Pinned 2.27.0 launcher extension does not verify.')
    return v3


def build_proof(root=None, *, kas_root=None):
    """Deterministically re-hash both installed trees against the pinned 2.27.0 reader."""
    root = Path(root or ROOT)
    v3 = _pinned_reader(root)
    source = Path(v3['source'])
    _need(source.parents[5].name == BASE_DIRECTORY and source.relative_to(source.parents[5]).as_posix() == PRODUCER,
          'Pinned producer path differs from the inspected 2.27.0 installation.')
    kas_root = Path(kas_root) if kas_root is not None else source.parents[6]
    base_dir, release_dir = kas_root / BASE_DIRECTORY, kas_root / RELEASE_DIRECTORY
    base_files, base_references = _tree(base_dir)
    release_files, release_references = _tree(release_dir)
    _need(set(base_files) == set(release_files), 'Installed KAS trees contain different files.')
    differences = [path for path in base_files if base_files[path] != release_files[path]]
    _need(differences == [LICENSE_MANIFEST], 'Installed KAS trees differ outside the inspected license manifest.')
    _need(base_files[PRODUCER] == release_files[PRODUCER] == base.KAS_SOURCE_SHA256,
          'Native output producer differs from the pinned 2.27.0 source.')
    _need(not base_references and not release_references,
          'An installed file names the preserved license manifests.')
    for directory in (base_dir, release_dir):
        _need(read_json(directory / PACKAGE).get('version') == KAS_VERSION, 'Installed @kiro/agent version differs.')
    change = _license_change(base_dir, release_dir)
    manifest = {'kind': 'PinnedKASReleaseTreeManifest', 'schema_version': 1,
                'base_directory': BASE_DIRECTORY, 'release_directory': RELEASE_DIRECTORY,
                'base_files': base_files, 'release_files': release_files}
    proof = {'kind': 'PinnedKASReleaseProducerIdentity', 'schema_version': 1, 'status': 'pass',
             'base_release': BASE_RELEASE, 'release': RELEASE, 'kas_version': KAS_VERSION,
             'launcher_sha256': base.ACTIVE_LAUNCHER_SHA256, 'kas_root': str(kas_root),
             'base_directory': BASE_DIRECTORY, 'release_directory': RELEASE_DIRECTORY,
             'producer': PRODUCER, 'producer_sha256': base.KAS_SOURCE_SHA256,
             'files': len(base_files), 'identical_files': len(base_files) - len(differences),
             'base_tree_sha256': digest(base_files), 'release_tree_sha256': digest(release_files),
             'tree_manifest_sha256': digest(manifest),
             'differences': [{'path': LICENSE_MANIFEST, 'base_sha256': base_files[LICENSE_MANIFEST],
                              'release_sha256': release_files[LICENSE_MANIFEST], **change,
                              'reason': 'Preserved third-party type-definition license manifest; only its version label and publisher hash differ.'}],
             'files_naming_preserved_licenses': [], 'reader_pins': dict(READER_PINS),
             'policy_entry_points': list(ENTRY_POINTS), 'proof_helper_sha256': file_sha256(__file__),
             'cli_host_binary_compared': False, 'native_canary': False, 'model_started': False,
             'tool_authorization_granted': False, 'behavior_acceptance_granted': False,
             'historical_receipts_rewritten': False, 'canonical_reader_modified': False,
             'scope': ('Exact release pair only. Recorded 2.27.1 runs are replayed with the unchanged 2.27.0 output, '
                       'tool, edit and housekeeping rules through a process-local resolver; every envelope, guard '
                       'decision, cardinality and budget check remains mandatory. No other version alias, model '
                       'fallback, runtime signature or host-binary equivalence.')}
    return proof, manifest


def verify_saved(root=None, *, kas_root=None):
    """The saved proof, only while a fresh rebuild reproduces it and its manifest exactly."""
    root = Path(root or ROOT)
    proof, manifest = build_proof(root, kas_root=kas_root)
    _need((root / PROOF).is_file() and (root / MANIFEST).is_file(), 'Release identity proof is missing.')
    _need(read_json(root / PROOF) == proof and read_json(root / MANIFEST) == manifest,
          'Saved release identity differs from the current installed trees or pinned reader.')
    return proof


def map_context(context):
    """The 2.27.0 policy context for the exact recorded 2.27.1 triple; otherwise None."""
    if not isinstance(context, dict) or any(context.get(key) != value for key, value in RELEASE_CONTEXT.items()):
        return None
    return {**context, 'cli_version': BASE_RELEASE}


@contextmanager
def release_identity_policy(*, root=None, kas_root=None):
    """Process-local exact resolver around the unchanged canonical reader.

    Verifies the saved proof on entry and again after the body. Yields the list
    of applied mappings. The original functions are restored even on error.
    """
    proof = verify_saved(root, kas_root=kas_root)
    _need(file_sha256(review.__file__) == READER_PINS[REVIEW], 'The loaded canonical reader differs from its pin.')
    originals = {name: getattr(review, name) for name in ENTRY_POINTS}
    applied = []

    def record(entry, context):
        applied.append({'entry_point': entry, 'recorded': {key: context.get(key) for key in RELEASE_CONTEXT},
                        'policy_cli_version': BASE_RELEASE})

    @functools.wraps(originals['_builtin_policy_227'])
    def builtin(context):
        mapped = map_context(context)
        if mapped is not None:
            record('_builtin_policy_227', context)
        return originals['_builtin_policy_227'](context if mapped is None else mapped)

    @functools.wraps(originals['audit_native_tool_outcomes'])
    def outcomes(*args, runtime_context=None, **kwargs):
        mapped = map_context(runtime_context)
        if mapped is not None:
            record('audit_native_tool_outcomes', runtime_context)
        return originals['audit_native_tool_outcomes'](
            *args, runtime_context=runtime_context if mapped is None else mapped, **kwargs)

    review._builtin_policy_227, review.audit_native_tool_outcomes = builtin, outcomes
    try:
        yield applied
    finally:
        for name, function in originals.items():
            setattr(review, name, function)
    _need(verify_saved(root, kas_root=kas_root) == proof, 'The release identity changed during the replay.')


def _launch_context(directory):
    launch = read_json(Path(directory) / 'kiro/launch.json')
    config = read_json(Path(directory) / 'kiro/config.json')
    return {'cli_version': launch.get('cli_version'), 'kas_version': launch.get('kas_version'),
            'launcher_sha256': config.get('launcher_sha256')}


def audit_native_run(directory, *, root=None, kas_root=None):
    """Canonical native replay of an exact recorded 2.27.1 run with the proven 2.27.0 rules."""
    directory = Path(directory).resolve()
    recorded = _launch_context(directory)
    _need(map_context(recorded) is not None,
          'Only an exact 2.27.1 / KAS 0.66.22 / launcher 628ec724 run uses this identity; use the canonical audit.',
          'release_identity_not_applicable')
    with release_identity_policy(root=root, kas_root=kas_root) as applied:
        native = review.audit_native_run(directory)
    expected = [{'entry_point': entry, 'recorded': recorded, 'policy_cli_version': BASE_RELEASE} for entry in ENTRY_POINTS]
    _need(sorted(applied, key=lambda item: item['entry_point']) == expected,
          'The replay did not reach each canonical policy entry point exactly once.')
    _need(native.get('status') == 'pass', 'Canonical native replay did not pass.', 'invalid_native_evidence')
    root = Path(root or ROOT)
    return {'kind': 'ReleaseIdentityNativeReplay', 'schema_version': 1, 'status': 'pass',
            'session': str(directory), 'recorded_runtime': recorded, 'policy_runtime': map_context(recorded),
            'mapped_entry_points': applied, 'native': native,
            'release_identity': {'proof': PROOF, 'proof_sha256': file_sha256(root / PROOF),
                                 'manifest_sha256': file_sha256(root / MANIFEST),
                                 'helper_sha256': file_sha256(__file__), 'reader_pins': dict(READER_PINS)},
            'canonical_reader_modified': False, 'historical_receipts_rewritten': False,
            'behavior_acceptance_granted': False, 'post_hoc_for_recorded_runs': True,
            'scope': 'Native transport/tool-boundary replay only; not source authorship review, behavior or stage acceptance.'}


def _canonical_outcome(directory):
    """The unchanged canonical audit result, or its exact refusal."""
    try:
        result = review.audit_native_run(directory)
        return {'status': result['status'], 'evidence_hashes': result['evidence_hashes'],
                'tool_calls': len(result['tool_outcomes'].get('calls', [])), 'result_sha256': digest(result)}
    except (VisualError, OSError, ValueError, KeyError) as exc:
        return {'status': 'fail', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}


def _session_identity(directory):
    directory = Path(directory).resolve()
    return {'session': str(directory), 'recorded_runtime': _launch_context(directory),
            'native_files': {name: file_sha256(directory / name) for name in NATIVE_FILES}}


def _create_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write('\n')
    return file_sha256(path)


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_red(sessions, root=None):
    """Canonical refusal before any release identity exists; chronology is append-only."""
    root = Path(root or ROOT)
    _need(not (root / PROOF).exists(), 'Record the canonical result before building the release identity.', 'output_exists')
    rows = [{**_session_identity(session), 'canonical': _canonical_outcome(session)} for session in sessions]
    value = {'kind': 'CanonicalNativeAuditBeforeReleaseIdentity', 'schema_version': 1, 'recorded_at': _now(),
             'reader_sha256': {relative: file_sha256(root / relative) for relative in READER_PINS},
             'helper_sha256': file_sha256(__file__), 'sessions': rows, 'release_identity_used': False,
             'historical_receipts_rewritten': False}
    return {'path': str(root / RED), 'sha256': _create_json(root / RED, value), 'sessions': rows}


def _observed_file(path):
    path = Path(path)
    info = path.stat()
    return {'path': str(path), 'sha256': file_sha256(path), 'bytes': info.st_size,
            'modified_at': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()}


def build(root=None, *, first_use_session=None):
    """Save the deterministic proof, its manifest and a one-time host observation."""
    root = Path(root or ROOT)
    _need((root / RED).is_file(), 'Record the canonical RED result first.')
    for relative in (PROOF, MANIFEST, PROVENANCE):
        _need(not (root / relative).exists(), 'Preserve the existing release identity: ' + relative, 'output_exists')
    proof, manifest = build_proof(root)
    kas_root = Path(proof['kas_root'])
    install = kas_root.parent
    first_use = None
    if first_use_session is not None:
        owned_path = Path(first_use_session) / 'kiro/run/owned-processes.json'
        lock = kas_root / (RELEASE_DIRECTORY + '.lock')
        owned, held = read_json(owned_path), read_json(lock)
        acquired = held.get('acquired_at_ms')
        first_use = {'session': str(Path(first_use_session).resolve()),
                     'session_process_record': _observed_file(owned_path), 'release_lock': _observed_file(lock),
                     'native_client_pid': owned.get('native_client_process', {}).get('pid'),
                     'native_client_bound_at': owned.get('recorded_at'), 'lock_pid': held.get('pid'),
                     'lock_acquired_at': acquired / 1000 if type(acquired) is int else None,
                     'note': 'Observation only: compares the release directory lock with the recorded native client.'}
    provenance = {'kind': 'KiroCliReleaseHostObservation', 'schema_version': 1, 'observed_at': _now(),
                  'host_files': [_observed_file(install / name) for name in
                                 ('kiro-cli.exe', 'node.exe', 'node.sha256', 'bun.exe', 'bun.sha256', 'tui.js', 'tui.js.sha256')
                                 if (install / name).is_file()],
                  'kas_directories': sorted(path.name for path in kas_root.iterdir() if path.is_dir()),
                  'release_first_use': first_use, 'cli_host_binary_compared': False,
                  'used_by_verification': False,
                  'scope': 'One-time host observation for the report. The installed host may update later; verification '
                           'relies only on the deterministic proof and the recorded run versions.'}
    hashes = {name: _create_json(root / relative, value) for name, relative, value in
              (('manifest', MANIFEST, manifest), ('proof', PROOF, proof), ('provenance', PROVENANCE, provenance))}
    return {'status': 'pass', 'files': proof['files'], 'identical_files': proof['identical_files'], **hashes}


def replay(sessions, controls, output, root=None):
    """Post-hoc replay: unchanged canonical result next to the exact release-identity replay."""
    root = Path(root or ROOT)
    output = Path(output)
    _need(not output.exists(), 'Preserve every earlier replay.', 'output_exists')
    verify_saved(root)
    output.mkdir(parents=True)
    rows = []
    for session in sessions:
        identity = _session_identity(session)
        canonical = _canonical_outcome(session)
        try:
            mapped = audit_native_run(session, root=root)
            name = Path(session).name + '-native-proof.json'
            row = {**identity, 'canonical': canonical, 'release_identity': {
                'status': 'pass', 'proof': str(output / name), 'proof_sha256': _create_json(output / name, mapped),
                'tool_calls': len(mapped['native']['tool_outcomes'].get('calls', [])),
                'evidence_hashes': mapped['native']['evidence_hashes']}}
        except (VisualError, OSError, ValueError, KeyError) as exc:
            row = {**identity, 'canonical': canonical, 'release_identity': {
                'status': 'fail', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}}
        rows.append(row)
    checks = []
    for session in controls:
        identity = _session_identity(session)
        outside = _canonical_outcome(session)
        with release_identity_policy(root=root) as applied:
            inside = _canonical_outcome(session)
        checks.append({**identity, 'canonical': outside, 'inside_resolver': inside, 'mappings_applied': len(applied),
                       'unchanged': inside == outside and not applied})
    summary = {'kind': 'ReleaseIdentityNativeReplaySummary', 'schema_version': 1, 'recorded_at': _now(),
               'proof': PROOF, 'proof_sha256': file_sha256(root / PROOF), 'manifest_sha256': file_sha256(root / MANIFEST),
               'red_receipt_sha256': file_sha256(root / RED), 'helper_sha256': file_sha256(__file__),
               'sessions': rows, 'controls': checks,
               'status': 'pass' if all(r['release_identity']['status'] == 'pass' for r in rows)
                         and all(c['unchanged'] for c in checks) else 'fail',
               'post_hoc_for_recorded_runs': True, 'historical_receipts_rewritten': False,
               'canonical_reader_modified': False, 'behavior_acceptance_granted': False,
               'scope': 'Native transport/tool-boundary evidence only. Earlier FAIL records stay unchanged; stage, '
                        'behavior, raw-review and source-authorship results are not changed.'}
    _create_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    red = commands.add_parser('red')
    red.add_argument('--session', type=Path, action='append', required=True)
    make = commands.add_parser('build')
    make.add_argument('--first-use-session', type=Path)
    commands.add_parser('verify')
    run = commands.add_parser('replay')
    run.add_argument('--session', type=Path, action='append', required=True)
    run.add_argument('--control', type=Path, action='append', default=[])
    run.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'red':
            value = record_red(args.session)
            value = {'path': value['path'], 'sha256': value['sha256'],
                     'sessions': [{'session': r['session'], **{k: r['canonical'].get(k) for k in ('status', 'code')}}
                                  for r in value['sessions']]}
        elif args.command == 'build':
            value = build(first_use_session=args.first_use_session)
        elif args.command == 'verify':
            proof = verify_saved()
            value = {'status': 'pass', 'files': proof['files'], 'identical_files': proof['identical_files'],
                     'proof_sha256': file_sha256(ROOT / PROOF)}
        else:
            summary = replay(args.session, args.control, args.output)
            value = {'status': summary['status'],
                     'sessions': [{'session': r['session'], 'canonical': r['canonical']['status'],
                                   'release_identity': r['release_identity']['status']} for r in summary['sessions']],
                     'controls': [{'session': c['session'], 'unchanged': c['unchanged']} for c in summary['controls']]}
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
