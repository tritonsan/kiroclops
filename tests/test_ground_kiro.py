"""Ground-motion has its own instructions and cannot inherit jump controls."""
import json
from pathlib import Path
import tempfile
import unittest

from kiroclops.integration import kiro


class GroundKiroTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.session = Path(self.temp.name) / 'session naïve space'
        target = self.session / 'target'
        target.mkdir(parents=True)
        (target / 'project.godot').write_text('[application]\n', encoding='utf-8')
        (self.session / 'session.json').write_text(json.dumps({
            'experiment_id': 'ground-motion-v1', 'status': 'new'}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_and_power_bind_the_same_ground_skill_without_reference_constants(self):
        config = kiro.configure_kiro(self.session)
        agent = json.loads(Path(config['agent_path']).read_text(encoding='utf-8'))
        skill = (Path(config['power_dir']) / 'skills/motion-calibration/SKILL.md').read_text(encoding='utf-8')
        self.assertEqual(agent['prompt'], skill)
        self.assertIn('grounded horizontal', skill)
        self.assertIn('6 and 48', skill)
        self.assertNotIn('3-tick and 24-tick', skill)
        self.assertIn('ground-motion-v1', Path(config['prompt_path']).read_text(encoding='utf-8'))
        self.assertEqual(kiro._load_config(self.session, 'tools')['experiment_id'], 'ground-motion-v1')
        ide = kiro.configure_kiro(self.session, surface='ide')
        self.assertEqual((Path(ide['power_dir']) / 'skills/motion-calibration/SKILL.md').read_text(encoding='utf-8'), skill)

    def test_experiment_change_cannot_reuse_generated_controls(self):
        kiro.configure_kiro(self.session)
        (self.session / 'session.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(kiro.KiroError, 'another experiment'):
            kiro._load_config(self.session, 'tools')

    def test_baseline_arm_and_unknown_experiment_are_rejected(self):
        with self.assertRaises(kiro.KiroError):
            kiro.configure_kiro(self.session, mode='baseline')
        (self.session / 'session.json').write_text('{"experiment_id":"unknown"}', encoding='utf-8')
        with self.assertRaises(kiro.KiroError):
            kiro.configure_kiro(self.session)
