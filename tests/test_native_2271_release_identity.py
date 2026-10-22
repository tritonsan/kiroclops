"""Exact 2.27.1 release identity; synthetic trees, no subprocess, application or model."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kiroclops.local_engine import review
from kiroclops.visual.schema import VisualError, write_json
from scripts import phase3d_v2_native_release_identity as identity
from tests.frozen_native_chain import REASON, V6_DIVERGENCE

PRODUCER_BYTES = b'/* unit producer */ export const acp = 1;\n'
LICENSE = {'name': '@types/node', 'version': '26.6.3', 'license': 'MIT', 'typesPublisherContentHash': 'a' * 64}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, indent=2).encode('utf-8'))


def _call(name, *args, **kwargs):
    """Call the reader attribute as currently bound (the resolver may have replaced it)."""
    return getattr(review, name)(*args, **kwargs)


class _Trees(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='release identity café naïve ')
        self.addCleanup(temp.cleanup)
        self.kas = Path(temp.name) / 'kas'
        self.base_dir = self.kas / identity.BASE_DIRECTORY
        self.release_dir = self.kas / identity.RELEASE_DIRECTORY
        for directory, version, publisher in ((self.base_dir, '26.6.3', 'a'), (self.release_dir, '26.6.4', 'b')):
            _write(directory / identity.PRODUCER, PRODUCER_BYTES)
            _write(directory / identity.PACKAGE, {'name': '@kiro/agent', 'version': identity.KAS_VERSION})
            _write(directory / 'node_modules/other/index.js', b'module.exports = 2;\n')
            _write(directory / 'node_modules/native/addon.node', bytes(range(256)))
            _write(directory / identity.LICENSE_MANIFEST,
                   {**LICENSE, 'version': version, 'typesPublisherContentHash': publisher * 64})
        self.source = str(self.base_dir / identity.PRODUCER)
        self.v3 = {'source': self.source}
        stack = [patch.object(identity, '_pinned_reader', side_effect=lambda *_: deepcopy(self.v3)),
                 patch.object(identity.base, 'KAS_SOURCE_SHA256', hashlib.sha256(PRODUCER_BYTES).hexdigest())]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)

    def build(self):
        return identity.build_proof(self.kas.parent, kas_root=self.kas)


class BuildProofTests(_Trees):
    def test_identical_trees_with_inspected_license_change_are_proved(self):
        proof, manifest = self.build()
        self.assertEqual((5, 4), (proof['files'], proof['identical_files']))
        self.assertEqual([identity.LICENSE_MANIFEST], [d['path'] for d in proof['differences']])
        self.assertEqual(['typesPublisherContentHash', 'version'], proof['differences'][0]['changed_keys'])
        self.assertEqual(manifest['base_files'][identity.PRODUCER], manifest['release_files'][identity.PRODUCER])
        self.assertEqual((proof, manifest), self.build())
        for field in ('cli_host_binary_compared', 'native_canary', 'model_started', 'tool_authorization_granted',
                      'behavior_acceptance_granted', 'historical_receipts_rewritten', 'canonical_reader_modified'):
            self.assertFalse(proof[field])

    def test_any_producer_runtime_or_file_set_change_is_rejected(self):
        changes = [lambda: _write(self.release_dir / identity.PRODUCER, PRODUCER_BYTES + b'//changed\n'),
                   lambda: _write(self.release_dir / 'node_modules/other/index.js', b'module.exports = 3;\n'),
                   lambda: _write(self.release_dir / 'node_modules/native/addon.node', bytes(range(255))),
                   lambda: _write(self.release_dir / 'node_modules/extra.js', b'1;\n'),
                   lambda: (self.release_dir / 'node_modules/other/index.js').unlink(),
                   lambda: _write(self.base_dir / identity.PRODUCER, b'other'),
                   lambda: _write(self.release_dir / identity.PACKAGE, {'version': '0.66.23'})]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.setUp()
                change()
                with self.assertRaises(VisualError):
                    self.build()

    def test_license_manifest_may_change_only_its_version_label(self):
        variants = [{**LICENSE, 'version': '26.6.4', 'typesPublisherContentHash': 'b' * 64, 'license': 'GPL'},
                    {**LICENSE, 'version': '26.7.0', 'typesPublisherContentHash': 'b' * 64},
                    {**LICENSE, 'version': '26.6.4', 'typesPublisherContentHash': 'b' * 64, 'main': 'x.js'},
                    {**LICENSE, 'name': '@types/other', 'version': '26.6.4', 'typesPublisherContentHash': 'b' * 64}]
        for value in variants:
            with self.subTest(value=value):
                self.setUp()
                _write(self.release_dir / identity.LICENSE_MANIFEST, value)
                with self.assertRaises(VisualError):
                    self.build()

    def test_a_file_naming_the_preserved_manifests_is_rejected(self):
        _write(self.release_dir / 'node_modules/other/index.js', b'require("./_preserved-licenses/x");\n')
        _write(self.base_dir / 'node_modules/other/index.js', b'require("./_preserved-licenses/x");\n')
        with self.assertRaises(VisualError):
            self.build()

    def test_pinned_producer_path_must_be_the_inspected_installation(self):
        self.v3 = {'source': str(self.kas / 'other-version' / identity.PRODUCER)}
        with self.assertRaises(VisualError):
            self.build()

    def test_saved_proof_must_be_reproduced_exactly(self):
        root = self.kas.parent
        proof, manifest = self.build()
        write_json(root / identity.PROOF, proof)
        write_json(root / identity.MANIFEST, manifest)
        self.assertEqual(proof, identity.verify_saved(root, kas_root=self.kas))
        write_json(root / identity.PROOF, {**proof, 'files': 6})
        with self.assertRaises(VisualError):
            identity.verify_saved(root, kas_root=self.kas)
        write_json(root / identity.PROOF, proof)
        _write(self.release_dir / 'node_modules/other/index.js', b'module.exports = 4;\n')
        with self.assertRaises(VisualError):
            identity.verify_saved(root, kas_root=self.kas)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.context = {**identity.RELEASE_CONTEXT, 'target_dir': 'C:/unit', 'hook_id': 'C:/unit#hook-0'}

    def test_only_the_exact_recorded_triple_is_read_with_base_rules(self):
        mapped = identity.map_context(self.context)
        self.assertEqual({**self.context, 'cli_version': identity.BASE_RELEASE}, mapped)
        for change in ({'cli_version': 'kiro-cli-chat 2.27.2'}, {'cli_version': identity.BASE_RELEASE},
                       {'cli_version': 'kiro-cli-chat 2.27.10'}, {'kas_version': '0.66.23'},
                       {'launcher_sha256': '0' * 64}, {'cli_version': None}):
            with self.subTest(change=change):
                self.assertIsNone(identity.map_context({**self.context, **change}))
        self.assertIsNone(identity.map_context({k: v for k, v in self.context.items() if k != 'kas_version'}))
        self.assertIsNone(identity.map_context(None))


class _Patched(unittest.TestCase):
    def setUp(self):
        self.proof = {'kind': 'unit-proof'}
        self.verified = patch.object(identity, 'verify_saved', return_value=self.proof)
        self.verified.start()
        self.addCleanup(self.verified.stop)
        self.seen = []
        self.builtin = lambda context: self.seen.append(('builtin', context)) or {'id': 'unit-policy'}
        self.outcomes = lambda *a, runtime_context=None, **k: self.seen.append(('outcomes', a, runtime_context, k)) or {'status': 'pass'}
        for name, value in (('_builtin_policy_227', self.builtin), ('audit_native_tool_outcomes', self.outcomes)):
            item = patch.object(review, name, value)
            item.start()
            self.addCleanup(item.stop)
        self.context = {**identity.RELEASE_CONTEXT, 'hook_id': 'unit#hook-0'}


class ResolverTests(_Patched):
    def test_release_context_is_mapped_and_originals_are_restored(self):
        with identity.release_identity_policy() as applied:
            self.assertIsNot(review._builtin_policy_227, self.builtin)
            self.assertEqual({'id': 'unit-policy'}, _call('_builtin_policy_227', dict(identity.RELEASE_CONTEXT)))
            _call('audit_native_tool_outcomes', [1], [2], session_id='s', runtime_context=self.context)
        self.assertIs(review._builtin_policy_227, self.builtin)
        self.assertIs(review.audit_native_tool_outcomes, self.outcomes)
        self.assertEqual(identity.BASE_RELEASE, self.seen[0][1]['cli_version'])
        self.assertEqual(([1], [2]), self.seen[1][1])
        self.assertEqual({'session_id': 's'}, self.seen[1][3])
        self.assertEqual({**self.context, 'cli_version': identity.BASE_RELEASE}, self.seen[1][2])
        self.assertEqual(['_builtin_policy_227', 'audit_native_tool_outcomes'], [a['entry_point'] for a in applied])

    def test_other_contexts_pass_through_unchanged(self):
        base = {**identity.BASE_CONTEXT, 'hook_id': 'x'}
        with identity.release_identity_policy() as applied:
            _call('_builtin_policy_227', dict(base))
            _call('audit_native_tool_outcomes', runtime_context=dict(base))
            _call('audit_native_tool_outcomes')
        self.assertEqual([], applied)
        self.assertEqual(base, self.seen[0][1])
        self.assertEqual(base, self.seen[1][2])
        self.assertIsNone(self.seen[2][2])

    def test_error_inside_restores_and_propagates(self):
        with self.assertRaises(RuntimeError):
            with identity.release_identity_policy():
                raise RuntimeError('unit')
        self.assertIs(review._builtin_policy_227, self.builtin)
        self.assertIs(review.audit_native_tool_outcomes, self.outcomes)

    def test_identity_changed_during_body_is_rejected(self):
        with patch.object(identity, 'verify_saved', side_effect=[self.proof, {'kind': 'changed'}]):
            with self.assertRaises(VisualError):
                with identity.release_identity_policy():
                    pass
        self.assertIs(review._builtin_policy_227, self.builtin)

    def test_unproven_identity_never_patches(self):
        with patch.object(identity, 'verify_saved', side_effect=VisualError('release_identity_unproven', 'x')):
            with self.assertRaises(VisualError):
                with identity.release_identity_policy():
                    self.fail('body must not run')
        self.assertIs(review._builtin_policy_227, self.builtin)


class AuditWrapperTests(_Patched):
    def session(self, cli_version):
        temp = TemporaryDirectory(prefix='release identity session ')
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        write_json(directory / 'kiro/launch.json', {'cli_version': cli_version, 'kas_version': identity.KAS_VERSION})
        write_json(directory / 'kiro/config.json', {'launcher_sha256': identity.base.ACTIVE_LAUNCHER_SHA256})
        for name in ('PROOF', 'MANIFEST'):
            write_json(directory / 'root' / getattr(identity, name), {'unit': name})
        return directory

    def canonical(self, reach=('_builtin_policy_227', 'audit_native_tool_outcomes')):
        def audit(directory):
            context = identity._launch_context(directory)
            if '_builtin_policy_227' in reach:
                _call('_builtin_policy_227', dict(context))
            if 'audit_native_tool_outcomes' in reach:
                _call('audit_native_tool_outcomes', [], [], runtime_context={**context, 'hook_id': 'h'})
            return {'status': 'pass', 'evidence_hashes': {}, 'tool_outcomes': {'calls': []}}
        return patch.object(review, 'audit_native_run', side_effect=audit)

    def test_exact_release_run_is_replayed_through_each_entry_point_once(self):
        directory = self.session(identity.RELEASE)
        with self.canonical():
            result = identity.audit_native_run(directory, root=directory / 'root')
        self.assertEqual('ReleaseIdentityNativeReplay', result['kind'])
        self.assertEqual(identity.BASE_RELEASE, result['policy_runtime']['cli_version'])
        self.assertEqual(identity.RELEASE, result['recorded_runtime']['cli_version'])
        self.assertFalse(result['behavior_acceptance_granted'])
        self.assertTrue(result['post_hoc_for_recorded_runs'])

    def test_other_release_or_partial_replay_is_refused(self):
        with self.assertRaises(VisualError) as caught:
            identity.audit_native_run(self.session(identity.BASE_RELEASE))
        self.assertEqual('release_identity_not_applicable', caught.exception.code)
        directory = self.session(identity.RELEASE)
        for reach in (('_builtin_policy_227',), ('audit_native_tool_outcomes',), ()):
            with self.subTest(reach=reach), self.canonical(reach), self.assertRaises(VisualError):
                identity.audit_native_run(directory, root=directory / 'root')


@unittest.skipUnless((identity.ROOT / identity.PROOF).is_file(), 'Saved release identity is not built yet.')
@unittest.skipIf(V6_DIVERGENCE, REASON)
class InstalledReleaseTests(unittest.TestCase):
    def test_saved_proof_matches_the_installed_trees(self):
        proof = identity.verify_saved()
        self.assertEqual((889, 888), (proof['files'], proof['identical_files']))

    def test_canonical_reader_alone_still_refuses_the_release(self):
        self.assertIsNone(review._builtin_policy_227(dict(identity.RELEASE_CONTEXT)))
        self.assertIsNotNone(review._builtin_policy_227(dict(identity.BASE_CONTEXT)))
        with identity.release_identity_policy() as applied:
            policy = _call('_builtin_policy_227', dict(identity.RELEASE_CONTEXT))
        self.assertEqual('kas-0.66.22-cli-2.27.0-observed-outcomes-v3', (policy or {}).get('id'))
        self.assertEqual(1, len(applied))
        self.assertIsNone(review._builtin_policy_227(dict(identity.RELEASE_CONTEXT)))


if __name__ == '__main__':
    unittest.main()
