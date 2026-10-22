"""Native reader timeline regressions; real historical JSON, mocked package/PNG I/O.

No original source, pixels, browser, game, CV or model is changed/run. Native
pixel SHA reads are explicitly mocked for the timeline unit; this is NOT full
source/PNG acceptance or a substitute for the later actual native smoke.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from kiroclops.local_engine import evidence as staged
from kiroclops.portable_v2.specs import scenarios
from kiroclops.visual.schema import VisualError
from tests.evidence import requires_evidence

def raw_case(name='h03',offset=0,repeat=1):
    stage='jump' if name.startswith('h') else 'ground'
    run=ROOT/(f'artifacts/runs/tiny-reference-{name}-x{offset}-r{repeat}' if stage=='jump' else
              f'artifacts/phase2b/reference/{name}-x{offset}-r{repeat}')
    raw=json.loads((run/'manifest.json').read_text('utf-8'))
    expected=next(s for s in scenarios(stage,raw['scenario']['split']) if s['id']==name and s['spawn_offset']==offset and s['repeat']==repeat)
    return run,raw,expected,stage

def package_from_raw(raw,run):
    previous=set();events=[]
    for f in raw['frames']:
        current=set(f['actions'])
        for action in sorted(previous^current):events.append({'time_s':f['time_s'],'action':action,'pressed':action in current})
        previous=current
    return {'frames':[{'index':i,'time_s':f['time_s'],'sha256':'a'*64} for i,f in enumerate(raw['frames'])],
        'inputs':{'provenance':'recorder_verified','events':events},
        'metadata':{'adapter':'legacy-godot-v1','game':raw['game'],'scenario':deepcopy(raw['scenario']),
                    'source_manifest_sha256':hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest()}}


class NativeClockTests(unittest.TestCase):
    def setUp(self):
        self.run,self.raw,self.expected,self.kind=raw_case('h08')
        self.package=package_from_raw(self.raw,self.run)

    def bridge(self,package=None,raw=None,legacy=True):
        return staged._native_frame_bridge(package or self.package,raw or self.raw,legacy_reference=legacy)

    def record(self,name):
        run,raw,expected,kind=raw_case(name);package=package_from_raw(raw,run)
        row={'scenario':deepcopy(raw['scenario']),'package':'synthetic-json-only-package','raw_run':str(run),
             'analysis':None,'measurement_status':'not_run'}
        source={'kind':'frozen_reference','raw_run':str(run.resolve())}
        def sha(path):
            path=Path(path)
            if path.suffix=='.png':return 'a'*64 # No image read; unit proof only.
            return hashlib.sha256(path.read_bytes()).hexdigest()
        return row,package,expected,kind,source,sha

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_actual_tiny_h03_h08_h14_and_ground_raw_only_reader(self):
        for name in ('h03','h08','h14','r06','r48','reverse24'):
            with self.subTest(name=name):
                row,package,expected,kind,source,sha=self.record(name)
                originals=(deepcopy(row),deepcopy(package))
                with patch.object(staged,'load_package',return_value=package),patch.object(staged,'file_sha256',side_effect=sha):
                    self.assertIsNone(staged._record_observation(row,expected,kind,measured=False,expected_source=source))
                self.assertEqual((row,package),originals)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_actual_tiny_rounding_is_preserved_not_replaced(self):
        before=deepcopy(self.package);proof=self.bridge()
        self.assertEqual(self.package,before)
        self.assertEqual(self.package['frames'][1]['time_s'],.016667)
        self.assertEqual(self.package['inputs']['events'][1]['time_s'],.633333)
        self.assertEqual(proof['frame_edges'],self.expected['events'])
        self.assertEqual(sum(abs(f['time_s']-i/60)>=1e-8 for i,f in enumerate(self.raw['frames'])),160)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_package_time_mutation_within_legacy_epsilon_is_rejected(self):
        package=deepcopy(self.package);package['frames'][1]['time_s']+=1e-7
        self.assertLess(abs(package['frames'][1]['time_s']-1/60),1e-5)
        with self.assertRaisesRegex(VisualError,'actual native frame'):self.bridge(package=package)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_nominal_timestamp_rewrite_is_rejected(self):
        package=deepcopy(self.package)
        for i,f in enumerate(package['frames']):f['time_s']=i/60
        with self.assertRaises(VisualError):self.bridge(package=package)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_input_time_must_equal_raw_edge_time_not_nominal(self):
        package=deepcopy(self.package);package['inputs']['events'][1]['time_s']=38/60
        with self.assertRaisesRegex(VisualError,'actual native frame/action'):self.bridge(package=package)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_shifted_wrong_duplicate_and_extra_input_edges_rejected(self):
        for mutation in ('shift','action','pressed','duplicate','extra'):
            package=deepcopy(self.package);events=package['inputs']['events']
            if mutation=='shift':events[1]['time_s']=self.raw['frames'][39]['time_s']
            elif mutation=='action':events[1]['action']='left'
            elif mutation=='pressed':events[1]['pressed']=True
            elif mutation=='duplicate':events.append(deepcopy(events[0]))
            else:events.append({'time_s':1.,'action':'right','pressed':True})
            with self.subTest(mutation=mutation),self.assertRaises(VisualError):self.bridge(package=package)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_correctly_rebound_raw_edges_still_must_match_recipe_frame(self):
        raw=deepcopy(self.raw);raw['frames'][38]['actions']=['jump']
        package=package_from_raw(raw,self.run)
        row={'scenario':raw['scenario'],'package':'unit','analysis':None}
        proof=self.bridge(package=package,raw=raw)
        self.assertEqual(proof['frame_edges'][1]['tick'],39)
        with patch.object(staged,'load_package',return_value=package),patch.object(staged,'_record_source',return_value=proof):
            with self.assertRaisesRegex(VisualError,'required recipe'):
                staged._record_observation(row,self.expected,'jump',measured=False,expected_source={'kind':'frozen_reference'})

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_wrong_physics_index_order_or_count_rejected(self):
        for mutation in ('tick','index','missing','duplicate'):
            raw=deepcopy(self.raw)
            if mutation=='tick':raw['frames'][7]['physics_tick']+=1
            elif mutation=='index':raw['frames'][7]['frame_index']=8
            elif mutation=='missing':raw['frames'].pop()
            else:raw['frames'][7]=deepcopy(raw['frames'][6])
            with self.subTest(mutation=mutation),self.assertRaises(VisualError):self.bridge(raw=raw)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_legacy_nominal_policy_not_unbounded_by_exact_copy(self):
        raw=deepcopy(self.raw);raw['frames'][1]['time_s']+=.001
        package=package_from_raw(raw,self.run)
        with self.assertRaisesRegex(VisualError,'nominal clock'):self.bridge(package=package,raw=raw)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_fake_tiny_metadata_does_not_relax_native_clock(self):
        # Both JSONs may say Tiny; trusted producer expectation controls policy.
        with self.assertRaisesRegex(VisualError,'nominal clock'):self.bridge(legacy=False)
        row,package,expected,kind,source,sha=self.record('h08');source={'kind':'native','sources':{}}
        with patch.object(staged,'load_package',return_value=package),patch.object(staged,'file_sha256',side_effect=sha):
            with self.assertRaisesRegex(VisualError,'nominal clock'):
                staged._record_observation(row,expected,kind,measured=False,expected_source=source)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_frozen_role_does_not_accept_non_tiny_raw(self):
        raw=deepcopy(self.raw);raw['game']='heart'
        with self.assertRaisesRegex(VisualError,'frozen Tiny'):self.bridge(raw=raw)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_native_precise_clock_stays_strict(self):
        raw=deepcopy(self.raw);raw['game']='heart'
        for i,f in enumerate(raw['frames']):f['time_s']=i/60
        package=package_from_raw(raw,self.run)
        self.assertFalse(self.bridge(package=package,raw=raw,legacy=False)['legacy_reference'])
        raw['frames'][1]['time_s']+=1e-7;package=package_from_raw(raw,self.run)
        with self.assertRaisesRegex(VisualError,'nominal clock'):self.bridge(package=package,raw=raw,legacy=False)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_no_trusted_producer_keeps_existing_strict_reader(self):
        row,package,expected,kind,source,sha=self.record('h08')
        with patch.object(staged,'load_package',return_value=package):
            with self.assertRaisesRegex(VisualError,'timing differs'):
                staged._record_observation(row,expected,kind,measured=False)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_unknown_input_provenance_cannot_claim_native_edges(self):
        package=deepcopy(self.package);package['inputs']['provenance']='unknown'
        with self.assertRaisesRegex(VisualError,'provenance'):self.bridge(package=package)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_wrong_source_manifest_hash_or_scenario_rejected_before_timeline(self):
        for mutation in ('hash','scenario'):
            row,package,expected,kind,source,sha=self.record('h08')
            if mutation=='hash':package['metadata']['source_manifest_sha256']='0'*64
            else:package['metadata']['scenario']['repeat']=2
            with self.subTest(mutation=mutation),patch.object(staged,'load_package',return_value=package),patch.object(staged,'file_sha256',side_effect=sha):
                with self.assertRaises(VisualError):staged._record_observation(row,expected,kind,measured=False,expected_source=source)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_clock_verification_does_not_mutate_input_or_raw_manifest(self):
        original_file=(self.run/'manifest.json').read_bytes()
        original_raw=deepcopy(self.raw);original_package=deepcopy(self.package)
        self.bridge()
        self.assertEqual(self.raw,original_raw)
        self.assertEqual(self.package,original_package)
        self.assertEqual((self.run/'manifest.json').read_bytes(),original_file)

    @requires_evidence('artifacts/runs', 'artifacts/phase2b/reference')
    def test_measured_observation_times_still_must_match_exact_package(self):
        row,package,expected,kind,source,sha=self.record('h08')
        package.update(package_sha256='b'*64,image_size=[228,128])
        selection={'objects':[{'id':'character'}]}
        observations={'package_sha256':'b'*64,'image_size':[228,128],'inputs':deepcopy(package['inputs']),
            'selection':selection,'objects':[{'id':'character','frames':[{'index':i,'time_s':f['time_s']} for i,f in enumerate(package['frames'])]}]}
        row.update(analysis='synthetic-clock-only-analysis.json',observations=observations,selection=selection)
        real_reader=staged.read_json
        def reader(path):
            if Path(path).name=='synthetic-clock-only-analysis.json':return {'scenario':deepcopy(row['scenario'])}
            return real_reader(path)
        with patch.object(staged,'load_package',return_value=package),patch.object(staged,'file_sha256',side_effect=sha),patch.object(staged,'read_json',side_effect=reader):
            proof=staged._record_observation(row,expected,kind,measured=True,expected_source=source)
            self.assertEqual(proof[0],'b'*64)
            observations['objects'][0]['frames'][1]['time_s']=1/60
            with self.assertRaisesRegex(VisualError,'Observation frame identities'):
                staged._record_observation(row,expected,kind,measured=True,expected_source=source)


if __name__=='__main__':unittest.main(verbosity=2)
