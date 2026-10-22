"""Opt-in startup configuration for new, non-focusing Windows captures.

Adds override.cfg and an early, protected own-window autoload. Rendering,
viewport size, input timing and movement code are unchanged. Installation is
not proof of successful offscreen rendering;
the operator must complete a separate real-pixel preflight before model trials.
"""
from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import uuid

from . import ground_worker
from .session import Session
from .storage import (ROOT, IntegrationError, _retry_permission, assert_allowed_changes,
                      file_lock, project_hashes, read_json, sha256, utc_now)

OVERRIDE_BYTES = b'''config_version=5

[display]
window/size/no_focus=true
window/size/borderless=false
window/size/resizable=true
window/size/mode=0
window/size/initial_position_type=0
window/size/initial_position=Vector2i(-10000, -10000)
window/size/always_on_top=false
window/per_pixel_transparency/enabled=false
'''
OVERRIDE_SHA256 = hashlib.sha256(OVERRIDE_BYTES).hexdigest()
SUPPORT_PATH = 'kiroclops_background/startup.gd'
AUTOLOAD_NAME = 'KiroClopsBackgroundStartup'
AUTOLOAD_LINE = f'{AUTOLOAD_NAME}="*res://{SUPPORT_PATH}"'
SUPPORT_BYTES = b'''extends Node
# Godot 4.5.1 Windows clamps initial positions onto a monitor. Move only our
# already non-activating window, before the main scene enters the tree.
const OFFSCREEN := Vector2i(-10000, -10000)
const FAILURE_EXIT := 74
var failed := false

func _enter_tree() -> void:
    process_priority = -1000
    get_tree().root.set_meta("kiroclops_background_configured", false)
    if DisplayServer.get_name() != "Windows":
        _fail("A real Windows display server is required")
        return
    DisplayServer.window_set_position(OFFSCREEN)
    if _verify():
        get_tree().root.set_meta("kiroclops_background_configured", true)

func _process(_delta: float) -> void:
    if failed:
        return
    if DisplayServer.window_get_position() != OFFSCREEN:
        DisplayServer.window_set_position(OFFSCREEN)
    _verify()

func _verify() -> bool:
    if not DisplayServer.window_get_flag(DisplayServer.WINDOW_FLAG_NO_FOCUS):
        _fail("The capture window lost its no-focus flag")
    elif DisplayServer.window_is_focused():
        _fail("The capture window unexpectedly received focus")
    elif DisplayServer.window_get_mode() != DisplayServer.WINDOW_MODE_WINDOWED:
        _fail("The capture window is no longer windowed")
    elif DisplayServer.window_get_position() != OFFSCREEN:
        _fail("The capture window is not at the required offscreen position")
    return not failed

func _fail(message: String) -> void:
    failed = true
    get_tree().root.set_meta("kiroclops_background_configured", false)
    push_error("KiroClops background capture: " + message)
    get_tree().quit(FAILURE_EXIT)
'''
SUPPORT_SHA256 = hashlib.sha256(SUPPORT_BYTES).hexdigest()
SETTINGS_FILES = ['override.cfg', SUPPORT_PATH, 'project.godot']
RECEIPT = 'background-capture-v1.json'
MODE = 'windows_no_focus_offscreen_v1'
CONFIGURATION_REVISION = 2
_KEYS = ('window/size/no_focus', 'window/size/borderless', 'window/size/mode',
         'window/size/initial_position_type', 'window/size/initial_position', 'window/size/always_on_top',
         'window/size/resizable', 'window/per_pixel_transparency/enabled')


