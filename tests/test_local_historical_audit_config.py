"""Pinned historical evidence stays readable, never executable via this API."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import launcher, review
from kiroclops.local_engine.session import create_session
from kiroclops.visual.schema import VisualError, file_sha256, write_json


class HistoricalAuditConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='historical café ')
        base = Path(self.temp.name)
        # load_config first loads the bound LocalSession and its frozen execution
        # policy, so the historical config belongs to a real legacy (v1) session.
        baseline = base/'original'
        (baseline/'src').mkdir(parents=True)
        (baseline/'src/panel-motion.js').write_text('export const speed = 1;', encoding='utf-8')
        reference = base/'reference'
        reference.mkdir()
        (reference/'clip.txt').write_text('reference pixels', encoding='utf-8')
        self.root = base/'session'
        create_session(self.root, 'web_panel', baseline, reference, 'Original task only.',
                       experiment={'capture_lock': str(base/'capture.lock')})
        self.target = self.root/'target'
        self.controls = self.target/'.kiro'
        self.controls.mkdir(parents=True)
        (self.controls/'prompt.md').write_text('Original task only.', encoding='utf-8')
        self.config = {'session_dir': str(self.root), 'target_dir': str(self.target),
            'model': launcher.MODEL, 'effort': launcher.EFFORT, 'approval_mode': launcher.APPROVAL,
            'launcher_sha256': review._NATIVE_OUTPUT_POLICY_OPUS55_MAX_V1['launcher_sha256'],
            'generated_hashes': {'prompt.md': file_sha256(self.controls/'prompt.md')}}
        write_json(self.root/'kiro/config.json', self.config)

    def tearDown(self):
        self.temp.cleanup()

    def audit_config(self):
        with patch.object(launcher, 'load_config', side_effect=VisualError('launcher_changed', 'New runtime')):
            return review._load_native_config_for_audit(self.root)

    def test_pinned_old_config_reads_without_changing_any_bytes(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(self.config, self.audit_config())
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_pinned_cloud_launcher_remains_auditable_after_startup_update(self):
        self.config['launcher_sha256'] = review._NATIVE_OUTPUT_POLICY['launcher_sha256']
        write_json(self.root/'kiro/config.json', self.config)
        self.assertEqual(self.config, self.audit_config())

    def test_historical_cloud_endpoint_binding_must_not_change(self):
        endpoint = self.root/'service.json'
        write_json(endpoint, {'service':'retired'})
        self.config.update(launcher_sha256=review._NATIVE_OUTPUT_POLICY['launcher_sha256'],
            analysis_execution={'backend':'remote','config_path':str(endpoint),'config_sha256':file_sha256(endpoint)})
        write_json(self.root/'kiro/config.json',self.config)
        write_json(endpoint,{'service':'substituted'})
        with self.assertRaises(VisualError) as caught:
            self.audit_config()
        self.assertEqual('config_changed',caught.exception.code)

    def test_current_execution_still_rejects_historical_launcher(self):
        with patch.object(launcher, 'file_sha256', return_value='new-runtime'):
            with self.assertRaises(VisualError) as caught:
                launcher.load_config(self.root)
        self.assertEqual('launcher_changed', caught.exception.code)

    def test_unknown_launcher_is_not_a_historical_audit_exemption(self):
        self.config['launcher_sha256'] = 'unreviewed-source'
        write_json(self.root/'kiro/config.json', self.config)
        with self.assertRaises(VisualError) as caught:
            self.audit_config()
        self.assertEqual('launcher_changed', caught.exception.code)

    def test_changed_control_file_remains_rejected(self):
        (self.controls/'prompt.md').write_text('A different task.', encoding='utf-8')
        with self.assertRaises(VisualError) as caught:
            self.audit_config()
        self.assertEqual('config_changed', caught.exception.code)

    def test_model_and_session_identity_cannot_change(self):
        for key, replacement, expected in [('model', 'other-model', 'model_changed'),
                ('effort', 'low', 'model_changed'),
                ('session_dir', str(self.root/'other'), 'config_changed')]:
            with self.subTest(key=key):
                changed = {**self.config, key: replacement}
                write_json(self.root/'kiro/config.json', changed)
                with self.assertRaises(VisualError) as caught:
                    self.audit_config()
                self.assertEqual(expected, caught.exception.code)

    def test_other_validation_errors_are_not_bypassed(self):
        with patch.object(launcher, 'load_config', side_effect=VisualError('config_changed', 'Controls changed')):
            with self.assertRaises(VisualError) as caught:
                review._load_native_config_for_audit(self.root)
        self.assertEqual('config_changed', caught.exception.code)

    def test_pinned_sol_audit_uses_its_original_model_without_enabling_execution(self):
        self.config.update(model='gpt-5.6-sol',
            launcher_sha256=review._NATIVE_OUTPUT_POLICY_SOL_MAX_V4['launcher_sha256'])
        write_json(self.root/'kiro/config.json', self.config)
        before = (self.root/'kiro/config.json').read_bytes()
        self.assertEqual(self.config, review._load_native_config_for_audit(self.root))
        self.assertEqual(before, (self.root/'kiro/config.json').read_bytes())
        with self.assertRaises(VisualError) as caught:
            launcher.load_config(self.root)
        self.assertEqual('model_changed', caught.exception.code)

    def test_historical_launcher_cannot_be_relabelled_as_another_model(self):
        self.config.update(model='gpt-5.6-sol')  # Still the pinned Opus launcher.
        write_json(self.root/'kiro/config.json', self.config)
        with self.assertRaises(VisualError) as caught:
            review._load_native_config_for_audit(self.root)
        self.assertEqual('model_changed', caught.exception.code)

    def test_sol_model_error_does_not_hide_changed_controls(self):
        self.config.update(model='gpt-5.6-sol',
            launcher_sha256=review._NATIVE_OUTPUT_POLICY_SOL_MAX_V4['launcher_sha256'])
        write_json(self.root/'kiro/config.json', self.config)
        (self.controls/'prompt.md').write_text('Mutated historical prompt', encoding='utf-8')
        with self.assertRaises(VisualError) as caught:
            review._load_native_config_for_audit(self.root)
        self.assertEqual('config_changed', caught.exception.code)


if __name__ == '__main__':
    unittest.main()
