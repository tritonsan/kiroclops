"""Tiny receipt/envelope fixtures only; no engine, capture, model, or attestation."""
from copy import deepcopy
import base64
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.cloud.results import envelope, legacy_result, make_measurement, request_binding
from kiroclops.local_engine import suite_svg_final_acceptance as final, suite_revision as revision, suite_web
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_svg_final_acceptance as cli


class FinalClosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.initial = self.root / 'initial'
        self.initial.mkdir()
        self.source = self.root / 'source/index.html'
        self.source.parent.mkdir()
        self.source.write_text('fixture historical source', encoding='utf-8')
        self.historical = self.root / 'historical'
        write_json(self.historical / 'kiro/launch.json', {'cli_version': 'OLD-CLI-fixture', 'kas_version': 'OLD-KAS-fixture'})
        for name in ('kiro/config.json', 'kiro/run/stdout.jsonl', 'kiro/run/outbound.jsonl', 'kiro/tool-audit.jsonl'):
            path = self.historical / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}\n', encoding='utf-8')
        self.audit = {'status': 'pass', 'model': 'historical-fixture-model', 'effort': 'historical-fixture-effort',
            'native_session_id': 'historical-session-fixture', 'scope': 'Mocked historical audit frontier; no actual run',
            'evidence_hashes': {str(p.relative_to(self.historical)).replace('\\', '/'): file_sha256(p)
                for p in self.historical.rglob('*') if p.is_file()}}
        self.native = patch.object(final.review, 'audit_native_run', return_value=self.audit)
        self.native.start()
        self.addCleanup(self.native.stop)
        self.old_source_review = self.root / 'historical-source-review.json'
        write_json(self.old_source_review, {'kind': 'IndependentSourceReview', 'status': 'pass',
            'historical_only': True, 'full_behavior_acceptance': False})
        old_review = patch.object(final.review, 'validate_source_review', return_value=read_json(self.old_source_review))
        old_review.start()
        self.addCleanup(old_review.stop)
        identity = {'model': self.audit['model'], 'effort': self.audit['effort'],
            'native_session_id': self.audit['native_session_id'], 'cli_version': 'OLD-CLI-fixture', 'kas_version': 'OLD-KAS-fixture'}
        self.hist = {'kind': 'HistoricalSVGGenerationEvidenceBindings', 'schema_version': 1,
            'historical_session': str(self.historical), 'historical_candidate_id': 'candidate-02',
            'files': deepcopy(self.audit['evidence_hashes']), 'recorded_model_identity': identity, 'new_model_generation': False}
        self.hist.update(source_review=str(self.old_source_review), source_review_sha256=file_sha256(self.old_source_review))
        self.verification = {'kind': 'UnchangedSVGAcceptanceVerification', 'schema_version': 1,
            'initial_acceptance_directory': str(self.initial), 'unchanged_source_sha256': digest('fixture-source'),
            'historical_generation': {'historical_session': str(self.historical), 'historical_candidate_id': 'candidate-02',
                                    'source_sha256': digest('fixture-source')},
            'input_files': {str(self.source): file_sha256(self.source)},
            'source_projects': {'unchanged': str(self.source.parent)}, 'visual': {'raw_review_directory': str(self.root / 'raw')},
            'numeric': {'status': 'pass'}, 'records': [], 'record_count': 112}
        proof = final._historical_proof(self.hist, self.verification)
        self.request = {'kind': 'UnsignedUnchangedSVGSourceInspectionRequest', 'schema_version': 1,
            'status': 'pending_independent_inspection', 'created_at': time.time() - 2,
            'verification': deepcopy(self.verification), 'verification_sha256': digest(self.verification),
            'historical_evidence': deepcopy(self.hist), 'historical_evidence_sha256': digest(self.hist),
            'historical_evidence_files': proof['files'], 'historical_native_validation': proof,
            'required_checks': sorted(final.SOURCE_CHECKS), 'source_inspection_inferred': False,
            'new_model_generation': False, 'full_behavior_acceptance': False}
        self.request_path, self.receipt_path = self.root / 'request.json', self.root / 'source-receipt.json'
        write_json(self.request_path, self.request)
        self.receipt = {'kind': 'IndependentUnchangedSVGSourceReview', 'schema_version': 1, 'status': 'pass',
            'method': 'independent_source_and_historical_native_inspection', 'reviewer': 'Fixture only: not a real inspector',
            'reviewer_role': 'independent_source_reviewer', 'authored_source': False,
            'inspection_performed': True, 'accepted': True, 'inspected_at': time.time() - 1,
            'request_sha256': digest(self.request), 'verification_sha256': digest(self.verification),
            'unchanged_source_sha256': self.verification['unchanged_source_sha256'],
            'initial_acceptance_directory': str(self.initial), 'historical_evidence_sha256': digest(self.hist),
            'new_model_generation': False, 'historical_generation_only': True,
            'checks': [{'id': name, 'verdict': 'pass', 'evidence': 'Synthetic unit-fixture evidence only.'} for name in final.SOURCE_CHECKS],
            'inspected_files': self.verification['input_files'] | proof['files'],
            'historical_generation_review': {'status': 'pass', 'generation_recorded': True, 'model_identity': identity,
                'validation_scope': 'Synthetic unit fixture only', 'evidence': 'Mocked historical audit, never qualification',
                'new_model_generation': False}}
        write_json(self.receipt_path, self.receipt)

    def source_review(self):
        return final._source_receipt(self.receipt_path, self.request_path, self.verification)

    def test_named_source_fixture_passes_gate_without_writing_attestation(self):
        before = file_sha256(self.receipt_path)
        self.assertEqual(self.receipt, self.source_review())
        self.assertEqual(before, file_sha256(self.receipt_path))
        self.assertFalse((self.initial / 'acceptance.json').exists())

    def test_missing_receipt_does_not_infer_source_inspection(self):
        self.receipt_path.unlink()
        with self.assertRaises(OSError):
            self.source_review()

    def test_foreign_initial_source_or_review_request_is_rejected(self):
        for name, value in (('initial_acceptance_directory', str(self.root / 'foreign')),
                            ('unchanged_source_sha256', digest('foreign')),
                            ('request_sha256', digest('foreign'))):
            with self.subTest(field=name):
                changed = {**self.receipt, name: value}
                write_json(self.receipt_path, changed)
                with self.assertRaises(VisualError):
                    self.source_review()

    def test_named_reviewer_checks_and_real_chronology_are_mandatory(self):
        for changes in ({'reviewer': ''}, {'reviewer_role': 'operator'}, {'authored_source': True},
                        {'inspection_performed': False}, {'checks': []}, {'inspected_at': 0},
                        {'new_model_generation': True}, {'historical_generation_only': False}):
            with self.subTest(changes=changes):
                write_json(self.receipt_path, {**self.receipt, **changes})
                with self.assertRaises(VisualError):
                    self.source_review()

    def test_current_source_byte_tamper_is_rejected(self):
        self.source.write_text('tampered source', encoding='utf-8')
        with self.assertRaises(VisualError):
            self.source_review()

    def test_historical_log_tamper_is_rejected(self):
        (self.historical / 'kiro/run/stdout.jsonl').write_text('tampered\n', encoding='utf-8')
        with self.assertRaises(VisualError):
            self.source_review()

    def test_source_request_verification_cannot_be_retconned(self):
        changed = deepcopy(self.request)
        changed['verification']['record_count'] = 1
        changed['verification_sha256'] = digest(changed['verification'])
        write_json(self.request_path, changed)
        with self.assertRaises(VisualError):
            self.source_review()

    def test_historical_identity_and_complete_actual_native_inventory_required(self):
        for changes in ({'recorded_model_identity': {'model': 'new-Opus-claim'}}, {'files': {}}, {'new_model_generation': True}):
            with self.subTest(changes=changes):
                with self.assertRaises(VisualError):
                    final._historical_proof({**self.hist, **changes}, self.verification)
        with patch.object(final.review, 'audit_native_run', side_effect=VisualError('invalid_native_evidence', 'Unsupported OLD tuple')):
            with self.assertRaises(VisualError) as caught:
                final._historical_proof(self.hist, self.verification)
            self.assertEqual('invalid_native_evidence', caught.exception.code)

    def test_arbitrary_equivalent_capture_path_is_not_an_allowed_origin(self):
        cases = [{'case_id': str(i)} for i in range(28)]
        allowed, rows = {}, []
        for role in final.ROLES:
            for case in cases:
                cid = case['case_id']
                capture = self.root / 'trusted' / role / cid
                worker = capture / 'measurement'
                if role == 'unchanged':
                    capture = self.initial / 'records/unchanged' / cid
                    worker = capture / 'measurement'
                allowed[role, cid] = {'capture': {capture}, 'worker': {worker}}
                rows.append({'role': role, 'case_id': cid, 'capture_directory': str(capture), 'worker_directory': str(worker)})
        value = {'kind': 'UnchangedSVGCaptureOrigins', 'schema_version': 1,
                 'initial_acceptance_directory': str(self.initial), 'records': rows}
        self.assertEqual(112, len(final._bindings(value, allowed, self.initial, cases)))
        for changes in ({'capture_directory': str(self.root / 'equivalent')},
                        {'worker_directory': str(self.root / 'foreign-worker')}, {'case_id': 'foreign'}):
            with self.subTest(changes=changes):
                altered = deepcopy(value)
                altered['records'][0].update(changes)
                with self.assertRaises(VisualError):
                    final._bindings(altered, allowed, self.initial, cases)

    def test_no_source_receipt_means_no_acceptance_output(self):
        self.receipt_path.unlink()
        output = self.root / 'closed'
        with patch.object(final, '_verification', return_value=deepcopy(self.verification)):
            with self.assertRaises(OSError):
                final.close_acceptance(self.initial, self.root / 'visual.json', self.root / 'origins.json',
                    self.request_path, self.receipt_path, output)
        self.assertFalse(output.exists())

    def test_numerical_failure_cannot_be_overridden_by_inspection(self):
        output = self.root / 'closed'
        with patch.object(final, '_verification', side_effect=VisualError('svg_numerical_failure', 'Genuine behavior failed')):
            with self.assertRaises(VisualError) as caught:
                final.close_acceptance(self.initial, self.root / 'visual.json', self.root / 'origins.json',
                    self.request_path, self.receipt_path, output)
        self.assertEqual('svg_numerical_failure', caught.exception.code)
        self.assertFalse(output.exists())

    def test_missing_genuine_functional_execution_never_enters_matrix_closure(self):
        with patch.object(final, '_context', side_effect=AssertionError('must stop before trusting result flags')):
            with self.assertRaises(VisualError) as caught:
                final._verification(self.initial, self.root / 'visual.json', self.root / 'origins.json')
        self.assertEqual('incomplete_svg_functional', caught.exception.code)

    def test_recomputed_genuine_numerical_failure_is_never_accepted(self):
        case = {'case_id': 'fixture', 'role': 'calibration'}
        failed = {'pass': False, 'measurement_pass': True, 'baseline_measurement_pass': True,
            'baseline_coverage_failures': [], 'failure_class': 'curve_error',
            'svg_geometry_preservation': {'measurement_status': 'ok'}}
        prior = {**failed, 'pass': True}
        keep = deepcopy(prior)
        compared = [{**failed, 'case_id': 'fixture', 'role': 'calibration'}]
        preserved = [{**keep, 'case_id': 'fixture', 'role': 'calibration', 'accepted_parent_reference_gates': True,
            'parent_reference_result': prior, 'preservation_required': True}]
        functional = {'kind': 'ReferenceSuiteFunctionalChecks', 'schema_version': 1, 'domain': 'svg',
            'checks_source': 'separate_private_browser_oracle', 'source_sha256': self.verification['unchanged_source_sha256'],
            'status': 'pass', 'checks': [{'id': key, 'pass': True} for key in final.FUNCTIONAL_CHECKS], 'page_errors': []}
        for name, value in (('comparisons.json', compared), ('preservation.json', preserved), ('functional.json', functional)):
            write_json(self.initial / name, value)
        result = {'comparisons': compared, 'parent_preservation': preserved, 'functional': functional, 'numerical_pass': False}
        with patch.object(final.media_suite, '_compare', side_effect=[failed, keep]), \
             patch.object(final.media_suite, '_parent_acceptance', return_value=prior):
            with self.assertRaises(VisualError) as caught:
                final._numerics(self.initial, {'unchanged_source_sha256': self.verification['unchanged_source_sha256']},
                    result, [case], {(role, 'fixture'): {} for role in final.ROLES})
        self.assertEqual('svg_numerical_failure', caught.exception.code)

    def test_cli_error_never_prints_an_accepted_claim(self):
        buffer = io.StringIO()
        with patch.object(cli.closure, 'validate_acceptance', side_effect=VisualError('svg_numerical_failure', 'failed')), \
             patch('sys.stdout', buffer):
            self.assertEqual(1, cli.main(['inspect', '--output', str(self.root / 'missing')]))
        value = json.loads(buffer.getvalue())
        self.assertFalse(value['full_behavior_acceptance'])
        self.assertFalse(value['new_model_generation'])


class RealDirectWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        image = self.root / 'image.png'
        cv2.imencode('.png', np.zeros((2, 2, 3), np.uint8))[1].tofile(image)
        manifest = create_package(self.root / 'package', [image, image], [0., .1])
        contract = suite_web.event_contract('svg')
        selection = {'objects': [{'id': 'a', 'roi': [0, 0, 2, 2]}]}
        observed = {'package_sha256': manifest['package_sha256'], 'status': 'ok', 'objects': [], 'inputs': manifest['inputs']}
        events = {'kind': 'EventSet', 'motion': []}
        self.sources = {'fixture-worker.py': digest('worker')}
        identity = {'opencv_version': 'fixture-only'}
        self.engine = {'identity': identity, 'engine_sha256': digest(identity)}
        request = request_binding(manifest['package_sha256'], selection, contract, digest('fixture-source'))
        delivery = envelope(make_measurement(observed, events), request, logical_job_id='fixtureworker',
            engine=self.engine, sources=self.sources, timings={'execution_wall_s': 0.})
        result = legacy_result(delivery, request, logical_job_id='fixtureworker')
        self.worker = self.root / 'worker'
        write_json(self.worker / 'analysis.json', result)
        write_json(self.worker / 'analysis-operation.json', {'job_id': 'fixtureworker', 'status': 'succeeded', 'request_sha256': digest(request)})
        job = self.worker / 'jobs/jobs/fixtureworker'
        write_json(job / 'request.json', request)
        write_json(job / 'delivery-v2.json', delivery)
        write_json(job / 'state.json', {'status': 'succeeded', 'request_sha256': digest(request),
            'result_sha256': digest(result), 'delivery_sha256': digest(delivery), 'started_at': 1., 'finished_at': 2.})
        copied = self.root / 'copied/analysis.json'
        write_json(copied, result)
        self.record = {'package': str(self.root / 'package'), 'analysis': str(copied), 'selection': selection,
            'scenario': {'case_id': 'tiny-worker-fixture'}, 'observations': observed, 'events': events,
            'source_sha256': request['candidate_sha256'], 'suite_event_contract': contract,
            'independent_review_contract': contract, 'event_contract_sha256': digest(contract)}
        with patch.object(revision, 'service_sources', return_value=self.sources):
            proof = revision._worker_proof({**self.record, 'analysis': str(self.worker / 'analysis.json')}, 'svg', contract)
        self.record['measurement_source_hashes'] = self.sources
        self.record['measurement_provenance'] = {'kind': 'SuitePixelMeasurement', 'schema_version': 1,
            'measurement_source_hashes': self.sources, 'binding': revision._record_binding(self.record, 'svg'),
            'contract_sha256': digest(contract), 'worker': proof}

    def replay(self):
        with patch.object(revision, 'service_sources', return_value=self.sources):
            return final._worker(self.record, self.worker, {}, self.sources, self.engine['engine_sha256'])

    def test_real_saved_envelope_replays_against_copied_inline_stamp(self):
        self.assertEqual(64, len(self.replay()))

    def test_request_state_delivery_candidate_and_operation_tamper_fail(self):
        paths = [self.worker / 'analysis-operation.json', self.worker / 'jobs/jobs/fixtureworker/request.json',
                 self.worker / 'jobs/jobs/fixtureworker/state.json', self.worker / 'jobs/jobs/fixtureworker/delivery-v2.json']
        for path in paths:
            original = read_json(path)
            with self.subTest(path=path.name):
                changed = deepcopy(original)
                if path.name == 'analysis-operation.json': changed['request_sha256'] = digest('foreign')
                elif path.name == 'request.json': changed['candidate_sha256'] = digest('foreign')
                elif path.name == 'state.json': changed['status'] = 'running'
                else: changed['receipt']['candidate_sha256'] = digest('foreign')
                write_json(path, changed)
                with self.assertRaises(VisualError):
                    self.replay()
                write_json(path, original)

    def test_stale_measurement_code_or_foreign_engine_rejected(self):
        with patch.object(revision, 'service_sources', return_value=self.sources):
            for sources, engine in (({'other.py': digest('other')}, self.engine['engine_sha256']),
                                    (self.sources, digest('foreign-engine'))):
                with self.subTest(sources=sources, engine=engine):
                    with self.assertRaises(VisualError):
                        final._worker(self.record, self.worker, {}, sources, engine)


