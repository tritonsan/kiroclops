"""V4 one-server MCP environment: only that status shape is mapped, inside the receipted window."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine import review
from kiroclops.visual.schema import VisualError, file_sha256
from scripts import phase3d_v4_native_environment as env

TOOLS = ('inspect_session', 'prepare_experiment', 'evaluate_candidate', 'finalize_candidate', 'get_job', 'cancel_job',
         'build_report')


def server(name, source):
    return {'name': name, 'status': 'connected', 'prompts': [], 'resources': [], 'resourceTemplates': [],
            'tools': [{'name': t, 'description': 'Tool ' + t, 'disabled': False, 'inputSchema': {'type': 'object'}}
                      for t in TOOLS],
            '_meta': {'kiro': {'resource': {'resourceType': 'mcpServer', 'source': source}}}}


def status(*servers):
    return {'jsonrpc': '2.0', 'method': '_kiro/mcp/status', 'params': {'sessionId': 'sid', 'servers': list(servers)}}


LOCAL = server('kiroclops', {'origin': 'workspace', 'root': 'C:\\kc3d\\s\\x\\target'})
POWER = server(env.POWER_SERVER, deepcopy(env.POWER_ORIGIN))


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.original = review._builtin_227_metadata_shape
        self.variant = env.one_server_variant(self.original)

    def test_fixture_matches_the_canonical_two_server_shape(self):
        self.assertTrue(self.original(status(LOCAL, POWER), 'sid'))
        self.assertTrue(self.variant(status(LOCAL, POWER), 'sid'))

    def test_only_the_one_server_workspace_status_is_added(self):
        self.assertFalse(self.original(status(LOCAL), 'sid'))
        self.assertTrue(self.variant(status(LOCAL), 'sid'))

    def test_every_canonical_server_rule_still_applies(self):
        missing_tool = deepcopy(LOCAL)
        missing_tool['tools'] = missing_tool['tools'][:-1]
        connecting = dict(deepcopy(LOCAL), status='connecting')
        wrong_origin = deepcopy(LOCAL)
        wrong_origin['_meta']['kiro']['resource']['source'] = deepcopy(env.POWER_ORIGIN)
        extra_key = dict(deepcopy(LOCAL), extra=True)
        for label, event in (('power only', status(POWER)), ('missing tool', status(missing_tool)),
                             ('connecting', status(connecting)), ('wrong origin', status(wrong_origin)),
                             ('extra key', status(extra_key)), ('other session', dict(status(LOCAL), params={
                                 'sessionId': 'other', 'servers': [LOCAL]})), ('three servers', status(LOCAL, POWER, LOCAL))):
            with self.subTest(label=label):
                self.assertFalse(self.variant(event, 'sid'))

    def test_other_events_keep_the_canonical_verdict(self):
        governance = {'jsonrpc': '2.0', 'method': '_kiro/governance/state', 'params': {'sessionId': 'sid'}}
        self.assertEqual(self.original(governance, 'sid'), self.variant(governance, 'sid'))


class WindowTests(unittest.TestCase):
    def fixture(self, root, started, finished):
        root = Path(root)
        session = root / 's'
        (session / 'kiro').mkdir(parents=True)
        (session / 'kiro/launch.json').write_text(json.dumps({'prompt_started_at': started, 'finished_at': finished}),
                                                  encoding='utf-8')
        receipt = root / 'receipt.json'
        receipt.write_text(json.dumps({
            'kind': 'V4KiroPowerRemovalWindow', 'schema_version': 1, 'current_powers_equal_backup': True,
            'removal': {'started_at': 100.}, 'restore': {'finished_at': 200., 'log_text': 'RESTORED_IDENTICAL'},
            'sessions': [{'session': str(session), 'inside_window': True,
                          'launch_sha256': file_sha256(session / 'kiro/launch.json')}]}), encoding='utf-8')
        return session, receipt

    def test_policy_applies_inside_the_window_and_always_restores(self):
        original = review._builtin_227_metadata_shape
        with tempfile.TemporaryDirectory() as root:
            session, receipt = self.fixture(root, 120., 180.)
            with self.assertRaises(RuntimeError):
                with env.environment_policy([session], receipt=receipt, receipt_sha256=file_sha256(receipt)):
                    self.assertIsNot(original, review._builtin_227_metadata_shape)
                    self.assertTrue(review._builtin_227_metadata_shape(status(LOCAL), 'sid'))
                    raise RuntimeError('boom')
        self.assertIs(original, review._builtin_227_metadata_shape)

    def test_sessions_outside_the_window_or_a_changed_receipt_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            session, receipt = self.fixture(root, 90., 180.)
            with self.assertRaises(VisualError):
                with env.environment_policy([session], receipt=receipt, receipt_sha256=file_sha256(receipt)):
                    pass
        with tempfile.TemporaryDirectory() as root:
            session, receipt = self.fixture(root, 120., 180.)
            with self.assertRaises(VisualError):
                with env.environment_policy([session], receipt=receipt, receipt_sha256='0' * 64):
                    pass


if __name__ == '__main__':
    unittest.main()
