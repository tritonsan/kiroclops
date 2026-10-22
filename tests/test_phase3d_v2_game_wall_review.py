from __future__ import annotations
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import numpy as np
from kiroclops.visual.schema import VisualError, digest, file_sha256, write_json
from kiroclops.local_engine.suite_final_raw_review import _wall as old_wall
from scripts import phase3d_v2_game_wall_review as review


def wall_image(side='right'):
    image = np.zeros((180,320,3),np.uint8)
    x = 192 if side == 'right' else 64
    image[64:,x:x+16] = 120
    if side == 'right':
        # Visible inward face is textured: one blank row in each eight.
        for y in range(64,180,8): image[y,x] = 0
    return image


def event(t=1., kind='overshoot'):
    return {'type':kind,'object_ids':['character'],'observed_interval_s':[t,t+.01],
        'uncertainty_s':.01,'timing_status':'resolved','indices':[10,11]}


class StaticWallTests(unittest.TestCase):
    def test_textured_inner_face_reproduces_old_failure_and_new_measurement(self):
        image=wall_image()
        self.assertEqual(old_wall(image,'right')['x'],207.5)
        self.assertEqual(review._edge(image,'right')['x'],191.5)

    def test_left_side_selects_inward_face(self):
        self.assertEqual(review._edge(wall_image('left'),'left')['x'],79.5)

    def test_static_pool_does_not_shorten_edge_when_character_occludes_it(self):
        image=wall_image('left'); covered=image.copy();covered[83:99,79:81]=250
        inputs={'provenance':'recorder_verified','events':[{'time_s':.5}]}
        wall=review.static_wall([image]*3+[covered],[0.,.1,.2,.6],inputs,'left')
        self.assertEqual((wall['x'],wall['top'],wall['bottom']),(79.5,64,179))
        self.assertTrue(review._supported(covered,wall, {'x':83.5,'y':92})[0])

    def test_camera_shift_during_geometry_pool_fails(self):
        image=wall_image(); shifted=np.roll(image,3,axis=1)
        with self.assertRaises(VisualError):review.static_wall([image,image,shifted],[0,.1,.2],
            {'provenance':'recorder_verified','events':[{'time_s':.5}]},'right')

    def test_post_input_wall_loss_is_not_filled_as_measurement(self):
        wall=review.static_wall([wall_image()]*3,[0,.1,.2], {'provenance':'recorder_verified','events':[{'time_s':.5}]},'right')
        self.assertFalse(review._supported(np.zeros((180,320,3),np.uint8),wall)[0])

    def test_outer_face_alias_from_a_full_wall_width_shift_fails(self):
        for side, shift in [('right', -16), ('left', 16)]:
            image=wall_image(side)
            wall=review.static_wall([image]*3,[0,.1,.2], {'provenance':'recorder_verified','events':[{'time_s':.5}]},side)
            shifted=np.roll(image,shift,axis=1)
            self.assertFalse(review._supported(shifted,wall)[0])

    def test_vertical_camera_shift_not_occluded_by_character_fails(self):
        image=wall_image()
        wall=review.static_wall([image]*3,[0,.1,.2], {'provenance':'recorder_verified','events':[{'time_s':.5}]},'right')
        self.assertFalse(review._supported(np.roll(image,4,axis=0),wall)[0])

    def test_unknown_input_history_does_not_select_pool(self):
        with self.assertRaises(VisualError):review.static_wall([wall_image()]*3,[0,.1,.2],
            {'provenance':'unknown','events':[{'time_s':.5}]},'right')

    def test_too_few_preinput_images_fails(self):
        with self.assertRaises(VisualError):review.static_wall([wall_image()]*3,[0,.1,.2],
            {'provenance':'recorder_verified','events':[{'time_s':.15}]},'right')

    def test_disjoint_short_fragments_are_not_joined_into_wall(self):
        image=np.zeros((180,320,3),np.uint8)
        for y in range(0,180,15):image[y:y+4,192:208]=120
        self.assertEqual(review._edge(image,'right')['status'],'unobservable')

    def test_motion_occurrences_remain_strict(self):
        bundle={'record':{'id':'case'},'event_set':{'events':[]}}
        _, errors=review._motion_compare(bundle,[event()])
        self.assertEqual(errors[0]['code'],'event_occurrence_mismatch')

    def test_motion_timing_gate_is_unchanged(self):
        e=event(); produced={**e,'time_interval_s':[1.,1.01]}
        bundle={'record':{'id':'case'},'event_set':{'events':[produced]}}
        checks,errors=review._motion_compare(bundle,[e]);self.assertFalse(errors);self.assertTrue(checks[0]['pass'])
        shifted=event(1.051);_,errors=review._motion_compare(bundle,[shifted]);self.assertTrue(errors)

    def test_boundary_events_are_not_counted_as_motion(self):
        bundle={'record':{'id':'case'},'event_set':{'events':[{'type':'boundary_enter'}]}}
        self.assertEqual(review._motion_compare(bundle,[]),([],[]))

    def test_motion_uncertainty_cannot_pass(self):
        e=event();e['uncertainty_s']=.101
        bundle={'record':{'id':'case'},'event_set':{'events':[{**e,'time_interval_s':[1,1.01]}]}}
        self.assertTrue(review._motion_compare(bundle,[e])[1])


