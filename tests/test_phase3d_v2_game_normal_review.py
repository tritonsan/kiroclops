from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from scripts import phase3d_v2_game_normal_review as mod
from kiroclops.visual.schema import file_sha256,write_json,read_json,VisualError


class NormalRawReview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.supp=self.root/'supplemental';self.supp.mkdir()
        for name in ('result.json','receipt.json','normal-level/result.json'):write_json(self.supp/name,{})
        self.result={'status':'pass','source_sha256':'exact-source'}
        self.routes=[]
        for name in ('normal-combined','normal-slope'):
            run=self.root/name;frames=run/'frames';frames.mkdir(parents=True)
            hashes={};rows=[]
            for i in range(240):
                path=frames/f'{i:06d}.png'
                image=np.full((20,30,3),i%40,np.uint8)
                mod.suite_raw_review._save_image(path,image)
                hashes[path.name]=file_sha256(path);rows.append({'time_s':i/60})
            write_json(run/'manifest.json',{'frames':rows,'raw_frame_hashes':hashes})
            for filename in ('recording.json','capture_config.json'):write_json(run/filename,{})
            video=self.supp/'normal-level'/f'{name}.mp4';video.write_bytes(b'unit fixture')
            write_json(video.with_suffix('.source.json'),{})
            self.routes.append({'id':name,'raw_run':str(run),'video':str(video),'video_sha256':file_sha256(video),
                'scenario':{'events':[{'tick':30,'action':'jump','pressed':True},{'tick':54,'action':'jump','pressed':False}]},
                'gameplay':{'status':'pass','native_coordinates_not_for_visual_proposal':[1000,9999]}})
        self.normal={'scenarios':self.routes}
        self.output=self.root/'aid'

    def prepare(self):
        with patch.object(mod.supplemental,'verify',return_value=self.result), \
             patch.object(mod.suite_game_demo,'verify',return_value=self.normal), \
             patch.object(mod.glyph,'anchor',return_value=[{'x':10,'y':10}]) as glyph:
            result=mod.prepare(self.supp,self.output)
        return result,glyph

    def test_complete_image_only_pass_controls_roles_inputs_and_dedup(self):
        result,glyph=self.prepare()
        self.assertEqual(glyph.call_count,480)
        self.assertTrue(all(isinstance(c.args[0],np.ndarray) for c in glyph.call_args_list))
        self.assertFalse(result['production_coordinates_used_for_proposals'])
        self.assertFalse(result['full_behavior_acceptance'])
        self.assertEqual({r['route'] for r in result['records']},{'normal-combined','normal-slope'})
        for row in result['records']:
            self.assertEqual(len(row['frames']),240)
            controls=[p for p in row['selected_images'] if 'distinct_raw_control' in p['reasons']]
            self.assertEqual(len({p['image_sha256'] for p in controls}),20)
            self.assertLess(row['unique_tile_count'],len(row['selected_images']))
            self.assertTrue(any('public_input:jump release' in p['reasons'] for p in row['selected_images']))
            self.assertFalse(row['physics_landing_inferred'])
        self.assertEqual(result['records'][0]['mandatory'],True)
        self.assertEqual(result['records'][1]['mandatory'],False)

    def test_source_image_hash_tamper_is_rejected(self):
        path=Path(self.routes[0]['raw_run'])/'frames/000009.png'
        mod.suite_raw_review._save_image(path,np.full((20,30,3),255,np.uint8))
        with self.assertRaises(VisualError) as raised:self.prepare()
        self.assertEqual(raised.exception.code,'source_changed')

    def test_diagnostic_evidence_cannot_replace_mandatory_route(self):
        result,_=self.prepare()
        self.assertEqual(len(result['sheets']),6)
        self.assertTrue(all(s['path'].startswith('sheets/normal-') for s in result['sheets']))
        self.assertEqual(result['status'],'pending_visual_review')
        self.assertFalse((self.output/'attestation.json').exists())


if __name__=='__main__':unittest.main()
