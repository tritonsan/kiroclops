"""Real subprocess contention and fail-closed audit storage for local hooks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.launcher import _append_guard_audit
from kiroclops.visual.schema import VisualError


class GuardAuditSerializationTests(unittest.TestCase):
    def test_concurrent_hook_processes_commit_complete_distinct_rows(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='space naïve guard ') as workspace:
            session=Path(workspace)/'session café'
            program=(
                'import sys\n'
                'from kiroclops.local_engine.launcher import _append_guard_audit\n'
                'for index in range(3):\n'
                '    _append_guard_audit(sys.argv[1], {"worker":int(sys.argv[2]),"index":index})\n'
            )
            environment=os.environ.copy()
            environment['PYTHONPATH']=str(root)+os.pathsep+environment.get('PYTHONPATH','')
            environment['PYTHONUTF8']='1'
            children=[]
            try:
                for worker in range(8):
                    children.append(subprocess.Popen(
                        [sys.executable,'-c',program,str(session),str(worker)],
                        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        env=environment,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
                for child in children:
                    stdout,stderr=child.communicate(timeout=40)
                    self.assertEqual(0,child.returncode,(stdout.decode('utf8','replace'),
                                                       stderr.decode('utf8','replace')))
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill();child.communicate()
            audit=session/'kiro/tool-audit.jsonl'
            data=audit.read_bytes()
            self.assertTrue(data.endswith(b'\n'))
            rows=[json.loads(line) for line in data.splitlines()]
            self.assertEqual(24,len(rows))
            self.assertEqual({(worker,index) for worker in range(8) for index in range(3)},
                             {(row['worker'],row['index']) for row in rows})
            self.assertTrue(all(isinstance(row,dict) for row in rows))
            self.assertFalse(list((session/'kiro').glob('*.tmp')))

    def test_malformed_prior_log_is_preserved_and_blocks_append(self):
        corruptions=(b'{"id":1}\n}\n',b'{"id":1',b'[]\n',b'\xff\n')
        with tempfile.TemporaryDirectory() as workspace:
            for number,original in enumerate(corruptions):
                with self.subTest(number=number):
                    session=Path(workspace)/str(number)
                    audit=session/'kiro/tool-audit.jsonl'
                    audit.parent.mkdir(parents=True)
                    audit.write_bytes(original)
                    with self.assertRaises(VisualError) as error:
                        _append_guard_audit(session,{'allow':True,'id':number})
                    self.assertEqual('invalid_native_evidence',error.exception.code)
                    self.assertEqual(original,audit.read_bytes())
                    self.assertFalse(list(audit.parent.glob('*.tmp')))

    def test_failed_atomic_replace_does_not_change_existing_audit(self):
        with tempfile.TemporaryDirectory() as workspace:
            session=Path(workspace)
            audit=session/'kiro/tool-audit.jsonl'
            audit.parent.mkdir(parents=True)
            original=b'{"id":1}\n'
            audit.write_bytes(original)
            with patch('kiroclops.local_engine.launcher.os.replace',side_effect=OSError('simulated')):
                with self.assertRaises(OSError):
                    _append_guard_audit(session,{'id':2})
            self.assertEqual(original,audit.read_bytes())
            self.assertFalse(list(audit.parent.glob('*.tmp')))


if __name__=='__main__':
    unittest.main()
