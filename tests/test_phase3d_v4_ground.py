"""V4 ground preparation: recipes and the scoped parent/recipe binding."""
import unittest

from kiroclops.visual.schema import VisualError
from scripts import phase3d_v2_prepare as prepare
from scripts import phase3d_v4_ground as v4


class V4GroundTests(unittest.TestCase):
    def test_recipes_cover_v4_ground_and_the_unchanged_protected_jumps(self):
        rows = v4.recipes()
        ground = [r for r in rows if r['stage'] == 'ground']
        jump = [r for r in rows if r['stage'] == 'jump']
        self.assertEqual(60, len(ground))
        self.assertEqual(30, len(jump))
        self.assertTrue(all(r['purpose'] == 'primary' and r['scenario']['suite_registry'] == 'v4' for r in ground))
        self.assertTrue(all(r['purpose'] == 'preservation' and 'suite_registry' not in r['scenario'] for r in jump))
        self.assertEqual({'r36', 'l42', 'reverse30'},
                         {r['scenario']['id'] for r in ground if r['role'] == 'validation'})
        self.assertEqual(len(rows), len({(r['case_id'], r['stage']) for r in rows}))

    def test_preparation_binding_is_scoped_and_always_restored(self):
        before = (prepare.sources, prepare.recipes)
        with self.assertRaises(RuntimeError):
            with v4._v4_preparation({}, 'unused', {'source_tree_sha256': 'x'}, {}):
                self.assertEqual(v4.recipes(), prepare.recipes('ground'))
                with self.assertRaises(VisualError):
                    prepare.recipes('panel')
                raise RuntimeError('boom')
        self.assertEqual(before, (prepare.sources, prepare.recipes))


if __name__ == '__main__':
    unittest.main()
