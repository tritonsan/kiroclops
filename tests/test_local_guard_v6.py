"""V6 guard tightening: parent traversal, session control files, unknown path keys."""
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine import launcher
from kiroclops.local_engine.session import create_session


class GuardV6Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        source = root/'baseline'
        (source/'src').mkdir(parents=True)
        (source/'src/panel-motion.js').write_text('// fixture\n', encoding='utf8')
        reference = root/'outside.txt'
        reference.write_text('DENIED\n', encoding='utf8')
        self.directory = root/'guard v6 session'
        self.state = create_session(self.directory, 'web_panel', source, reference, 'Guard V6 unit test; no execution', mode='tools')
        launcher.configure(self.directory)
        self.target = Path(self.state['target'])

    def shell(self, command):
        return launcher.guard(self.directory, {'tool_name': 'execute_pwsh', 'tool_input': {'command': command}})['allow']

    def test_parent_traversal_tokens_are_denied(self):
        for command in ('cd ..; Get-Content baseline\\x.js', 'cd .. && type jobs\\a\\job.json', 'Get-ChildItem ..',
                        'python x.py ..\\..\\secret', 'cat ../../x'):
            with self.subTest(command=command):
                self.assertFalse(self.shell(command))
        for command in ('Get-ChildItem src', 'git diff src/panel-motion.js', 'echo done...', 'python .kiroclops-scratch/a.py --level 2'):
            with self.subTest(command=command):
                self.assertTrue(self.shell(command))

    def test_session_control_files_and_folder_are_denied(self):
        self.assertFalse(self.shell(f'Get-Content "{self.directory}\\kiro\\tool-audit.jsonl"'))
        self.assertFalse(self.shell(f'type "{self.directory}\\session.json"'))
        self.assertFalse(self.shell('Get-Content binding.json'))
        self.assertFalse(self.shell(f'Copy-Item "{self.directory}\\baseline\\src\\panel-motion.js" src'))

    def test_read_tool_with_unknown_path_key_does_not_fall_open(self):
        denied = launcher.guard(self.directory, {'tool_name': 'read_image', 'tool_input': {'image_path': 'C:/Windows/notepad.png'}})
        self.assertFalse(denied['allow'])
        self.assertEqual(denied['reason'], 'Unrecognised path argument')
        allowed = launcher.guard(self.directory, {'tool_name': 'read_image', 'tool_input': {'path': str(self.target/'src/panel-motion.js')}})
        self.assertTrue(allowed['allow'])
        plain = launcher.guard(self.directory, {'tool_name': 'grep', 'tool_input': {'query': 'speed'}})
        self.assertTrue(plain['allow'])

    def test_helpers(self):
        self.assertTrue(launcher._parent_traversal('cd ..'))
        self.assertTrue(launcher._parent_traversal('type ../a'))
        self.assertTrue(launcher._parent_traversal('x=..;'))
        self.assertFalse(launcher._parent_traversal('echo done...'))
        self.assertFalse(launcher._parent_traversal('git log ..main'))
        self.assertEqual(launcher._unrecognised_path_arguments({'a': {'b': ['x/y', 'plain']}, 'c': 'D:\\z'}), ['x/y', 'D:\\z'])


if __name__ == '__main__':
    unittest.main()
