"""Pinned-source progressive transport fixtures; no model or subprocess calls."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from kiroclops.local_engine import launcher, review
from kiroclops.visual.schema import file_sha256, read_json
from scripts.prove_native_progressive_tools import build_proof, OUTPUT
from tests.test_local_native_tool_outcomes import event, decision, audit, SID
from tests.evidence import requires_evidence


CONTEXT={**{k:review._NATIVE_PROGRESSIVE_POLICY[k] for k in ('cli_version','kas_version')},
         'launcher_sha256':'a'*64,'hook_id':'progressive-hook'}
CID='toolu_bdrk_01GNcMeVLWswcCamW9Usg8xE'


def load():
    catalog={'loaded':[{'tool_id':'kiroclops::'+t,'description':'tool catalog',
                       'input_schema':{'type':'object'}} for t in launcher.MCP_TOOLS],
             'not_found':[], 'note':review._TOOL_LOAD_NOTE}
    message=json.dumps(catalog)
    return [event({'sessionUpdate':'tool_call_update','toolCallId':CID,'status':'completed',
        'title':'Tool Load','rawInput':{'tool_ids':['kiroclops::*']},
        '_meta':{'kiro':{'toolOrigin':'default'}},'rawOutput':{'message':message},
        'content':[{'type':'content','content':{'type':'text','text':message}}]})]


def deferred(tool='inspect_session',args=None):
    args={} if args is None else args
    raw={'tool_id':'kiroclops::'+tool,'arguments':args}
    base={'toolCallId':CID,'rawInput':raw,
          '_meta':{'kiro':{'serverName':'kiroclops','toolOrigin':'default'}}}
    message='{"status":"prepared"}'
    return [event({**deepcopy(base),'sessionUpdate':'tool_call','status':'pending','kind':'other','title':'@kiroclops/'+tool}),
            event({**deepcopy(base),'sessionUpdate':'tool_call_update','status':'in_progress'}),
            event({**deepcopy(base),'sessionUpdate':'tool_call_update','status':'completed','title':'@kiroclops/'+tool,
                   'rawOutput':{'response':message,'message':message,'imageBase64Urls':[]},
                   'content':[{'type':'content','content':{'type':'text','text':message}}]})]


class ProgressiveToolTests(unittest.TestCase):
    @requires_evidence('artifacts/phase3d')
    def test_source_proof_is_rebuilt_from_exact_current_installed_bytes(self):
        self.assertEqual(read_json(OUTPUT),build_proof())
        self.assertEqual(review._NATIVE_PROGRESSIVE_POLICY['source_proof_sha256'],file_sha256(OUTPUT))
        self.assertFalse(read_json(OUTPUT)['live_model_run'])

    @requires_evidence('artifacts/phase3d')
    def test_catalog_terminal_only_is_counted_and_independently_decided(self):
        r=audit(load(),[decision('tool_load',{'tool_ids':['kiroclops::*']})],CONTEXT)
        self.assertEqual('pass',r['status'],r)
        self.assertEqual(1,r['application_call_count']);self.assertEqual([],r['excluded_initialization'])
        self.assertIsNone(r['native_output_policy'])
        self.assertEqual(review._NATIVE_PROGRESSIVE_POLICY,r['progressive_tool_output_policy'])

    @requires_evidence('artifacts/phase3d')
    def test_mcp_wrapper_binds_exact_inner_hook_arguments(self):
        r=audit(deferred('get_job',{'job_id':'same'}),[decision('mcp_kiroclops_get_job',{'job_id':'same'})],CONTEXT)
        self.assertEqual('pass',r['status'],r)
        self.assertEqual(3,len(r['calls'][0]['raw_argument_sha256s']))

    def test_wrong_version_or_missing_bound_proof_stays_closed(self):
        for context in (None,{**CONTEXT,'kas_version':'0.66.8'},{**CONTEXT,'cli_version':'kiro-cli-chat 2.24.0'}):
            self.assertEqual('fail',audit(deferred(),[decision('mcp_kiroclops_inspect_session',{})],context)['status'])
        with patch.object(review,'file_sha256',return_value='0'*64):
            self.assertEqual('fail',audit(load(),[decision('tool_load',{'tool_ids':['kiroclops::*']})],CONTEXT)['status'])

    def test_missing_duplicate_unmatched_or_outer_hook_arguments_fail(self):
        d=decision('mcp_kiroclops_inspect_session',{})
        for rows in ([],[d,d],[d,decision('read_file',{'path':'other'})],
                     [decision('mcp_kiroclops_inspect_session',{'tool_id':'kiroclops::inspect_session','arguments':{}})]):
            self.assertEqual('fail',audit(deferred(),rows,CONTEXT)['status'])

    def test_malformed_wrapper_identity_metadata_output_and_updates_fail(self):
        changes=[lambda u:u[0]['rawInput'].update(extra='hidden'),
                 lambda u:u[0]['rawInput'].pop('arguments'),
                 lambda u:u[1]['rawInput']['arguments'].update(hidden=True),
                 lambda u:u[2]['rawInput'].update(tool_id='other::inspect_session'),
                 lambda u:u[1]['_meta']['kiro'].update(serverName='other'),
                 lambda u:u[1].update(rawOutput={'message':'early output'}),
                 lambda u:u[2]['rawOutput'].update(extra='output'),
                 lambda u:u[2]['rawOutput'].update(message='different'),
                 lambda u:u[2].update(title='@kiroclops/prepare_experiment'),
                 lambda u:u[2].update(status='failed')]
        for change in changes:
            es=deferred();us=[e['params']['update'] for e in es];change(us)
            self.assertEqual('fail',audit(es,[decision('mcp_kiroclops_inspect_session',{})],CONTEXT)['status'])

    def test_catalog_extra_fields_wrong_note_tool_or_private_namespace_fail(self):
        for mutate in (lambda u:u.update(kind='other'),
                       lambda u:u['rawInput'].update(extra='hidden'),
                       lambda u:u['rawInput'].update(tool_ids=['other::*']),
                       lambda u:u['rawOutput'].update(extra='hidden')):
            es=load();mutate(es[0]['params']['update'])
            self.assertEqual('fail',audit(es,[decision('tool_load',{'tool_ids':['kiroclops::*']})],CONTEXT)['status'])
        for mutate in (lambda c:c.update(note='wrong'),lambda c:c['loaded'][0].update(tool_id='other::secret'),
                       lambda c:c['loaded'].append(c['loaded'][0]),lambda c:c.update(not_found=['private'])):
            es=load();u=es[0]['params']['update'];catalog=json.loads(u['rawOutput']['message']);mutate(catalog)
            message=json.dumps(catalog);u['rawOutput']['message']=message;u['content'][0]['content']['text']=message
            self.assertEqual('fail',audit(es,[decision('tool_load',{'tool_ids':['kiroclops::*']})],CONTEXT)['status'])

    @requires_evidence('artifacts/phase3d')
    def test_completed_catalog_or_mcp_never_proves_a_denied_call_was_prevented(self):
        for es,tool,args in ((load(),'tool_load',{'tool_ids':['kiroclops::*']}),
                             (deferred(),'mcp_kiroclops_inspect_session',{})):
            r=audit(es,[decision(tool,args,allow=False)],CONTEXT)
            self.assertEqual('fail',r['status']);self.assertFalse(r['calls'][0]['denial_proven'])


if __name__=='__main__':unittest.main()
