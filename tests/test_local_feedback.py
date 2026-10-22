from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from kiroclops.local_engine.feedback import motion_feedback
from kiroclops.visual.schema import read_json,file_sha256


class FeedbackTests(unittest.TestCase):
    def record(self):
        frames=[{'index':i,'time_s':i*.017,'x':float(i),'y':0.,'width':20.,'height':30.,
                 'valid':i not in (80,81),'reason':'occluded' if i in (80,81) else None} for i in range(400)]
        return {'scenario':{'id':'visible-response'},'observations':{
            'inputs':{'provenance':'unknown','events':[]},
            'objects':[{'id':'card','valid_ratio':398/400,'frames':frames}]},
            'events':{'events':[{'type':'overshoot','object_ids':['card'],'time_interval_s':[.1,.12],
                               'peak_timing_status':'unresolved','peak_time_interval_s':[.3,.7]}],
                      'relations':[{'type':'connection_attachment','connection_id':'a-b','index':i,
                                    'time_s':i*.017,'endpoint_distances_px':[0,8 if i==110 else 1],
                                    'visually_attached':i!=110} for i in range(400)],
                      'unobservable':[{'index':80,'reason':'occluded'}]}}

    def test_bounded_summary_preserves_real_samples_gaps_and_full_evidence(self):
        record=self.record();original=deepcopy(record)
        with tempfile.TemporaryDirectory(prefix='local café ') as folder:
            path=Path(folder)/'complete.json';out=motion_feedback([record],full_output=path)
            samples=out['profiles'][0]['objects'][0]['trajectory']
            self.assertEqual(64,len(samples));self.assertEqual(0,samples[0]['index']);self.assertEqual(399,samples[-1]['index'])
            self.assertFalse({80,81}&{p['index'] for p in samples})
            gap=out['profiles'][0]['objects'][0]['invalid_ranges'][0]
            self.assertEqual((80,81),(gap['start_index'],gap['end_index']))
            self.assertEqual(record,read_json(path)['records'][0])
            self.assertEqual(file_sha256(path),out['full_pixel_evidence']['sha256'])
            self.assertEqual(original,record)

    def test_detachment_and_unresolved_peak_survive_summary(self):
        out=motion_feedback([self.record()])['profiles'][0]
        self.assertEqual(1,out['relations'][0]['invalid_or_detached_count'])
        self.assertEqual([0,8],out['relations'][0]['ranges']['endpoint_distances_px'])
        self.assertEqual(110,out['relations'][0]['failure_examples'][0]['index'])
        self.assertEqual('unresolved',out['events'][0]['peak_timing_status'])
        self.assertEqual(1,out['unobservable_count'])

    def test_absolute_and_relative_event_timing_are_separate(self):
        record=self.record();record['events']['relations']=[{
            'type':'relative_event_timing','object_ids':['title','content'],
            'event_type':'motion_start','occurrence':0,'lag_interval_s':[.07,.09],
            'order':'after','timing_status':'resolved'}]
        row=motion_feedback([record])['profiles'][0]['relations'][0]
        self.assertEqual([.07,.09],row['lag_interval_s']);self.assertEqual('after',row['order'])


if __name__=='__main__':unittest.main()
