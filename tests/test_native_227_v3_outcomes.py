"""Current production v3 parser fixtures; no process, model or capture.

External native-output reads/stat are mocked from the immutable archive. These
unit fixtures never establish that the original native file exists today.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiroclops.local_engine import review
from kiroclops.visual.schema import digest

from tests.evidence import skip_module_without

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'reviews/staged-native227v3'
skip_module_without('reviews/staged-native227v3/observed/p04-outcome-fixtures.json')
DATA = json.loads((HERE / 'observed/p04-outcome-fixtures.json').read_text(encoding='utf-8'))
SID = DATA['native_session_id']
TARGET = r'C:\kc3d\s\p04\target'
CONTEXT = {**{k: review._NATIVE_BUILTIN_POLICY_227[k] for k in ('cli_version', 'kas_version', 'launcher_sha256')}, 'target_dir': TARGET}
CASES = list(DATA['cases'].values())
ERROR = next(x for x in CASES if x['updates'][-1]['status'] == 'failed')
OFFLOAD = next(x for x in CASES if 'external_output' in x)


def audit(case, decisions=None, context=None):
    es = [{'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': SID, 'update': u}} for u in case['updates']]
    return review.audit_native_tool_outcomes(es, [case['guard']] if decisions is None else decisions, session_id=SID,
        started_at=0, finished_at=2000000000, scope_sha256=digest({'fixture': 'unit'}), runtime_context=CONTEXT if context is None else context)


def metadata(tail=None):
    tail = [x['event'] for x in DATA['post_response_tail']] if tail is None else tail
    prefix = [deepcopy(tail[1]), deepcopy(tail[2]), {'method': 'session/update', 'params': {'sessionId': SID,
              'update': {'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'fixture'}}}}]
    return prefix + deepcopy(tail), 3


class Native227V3OutcomeTests(unittest.TestCase):
    def setUp(self):
        # The parser still requires the exact original native path and full pin.
        # Supply a controlled fixture filesystem without reading that live file.
        original = Path(OFFLOAD['external_output']['original_path']).resolve()
        archive = HERE / OFFLOAD['external_output']['archive']
        content = archive.read_bytes()
        fixture_stat = archive.stat()
        real_read, real_stat, real_lstat = Path.read_bytes, Path.stat, Path.lstat
        def read_bytes(path):
            return content if path.resolve() == original else real_read(path)
        def stat(path, *args, **kwargs):
            return fixture_stat if path.absolute() == original else real_stat(path, *args, **kwargs)
        def lstat(path, *args, **kwargs):
            return fixture_stat if path.absolute() == original else real_lstat(path, *args, **kwargs)
        for method, replacement in [('read_bytes', read_bytes), ('stat', stat), ('lstat', lstat)]:
            mocked = patch.object(Path, method, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_current_policy_static_proof_and_archived_v2_unchanged(self):
        policy = review._builtin_policy_227(CONTEXT)
        self.assertIsNotNone(policy)
        self.assertEqual('kas-0.66.22-cli-2.27.0-observed-outcomes-v3', policy['id'])
        self.assertEqual('kas-0.66.22-cli-2.27.0-successful-builtins-v2', review._NATIVE_BUILTIN_POLICY_227_V2['id'])
        for k in ('cli_version', 'kas_version', 'launcher_sha256'):
            self.assertIsNone(review._builtin_policy_227({**CONTEXT, k: 'unknown'}))
        old = {**CONTEXT, 'cli_version': 'kiro-cli-chat 2.24.0', 'kas_version': '0.66.8'}
        self.assertIsNone(review._builtin_policy_227(old))
        self.assertEqual('0.66.8', review._NATIVE_OUTPUT_POLICY['kas_version'])
        self.assertEqual('2b941119342c38a0d3b7afb417fd38643c13a17b2360f3629bd78677fdd1f6ad', hashlib.sha256((ROOT / 'reviews/native227/v3-integration-01/review-v2.py').read_bytes()).hexdigest())
        # The current reader epoch includes the typed ordered-prefix fix.
        # Historical helper and native source-proof pins remain unchanged.
        self.assertEqual('86c1be95c0140d63cc37d72e984ff6446161384dcb7b658c93f11fd45c789a1f', hashlib.sha256(Path(review.__file__).read_bytes()).hexdigest())

    def test_observed_exit1_remains_failed_actual_error_not_denial_or_success(self):
        r = audit(ERROR)
        self.assertEqual('pass', r['status'], r)
        self.assertEqual(1, len(r['calls']))
        self.assertEqual('failed', r['calls'][0]['status'])
        self.assertIs(r['calls'][0]['decision_allow'], True)
        self.assertIs(r['calls'][0]['denial_proven'], False)
        self.assertIsNone(r['calls'][0]['denial_class'])

    def test_exit_minus_one_catch_and_other_unproved_codes_stay_closed(self):
        for code in (-1, 2, 0, None, True, 1.0):
            c = deepcopy(ERROR)
            u = c['updates'][-1]
            u['rawOutput']['exitCode'] = code
            text = 'Output:\n' + u['rawOutput']['output'] + '\n\nExit Code: ' + str(code)
            u['rawOutput']['message'] = text
            u['content'][0]['content']['text'] = text
            self.assertEqual('fail', audit(c)['status'], code)
        c = deepcopy(ERROR)
        del c['updates'][-1]['rawOutput']['exitCode']
        self.assertEqual('fail', audit(c)['status'])

    def test_error_content_status_guard_and_full_envelope_cannot_drift(self):
        mutations = [lambda u: u[-1].update(status='completed'), lambda u: u[-1]['rawOutput'].update(output='wrong'),
                     lambda u: u[-1]['rawOutput'].update(message='wrong'), lambda u: u[-1]['content'][0]['content'].update(text='wrong'),
                     lambda u: u[-1]['_meta']['kiro'].update(policyDenial={'reason': 'denied'}),
                     lambda u: u[1]['rawInput'].update(run_in_background=0),
                     lambda u: u[1]['rawInput'].update(command='different'), lambda u: u.pop(-1),
                     lambda u: u.append(deepcopy(u[-1]))]
        for mutate in mutations:
            c = deepcopy(ERROR)
            mutate(c['updates'])
            self.assertEqual('fail', audit(c)['status'])
        c = deepcopy(ERROR)
        c['guard']['allow'] = False
        self.assertEqual('fail', audit(c)['status'])
        c = deepcopy(ERROR)
        c['guard']['arguments']['command'] = 'different'
        self.assertEqual('fail', audit(c)['status'])

    def test_exact_observed_full_offload0_is_bound_to_original_and_archive(self):
        r = audit(OFFLOAD)
        self.assertEqual('pass', r['status'], r)
        self.assertEqual('completed', r['calls'][0]['status'])
        self.assertEqual(OFFLOAD['canonical_arguments_sha256'], r['calls'][0]['arguments_sha256'])

    def test_offload_metadata_output_identity_and_guard_mutations_are_closed(self):
        mutations = [lambda u: u[-1]['_meta']['kiro']['outputTransformation'].update(totalChars=True),
                     lambda u: u[-1]['_meta']['kiro']['outputTransformation'].update(totalChars=334347),
                     lambda u: u[-1]['_meta']['kiro']['outputTransformation'].update(absFilePath=r'C:\other\file.txt'),
                     lambda u: u[-1]['_meta']['kiro']['outputTransformation'].update(kind='unknown'),
                     lambda u: u[-1]['_meta']['kiro'].update(resources=[]), lambda u: u[-1]['rawOutput'].update(output='wrong'),
                     lambda u: u[-1]['rawOutput'].update(message='wrong'), lambda u: u[-1]['rawOutput'].update(exitCode=1),
                     lambda u: u[-1]['content'][0]['content'].update(text='wrong'), lambda u: u[-1].update(status='failed'),
                     lambda u: [x.update(toolCallId='run_command_toolu_bdrk_01aaaaaaaaaaaaaaaaaaaaaa') for x in u]]
        for mutate in mutations:
            c = deepcopy(OFFLOAD)
            mutate(c['updates'])
            self.assertEqual('fail', audit(c)['status'])
        c = deepcopy(OFFLOAD)
        c['guard']['arguments']['command'] = 'different'
        self.assertEqual('fail', audit(c)['status'])

    def test_missing_or_changed_original_offload_file_cannot_use_archive_preview(self):
        original = Path(OFFLOAD['external_output']['original_path'])
        actual = Path.read_bytes
        def missing(path):
            if path == original:
                raise FileNotFoundError('fixture missing exact output')
            return actual(path)
        with patch.object(Path, 'read_bytes', missing):
            self.assertEqual('fail', audit(OFFLOAD)['status'])
        def changed(path):
            raw = actual(path)
            if path == original:
                mid = len(raw) // 2
                return raw[:mid] + b'X' + raw[mid + 1:]
            return raw
        with patch.object(Path, 'read_bytes', changed):
            self.assertEqual('fail', audit(OFFLOAD)['status'])

    def test_metadata_triple_is_typed_identical_preprompt_and_same_workspace(self):
        es, count = metadata()
        policy = review._builtin_policy_227(CONTEXT)
        self.assertEqual({4, 5, 6}, review._builtin_227_housekeeping_tail(es, count, SID, policy, TARGET))
        self.assertFalse(review._context_usage_housekeeping(es[count], SID))
        self.assertEqual(set(), review._builtin_227_housekeeping_tail(es, count, SID, review._NATIVE_BUILTIN_POLICY_227_V2, TARGET))
        self.assertEqual(set(), review._builtin_227_housekeeping_tail(es, count, SID, policy, r'C:\different'))

    def test_external_and_archived_output_ancestor_reparse_points_stay_closed(self):
        original = Path(OFFLOAD['external_output']['original_path'])
        archive = HERE / OFFLOAD['external_output']['archive']
        real = Path.lstat
        for ancestor in (original.parent.parent, archive.parent):
            def reparse(path, *, _ancestor=ancestor):
                st = real(path)
                if path == _ancestor:
                    return SimpleNamespace(st_mode=st.st_mode, st_file_attributes=0x400)
                return st
            with patch.object(Path, 'lstat', reparse):
                self.assertEqual('fail', audit(OFFLOAD)['status'], str(ancestor))

    def test_metadata_changed_descriptor_flags_namespace_or_callback_stays_closed(self):
        policy = review._builtin_policy_227(CONTEXT)
        mutations = [lambda es: es[-1]['params']['features'].update(mcpEnabled=1),
                     lambda es: es[-1]['params'].update(sessionId='foreign'),
                     lambda es: es[-1].update(id=9), lambda es: es[-1]['params'].update(callback='resume'),
                     lambda es: es[-2]['params']['servers'][0]['tools'][0].update(disabled=True),
                     lambda es: es[-2]['params']['servers'][0]['tools'][0].update(name='new_tool'),
                     lambda es: es[-2]['params']['servers'][0]['tools'][0]['inputSchema'].update(new_field=True),
                     lambda es: es[-2]['params']['servers'][0].update(name='foreign'),
                     lambda es: es[-2]['params']['servers'][0]['_meta']['kiro']['resource']['source'].update(root=r'C:\foreign')]
        for mutate in mutations:
            es, count = metadata()
            mutate(es)
            self.assertEqual(set(), review._builtin_227_housekeeping_tail(es, count, SID, policy, TARGET))

    def test_context_memory_wrong_types_unknown_fields_and_extra_tail_are_closed(self):
        policy = review._builtin_policy_227(CONTEXT)
        for memory in ({'tokens': True, 'percent': 0}, {'tokens': -1, 'percent': 0}, {'tokens': 0, 'percent': True},
                       {'tokens': 0, 'percent': float('inf')}, {'tokens': 0, 'percent': 0, 'callback': 'resume'}):
            es, count = metadata()
            es[count]['params']['update']['_meta']['kiro']['breakdown']['memory'] = memory
            self.assertEqual(set(), review._builtin_227_housekeeping_tail(es, count, SID, policy, TARGET))
        for update in ('agent_thought_chunk', 'agent_message_chunk', 'tool_call', 'config_option_update'):
            es, count = metadata()
            es.append({'method': 'session/update', 'params': {'sessionId': SID, 'update': {'sessionUpdate': update}}})
            self.assertEqual(set(), review._builtin_227_housekeeping_tail(es, count, SID, policy, TARGET))

    def test_missing_historical_descriptor_or_wrong_order_cannot_be_housekeeping(self):
        policy = review._builtin_policy_227(CONTEXT)
        es, count = metadata()
        es[0]['params']['servers'][0]['tools'][0]['description'] = 'different'
        self.assertEqual(set(), review._builtin_227_housekeeping_tail(es, count, SID, policy, TARGET))
        es, count = metadata()
        es[-1], es[-2] = es[-2], es[-1]
        self.assertEqual(set(), review._builtin_227_housekeeping_tail(es, count, SID, policy, TARGET))


if __name__ == '__main__':
    unittest.main()
