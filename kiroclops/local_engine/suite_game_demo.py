"""Original Heart level evidence, separate from motion fitting and test rooms.

The caller owns the common capture lease. The real selected controller runs
the existing fixed normal-map route; no position/velocity enters analysis.
"""
from pathlib import Path

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.portable_v2 import runtime
from .session import copy_project, source_hashes
from .reliability import check_path_budget, planned_tmp_paths
from .source_inventory import full_byte_inventory


def _scenarios():
    return [runtime.canonical_scenario({
        'id': name, 'events': [dict(tick=t, action=a, pressed=p) for t, a, p in runtime.NORMAL_RECIPES[name]],
        'split': 'smoke', 'repeat': 1, 'spawn_offset': 0, 'variant': 'after'})
        for name in ('normal-combined', 'normal-slope')]


def capture(project, output, check=lambda: None, cancel=None):
    from .suite_game import _short_runtime
    from .game import _Cancellation
    from kiroclops.portable_v2.reporting import encode_video
    cancel = _Cancellation(cancel, check)
    project, output = Path(project).resolve(), Path(output).resolve()
    if output.exists():
        raise VisualError('output_exists', 'Keep earlier normal-level evidence unchanged.')
    full = source_hashes(project)
    native = _short_runtime() / 'normal-level'
    budget = check_path_budget(planned_tmp_paths(native))
    output.mkdir(parents=True)
    copy_project(project, native / 'project')
    projected = runtime.source_hashes(native / 'project')
    check()
    imported = runtime.import_project(native / 'project', native / 'import', cancel=cancel)
    scenarios = []
    for scenario in _scenarios():
        check()
        name = scenario['id']
        run = native / name
        manifest = runtime.capture_project(native / 'project', run, scenario, normal_level=True, cancel=cancel)
        check()
        video = encode_video(run, output / (name + '.mp4'))
        scenarios.append({'id': name, 'scenario': scenario, 'raw_run': str(run),
                          'manifest_sha256': file_sha256(run / 'manifest.json'),
                          'gameplay': manifest['gameplay'], 'video': str(video),
                          'video_sha256': file_sha256(video)})
    if source_hashes(project) != full or runtime.source_hashes(native / 'project') != projected:
        raise VisualError('source_changed', 'Selected normal-level controller or assets changed.')
    result = {'kind': 'ReferenceSuiteNormalLevelEvidence', 'schema_version': 1,
              'status': scenarios[0]['gameplay']['status'],
              'mandatory_route': 'normal-combined',
              'slope_diagnostic_status': scenarios[1]['gameplay']['status'],
              'selected_source': str(project), 'source_sha256': digest(full), 'full_source_hashes': full,
              'native_root': str(native), 'native_source_hashes': projected, 'protocol': runtime.protocol_record(),
              'import': imported, 'scenarios': scenarios, 'coordinates_exported_to_fitter': False,
              'path_budget': budget,
              'independent_visual_status': 'pending',
              'scope': 'Required original-level run/jump/land/brake route plus a separate slope diagnostic; not full-game equivalence.',
              'file_hashes': full_byte_inventory(native, ignored=(), excluded_suffixes=())}
    write_json(output / 'result.json', result)
    return result


def verify(output, *, source_sha256):
    from kiroclops.portable_v2.reporting import encode_video
    output = Path(output).resolve()
    result = read_json(output / 'result.json')
    root = Path(result['native_root'])
    if (result.get('kind') != 'ReferenceSuiteNormalLevelEvidence' or result.get('source_sha256') != source_sha256
            or digest(source_hashes(result['selected_source'])) != source_sha256
            or result.get('protocol') != runtime.protocol_record()
            or result.get('coordinates_exported_to_fitter') is not False
            or result.get('mandatory_route') != 'normal-combined'
            or result.get('file_hashes') != full_byte_inventory(root, ignored=(), excluded_suffixes=())):
        raise VisualError('incomplete_review', 'Normal-level source, protocol or owned evidence changed.')
    rows = result.get('scenarios', [])
    if [s['id'] for s in rows] != ['normal-combined', 'normal-slope']:
        raise VisualError('incomplete_review', 'Both fixed normal-map routes are required.')
    for row, scenario in zip(rows, _scenarios()):
        run = Path(row['raw_run']).resolve()
        video = Path(row['video']).resolve()
        if (run != root.resolve()/row['id'] or row['scenario'] != scenario
                or video != output / (row['id'] + '.mp4')
                or file_sha256(run / 'manifest.json') != row['manifest_sha256']):
            raise VisualError('incomplete_review', 'Normal-level recording escaped or changed.')
        manifest=read_json(run/'manifest.json')
        recorded=read_json(run/'recording.json')
        runtime.validate_recording(recorded, scenario, True)
        expected_config=dict(scenario, output_dir=str(run), game='heart', normal_level=True,
                             protocol=runtime.VERSION, viewport=[320,180], fps=60)
        proof_path=run/('engine.'+runtime.VERSION)/'bridge.json'
        if (manifest.get('scenario')!=scenario or manifest.get('normal_level') is not True
                or manifest.get('frames')!=recorded['frames']
                or read_json(run/'capture_config.json')!=expected_config
                or manifest.get('raw_frame_hashes')!=runtime._frames(run)
                or manifest.get('provenance',{}).get('runtime_receipt')!=str(proof_path)
                or manifest['provenance'].get('runtime_receipt_sha256')!=file_sha256(proof_path)):
            raise VisualError('incomplete_review','Original-level inputs, frames or process identity changed.')
        actual = runtime.gameplay_checks(recorded, scenario)
        proof = runtime.verify_runtime_receipt(proof_path, project=root / 'project',
                                             expected_source_hashes=result['native_source_hashes'])
        if (actual != row['gameplay'] or proof.get('status') != 'pass'
                or file_sha256(row['video']) != row['video_sha256']):
            raise VisualError('incomplete_review', 'Normal-level checks, video or native process proof changed.')
        # Existing-video branch verifies all 240 frame hashes and the exact
        # encoder sidecar. This is read-only; no encoding/recapture is allowed.
        if not video.is_file() or not video.with_suffix('.source.json').is_file():
            raise VisualError('incomplete_review', 'Normal-level video lacks its frame binding.')
        encode_video(run, video)
    if (result.get('status') != rows[0]['gameplay']['status']
            or result.get('slope_diagnostic_status') != rows[1]['gameplay']['status']):
        raise VisualError('incomplete_review', 'Normal-level primary or diagnostic status changed.')
    return result
