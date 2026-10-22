"""Sealed-game pixel boundary checks with explicit full/native source identities."""
from pathlib import Path

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from .session import _ordinary, source_hashes


def capture(project, output, candidate_id, check=lambda:None, progress=lambda *args:None, cancel=None):
    from . import game,suite_game,reliability
    from kiroclops.portable_v2 import runtime
    project=Path(project);output=Path(output);full=source_hashes(project)
    native=suite_game._short_runtime()/'visual-wall'
    reliability.check_path_budget([native/'import'/('engine.'+runtime.VERSION)/'environment/tmp',
        native/'raw'/'boundary-longest-scenario'/('engine.'+runtime.VERSION)/'environment/tmp'])
    result=game._visual_wall(project,native,candidate_id,check,progress,game._Cancellation(cancel,check))
    if result.get('status')=='invalid_fixture_for_behavior':
        raise VisualError('insufficient_observability','The visual wall fixture cannot expose this selected controller behavior.')
    if source_hashes(project)!=full:raise VisualError('source_changed','Sealed source changed during boundary checks.')
    inventory={p.relative_to(native).as_posix():file_sha256(p) for p in sorted(native.rglob('*')) if p.is_file()}
    receipt={**result,'kind':'ReferenceSuiteBoundaryEvidence','source_sha256':digest(full),
        'full_source_hashes':full,'selected_source':str(project.resolve()),'native_root':str(native),'file_hashes':inventory,
        'numeric_only':True,'independent_review_status':'pending'}
    write_json(output,receipt)
    return receipt


