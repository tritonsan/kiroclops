"""Portable file transformations only; no engine, model, shell or UI is run."""
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_sol_playable as pack
from kiroclops.integration.storage import project_hashes, read_json, write_json, sha256


class PlayablePackagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'café ! percent% & game'
        patched = patch.object(pack, 'ROOT', self.root); patched.start(); self.addCleanup(patched.stop)
        # A process invocation by any code under test is always a failure.
        process = patch('subprocess.Popen', side_effect=AssertionError('No processes allowed'))
        process.start(); self.addCleanup(process.stop)
        self.sid, self.cid = 'cli-sol-ground-01', 'candidate-03'
        self.session = self.root / 'sessions' / self.sid
        self.baseline = self.session / 'baseline'
        self.project = self.session / 'candidates' / self.cid / 'project'
        self.original = b'config_version=5\r\n[application]\r\nrun/main_scene="res://Main.tscn"\r\n[autoload]\r\nOriginal="*res://original.gd"\r\n'
        self.put(self.baseline / 'project.godot', self.original)
        self.put(self.baseline / 'source/characters/player/player.gd', b'extends Node\r\n# parent movement\r\n')
        self.put(self.baseline / 'Main.tscn', b'[gd_scene format=3]\n[node name="Main" type="Node"]\n')
        self.put(self.baseline / 'original.gd', b'extends Node\n')
        self.put(self.baseline / 'assets/art.png', b'original pixels')
        before = project_hashes(self.baseline)
        self.put(self.baseline / 'project.godot', pack.background.autoload_bytes(self.original))
        self.put(self.baseline / 'override.cfg', pack.background.OVERRIDE_BYTES)
        self.put(self.baseline / pack.background.SUPPORT_PATH, pack.background.SUPPORT_BYTES)
        for path in self.baseline.rglob('*'):
            if path.is_file(): self.put(self.project / path.relative_to(self.baseline), path.read_bytes())
        self.put(self.project / 'source/characters/player/player.gd', b'extends Node\r\n# actual candidate movement\r\n')
        self.put(self.project / 'source/characters/player/player.gd.uid', b'uid://retained')
        self.put(self.project / '.godot/editor/private-cache', b'not copied')
        self.put(self.project / '.kiro/private-tool-config.json', b'not copied')
        self.metadata = {'candidate_id': self.cid, 'status': 'capturing', 'source_hashes': project_hashes(self.project)}
        write_json(self.project.parent / 'candidate.json', self.metadata)
        self.state = {'schema_version': 3, 'experiment_id': 'ground-motion-v1', 'mode': 'tools',
                      'session_id': self.sid, 'status': 'finalizing', 'baseline': str(self.baseline),
                      'baseline_hashes': project_hashes(self.baseline), 'candidates': [self.metadata],
                      'selected_candidate_id': self.cid}
        write_json(self.session / 'session.json', self.state)
        self.receipt = {'schema_version': 1, 'mode': pack.background.MODE,
                        'configuration_revision': pack.background.CONFIGURATION_REVISION,
                        'session_id': self.sid, 'surface': 'cli', 'installed': True, 'acceptance_credit': False,
                        'settings_source_files': pack.background.SETTINGS_FILES,
                        'changed_source_files': pack.background.SETTINGS_FILES,
                        'override_sha256': pack.background.OVERRIDE_SHA256,
                        'support_sha256': pack.background.SUPPORT_SHA256,
                        'project_settings_before_base64': base64.b64encode(self.original).decode(),
                        'before_source_hashes': before, 'after_source_hashes': self.state['baseline_hashes']}
        self.receipt_path = self.session / pack.background.RECEIPT
        write_json(self.receipt_path, self.receipt)
        self.registration_path = self.root / 'config/model-trials/sol-v1.json'
        self.registration = {'model': 'gpt-5.6-sol', 'cli_session_ids': [self.sid],
                             'starting_source_hashes': self.state['baseline_hashes'],
                             'prepared_sessions': {self.sid: {'background_receipt_sha256': sha256(self.receipt_path)}}}
        write_json(self.registration_path, self.registration)
        self.engine = self.root / '.tools/godot4/Godot_v4.5.1-stable_win64.exe'
        self.put(self.engine, b'MZ' + b'fake test executable; never run' * 40000)
        self.output = self.root / 'playables' / 'café ! % & copy'

    def put(self, path, raw):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)

    def prepare(self, **kwargs):
        return pack.prepare_playable(self.sid, self.cid, self.output, **kwargs)

    def snapshot(self):
        return {str(path.relative_to(self.session)): path.read_bytes() for path in self.session.rglob('*') if path.is_file()}

    def test_exact_three_file_unwrap_preserves_movement_sources_and_never_launches(self):
        source_snapshot = self.snapshot()
        output = self.prepare()
        self.assertEqual(source_snapshot, self.snapshot())
        copied = output / 'project'; manifest = read_json(output / 'manifest.json')
        self.assertEqual((copied / 'project.godot').read_bytes(), self.original)
        self.assertFalse((copied / 'override.cfg').exists())
        self.assertFalse((copied / pack.background.SUPPORT_PATH).exists())
        for relative, expected in self.metadata['source_hashes'].items():
            if relative not in pack.background.SETTINGS_FILES:
                self.assertEqual(sha256(copied / relative), expected)
        self.assertEqual((copied / 'source/characters/player/player.gd.uid').read_bytes(), b'uid://retained')
        self.assertFalse((copied / '.godot').exists()); self.assertFalse((copied / '.kiro').exists())
        self.assertEqual(project_hashes(copied), manifest['playable_source_hashes'])
        self.assertEqual(manifest['source_hashes_before'], manifest['source_hashes_after'])
        self.assertEqual(sha256(output / 'engine/Godot.exe'), sha256(self.engine))
        self.assertTrue(manifest['diagnostic']); self.assertFalse(manifest['acceptance_credit'])
        self.assertFalse(manifest['launcher']['invoked'])

    def test_portable_launcher_quotes_relative_paths_and_disables_delayed_expansion(self):
        raw = pack.PLAY_CMD
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
        text = raw.decode('ascii')
        self.assertIn('DisableDelayedExpansion', text)
        self.assertIn('"%~dp0engine\\Godot.exe" --path "%~dp0project"', text)
        self.assertIn('set "APPDATA=%~dp0runtime\\appdata"', text)
        self.assertNotIn('set PATH', text); self.assertNotIn('call ', text.lower())
        self.assertNotIn(str(self.output), text)

    def test_existing_output_and_protected_destinations_are_never_touched(self):
        self.output.mkdir(parents=True); sentinel=self.output/'keep'; sentinel.write_bytes(b'old playable')
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(sentinel.read_bytes(), b'old playable')
        for destination in (self.session/'new', self.root/'playables', self.root/'outside', self.output/'nested'):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                pack.prepare_playable(self.sid,self.cid,destination)

    def test_changed_candidate_background_refuses_even_when_metadata_matches_it(self):
        self.put(self.project / 'override.cfg', b'new custom settings')
        current=project_hashes(self.project); self.metadata['source_hashes']=current
        write_json(self.project.parent/'candidate.json',self.metadata)
        self.state['candidates']=[self.metadata];write_json(self.session/'session.json',self.state)
        with self.assertRaisesRegex(ValueError,'background settings changed'): self.prepare()
        self.assertFalse(self.output.exists())

    def test_wrong_receipt_original_bytes_reject_even_if_new_receipt_is_registered(self):
        self.receipt['project_settings_before_base64']=base64.b64encode(self.original+b'#changed').decode()
        write_json(self.receipt_path,self.receipt)
        self.registration['prepared_sessions'][self.sid]['background_receipt_sha256']=sha256(self.receipt_path)
        write_json(self.registration_path,self.registration)
        with self.assertRaisesRegex(ValueError,'exact three-file'): self.prepare()
        self.assertFalse(self.output.exists())

    def test_source_change_during_copy_removes_only_own_staging_and_publishes_nothing(self):
        real_copy=pack.copy_project
        def change(source,destination):
            result=real_copy(source,destination)
            self.put(self.project/'source/characters/player/player.gd',b'concurrent change')
            return result
        with patch.object(pack,'copy_project',side_effect=change), self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.output.parent.glob('*.preparing-*')))
        self.assertTrue((self.project/'source/characters/player/player.gd').exists())

    def test_unregistered_or_wrong_candidate_sources_reject_before_copy(self):
        with patch.object(pack,'copy_project') as copy:
            self.registration['model']='different-model';write_json(self.registration_path,self.registration)
            with self.assertRaises(ValueError): self.prepare()
            self.registration['model']='gpt-5.6-sol';write_json(self.registration_path,self.registration)
            self.put(self.project/'Main.tscn',b'changed')
            with self.assertRaises(ValueError): self.prepare()
            copy.assert_not_called()

    def test_console_launcher_and_non_executable_are_rejected_without_running(self):
        console=self.engine.with_name('Godot_console.exe');self.put(console,self.engine.read_bytes())
        with self.assertRaises(ValueError): self.prepare(engine=console)
        bad=self.engine.with_name('Bad.exe');self.put(bad,b'not executable'*90000)
        with self.assertRaises(ValueError): self.prepare(engine=bad)

    def test_linked_output_ancestor_is_rejected_before_writes(self):
        target=self.root/'unrelated';target.mkdir()
        link=self.root/'playables'
        try: link.symlink_to(target,target_is_directory=True)
        except OSError: self.skipTest('Native symlink creation privilege unavailable')
        with self.assertRaises(RuntimeError): self.prepare()
        self.assertEqual(list(target.iterdir()),[])

    def test_mocked_windows_reparse_output_is_rejected_without_symlink_privileges(self):
        target=self.root/'playables';target.mkdir()
        actual_lstat=Path.lstat
        def reparse(path):
            observed=actual_lstat(path)
            if path==target:
                class Linked:
                    st_file_attributes=0x400
                    def __getattr__(self, name):
                        return getattr(observed, name)
                return Linked()
            return observed
        with patch.object(Path,'lstat',reparse), self.assertRaises(RuntimeError): self.prepare()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
