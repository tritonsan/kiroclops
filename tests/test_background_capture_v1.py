"""Startup configuration transactions; no engine, UI or model is started."""
from copy import deepcopy
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import background_capture_v1 as bg, ground_worker as gw
from kiroclops.integration import storage, session as session_module, experiments


class BackgroundCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'sessions/new-ground'
        for module in (bg, storage, session_module, gw):
            mocked = patch.object(module, 'ROOT', self.root)
            mocked.start(); self.addCleanup(mocked.stop)
        for name in ('baseline', 'target'):
            self.put(self.directory/name/'project.godot', 'config_version=5\n[application]\n[display]\nwindow/size/viewport_width=640\nwindow/size/viewport_height=360\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n')
            self.put(self.directory/name/'source/characters/player/player.gd', 'extends CharacterBody2D\r\n# unchanged movement bytes\r\n')
            self.put(self.directory/name/'kiroclops_ground/capture.gd', '# unchanged ground fixture\n')
            self.put(self.directory/name/'kiroclops_capture/capture.gd', '# unchanged jump fixture\n')
        hashes = storage.project_hashes(self.directory/'baseline')
        parent = {'project': 'sessions/old/project', 'source_hashes': {'original': 'hash'}}
        self.lock = {'tracking': {'parent': parent}, 'tracking_lock_sha256': 'locked-ground', 'reference_lock_sha256': 'locked-reference'}
        self.state = {'schema_version': 3, 'experiment_id': 'ground-motion-v1',
            'experiment_spec': experiments.get_experiment('ground-motion-v1').record(), 'session_id': 'new-ground',
            'mode': 'tools', 'status': 'new', 'started_at': None, 'deadline_at': None,
            'target': str(self.directory/'target'), 'baseline': str(self.directory/'baseline'),
            'baseline_hashes': hashes, 'prepared_baseline_hashes': hashes, 'parent': parent,
            'experiment_locks': {k:self.lock[k] for k in ('tracking_lock_sha256','reference_lock_sha256')},
            'candidates': [], 'reference': {}, 'baseline_runs': {}, 'selected_candidate_id': None,
            'active_job_id': None, 'final': None, 'budget': {'max_candidates':3,'max_seconds':1800}}
        storage.write_json(self.directory/'session.json', self.state)
        mocked = patch.object(gw, 'verify_ground_lock', return_value=self.lock)
        mocked.start(); self.addCleanup(mocked.stop)

    @staticmethod
    def put(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))

    def snapshot(self):
        return {p.relative_to(self.directory).as_posix():p.read_bytes()
                for p in self.directory.rglob('*') if p.is_file() and p.name != '.session.lock'}

    def rejected(self):
        before = self.snapshot()
        with self.assertRaises((storage.IntegrationError, OSError, ValueError)):
            bg.configure_background_session(self.directory)
        self.assertEqual(self.snapshot(), before)

    def test_new_session_updates_both_hash_fields_and_keeps_all_original_source_and_clock_bytes(self):
        before = self.snapshot()
        with patch.object(gw, 'verify_session', wraps=gw.verify_session) as verify:
            receipt_path = bg.configure_background_session(self.directory)
            self.assertGreaterEqual(verify.call_count, 4)
        updated = storage.read_json(self.directory/'session.json')
        hashes = storage.project_hashes(self.directory/'baseline')
        self.assertEqual(set(hashes)-set(self.state['baseline_hashes']), {'override.cfg', bg.SUPPORT_PATH})
        self.assertEqual(updated, {**self.state, 'baseline_hashes':hashes, 'prepared_baseline_hashes':hashes})
        for path, raw in before.items():
            if path != 'session.json' and not path.endswith('/project.godot'):
                self.assertEqual((self.directory/path).read_bytes(), raw)
        for name in ('baseline','target'):
            self.assertEqual((self.directory/name/'override.cfg').read_bytes(),bg.OVERRIDE_BYTES)
            self.assertEqual((self.directory/name/bg.SUPPORT_PATH).read_bytes(),bg.SUPPORT_BYTES)
            self.assertEqual((self.directory/name/'project.godot').read_bytes(),
                             bg.autoload_bytes(before[f'{name}/project.godot']))
        receipt = storage.read_json(receipt_path)
        self.assertEqual(receipt['before_source_hashes'],self.state['baseline_hashes'])
        self.assertEqual(receipt['after_source_hashes'],hashes)
        self.assertEqual(base64.b64decode(receipt['project_settings_before_base64']),before['baseline/project.godot'])
        self.assertEqual(receipt['changed_source_files'],bg.SETTINGS_FILES)
        self.assertTrue(receipt['ground_contract_verified_before_and_after'])
        self.assertFalse(receipt['runtime_focus_verified'])
        self.assertFalse(receipt['pixel_equivalence_verified'])
        self.assertFalse(receipt['acceptance_credit'])
        self.assertEqual(bg.verify_background_session(self.directory)['status'],'pass')
        self.rejected()  # Repeated calls cannot overwrite an opt-in receipt.

    def test_started_clock_candidates_jobs_or_result_refuse_without_writes(self):
        variants=[{'started_at':'old'}, {'deadline_at':123}, {'status':'ready'},
                  {'candidates':[{'candidate_id':'candidate-01'}]}, {'active_job_id':'job-running'},
                  {'selected_candidate_id':'candidate-01'}, {'final':{}},
                  {'reference':{'calibration':['run']}}, {'baseline_runs':{'calibration':['run']}}]
        for changes in variants:
            with self.subTest(changes=changes):
                storage.write_json(self.directory/'session.json',{**self.state,**changes})
                self.rejected()
        storage.write_json(self.directory/'session.json',self.state)
        self.put(self.directory/'jobs/orphan.json','{}')
        self.rejected()

    def test_prior_launch_override_or_ide_configuration_is_preserved(self):
        cases=[('kiro/launch.json','{}'),('baseline/override.cfg','existing'),
               ('target/override.cfg','existing'),(bg.RECEIPT,'old receipt'),
               ('target/.kiro/kiroclops-session.json','{"surface":"ide"}')]
        for name,text in cases:
            with self.subTest(name=name):
                path=self.directory/name; self.put(path,text)
                self.rejected()
                path.unlink()

    def test_lock_or_source_mismatch_is_never_repaired_silently(self):
        altered=deepcopy(self.state); altered['experiment_locks']['tracking_lock_sha256']='changed'
        storage.write_json(self.directory/'session.json',altered)
        self.rejected()
        storage.write_json(self.directory/'session.json',self.state)
        self.put(self.directory/'target/source/characters/player/player.gd','changed input code')
        self.rejected()

    def test_partial_second_override_failure_restores_original_state_and_source(self):
        before=self.snapshot(); original=bg._exclusive
        def second_fails(path, raw):
            if path==self.directory/'target/override.cfg':
                raise KeyboardInterrupt()
            return original(path,raw)
        with patch.object(bg,'_exclusive',side_effect=second_fails):
            with self.assertRaises(KeyboardInterrupt): bg.configure_background_session(self.directory)
        self.assertEqual(self.snapshot(),before)
        self.assertFalse((self.directory/'baseline'/bg.SUPPORT_PATH).parent.exists())
        self.assertFalse((self.directory/'target'/bg.SUPPORT_PATH).parent.exists())

    def test_failure_after_state_commit_rolls_back_exact_original_json(self):
        path=self.directory/'session.json'
        path.write_bytes(path.read_bytes()+b'  \n')
        before=self.snapshot(); original=gw.verify_session
        def reject_updated(session):
            if 'override.cfg' in session.read()['baseline_hashes']:
                raise storage.IntegrationError('injected_failure','post-write check failed')
            return original(session)
        with patch.object(gw,'verify_session',side_effect=reject_updated):
            self.rejected()
        self.assertEqual(self.snapshot(),before)

    def test_context_change_after_lock_is_rejected_before_config_addition(self):
        context=bg._fresh(session_module.Session(self.directory))
        other=(deepcopy(context[0]),context[1]+b' ')
        with patch.object(bg,'_fresh',side_effect=[context,other]):
            self.rejected()

    def test_verifier_allows_later_player_edits_but_rejects_changed_override_or_protected_source(self):
        bg.configure_background_session(self.directory)
        self.put(self.directory/'target/source/characters/player/player.gd','legitimate later candidate edit')
        self.assertEqual(bg.verify_background_session(self.directory)['status'],'pass')
        path=self.directory/'target/override.cfg'; raw=path.read_bytes()
        path.write_bytes(raw+b'\n')
        with self.assertRaises(storage.IntegrationError): bg.verify_background_session(self.directory)
        path.write_bytes(raw)
        self.put(self.directory/'target/kiroclops_ground/capture.gd','forbidden fixture edit')
        with self.assertRaises(storage.IntegrationError): bg.verify_background_session(self.directory)

    def disposable(self):
        root=self.root/'artifacts/preflight'
        project=root/'copy'
        self.put(project/'project.godot','config_version=5\n[application]\n[display]\nwindow/size/viewport_width=640\n')
        self.put(project/'player.gd','original physics bytes')
        return root,project,storage.project_hashes(project)

    def test_disposable_installer_changes_only_three_settings_files_and_reports_static_evidence(self):
        root,project,hashes=self.disposable()
        original=(project/'project.godot').read_bytes()
        record=bg.install_disposable_project(project,disposable_root=root,expected_source_hashes=hashes)
        self.assertEqual(storage.project_hashes(project),record['after_source_hashes'])
        self.assertEqual(record['before_source_hashes'],hashes)
        self.assertEqual(record['changed_source_files'],bg.SETTINGS_FILES)
        self.assertEqual((project/'project.godot').read_bytes(),bg.autoload_bytes(original))
        self.assertFalse(record['runtime_focus_verified'])
        self.assertEqual((project/'player.gd').read_bytes(),b'original physics bytes')
        with self.assertRaises(storage.IntegrationError):
            bg.install_disposable_project(project,disposable_root=root,expected_source_hashes=record['after_source_hashes'])

    def test_autoload_is_first_and_preserves_original_lf_crlf_and_missing_final_newline(self):
        for eol in (b'\n',b'\r\n'):
            for trailing in (b'',eol):
                with self.subTest(eol=eol,trailing=trailing):
                    original=eol.join([b'config_version=5',b'[autoload]',b'',b'Events="*res://events.gd"',
                                       b'[display]',b'window/size/viewport_width=640'])+trailing
                    expected=original.replace(b'[autoload]'+eol,
                        b'[autoload]'+eol+bg.AUTOLOAD_LINE.encode()+eol,1)
                    self.assertEqual(bg.autoload_bytes(original),expected)
                    with self.assertRaises(storage.IntegrationError): bg.autoload_bytes(expected)
        original=b'config_version=5\n[autoload]'
        self.assertEqual(bg.autoload_bytes(original),original+b'\n'+bg.AUTOLOAD_LINE.encode()+b'\n')

    def test_existing_exact_background_copy_is_verified_without_changes_or_duplicate_autoload(self):
        root,project,hashes=self.disposable()
        installed=bg.ensure_disposable_project(project,disposable_root=root,expected_source_hashes=hashes)
        current=storage.project_hashes(project)
        result=bg.ensure_disposable_project(project,disposable_root=root,expected_source_hashes=current)
        self.assertTrue(installed['installed'])
        self.assertFalse(result['installed'])
        self.assertEqual(result['changed_source_files'],[])
        self.assertEqual(result['settings_source_files'],bg.SETTINGS_FILES)
        self.assertEqual(result['before_source_hashes'],current)
        self.assertEqual(result['after_source_hashes'],current)
        self.assertEqual(storage.project_hashes(project),current)
        self.assertFalse(result['runtime_focus_verified'])
        self.assertFalse(result['acceptance_credit'])

    def test_reuse_rejects_partial_tampered_or_reordered_setup_even_with_fresh_current_hashes(self):
        root,project,hashes=self.disposable()
        bg.install_disposable_project(project,disposable_root=root,expected_source_hashes=hashes)
        paths=[project/'override.cfg',project/bg.SUPPORT_PATH,project/'project.godot']
        original={p:p.read_bytes() for p in paths}
        mutations=[(paths[0],bg.OVERRIDE_BYTES.replace(b'borderless=false',b'borderless=true')),
                   (paths[1],bg.SUPPORT_BYTES+b'# tampered\n'),
                   (paths[2],original[paths[2]].replace(b'[autoload]\n',b'[autoload]\nOther="*res://other.gd"\n'))]
        for path,raw in mutations:
            with self.subTest(path=path.name):
                path.write_bytes(raw)
                before=storage.project_hashes(project)
                with self.assertRaises(storage.IntegrationError):
                    bg.ensure_disposable_project(project,disposable_root=root,expected_source_hashes=before)
                self.assertEqual(storage.project_hashes(project),before)
                path.write_bytes(original[path])
        paths[1].unlink()
        before=storage.project_hashes(project)
        with self.assertRaises(storage.IntegrationError):
            bg.ensure_disposable_project(project,disposable_root=root,expected_source_hashes=before)
        self.assertEqual(storage.project_hashes(project),before)

    def test_failure_during_second_project_update_restores_all_original_bytes(self):
        before=self.snapshot(); original=bg._atomic_bytes
        def fail_after_replace(path,raw):
            result=original(path,raw)
            if path==self.directory/'target/project.godot' and bg.AUTOLOAD_LINE.encode() in raw:
                raise KeyboardInterrupt()
            return result
        with patch.object(bg,'_atomic_bytes',side_effect=fail_after_replace):
            with self.assertRaises(KeyboardInterrupt): bg.configure_background_session(self.directory)
        self.assertEqual(self.snapshot(),before)
        self.assertFalse((self.directory/'baseline'/bg.SUPPORT_PATH).parent.exists())
        self.assertFalse((self.directory/'target'/bg.SUPPORT_PATH).parent.exists())

    def test_verifier_rejects_tampered_support_original_bytes_and_revision(self):
        receipt_path=bg.configure_background_session(self.directory)
        receipt=storage.read_json(receipt_path)
        for changes in ({'configuration_revision':1}, {'project_settings_before_base64':'not base64!'},
                        {'project_settings_before_base64':base64.b64encode(b'config_version=5\n').decode()}):
            with self.subTest(changes=changes):
                storage.write_json(receipt_path,{**receipt,**changes})
                with self.assertRaises(storage.IntegrationError): bg.verify_background_session(self.directory)
        storage.write_json(receipt_path,receipt)
        path=self.directory/'target'/bg.SUPPORT_PATH
        path.write_bytes(bg.SUPPORT_BYTES+b'# changed\n')
        with self.assertRaises(storage.IntegrationError): bg.verify_background_session(self.directory)

    def test_disposable_installer_refuses_live_session_outside_root_stale_map_and_later_override_conflicts(self):
        root,project,hashes=self.disposable()
        for candidate,scope,wanted in [(self.directory/'target',self.root,self.state['baseline_hashes']),
                                        (project,self.root/'elsewhere',hashes),(project,root,{'wrong':'hash'})]:
            with self.assertRaises(storage.IntegrationError):
                bg.install_disposable_project(candidate,disposable_root=scope,expected_source_hashes=wanted)
            self.assertFalse((candidate/'override.cfg').exists())
        for addition in ('[display]\nwindow/size/no_focus.windows=false\n',
                         '[application]\nconfig/project_settings_override="user://later.cfg"\n'):
            self.put(project/'project.godot','config_version=5\n'+addition)
            with self.assertRaises(storage.IntegrationError):
                bg.install_disposable_project(project,disposable_root=root,expected_source_hashes=storage.project_hashes(project))
            self.assertFalse((project/'override.cfg').exists())

    def test_shared_project_file_is_rejected_before_any_mutation(self):
        original=self.directory/'target/project.godot'
        linked=self.root/'shared.godot'
        linked.hardlink_to(original)
        self.rejected()


if __name__=='__main__':
    unittest.main()
