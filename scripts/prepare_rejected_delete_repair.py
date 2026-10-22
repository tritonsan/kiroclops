"""Stage and test the native-audit repair without changing a running cohort."""
from pathlib import Path
import difflib
import hashlib
import importlib.util
import io
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.local_engine import review
from tests.test_local_native_tool_outcomes import event, hook_event, decision, audit, CONTEXT


class RejectedDeleteTests(unittest.TestCase):
    def fixture(self):
        args = {'explanation': 'Remove temporary helper', 'targetFile': r'D:\workspace\.kiroclops-scratch\track.py'}
        rejected = event({'sessionUpdate': 'tool_call_update', 'toolCallId': 'delete-1',
            'status': 'failed', 'title': 'Delete File', 'rawInput': dict(args),
            'locations': [{'path': args['targetFile']}], '_meta': {'kiro': {'toolOrigin': 'acp'}}})
        return [rejected, hook_event(1)], [decision('delete_file', args, allow=False)]

    def test_bound_prehandle_delete_rejection(self):
        events, rows = self.fixture()
        result = audit(events, rows, CONTEXT)
        self.assertEqual(result['status'], 'pass', result)
        self.assertTrue(result['calls'][0]['denial_proven'])
        self.assertFalse(result['calls'][0]['decision_allow'])

    def test_adversarial_rejections(self):
        for case in ('output', 'empty_output', 'content', 'wrong_location', 'missing_hook',
                     'changed_input', 'completed', 'unknown_tool', 'extra_schema_field', 'wrong_type'):
            with self.subTest(case=case):
                events, rows = self.fixture()
                update = events[0]['params']['update']
                if case == 'output': update['rawOutput'] = {'message': 'deleted'}
                elif case == 'empty_output': update['rawOutput'] = {}
                elif case == 'content': update['content'] = []
                elif case == 'wrong_location': update['locations'][0]['path'] = 'unrelated.txt'
                elif case == 'missing_hook': events.pop()
                elif case == 'changed_input': rows[0]['arguments']['targetFile'] += '.other'
                elif case == 'completed': update['status'] = 'completed'
                elif case == 'unknown_tool': update['title'] = 'Unverified File Tool'
                elif case == 'extra_schema_field':
                    update['rawInput']['unverified'] = True
                    rows[0]['arguments']['unverified'] = True
                elif case == 'wrong_type':
                    update['rawInput']['targetFile'] = []
                    rows[0]['arguments']['targetFile'] = []
                self.assertEqual(audit(events, rows, CONTEXT)['status'], 'fail')


def run(output):
    output = Path(output).resolve()
    assert not output.exists(), 'Fresh repair output required'
    source = Path(review.__file__)
    original = source.read_text('utf-8')
    alias = "'Run Command':'execute_pwsh','Grep Search':'grep_search'}"
    revised = original.replace(alias, "'Run Command':'execute_pwsh','Grep Search':'grep_search','Delete File':'delete_file'}")
    location = "if not isinstance(location,dict) or location!={'path':args.get('path')}:return False"
    revised = revised.replace(location,
        "expected_path=args.get('targetFile') if tool=='delete_file' else args.get('path')\n"
        "            if not isinstance(location,dict) or location!={'path':expected_path}:return False")
    anchor = "if tool=='str_replace':result.setdefault('replace_all',False)"
    revised = revised.replace(anchor,
        "# The installed delete schema has two string fields; its native location\n"
        "    # uses targetFile. Preserve exact input digests and pre-handle denial.\n"
        "    if tool=='delete_file' and (set(result)!={'targetFile','explanation'}\n"
        "        or not all(isinstance(result[k],str) and result[k] for k in result)):return None\n"
        "    " + anchor)
    assert all(original.count(value) == 1 for value in (alias, location, anchor))
    output.mkdir(parents=True)
    proposal = output/'review-proposed.py'
    proposal.write_text(revised, 'utf-8')
    (output/'review.patch').write_text(''.join(difflib.unified_diff(original.splitlines(True),
        revised.splitlines(True), fromfile='kiroclops/local_engine/review.py', tofile='kiroclops/local_engine/review.py')), 'utf-8')
    installed_proof = read_json(ROOT/'artifacts/phase3a/native-hook-sol24-installed-source-proof.json')
    installed = Path(installed_proof['source'])
    data = installed.read_bytes()
    assert hashlib.sha256(data).hexdigest() == installed_proof['source_sha256']
    begin = data.index(b'var V_r={id:"delete_file"')
    end = data.index(b';Nr();Gn();', begin)
    fragment = data[begin:end]
    (output/'installed-delete-tool.js').write_bytes(fragment)
    assert all(value in fragment for value in (b'title:"Delete File"', b'targetFile:A.string()',
                                               b'explanation:A.string()', b'workspace.deleteFile'))
    spec = importlib.util.spec_from_file_location('kiroclops.local_engine._delete_repair_proposal', proposal)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.TestSuite()
    for name in ('tests.test_local_native_tool_outcomes', 'tests.test_local_native_described_pwsh',
                 'tests.test_local_postturn_native_audit', 'tests.test_local_historical_audit_config'):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RejectedDeleteTests))
    stream = io.StringIO()
    with patch.object(review, '_native_tool_name', module._native_tool_name), \
         patch.object(review, '_native_arguments', module._native_arguments), \
         patch.object(review, '_hook_denied_no_output', module._hook_denied_no_output):
        result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    (output/'tests.txt').write_text(stream.getvalue(), 'utf-8')
    receipt = {'status': 'pass' if result.wasSuccessful() else 'fail', 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'production_modified': False, 'cohort_acceptance_modified': False,
        'production_review_sha256': file_sha256(source), 'proposed_review_sha256': file_sha256(proposal),
        'installed_source_sha256': file_sha256(installed), 'installed_delete_fragment_sha256': file_sha256(output/'installed-delete-tool.js'),
        'fragment_byte_offset': begin, 'fragment_byte_length': len(fragment),
        'test_scope': 'Proposed three helper functions in an isolated test process; no running model process modified.'}
    write_json(output/'result.json', receipt)
    print(receipt, flush=True)
    assert result.wasSuccessful(), stream.getvalue()


if __name__ == '__main__':
    run(sys.argv[1])
