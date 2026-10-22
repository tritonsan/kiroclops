"""Normal-map evidence seals fail closed; no native execution in these tests."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from kiroclops.local_engine import suite_game_demo as demo
from kiroclops.local_engine.source_inventory import full_byte_inventory
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class NormalLevelEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)/'output';self.native=Path(self.temp.name)/'native'
        self.source=Path(self.temp.name)/'source';self.source.mkdir()
        self.full={'controller.gd':'selected-real-controller'};self.source_sha=digest(self.full)
        self.rows=[]
        for scenario in demo._scenarios():
            name=scenario['id'];run=self.native/name
            write_json(run/'recording.json',{'frames':[{'real-fixture':name}]})
            config=dict(scenario,output_dir=str(run),game='heart',normal_level=True,
                        protocol=demo.runtime.VERSION,viewport=[320,180],fps=60)
            write_json(run/'capture_config.json',config)
            proof=run/('engine.'+demo.runtime.VERSION)/'bridge.json';write_json(proof,{'fixture':True})
            manifest={'scenario':scenario,'normal_level':True,'frames':[{'real-fixture':name}],
                      'raw_frame_hashes':{'frame':'pixel-hash'},'provenance':{
                          'runtime_receipt':str(proof),'runtime_receipt_sha256':file_sha256(proof)}}
            write_json(run/'manifest.json',manifest)
            video=self.output/(name+'.mp4');video.parent.mkdir(parents=True,exist_ok=True)
            video.write_bytes(b'fixture-video');write_json(video.with_suffix('.source.json'),{'fixture':True})
            self.rows.append({'id':name,'scenario':scenario,'raw_run':str(run),'gameplay':{'status':'pass'},
                'manifest_sha256':file_sha256(run/'manifest.json'),'video':str(video),'video_sha256':file_sha256(video)})
        self.value={'kind':'ReferenceSuiteNormalLevelEvidence','source_sha256':self.source_sha,
            'selected_source':str(self.source),'native_root':str(self.native),'native_source_hashes':self.full,
            'protocol':demo.runtime.protocol_record(),'coordinates_exported_to_fitter':False,
            'mandatory_route':'normal-combined','status':'pass','slope_diagnostic_status':'pass','scenarios':self.rows}
        self.seal()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for target,kwargs in (
            ('kiroclops.local_engine.suite_game_demo.source_hashes',{'return_value':self.full}),
            ('kiroclops.portable_v2.runtime.validate_recording',{'return_value':None}),
            ('kiroclops.portable_v2.runtime._frames',{'return_value':{'frame':'pixel-hash'}}),
            ('kiroclops.portable_v2.runtime.gameplay_checks',{'return_value':{'status':'pass'}}),
            ('kiroclops.portable_v2.runtime.verify_runtime_receipt',{'return_value':{'status':'pass'}}),
            ('kiroclops.portable_v2.reporting.encode_video',{'side_effect':lambda run,video:video})):
            self.stack.enter_context(patch(target,**kwargs))

    def seal(self):
        self.value['file_hashes']=full_byte_inventory(self.native,ignored=(),excluded_suffixes=())
        write_json(self.output/'result.json',self.value)

    def verify(self):return demo.verify(self.output,source_sha256=self.source_sha)

    def test_intact_evidence_replays_without_recapture(self):
        with patch.object(demo.runtime,'capture_project',side_effect=AssertionError('No recapture')):
            self.assertEqual(self.verify()['status'],'pass')

    def test_changed_native_byte_is_rejected(self):
        (Path(self.rows[0]['raw_run'])/'recording.json').write_text('{}')
        with self.assertRaises(VisualError):self.verify()

    def test_coherent_changed_recipe_cannot_replace_registered_route(self):
        self.rows[0]['scenario']['events'][0]['tick']+=1;self.seal()
        with self.assertRaises(VisualError):self.verify()

    def test_coherent_normal_level_switch_is_rejected(self):
        path=Path(self.rows[0]['raw_run'])/'capture_config.json';value=read_json(path)
        value['normal_level']=False;write_json(path,value);self.seal()
        with self.assertRaises(VisualError):self.verify()

    def test_other_source_cannot_supply_normal_map_proof(self):
        with patch.object(demo,'source_hashes',return_value={'other':'source'}):
            with self.assertRaises(VisualError):self.verify()

    def test_unverified_native_process_is_rejected(self):
        with patch.object(demo.runtime,'verify_runtime_receipt',return_value={'status':'unverified'}):
            with self.assertRaises(VisualError):self.verify()

    def test_missing_video_frame_binding_is_rejected(self):
        Path(self.rows[0]['video']).with_suffix('.source.json').unlink()
        with self.assertRaises(VisualError):self.verify()

    def test_stored_primary_pass_cannot_hide_failed_gameplay(self):
        with patch.object(demo.runtime,'gameplay_checks',return_value={'status':'fail'}):
            with self.assertRaises(VisualError):self.verify()


if __name__=='__main__':unittest.main()