class EvidenceTests(unittest.TestCase):
    def fixture(self, directory):
        rows=[{'record_id':'plain-'+str(i),'role':'reference'} for i in range(120)]
        rows += [{'record_id':'boundary-'+str(i),'role':'boundary_selected'} for i in range(8)]
        result={'kind':'UnsignedMixedEpochGameSupplementalRawAid','numeric_status':'fail','historical_final_status':'failed',
            'full_behavior_acceptance':False,'unsupported':[],'records':rows,
            'producer_binding':{'source_binding_status':'pass'},'positions':[{'pass':True}], 'events':[],
            'failures':[{'record_id':'boundary-0','code':'raw_boundary_distance_disagreement'}]}
        matrix={'raw':'source_bound'}
        write_json(directory/'result.json',result);write_json(directory/'matrix.json',matrix)
        receipt={'result_sha256':digest(result),'matrix_sha256':digest(matrix),'measurement_bindings_sha256':digest(rows),
            'source_files':{},'code_sources':{},'generated_files':{n:file_sha256(directory/n) for n in ('result.json','matrix.json')}}
        write_json(directory/'evidence-receipt.json',receipt)
        return result,receipt

    def test_unmodified_byte_bound_predecessor_reuses_proof(self):
        with TemporaryDirectory() as d:
            directory=Path(d);self.fixture(directory)
            self.assertEqual(review._verify_parent(directory)[0]['numeric_status'],'fail')

    def test_changed_predecessor_generated_bytes_are_rejected(self):
        with TemporaryDirectory() as d:
            directory=Path(d);self.fixture(directory)
            with (directory/'matrix.json').open('a') as f:f.write(' ')
            with self.assertRaises(VisualError):review._verify_parent(directory)

    def test_nonwall_failure_cannot_be_reused_as_success(self):
        with TemporaryDirectory() as d:
            directory=Path(d);result,receipt=self.fixture(directory)
            result['failures'][0]['record_id']='plain-1'
            write_json(directory/'result.json',result);receipt['result_sha256']=digest(result)
            write_json(directory/'evidence-receipt.json',receipt)
            with self.assertRaises(VisualError):review._verify_parent(directory)

    def test_distinct_new_geometry_cannot_share_an_old_tile(self):
        values=[{'image_sha256':'pixel','raw_proposal_sha256':'face-a'},
            {'image_sha256':'pixel','raw_proposal_sha256':'face-b'}]
        with self.assertRaises(VisualError):review._verify_tile_groups({0:values})

    def test_same_pixel_geometry_can_reuse_tile(self):
        value={'image_sha256':'pixel','raw_proposal_sha256':'same-face'}
        review._verify_tile_groups({0:[value,dict(value)]})

    def test_unresolved_raw_episode_produces_failure_even_if_counts_match(self):
        errors=review._unsupported_failures('actual-case', {'unsupported':[{'reason':'raw_episode_binding_unresolved'}]})
        self.assertEqual(errors,[{'code':'raw_input_episode_binding_unresolved','record_id':'actual-case','reason':'raw_episode_binding_unresolved'}])


if __name__=='__main__':unittest.main()
