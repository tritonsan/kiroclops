"""One immutable raw web reference corpus shared by both experimental arms.

Private capture receipts stay outside pixel packages. No analysis, fit or code
solution is generated here. Validation clips are never included in calibration
exports. Manifest resource paths and VideoPackage paths are relative, so the
corpus may move as a unit. Original private capture receipts retain historical
host paths as provenance; they never enter model/analysis/report exports.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from kiroclops.visual.package import load_package, copy_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json, file_sha256, safe_path
from kiroclops.web_local.specs import fixture_project, scenarios
from kiroclops.web_local.capture import adapter_hashes
from .backend import _lock
from .session import source_hashes, _ordinary

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1
DOMAINS = ('panel', 'svg')
SPLITS = ('calibration', 'validation')
MIN_COMPARISON_COVERAGE = .95
MAX_COMPARISON_GAP_S = .12


def _recipes(domain, split, policy=None):
    if policy is None:
        return scenarios(domain, split)
    from kiroclops.web_local.specs import capture_scenarios
    return capture_scenarios(domain, split, policy)


def _domain(value):
    if value == 'web_panel':
        value = 'panel'
    if value not in DOMAINS:
        raise VisualError('invalid_reference_pack', 'Expected panel or svg reference domain')
    return value


def _relative_record(domain, split, scenario):
    identifier = scenario.get('id')
    if not isinstance(identifier, str) or not identifier or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in identifier):
        raise VisualError('invalid_reference_pack', 'Scenario identifier must be a plain filename component')
    return Path('calibration' if split == 'calibration' else 'heldout') / domain / identifier


def _tree(directory):
    # The same source helper rejects symbolic links/reparse points. Capture
    # directories contain no ignored control folders; explicitly reject them.
    directory = _ordinary(directory)
    if any(p.name in ('.git', '.kiro', '.kiroclops-evidence', '.kiroclops-scratch') for p in directory.rglob('*')):
        raise VisualError('invalid_reference_pack', 'Unexpected private/control directory in reference capture')
    return source_hashes(directory)


def _settings(recipe):
    return digest({'scenario': recipe, 'timing_limits': recipe.get('timing_limits'),
                   'measurement': 'none_raw_reference_only', 'variant': 'reference'})


def _capture(project, output, scenario, check, progress, cancel):
    from .evaluation import _capture_web
    return _capture_web(project, output, scenario, check, progress, cancel, 'reference', analyze=False)


def _cancel_check(cancel):
    if cancel and cancel():
        raise VisualError('cancelled', 'Reference preparation was cancelled; partial captures remain evidence')


def reference_time_coverage(manifest, *, expected_scenario=None):
    """Necessary temporal support for the recorded interaction windows.

    This uses only actual timestamps, never invented coordinates or frames. It
    follows the comparator's next-command censoring and 120ms interpolation
    bound. A perfect target on the same interaction timeline cannot cover time
    missing from its reference. A differently timed interruption might shorten
    the comparison, but is not allowed to rescue an unqualified reference recipe.

    The final window ends at the clip boundary here: the actual pixel comparator
    may end earlier, depending on observed motion/lifecycle. Consequently this
    necessary gate never replaces check_reference_comparison_coverage().
    """
    scenario = expected_scenario or manifest.get('metadata', {}).get('scenario', {})
    timeline = manifest.get('inputs', {})
    if timeline.get('provenance') != 'recorder_verified':
        raise VisualError('reference_input_mismatch', 'Temporal coverage needs recorder-verified inputs')
    events = timeline.get('events', [])
    declared = scenario.get('events', [])
    identity = lambda e: (e.get('action'), e.get('selector') if e.get('action') in ('click', 'drag_start') else None)
    if [identity(e) for e in events] != [identity(e) for e in declared]:
        raise VisualError('reference_input_mismatch', 'Reference input receipts differ from the scenario recipe')
    if not events and not declared:
        return {'status': 'not_applicable', 'windows': [], 'reason': 'No declared interaction in this fixture'}
    from .evaluation import _motion_origin
    origin, _ = _motion_origin({'inputs': timeline})
    times = [frame['time_s'] for frame in manifest['frames']]
    observed_policy=scenario.get('analysis_policy') == 'observed_response_v2'
    observed_boundary=observed_policy or (scenario.get('domain')=='panel'
        and scenario.get('frame_driver')=='natural_browser_raf_v1')
    def response_input(index,event):
        if not observed_policy:return event.get('action') in ('click','drag_start','drag_end')
        return event.get('action') in ('click','drag_end') or (event.get('action')=='drag_move'
            and index>0 and events[index-1].get('action')=='drag_start')
    commands = [e for i,e in enumerate(events) if response_input(i,e) and e['time_s'] >= origin - 1e-9]
    windows = []
    for index, command in enumerate(commands):
        start = command['time_s']
        next_input = commands[index + 1]['time_s'] if index + 1 < len(commands) else None
        end = next_input if next_input is not None else times[-1]
        if observed_boundary and next_input is not None:
            # Match the comparator's observed boundary: a new input
            # censors the response at its last genuinely observed prior image.
            prior=[t for t in times if t < next_input-1e-9]
            if not prior:raise VisualError('reference_window_coverage_failure','No observed pre-input boundary')
            end=prior[-1]
        if end <= start:
            raise VisualError('reference_window_coverage_failure', 'Reference input window has no positive recorded duration')
        # _before_input forbids borrowing any frame at/after the terminating
        # command, even for interpolation. Isolated exact samples cover no time.
        allowed = [t for t in times if next_input is None or t < next_input - 1e-9]
        support = []
        for a, b in zip(allowed, allowed[1:]):
            left, right = max(start, a), min(end, b)
            if right > left and b - a <= MAX_COMPARISON_GAP_S:
                support.append([left, right])
        covered = sum(b - a for a, b in support)
        upper_bound = covered / (end - start)
        windows.append({'window_id': f'motion-{index}', 'input_action': command['action'],
            'input_selector': command.get('selector'), 'start_s': start, 'end_s': end,
            'next_input_s': next_input, 'covered_time_s': covered,
            'reference_time_coverage_upper_bound': upper_bound,
            'minimum_coverage': MIN_COMPARISON_COVERAGE,
            'max_interpolation_gap_s': MAX_COMPARISON_GAP_S,
            'supported_intervals_s': support, 'pass': upper_bound >= MIN_COMPARISON_COVERAGE})
    return {'status': 'pass' if windows and all(w['pass'] for w in windows) else 'fail',
        'windows': windows, 'measurement_source': 'recorded_frame_and_input_timestamps_only',
        'pixel_geometry_or_lifecycle_checked': False, 'behavior_transfer_success_claimed': False,
        'scope': 'Necessary support for the complete recorded response windows, not sufficient pixel/comparator validity.'}


def require_reference_time_coverage(manifest, *, expected_scenario=None):
    result = reference_time_coverage(manifest, expected_scenario=expected_scenario)
    if result['status'] == 'fail':
        failed = [w for w in result['windows'] if not w['pass']]
        first = failed[0]
        raise VisualError('reference_window_coverage_failure',
            f'Reference {first["window_id"]} [{first["start_s"]:.9f}, {first["end_s"]:.9f}] '
            f'has at most {first["reference_time_coverage_upper_bound"]:.6%} temporal coverage; '
            'the unchanged comparator requires95%. No interpolation beyond120ms or after the next input is permitted.')
    return result


def check_reference_comparison_coverage(record):
    """Replay the current comparator on existing pixel observations only.

    This establishes reference self-consistency, never transfer/model success.
    Unlike the timestamp upper bound, it covers the exact observed-motion
    windows, visible geometry, lifecycle, required relations and dynamic checks.
    It does not capture, extract new pixel measurements, or write evidence.
    """
    from .evaluation import _compare_web
    result = _compare_web(record, record, record, improvement=False)
    pixel = result['pixel_comparison']
    # Identical imprecise timestamps still produce a zero self-match error.
    # Apply the existing event resolution verdict before admitting references.
    events=record.get('events',{})
    unresolved=[{'scope':scope,'index':i,**item}
                for scope,key in (('event','events'),('relationship','relations'))
                for i,item in enumerate(events.get(key,[]))
                if item.get('timing_status')=='unresolved']
    return {'kind': 'ReferenceComparatorSelfConsistency', 'status': 'pass' if result['pass'] and not unresolved else 'fail',
        'unresolved_timing':unresolved,
        'scenario_id': record['scenario']['id'],
        'reference_package_sha256': record['observations']['package_sha256'],
        'reference_observations_sha256': digest(record['observations']),
        'coverage_windows': [{k: row.get(k) for k in ('reference_id', 'window_id', 'segment_index',
            'time_window_s', 'valid_time_ratio', 'endpoint_required', 'gates')} for row in pixel['object_windows']],
        'pixel_comparison': pixel, 'dynamics': result['dynamics'], 'relative_timing': result['relative_timing'],
        'functional_status': record['functional'].get('status'),
        'behavior_transfer_success_claimed': False, 'new_pixel_measurement_executed': False,
        'scope': 'A recording compared with itself is an input-adequacy check, not evidence that a candidate or model transferred behavior.'}


def _binding(directory, relative, recipe, sources, recorder, *, capture_files=None):
    capture = _ordinary(safe_path(directory, relative.as_posix(), must_exist=False))
    if not capture.is_dir():
        raise VisualError('reference_pack_changed', 'Reference capture directory is missing')
    package = load_package(capture / 'package')
    start = read_json(capture / 'capture-start.json')
    receipt = read_json(capture / 'capture-result.json')
    raw = read_json(capture / 'capture-record.json')
    oracle = read_json(capture / 'private/oracle.json')
    metadata = package['metadata']
    if start.get('variant') != 'reference' or metadata.get('variant') != 'reference':
        raise VisualError('foreign_reference_package', 'A target recording cannot be used as a reference')
    if any(value != recipe for value in (start.get('scenario'), raw.get('scenario'), metadata.get('scenario'))):
        raise VisualError('foreign_reference_package', 'Reference pixels have another input recipe')
    if metadata.get('domain') != recipe['domain'] or package.get('image_size') != recipe['viewport']:
        raise VisualError('foreign_reference_package', 'Reference domain or viewport differs')
    if any(value != sources for value in (start.get('source_hashes'), receipt.get('source_hashes'), metadata.get('source_hashes'))):
        raise VisualError('reference_source_changed', 'Capture and package must bind the registered reference application')
    if any(value != recorder for value in (start.get('adapter_hashes'), receipt.get('adapter_hashes'), metadata.get('adapter_hashes'))):
        raise VisualError('reference_recorder_changed', 'Reference capture adapter differs')
    timing = receipt.get('timing', {})
    functional = receipt.get('functional', {})
    if timing.get('status') != 'pass' or timing != raw.get('timing') or timing != metadata.get('timing'):
        raise VisualError('capture_timing_failure', 'Reference timing did not pass consistently')
    if receipt.get('status') != 'ok' or functional.get('status') != 'pass' or functional != oracle.get('functional'):
        raise VisualError('application_integrity_failure', 'Reference functionality must pass its private receipt')
    if package.get('inputs') != raw.get('inputs') or package['inputs'].get('provenance') != 'recorder_verified':
        raise VisualError('foreign_reference_package', 'Recorded input receipts differ from packaged inputs')
    if [frame['time_s'] for frame in package['frames']] != raw.get('timestamps'):
        raise VisualError('capture_timing_failure', 'Package and capture timestamps differ')
    require_reference_time_coverage(package, expected_scenario=recipe)
    return {'scenario': deepcopy(recipe), 'scenario_sha256': digest(recipe), 'settings_sha256': _settings(recipe),
        'capture_dir': relative.as_posix(), 'package_dir': (relative / 'package').as_posix(),
        'package_sha256': package['package_sha256'], 'package_manifest_sha256': file_sha256(capture / 'package/manifest.json'),
        'timing': timing, 'functional': functional, 'source_sha256': digest(sources),
        # A loader already checked the complete capture tree immediately before
        # this binding. Reuse that content digest instead of hashing every file
        # a second time; preparation still computes it here for the first time.
        'capture_files': capture_files if capture_files is not None else _tree(capture)}


def prepare_reference_pack(output, *, domains=DOMAINS, progress=print, cancel=None, capture_policy=None):
    """Capture each registered reference recipe exactly once, before model runs.

    A failed attempt is never retried within this pack. Its directory and failure
    receipt remain, and there is no usable final manifest. Use a new explicit
    preparation attempt after diagnosing an engineering failure.
    """
    output = _ordinary(output)
    if output.exists():
        raise VisualError('output_exists', 'Never overwrite a reference corpus or a failed preparation')
    domains = tuple(_domain(d) for d in domains)
    if not domains or len(set(domains)) != len(domains):
        raise VisualError('invalid_reference_pack', 'Reference domains must be unique and nonempty')
    recorder = adapter_hashes()
    sources = {domain: source_hashes(fixture_project(domain, 'reference')) for domain in domains}
    recipes = {domain: {split: _recipes(domain, split, capture_policy) for split in SPLITS} for domain in domains}
    for domain in domains:
        for split in SPLITS:
            names = [_relative_record(domain, split, r) for r in recipes[domain][split]]
            if not names or len(set(names)) != len(names):
                raise VisualError('invalid_reference_pack', 'Each split needs unique input scenarios')
    output.mkdir(parents=True)
    registration = {'schema_version': SCHEMA_VERSION, 'kind': 'WebReferencePreparation',
        'domains': list(domains), 'source_hashes': sources, 'adapter_hashes': recorder,
        **({'capture_policy':capture_policy} if capture_policy is not None else {}),
        'scenarios': recipes, 'scenario_matrix_sha256': digest(recipes)}
    write_json(output / 'registration.json', registration)
    records = []
    try:
        # Same lock as Session jobs and native wall captures; no desktop window.
        with _lock(ROOT / 'artifacts/local-engine/capture.lock', timeout=180):
            for domain in domains:
                for split in SPLITS:
                    for recipe in recipes[domain][split]:
                        _cancel_check(cancel)
                        if source_hashes(fixture_project(domain, 'reference')) != sources[domain] or adapter_hashes() != recorder:
                            raise VisualError('reference_source_changed', 'Source changed during reference preparation')
                        relative = _relative_record(domain, split, recipe)
                        progress(f'Reference {domain}/{split}/{recipe["id"]}')
                        _capture(fixture_project(domain, 'reference'), output / relative, recipe,
                                 lambda: _cancel_check(cancel), progress, cancel)
                        records.append({'domain': domain, 'split': split,
                            **_binding(output, relative, recipe, sources[domain], recorder)})
            if adapter_hashes() != recorder or any(source_hashes(fixture_project(d, 'reference')) != sources[d] for d in domains):
                raise VisualError('reference_source_changed', 'Reference source changed before sealing')
        manifest = {**registration, 'kind': 'FrozenWebReferencePack', 'status': 'sealed',
            'registration_sha256': file_sha256(output / 'registration.json'), 'records': records,
            'calibration_export_policy': 'Only calibration pixel packages; never heldout or private receipts.'}
        manifest['pack_sha256'] = digest(manifest)
        write_json(output / 'manifest.json', manifest)
        return {'status': 'sealed', 'manifest': str(output / 'manifest.json'),
                'manifest_sha256': file_sha256(output / 'manifest.json'), 'records': len(records)}
    except BaseException as exc:
        write_json(output / 'failure.json', {'status': 'failed', 'completed_records': len(records),
            'code': getattr(exc, 'code', 'reference_preparation_failed'), 'message': str(exc),
            'usable_reference_manifest': False})
        raise


def _load(manifest_path):
    path = _ordinary(manifest_path)
    if path.is_dir():
        path /= 'manifest.json'
    value = read_json(path)
    expected = deepcopy(value); supplied = expected.pop('pack_sha256', None)
    if value.get('schema_version') != SCHEMA_VERSION or value.get('kind') != 'FrozenWebReferencePack' or value.get('status') != 'sealed' or supplied != digest(expected):
        raise VisualError('invalid_reference_pack', 'Expected a sealed, unchanged reference manifest')
    if value.get('registration_sha256') != file_sha256(path.parent / 'registration.json'):
        raise VisualError('reference_pack_changed', 'Reference preparation registration changed')
    registration = read_json(path.parent / 'registration.json')
    if any(value.get(k) != registration.get(k) for k in ('domains', 'source_hashes', 'adapter_hashes', 'scenarios', 'scenario_matrix_sha256','capture_policy')):
        raise VisualError('reference_pack_changed', 'Final reference matrix differs from its preparation')
    if value.get('scenario_matrix_sha256') != digest(value.get('scenarios')) or value.get('adapter_hashes') != adapter_hashes():
        raise VisualError('reference_recorder_changed', 'Reference settings or current recorder changed')
    return path, value


def load_reference_split(manifest_path, domain, split, *, expected_source_hashes=None, expected_scenarios=None):
    """Verify and return raw records; never recapture or analyze a reference."""
    domain = _domain(domain)
    if split not in SPLITS:
        raise VisualError('invalid_reference_pack', 'Explicit calibration or validation split required')
    path, value = _load(manifest_path)
    sources = expected_source_hashes if expected_source_hashes is not None else source_hashes(fixture_project(domain, 'reference'))
    recipes = expected_scenarios if expected_scenarios is not None else _recipes(domain, split,value.get('capture_policy'))
    if recipes != _recipes(domain,split,value.get('capture_policy')):
        raise VisualError('reference_pack_changed','Requested scenario policy differs from the registered reference policy')
    if value.get('source_hashes', {}).get(domain) != sources or value.get('scenarios', {}).get(domain, {}).get(split) != recipes:
        raise VisualError('reference_pack_changed', 'Reference source or scenario matrix differs from expected current configuration')
    selected = [r for r in value['records'] if r.get('domain') == domain and r.get('split') == split]
    if len(selected) != len(recipes) or [r.get('scenario') for r in selected] != recipes:
        raise VisualError('foreign_reference_package', 'Missing, reordered, duplicated or foreign reference record')
    result = []
    for item, recipe in zip(selected, recipes):
        relative = _relative_record(domain, split, recipe)
        if item.get('capture_dir') != relative.as_posix() or item.get('package_dir') != (relative / 'package').as_posix():
            raise VisualError('foreign_reference_package', 'Reference record points outside its fixed split/domain/scenario')
        capture = _ordinary(safe_path(path.parent, relative.as_posix(), must_exist=False))
        capture_files = _tree(capture)
        if capture_files != item.get('capture_files'):
            raise VisualError('reference_pack_changed', 'A bound raw/private/package evidence file changed')
        actual = _binding(path.parent, relative, recipe, sources, value['adapter_hashes'],
                          capture_files=capture_files)
        if actual != {k: v for k, v in item.items() if k not in ('domain', 'split')}:
            raise VisualError('foreign_reference_package', 'Reference package or private receipt does not reproduce its binding')
        result.append({'scenario': deepcopy(recipe), 'package': str(capture / 'package'),
            'timing': deepcopy(item['timing']), 'functional': deepcopy(item['functional']),
            'source_sha256': item['source_sha256'], 'reference_pack_sha256': file_sha256(path),
            'reference_package_sha256': item['package_sha256']})
    return result


def verify_reference_pack(manifest_path, *, include_splits=False):
    """Full two-domain/two-split gate for cohort registration and replay."""
    path, value = _load(manifest_path)
    if value.get('domains') != list(DOMAINS) or set(value.get('scenarios', {})) != set(DOMAINS):
        raise VisualError('invalid_reference_pack', 'Cohort requires exactly panel and svg references')
    verified_splits = {}
    for domain in DOMAINS:
        if set(value['scenarios'][domain]) != set(SPLITS):
            raise VisualError('invalid_reference_pack', 'Both calibration and heldout splits are required')
        for split in SPLITS:
            verified_splits[domain, split] = load_reference_split(path, domain, split,
                expected_source_hashes=source_hashes(fixture_project(domain, 'reference')),
                expected_scenarios=_recipes(domain, split,value.get('capture_policy')))
    if len(value['records']) != sum(len(value['scenarios'][d][s]) for d in DOMAINS for s in SPLITS):
        raise VisualError('foreign_reference_package', 'Unexpected extra reference record')
    result = {**value, 'manifest_sha256': file_sha256(path), 'verified_records': len(value['records'])}
    if include_splits:
        # Internal one-invocation handoff to the 32-record comparator gate.
        # Do not persist this expanded structure in the cohort registration.
        result['verified_splits'] = verified_splits
    return result


def export_calibration(manifest_path, domain, output):
    """Export pixels and recorded inputs only; there is no heldout export API."""
    output = _ordinary(output)
    if output.exists():
        raise VisualError('output_exists', 'Calibration export destination must be fresh')
    records = load_reference_split(manifest_path, domain, 'calibration')
    output.mkdir(parents=True)
    entries = []
    for index, record in enumerate(records):
        relative = Path(f'{index:02d}-{record["scenario"]["id"]}')
        copied = copy_package(record['package'], output / relative)
        entries.append({'scenario': record['scenario'], 'package': relative.as_posix(), 'package_sha256': copied['package_sha256']})
    result = {'kind': 'CalibrationReferencePixels', 'domain': _domain(domain), 'split': 'calibration',
        'reference_pack_sha256': records[0]['reference_pack_sha256'], 'records': entries}
    write_json(output / 'index.json', result)
    return result
