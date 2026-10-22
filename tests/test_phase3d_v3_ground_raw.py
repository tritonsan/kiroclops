"""V3 ground raw wrapper: only the parent-chain reader and version names change."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine.media_suite import (GROUND_FLOOR_PROFILE, GROUND_FLOOR_RULE, GROUND_SPEED_PROFILE,
                                               GROUND_SPEED_RULE)
from kiroclops.visual.schema import VisualError, digest, file_sha256, write_json
from scripts import phase3d_v2_ground_final_raw as ground
from scripts import phase3d_v2_ground_normal_raw as normal
from scripts import phase3d_v3_ground_parent as bridge
from scripts import phase3d_v3_ground_raw as v3
from tests.evidence import requires_evidence


def chain_fixture(root, *, kind=v3.CHAIN_KIND, profile=GROUND_FLOOR_PROFILE, extra_consumed=None, snapshot_bound=(),
                  tree_sha256=None, chain_profile=GROUND_FLOOR_PROFILE, chain_rule=GROUND_FLOOR_RULE):
    root = Path(root)
    session, suite = root / 's' / 'v3g01', root / 'p' / 'v3g01' / 'suite'
    parent_hashes, original_hashes = {'a.gd': '1' * 64}, {'a.gd': '2' * 64}
    write_json(suite / 'suite.json', {'acceptance_profile': profile})
    write_json(suite / 'suite-binding.json', {'suite': 'binding'})
    write_json(session / 'session.json', {'session_id': 'sid'})
    initial = root / 'p' / 'v3g01' / 'ground-parent-initial-session.json'
    write_json(initial, {'session_id': 'sid', 'started_at': None, 'candidates': [],
                         'experiment': {'suite_sha256': 'f' * 64}})
    producer = root / 'p' / 'v3g01' / 'ground-parent-producer.py'
    producer.write_text('# producer\n', encoding='utf-8')
    consumed_file = root / 'consumed.json'
    write_json(consumed_file, {'x': 1})
    parent = {'parent_stage': 'jump', 'stage': 'ground', 'parent_source_sha256': digest(parent_hashes),
              'original_source_sha256': digest(original_hashes),
              'parent_acceptance_mode': 'append_only_mixed_epoch_selected_code_revalidation'}
    consumed = {str(consumed_file): file_sha256(consumed_file), **(extra_consumed or {})}
    chain = {'kind': kind, 'schema_version': 1, 'status': 'pass', 'parent': parent, 'parent_sha256': digest(parent),
             'session': str(session), 'session_id': 'sid', 'suite': str(suite), 'suite_sha256': 'f' * 64,
             'suite_file_sha256': file_sha256(suite / 'suite.json'),
             'suite_binding_sha256': file_sha256(suite / 'suite-binding.json'),
             'acceptance_profile': chain_profile, 'acceptance_rule': deepcopy(chain_rule),
             'initial_session_archive': str(initial), 'initial_session_archive_sha256': file_sha256(initial),
             'producer_archive': str(producer), 'producer_sha256': file_sha256(producer),
             'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
             'snapshot_bound_files': sorted(snapshot_bound),
             'v2_snapshot': {'receipt_sha256': bridge.SNAPSHOT_RECEIPT_SHA256, 'tree_sha256': tree_sha256},
             'canonical_session_promoted': False, 'historical_session_rewritten': False,
             'ground_behavior_acceptance': False, 'model_started': False}
    path = root / 'p' / 'v3g01' / 'ground-parent-chain.json'
    write_json(path, chain)
    context = {'directory': str(session), 'suite': str(suite), 'suite_sha256': 'f' * 64,
               'authoring_parent_source_hashes': parent_hashes, 'original_source_hashes': original_hashes}
    return path, chain, context


@requires_evidence('artifacts/phase3d-v3/v2-source-epoch-01', 'C:/kc3d/code/v2-final', 'artifacts/phase3a/tools')
class SnapshotBoundTests(unittest.TestCase):
    """A V2-bound repository file changed in V3a: valid only through the frozen V2 snapshot."""
    @classmethod
    def setUpClass(cls):
        epoch = bridge.verify_snapshot()
        cls.tree = epoch['source_tree_sha256']
        cls.name = str((bridge.ROOT / 'kiroclops/local_engine/media_suite.py').resolve())
        cls.v2_sha = epoch['source_files']['kiroclops/local_engine/media_suite.py']
        cls.live_sha = file_sha256(cls.name)
        assert cls.v2_sha != cls.live_sha

    def check(self, sha, bound):
        with tempfile.TemporaryDirectory() as root:
            path, _, context = chain_fixture(root, extra_consumed={self.name: sha},
                                             snapshot_bound=(self.name,) if bound else (), tree_sha256=self.tree)
            return v3._parent_chain(path, file_sha256(path), context)

    def test_v2_bytes_pass_through_the_snapshot(self):
        self.assertEqual(v3.CHAIN_KIND, self.check(self.v2_sha, True)['chain_kind'])

    def test_live_v3a_bytes_cannot_claim_the_snapshot(self):
        with self.assertRaises(VisualError):
            self.check(self.live_sha, True)

    def test_v2_bytes_without_the_snapshot_mark_fail_in_place(self):
        with self.assertRaises(VisualError):
            self.check(self.v2_sha, False)

    def test_snapshot_mark_needs_the_pinned_tree(self):
        with tempfile.TemporaryDirectory() as root:
            path, _, context = chain_fixture(root, extra_consumed={self.name: self.v2_sha},
                                             snapshot_bound=(self.name,), tree_sha256='0' * 64)
            with self.assertRaises(VisualError):
                v3._parent_chain(path, file_sha256(path), context)


class ConsumerInputTests(unittest.TestCase):
    @requires_evidence('artifacts/phase3d-v3/v2-source-epoch-01', 'C:/kc3d/code/v2-final', 'artifacts/phase3a/tools')
    def test_snapshot_bound_chain_inputs_resolve_to_snapshot_copies(self):
        epoch = bridge.verify_snapshot()
        name = str((bridge.ROOT / 'kiroclops/local_engine/media_suite.py').resolve())
        v2_sha = epoch['source_files']['kiroclops/local_engine/media_suite.py']
        chain = {'consumed_files': {name: v2_sha, 'C:/elsewhere/x.json': '1' * 64}, 'snapshot_bound_files': [name]}
        mapped = v3._snapshot_chain_inputs(chain)
        self.assertEqual({str(bridge.SNAPSHOT / 'kiroclops/local_engine/media_suite.py'): v2_sha,
                          'C:/elsewhere/x.json': '1' * 64}, mapped)
        with self.assertRaises(VisualError):
            v3._snapshot_chain_inputs(dict(chain, consumed_files={name: file_sha256(name)}))
        self.assertEqual(chain['consumed_files'], v3._snapshot_chain_inputs({'consumed_files': chain['consumed_files']}))

    def test_v4_controller_uses_the_mirrored_anchor_and_restores(self):
        from kiroclops.local_engine import suite_generic_raw_review as generic
        from scripts import prepare_game_raw_review_aid_v4 as anchor_v4
        before = (generic.game_anchor, ground._chain_inputs, dict(ground.MATRIX))
        with v3._v3('V4GroundParentChainReceipt'):
            self.assertIs(anchor_v4.anchor, generic.game_anchor)
            self.assertIs(v3._snapshot_chain_inputs, ground._chain_inputs)
            self.assertEqual({'cases': 90, 'records': 368}, ground.MATRIX)
        with v3._v3():
            self.assertIs(before[0], generic.game_anchor)
        self.assertEqual(before, (generic.game_anchor, ground._chain_inputs, dict(ground.MATRIX)))


class ParentChainTests(unittest.TestCase):
    def test_v4_chain_is_accepted_only_with_the_v4_rule(self):
        v4 = dict(kind='V4GroundParentChainReceipt', profile=GROUND_SPEED_PROFILE, chain_profile=GROUND_SPEED_PROFILE,
                  chain_rule=GROUND_SPEED_RULE)
        with tempfile.TemporaryDirectory() as root:
            path, _, context = chain_fixture(root, **v4)
            value = v3._parent_chain(path, file_sha256(path), context)
            self.assertEqual(('V4GroundParentChainReceipt', GROUND_SPEED_PROFILE), (value['chain_kind'], value['acceptance_profile']))
        for wrong in (dict(v4, chain_rule=GROUND_FLOOR_RULE), dict(v4, profile=GROUND_FLOOR_PROFILE),
                      dict(v4, kind=v3.CHAIN_KIND)):
            with self.subTest(wrong=sorted(wrong.items())[:1]), tempfile.TemporaryDirectory() as root:
                path, _, context = chain_fixture(root, **wrong)
                with self.assertRaises(VisualError):
                    v3._parent_chain(path, file_sha256(path), context)

    def test_v3_chain_is_accepted_with_its_floor_rule(self):
        with tempfile.TemporaryDirectory() as root:
            path, _, context = chain_fixture(root)
            value = v3._parent_chain(path, file_sha256(path), context)
            self.assertEqual(v3.CHAIN_KIND, value['chain_kind'])
            self.assertEqual(GROUND_FLOOR_PROFILE, value['acceptance_profile'])

    def test_v2_kind_wrong_pin_rule_or_suite_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path, _, context = chain_fixture(root, kind='ProspectiveGroundParentChainReceipt')
            with self.assertRaises(VisualError):
                v3._parent_chain(path, file_sha256(path), context)
        with tempfile.TemporaryDirectory() as root:
            path, _, context = chain_fixture(root)
            with self.assertRaises(VisualError):
                v3._parent_chain(path, '0' * 64, context)
        with tempfile.TemporaryDirectory() as root:
            path, chain, context = chain_fixture(root)
            chain['acceptance_rule']['resolvability_factor'] = 1.
            write_json(path, chain)
            with self.assertRaises(VisualError):
                v3._parent_chain(path, file_sha256(path), context)
        with tempfile.TemporaryDirectory() as root:
            path, chain, context = chain_fixture(root, profile='legacy-game-v1')
            with self.assertRaises(VisualError):
                v3._parent_chain(path, file_sha256(path), context)

    def test_v2_reader_rejects_the_v3_chain(self):
        with tempfile.TemporaryDirectory() as root:
            path, _, context = chain_fixture(root)
            with self.assertRaises(VisualError):
                ground._parent_chain(path, file_sha256(path), context)

    @requires_evidence('artifacts/phase3d-v3/v2-source-epoch-01', 'C:/kc3d/code/v2-final', 'artifacts/phase3a/tools')
    def test_controller_swaps_and_always_restores(self):
        before = (ground._parent_chain, ground._extra_sources, ground.VERSION, normal.VERSION)
        with self.assertRaises(RuntimeError):
            with v3._v3():
                self.assertIs(v3._parent_chain, ground._parent_chain)
                self.assertEqual((v3.FINAL_VERSION, v3.NORMAL_VERSION), (ground.VERSION, normal.VERSION))
                self.assertIn(str(Path(v3.__file__).resolve()), ground._extra_sources())
                self.assertIn(str(Path(v3.__file__).resolve()), normal._code_sources())
                raise RuntimeError('boom')
        self.assertEqual(before, (ground._parent_chain, ground._extra_sources, ground.VERSION, normal.VERSION))
        self.assertFalse(v3._active)


if __name__ == '__main__':
    unittest.main()
