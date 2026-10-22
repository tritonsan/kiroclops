"""One immutable imported target image per job/source; fresh trials only.

The caller owns the common capture lease. No frames, analyses, input recipes or
comparison results are cached. Import and trial receipts remain replayable.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import time
import uuid

from kiroclops.common import ROOT
from kiroclops.integration.storage import copy_project
from kiroclops.portable_v2 import runtime
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from .backend import _validate_job_id
from .session import _ordinary, source_hashes
from .source_inventory import full_byte_inventory
from . import reliability


VERSION = 1
ROLES = frozenset({'original', 'before', 'candidate', 'final'})
IMPORT_RUNTIME_OUTPUTS = frozenset({'editor', 'shader_cache'})


def _need(value, message, code='prepared_target_changed'):
    if not value:
        raise VisualError(code, message)


def source_closure():
    """Actual preparation/capture/receipt code, not only a policy label."""
    paths = [Path(__file__), Path(runtime.__file__), Path(runtime.private.__file__),
             ROOT / 'kiroclops/integration/storage.py', ROOT / 'kiroclops/local_engine/session.py',
             ROOT / 'kiroclops/local_engine/source_inventory.py',
             ROOT / 'kiroclops/local_engine/suite_game.py', ROOT / 'kiroclops/local_engine/reliability.py',
             ROOT / 'kiroclops/local_engine/backend.py', ROOT / 'kiroclops/visual/schema.py']
    return {p.relative_to(ROOT).as_posix(): file_sha256(p) for p in paths}


def _import_products(project):
    directory = _ordinary(project / '.godot')
    _need(directory.is_dir(), 'Prepared target has no actual Godot import products.')
    return full_byte_inventory(directory, ignored=IMPORT_RUNTIME_OUTPUTS, excluded_suffixes=())


def _verify_import(root, project, sources, summary, hashes):
    imported = root / 'import'
    _need(full_byte_inventory(imported, ignored=(), excluded_suffixes=()) == hashes,
          'Saved import process/environment evidence changed.')
    summary_path = _ordinary(imported / 'import-result.json')
    _need(read_json(summary_path) == summary and summary.get('import_summary_path') == str(summary_path)
          and summary.get('status') == 'completed' and summary.get('kind') == 'import'
          and summary.get('scene') is None and summary.get('config') is None,
          'Prepared target needs a completed source-bound import receipt.')
    attempts = summary.get('import_attempts')
    _need(isinstance(attempts, list) and len(attempts) in (1, 2), 'Import attempt scope differs.')
    final = imported / (('clean-retry/' if len(attempts) == 2 else '') + 'engine.' + runtime.VERSION) / 'bridge.json'
    _need(summary.get('receipt_path') == str(final)
          and summary.get('project') == str(project)
          and summary.get('source_hashes_before') == summary.get('source_hashes_after') == sources
          and summary.get('protocol') == runtime.protocol_record(), 'Import belongs to another target/runtime.')
    for index, attempt in enumerate(attempts):
        expected = imported / ('engine.' + runtime.VERSION) / 'bridge.json' if index == 0 else final
        _need(_ordinary(attempt.get('path', '')) == expected
              and attempt.get('sha256') == file_sha256(expected)
              and attempt.get('status') == ('completed' if index == len(attempts) - 1 else 'initial_dependency_load_failed'),
              'Import attempt receipt differs.')
    final_value = read_json(final)
    _need(summary == {**final_value, 'import_attempts': attempts, 'import_summary_path': str(summary_path)},
          'Saved import summary differs from its actual final native receipt.')
    proof = runtime.verify_runtime_receipt(final, project=project, expected_source_hashes=sources)
    _need(proof.get('status') == 'pass', 'Prepared target import proof failed replay.')
    if len(attempts) == 2:
        _need(runtime._cold_import_dependency_failure(project, imported,
              runtime.PortableRuntimeError('engine_error', 'replay recorded qualified import retry')),
              'First import attempt is not the qualified cold dependency retry.')


def prepare_target(project, output, *, job_id, role, expected_full_sources=None,
                   check=lambda: None, progress=lambda *args: None, cancel=None):
    """Copy/import once; publish the immutable pool receipt only after verification."""
    _validate_job_id(job_id)
    _need(role in ROLES, 'Unknown prepared target source role.', 'wrong_role')
    project, output = _ordinary(project), _ordinary(output)
    _need(not output.exists() and not output.is_relative_to(project) and not project.is_relative_to(output),
          'Prepared pool must be a fresh disposable directory outside source.', 'output_exists')
    check(); runtime._cancel(cancel)
    code, policy = source_closure(), runtime.protocol_record()
    full, native = source_hashes(project), runtime.source_hashes(project)
    _need(expected_full_sources is None or full == expected_full_sources,
          'Pool preparation received another immutable source.', 'source_changed')
    rendered = output / 'project'
    planned = [output / operation / ('engine.' + runtime.VERSION) / 'environment' / tail
               for operation in ('import', 'import/clean-retry', 't-' + '0' * 12)
               for tail in ('tmp', 'localappdata/Godot/app_userdata/KiroClops/portable-heart/tmp')]
    reliability.check_path_budget(planned)
    output.mkdir(parents=True)
    copy_project(project, rendered)
    _need(source_hashes(rendered) == full and runtime.source_hashes(rendered) == native,
          'Disposable prepared image differs from selected source.', 'source_changed')
    progress('prepared_target_import', 0, 1)
    summary = runtime.import_project(rendered, output / 'import', cancel=cancel)
    check(); runtime._cancel(cancel)
    _need(source_hashes(project) == full and runtime.source_hashes(project) == native
          and runtime.source_hashes(rendered) == native and source_closure() == code
          and runtime.protocol_record() == policy, 'Source/runtime changed during pool preparation.', 'source_changed')
    rendered_full = source_hashes(rendered)
    # Import may add generated UID/import sidecars. Bind the resulting image
    # independently; never call those additions part of the selected source.
    protected = {name: value for name, value in full.items()
                 if Path(name).suffix.lower() not in {'.uid', '.import'}}
    _need(all(rendered_full.get(name) == value for name, value in protected.items())
          and all(name in full or Path(name).suffix.lower() in {'.uid', '.import'} for name in rendered_full),
          'Import changed gameplay source or introduced an unregistered source file.', 'source_changed')
    import_hashes = full_byte_inventory(output / 'import', ignored=(), excluded_suffixes=())
    _verify_import(output, rendered, native, summary, import_hashes)
    products = _import_products(rendered)
    receipt = {'schema_version': VERSION, 'kind': 'PreparedSuiteTargetPool', 'status': 'ready',
        'job_id': job_id, 'role': role, 'created_at': time.time(), 'root': str(output),
        'selected_source': str(project), 'project': str(rendered), 'full_source_hashes': full,
        'source_sha256': digest(full), 'native_source_hashes': native, 'native_source_sha256': digest(native),
        'rendered_full_source_hashes': rendered_full, 'protocol': policy, 'source_closure': code,
        'import_summary': summary, 'import_evidence_hashes': import_hashes,
        'import_product_hashes': products, 'excluded_import_runtime_outputs': sorted(IMPORT_RUNTIME_OUTPUTS),
        'fresh_process_per_trial': True, 'fresh_userdata_per_trial': True,
        'cached_scope': 'Imported disposable project only; no recordings, measurements or input recipes.'}
    receipt_path = output / 'prepared-target.json'
    write_json(receipt_path, receipt)
    descriptor = {'schema_version': VERSION, 'kind': 'PreparedSuiteTarget', 'receipt_path': str(receipt_path),
        'receipt_sha256': file_sha256(receipt_path), 'job_id': job_id, 'role': role,
        'source_sha256': digest(full), 'native_source_sha256': digest(native)}
    verify_prepared(descriptor, project, job_id=job_id, role=role, expected_full_sources=full)
    progress('prepared_target_import', 1, 1)
    return descriptor


def verify_prepared(prepared, project, *, job_id, role, expected_full_sources=None):
    """Replay import/source/cache identity without starting a native process."""
    _validate_job_id(job_id)
    _need(role in ROLES and isinstance(prepared, dict) and prepared.get('schema_version') == VERSION
          and prepared.get('kind') == 'PreparedSuiteTarget', 'Prepared target descriptor is malformed.')
    receipt_path = _ordinary(prepared['receipt_path'])
    root = receipt_path.parent
    _need(receipt_path.name == 'prepared-target.json'
          and file_sha256(receipt_path) == prepared.get('receipt_sha256'), 'Prepared target receipt changed.')
    receipt = read_json(receipt_path)
    selected, rendered = _ordinary(project), root / 'project'
    full, native = source_hashes(selected), runtime.source_hashes(selected)
    _need(receipt.get('kind') == 'PreparedSuiteTargetPool' and receipt.get('schema_version') == VERSION
          and receipt.get('status') == 'ready' and receipt.get('job_id') == prepared.get('job_id') == job_id
          and receipt.get('role') == prepared.get('role') == role
          and receipt.get('root') == str(root) and receipt.get('project') == str(rendered)
          and receipt.get('selected_source') == str(selected)
          and receipt.get('full_source_hashes') == full
          and receipt.get('source_sha256') == prepared.get('source_sha256') == digest(full)
          and receipt.get('native_source_hashes') == native
          and receipt.get('native_source_sha256') == prepared.get('native_source_sha256') == digest(native)
          and (expected_full_sources is None or full == expected_full_sources),
          'Prepared target belongs to another job, role or full/native source.')
    _need(receipt.get('source_closure') == source_closure() and receipt.get('protocol') == runtime.protocol_record()
          and receipt.get('excluded_import_runtime_outputs') == sorted(IMPORT_RUNTIME_OUTPUTS)
          and receipt.get('fresh_process_per_trial') is True and receipt.get('fresh_userdata_per_trial') is True,
          'Prepared target runtime/code/freshness policy changed.')
    _need(source_hashes(rendered) == receipt.get('rendered_full_source_hashes')
          and runtime.source_hashes(rendered) == native
          and _import_products(rendered) == receipt.get('import_product_hashes'),
          'Prepared image or imported products changed; code edits are forbidden.')
    _verify_import(root, rendered, native, receipt['import_summary'], receipt['import_evidence_hashes'])
    return receipt


def capture_case(prepared, project, scenario, *, job_id, role, trial_id=None,
                 expected_full_sources=None, check=lambda: None, cancel=None):
    """Run a new native process/clock/userdir and record new pixels for one case."""
    check(); runtime._cancel(cancel)
    receipt = verify_prepared(prepared, project, job_id=job_id, role=role,
                              expected_full_sources=expected_full_sources)
    scenario = runtime.canonical_scenario(scenario)
    expected_variant = 'before' if role in ('original', 'before') else 'after'
    _need(scenario.get('variant') == expected_variant and scenario['warmup_ticks'] == 60
          and scenario['frame_count'] == 240, 'Prepared target cannot alter role or native clock.', 'wrong_role')
    trial_id = trial_id or uuid.uuid4().hex[:12]
    _need(isinstance(trial_id, str) and re.fullmatch(r'[0-9a-f]{12}', trial_id),
          'Trial identity must be a fresh short server-owned identifier.', 'invalid_job_id')
    root, rendered = _ordinary(receipt['root']), _ordinary(receipt['project'])
    raw = root / ('t-' + trial_id)
    _need(not raw.exists(), 'A prepared trial never reuses a recording.', 'output_exists')
    reliability.check_path_budget([raw / ('engine.' + runtime.VERSION) / 'environment' / tail
        for tail in ('tmp', 'localappdata/Godot/app_userdata/KiroClops/portable-heart/tmp')])
    manifest = runtime.capture_project(rendered, raw, scenario, cancel=cancel)
    check(); runtime._cancel(cancel)
    verify_prepared(prepared, project, job_id=job_id, role=role, expected_full_sources=expected_full_sources)
    _need(manifest == read_json(raw / 'manifest.json') and manifest.get('scenario') == scenario
          and manifest.get('provenance', {}).get('project') == str(rendered)
          and manifest['provenance'].get('full_source_hashes_before') ==
              manifest['provenance'].get('full_source_hashes_after') == receipt['native_source_hashes'],
          'Fresh native trial manifest differs from its prepared source.')
    use = {'schema_version': VERSION, 'kind': 'PreparedSuiteTargetUse', 'prepared': deepcopy(prepared),
        'job_id': job_id, 'role': role, 'trial_id': trial_id, 'raw_run': str(raw),
        'scenario': deepcopy(scenario), 'scenario_sha256': digest(scenario),
        'manifest_sha256': file_sha256(raw / 'manifest.json'),
        'runtime_receipt_sha256': manifest['provenance']['runtime_receipt_sha256'],
        'selected_source_sha256': receipt['source_sha256'],
        'native_source_sha256': receipt['native_source_sha256'],
        'raw_frames_sha256': manifest['raw_frames_sha256'], 'fresh_process': True, 'fresh_userdata': True}
    write_json(raw / 'prepared-target-use.json', use)
    verify_case(raw, prepared, project, job_id=job_id, role=role, scenario=scenario,
                expected_full_sources=expected_full_sources)
    return raw


def verify_case(raw, prepared, project, *, job_id, role, scenario=None, expected_full_sources=None):
    """Read-only replay of a fresh trial and its one shared import receipt."""
    receipt = verify_prepared(prepared, project, job_id=job_id, role=role,
                              expected_full_sources=expected_full_sources)
    raw = _ordinary(raw)
    use_path = raw / 'prepared-target-use.json'
    use, manifest = read_json(use_path), read_json(raw / 'manifest.json')
    _need(raw.parent == Path(receipt['root']) and re.fullmatch(r't-[0-9a-f]{12}', raw.name)
          and use.get('kind') == 'PreparedSuiteTargetUse' and use.get('schema_version') == VERSION
          and use.get('prepared') == prepared and use.get('job_id') == job_id and use.get('role') == role
          and use.get('raw_run') == str(raw) and raw.name == 't-' + use['trial_id']
          and use.get('scenario') == manifest.get('scenario')
          and use.get('scenario_sha256') == digest(manifest['scenario'])
          and (scenario is None or manifest['scenario'] == runtime.canonical_scenario(scenario))
          and use.get('selected_source_sha256') == receipt['source_sha256']
          and use.get('native_source_sha256') == receipt['native_source_sha256']
          and use.get('manifest_sha256') == file_sha256(raw / 'manifest.json')
          and use.get('runtime_receipt_sha256') == manifest['provenance']['runtime_receipt_sha256']
          and use.get('raw_frames_sha256') == manifest['raw_frames_sha256']
          and use.get('fresh_process') is True and use.get('fresh_userdata') is True,
          'Prepared trial source/recipe/receipt identity differs.')
    provenance = manifest.get('provenance', {})
    config = read_json(raw / 'capture_config.json')
    expected_config = dict(manifest['scenario'], output_dir=str(raw), game='heart', normal_level=False,
                           protocol=runtime.VERSION, viewport=[320, 180], fps=60)
    expected_variant = 'before' if role in ('original', 'before') else 'after'
    _need(manifest.get('status') == 'ok' and manifest.get('game') == 'heart'
          and manifest.get('variant') == manifest['scenario'].get('variant') == expected_variant
          and manifest.get('fps') == 60 and manifest.get('viewport') == [320, 180]
          and manifest.get('frame_count') == 240 and manifest.get('warmup_ticks') == 60
          and manifest.get('normal_level') is False and manifest.get('settled') is True
          and provenance.get('project') == receipt['project']
          and provenance.get('full_source_hashes_before') == provenance.get('full_source_hashes_after') == receipt['native_source_hashes']
          and provenance.get('source_digest_before') == provenance.get('source_digest_after') == receipt['native_source_sha256']
          and provenance.get('protocol') == runtime.protocol_record()
          and provenance.get('coordinates_from_engine') is False
          and provenance.get('capture_config_sha256') == file_sha256(raw / 'capture_config.json')
          and provenance.get('recording_sha256') == file_sha256(raw / 'recording.json')
          and config == expected_config,
          'Prepared trial configuration, clock, role or native producer identity differs.')
    proof_path = _ordinary(manifest['provenance']['runtime_receipt'])
    _need(proof_path == raw / ('engine.' + runtime.VERSION) / 'bridge.json'
          and file_sha256(proof_path) == use['runtime_receipt_sha256'],
          'Prepared trial runtime proof escaped or changed.')
    proof = runtime.verify_runtime_receipt(proof_path, project=receipt['project'],
                                         expected_source_hashes=receipt['native_source_hashes'])
    _need(proof.get('status') == 'pass', 'Fresh prepared trial process proof failed replay.')
    actual = read_json(raw / 'recording.json')
    try:
        runtime.validate_recording(actual, manifest['scenario'], False)
    except runtime.PortableRuntimeError as error:
        raise VisualError('prepared_target_changed', 'Prepared trial frame/input proof differs.') from error
    _need(actual['frames'] == manifest['frames'] and runtime._frames(raw) == manifest['raw_frame_hashes']
          and digest(manifest['raw_frame_hashes']) == use['raw_frames_sha256'],
          'Prepared trial PNGs or native frame/action receipts changed.')
    return use


def attach_record(record, prepared, project, *, job_id, role, expected_full_sources=None):
    """Add a separately bound use receipt without altering raw/analysis payloads."""
    use = verify_case(record['raw_run'], prepared, project, job_id=job_id, role=role,
                      expected_full_sources=expected_full_sources)
    value = deepcopy(record)
    value['prepared_target'] = {'prepared': deepcopy(prepared), 'job_id': job_id, 'role': role,
        'raw_run': use['raw_run'], 'use_sha256': file_sha256(Path(use['raw_run']) / 'prepared-target-use.json')}
    return value


def verify_record(record, *, expected_job_id=None, expected_full_sources=None):
    """Replay the producer binding when reviewing fresh or frozen cloned records."""
    binding = record.get('prepared_target')
    _need(isinstance(binding, dict), 'Prepared target record has no producer binding.')
    prepared = binding['prepared']
    receipt = read_json(_ordinary(prepared['receipt_path']))
    job_id = expected_job_id or binding['job_id']
    _need(binding.get('job_id') == job_id and binding.get('role') == prepared.get('role')
          and binding.get('raw_run') == record.get('raw_run')
          and binding.get('use_sha256') == file_sha256(Path(record['raw_run']) / 'prepared-target-use.json')
          and record.get('source_sha256') == prepared.get('source_sha256'),
          'Prepared target record belongs to another job/source/trial.')
    use = verify_case(record['raw_run'], prepared, receipt['selected_source'], job_id=job_id,
        role=binding['role'], expected_full_sources=expected_full_sources)
    return {'status': 'pass', 'job_id': job_id, 'role': binding['role'],
            'source_sha256': prepared['source_sha256'], 'raw_run': use['raw_run'],
            'use_sha256': binding['use_sha256'], 'import_reused': True,
            'native_trial_fresh_at_creation': True, 'cached_scope': 'imported_project_only'}
