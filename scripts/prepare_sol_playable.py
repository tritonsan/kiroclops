"""Operator-only portable copy preparation. This script never starts a process."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.common import ROOT
from kiroclops.integration import background_capture_v1 as background
from kiroclops.integration.storage import copy_project, identifier, project_hashes, read_json, sha256


PLAY_CMD = r'''@echo off
setlocal EnableExtensions DisableDelayedExpansion
echo Diagnostic playable copy. Packaging does not grant experiment acceptance.
pushd "%~dp0"
if errorlevel 1 exit /b 1
set "APPDATA=%~dp0runtime\appdata"
set "LOCALAPPDATA=%~dp0runtime\localappdata"
set "KIROCLOPS_CONFIG="
if not exist "%~dp0runtime" mkdir "%~dp0runtime"
if not exist "%APPDATA%" mkdir "%APPDATA%"
if not exist "%LOCALAPPDATA%" mkdir "%LOCALAPPDATA%"
if not exist "%~dp0engine\Godot.exe" goto missing_engine
echo Preparing game assets. The first start may take a little longer.
"%~dp0engine\Godot.exe" --path "%~dp0project" --editor --headless --import --rendering-method gl_compatibility > "%~dp0runtime\import.log" 2>&1
if errorlevel 1 goto import_failed
"%~dp0engine\Godot.exe" --path "%~dp0project" --windowed --rendering-method gl_compatibility
set "GAME_EXIT=%ERRORLEVEL%"
popd
exit /b %GAME_EXIT%
:missing_engine
echo The bundled engine is missing. See manifest.json.
popd
pause
exit /b 1
:import_failed
echo Asset import failed. See runtime\import.log.
popd
pause
exit /b 1
'''.replace('\n', '\r\n').encode('ascii')


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _ordinary(path):
    return background._ordinary(path)


def _candidate_context(session_id, candidate_id):
    session_id, candidate_id = identifier(session_id), identifier(candidate_id)
    directory = _ordinary(ROOT / 'sessions' / session_id)
    state_path = _ordinary(directory / 'session.json')
    metadata_path = _ordinary(directory / 'candidates' / candidate_id / 'candidate.json')
    state, metadata = read_json(state_path), read_json(metadata_path)
    registration_path = _ordinary(ROOT / 'config/model-trials/sol-v1.json')
    registration = read_json(registration_path)
    if (registration.get('model') != 'gpt-5.6-sol'
            or session_id not in registration.get('cli_session_ids', [])
            or state.get('session_id') != session_id or state.get('experiment_id') != 'ground-motion-v1'
            or state.get('schema_version') != 3 or state.get('mode') != 'tools'
            or metadata.get('candidate_id') != candidate_id or not metadata.get('source_hashes')):
        raise ValueError('Expected an explicitly named immutable candidate in the registered Sol CLI cohort')
    matches = [row for row in state.get('candidates', []) if row.get('candidate_id') == candidate_id]
    if len(matches) != 1 or matches[0].get('source_hashes') != metadata['source_hashes']:
        raise ValueError('Candidate is not bound to the session snapshot')
    baseline = _ordinary(directory / 'baseline')
    project = _ordinary(metadata_path.parent / 'project')
    if Path(state['baseline']).resolve() != baseline:
        raise ValueError('Unexpected baseline location')
    before, candidate = project_hashes(baseline), project_hashes(project)
    if (before != state.get('baseline_hashes') or before != registration.get('starting_source_hashes')
            or candidate != metadata['source_hashes']):
        raise ValueError('Baseline or candidate source bytes changed')
    # Inspect every copied file as well as its ancestors before any destination write.
    for relative in candidate:
        _ordinary(project / relative)
    receipt_path = _ordinary(directory / background.RECEIPT)
    receipt = read_json(receipt_path)
    if sha256(receipt_path) != registration.get('prepared_sessions', {}).get(session_id, {}).get('background_receipt_sha256'):
        raise ValueError('Background receipt is not the one bound at cohort registration')
    try:
        original = base64.b64decode(receipt['project_settings_before_base64'], validate=True)
        unwrapped = receipt['before_source_hashes']
        expected = {**unwrapped, 'project.godot': _digest(background.autoload_bytes(original)),
                    'override.cfg': background.OVERRIDE_SHA256, background.SUPPORT_PATH: background.SUPPORT_SHA256}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Invalid original project settings in the background receipt') from exc
    if (receipt.get('schema_version') != 1 or receipt.get('mode') != background.MODE
            or receipt.get('configuration_revision') != background.CONFIGURATION_REVISION
            or receipt.get('session_id') != session_id or receipt.get('surface') != 'cli'
            or receipt.get('installed') is not True or receipt.get('acceptance_credit') is not False
            or receipt.get('settings_source_files') != background.SETTINGS_FILES
            or receipt.get('changed_source_files') != background.SETTINGS_FILES
            or receipt.get('override_sha256') != background.OVERRIDE_SHA256
            or receipt.get('support_sha256') != background.SUPPORT_SHA256
            or unwrapped.get('project.godot') != _digest(original)
            or 'override.cfg' in unwrapped or background.SUPPORT_PATH in unwrapped
            or receipt.get('after_source_hashes') != expected or before != expected):
        raise ValueError('Background receipt does not prove the exact three-file transformation')
    for relative, raw in (('project.godot', background.autoload_bytes(original)),
                          ('override.cfg', background.OVERRIDE_BYTES),
                          (background.SUPPORT_PATH, background.SUPPORT_BYTES)):
        if candidate.get(relative) != expected[relative] or (project / relative).read_bytes() != raw:
            raise ValueError('Candidate background settings changed; refusing to remove them')
    return {'directory': directory, 'project': project, 'baseline': baseline, 'state': state,
            'candidate_id': candidate_id, 'candidate_hashes': candidate, 'baseline_hashes': before,
            'original_settings': original, 'receipt_path': receipt_path,
            'receipt_sha256': sha256(receipt_path), 'registration_sha256': sha256(registration_path),
            'metadata_sha256': sha256(metadata_path)}


def prepare_playable(session_id, candidate_id, output, *, engine=None):
    """Create a fresh ROOT/playables child; never launch Godot or a shell.

    Every package stays explicitly diagnostic, including packages from a session
    that may later pass. Packaging neither adjudicates nor grants acceptance.
    """
    output = Path(output)
    if not output.is_absolute():
        output = ROOT / output
    output = _ordinary(output)
    playables = _ordinary(ROOT / 'playables')
    if output.parent != playables or output.exists():
        raise ValueError('Output must be a new immediate child of this workspace playables directory')
    context = _candidate_context(session_id, candidate_id)
    engine = _ordinary(engine or ROOT / '.tools/godot4/Godot_v4.5.1-stable_win64.exe')
    if (not engine.is_file() or engine.suffix.lower() != '.exe' or '_console' in engine.name.lower()
            or engine.stat().st_size < 1024 * 1024):
        raise ValueError('Supply the standalone Godot Windows executable, not its small console wrapper')
    with engine.open('rb') as handle:
        if handle.read(2) != b'MZ':
            raise ValueError('Expected a Windows executable')
    engine_sha = sha256(engine)
    staging_name = '.' + output.name + '.preparing-' + uuid.uuid4().hex
    staging = output.parent / staging_name
    staging = _ordinary(staging)
    staging.mkdir(parents=True)
    try:
        copied = copy_project(context['project'], staging / 'project')
        if project_hashes(copied) != context['candidate_hashes']:
            raise ValueError('Copy differs from the immutable candidate')
        (copied / 'project.godot').write_bytes(context['original_settings'])
        (copied / 'override.cfg').unlink()
        (copied / background.SUPPORT_PATH).unlink()
        expected_copy = {key: value for key, value in context['candidate_hashes'].items()
                         if key not in {'override.cfg', background.SUPPORT_PATH}}
        expected_copy['project.godot'] = _digest(context['original_settings'])
        if project_hashes(copied) != expected_copy:
            raise ValueError('Playable transformation changed more than the proven background wrapper')
        (staging / 'engine').mkdir()
        shutil.copy2(engine, staging / 'engine/Godot.exe')
        if sha256(staging / 'engine/Godot.exe') != engine_sha:
            raise ValueError('Bundled engine copy changed')
        # Re-read source identity without freezing or changing the running session.
        final = _candidate_context(session_id, candidate_id)
        for key in ('candidate_hashes', 'baseline_hashes', 'receipt_sha256', 'registration_sha256', 'original_settings'):
            if context[key] != final[key]:
                raise ValueError('Source or receipt changed during packaging')
        manifest = {'schema_version': 1, 'kind': 'diagnostic_playable_copy',
                    'created_at': datetime.now(timezone.utc).isoformat(), 'session_id': session_id,
                    'candidate_id': candidate_id, 'session_status_observed': final['state']['status'],
                    'selected_candidate_observed': final['state'].get('selected_candidate_id'),
                    'diagnostic': True, 'acceptance_credit': False, 'fully_accepted_verified': False,
                    'acceptance_note': 'Diagnostic copy. Packaging does not verify or grant experiment acceptance.',
                    'source_project': str(context['project']), 'output': str(output),
                    'source_hashes_before': context['candidate_hashes'], 'source_hashes_after': final['candidate_hashes'],
                    'playable_source_hashes': expected_copy, 'movement_source_bytes_preserved': True,
                    'restored_project_settings_sha256': _digest(context['original_settings']),
                    'removed_source_files': ['override.cfg', background.SUPPORT_PATH],
                    'background_receipt': {'path': str(context['receipt_path']), 'sha256': context['receipt_sha256']},
                    'registration_sha256': context['registration_sha256'],
                    'candidate_metadata_sha256_observed': final['metadata_sha256'],
                    'engine': {'original_path': str(engine), 'path': 'engine/Godot.exe', 'sha256': engine_sha,
                               'version_executed_or_verified_by_packager': False},
                    'launcher': {'path': 'Play.cmd', 'sha256': _digest(PLAY_CMD), 'invoked': False},
                    'copy_policy': 'Original sources and UID/import sidecars retained; generated caches and session/tool metadata excluded by the existing snapshot copier.',
                    'limitations': ['No game, editor, model or shell was launched by this packager.',
                                    'Play.cmd imports assets on the user first start, then opens the original main scene with local save directories.',
                                    'Source hashes describe packaging time; subsequent play may generate import caches or save data.']}
        (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        (staging / 'Play.cmd').write_bytes(PLAY_CMD)
        _ordinary(output)
        if output.exists():
            raise ValueError('Destination appeared during packaging; refusing to replace it')
        staging.rename(output)
        return output
    finally:
        if staging.exists():
            # This uniquely-created staging directory was checked inside ROOT/playables.
            resolved = _ordinary(staging)
            if resolved != staging or not resolved.is_relative_to(playables) or staging.name != staging_name:
                raise ValueError('Refusing to clean an unexpected staging path')
            shutil.rmtree(resolved)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, help='Registered Sol session identifier')
    parser.add_argument('--candidate', required=True, help='Explicit immutable candidate identifier')
    parser.add_argument('--output', required=True, help='Fresh folder under this workspace playables/')
    parser.add_argument('--engine', type=Path, help='Standalone Windows Godot executable; never executed now')
    args = parser.parse_args(argv)
    try:
        output = prepare_playable(args.session, args.candidate, args.output, engine=args.engine)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f'Playable preparation failed: {exc}\n')
    print(output)
    print('DIAGNOSTIC copy prepared; game not started. Open Play.cmd yourself when ready.')


if __name__ == '__main__':
    main()
