"""Pinned CLI 2.24/KAS 0.66.8 shell titles described by the model."""
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest

from kiroclops.local_engine import review
from kiroclops.visual.schema import digest, read_json
from tests.evidence import requires_evidence


SID = 'described-pwsh-test-session'
IDENTITY = 'run_command_call_c5e4b376-cfbc-4d22-be50-a68968a2f3a6'
OPUS_IDENTITY = 'run_command_toolu_bdrk_01NdToTdv8Zvns7LLwLjeSCF'
ROOT = r'D:\workspace\target'
CONTEXT = {**{key: review._NATIVE_OUTPUT_POLICY[key]
              for key in ('cli_version', 'kas_version', 'launcher_sha256')},
           'target_dir': ROOT, 'hook_id': 'guard#hook-0'}
ARGS = {'command': 'python -c "print(1)"', 'description': 'Check candidate image',
        'cwd': ROOT, 'run_in_background': False, 'timeout': 120000}


def event(update):
    return {'jsonrpc': '2.0', 'method': 'session/update',
            'params': {'sessionId': SID, 'update': update}}


def call(args=None, identity=IDENTITY, final='completed'):
    args = deepcopy(ARGS if args is None else args)
    base = {'toolCallId': identity, 'rawInput': args,
            '_meta': {'kiro': {'toolOrigin': 'default'}}}
    return [event({'sessionUpdate': 'tool_call', 'title': args['description'],
                   'kind': 'execute', 'status': 'pending', **deepcopy(base)}),
            event({'sessionUpdate': 'tool_call_update', 'status': 'in_progress', **deepcopy(base)}),
            event({'sessionUpdate': 'tool_call_update', 'status': 'in_progress', **deepcopy(base)}),
            event({'sessionUpdate': 'tool_call_update', 'title': args['description'],
                   'status': final, 'rawOutput': {'message': 'shell output'},
                   'content': [], **deepcopy(base)})]


def decision(args=None):
    return {'at': 15, 'tool': 'execute_pwsh', 'arguments': deepcopy(ARGS if args is None else args),
            'allow': True, 'reason': 'Allowed local trial operation', 'audit_origin': 'pre_tool_use'}


def audit(events, rows=None, context=None):
    return review.audit_native_tool_outcomes(events, [decision()] if rows is None else rows,
        session_id=SID, started_at=10, finished_at=20, scope_sha256=digest({'test': 'shell'}),
        runtime_context=CONTEXT if context is None else context)


def hook_event():
    return event({'sessionUpdate': 'session_info_update', '_meta': {'kiro': {
        'kind': 'hook_update', 'hook': {'hookId': CONTEXT['hook_id'],
            'operationId': 'hook-1', 'name': 'Local trial boundary',
            'status': 'completed', 'actionType': 'runCommand'}}}})


def rejected_call(args):
    return [hook_event(), event({'sessionUpdate': 'tool_call_update',
        'toolCallId': OPUS_IDENTITY.removeprefix('run_command_'), 'status': 'failed',
        'title': 'Run Command', 'rawInput': deepcopy(args),
        '_meta': {'kiro': {'toolOrigin': 'default'}}})]


