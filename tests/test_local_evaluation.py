"""Image-observation contracts, lifecycle mappings and fair neutral execution."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.evaluation import _canonical_domain, _compare_web, _contract, _dynamic_checks, _lifecycle_bindings, _map_lifecycle, _run_web
from kiroclops.visual.schema import VisualError, write_json


def record(*, amplitude=100., duration=.6, shift=0., static=True, extra_inputs=None, point=None):
    frames = []
    for index in range(181):
        t = index/60 + shift
        x = amplitude * max(0., min(1., (t-shift-.4)/duration))
        if point:
            x = point(t-shift, x)
        frames.append({'index': index, 'time_s': t, 'x': x, 'y': 30., 'left': x-5, 'top': 25.,
                       'width': 10., 'height': 10., 'valid': True, 'present': True, 'expected_active': True})
    objects = [{'id': 'panel', 'frames': frames, 'active_intervals_s': [[shift, 3.+shift]], 'valid_ratio': 1.}]
    if static:
        still = deepcopy(frames)
        for frame in still:
            frame.update(x=20., left=15.)
        objects.append({'id': 'label', 'frames': still, 'active_intervals_s': [[shift, 3.+shift]], 'valid_ratio': 1.})
    inputs = {'provenance': 'recorder_verified', 'events': [{'action': 'click', 'selector': '#open-panel', 'time_s': .4+shift}, *(extra_inputs or [])]}
    observations = {'schema_version': 1, 'kind': 'ObservationSet', 'status': 'ok', 'measurement_status': 'pass',
                    'package_sha256': 'example', 'image_size': [640,360], 'objects': objects, 'connections': [], 'inputs': inputs}
    return {'scenario': {'id': 'synthetic-open', 'domain': 'panel'}, 'observations': observations, 'measurement_status': 'ok',
            'functional': {'status': 'pass'}, 'selection': {'schema_version': 1, 'objects': [{'id': obj['id']} for obj in objects]}}


class ContractTests(unittest.TestCase):
    def test_removed_by_first_input_requires_visible_initial_identity_then_absence(self):
        ref=record();ref['observations']['inputs']['events'][0]['selector']='#remove-node'
        gone=deepcopy(ref['observations']['objects'][1]);gone['id']='gone';gone['active_intervals_s']=[[0.,23/60]]
        for frame in gone['frames']:
            if frame['time_s']>23/60:frame.update(valid=False,present=False,expected_active=False,x=None,y=None,width=None,height=None)
        ref['observations']['objects'].append(gone)
        result=_compare_web(ref,ref,deepcopy(ref),improvement=False)
        self.assertTrue(result['pass'],result)
        window=next(w for w in result['contract']['windows'] if w['id']=='initial-preservation')
        self.assertEqual(['gone'],window['object_ids']);self.assertLess(window['start_s'],0)
        missing=deepcopy(ref);missing['observations']['objects'].pop()
        self.assertFalse(_compare_web(ref,ref,missing,improvement=False)['pass'])
        extra=deepcopy(ref);frame=extra['observations']['objects'][-1]['frames'][60];frame['present']=True
        self.assertFalse(_compare_web(ref,ref,extra,improvement=False)['pass'])

    def test_child_timing_cannot_hide_in_large_parent_translation(self):
        def panel(delay):
            item=record(amplitude=625,static=False);parent=item['observations']['objects'][0];parent['id']='handle'
            child=deepcopy(parent);child['id']='title'
            for frame,base in zip(child['frames'],parent['frames']):
                frame.update(x=base['x']+30,y=50+24*min(1.,max(0.,(frame['time_s']-.4-delay)/.3)))
            item['observations']['objects'].append(child)
            item['selection']['relative_motion']=[{'object_id':'title','relative_to':'handle'}]
            return item
        ref,candidate=panel(.08),panel(.22)
        result=_compare_web(ref,ref,candidate,improvement=False)
        pair=next(p for p in result['contract']['object_pairs'] if p['reference_id']=='title')
        self.assertEqual(24.,pair['scale_px']);self.assertFalse(result['pass'])
        self.assertFalse(next(r for r in result['pixel_comparison']['object_windows'] if r['reference_id']=='title')['gates']['position'])
        identical=_compare_web(ref,candidate,deepcopy(ref),improvement=True)
        # An already-correct parent cannot manufacture a 50% improvement claim.
        self.assertFalse(identical['pass'])
        self.assertTrue(_compare_web(ref,ref,deepcopy(ref),improvement=False)['pass'])

    def test_two_actual_clicks_have_separate_response_clocks(self):
        def closing(second,shift=0.,speed=1.):
            item=record(shift=shift,extra_inputs=[{'action':'click','selector':'#close-panel','time_s':second+shift}])
            for frame in item['observations']['objects'][0]['frames']:
                t=frame['time_s']-shift
                frame['x']=100*max(0.,min(1.,(t-.4)/.4)) if t<second else 100*max(0.,1.-speed*(t-second)/.4)
                frame['left']=frame['x']-5
            return item
        reference=closing(1.2);candidate=closing(1.2137,shift=.09)
        result=_compare_web(reference,closing(1.2,speed=.5),candidate,improvement=False)
        self.assertTrue(result['pass'],result)
        windows=result['contract']['windows']
        self.assertEqual('#open-panel',windows[0]['alignment']['selector'])
        self.assertEqual('#close-panel',windows[1]['alignment']['selector'])
        dynamic=[d for d in result['dynamics'] if d['observable']]
        self.assertEqual(1,len(dynamic))
        self.assertAlmostEqual(1.3037,dynamic[0]['input_timing']['candidate_origin_s'])
        self.assertFalse(_compare_web(reference,reference,closing(1.2137,speed=.5),improvement=False)['pass'])

    def test_repeated_toggle_click_uses_occurrence_identity(self):
        reference=record(extra_inputs=[{'action':'click','selector':'#open-panel','time_s':1.1}])
        windows=_contract(reference)['windows']
        self.assertEqual([0,1],[w['alignment']['ordinal'] for w in windows])

    def test_aliases(self):
        self.assertEqual('panel', _canonical_domain('web_panel'))
        self.assertEqual('svg', _canonical_domain('svg'))

    def test_identical_final_improves_moving_objects_and_keeps_static_identity(self):
        reference = record()
        result = _compare_web(reference, record(amplitude=65), deepcopy(reference), improvement=True)
        self.assertTrue(result['pass'], result)
        self.assertEqual(['panel'], [gain['object_id'] for gain in result['improvement']])
        self.assertTrue(all(row['pass'] for row in result['dynamics'] if row['observable']))

    def test_missing_static_object_never_passes(self):
        reference, candidate = record(), record()
        candidate['observations']['objects'] = candidate['observations']['objects'][:1]
        result = _compare_web(reference, record(amplitude=50), candidate, improvement=True)
        self.assertFalse(result['pass'])
        self.assertIn('missing_object', [error['code'] for error in result['pixel_comparison']['errors']])

    def test_gain_uses_equal_window_object_average_not_each_zero_error_window(self):
        reference = record(extra_inputs=[{'action': 'click', 'selector': '#close-panel', 'time_s': 1.1}])
        before = deepcopy(reference)
        # The idle second segment is already exact. It is not required to beat zero.
        for frame in before['observations']['objects'][0]['frames']:
            if .4 < frame['time_s'] < 1.:
                frame['x'] -= 10
                frame['left'] -= 10
        result = _compare_web(reference, before, deepcopy(reference), improvement=True)
        self.assertTrue(result['pass'], result)
        self.assertEqual(1., result['improvement'][0]['improvement_fraction'])

    def test_baseline_already_perfect_does_not_claim_improvement(self):
        reference = record()
        result = _compare_web(reference, deepcopy(reference), deepcopy(reference), improvement=True)
        self.assertFalse(result['pass'])
        self.assertIsNone(result['improvement'][0]['improvement_fraction'])

    def test_missing_settlement_is_explicit_failure(self):
        reference = record()
        candidate = record(point=lambda t,x: x+4 if t >= .8 else x)
        checks = _dynamic_checks(reference, candidate, _contract(reference, candidate))
        observable = [row for row in checks if row['observable']]
        self.assertTrue(observable)
        self.assertFalse(observable[0]['pass'])
        self.assertFalse(observable[0]['gates']['settled_event_observed'])

    def test_extra_overshoot_cannot_hide_in_correct_endpoint(self):
        reference = record()
        candidate = record(point=lambda t,x: x+12 if 1.02 <= t <= 1.08 else x)
        checks = _dynamic_checks(reference, candidate, _contract(reference, candidate))
        self.assertFalse([row for row in checks if row['observable']][0]['gates']['overshoot'])

    def test_wrong_settling_time_fails_fixed_tolerance(self):
        reference = record()
        candidate = record(duration=1.1)
        checks = _dynamic_checks(reference, candidate, _contract(reference, candidate))
        observable = [row for row in checks if row['observable']]
        self.assertFalse(observable[0]['pass'])

    def test_interrupted_short_segment_does_not_invent_a_settled_event(self):
        reference = record(extra_inputs=[{'action': 'click', 'selector': '#close-panel', 'time_s': .55}])
        checks = _dynamic_checks(reference, deepcopy(reference), _contract(reference))
        first = next(row for row in checks if row['window_id'] == 'motion-0')
        self.assertFalse(first['observable'])
        self.assertIn(first['reason'], ('reference_interval_too_short', 'reference_interrupted_or_unsettled'))

    def test_timestamp_shift_preserves_actual_motion_duration(self):
        reference = record()
        result = _compare_web(reference, record(amplitude=65), record(shift=.137), improvement=True)
        self.assertTrue(result['pass'], result)

    def test_reference_lifetimes_do_not_truncate_other_objects(self):
        reference = record()
        transient = deepcopy(reference['observations']['objects'][0])
        transient['id'] = 'new-node'
        transient['active_intervals_s'] = [[.7,1.2]]
        for frame in transient['frames']:
            if not .7 <= frame['time_s'] <= 1.2:
                frame.update(valid=False, present=False, expected_active=False, x=None, y=None, width=None, height=None)
        reference['observations']['objects'].append(transient)
        contract = _contract(reference)
        self.assertGreater(contract['windows'][-1]['end_s']+.4, 1.2)
        result = _compare_web(reference, deepcopy(reference), deepcopy(reference), improvement=False)
        self.assertTrue(result['pass'], result)


class LifecycleTests(unittest.TestCase):
    def manifest(self, shift=0., add=.4, remove=1.8):
        return {'frames': [{'time_s': i/60+shift} for i in range(181)],
                'inputs': {'provenance': 'recorder_verified', 'events': [
                    {'action': 'click', 'selector': '#add-node', 'time_s': add+shift},
                    {'action': 'click', 'selector': '#remove-node', 'time_s': remove+shift}]}}

    def test_clip_edges_map_to_actual_candidate_clip(self):
        selection = {'objects': [{'id': 'a', 'active_intervals_s': [[0,3]]}]}
        bindings = _lifecycle_bindings(selection, self.manifest())
        mapped = _map_lifecycle(bindings, self.manifest(shift=.13))
        self.assertEqual([[.13,3.13]], mapped['a'])

    def test_birth_and_removal_map_to_their_own_actual_inputs(self):
        selection = {'objects': [{'id': 'd', 'active_intervals_s': [[.42,1.78]]}]}
        bindings = _lifecycle_bindings(selection, self.manifest())
        mapped = _map_lifecycle(bindings, self.manifest(shift=.1, add=.43, remove=1.86))
        self.assertAlmostEqual(.55, mapped['d'][0][0])
        self.assertAlmostEqual(1.94, mapped['d'][0][1])

    def test_missing_defining_input_is_timing_error(self):
        selection = {'objects': [{'id': 'd', 'active_intervals_s': [[.42,3]]}]}
        bindings = _lifecycle_bindings(selection, self.manifest())
        target = self.manifest()
        target['inputs']['events'] = []
        with self.assertRaises(VisualError) as error:
            _map_lifecycle(bindings, target)
        self.assertEqual('capture_timing_failure', error.exception.code)

    def test_comparison_binds_candidate_declared_lifecycle_to_inputs(self):
        reference, candidate = record(), record(shift=.1)
        ref_manifest = {'frames': reference['observations']['objects'][0]['frames'], 'inputs': reference['observations']['inputs']}
        selection = {'objects': [{'id': obj['id'], 'active_intervals_s': obj['active_intervals_s']} for obj in reference['observations']['objects']]}
        reference['selection']['lifecycle_bindings'] = _lifecycle_bindings(selection, ref_manifest)
        contract = _contract(reference, candidate)
        self.assertAlmostEqual(-.4, contract['object_pairs'][0]['candidate_active_intervals_s'][0][0])


class NeutralFairnessTests(unittest.TestCase):
    def test_baseline_prepare_does_not_analyze_or_create_profile(self):
        raw = {'scenario': {'id':'one'}, 'functional': {'status':'pass'}, 'package':'unused'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = {'mode':'baseline','domain':'web_panel','reference':'private','baseline':'initial'}
            with patch('kiroclops.local_engine.evaluation._web_matrix', return_value=[raw]) as capture, \
                 patch('kiroclops.local_engine.prepared_evidence.store_prepared',return_value='bound-by-separate-integrity-tests'), \
                 patch('kiroclops.local_engine.evaluation._export_calibration', return_value=[]) as export, \
                 patch('kiroclops.local_engine.evaluation._profile', side_effect=AssertionError('Profile forbidden')):
                result = _run_web(root, {'job_id':'j','operation':'prepare'}, state, lambda:None, lambda *a:None, lambda:False)
            self.assertNotIn('profile', result)
            self.assertTrue(all(call.kwargs['analyze'] is False for call in capture.call_args_list))
            self.assertEqual(2, export.call_count)

    def test_baseline_evaluate_only_captures_and_never_compares(self):
        raw = {'scenario': {'id':'one'}, 'functional': {'status':'pass'}, 'package':'unused'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root/'prepared-private.json', {'reference':'must-not-read','before':'must-not-read'})
            state = {'mode':'baseline','domain':'web_panel'}
            job = {'job_id':'j','operation':'evaluate','candidate_id':'candidate-01','source_path':'actual-source'}
            with patch('kiroclops.local_engine.evaluation._web_matrix', return_value=[raw]) as capture, \
                 patch('kiroclops.local_engine.prepared_evidence.load_prepared',return_value={}), \
                 patch('kiroclops.local_engine.evaluation._export_candidate', return_value=[]), \
                 patch('kiroclops.local_engine.evaluation._compare_web', side_effect=AssertionError('Comparison forbidden')):
                result = _run_web(root,job,state,lambda:None,lambda *a:None,lambda:False)
            self.assertTrue(result['eligible'])
            self.assertIsNone(result['score'])
            self.assertNotIn('comparisons', result)
            self.assertFalse(capture.call_args.kwargs['analyze'])

    def test_baseline_functional_failure_is_neutral_ineligible(self):
        raw = {'scenario': {'id':'one'}, 'functional': {'status':'fail'}, 'package':'unused'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root/'prepared-private.json', {})
            with patch('kiroclops.local_engine.evaluation._web_matrix', return_value=[raw]), \
                 patch('kiroclops.local_engine.prepared_evidence.load_prepared',return_value={}), \
                 patch('kiroclops.local_engine.evaluation._export_candidate', return_value=[]):
                result = _run_web(root, {'job_id':'j','operation':'evaluate','candidate_id':'candidate-01','source_path':'source'},
                                  {'domain':'svg','mode':'baseline'},lambda:None,lambda *a:None,lambda:False)
            self.assertFalse(result['eligible'])
            self.assertNotIn('comparisons', result)


if __name__ == '__main__':
    unittest.main()
