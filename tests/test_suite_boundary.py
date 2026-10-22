import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from kiroclops.local_engine import suite_boundary
from kiroclops.visual.schema import VisualError, read_json


class BoundaryCaptureTests(unittest.TestCase):
    def test_canonical_sidecars_bound_separately_from_native_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);project=root/'game';project.mkdir();(project/'player.gd').write_text('source')
            (project/'player.gd.uid').write_text('canonical-sidecar')
            native=root/'native';native.mkdir();(native/'proof.json').write_text('native')
            value={'status':'pass','required':8,'passed':8,'candidate_id':'candidate-01','physics_collision_inferred':False}
            with patch('kiroclops.local_engine.suite_game._short_runtime',return_value=root),patch(
                'kiroclops.local_engine.game._visual_wall',return_value=value):
                result=suite_boundary.capture(project,root/'receipt.json','candidate-01')
            self.assertIn('player.gd.uid',result['full_source_hashes'])
            self.assertEqual(result,read_json(root/'receipt.json'))
            self.assertTrue(result['numeric_only']);self.assertEqual(result['independent_review_status'],'pending')

    def test_invalid_fixture_is_measurement_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);project=root/'game';project.mkdir();(project/'player.gd').write_text('source')
            with patch('kiroclops.local_engine.suite_game._short_runtime',return_value=root),patch(
                'kiroclops.local_engine.game._visual_wall',return_value={'status':'invalid_fixture_for_behavior'}):
                with self.assertRaises(VisualError) as error:suite_boundary.capture(project,root/'receipt.json','c1')
            self.assertEqual(error.exception.code,'insufficient_observability')


if __name__=='__main__':unittest.main()
