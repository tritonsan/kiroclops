"""Exact static producer extension; no subprocess, application or model."""
from copy import deepcopy
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.local_engine import launcher_policy_proof as proof
from kiroclops.visual.schema import VisualError, digest
from tests.evidence import requires_evidence
from tests.frozen_native_chain import EVIDENCE, REASON, V6_DIVERGENCE


@requires_evidence(*EVIDENCE)
@unittest.skipIf(V6_DIVERGENCE, REASON)
class LauncherPolicyProofTests(unittest.TestCase):
    def setUp(self):
        self.old = (proof.ROOT / proof.ARCHIVE).read_bytes()
        self.active = (proof.ROOT / proof.ACTIVE).read_bytes()
        self.context = {'cli_version': 'kiro-cli-chat 2.27.0', 'kas_version': '0.66.22',
                        'launcher_sha256': proof.ACTIVE_LAUNCHER_SHA256}

    def test_exact_change_set_and_required_guards_are_proved(self):
        result = proof.inspect_ast_extension(self.old, self.active)
        self.assertEqual(list(proof.CHANGED_DEFINITIONS), result['changed_definitions'])
        for name in ('guard', 'LocalACPClient.handshake', 'LocalACPClient._permission_reply'):
            self.assertIn(name, result['protected_definition_ast_sha256'])

    def test_protected_guard_or_model_handshake_change_is_rejected(self):
        for before, after in [(b"if audit_origin not in", b"if audit_origin in"),
                              (b"if result.get('protocolVersion')!=1", b"if result.get('protocolVersion')!=2")]:
            self.assertIn(before, self.active)
            with self.subTest(before=before), self.assertRaises(VisualError):
                proof.inspect_ast_extension(self.old, self.active.replace(before, after, 1))

    def test_prompt_change_beyond_passive_reset_is_rejected(self):
        source = self.active.replace(b"self.prompt_sent=True", b"self.prompt_sent=False", 1)
        with self.assertRaises(VisualError):
            proof.inspect_ast_extension(self.old, source)

    def test_unknown_definition_or_import_is_rejected(self):
        for extra in (b'\ndef hidden_override():\n    return True\n', b'\nimport dangerous_extra\n'):
            with self.subTest(extra=extra), self.assertRaises(VisualError):
                proof.inspect_ast_extension(self.old, self.active + extra)

    def test_permission_profile_changes_are_not_part_of_budget_extension(self):
        before = b"tools = ['read','write','shell']"
        self.assertIn(before, self.active)
        with self.assertRaises(VisualError):
            proof.inspect_ast_extension(self.old, self.active.replace(before, b"tools = ['read','write','shell','web_search']", 1))

    def test_build_is_deterministic_and_does_not_claim_runtime_or_behavior_acceptance(self):
        value = proof.build_proof()
        self.assertEqual(digest(value), digest(proof.build_proof()))
        for field in ('native_canary', 'model_started', 'tool_authorization_granted',
                      'behavior_acceptance_granted', 'historical_receipts_rewritten'):
            self.assertFalse(value[field])

    def test_source_pin_change_blocks_build(self):
        with patch.object(proof, 'file_sha256', return_value='0' * 64), self.assertRaises(VisualError):
            proof.build_proof()

    def test_session_closure_keeps_source_and_job_guards_exact(self):
        old = (proof.ROOT / proof.SESSION_ARCHIVE).read_bytes()
        active = (proof.ROOT / 'kiroclops/local_engine/session.py').read_bytes()
        value = proof.inspect_session_closure(old, active)
        self.assertEqual(value['changed_session_methods'], ['inspect', 'submit'])
        for name in ('Session._verify_sources', 'Session._reconcile_active', 'Session.cancel_job'):
            self.assertIn(name, value['protected_definition_ast_sha256'])
        tree = ast.parse(active.decode('utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Session')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_verify_sources')
        method.body.append(ast.Pass())
        with self.assertRaises(VisualError):
            proof.inspect_session_closure(old, ast.unparse(tree).encode('utf-8'))

    def test_context_is_exact_and_cannot_alias_old_launcher_or_version(self):
        for key, value in [('launcher_sha256', proof.BASE_LAUNCHER_SHA256), ('cli_version', 'kiro-cli-chat 2.28.0'),
                           ('kas_version', '0.66.23')]:
            with self.subTest(key=key), patch.object(proof, 'read_json', side_effect=AssertionError('No read for unknown context')):
                self.assertIsNone(proof.verify_extension(dict(self.context, **{key: value})))

    def test_receipt_tampering_or_missing_source_fails_closed(self):
        value = proof.build_proof()
        with patch.object(proof, 'read_json', return_value=value), patch.object(proof, 'build_proof', return_value=value):
            self.assertEqual(value, proof.verify_extension(self.context))
        bad = deepcopy(value); bad['behavior_acceptance_granted'] = True
        with patch.object(proof, 'read_json', return_value=bad), patch.object(proof, 'build_proof', return_value=value):
            self.assertIsNone(proof.verify_extension(self.context))
        with patch.object(proof, 'read_json', return_value=value), patch.object(proof, 'build_proof', side_effect=OSError('missing source')):
            self.assertIsNone(proof.verify_extension(self.context))


if __name__ == '__main__':
    unittest.main()
