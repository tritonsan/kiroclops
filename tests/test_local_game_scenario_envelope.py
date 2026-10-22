"""Adapter scenario-envelope regressions: JSON-only, with every renderer/tracker replaced by a fail guard."""
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import VisualError,digest,file_sha256,read_json,write_json
from kiroclops.portable_v2.specs import scenarios,run_name

from kiroclops.local_engine import game


def native_value(scenario,*,legacy=True):
    raw={k:deepcopy(scenario[k]) for k in ('hold_ticks','start_tick','split','repeat','spawn_offset')} if legacy else deepcopy(scenario)
    state=set();frames=[]
    for index in range(240):
        for event in scenario['events']:
            if event['tick']==index:
                if event['pressed']:state.add(event['action'])
                else:state.discard(event['action'])
        frames.append({'frame_index':index,'physics_tick':60+index,'time_s':round(index/60,6),
                       'actions':sorted(state)})
    return {'schema_version':1,'status':'ok','fps':60,'frame_count':240,'scenario':raw,
            'game':'tiny' if legacy else 'heart','variant':'reference' if legacy else 'before','frames':frames}


def package_json(run,package,analysis=None):
    raw=read_json(run/'manifest.json');previous=set();events=[]
    for frame in raw['frames']:
        actual=set(frame['actions'])
        for action in sorted(previous ^ actual):
            events.append({'time_s':frame['time_s'],'action':action,'pressed':action in actual})
        previous=actual
    value={'kind':'VideoPackage','schema_version':1,'image_size':[228,128],
        'frames':[{'index':i,'time_s':f['time_s'],'path':f'frames/{i:06d}.png','sha256':'a'*64}
                  for i,f in enumerate(raw['frames'])],
        'inputs':{'provenance':'recorder_verified','events':events},
        'metadata':{'adapter':'legacy-godot-v1','source_manifest_sha256':file_sha256(run/'manifest.json'),
                    'scenario':raw['scenario'],'source_id':run.name,'domain':'game','game':raw['game'],'variant':raw['variant']}}
    value['package_sha256']=digest(value);write_json(package/'manifest.json',value)
    if analysis:
        write_json(analysis,{'scenario':raw['scenario'],'status':'ok','provenance':{
            'manifest_sha256':file_sha256(run/'manifest.json'),'package_sha256':value['package_sha256']}})
    return {'scenario':deepcopy(raw['scenario']),'raw_run':str(run),'package':str(package),
            'analysis':str(analysis) if analysis else None,'measurement_status':'ok' if analysis else 'not_run'}


class ScenarioEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.s=scenarios('jump','calibration')[0]
        self.raw=self.root/'raw';write_json(self.raw/'manifest.json',native_value(self.s))
        self.record=package_json(self.raw,self.root/'package',self.root/'analysis.json')
        self.no_cv=patch.object(game,'analyze_package',side_effect=AssertionError('No CV permitted'))
        self.no_cv.start();self.addCleanup(self.no_cv.stop)

    def update_raw(self,edit):
        value=read_json(self.raw/'manifest.json');edit(value);write_json(self.raw/'manifest.json',value)
        self.record=package_json(self.raw,self.root/'package',self.root/'analysis.json')

    def update_package(self,edit,*,rehash=True):
        p=Path(self.record['package'])/'manifest.json';value=read_json(p);edit(value)
        if rehash:value['package_sha256']=digest({k:v for k,v in value.items() if k!='package_sha256'})
        write_json(p,value)

    def bind(self):return game._canonical_record(self.record,self.s,'jump')

    def test_legacy_missing_id_gets_canonical_envelope_without_evidence_rewrite(self):
        files=[self.raw/'manifest.json',self.root/'package/manifest.json',self.root/'analysis.json']
        before={str(p):p.read_bytes() for p in files};original=deepcopy(self.record)
        bound=self.bind()
        self.assertEqual(bound['scenario'],self.s);self.assertNotIn('id',self.record['scenario'])
        self.assertEqual(self.record,original);self.assertEqual(before,{str(p):p.read_bytes() for p in files})
        self.assertEqual(bound['scenario_binding']['source_manifest_sha256'],file_sha256(files[0]))
        self.assertEqual(bound['scenario_binding']['analysis_sha256'],file_sha256(files[2]))

    def test_explicit_matching_id_is_accepted(self):
        self.update_raw(lambda d:d['scenario'].update(id='h03'))
        self.assertEqual(self.bind()['scenario'],self.s)

    def test_conflicting_scalar_recipe_fields_rejected(self):
        for key,value in [('id','h24'),('hold_ticks',4),('start_tick',31),('split','validation'),
                          ('repeat',2),('spawn_offset',24),('fps',30),('frame_count',239),
                          ('warmup_ticks',59),('mechanic','ground')]:
            with self.subTest(field=key):
                write_json(self.raw/'manifest.json',native_value(self.s))
                self.update_raw(lambda d:d['scenario'].update({key:value}))
                with self.assertRaises(VisualError):self.bind()

    def test_conflicting_explicit_events_rejected_even_when_raw_actions_match(self):
        wrong=deepcopy(self.s['events']);wrong[1]['tick']+=1
        self.update_raw(lambda d:d['scenario'].update(events=wrong))
        with self.assertRaisesRegex(VisualError,'events'):self.bind()

    def test_missing_required_fields_rejected(self):
        for key in ('hold_ticks','start_tick','split','repeat','spawn_offset'):
            with self.subTest(field=key):
                write_json(self.raw/'manifest.json',native_value(self.s))
                self.update_raw(lambda d:d['scenario'].pop(key))
                with self.assertRaises(VisualError):self.bind()

    def test_nonregistered_recipe_is_rejected(self):
        self.s['events'][1]['tick']=34
        with self.assertRaisesRegex(VisualError,'registered experiment'):self.bind()

    def test_shifted_missing_extra_duplicate_or_late_inputs_rejected(self):
        for mode in ('shift','missing','extra','duplicate','tail'):
            def mutate(d):
                if mode=='shift':d['frames'][30]['actions']=[];d['frames'][33]['actions']=['jump']
                if mode=='missing':d['frames'][31]['actions']=[]
                if mode=='extra':d['frames'][31]['actions']=['jump','left']
                if mode=='duplicate':d['frames'][31]['actions']=['jump','jump']
                if mode=='tail':d['frames'][239]['actions']=['jump']
            with self.subTest(mode=mode):
                write_json(self.raw/'manifest.json',native_value(self.s));self.update_raw(mutate)
                with self.assertRaisesRegex(VisualError,'Recorded actions'):self.bind()

    def test_missing_duplicated_physics_or_corrupt_frame_time_rejected(self):
        for mode in ('missing','physics','index','nan','shift'):
            def mutate(d):
                if mode=='missing':d['frames'].pop()
                if mode=='physics':d['frames'][100]['physics_tick']=159
                if mode=='index':d['frames'][100]['frame_index']=99
                if mode=='nan':d['frames'][100]['time_s']='not-a-time'
                if mode=='shift':d['frames'][100]['time_s']+=.001
            with self.subTest(mode=mode):
                write_json(self.raw/'manifest.json',native_value(self.s));self.update_raw(mutate)
                with self.assertRaises(VisualError):self.bind()

    def test_serialized_six_decimal_timestamps_are_preserved(self):
        original=read_json(self.raw/'manifest.json')['frames'][1]['time_s']
        self.assertEqual(original,.016667);self.bind()
        self.assertEqual(read_json(self.root/'package/manifest.json')['frames'][1]['time_s'],original)

    def test_package_source_manifest_or_scenario_disagreement_rejected(self):
        for key,value in [('source_manifest_sha256','0'*64),('scenario',{}),('source_id','foreign'),
                          ('adapter','other'),('game','heart'),('variant','candidate')]:
            with self.subTest(field=key):
                self.record=package_json(self.raw,self.root/'package',self.root/'analysis.json')
                self.update_package(lambda p:p['metadata'].update({key:value}))
                with self.assertRaisesRegex(VisualError,'native recording'):self.bind()

    def test_imported_input_shift_extra_missing_and_provenance_rejected(self):
        for mode in ('shift','extra','missing','provenance'):
            def mutate(p):
                if mode=='shift':p['inputs']['events'][0]['time_s']+=1/60
                if mode=='extra':p['inputs']['events'].append({'time_s':1.,'action':'left','pressed':True})
                if mode=='missing':p['inputs']['events'].pop()
                if mode=='provenance':p['inputs']['provenance']='user_declared'
            with self.subTest(mode=mode):
                self.record=package_json(self.raw,self.root/'package',self.root/'analysis.json')
                self.update_package(mutate)
                with self.assertRaisesRegex(VisualError,'input timeline'):self.bind()

    def test_package_manifest_digest_and_frame_time_tamper_rejected(self):
        self.update_package(lambda p:p.update(image_size=[320,180]),rehash=False)
        with self.assertRaisesRegex(VisualError,'manifest identity'):self.bind()
        self.record=package_json(self.raw,self.root/'package',self.root/'analysis.json')
        self.update_package(lambda p:p['frames'][120].update(time_s=2.01))
        with self.assertRaisesRegex(VisualError,'frame timeline'):self.bind()

    def test_analysis_foreign_scenario_manifest_or_package_rejected(self):
        for mode in ('scenario','manifest','package'):
            with self.subTest(mode=mode):
                self.record=package_json(self.raw,self.root/'package',self.root/'analysis.json')
                value=read_json(self.record['analysis'])
                if mode=='scenario':value['scenario']['repeat']=3
                else:value['provenance'][mode+'_sha256']='0'*64
                write_json(self.record['analysis'],value)
                with self.assertRaisesRegex(VisualError,'Analysis belongs'):self.bind()

    def test_analysis_envelope_disagreement_rejected(self):
        self.record['scenario']['repeat']=3
        with self.assertRaisesRegex(VisualError,'envelope'):self.bind()

    def test_ground_reverse_actions_and_identity_stay_canonical(self):
        s=next(s for s in scenarios('ground','validation') if s['id']=='reverse24')
        write_json(self.raw/'manifest.json',native_value(s,legacy=False))
        record=package_json(self.raw,self.root/'package')
        bound=game._canonical_record(record,s,'ground')
        self.assertEqual(bound['scenario'],s)
        self.assertEqual([e['action'] for e in read_json(self.root/'package/manifest.json')['inputs']['events']
                          if e['time_s']==.9],['left','right'])

    def test_raw_only_does_not_read_or_expose_analysis(self):
        self.record['analysis']=None;self.record['measurement_status']='not_run'
        (self.root/'analysis.json').unlink();bound=self.bind()
        self.assertIsNone(bound['analysis']);self.assertNotIn('analysis_sha256',bound['scenario_binding'])
        self.assertNotIn('metrics',bound);self.assertNotIn('profile',bound)

    def test_reference_dispatch_measured_and_raw_use_same_binding(self):
        for analyze in (True,False):
            with self.subTest(analyze=analyze):
                run=self.root/'artifacts/runs/tiny-reference-h03-x0-r1'
                write_json(run/'manifest.json',native_value(self.s))
                def importer(raw,package):return package_json(raw,package)
                def analyzer(raw,stage,output,**kwargs):return package_json(raw,Path(output)/'package',Path(output)/'analysis.json')
                with patch.object(game,'ROOT',self.root),patch.object(game,'scenarios',return_value=[self.s]),\
                     patch.object(game,'import_legacy_run',side_effect=importer) as imported,\
                     patch.object(game,'analyze_game_run',side_effect=analyzer) as analyzed:
                    rows=game._reference('jump','calibration',self.root/str(analyze),lambda:None,lambda *a:None,analyze=analyze)
                self.assertEqual(rows[0]['scenario'],self.s);self.assertIn('scenario_binding',rows[0])
                self.assertEqual(analyzed.call_count,int(analyze));self.assertEqual(imported.call_count,int(not analyze))

    def test_capture_dispatch_measured_and_raw_use_same_binding(self):
        for analyze in (True,False):
            with self.subTest(analyze=analyze),ExitStack() as stack:
                def capture(project,raw,recipe,**kwargs):
                    value=native_value(self.s,legacy=False);value['scenario']=recipe;write_json(raw/'manifest.json',value)
                def analyzer(raw,stage,output,**kwargs):return package_json(raw,Path(output)/'package',Path(output)/'analysis.json')
                stack.enter_context(patch.object(game,'scenarios',return_value=[self.s]))
                stack.enter_context(patch.object(game,'copy_project'))
                stack.enter_context(patch.object(game.runtime,'source_hashes',return_value={'player.gd':'a'*64}))
                stack.enter_context(patch.object(game.runtime,'import_project'))
                stack.enter_context(patch.object(game.runtime,'capture_project',side_effect=capture))
                stack.enter_context(patch.object(game,'import_legacy_run',side_effect=package_json))
                measured=stack.enter_context(patch.object(game,'analyze_game_run',side_effect=analyzer))
                rows=game._capture(self.root/'project','jump','calibration',self.root/str(analyze),'before',lambda:None,lambda *a:None,None,analyze=analyze)
                self.assertEqual(rows[0]['scenario'],self.s);self.assertIn('scenario_binding',rows[0])
                self.assertEqual(measured.call_count,int(analyze))

    def test_paired_export_and_actual_reporting_group_identity(self):
        from kiroclops.local_engine import reporting
        ref=self.bind();raw_before=self.root/'before';write_json(raw_before/'manifest.json',native_value(self.s,legacy=False))
        before=game._canonical_record(package_json(raw_before,self.root/'before-package'),self.s,'jump')
        with patch.object(game,'copy_package') as copy:
            exported=game._export(self.root,[ref],'reference')+game._export(self.root,[before],'before')
        self.assertEqual([Path(e['package']).name for e in exported],['h03-x0-r1']*2)
        self.assertEqual(copy.call_count,2)
        measurement=self.root/'session/measurements/job'
        write_json(measurement/'reference.json',[ref]);write_json(measurement/'before.json',[before])
        state={'domain':'game','mode':'baseline','goal':'test','status':'prepared','candidates':[]}
        with patch.object(reporting,'Session') as session,patch.object(reporting,'session_costs',return_value={}),\
             patch.object(reporting,'_media',return_value={'frames':[],'image_size':[228,128]}):
            session.return_value._load.return_value=state
            result=reporting.build_report(self.root/'session',encode=False)
        report=read_json(result['data'])
        self.assertEqual(len(report['groups']),1)
        self.assertEqual(set(report['groups'][0]['roles']),{'reference','before'})


if __name__=='__main__':unittest.main(verbosity=2)
