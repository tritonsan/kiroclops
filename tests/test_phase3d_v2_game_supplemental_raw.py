"""Compact raw contacts preserve every role/time/proposal binding."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from scripts import phase3d_v2_game_supplemental_raw as mod
from kiroclops.visual.schema import file_sha256,read_json


class CompactContacts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.paths=[]
        for i in range(20):
            path=self.root/f'frame-{i:02}.png'
            image=np.full((40,60,3),i*10,np.uint8)
            mod.raw.common._save_image(path,image)
            self.paths.append(path)

    def bundle(self,role):
        frames=[{'index':i,'time_s':i/60,'sha256':file_sha256(p)} for i,p in enumerate(self.paths)]
        return {'record':{'id':role,'role':role,'package':self.root,'domain':'game'},
            'manifest':{'frames':frames,'inputs':{'events':[{'time_s':.10}]}},
            'mapping':{'hero':'character'},'active':{'hero':[[0,1]]},
            'rows':[{'index':i,'time_s':i/60,'image_sha256':f['sha256'],
                'points':{'hero':{'x':5+i,'y':10}},'objects':{},'connections':{}}
                for i,f in enumerate(frames)]}

    def contacts(self,bundles,events=None):
        output=self.root/'aid'
        with patch.object(mod,'frame_paths',return_value=self.paths):
            return mod._compact_sheets(bundles,events or [],[None]*len(bundles),output)

    def test_same_pixels_and_same_proposal_dedup_but_roles_remain(self):
        sheets,contact=self.contacts([self.bundle('before'),self.bundle('selected')])
        self.assertEqual(contact['unique_tile_count'],20)
        self.assertEqual(contact['selected_occurrence_count'],40)
        self.assertEqual(len(sheets),2)
        self.assertEqual({x['role'] for x in contact['selected_images']},{'before','selected'})
        for role in ('before','selected'):
            points=[c for c in contact['controls'] if c['role']==role]
            self.assertEqual(len({p['image_sha256'] for p in points}),20)
        self.assertEqual(len({x['tile_index'] for x in contact['selected_images']}),20)
        self.assertFalse(contact['production_coordinates_or_events_used'])

    def test_same_pixels_different_raw_proposal_never_share_tile(self):
        a,b=self.bundle('before'),self.bundle('selected')
        b['rows'][7]['points']['hero']['y']+=1
        _,contact=self.contacts([a,b])
        self.assertEqual(contact['unique_tile_count'],21)
        occurrences=[r for r in contact['selected_images'] if r['index']==7]
        self.assertNotEqual(occurrences[0]['tile_index'],occurrences[1]['tile_index'])
        self.assertEqual(occurrences[0]['image_sha256'],occurrences[1]['image_sha256'])
        self.assertNotEqual(occurrences[0]['raw_proposal_sha256'],occurrences[1]['raw_proposal_sha256'])

    def test_event_support_and_actual_time_survive_cross_role_dedup(self):
        a,b=self.bundle('reference'),self.bundle('selected')
        b['rows'][8]['time_s']+=.003
        _,contact=self.contacts([a,b],[{'record_id':'selected','raw_events':[
            {'indices':[8],'support_indices':[9,10]}]}])
        row=next(r for r in contact['selected_images'] if r['record_id']=='selected' and r['index']==8)
        self.assertAlmostEqual(row['time_s'],8/60+.003)
        self.assertIn('independent_raw_event',row['reasons'])
        for i in (7,8,9,10,11):
            row=next(r for r in contact['selected_images'] if r['record_id']=='selected' and r['index']==i)
            self.assertIn('independent_raw_event',row['reasons'])
        self.assertEqual(contact['unique_tile_count'],20)


if __name__=='__main__':unittest.main()
