"""V5b offloaded shell output extension (PHASE3D_V5B_PLAN.md 2.4)."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kiroclops.local_engine import review
from kiroclops.visual.schema import VisualError, file_sha256, write_json
from scripts import phase3d_v5b_native_offload as offload
from tests.evidence import requires_evidence

ROOT = Path(offload.__file__).resolve().parents[1]
SESSION_ID = 'sess_0123abcd-0000-4000-8000-00000000test'


def offload_file(base, output_text):
    """A Kiro tool-outputs file holding the full shell result, at the pattern the reader requires."""
    folder = Path(base) / '.kiro' / 'sessions' / '0123456789abcdef' / SESSION_ID / 'tool-outputs'
    folder.mkdir(parents=True)
    path = folder / 'execute_pwsh-0123abcd.txt'
    full = 'Output:\n' + output_text + '\n\nExit Code: 0'
    path.write_bytes(full.encode('utf-8'))
    return str(path), full


def envelope(ident, name, full, output_text, command='Get-ChildItem -File'):
    """The four native updates of an offloaded execute_pwsh call (tool_call, two progress, completed)."""
    message = review._builtin_227_v3_offload_message(name, full)
    raw_input = {'command': command, 'description': 'List files', 'cwd': 'c:\\target', 'run_in_background': False}
    first = {'sessionUpdate': 'tool_call', 'toolCallId': ident, 'status': 'pending', 'title': 'List files',
             'kind': 'execute', '_meta': {'kiro': {'toolOrigin': 'default'}}, 'rawInput': raw_input}
    progress = {'sessionUpdate': 'tool_call_update', 'toolCallId': ident, 'status': 'in_progress',
                '_meta': {'kiro': {'toolOrigin': 'default'}}, 'rawInput': raw_input}
    last = {'sessionUpdate': 'tool_call_update', 'toolCallId': ident, 'status': 'completed', 'title': 'List files',
            '_meta': {'kiro': {'toolOrigin': 'default', 'outputTransformation': {
                'kind': 'offloaded', 'absFilePath': name, 'totalChars': review._builtin_227_v3_units(full)}}},
            'rawInput': raw_input,
            'rawOutput': {'output': review._builtin_227_v3_clamp(output_text), 'exitCode': 0, 'message': message},
            'content': [{'type': 'content', 'content': {'type': 'text', 'text': message}}]}
    return [first, progress, dict(progress), last]


def stream(updates):
    return '\n'.join(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'update': u}}) for u in updates)


class PreserveTests(unittest.TestCase):
    def test_every_offloaded_call_is_copied_with_a_manifest(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as root:
            name, full = offload_file(base, 'x' * 40000)
            session = Path(base) / 'session'
            (session / 'kiro/run').mkdir(parents=True)
            plain = {'sessionUpdate': 'tool_call', 'toolCallId': 'tooluse_read', 'status': 'completed', 'title': 'Read File'}
            (session / 'kiro/run/stdout.jsonl').write_text(
                stream(envelope('run_command_a', name, full, 'x' * 40000) + [plain]), encoding='utf-8')
            manifest = offload.preserve(session, 'artifacts/offloads', root=root)
            self.assertEqual(1, len(manifest['files']))
            entry = manifest['files'][0]
            self.assertEqual(('run_command_a', name, 'artifacts/offloads/execute_pwsh-0123abcd.txt'),
                             (entry['tool_call_id'], entry['original_path'], entry['archive']))
            self.assertEqual(hashlib.sha256(full.encode('utf-8')).hexdigest(), entry['sha256'])
            path = Path(root) / 'artifacts/offloads/manifest.json'
            self.assertEqual(manifest['files'], offload.load_manifest(path, file_sha256(path), root=root)['files'])
            with self.assertRaises(VisualError):
                offload.preserve(session, 'artifacts/offloads', root=root)
            with self.assertRaises(VisualError):
                offload.preserve(session, Path(base) / 'outside', root=root)

    def test_a_changed_archive_or_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / 'a/execute_pwsh-0123abcd.txt'
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b'Output:\nabc\n\nExit Code: 0')
            manifest = Path(root) / 'a/manifest.json'
            write_json(manifest, {'kind': offload.MANIFEST_KIND, 'schema_version': 1, 'policy_id': offload.POLICY_ID,
                                  'files': [{'tool_call_id': 'run_command_a', 'original_path': 'C:\\x.txt',
                                             'archive': 'a/execute_pwsh-0123abcd.txt',
                                             'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}]})
            sha = file_sha256(manifest)
            offload.load_manifest(manifest, sha, root=root)
            with self.assertRaises(VisualError):
                offload.load_manifest(manifest, '0' * 64, root=root)
            archive.write_bytes(b'Output:\nabd\n\nExit Code: 0')
            with self.assertRaises(VisualError):
                offload.load_manifest(manifest, sha, root=root)


class PolicyTests(unittest.TestCase):
    def manifest(self, root, entries):
        path = Path(root) / 'manifest.json'
        write_json(path, {'kind': offload.MANIFEST_KIND, 'schema_version': 1, 'policy_id': offload.POLICY_ID,
                          'files': entries})
        return path, file_sha256(path)

    def test_only_manifest_calls_get_a_pin_and_everything_is_restored(self):
        seen = []
        policy = {'id': 'pinned'}

        def fake_proof(value):
            return {'observed_output_pins': [{'tool_call_id': 'p04'}]} if value is policy else None

        def fake_offload(ident, updates, session_id, value):
            seen.append((ident, [p['tool_call_id'] for p in review._builtin_227_v3_proof(value)['observed_output_pins']]))
            return 'text'

        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'execute_pwsh-0123abcd.txt').write_bytes(b'data')
            path, sha = self.manifest(root, [{'tool_call_id': 'run_command_a', 'original_path': 'C:\\o\\execute_pwsh-0123abcd.txt',
                                              'archive': 'execute_pwsh-0123abcd.txt',
                                              'sha256': hashlib.sha256(b'data').hexdigest()}])
            change = {'kind': 'offloaded', 'absFilePath': 'C:\\o\\execute_pwsh-0123abcd.txt', 'totalChars': 40000}
            listed = [{'_meta': {'kiro': {'toolOrigin': 'default', 'outputTransformation': change}}}]
            with mock.patch.object(review, '_builtin_227_v3_proof', fake_proof), \
                    mock.patch.object(review, '_builtin_227_v3_offload_output', fake_offload):
                with offload.offload_policy(path, sha, root=root) as ledger:
                    self.assertEqual('text', review._builtin_227_v3_offload_output('run_command_a', listed, 's', policy))
                    review._builtin_227_v3_offload_output('run_command_b', listed, 's', policy)
                    review._builtin_227_v3_offload_output('run_command_a', [{'_meta': {}}], 's', policy)
                    self.assertIs(fake_proof, review._builtin_227_v3_proof)
                self.assertIs(fake_offload, review._builtin_227_v3_offload_output)
            self.assertEqual([('run_command_a', ['p04', 'run_command_a']), ('run_command_b', ['p04']),
                              ('run_command_a', ['p04'])], seen)
            self.assertEqual([{'tool_call_id': 'run_command_a', 'archive': 'execute_pwsh-0123abcd.txt',
                               'sha256': hashlib.sha256(b'data').hexdigest(), 'verified': True}], ledger)

    @requires_evidence('reviews/staged-native227v3')
    def test_the_unchanged_reader_verifies_a_preserved_offload_end_to_end(self):
        policy = review._NATIVE_BUILTIN_POLICY_227
        text = 'C:\\target\\.kiroclops-evidence\\frame.png  76749\r\n' * 900
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory(dir=ROOT / 'tests') as inside:
            name, full = offload_file(base, text)
            updates = envelope('run_command_a', name, full, text)
            archive = Path(inside) / 'execute_pwsh-0123abcd.txt'
            archive.write_bytes(full.encode('utf-8'))
            relative = archive.resolve().relative_to(ROOT).as_posix()
            path, sha = self.manifest(inside, [{'tool_call_id': 'run_command_a', 'original_path': name,
                                                'archive': relative,
                                                'sha256': hashlib.sha256(full.encode('utf-8')).hexdigest()}])
            self.assertIsNone(review._builtin_227_v3_offload_output('run_command_a', updates, SESSION_ID, policy))
            with offload.offload_policy(path, sha) as ledger:
                self.assertEqual(text, review._builtin_227_v3_offload_output('run_command_a', updates, SESSION_ID, policy))
                broken = json.loads(json.dumps(updates))
                broken[-1]['rawOutput']['message'] += ' '
                self.assertIsNone(review._builtin_227_v3_offload_output('run_command_a', broken, SESSION_ID, policy))
                self.assertIsNone(review._builtin_227_v3_offload_output('run_command_a', updates, 'sess_other', policy))
            self.assertEqual([True, False, False], [row['verified'] for row in ledger])
            Path(name).write_bytes(full.encode('utf-8') + b' ')
            with offload.offload_policy(path, sha):
                self.assertIsNone(review._builtin_227_v3_offload_output('run_command_a', updates, SESSION_ID, policy))


if __name__ == '__main__':
    unittest.main()