class NativeClockReplayTests(unittest.TestCase):
    """Ten 84x6 counter-only PNGs exercise actual clock/transport replay."""
    def setUp(self):
        from kiroclops.web_local import capture as native
        from kiroclops.web_local.oracle import validate_oracle
        from kiroclops.web_local.specs import TIMING_LIMITS
        from kiroclops.web_local.transport import normalize_compositor_delivery, SUPPORTED_BROWSER_VERSION
        from kiroclops.web_local.visual_clock import bind_frame_times
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.capture = self.root / 'native'
        self.capture.mkdir()
        scenario = {'case_id': 'counter-fixture-only', 'domain': 'svg', 'events': [], 'timing_limits': dict(TIMING_LIMITS),
                    'initial_node_count': 3, 'expected_final_node_count': 3}
        sources = {'index.html': digest('counter-fixture-only')}
        clock_rows, frames = [], []
        for index in range(10):
            ident = index + 1
            bits = [1, 0, 1, 0] + [(ident >> i) & 1 for i in range(15, -1, -1)]
            crc = (ident ^ (ident >> 8) ^ 0xa7) & 255
            bits += [(crc >> i) & 1 for i in range(7, -1, -1)]
            image = np.zeros((6, 84, 3), np.uint8)
            for cell, bit in enumerate(bits):
                for row in (0, 1):
                    image[row*3:(row+1)*3, cell*3:(cell+1)*3] = 250 if bit ^ row else 5
            data = cv2.imencode('.png', image)[1].tobytes()
            stamp = 100.01 + index * .01
            clock_rows.append({'id': ident, 'callback_epoch_s': stamp-.001, 'callback_end_epoch_s': stamp,
                               'after_task_epoch_s': stamp+.001})
            frames.append({'epoch_s': stamp, 'interval_epoch_s': [stamp, stamp], 'timestamp_source': 'cdp_compositor',
                'receive_index': index, 'receive_epoch_s': stamp+.002, 'metadata': {'timestamp': stamp},
                'data': base64.b64encode(data).decode('ascii')})
        objects = {key: {'x': i*20., 'y': 0., 'width': 10., 'height': 10.} for i, key in enumerate('abc')}
        edges = [{'id': left+right, 'source': left, 'target': right, 'geometry_status': 'pass',
            'start': {'x': objects[left]['x']+5., 'y': 5.}, 'end': {'x': objects[right]['x']+5., 'y': 5.}} for left, right in ('ab', 'bc')]
        oracle = {'scenario': scenario, 'domain': 'svg', 'start_epoch_s': 100., 'events': [], 'commands': [],
            'samples': [{'objects': objects, 'edges': edges, 'selected': []} for _ in range(10)],
            'visual_clock': {'policy': 'svg_raf_tag_v1', 'rows': clock_rows}}
        oracle['functional'] = validate_oracle(oracle, 'svg')
        native.preserve_capture_receipts(self.capture, frames, [], [], private=oracle)
        context = {'browser_version': SUPPORTED_BROWSER_VERSION, 'browser_observation_interval_epoch_s': [100., 100.2],
                   'actual_delivery_interval_epoch_s': [100., 100.21]}
        normalized, order = normalize_compositor_delivery(frames, segment_start_s=100., segment_end_s=100.2,
            delivery_end_s=100.21, browser_version=SUPPORTED_BROWSER_VERSION)
        order['raw_received_evidence_sha256'] = file_sha256(self.capture / 'private/received-evidence.json')
        normalized, redundant = native.exclude_redundant_checkpoints(normalized)
        normalized, ambiguous = native.exclude_input_ambiguous_frames(normalized, [])
        normalized = native.merge_frame_receipts(normalized)
        normalized, visual = bind_frame_times(normalized, oracle['visual_clock'], clock_tolerance_s=.001)
        normalized, visual_ambiguous = native.exclude_input_ambiguous_frames(normalized, [])
        visual['input_ambiguous_images'] = visual_ambiguous
        times = [row['epoch_s']-100. for row in normalized]
        metadata = [{'timestamp_source': row['timestamp_source'], 'uncertainty_s': (row['interval_epoch_s'][1]-row['interval_epoch_s'][0])/2,
            'time_interval_s': [value-100. for value in row['interval_epoch_s']]} for row in normalized]
        sample = {'uncertainty_s': .001, 'epoch_in_host_bracket': True}
        clock = {'start': sample, 'end': sample, 'commands': [], 'frames': [{k:v for k,v in row.items() if k!='data'} for row in normalized]}
        timing = native.timing_summary(times, [sample, sample], [], TIMING_LIMITS, metadata)
        timing['frame_clock'] = 'Visible SVG counter bound to real rendering-batch interval; CDP transport times retained privately'
        timing['source_clock_semantics'].update(measurement_clock='svg_visible_raf_batch', physical_display_presentation_claimed=False,
                                               source_geometry_used=False)
        paths = []
        for index, frame in enumerate(normalized):
            path = self.capture / 'raw-frames' / f'{index:06d}.png'
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(base64.b64decode(frame['data']))
            paths.append(path)
        manifest = create_package(self.capture / 'package', paths, times, {'provenance':'recorder_verified','events':[]},
            {'source_hashes': sources, 'adapter_hashes': native.adapter_hashes(), 'variant': 'candidate',
             'browser_version': SUPPORTED_BROWSER_VERSION, 'timing': timing}, metadata)
        result = {'status': 'ok', 'package_dir': str(self.capture / 'package'), 'source_hashes': sources,
                  'adapter_hashes': native.adapter_hashes(), 'functional': oracle['functional'], 'timing': timing}
        saved = {'scenario': scenario, 'browser_version': SUPPORTED_BROWSER_VERSION, 'timestamps': times, 'frame_metadata': metadata,
            'inputs': manifest['inputs'], 'frames': [str(p.relative_to(self.capture)).replace('\\','/') for p in paths], 'timing': timing}
        for name, value in (('capture-start.json', {'variant':'candidate','scenario':scenario,'source_hashes':sources,'adapter_hashes':native.adapter_hashes()}),
            ('capture-result.json', result), ('capture-record.json', saved), ('private/transport-context.json', context),
            ('private/transport-order.json', order), ('private/frame-receipts.json', {'excluded_redundant_checkpoints': redundant,
             'excluded_input_ambiguous_frames': ambiguous}), ('private/visual-clock.json', visual), ('private/oracle.json', oracle),
            ('private/clock.json', clock), ('private/clock-runtime.json', {'kind':'CaptureClockRuntime','status':'completed',
             'loop_closed':True,'timer_period_released':True,'timer_period_requested_ms':None})):
            write_json(self.capture / name, value)
        self.record = {'package': str(self.capture / 'package'), 'scenario':scenario, 'timing':timing}
        self.sources = sources

    def replay(self):
        return final._capture(self.record, self.capture, self.sources, 'candidate', {})

    def test_real_saved_pixel_clock_transport_receipts_replay(self):
        self.assertEqual('pass', self.replay()['native_capture_replay'])

    def test_native_role_clock_or_completed_runtime_tamper_rejected(self):
        for name, key, value in (('capture-start.json','variant','before'), ('private/clock-runtime.json','status','failed'),
                                 ('private/visual-clock.json','physical_display_presentation_claimed',True)):
            path = self.capture / name
            original = read_json(path)
            with self.subTest(name=name):
                write_json(path, {**original, key:value})
                with self.assertRaises(VisualError):
                    self.replay()
                write_json(path, original)


if __name__ == '__main__':
    unittest.main()