def _ordinary(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise IntegrationError('unsafe_path', 'Background setup rejects linked paths')
    for component in (path, *path.parents):
        if component.is_symlink() or (component.exists() and getattr(component.lstat(), 'st_file_attributes', 0) & 0x400):
            raise IntegrationError('unsafe_path', 'Background setup rejects reparse points')
    if path.is_file() and path.stat().st_nlink != 1:
        raise IntegrationError('unsafe_path', 'Background setup rejects shared file inodes')
    return path


def _validate_settings(raw):
    text = raw.decode('utf-8-sig')
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('['):
            section = line
        if '=' not in line or line.startswith(';'):
            continue
        key, value = (part.strip() for part in line.split('=', 1))
        if section == '[display]' and any(key.startswith(name + '.') for name in _KEYS):
            raise IntegrationError('background_conflict', 'Feature-tagged window settings need an explicit separate design')
        if section == '[application]' and key.startswith('config/project_settings_override') and value != '""':
            raise IntegrationError('background_conflict', 'A later custom project-settings override could replace no_focus')


def _autoload(original):
    """Insert one first autoload; retain every byte of the original settings."""
    _validate_settings(original)
    text = original.decode('utf-8-sig')
    if AUTOLOAD_NAME in text or SUPPORT_PATH in text:
        raise IntegrationError('background_conflict', 'The background autoload is already configured')
    lines = original.splitlines(keepends=True)
    sections = [i for i, line in enumerate(lines) if line.strip() == b'[autoload]']
    if len(sections) > 1:
        raise IntegrationError('background_conflict', 'Duplicate autoload sections are not supported')
    eol = b'\r\n' if b'\r\n' in original else b'\n'
    addition = AUTOLOAD_LINE.encode('utf-8') + eol
    if sections:
        index = sections[0] + 1
        if not lines[index - 1].endswith((b'\n', b'\r')):
            lines[index - 1] += eol
        lines.insert(index, addition)
        return b''.join(lines)
    separator = b'' if not original or original.endswith((b'\n', b'\r')) else eol
    return original + separator + eol + b'[autoload]' + eol + addition


def _project(project, expected):
    project = _ordinary(project)
    config = _ordinary(project / 'override.cfg')
    support = _ordinary(project / SUPPORT_PATH)
    original = _ordinary(project / 'project.godot')
    if (not project.is_dir() or not original.is_file() or config.exists()
            or support.parent.exists()):
        raise IntegrationError('background_not_fresh', 'Expected a copied Godot project with no background configuration')
    _autoload(original.read_bytes())
    current = project_hashes(project)
    if not expected or current != expected:
        raise IntegrationError('source_changed', 'Copied source bytes differ from the explicitly expected source map')
    return current


def _expected_after(before, original):
    if before.get('project.godot') != hashlib.sha256(original).hexdigest():
        raise IntegrationError('background_integrity', 'Original project settings do not match the before source map')
    return {**before, 'override.cfg': OVERRIDE_SHA256, SUPPORT_PATH: SUPPORT_SHA256,
            'project.godot': hashlib.sha256(_autoload(original)).hexdigest()}


def _added(before, project, original):
    after = project_hashes(project)
    if (after != _expected_after(before, original)
            or _ordinary(project / 'override.cfg').read_bytes() != OVERRIDE_BYTES
            or _ordinary(project / SUPPORT_PATH).read_bytes() != SUPPORT_BYTES
            or _ordinary(project / 'project.godot').read_bytes() != _autoload(original)):
        raise IntegrationError('source_changed', 'Background setup may change only its exact three settings files')
    return after


def _exclusive(path, raw):
    created = False
    try:
        with path.open('xb') as stream:
            created = True
            stream.write(raw)
    except BaseException:
        if created:
            _retry_permission(lambda: path.unlink())
        raise


def _atomic_bytes(path, raw):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        _exclusive(temporary, raw)
        _retry_permission(lambda: os.replace(temporary, path))
    finally:
        _retry_permission(lambda: temporary.unlink(missing_ok=True))


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def autoload_bytes(original: bytes) -> bytes:
    """Pure exact-byte transformation, also usable by independent auditors."""
    return _autoload(original)


def _disposable(project, disposable_root):
    root, project = _ordinary(disposable_root), _ordinary(project)
    if (project == root or not project.is_relative_to(root) or not root.is_dir()
            or any(project.is_relative_to((ROOT / name).resolve()) for name in ('sessions', 'vendor', 'work', 'config', 'adapters', 'kiroclops'))):
        raise IntegrationError('invalid_disposable_path', 'Use a copied project beneath an explicit disposable directory')
    return project


def _rollback_install(change):
    if change['attempted_project']:
        _atomic_bytes(change['project'] / 'project.godot', change['original'])
    for path in reversed(change['created']):
        _retry_permission(lambda path=path: path.unlink())
    if change['created_directory']:
        _retry_permission(lambda: (change['project'] / SUPPORT_PATH).parent.rmdir())


def _install(project, before):
    _project(project, before)
    original = (project / 'project.godot').read_bytes()
    change = {'project': project, 'original': original, 'created': [],
              'created_directory': False, 'attempted_project': False}
    try:
        support = project / SUPPORT_PATH
        support.parent.mkdir()
        change['created_directory'] = True
        for path, raw in ((project / 'override.cfg', OVERRIDE_BYTES), (support, SUPPORT_BYTES)):
            _exclusive(path, raw)
            change['created'].append(path)
        change['attempted_project'] = True
        _atomic_bytes(project / 'project.godot', _autoload(original))
        _added(before, project, original)
        return change
    except BaseException:
        _rollback_install(change)
        raise


def _record(before, after, original, *, installed):
    return {'schema_version': 1, 'mode': MODE, 'configuration_revision': CONFIGURATION_REVISION,
            'override_sha256': OVERRIDE_SHA256, 'support_sha256': SUPPORT_SHA256,
            'before_source_hashes': before, 'after_source_hashes': after,
            'project_settings_before_base64': base64.b64encode(original).decode('ascii'),
            'changed_source_files': SETTINGS_FILES.copy() if installed else [],
            'settings_source_files': SETTINGS_FILES.copy(), 'installed': installed,
            'movement_source_bytes_preserved': True, 'runtime_focus_verified': False,
            'pixel_equivalence_verified': False, 'acceptance_credit': False}


def install_disposable_project(project, *, disposable_root, expected_source_hashes):
    """Add exact startup settings to an explicit disposable copy, never a session.

    The caller creates the copy and supplies its independently computed map.
    Returns before/after evidence for the caller's preflight receipt; no engine
    or model is launched. Only the autoload declaration changes project.godot.
    """
    project = _disposable(project, disposable_root)
    before = _project(project, expected_source_hashes)
    change = _install(project, before)
    return {**_record(before, _expected_after(before, change['original']), change['original'], installed=True),
            'project': str(project)}


def verify_disposable_project(project, *, disposable_root, expected_source_hashes):
    """Read-only exact current-map check for copies which already carry setup.

    This proves protected configuration, not its historical installation or
    runtime focus. The caller must independently supply the original copy map.
    """
    project = _disposable(project, disposable_root)
    current = project_hashes(project)
    if not expected_source_hashes or current != expected_source_hashes:
        raise IntegrationError('source_changed', 'Disposable source differs from the caller-supplied source map')
    for relative, raw in (('override.cfg', OVERRIDE_BYTES), (SUPPORT_PATH, SUPPORT_BYTES)):
        path = _ordinary(project / relative)
        if not path.is_file() or path.read_bytes() != raw:
            raise IntegrationError('background_integrity', 'Existing background support is missing or not exact')
    raw = _ordinary(project / 'project.godot').read_bytes()
    _validate_settings(raw)
    lines = raw.decode('utf-8-sig').splitlines()
    sections = [i for i, line in enumerate(lines) if line.strip() == '[autoload]']
    if (len(sections) != 1 or sections[0] + 1 >= len(lines)
            or lines[sections[0] + 1] != AUTOLOAD_LINE
            or sum(AUTOLOAD_NAME in line or SUPPORT_PATH in line for line in lines) != 1):
        raise IntegrationError('background_integrity', 'Existing background autoload must be the exact first entry')
    return {**_record(current, current, raw, installed=False), 'project': str(project), 'status': 'pass'}


def ensure_disposable_project(project, *, disposable_root, expected_source_hashes):
    """Install absent setup or strictly verify an already-configured copy."""
    project = _disposable(project, disposable_root)
    if (_ordinary(project / 'override.cfg').exists() or _ordinary(project / SUPPORT_PATH).parent.exists()
            or AUTOLOAD_NAME in _ordinary(project / 'project.godot').read_text(encoding='utf-8-sig')):
        return verify_disposable_project(project, disposable_root=disposable_root,
                                         expected_source_hashes=expected_source_hashes)
    return install_disposable_project(project, disposable_root=disposable_root,
                                      expected_source_hashes=expected_source_hashes)


def _fresh(session):
    directory = _ordinary(session.directory)
    path = _ordinary(session.path)
    state = ground_worker.verify_session(session)
    if (state.get('session_id') != directory.name or state.get('status') != 'new'
            or state.get('started_at') is not None or state.get('deadline_at') is not None
            or state.get('candidates') != [] or state.get('selected_candidate_id') is not None
            or state.get('active_job_id') is not None or state.get('final') is not None
            or state.get('reference') or state.get('baseline_runs')):
        raise IntegrationError('background_not_fresh', 'Only unused ground tools sessions may opt into background startup')
    for relative in ('kiro/launch.json', 'kiro/launch-start.json', 'ide/finish-receipt.json', 'ide/operator-stop.json', RECEIPT):
        if _ordinary(directory / relative).exists():
            raise IntegrationError('background_not_fresh', 'Existing launch/setup evidence must be preserved')
    for name in ('jobs', 'candidates'):
        folder = _ordinary(directory / name)
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise IntegrationError('background_not_fresh', 'A session with prior jobs or candidate artifacts is not fresh')
    marker = _ordinary(directory / 'target/.kiro/kiroclops-session.json')
    if marker.exists() and read_json(marker).get('surface') == 'ide':
        raise IntegrationError('background_cli_only', 'Background trials use the CLI, not an IDE-configured session')
    expected = state['baseline_hashes']
    for name in ('baseline', 'target'):
        project = _ordinary(directory / name)
        if Path(state[name]).resolve() != project:
            raise IntegrationError('unsafe_path', 'Expected session-owned baseline and target directories')
        _project(project, expected)
    return state, path.read_bytes()


def configure_background_session(session_dir):
    """Install both copies transactionally before any clock, job or candidate.

    The only session fields changed are baseline_hashes and
    prepared_baseline_hashes. The original immutable ground contract is checked
    before and after, and the new receipt grants no experiment acceptance.
    """
    directory = _ordinary(session_dir)
    session = Session(directory)
    context = _fresh(session)
    lock = _ordinary(directory / '.session.lock')
    with file_lock(lock):
        if _fresh(session) != context:
            raise IntegrationError('source_changed', 'Session changed while waiting for the setup lock')
        state, original_bytes = context
        before = state['baseline_hashes']
        installed, attempted_state, created_receipt = [], False, False
        receipt_path = directory / RECEIPT
        try:
            for name in ('baseline', 'target'):
                installed.append(_install(directory / name, before))
            original_project = installed[0]['original']
            if any(item['original'] != original_project for item in installed):
                raise IntegrationError('source_changed', 'Baseline and target original settings differ')
            after = _added(before, directory / 'baseline', original_project)
            updated = deepcopy(state)
            updated['baseline_hashes'] = after
            updated['prepared_baseline_hashes'] = after
            updated_bytes = _json_bytes(updated)
            attempted_state = True
            _atomic_bytes(session.path, updated_bytes)
            if ground_worker.verify_session(session) != updated:
                raise IntegrationError('experiment_integrity', 'Ground verification returned a different updated session')
            _added(before, directory / 'target', original_project)
            receipt = {**_record(before, after, original_project, installed=True),
                'session_id': directory.name, 'surface': 'cli', 'created_at': utc_now(),
                'implementation_sha256': sha256(Path(__file__)),
                'state_before_sha256': hashlib.sha256(original_bytes).hexdigest(),
                'state_after_sha256': hashlib.sha256(updated_bytes).hexdigest(),
                'changed_state_fields': ['baseline_hashes', 'prepared_baseline_hashes'],
                'ground_contract_verified_before_and_after': True,
                'limitations': ['Static startup configuration only; a real-pixel, non-focusing preflight is still required.',
                                'Windows may briefly show a non-activating window before the first autoload moves it offscreen.',
                                'No viewport, renderer, input schedule, movement code, clock or budget is changed.']}
            _exclusive(receipt_path, _json_bytes(receipt))
            created_receipt = True
            verify_background_session(directory)
        except BaseException:
            if created_receipt:
                _retry_permission(lambda: receipt_path.unlink())
            if attempted_state:
                _atomic_bytes(session.path, original_bytes)
            for change in reversed(installed):
                _rollback_install(change)
            raise
    return receipt_path


def verify_background_session(session_dir):
    """Read-only verification of the installed baseline and protected settings.

    Later allowed player edits remain permitted; this does not attest their
    correctness or promote the installation receipt to runtime evidence.
    """
    directory = _ordinary(session_dir)
    session = Session(directory)
    state = ground_worker.verify_session(session)
    receipt = read_json(_ordinary(directory / RECEIPT))
    before, after = receipt.get('before_source_hashes'), receipt.get('after_source_hashes')
    try:
        original = base64.b64decode(receipt['project_settings_before_base64'], validate=True)
        expected_after = _expected_after(before, original) if isinstance(before, dict) else None
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrationError('background_integrity', 'Background receipt has invalid original project settings') from exc
    if (receipt.get('schema_version') != 1 or receipt.get('mode') != MODE
            or receipt.get('configuration_revision') != CONFIGURATION_REVISION
            or receipt.get('session_id') != directory.name or receipt.get('surface') != 'cli'
            or receipt.get('override_sha256') != OVERRIDE_SHA256 or receipt.get('support_sha256') != SUPPORT_SHA256
            or not isinstance(before, dict) or after != expected_after
            or receipt.get('changed_source_files') != SETTINGS_FILES
            or receipt.get('settings_source_files') != SETTINGS_FILES or receipt.get('installed') is not True
            or state['baseline_hashes'] != after or receipt.get('acceptance_credit') is not False
            or receipt.get('runtime_focus_verified') is not False or receipt.get('pixel_equivalence_verified') is not False):
        raise IntegrationError('background_integrity', 'Background receipt does not match the current session baseline')
    for name in ('baseline', 'target'):
        project = _ordinary(directory / name)
        if Path(state[name]).resolve() != project:
            raise IntegrationError('background_integrity', 'The protected project path changed')
        for relative, raw in (('override.cfg', OVERRIDE_BYTES), (SUPPORT_PATH, SUPPORT_BYTES),
                              ('project.godot', _autoload(original))):
            if _ordinary(project / relative).read_bytes() != raw:
                raise IntegrationError('background_integrity', 'The exact protected startup settings changed')
    assert_allowed_changes(directory / 'target', after)
    return {'status': 'pass', 'session_id': directory.name, 'mode': MODE,
            'configuration_revision': CONFIGURATION_REVISION,
            'override_sha256': OVERRIDE_SHA256, 'support_sha256': SUPPORT_SHA256,
            'receipt_sha256': sha256(directory / RECEIPT),
            'scope': 'static startup settings and source preservation', 'runtime_focus_verified': False,
            'pixel_equivalence_verified': False, 'acceptance_credit': False}
