"""Typed native housekeeping prefixes; no capture, model or historical audit rewrite.

The singleton payload is copied from v2j03 inbound line 6556. It is a unit
fixture only; acceptance of that run remains the separately preserved FAIL.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from kiroclops.integration import kiro as native
from kiroclops.local_engine import review
from kiroclops.visual.schema import VisualError, digest

from tests.evidence import skip_module_without

ROOT = Path(__file__).resolve().parents[1]
skip_module_without('reviews/staged-native227v3/observed/p04-outcome-fixtures.json')
DATA = json.loads((ROOT / 'reviews/staged-native227v3/observed/p04-outcome-fixtures.json').read_text('utf-8'))
SID = DATA['native_session_id']
TARGET = r'C:\kc3d\s\p04\target'
ACTUAL_SID = 'sess_eaeb4a94-9a51-4154-a6b4-b3b8ad609106'
ACTUAL_CONTEXT = {
    'jsonrpc': '2.0', 'method': 'session/update', 'params': {
        'sessionId': ACTUAL_SID, 'update': {'sessionUpdate': 'session_info_update', '_meta': {'kiro': {
            'contextUsage': {'usagePercentage': 15.599300384521484},
            'breakdown': {
                'contextFiles': {'tokens': 0, 'percent': 0, 'items': []},
                'tools': {'tokens': 6376, 'percent': 0.6,
                          'mcp': {'tokens': 719, 'percent': 0.1}, 'builtin': {'tokens': 5657, 'percent': 0.6}},
                'memory': {'tokens': 0, 'percent': 0},
                'kiroResponses': {'tokens': 38497, 'percent': 14.9},
                'yourPrompts': {'tokens': 136, 'percent': 0.1},
                'sessionFiles': {'tokens': 0, 'percent': 0, 'items': []},
            }, 'kind': 'context_usage', 'usagePercentage': 15.599300384521484,
        }}}},
}


def metadata_prefix(tail_size):
    tail = deepcopy([row['event'] for row in DATA['post_response_tail']])
    prefix = [deepcopy(tail[1]), deepcopy(tail[2]), {'method': 'session/update', 'params': {'sessionId': SID,
              'update': {'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'fixture'}}}}]
    return prefix + tail[:tail_size], len(prefix)


class Native227TailPrefixTests(unittest.TestCase):
    def setUp(self):
        self.policy = review._builtin_policy_227({k: review._NATIVE_BUILTIN_POLICY_227[k]
            for k in ('cli_version', 'kas_version', 'launcher_sha256')})
        self.assertIsNotNone(review._builtin_227_v3_proof(self.policy))

    def check_tail(self, tail, *, prefix=None, policy=None, target=TARGET, sid=SID):
        whole, count = metadata_prefix(3)
        prefix = whole[:count] if prefix is None else prefix
        return review._builtin_227_housekeeping_tail(prefix + tail, len(prefix), sid,
            self.policy if policy is None else policy, target)

    def test_exact_actual_context_payload_and_singleton_are_supported(self):
        self.assertEqual('abeb1204140984720f3179fce1fd870e83c795d8fc368e287b456c715fd456c8', digest(ACTUAL_CONTEXT))
        self.assertFalse(review._context_usage_housekeeping(ACTUAL_CONTEXT, ACTUAL_SID))
        self.assertTrue(review._builtin_227_context_housekeeping(ACTUAL_CONTEXT, ACTUAL_SID))
        self.assertEqual({2}, review._builtin_227_housekeeping_tail([{'id': 7}, ACTUAL_CONTEXT], 1,
            ACTUAL_SID, self.policy, r'C:\kc3d\s\v2j03\target'))

    def test_ordered_source_bound_prefix_lengths(self):
        for size in (1, 2, 3):
            with self.subTest(size=size):
                whole, count = metadata_prefix(size)
                self.assertEqual(set(range(4, 4 + size)),
                    review._builtin_227_housekeeping_tail(whole, count, SID, self.policy, TARGET))

    def test_missing_wrong_or_changed_proof_stays_closed(self):
        whole, count = metadata_prefix(1)
        for policy in (None, review._NATIVE_BUILTIN_POLICY_227_V2,
                       {**self.policy, 'source_proof_sha256': '0' * 64}, {**self.policy, 'id': 'unknown'}):
            self.assertEqual(set(), review._builtin_227_housekeeping_tail(whole, count, SID, policy, TARGET))

    def test_wrong_session_stays_closed(self):
        whole, count = metadata_prefix(1)
        self.assertEqual(set(), review._builtin_227_housekeeping_tail(whole, count, 'foreign', self.policy, TARGET))

    def test_only_ordered_prefixes_with_no_duplicate_or_extra_tail(self):
        whole, count = metadata_prefix(3)
        context, mcp, governance = whole[count:]
        for tail in ([], [mcp], [governance], [context, governance], [context, context],
                     [mcp, context], [context, governance, mcp], [context, mcp, mcp],
                     [context, mcp, governance, context]):
            self.assertEqual(set(), self.check_tail(tail))

    def test_single_context_cannot_hide_model_permission_or_tool_fields(self):
        whole, count = metadata_prefix(1)
        context = whole[count]
        for key, value in (('modelId', 'other-model'), ('configOptions', []),
                           ('permission', 'always'), ('toolCallId', 'foreign'), ('callback', 'resume')):
            for path in ((), ('params',), ('params', 'update'), ('params', 'update', '_meta', 'kiro')):
                event = deepcopy(context)
                obj = event
                for part in path:
                    obj = obj[part]
                obj[key] = value
                self.assertEqual(set(), self.check_tail([event]))

    def test_memory_fields_and_numeric_usage_stay_strict(self):
        whole, count = metadata_prefix(1)
        for memory in ({'tokens': True, 'percent': 0}, {'tokens': -1, 'percent': 0},
                       {'tokens': 0, 'percent': True}, {'tokens': 0, 'percent': 101},
                       {'tokens': 0, 'percent': float('inf')}, {'tokens': 0, 'percent': '0'},
                       {'tokens': 0, 'percent': 0, 'callback': 'resume'}):
            event = deepcopy(whole[count])
            event['params']['update']['_meta']['kiro']['breakdown']['memory'] = memory
            self.assertEqual(set(), self.check_tail([event]))
        event = deepcopy(whole[count])
        event['params']['update']['_meta']['kiro']['usagePercentage'] += 1
        self.assertEqual(set(), self.check_tail([event]))

    def test_nonempty_context_or_session_files_stay_closed(self):
        whole, count = metadata_prefix(1)
        for section in ('contextFiles', 'sessionFiles'):
            event = deepcopy(whole[count])
            event['params']['update']['_meta']['kiro']['breakdown'][section]['items'] = [{'uri': 'private'}]
            self.assertEqual(set(), self.check_tail([event]))

    def test_not_a_metadata_method_allowlist(self):
        for kind in ('agent_message_chunk', 'agent_thought_chunk', 'tool_call', 'config_option_update'):
            event = {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': SID,
                     'update': {'sessionUpdate': kind}}}
            self.assertEqual(set(), self.check_tail([event]))

    def test_metadata_must_repeat_exact_pregeneration_catalog(self):
        whole, count = metadata_prefix(3)
        context, mcp, governance = deepcopy(whole[count:])
        self.assertEqual(set(), self.check_tail([context, mcp], prefix=[whole[2]]))
        changed = deepcopy(mcp)
        changed['params']['servers'][0]['tools'][0]['description'] += ' changed'
        self.assertEqual(set(), self.check_tail([context, changed]))
        changed = deepcopy(governance)
        changed['params']['features']['mcpEnabled'] = not changed['params']['features']['mcpEnabled']
        self.assertEqual(set(), self.check_tail([context, mcp, changed]))

    def test_malformed_metadata_cannot_pass_even_when_repeated(self):
        whole, count = metadata_prefix(3)
        context, mcp, governance = deepcopy(whole[count:])
        mcp['params']['servers'][0]['status'] = 'connecting'
        self.assertEqual(set(), self.check_tail([context, mcp], prefix=[mcp, governance, whole[2]]))

    def test_mcp_target_root_is_checked_when_present(self):
        for size in (2, 3):
            whole, count = metadata_prefix(size)
            self.assertEqual(set(), review._builtin_227_housekeeping_tail(whole, count, SID, self.policy, r'C:\foreign'))

    def test_full_guard_replay_accounts_singleton_without_semantic_drift(self):
        agent, model, effort = 'synthetic-tools', 'claude-opus-5.5', 'max'
        response = {'jsonrpc': '2.0', 'id': 7, 'result': {'stopReason': 'end_turn'}}
        prefix = [{'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': ACTUAL_SID,
                   'currentModeId': agent, 'currentModelId': model, 'effortLevel': effort,
                   'configOptions': [{'id': 'autopilot', 'currentValue': 'on'}]}},
                  {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': ACTUAL_SID,
                   'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'fixture'}}}}, response]
        guard = native.StreamConfigurationGuard(agent, model=model, effort=effort)
        for event in prefix:
            guard.observe(event)
        launch = {'stream_configuration': guard.summary(), 'response': response['result'],
                  'preflight': {'session_id': ACTUAL_SID},
                  **{k: self.policy[k] for k in ('cli_version', 'kas_version')}}
        config = {'agent_name': agent, 'model': model, 'effort': effort,
                  'launcher_sha256': self.policy['launcher_sha256'], 'target_dir': r'C:\synthetic\target'}
        result = review._replay_consumed_native_stream(prefix + [ACTUAL_CONTEXT], launch, config, response)
        self.assertEqual(3, result['consumed_events'])
        self.assertEqual(4, result['recorded_events'])
        self.assertTrue(result['tail_semantic_configuration_unchanged'])
        for key, value in (('modelId', 'other-model'), ('effortLevel', 'low'), ('currentModeId', 'foreign')):
            event = deepcopy(ACTUAL_CONTEXT)
            event['params']['update'][key] = value
            with self.assertRaises(VisualError):
                review._replay_consumed_native_stream(prefix + [event], launch, config, response)


if __name__ == '__main__':
    unittest.main()
