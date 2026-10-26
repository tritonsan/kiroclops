import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiroclops.common import ROOT
from kiroclops.local_engine.workspace_mcp import WorkspaceSessions, detect_domain, create_server
from kiroclops.visual.schema import VisualError
from kiroclops.web_local.specs import fixture_project


class WorkspaceSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root/'my-panel-app'
        shutil.copytree(fixture_project('panel', 'target'), self.workspace)
        self.sessions = WorkspaceSessions(root/'sessions')

    def tearDown(self):
        if self.sessions.session is not None:
            self.sessions.session.close()
        self.temporary.cleanup()

    def test_domain_comes_from_the_editable_entry_point(self):
        self.assertEqual(detect_domain(self.workspace), ('web_panel', 'panel'))
        svg = Path(self.temporary.name)/'svg-app'
        shutil.copytree(fixture_project('svg', 'target'), svg)
        self.assertEqual(detect_domain(svg), ('svg', 'svg'))
        empty = Path(self.temporary.name)/'empty'
        empty.mkdir()
        with self.assertRaises(VisualError) as caught:
            detect_domain(empty)
        self.assertEqual(caught.exception.code, 'unsupported_project')

    def test_start_creates_a_session_from_a_snapshot_and_resumes_it(self):
        created = self.sessions.start(self.workspace, 'Make the panel move like the reference.')
        self.assertEqual(created['status'], 'created')
        self.assertEqual(created['domain'], 'web_panel')
        self.assertEqual(created['workspace'], str(self.workspace.resolve()))
        self.assertIn('src/panel-motion.js', created['editable_scope'])
        session = Path(created['session'])
        self.assertFalse(session.is_relative_to(self.workspace))
        self.assertEqual((session/'target'/'src'/'panel-motion.js').read_bytes(),
                         (self.workspace/'src'/'panel-motion.js').read_bytes())
        again = WorkspaceSessions(self.sessions.root).start(self.workspace, 'Another request.')
        self.assertEqual((again['status'], again['session']), ('resumed', created['session']))

    def test_a_folder_inside_the_project_selects_the_project(self):
        created = self.sessions.start(self.workspace/'src', 'Make the panel move like the reference.')
        self.assertEqual((created['workspace'], created['domain']), (str(self.workspace.resolve()), 'web_panel'))

    def test_sync_copies_only_editable_files_and_reports_protected_changes(self):
        self.sessions.start(self.workspace, 'Make the panel move like the reference.')
        target = self.sessions.target
        (self.workspace/'src'/'panel-motion.js').write_text('// edited\n', encoding='utf-8')
        (self.workspace/'src'/'motion').mkdir(exist_ok=True)
        (self.workspace/'src'/'motion'/'spring.js').write_text('export const k = 1;\n', encoding='utf-8')
        (self.workspace/'index.html').write_text('<p>changed</p>\n', encoding='utf-8')
        first = self.sessions.sync()
        self.assertEqual(first['copied_into_measurement'], ['src/motion/spring.js', 'src/panel-motion.js'])
        self.assertEqual(first['not_measured_protected_changes'], ['index.html'])
        self.assertEqual((target/'src'/'panel-motion.js').read_text(encoding='utf-8'), '// edited\n')
        self.assertNotEqual((target/'index.html').read_text(encoding='utf-8'), '<p>changed</p>\n')
        (self.workspace/'src'/'motion'/'spring.js').unlink()
        second = self.sessions.sync()
        self.assertEqual((second['copied_into_measurement'], second['removed_from_measurement']), ([], ['src/motion/spring.js']))
        self.assertFalse((target/'src'/'motion'/'spring.js').exists())
        (self.workspace/'kiroclops-scratch').mkdir()
        (self.workspace/'kiroclops-scratch'/'summarize.js').write_text('// helper', encoding='utf-8')
        third = self.sessions.sync()
        self.assertEqual(third['not_measured_protected_changes'], ['index.html'])
        self.assertFalse((target/'kiroclops-scratch').exists())

    def test_the_session_writes_nothing_into_the_opened_project(self):
        before = sorted(p.relative_to(self.workspace).as_posix() for p in self.workspace.rglob('*'))
        self.sessions.start(self.workspace, 'Make the panel move like the reference.')
        (self.workspace/'src'/'panel-motion.js').write_text('// edited\n', encoding='utf-8')
        self.sessions.sync()
        self.sessions.require().inspect()
        after = sorted(p.relative_to(self.workspace).as_posix() for p in self.workspace.rglob('*'))
        self.assertEqual(after, before)
        self.assertFalse((self.workspace/'.kiroclops-evidence').exists())

    def test_a_godot_project_starts_the_ground_stage_against_the_original_heart(self):
        game = Path(self.temporary.name)/'my-game'
        game.mkdir()
        (game/'player.gd').write_text('extends CharacterBody2D\n', encoding='utf-8')
        (game/'project.godot').write_text('[application]\n', encoding='utf-8')
        self.assertEqual(detect_domain(game), ('game', 'ground'))
        with mock.patch.dict(os.environ):
            os.environ.pop('KIROCLOPS_GAME_ORIGINAL', None)
            with self.assertRaises(VisualError) as caught:
                self.sessions.start(game, 'Match the ground movement.')
            self.assertEqual(caught.exception.code, 'missing_original_baseline')
            original = Path(self.temporary.name)/'game'/'heart-original'
            original.mkdir(parents=True)
            (original/'player.gd').write_text('extends CharacterBody2D\n', encoding='utf-8')
            created = self.sessions.start(game, 'Match the ground movement.')
        self.assertEqual((created['status'], created['domain']), ('created', 'game'))
        self.assertIn('player.gd', created['editable_scope'])
        state = json.loads((Path(created['session'])/'session.json').read_text(encoding='utf-8'))
        self.assertEqual(state['experiment'], {'stage': 'ground', 'original_baseline': str(original.resolve()),
                                               'experiment_id': 'local-heart-ground'})
        self.assertEqual(Path(state['reference']).resolve(), (ROOT/'config/phase2c-v2').resolve())

    def test_tools_need_a_session_and_the_session_store_stays_outside_the_project(self):
        with self.assertRaises(VisualError) as caught:
            self.sessions.require()
        self.assertEqual(caught.exception.code, 'no_session')
        with self.assertRaises(VisualError) as caught:
            WorkspaceSessions(self.workspace/'store').start(self.workspace, 'request')
        self.assertEqual(caught.exception.code, 'unsafe_path')

    def test_server_lists_start_session_and_the_eight_session_tools(self):
        server, sessions = create_server(Path(self.temporary.name)/'server-sessions')
        listed = server.list_tools() if hasattr(server, 'list_tools') else None
        if hasattr(listed, '__await__'):
            import asyncio
            listed = asyncio.run(listed)
        names = sorted(tool.name for tool in listed) if listed is not None else None
        if names is not None:
            self.assertEqual(names, sorted(['start_session', 'inspect_session', 'prepare_experiment', 'evaluate_candidate',
                                            'inspect_window', 'finalize_candidate', 'get_job', 'cancel_job', 'build_report']))
        self.assertIsNone(sessions.session)


if __name__ == '__main__':
    unittest.main()
