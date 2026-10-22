"""Guard CLI exit and audit contracts using disposable, nonexecuted projects."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiroclops.local_engine import launcher
from kiroclops.local_engine.session import create_session
from kiroclops.visual.schema import VisualError


def fixture(root, mode='tools'):
    source=root/'baseline';(source/'src').mkdir(parents=True)
    (source/'src/panel-motion.js').write_text('// harmless test fixture\n',encoding='utf8')
    (source/'allowed.txt').write_text('ALLOWED_SENTINEL\n',encoding='utf8')
    reference=root/'outside.txt';reference.write_text('DENIED_SENTINEL\n',encoding='utf8')
    directory=root/'space naïve session'
    state=create_session(directory,'web_panel',source,reference,'Hook unit test; no execution',mode=mode)
    launcher.configure(directory)
    return directory,state,reference


class HookEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory,self.state,self.outside=fixture(Path(self.temp.name))

    def invoke(self,payload,**kwargs):return launcher.guard(self.directory,payload,**kwargs)

    def cli(self,payload):
        stdout,stderr=io.StringIO(),io.StringIO()
        text=payload if isinstance(payload,str) else json.dumps(payload)
        with patch.object(sys,'argv',['guard','--session',str(self.directory),'--guard']),\
             patch.object(sys,'stdin',io.StringIO(text)),redirect_stdout(stdout),redirect_stderr(stderr):
            code=launcher.main()
        return code,stdout.getvalue(),stderr.getvalue()

    def test_all_seven_exact_native_mcp_aliases_allowed(self):
        for name in launcher.MCP_TOOLS:
            with self.subTest(name=name):
                code,out,err=self.cli({'tool_name':'mcp_kiroclops_'+name,'tool_input':{}})
                self.assertEqual(code,0);self.assertTrue(json.loads(out)['allow']);self.assertEqual(err,'')

    def test_foreign_unknown_and_prefix_aliases_denied(self):
        names=('mcp_other_inspect_session','mcp_kiroclops_unknown','prefix_mcp_kiroclops_get_job',
               'mcp_kiroclops_get_job_suffix','mcp_kiroclops_','mcp_kiroclops_*','mcp_kiroclopsget_job')
        for name in names:
            with self.subTest(name=name):
                code,out,err=self.cli({'tool_name':name,'tool_input':{}})
                self.assertEqual(code,2);self.assertFalse(json.loads(out)['allow']);self.assertTrue(err.strip())

    def test_baseline_keeps_all_seven_mcp_aliases_denied(self):
        directory,_,_=fixture(Path(self.temp.name)/'neutral','baseline')
        for name in launcher.MCP_TOOLS:
            self.assertFalse(launcher.guard(directory,{'tool_name':'mcp_kiroclops_'+name,'tool_input':{}})['allow'])

    def test_native_hook_envelope_preserves_exact_tool_arguments(self):
        arguments={'job_id':'a'*32,'wait_seconds':60}
        payload={'session_id':'fixture-native-session','hook_event_name':'PreToolUse','cwd':self.state['target'],
                 'tool_name':'mcp_kiroclops_get_job','tool_input':arguments}
        code,_,err=self.cli(payload)
        self.assertEqual(code,0);self.assertEqual(err,'')
        row=json.loads((self.directory/'kiro/tool-audit.jsonl').read_text('utf8').splitlines()[-1])
        self.assertEqual(row['arguments'],arguments);self.assertEqual(row['tool'],'mcp_kiroclops_get_job')
        self.assertEqual(row['audit_origin'],'pre_tool_use');self.assertNotIn('tool_call_id',row)

    def test_allowed_read_has_quiet_stderr(self):
        code,out,err=self.cli({'tool_name':'read_file','tool_input':{'path':str(Path(self.state['target'])/'allowed.txt')}})
        self.assertEqual(code,0);self.assertTrue(json.loads(out)['allow']);self.assertEqual(err,'')

    def test_denied_read_has_nonempty_stderr_and_exit_two(self):
        code,out,err=self.cli({'tool_name':'read_file','tool_input':{'path':str(self.outside)}})
        self.assertEqual(code,2);self.assertFalse(json.loads(out)['allow']);self.assertIn('Outside',err)

    def test_malformed_json_and_nonobject_payload_have_nonempty_stderr(self):
        values=('not json','[]','null','42',{'tool_name':None,'tool_input':{}},{'tool_name':'read_file','tool_input':None})
        for value in values:
            with self.subTest(value=value):
                code,_,err=self.cli(value);self.assertEqual(code,2);self.assertTrue(err.strip())

    def test_empty_guard_exception_gets_nonempty_fallback(self):
        with patch.object(launcher,'guard',side_effect=VisualError('fixture_failure','')):
            code,out,err=self.cli({})
        self.assertEqual(code,2);self.assertTrue(err.strip());self.assertEqual(json.loads(out)['code'],'fixture_failure')

    def test_unexpected_guard_exception_is_explicit_exit_two(self):
        for error in (TypeError('unexpected type'),AttributeError('unexpected attribute'),RuntimeError('unexpected runtime')):
            with self.subTest(error=type(error).__name__),patch.object(launcher,'guard',side_effect=error):
                code,out,err=self.cli({})
                self.assertEqual(code,2);self.assertTrue(err.strip());self.assertEqual(json.loads(out)['code'],'guard_failure')

    def test_origin_tags_and_true_callback_id_remain_separate(self):
        payload={'tool_name':'read_file','tool_input':{'path':str(Path(self.state['target'])/'allowed.txt')}}
        self.invoke(payload);self.cli(payload)
        self.invoke(payload,audit_origin='permission_reply',tool_call_id='actual-native-call-id')
        rows=[json.loads(s) for s in (self.directory/'kiro/tool-audit.jsonl').read_text('utf8').splitlines()]
        self.assertEqual([r['audit_origin'] for r in rows],['direct_guard','pre_tool_use','permission_reply'])
        self.assertNotIn('tool_call_id',rows[0]);self.assertNotIn('tool_call_id',rows[1])
        self.assertEqual(rows[2]['tool_call_id'],'actual-native-call-id')

    def test_permission_callback_uses_actual_call_id_and_origin(self):
        client=object.__new__(launcher.LocalACPClient)
        client.config={'session_dir':str(self.directory)};client._send=Mock()
        call_id='native-callback-17';tool='read_file'
        client.tool_inputs={call_id:{'path':str(Path(self.state['target'])/'allowed.txt')}}
        client.tool_identifiers={call_id:tool}
        client._permission_reply({'id':45,'params':{'toolCall':{'toolCallId':call_id},
            '_meta':{'kiro':{'toolId':tool}},'options':[{'kind':'allow_always','optionId':'session-allow'}]}})
        row=json.loads((self.directory/'kiro/tool-audit.jsonl').read_text('utf8').splitlines()[-1])
        self.assertEqual(row['audit_origin'],'permission_reply');self.assertEqual(row['tool_call_id'],call_id)
        self.assertEqual(client._send.call_args.args[0]['result']['outcome']['optionId'],'session-allow')

    def test_unproven_callback_id_or_origin_rejected(self):
        for kwargs in ({'audit_origin':'unknown'},{'tool_call_id':'fake'},
                       {'audit_origin':'pre_tool_use','tool_call_id':'fake'},
                       {'audit_origin':'permission_reply','tool_call_id':''}):
            with self.subTest(kwargs=kwargs),self.assertRaises(VisualError):self.invoke({},**kwargs)

    def test_parallel_native_hook_processes_keep_every_decision(self):
        """Two tools launched together must not overwrite each other's audit row."""
        gate=self.directory/'begin-concurrent-hooks'
        script='''import json,sys,time
from pathlib import Path
from kiroclops.local_engine.launcher import guard
print('READY',flush=True)
while not Path(sys.argv[2]).exists():time.sleep(.001)
print(json.dumps(guard(Path(sys.argv[1]),{'tool_name':sys.argv[3],'tool_input':{}},audit_origin='pre_tool_use')),flush=True)
'''
        root=Path(launcher.__file__).resolve().parents[2]
        env=os.environ.copy();env['PYTHONPATH']=str(root)+os.pathsep+env.get('PYTHONPATH','')
        names=['mcp_kiroclops_prepare_experiment','mcp_kiroclops_inspect_session']*6
        workers=[]
        try:
            for name in names:
                worker=subprocess.Popen([sys.executable,'-c',script,str(self.directory),str(gate),name],
                    cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                workers.append(worker)
            for worker in workers:self.assertEqual(worker.stdout.readline().strip(),'READY')
            gate.touch()
            for worker in workers:
                stdout,stderr=worker.communicate(timeout=30)
                self.assertEqual(worker.returncode,0,stderr)
                self.assertTrue(json.loads(stdout)['allow'])
            rows=[json.loads(line) for line in (self.directory/'kiro/tool-audit.jsonl').read_text('utf8').splitlines()]
            self.assertEqual(len(rows),len(names))
            self.assertCountEqual([row['tool'] for row in rows],names)
            self.assertTrue(all(row['audit_origin']=='pre_tool_use' for row in rows))
        finally:
            gate.touch()
            for worker in workers:
                if worker.poll() is None:worker.kill()
                worker.wait(timeout=10)

    def test_damaged_audit_fails_closed_without_rewriting_evidence(self):
        self.invoke({'tool_name':'mcp_kiroclops_inspect_session','tool_input':{}})
        audit=self.directory/'kiro/tool-audit.jsonl'
        with audit.open('ab') as stream:stream.write(b'}\n')
        original=audit.read_bytes()
        with self.assertRaises(VisualError) as caught:
            self.invoke({'tool_name':'mcp_kiroclops_prepare_experiment','tool_input':{}})
        self.assertEqual(caught.exception.code,'invalid_native_evidence')
        self.assertEqual(audit.read_bytes(),original)

    def test_failed_atomic_replace_preserves_prior_audit(self):
        self.invoke({'tool_name':'mcp_kiroclops_inspect_session','tool_input':{}})
        audit=self.directory/'kiro/tool-audit.jsonl';original=audit.read_bytes()
        with patch.object(launcher.os,'replace',side_effect=OSError('synthetic atomic replace failure')):
            with self.assertRaises(OSError):
                self.invoke({'tool_name':'mcp_kiroclops_prepare_experiment','tool_input':{}})
        self.assertEqual(audit.read_bytes(),original)
        self.assertFalse(list(audit.parent.glob('.tool-audit.jsonl.*.tmp')))


if __name__=='__main__':unittest.main()