class DescribedPwshTests(unittest.TestCase):
    def assert_unproven(self, events, rows=None, context=None):
        result = audit(events, rows, context)
        self.assertEqual('fail', result['status'])
        self.assertIn('tool_identity_or_arguments_unproven',
                      {item['code'] for item in result['errors']})

    @requires_evidence('artifacts/phase3a')
    def test_installed_source_binds_dynamic_title_and_command_variant(self):
        root = Path(review.__file__).resolve().parents[2]
        proof = read_json(root / 'artifacts/phase3a/native-hook-sol24-installed-source-proof.json')
        source = Path(proof['source'])
        if not source.exists():
            self.skipTest('Pinned installed KAS source is unavailable on this host')
        data = source.read_bytes()
        self.assertEqual(review._NATIVE_OUTPUT_POLICY['installed_source_sha256'],
                         hashlib.sha256(data).hexdigest())
        self.assertIn(b'id:i?"execute_pwsh":"execute_bash"', data)
        self.assertIn(b'static id="run_command"', data)
        self.assertIn(b'let s=`run_command_${o}`', data)
        self.assertIn(b'l=voc(t.description)', data)

    @requires_evidence('artifacts/phase3a')
    def test_shell_correlation_policy_binds_inspected_source_fragments(self):
        root = Path(review.__file__).resolve().parents[2]
        path = root / 'artifacts/phase3a/panel-opus55-native-audit-02/source-proof.json'
        self.assertEqual(review._NATIVE_OUTPUT_POLICY['shell_correlation_source_proof_sha256'],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        proof = read_json(path)
        self.assertEqual('pass', proof['status'])
        self.assertEqual(review._NATIVE_OUTPUT_POLICY['installed_source_sha256'], proof['source_sha256'])
        fragments = proof['fragments']
        self.assertIn('t[r]=n===null?void 0:n', fragments['argument_null_to_undefined']['text'])
        self.assertIn('let r=EQa(t)', fragments['validated_input']['text'])
        self.assertIn('id:i?"execute_pwsh":"execute_bash"', fragments['shell_variant']['text'])
        self.assertIn('let s=`run_command_${o}`', fragments['shell_execution']['text'])
        self.assertIn('r.execution.workspace.listWorkspaceFolders().folders[0]', fragments['shell_execution']['text'])
        self.assertIn('timeout:ro({description:e}).optional().nullable()', fragments['shell_parameters']['text'])
        self.assertIn('actionId:r,actionState:"Rejected",actionType:this.streamErrorActionType,input:s.args,rawInput:s.args',
                      fragments['prehandle_rejection']['text'])
        self.assertIn('title:r.title??this.title', fragments['rejected_static_title']['text'])
        source = Path(proof['source'])
        if source.exists():
            data = source.read_bytes()
            self.assertEqual(proof['source_sha256'], hashlib.sha256(data).hexdigest())
            for fragment in fragments.values():
                value = fragment['text'].encode('utf-8')
                self.assertEqual(fragment['sha256'], hashlib.sha256(value).hexdigest())
                self.assertEqual(value, data[fragment['byte_offset']:fragment['byte_offset'] + len(value)])

    def test_exact_observed_envelope_matches_full_guard_arguments(self):
        for final in ('completed', 'failed'):
            with self.subTest(final=final):
                result = audit(call(final=final))
                self.assertEqual('pass', result['status'], result)
                self.assertEqual('execute_pwsh', result['calls'][0]['tool'])
                self.assertEqual(digest(ARGS), result['calls'][0]['arguments_sha256'])

    @requires_evidence('artifacts/phase3a')
    def test_nested_cwd_policy_binds_pinned_source(self):
        root = Path(review.__file__).resolve().parents[2]
        path = root / 'artifacts/phase3a/panel-opus55-native-audit-03/cwd-source-proof.json'
        self.assertEqual(review._NATIVE_OUTPUT_POLICY['shell_cwd_source_proof_sha256'],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        proof = read_json(path)
        self.assertEqual('pass', proof['status'])
        self.assertEqual(review._NATIVE_OUTPUT_POLICY['installed_source_sha256'], proof['source_sha256'])
        self.assertIn('t.cwd?await oo.fromRelativePath', proof['fragments']['cwd_selection']['text'])
        source = Path(proof['source'])
        if source.exists():
            data = source.read_bytes()
            self.assertEqual(proof['source_sha256'], hashlib.sha256(data).hexdigest())
            for fragment in proof['fragments'].values():
                value = fragment['text'].encode('utf-8')
                self.assertEqual(fragment['sha256'], hashlib.sha256(value).hexdigest())
                self.assertEqual(value, data[fragment['byte_offset']:fragment['byte_offset'] + len(value)])

    def test_nested_cwd_retains_exact_independent_hook_match(self):
        args = {**ARGS, 'cwd': ROOT + r'\.kiroclops-evidence'}
        events = call(args, identity=OPUS_IDENTITY)
        before = deepcopy(events)
        result = audit(events, [decision(args)])
        self.assertEqual('pass', result['status'], result)
        self.assertEqual(digest(args), result['calls'][0]['arguments_sha256'])
        self.assertEqual(before, events)
        # Even equivalent spellings cannot supply a different hook's proof.
        for cwd in (ROOT, args['cwd'].upper(), args['cwd'].replace('\\', '/')):
            result = audit(events, [decision({**args, 'cwd': cwd})])
            self.assertEqual('fail', result['status'])
            self.assertIn('missing_or_ambiguous_tool_decision', {e['code'] for e in result['errors']})

    def test_shell_cwd_recognition_rejects_paths_outside_supported_subset(self):
        for cwd in (r'.kiroclops-evidence', r'\workspace\target\nested',
                    ROOT + r'-lookalike\nested', ROOT + r'\..\outside',
                    r'E:\workspace\target\nested', r'\\server\share\nested'):
            with self.subTest(cwd=cwd):
                args = {**ARGS, 'cwd': cwd}
                self.assert_unproven(call(args, identity=OPUS_IDENTITY), [decision(args)])

    def test_missing_rejection_decision_cannot_borrow_later_retry_allow(self):
        args = {**ARGS, 'command': 'python .kiroclops-scratch/fit2.py', 'timeout': 240000}
        rejected = rejected_call(args)
        retry_hook = hook_event()
        retry_hook['params']['update']['_meta']['kiro']['hook']['operationId'] = 'hook-2'
        events = rejected + [retry_hook] + call(args)
        result = audit(events, [decision(args)])
        self.assertEqual('fail', result['status'])
        self.assertFalse(result['hook_cardinality_verified'])
        missing = [e for e in result['errors'] if e['code'] == 'missing_or_ambiguous_tool_decision']
        self.assertEqual(1, len(missing))
        self.assertEqual({OPUS_IDENTITY.removeprefix('run_command_'), IDENTITY},
                         set(missing[0]['tool_call_ids']))
        self.assertEqual(1, missing[0]['decision_count'])
        self.assertEqual([], result['calls'])

    def test_title_identity_and_runtime_must_be_exact(self):
        cases = []
        altered = call(); altered[0]['params']['update']['title'] = 'Run a script'; cases.append(altered)
        altered = call(); altered[-1]['params']['update']['title'] = 'Run a script'; cases.append(altered)
        altered = call(identity='unknown_call_c5e4b376-cfbc-4d22-be50-a68968a2f3a6'); cases.append(altered)
        altered = call(); altered[0]['params']['update']['kind'] = 'read'; cases.append(altered)
        altered = call(); altered[1]['params']['update']['_meta']['kiro']['toolOrigin'] = 'client'; cases.append(altered)
        for altered in cases:
            with self.subTest(case=cases.index(altered)):
                self.assert_unproven(altered)
        self.assert_unproven(call(), context={**CONTEXT, 'kas_version': 'changed'})

    def test_static_run_command_title_cannot_bypass_pinned_shell_envelope(self):
        static = {**ARGS, 'description': 'Run Command'}
        self.assertEqual('pass', audit(call(static), [decision(static)])['status'])
        for altered in (
                {**static, 'run_in_background': True},
                {**static, 'timeout': True},
                {**static, 'timeout': 0}):
            with self.subTest(arguments=altered):
                self.assert_unproven(call(altered), [decision(altered)])
        self.assert_unproven(call(static, identity='call-1'), [decision(static)])

    def test_opus_provider_id_and_optional_shell_fields_bind_exact_arguments(self):
        for omitted in ((), ('timeout',), ('cwd', 'timeout')):
            for timeout in (1, 120000, 240000, 300000):
                args = {k: v for k, v in {**ARGS, 'timeout': timeout}.items() if k not in omitted}
                hook_args = {**args, **{k: None for k in omitted}}
                events = call(args, identity=OPUS_IDENTITY)
                before = deepcopy(events)
                result = audit(events, [decision(hook_args)])
                self.assertEqual('pass', result['status'], result)
                observed = result['calls'][0]
                self.assertEqual(OPUS_IDENTITY, observed['tool_call_id'])
                self.assertEqual(digest(args), observed['arguments_sha256'])
                self.assertEqual([digest(args)] * 4, observed['raw_argument_sha256s'])
                self.assertEqual(before, events)
        # Optional absence is not rewritten to an effective timeout or cwd.
        omitted = {k: v for k, v in ARGS.items() if k not in ('cwd', 'timeout')}
        self.assertEqual('fail', audit(call(omitted, identity=OPUS_IDENTITY), [decision(ARGS)])['status'])
        self.assertEqual('fail', audit(call(identity=OPUS_IDENTITY),
            [decision({**ARGS, 'timeout': 240000})])['status'])

    def test_opus_shell_id_and_native_update_identity_cannot_be_guessed(self):
        for identity in ('run_command_unknown_01NdToTdv8Zvns7LLwLjeSCF',
                         OPUS_IDENTITY + 'x', OPUS_IDENTITY.removeprefix('run_command_')):
            self.assert_unproven(call(identity=identity))
        changed = call(identity=OPUS_IDENTITY)
        changed[1]['params']['update']['toolCallId'] += 'x'
        self.assertEqual('fail', audit(changed)['status'])
        explicit = {**decision(), 'tool_call_id': OPUS_IDENTITY.removeprefix('run_command_')}
        self.assertEqual('fail', audit(call(identity=OPUS_IDENTITY), [explicit])['status'])

    def test_prehandle_shell_rejection_requires_independent_deny_and_no_output(self):
        raw = {**ARGS, 'cwd': None, 'timeout': None}
        denied = {**decision(raw), 'allow': False, 'reason': 'Outside boundary'}
        events = rejected_call(raw)
        before = deepcopy(events)
        result = audit(events, [denied])
        self.assertEqual('pass', result['status'], result)
        observed = result['calls'][0]
        self.assertEqual(OPUS_IDENTITY.removeprefix('run_command_'), observed['tool_call_id'])
        self.assertEqual('execute_pwsh', observed['tool'])
        self.assertEqual('denied_no_output', observed['denial_class'])
        self.assertTrue(observed['denial_proven'])
        self.assertEqual([digest(raw)], observed['raw_argument_sha256s'])
        self.assertEqual(before, events)
        for rows in ([], [decision(raw)], [{**denied, 'arguments': {**raw, 'command': 'different'}}],
                     [{**denied, 'tool': 'read_file'}], [denied, denied]):
            self.assertEqual('fail', audit(events, rows)['status'])
        self.assertEqual('fail', audit(events[1:], [denied])['status'])

    def test_prehandle_shell_rejection_rejects_output_origin_title_and_status_changes(self):
        raw = {**ARGS, 'cwd': None, 'timeout': None}
        denied = {**decision(raw), 'allow': False, 'reason': 'Outside boundary'}
        for key, value in (('rawOutput', {}), ('content', []), ('status', 'completed'),
                           ('title', raw['description']), ('toolCallId', OPUS_IDENTITY),
                           ('_meta', {'kiro': {'toolOrigin': 'client'}})):
            changed = rejected_call(raw)
            changed[-1]['params']['update'][key] = value
            self.assert_unproven(changed, [denied])
        self.assertEqual('fail', audit(rejected_call(raw), [denied],
            context={**CONTEXT, 'kas_version': 'changed'})['status'])

    def test_read_file_title_collision_is_never_a_read_file_call(self):
        collision = {**ARGS, 'description': 'Read File'}
        # The pinned run_command identity and envelope determine the shell
        # tool; a colliding title cannot redirect its guard decision to read.
        result = audit(call(collision), [decision(collision)])
        self.assertEqual('pass', result['status'], result)
        self.assertEqual('execute_pwsh', result['calls'][0]['tool'])
        wrong_guard = {**decision(collision), 'tool': 'read_file'}
        self.assertEqual('fail', audit(call(collision), [wrong_guard])['status'])
        altered = call(collision)
        altered[0]['params']['update']['kind'] = 'read'
        self.assert_unproven(altered, [decision(collision)])

    def test_input_drift_and_unknown_shapes_fail_closed(self):
        altered = call(); altered[2]['params']['update']['rawInput']['command'] = 'different'; self.assert_unproven(altered)
        altered = call(); altered[0]['params']['update']['rawInput']['extra'] = True; self.assert_unproven(altered)
        altered = call(); altered[0]['params']['update']['rawInput']['cwd'] = r'D:\elsewhere'; self.assert_unproven(altered)
        altered = call(); altered[0]['params']['update']['status'] = 'in_progress'; self.assert_unproven(altered)
        altered = call(); altered.pop(1); self.assert_unproven(altered)
        altered = call(); altered[-1]['params']['update']['rawOutput'].pop('message');
        # Unknown shell output is not an identity failure for an allowed call;
        # the native hook still binds the exact input and source hash is separate.
        self.assertEqual('pass', audit(altered)['status'])

    def test_hook_argument_mismatch_still_fails(self):
        changed = {**ARGS, 'command': 'python -c "print(2)"'}
        result = audit(call(), [decision(changed)])
        self.assertEqual('fail', result['status'])
        self.assertIn('missing_or_ambiguous_tool_decision',
                      {item['code'] for item in result['errors']})


if __name__ == '__main__':
    unittest.main()