def bindings(path, *, candidate_sha256, candidate_id):
    from kiroclops.visual.package import load_package
    from kiroclops.portable_v2 import runtime
    from . import evidence, visual_boundary_v4
    from .backend import _validate_job_id
    from .source_inventory import full_byte_inventory
    from kiroclops.cloud.results import legacy_result, request_binding
    from kiroclops.cloud.identity import service_sources, engine_identity
    from kiroclops.visual.events import extract_events

    def need(value, message):
        if not value:
            raise VisualError('incomplete_review', message)

    receipt = read_json(path)
    root = _ordinary(receipt['native_root'])

    def owned(value, expected):
        need(isinstance(value, str), 'Boundary evidence path is missing.')
        actual = _ordinary(value)
        target = _ordinary(root / expected)
        need(actual == target and actual.is_relative_to(root),
             'Boundary evidence path is outside its registered owned case.')
        return actual

    need(receipt.get('kind') == 'ReferenceSuiteBoundaryEvidence'
         and receipt.get('candidate_id') == candidate_id
         and receipt.get('source_sha256') == candidate_sha256
         and digest(receipt['full_source_hashes']) == candidate_sha256
         and receipt.get('required') == 8
         and receipt.get('physics_collision_inferred') is False,
         'Visual boundary evidence belongs to another sealed source or scope.')
    need(full_byte_inventory(root, ignored=(), excluded_suffixes=()) == receipt['file_hashes'],
         'Owned boundary pixels or native receipts changed.')
    result_path = owned(receipt['evidence_path'], 'result.json')
    rows_path = owned(receipt['records_path'], 'candidate.json')
    result, rows = read_json(result_path), read_json(rows_path)
    selected = _ordinary(receipt['selected_source'])
    policy = visual_boundary_v4.policy()
    native_sources = receipt['source_hashes']
    need(source_hashes(selected) == receipt['full_source_hashes']
         and runtime.source_hashes(selected) == native_sources
         and result.get('protocol') == policy,
         'Selected full/native sources or boundary policy changed.')
    added = {f'{visual_boundary_v4.OVERLAY}/{name}': file_sha256(visual_boundary_v4.ADAPTER / name)
             for name in ('Boundary.tscn', 'boundary.gd')}
    need(not (set(native_sources) & set(added)), 'Selected source already contains the fixture overlay.')
    rendered_sources = {**native_sources, **added}
    rendered = _ordinary(root / 'project')
    need(result.get('project_sources') == rendered_sources
         and runtime.source_hashes(rendered) == rendered_sources,
         'Rendered source must be the selected native projection plus the exact fixed overlay.')
    need(file_sha256(result_path) == receipt['evidence_sha256']
         and isinstance(rows, list) and len(rows) == 8
         and result.get('candidate_id') == candidate_id
         and result.get('status') == receipt.get('status')
         and result.get('original_sources') == native_sources
         and result.get('physics_collision_inferred') is False,
         'Visual boundary result or native projection differs.')
    cases = visual_boundary_v4.cases()
    registered = read_json(_ordinary(root / 'qualification-start.json'))
    need(registered == {'protocol': policy, 'original_sources': native_sources, 'cases': cases},
         'Boundary qualification did not bind the registered source and cases.')
    expected = {case['id']: case for case in cases}
    summaries = result.get('rows')
    need(isinstance(summaries, list) and len(summaries) == 8
         and [r.get('id') for r in summaries] == [case['id'] for case in cases]
         and [r.get('scenario', {}).get('id') for r in rows] == [case['id'] for case in cases],
         'All eight ordered boundary case identities are required.')
    labels = {'approach': 'visible_approach', 'parallel': 'visible_parallel_motion',
              'rebound': 'visible_departure_after_approach', 'near_miss': 'distant_motion'}
    values, passed, fixture_statuses = set(), [], []
    worker_sources = service_sources()
    engine_sha = engine_identity()['engine_sha256']
    for row, summary in zip(rows, summaries):
        scenario = row['scenario']
        case_id = scenario['id']
        need(scenario == expected[case_id], 'Boundary scenario differs from its registered recipe.')
        raw = owned(row['raw_run'], f'raw/{case_id}')
        package_path = owned(row['package'], f'packages/{case_id}')
        measured = _ordinary(root / 'measurement' / case_id)
        for key, filename in (('observations', 'observations.json'), ('selection', 'selection.json'),
                              ('events', 'events.json'), ('analysis', 'result.json')):
            owned(row[key], f'measurement/{case_id}/{filename}')
        for key, target in (('package', package_path), ('measurement', measured),
                            ('runtime_receipt', raw / 'runtime-receipt.json'),
                            ('fixture_validity_path', raw / 'private-fixture-validity.json')):
            owned(summary[key], target.relative_to(root))
        observed, selection = read_json(row['observations']), read_json(row['selection'])
        package = load_package(package_path, verify='hashes')
        common = _ordinary(measured / 'common-analysis')
        worker = read_json(common / 'analysis.json')
        operation = read_json(common / 'analysis-operation.json')
        job_id = _validate_job_id(operation['job_id'])
        job = _ordinary(common / 'jobs/jobs' / job_id)
        contract = {'require_static_camera': True, 'min_valid_ratio': .95}
        request = request_binding(package['package_sha256'], selection, contract, digest(native_sources))
        worker_state, delivery = read_json(job / 'state.json'), read_json(job / 'delivery-v2.json')
        need(operation.get('status') == 'succeeded'
             and operation.get('request_sha256') == digest(request)
             and worker_state.get('status') == 'succeeded'
             and worker_state.get('job_id') == job_id
             and worker_state.get('request_sha256') == digest(request)
             and worker_state.get('package_sha256') == request['package_sha256']
             and worker_state.get('candidate_sha256') == request['candidate_sha256']
             and read_json(job / 'request.json') == request
             and worker_state.get('result_sha256') == digest(worker)
             and worker_state.get('delivery_sha256') == digest(delivery)
             and legacy_result(delivery, request, logical_job_id=job_id,
                               expected_sources=worker_sources, expected_engine_sha256=engine_sha) == worker,
             'Boundary worker receipt differs.')
        boundary_events = extract_events(observed, selection, {'event_options': {'boundary_tolerance_px': 12}})
        need(read_json(row['events']) == boundary_events
             and worker.get('events') == extract_events(observed, selection, contract),
             'Boundary or common worker events differ from the shared policy.')
        need(row.get('candidate_id') == candidate_id and row.get('source_hashes') == native_sources
             and row.get('measurement_status') == 'ok' and observed.get('status') == 'ok'
             and worker.get('observations') == observed
             and worker.get('candidate_sha256') == digest(native_sources),
             'Visual boundary observation belongs to another native projection.')
        # The AnalysisResult, not the domain classifier, owns the candidate
        # identity. This verifies case/input/frame/source/selection bindings.
        value = evidence._record_observation({**row, 'analysis': str(common / 'analysis.json')},
            scenario, 'boundary', measured=True,
            expected_source={'kind': 'boundary', 'sources': native_sources,
                             'rendered_sources': rendered_sources, 'candidate_id': candidate_id})
        native = read_json(raw / 'runtime-receipt.json')
        manifest, config = read_json(raw / 'manifest.json'), read_json(raw / 'capture_config.json')
        need(native.get('protocol') == manifest.get('provenance', {}).get('protocol') == policy
             and native.get('project') == str(rendered)
             and native.get('process') == str(raw / 'private-process/process.json')
             and native.get('non_input_desktop') is True and native.get('owned_cleanup_verified') is True
             and manifest.get('scenario') == scenario
             and all(config.get(k) == v for k, v in scenario.items())
             and config.get('output_dir') == str(raw) and config.get('normal_level') is False
             and config.get('viewport') == [320, 180] and config.get('fps') == 60,
             'Native boundary capture configuration or policy differs.')
        argv = [str(runtime.ROOT / runtime.ENGINE_RELATIVE), '--path', str(rendered), '--fixed-fps', '60',
                '--resolution', '320x180', '--rendering-method', 'gl_compatibility', '--windowed',
                '--disable-vsync', visual_boundary_v4.SCENE]
        need(native.get('argv') == argv, 'Native boundary command differs.')
        process = raw / 'private-process'
        try:
            runtime._verify_process(read_json(process / 'process.json'), process, argv, rendered, True)
        except runtime.PortableRuntimeError as error:
            raise VisualError('incomplete_review', 'Native boundary private process proof differs.') from error
        log = '\n'.join((process / name).read_text(encoding='utf-8', errors='replace')
                        for name in ('stdout.log', 'stderr.log'))
        need(runtime.ENGINE_VERSION in log and not runtime._SCRIPT_ERROR.search(log),
             'Native boundary engine log did not complete cleanly.')
        recording = read_json(raw / 'recording.json')
        try:
            runtime.validate_recording(recording, {k: v for k, v in scenario.items()
                                                  if k not in ('kind', 'wall_side')}, False)
        except runtime.PortableRuntimeError as error:
            raise VisualError('incomplete_review', 'Native boundary frame or input proof differs.') from error
        fixture = visual_boundary_v4.fixture_validity(raw, scenario)
        need(native.get('fixture_validity_sha256') == file_sha256(raw / 'private-fixture-validity.json'),
             'Native fixture validity receipt changed.')
        actual = visual_boundary_v4.classify(observed, boundary_events)
        need(read_json(row['analysis']) == actual, 'Stored boundary classifier differs from its observations.')
        case_pass = fixture['status'] == 'pass' and actual['status'] == 'observed' and actual['classification'] == labels[scenario['kind']]
        need(summary.get('expected') == labels[scenario['kind']]
             and summary.get('actual') == actual['classification']
             and summary.get('fixture_status') == fixture['status']
             and summary.get('pass') is case_pass,
             'Stored boundary case classification or pass status differs.')
        values.add(value)
        passed.append(case_pass)
        fixture_statuses.append(fixture['status'])
    status = ('invalid_fixture_for_behavior' if any(s != 'pass' for s in fixture_statuses)
              else 'pass' if all(passed) else 'fail')
    need(len(values) == 8 and result.get('status') == status and receipt.get('passed') == sum(passed),
         'Boundary aggregate status or unique measurement count differs.')
    return values
