"""Resume an interrupted unsigned SVG pixel proposal into a fresh directory.

The external inventory is made after the interruption. It binds observed old
bytes, not their pre-interruption provenance. Reuse is never fresh extraction
or an attestation. The frozen consumer still extracts every missing recording
before its measurement comparison stage, and all actual inspection is pending.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import realistic_review as review
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.package import frame_paths, load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts import phase3d_v2_web_final_raw as bridge

VERSION = 'interrupted-unsigned-svg-raw-resume-v1'
FROZEN_BRIDGE_SHA256 = '72a3b518e1c494438f8c1e289b7916451faa5f775f7250ee14011b30d91452b5'
PRIOR_RECEIPT_SHA256 = 'd6a1d7f1be5c2b5e5b426908219383d96a7d83ec663ff59b6acb0743e1864a98'
WITNESS_SHA256 = 'f83ca89e53a15edec9f256cdfc6ddd2a01903c7d075369b0ce891b31ab585b92'
CLARIFICATION_SHA256 = '750ddea825bc2f1e588b805a6014fd155fa8c992cf7e8ded2971a8bd64bb3296'
EXPECTED_RECORDS = 112
EXPECTED_REUSED = 87
GLYPHS = set('abcdef')
_active = False


def _need(value, message, code='invalid_resume'):
    if not value:
        raise VisualError(code, message)


def _own_sources():
    path = Path(__file__).resolve()
    return {str(path): file_sha256(path)}


def _runtime():
    return {'opencv': review.cv2.__version__, 'numpy': review.np.__version__,
            'opencv_build_sha256': digest(review.cv2.getBuildInformation())}


def _baseline_sources():
    """Exactly the completed consumer closure, before adding this new helper."""
    _need(not _active and not bridge._active, 'Baseline source replay needs an inactive controller.')
    path = Path(bridge.__file__).resolve()
    _need(file_sha256(path) == FROZEN_BRIDGE_SHA256, 'The frozen alias producer changed.', 'source_changed')
    return review._code_sources() | bridge._extra_sources()


def public_record_identity(row):
    """Public identity only; no produced observations or event coordinates."""
    names = ('id', 'role', 'case_id', 'split_role', 'domain', 'scenario', 'source_sha256',
             'package_sha256', 'selection', 'object_map', 'source_project', 'full_source_hashes',
             'purpose', 'reference', 'expected_source', 'actual_input_times', 'intake_binding', 'raw_run')
    return {name: deepcopy(row.get(name)) for name in names} | {'package': str(row['package'])}


def canonical_snapshot(session_directory):
    """Read the frozen authority metadata for an external operator inventory.

    The normal authority replay reads canonical measurement indexes. This is
    not a claim that _session_context performs no metadata reads. Neither this
    snapshot nor cached extraction uses produced coordinates to mark pixels.
    """
    with bridge._controller():
        context, rows = review._session_context(_ordinary(session_directory))
        normalized = review._records(rows, 'svg', 'final')
    _need(context.get('stage') == 'svg' and len(normalized) == EXPECTED_RECORDS,
          'Only the complete prospective 112-row SVG matrix may resume.')
    return {'canonical_context_sha256': digest(context),
            'canonical_input_files': context['canonical_input_proof']['canonical_input_files'],
            'record_order': [row['id'] for row in normalized],
            'records_identity_sha256': {row['id']: digest(public_record_identity(row)) for row in normalized}}, context, normalized


def _prior_receipt(binding):
    _need(isinstance(binding, dict) and binding.get('sha256') == PRIOR_RECEIPT_SHA256,
          'The independently pinned completed receipt is required.', 'missing_resume_pin')
    path = _ordinary(binding.get('path', ''))
    _need(path.is_file() and file_sha256(path) == PRIOR_RECEIPT_SHA256,
          'The completed receipt differs from its external pin.', 'stale_resume')
    receipt = read_json(path)
    _need(receipt.get('kind') == 'RealisticPixelEvidenceReceipt' and receipt.get('schema_version') == 2,
          'The prior completed pixel receipt has another schema.')
    _need(receipt.get('code_sources') == _baseline_sources(),
          'Current independent extractor/consumer sources differ from the completed pin.', 'source_changed')
    bridge._check_files(receipt['code_sources'])
    _need(receipt.get('runtime') == _runtime(), 'The completed extractor runtime changed.', 'source_changed')
    generated = receipt.get('generated_files', {})
    consumed = {str(path): PRIOR_RECEIPT_SHA256} | receipt['code_sources']
    for name in ('result.json', 'records.json', 'extraction-complete.json', 'consumer-binding.json'):
        target = safe_path(path.parent, name)
        _need(generated.get(name) == file_sha256(target), 'Prior completion file changed: ' + name, 'stale_resume')
        consumed[str(target)] = generated[name]
    consumer = read_json(path.parent / 'consumer-binding.json')
    archive = _ordinary(consumer.get('producer_archive', ''))
    _need(consumer.get('version') == bridge.VERSION and consumer.get('producer_sha256') == FROZEN_BRIDGE_SHA256
          and archive.is_relative_to(path.parent / 'consumer-sources') and file_sha256(archive) == FROZEN_BRIDGE_SHA256
          and receipt.get('consumer_binding_sha256') == digest(consumer),
          'The completed frozen wrapper producer binding changed.', 'source_changed')
    result = read_json(path.parent / 'result.json')
    _need(result.get('phase') == 'final' and result.get('domain') == 'panel'
          and result.get('version') == bridge.VERSION and receipt.get('result_sha256') == digest(result),
          'The pinned receipt must belong to the earlier completed final panel proposal.')
    consumed[str(archive)] = FROZEN_BRIDGE_SHA256
    return receipt, consumed


def _interruption(inventory, partial, authority):
    """Bind the observed reboot and its explicit metadata-read clarification."""
    bindings = inventory['interruption']
    consumed, values = {}, {}
    for name, expected in (('witness', WITNESS_SHA256), ('clarification', CLARIFICATION_SHA256)):
        item = bindings.get(name)
        _need(isinstance(item, dict) and item.get('sha256') == expected,
              'The independently pinned interruption ' + name + ' is required.', 'missing_resume_pin')
        path = _ordinary(item.get('path', ''))
        _need(path.is_file() and file_sha256(path) == expected, 'Observed interruption pin changed.', 'stale_resume')
        consumed[str(path)] = expected
        values[name] = read_json(path)
    witness, clarification = values['witness'], values['clarification']
    expected_points = {str(safe_path(partial, 'raw/' + name + '/points.json')): sha
                       for name, sha in inventory['raw_points_sha256'].items()}
    witnessed = witness.get('partial_points_sha256')
    _need(isinstance(witnessed, dict) and len(witnessed) == EXPECTED_REUSED
          and {str(_ordinary(path)): sha for path, sha in witnessed.items()} == expected_points,
          'The externally inventoried bytes differ from the observed interrupted bytes.', 'stale_resume')
    expected_command = ('scripts/' + Path(bridge.__file__).name + ' prepare --session ' + authority.as_posix()
                        + ' --output ' + partial.as_posix())
    _need(witness.get('kind') == 'ObservedInterruptedIndependentSvgExtraction' and witness.get('schema_version') == 1
          and _ordinary(witness.get('session', '')) == authority and _ordinary(witness.get('partial_directory', '')) == partial
          and witness.get('complete_points_count') == EXPECTED_REUSED and witness.get('expected_record_count') == EXPECTED_RECORDS
          and witness.get('point_pins_observed_after_reboot') is True
          and witness.get('pre_interruption_initial_receipt_exists') is False
          and witness.get('partial_origin') == 'unsigned_unattested_incomplete_pixel_proposals'
          and witness.get('observed_producer_sha256') == FROZEN_BRIDGE_SHA256
          and witness.get('observed_worker_command') == expected_command
          and witness.get('observed_exit_code') == 0x40010004
          and witness.get('windows_last_boot_up_time') == '2026-10-03T10:37:15.500000+03:00'
          and _ordinary(witness.get('previous_completed_source_runtime_receipt', '')) == _ordinary(inventory['prior_completed_receipt']['path'])
          and witness.get('previous_completed_source_runtime_receipt_sha256') == PRIOR_RECEIPT_SHA256
          and witness.get('accepted_scopes') == {} and witness.get('full_behavior_acceptance') is False,
          'The interruption witness belongs to another command/source/reboot or claims acceptance.', 'stale_resume')
    _need(clarification.get('kind') == 'ObservedSvgInterruptionScopeClarification' and clarification.get('schema_version') == 1
          and _ordinary(clarification.get('witness_path', '')) == _ordinary(bindings['witness']['path'])
          and clarification.get('witness_sha256') == WITNESS_SHA256
          and clarification.get('clarified_field') == 'production_measurements_opened_by_original_worker'
          and clarification.get('narrow_claim') == 'The _prepare measurement comparison stage was not reached before the interruption.'
          and clarification.get('canonical_context_metadata_read_before_pixel_extraction') is True
          and clarification.get('production_coordinates_used_for_pixel_extraction') is False
          and clarification.get('pre_interruption_initial_receipt_exists') is False
          and clarification.get('point_pins_observed_after_reboot') is True
          and clarification.get('accepted_scopes') == {} and clarification.get('full_behavior_acceptance') is False,
          'The interruption scope clarification changed.', 'stale_resume')
    return consumed


def preflight(session_directory, inventory_path, inventory_sha256):
    """Verify external post-reboot pins and current complete canonical identity."""
    authority, inventory_path = map(_ordinary, (session_directory, inventory_path))
    _need(review._sha(inventory_sha256), 'Pass the external inventory byte SHA256 explicitly.', 'missing_resume_pin')
    _need(inventory_path.is_file() and file_sha256(inventory_path) == inventory_sha256,
          'The partial inventory differs from its external byte pin.', 'stale_resume')
    inventory = read_json(inventory_path)
    _need(inventory.get('kind') == 'InterruptedUnsignedSvgExtractionInventory'
          and inventory.get('schema_version') == 1 and _ordinary(inventory.get('session_directory', '')) == authority,
          'The external inventory belongs to another Session or schema.')
    _need(inventory.get('pre_interruption_receipt_exists') is False
          and inventory.get('reused_origin_attested') is False
          and inventory.get('production_coordinates_used_for_extraction') is False
          and inventory.get('measurement_stage_reached_before_interruption') is False
          and isinstance(inventory.get('interruption'), dict) and inventory['interruption'],
          'State the unsigned origin, absent initial receipt and interrupted measurement stage explicitly.')
    _, consumed = _prior_receipt(inventory.get('prior_completed_receipt'))
    current, context, rows = canonical_snapshot(authority)
    _need(all(inventory.get(key) == value for key, value in current.items()),
          'Canonical context, public selection/record or source/job input changed.', 'stale_resume')
    bridge._check_files(current['canonical_input_files'])
    partial = _ordinary(inventory.get('partial_directory', ''))
    pins = inventory.get('raw_points_sha256')
    _need(isinstance(pins, dict) and len(pins) == EXPECTED_REUSED
          and set(pins) == set(current['record_order'][:EXPECTED_REUSED]) and all(review._sha(sha) for sha in pins.values()),
          'Bind exactly the observed 87 complete unsigned raw recordings.', 'missing_resume_pin')
    paths = {str(safe_path(partial, 'raw/' + name + '/points.json')) for name in pins}
    _need(partial.is_dir() and {str(path.resolve()) for path in partial.rglob('*') if path.is_file()} == paths,
          'The interrupted tree must contain only the exact pinned complete points files.', 'stale_resume')
    for name, sha in pins.items():
        _need(file_sha256(safe_path(partial, 'raw/' + name + '/points.json')) == sha,
              'Observed old raw bytes changed: ' + name, 'stale_resume')
    consumed.update(_interruption(inventory, partial, authority))
    consumed.update(current['canonical_input_files'])
    consumed[str(inventory_path)] = inventory_sha256
    consumed.update({str(safe_path(partial, 'raw/' + name + '/points.json')): sha for name, sha in pins.items()})
    return {'inventory_path': str(inventory_path), 'inventory_sha256': inventory_sha256,
            'inventory': inventory, 'context': context, 'rows': rows, 'partial': partial, 'consumed_files': consumed}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _xy(value, size):
    return isinstance(value, list) and len(value) == 2 and all(_finite(v) for v in value) \
        and 0 <= value[0] < size[0] and 0 <= value[1] < size[1]


def _color_bounds(box, size):
    return isinstance(box, list) and len(box) == 4 and all(type(v) is int for v in box) \
        and 0 <= box[0] < box[0] + box[2] <= size[0] and 0 <= box[1] < box[1] + box[3] <= size[1]


def _raw_schema(raw, manifest):
    """Validate unsmoothed glyph/card/stroke geometry without target coordinates."""
    _need(isinstance(raw, dict) and set(raw) == {'rows', 'package_sha256', 'production_coordinates_used',
          'production_events_used', 'target_code_or_dom_read', 'status'}
          and raw.get('status') == 'unsigned' and raw.get('production_coordinates_used') is False
          and raw.get('production_events_used') is False and raw.get('target_code_or_dom_read') is False
          and raw.get('package_sha256') == manifest['package_sha256'], 'Cached unsigned raw schema/package differs.')
    rows, size = raw.get('rows'), manifest['image_size']
    _need(isinstance(rows, list) and len(rows) == len(manifest['frames']), 'Cached extraction must retain every frame.')
    for row, frame in zip(rows, manifest['frames']):
        _need(isinstance(row, dict) and set(row) == {'index', 'time_s', 'image_sha256', 'points', 'objects', 'connections'}
              and type(row.get('index')) is int and row['index'] == frame['index']
              and _finite(row.get('time_s')) and row['time_s'] == frame['time_s']
              and row.get('image_sha256') == frame['sha256'], 'Cached frame index, time or image identity changed.')
        points, objects, connections = row['points'], row['objects'], row['connections']
        _need(isinstance(points, dict) and set(points) == GLYPHS and isinstance(objects, dict)
              and set(objects) <= GLYPHS and isinstance(connections, dict), 'Cached glyph/geometry identities are malformed.')
        for point in points.values():
            _need(isinstance(point, dict), 'Cached glyph must be an object.')
            if point.get('status') == 'unsigned':
                box = point.get('color_bounds')
                _need(set(point) == {'x', 'y', 'color_bounds', 'status'} and _xy([point.get('x'), point.get('y')], size)
                      and _color_bounds(box, size) and box[0] <= point['x'] < box[0]+box[2]
                      and box[1] <= point['y'] < box[1]+box[3], 'Cached glyph anchor/bounds are invalid.')
            else:
                _need(point.get('status') in ('not_detected', 'ambiguous') and 'x' not in point and 'y' not in point
                      and (set(point) == {'status', 'boxes'} and isinstance(point['boxes'], list)
                           and all(_color_bounds(box, size) for box in point['boxes'])
                           and (len(point['boxes']) >= 2 if point['status'] == 'ambiguous' else not point['boxes'])
                           or set(point) == {'status', 'reason'} and point['status'] == 'ambiguous'
                           and isinstance(point['reason'], str) and point['reason']), 'Missing glyphs must remain explicit invalid samples.')
        for glyph, obj in objects.items():
            _need(isinstance(obj, dict) and set(obj) == {'bounds', 'anchor'}, 'Cached card schema is invalid.')
            _need(points[glyph].get('status') == 'unsigned' or set(points[glyph]) == {'status', 'reason'},
                  'A missing/ambiguous color component cannot supply a painted card.')
            bounds, anchor = obj.get('bounds'), obj.get('anchor')
            _need(isinstance(bounds, list) and len(bounds) == 4 and all(type(v) is int for v in bounds)
                  and 0 <= bounds[0] < bounds[2] <= size[0] and 0 <= bounds[1] < bounds[3] <= size[1]
                  and _xy(anchor, size) and bounds[0] <= anchor[0] < bounds[2] and bounds[1] <= anchor[1] < bounds[3],
                  'Cached outer card/anchor geometry is invalid.')
            if points[glyph].get('status') == 'unsigned':
                _need(anchor == [points[glyph]['x'], points[glyph]['y']], 'Cached card and glyph anchor disagree.')
        for edge, stroke in connections.items():
            _need(edge in {'a-'+glyph for glyph in GLYPHS-{'a'}} and isinstance(stroke, dict)
                  and 'a' in objects and edge[2:] in objects
                  and set(stroke) == {'start', 'end', 'pixels'} and _xy(stroke.get('start'), size)
                  and _xy(stroke.get('end'), size) and type(stroke.get('pixels')) is int
                  and 4 <= stroke['pixels'] <= size[0]*size[1], 'Cached visible stroke geometry is invalid.')
    return rows


def _reuse(record, output, inventory, plan):
    name = record['id']
    source = safe_path(plan['partial'], 'raw/' + name + '/points.json')
    expected = plan['inventory']['raw_points_sha256'][name]
    _need(file_sha256(source) == expected, 'Old raw byte pin changed before reuse.', 'stale_resume')
    # Official hash/header validation intentionally does not decode/re-extract.
    manifest = load_package(record['package'], verify='hashes')
    _need(record.get('package_sha256') in (None, manifest['package_sha256'])
          and isinstance(manifest.get('metadata', {}).get('source_hashes'), dict)
          and digest(manifest['metadata']['source_hashes']) == record['source_sha256'],
          'Cached public package/source binding differs.', 'stale_resume')
    data = source.read_bytes()
    _need(hashlib.sha256(data).hexdigest() == expected, 'Old raw changed during reuse.', 'stale_resume')
    def invalid(value):
        raise VisualError('invalid_document', 'Nonfinite JSON number: ' + value)
    try:
        raw = json.loads(data.decode('utf-8-sig'), parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VisualError('invalid_document', str(exc)) from exc
    rows = _raw_schema(raw, manifest)
    target = safe_path(output, 'raw/' + name + '/points.json', must_exist=False)
    _need(not target.exists(), 'Resumption must never overwrite points.', 'output_exists')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    _need(file_sha256(target) == expected, 'Copied raw bytes differ from their external pin.', 'stale_resume')
    inventory[str(source)] = expected
    inventory[str(record['package'] / 'manifest.json')] = file_sha256(record['package'] / 'manifest.json')
    inventory.update({str(path): frame['sha256'] for frame, path in zip(manifest['frames'], frame_paths(record['package'], manifest))})
    return {'record': record, 'manifest': manifest, 'raw': {'rows': rows}, 'sheets': []}


@contextmanager
def _controller(plan=None):
    """Only this process-local seam changes; every callback restores on failure."""
    global _active
    _need(not _active and not bridge._active, 'Only one resumable controller may run.', 'controller_active')
    sources, extract = bridge._extra_sources, review.generic._extract
    own, seen = _own_sources(), set()
    _active = True
    try:
        bridge._extra_sources = lambda: sources() | own
        if plan is not None:
            def resumed(record, output, inventory):
                name = record['id']
                _need(name not in seen and len(seen) < len(plan['inventory']['record_order'])
                      and plan['inventory']['record_order'][len(seen)] == name
                      and plan['inventory']['records_identity_sha256'].get(name) == digest(public_record_identity(record)),
                      'Resumed extraction row differs from its complete public identity.', 'stale_resume')
                seen.add(name)
                return _reuse(record, output, inventory, plan) if name in plan['inventory']['raw_points_sha256'] else extract(record, output, inventory)
            review.generic._extract = resumed
        yield seen
        if plan is not None:
            _need(seen == set(plan['inventory']['records_identity_sha256']), 'Resumed extraction omitted a complete matrix row.')
        _need(own == _own_sources(), 'The resume consumer changed during execution.', 'source_changed')
    finally:
        bridge._extra_sources, review.generic._extract = sources, extract
        _active = False


def _protected_roots(plan):
    roots = [plan['partial'], _ordinary(plan['context']['session_directory']), _ordinary(plan['context']['suite_directory']),
             _ordinary(plan['inventory_path']), _ordinary(plan['inventory']['prior_completed_receipt']['path']).parent]
    roots.extend(row['package'] for row in plan['rows'])
    roots.extend(_ordinary(row['source_project']) for row in plan['rows'] if row.get('source_project'))
    return roots


def _safe_output(output, plan):
    _need(not output.exists(), 'Preserve old artifacts; choose a fresh output.', 'output_exists')
    _need(all(not output.is_relative_to(root) and not root.is_relative_to(output) for root in _protected_roots(plan)),
          'Fresh output cannot contain or enter canonical/source/prior evidence.', 'unsafe_path')


def prepare(session_directory, output, *, inventory_path, inventory_sha256):
    authority, output = map(_ordinary, (session_directory, output))
    _need(not output.exists(), 'Preserve old artifacts; choose a fresh output.', 'output_exists')
    plan = preflight(authority, inventory_path, inventory_sha256)
    _safe_output(output, plan)
    with _controller(plan):
        value = bridge.prepare(authority, output)
    archive = output / 'consumer-sources' / Path(__file__).name
    archive.write_bytes(Path(__file__).read_bytes())
    binding = {'kind': 'InterruptedUnsignedSvgResumeConsumerBinding', 'schema_version': 1, 'version': VERSION,
        'base_consumer_version': bridge.VERSION, 'session_directory': str(authority),
        'inventory_path': plan['inventory_path'], 'inventory_sha256': inventory_sha256,
        'prior_completed_receipt': deepcopy(plan['inventory']['prior_completed_receipt']),
        'interruption': deepcopy(plan['inventory']['interruption']),
        'partial_directory': str(plan['partial']), 'canonical_context_sha256': digest(plan['context']),
        'record_count': len(plan['rows']), 'reused_record_count': EXPECTED_REUSED,
        'freshly_extracted_record_count': EXPECTED_RECORDS-EXPECTED_REUSED,
        'reused_raw_points_sha256': deepcopy(plan['inventory']['raw_points_sha256']),
        'extra_sources': _own_sources(), 'producer_archive': str(archive), 'producer_sha256': file_sha256(archive),
        'origin': 'unsigned_post_reboot_observed_bytes_no_pre_interruption_receipt',
        'reused_origin_attested': False, 'reused_points_freshly_recomputed': False,
        'production_coordinates_used_for_extraction': False, 'measurement_stage_reached_before_interruption': False,
        'all_pixel_extraction_finished_before_measurement_read': True,
        'raw_only': True, 'ordinary_reader_compatibility_claimed': False,
        'source_code_acceptance_granted': False, 'native_model_generation_acceptance_granted': False,
        'new_model_generation': False, 'full_behavior_acceptance': False}
    write_json(output / 'resume-consumer-binding.json', binding)
    receipt = read_json(output / 'evidence-receipt.json')
    receipt['resume_consumer_binding_sha256'] = digest(binding)
    receipt['generated_files'] = {path.relative_to(output).as_posix(): file_sha256(path)
        for path in sorted(output.rglob('*')) if path.is_file() and path != output / 'evidence-receipt.json'}
    write_json(output / 'evidence-receipt.json', receipt)
    # Recheck external/canonical pins after completion, without running CV again.
    preflight(authority, inventory_path, inventory_sha256)
    return {'summary': value['summary'], 'resume_binding': binding, 'result': value['result']}


def validate(output, *, session_directory, require_attested=True):
    output, authority = map(_ordinary, (output, session_directory))
    binding, receipt = read_json(output / 'resume-consumer-binding.json'), read_json(output / 'evidence-receipt.json')
    _need(binding.get('kind') == 'InterruptedUnsignedSvgResumeConsumerBinding' and binding.get('schema_version') == 1
          and binding.get('version') == VERSION and binding.get('base_consumer_version') == bridge.VERSION
          and binding.get('extra_sources') == _own_sources() and _ordinary(binding.get('session_directory', '')) == authority
          and receipt.get('resume_consumer_binding_sha256') == digest(binding), 'Resume consumer identity changed.', 'stale_resume')
    for key in ('reused_origin_attested', 'reused_points_freshly_recomputed', 'production_coordinates_used_for_extraction',
                'measurement_stage_reached_before_interruption', 'ordinary_reader_compatibility_claimed',
                'source_code_acceptance_granted', 'native_model_generation_acceptance_granted', 'new_model_generation', 'full_behavior_acceptance'):
        _need(binding.get(key) is False, 'Resume acceptance/origin scope changed.', 'stale_resume')
    _need(binding.get('raw_only') is True and binding.get('all_pixel_extraction_finished_before_measurement_read') is True
          and binding.get('origin') == 'unsigned_post_reboot_observed_bytes_no_pre_interruption_receipt', 'Resume provenance scope changed.')
    archive = _ordinary(binding.get('producer_archive', ''))
    _need(archive.is_relative_to(output / 'consumer-sources') and binding.get('producer_sha256') == next(iter(_own_sources().values()))
          and file_sha256(archive) == binding['producer_sha256'], 'Resume producer archive changed.', 'source_changed')
    plan = preflight(authority, binding['inventory_path'], binding['inventory_sha256'])
    _need(binding.get('canonical_context_sha256') == digest(plan['context'])
          and binding.get('partial_directory') == str(plan['partial'])
          and binding.get('prior_completed_receipt') == plan['inventory']['prior_completed_receipt']
          and binding.get('interruption') == plan['inventory']['interruption']
          and binding.get('reused_raw_points_sha256') == plan['inventory']['raw_points_sha256']
          and binding.get('record_count') == EXPECTED_RECORDS and binding.get('reused_record_count') == EXPECTED_REUSED
          and binding.get('freshly_extracted_record_count') == EXPECTED_RECORDS-EXPECTED_REUSED,
          'Resume extraction coverage or public input binding changed.', 'stale_resume')
    complete = read_json(output / 'extraction-complete.json')
    _need(complete.get('all_pixel_extraction_finished_before_measurement_read') is True
          and set(complete.get('raw_points_sha256', {})) == set(plan['inventory']['records_identity_sha256']),
          'The complete extraction barrier changed.', 'stale_resume')
    for name, sha in complete['raw_points_sha256'].items():
        _need(review._sha(sha) and file_sha256(safe_path(output, 'raw/' + name + '/points.json')) == sha,
              'The complete extraction barrier no longer binds every actual raw output.', 'stale_resume')
    for name, sha in plan['inventory']['raw_points_sha256'].items():
        _need(complete['raw_points_sha256'].get(name) == sha
              and file_sha256(safe_path(output, 'raw/' + name + '/points.json')) == sha,
              'Reused output no longer matches the exact external raw byte pin.', 'stale_resume')
    with _controller():
        result = bridge.validate(output, session_directory=authority, require_attested=require_attested)
    consumed = result['consumed_files'] | plan['consumed_files']
    return result | {'kind': 'IndependentResumedSvgEnvelopeRawVerification', 'version': VERSION,
        'base_consumer_version': bridge.VERSION, 'resume_consumer_version': VERSION,
        'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
        'resume_consumer_binding_sha256': digest(binding), 'inventory_sha256': binding['inventory_sha256'],
        'reused_record_count': EXPECTED_REUSED, 'freshly_extracted_record_count': EXPECTED_RECORDS-EXPECTED_REUSED,
        'reused_origin_attested': False, 'reused_points_freshly_recomputed': False,
        'ordinary_reader_compatibility_claimed': False, 'full_behavior_acceptance': False}


def attest(output, *, session_directory, reviewer, inspected_sheets, accepted, images_inspected):
    validate(output, session_directory=session_directory, require_attested=False)
    with _controller():
        return bridge.attest(output, session_directory=session_directory, reviewer=reviewer,
                             inspected_sheets=inspected_sheets, accepted=accepted, images_inspected=images_inspected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'validate', 'attest'):
        command = commands.add_parser(name)
        command.add_argument('--session', required=True, type=Path)
        command.add_argument('--output', required=True, type=Path)
        if name == 'prepare':
            command.add_argument('--inventory', required=True, type=Path)
            command.add_argument('--inventory-sha256', required=True)
        elif name == 'validate':
            command.add_argument('--proposal-only', action='store_true')
            command.add_argument('--receipt', type=Path)
        else:
            command.add_argument('--inspection', required=True, type=Path)
            command.add_argument('--reviewer', required=True)
            command.add_argument('--accepted', action='store_true')
            command.add_argument('--images-inspected', action='store_true')
    args = parser.parse_args()
    if args.command == 'prepare':
        value = prepare(args.session, args.output, inventory_path=args.inventory, inventory_sha256=args.inventory_sha256)['summary']
    elif args.command == 'attest':
        value = attest(args.output, session_directory=args.session, reviewer=args.reviewer,
                       inspected_sheets=read_json(args.inspection), accepted=args.accepted, images_inspected=args.images_inspected)
    else:
        value = validate(args.output, session_directory=args.session, require_attested=not args.proposal_only)
        if args.receipt:
            path = _ordinary(args.receipt)
            binding = read_json(_ordinary(args.output) / 'resume-consumer-binding.json')
            plan = preflight(args.session, binding['inventory_path'], binding['inventory_sha256'])
            _need(not path.exists() and not path.is_relative_to(_ordinary(args.output))
                  and all(not path.is_relative_to(root) and not root.is_relative_to(path) for root in _protected_roots(plan)),
                  'Use a fresh external verification receipt outside all protected evidence.', 'unsafe_path')
            write_json(path, value)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
