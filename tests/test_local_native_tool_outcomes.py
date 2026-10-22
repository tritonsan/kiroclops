"""Synthetic transport/decision fixtures; no native process or historical paths."""
import ast
from copy import deepcopy
import difflib
import hashlib
import ntpath
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import quote

from kiroclops.local_engine import launcher,review
from kiroclops.portable_v2 import launcher as portable_launcher
from kiroclops.visual.schema import digest,VisualError,file_sha256,read_json
from tests.evidence import requires_evidence


SID='unit-session'
CONTEXT={**{k:review._NATIVE_OUTPUT_POLICY[k] for k in ('cli_version','kas_version','launcher_sha256')},
    'hook_id':'unit/.kiro/hooks/kiroclops-guard.json#hook-0'}
APPEND_CONTEXT={'target_dir':r'D:\workspace\target'}


def event(update):return {'jsonrpc':'2.0','method':'session/update','params':{'sessionId':SID,'update':update}}
def hook_event(number):return event({'sessionUpdate':'session_info_update','_meta':{'kiro':{'kind':'hook_update','hook':{
    'hookId':CONTEXT['hook_id'],'operationId':f'hook-{number}','name':'Local trial boundary',
    'status':'completed','actionType':'runCommand'}}}})
def call(cid='call-1',title='Read File',args=None,status='completed',output=None):
    args={'path':'target/allowed.txt'} if args is None else args
    return [event({'sessionUpdate':'tool_call','toolCallId':cid,'title':title,'kind':'read','status':'in_progress','rawInput':deepcopy(args)}),
        event({'sessionUpdate':'tool_call_update','toolCallId':cid,'status':status,'rawInput':deepcopy(args),
            'rawOutput':{'message':'allowed data'} if output is None else output})]
def append_call(args,old='previous content\n'):
    events=call(title='Append to File',args=args)
    root=r'D:\workspace\target';resolved=args['path'] if ntpath.isabs(args['path']) else ntpath.join(root,args['path'])
    events[0]['params']['update']['rawInput']={'path':args['path']}
    pending=deepcopy(events[1]);update=pending['params']['update']
    update['status']='pending';update.pop('rawOutput');update.pop('title',None)
    first=events[0]['params']['update'];first['kind']='edit'
    first['locations']=[{'path':resolved}]
    first['_meta']={'kiro':{'toolOrigin':'acp','preview':{'file':args['path']}}}
    update['locations']=deepcopy(first['locations']);update['_meta']=deepcopy(first['_meta'])
    terminal=events[1]['params']['update'];path=args['path']
    local='file:///'+quote(resolved.replace('\\','/'),safe='/')
    relative=ntpath.relpath(resolved,root).replace('\\','/')
    snapshot_query=quote('originalPath='+quote(relative,safe=''),safe='')
    original=f'kiro-snapshot-v2://{SID}:aaaaaaaa/?'+snapshot_query
    modified=f'kiro-snapshot-v2://{SID}:bbbbbbbb/?'+snapshot_query
    new=old+('\n' if args['text'] and not old.endswith('\n') else '')+args['text']
    terminal['title']='Append to File';terminal['locations']=[{'path':resolved}]
    terminal['rawOutput']={'message':f'Appended the text to the {path} file.'}
    terminal['content']=[{'type':'diff','path':local,'newText':new,'oldText':old}]
    terminal['_meta']={'kiro':{'toolOrigin':'acp','preview':{'file':resolved,'local':local,
        'original':original,'modified':modified,'originalContent':old,'modifiedContent':new},
        'checkpoint':{'original':original,'modified':modified,'local':local}}}
    return [events[0],pending,events[1]]
def decision(tool='read_file',args=None,allow=True,**kw):return {
    'at':15,'tool':tool,'arguments':{'path':'target/allowed.txt'} if args is None else deepcopy(args),
    'allow':allow,'reason':'allowed' if allow else 'outside scope','audit_origin':'pre_tool_use',**kw}
def empty_rejection(cid='call-1',title='Read File',args=None):
    args={'path':'outside/private.txt'} if args is None else args
    return event({'sessionUpdate':'tool_call_update','toolCallId':cid,'status':'failed','title':title,
        'rawInput':deepcopy(args),'locations':[{'path':args['path']}],'_meta':{'kiro':{'toolOrigin':'default'}}})
def audit(events,rows,context=None):return review.audit_native_tool_outcomes(events,rows,session_id=SID,
    started_at=10,finished_at=20,scope_sha256=digest({'scope':'unit'}),runtime_context=context)


class NativeOutcomeTests(unittest.TestCase):
    def audit_fails(self,events,rows,code,context=None):
        result=audit(events,rows,context);self.assertEqual(result['status'],'fail',result)
        self.assertIn(code,{e['code'] for e in result['errors']});return result

    @requires_evidence('artifacts/phase3a', 'artifacts/phase3b')
    def test_historical_opus55_policy_remains_bound_to_frozen_launcher(self):
        root=Path(review.__file__).resolve().parents[2]
        path=root/'artifacts/phase3a/native-hook-opus55-model-source-proof.json'
        proof=read_json(path);policy=review._NATIVE_OUTPUT_POLICY_OPUS55_MAX_V3
        frozen_current=root/'artifacts/phase3a/acceptance-reporting-01/launcher-before.py'
        self.assertEqual('NativeHookOpus55ModelSourceEquivalence',proof['kind'])
        self.assertEqual('pass',proof['status'])
        self.assertEqual(file_sha256(path),policy['launcher_equivalence_proof_sha256'])
        self.assertEqual(file_sha256(frozen_current),policy['launcher_sha256'])
        self.assertEqual(policy['launcher_sha256'],proof['current_local_launcher']['sha256'])
        self.assertEqual(file_sha256(portable_launcher.__file__),policy['portable_client_sha256'])
        self.assertEqual(policy['portable_client_sha256'],proof['current_portable_client']['sha256'])
        self.assertEqual((launcher.MODEL,launcher.EFFORT),('claude-opus-5.5','max'))
        self.assertEqual({'before':['gpt-5.6-sol','max'],'after':['claude-opus-5.5','max']},
                         proof['approved_model_change'])
        self.assertTrue(proof['local_source_equivalent_outside_model_assignment'])
        for key in ('frozen_local_launcher','current_local_launcher','current_portable_client',
                    'prior_source_proof','installed_runtime_source_proof','historical_native_canary'):
            bound=frozen_current if key=='current_local_launcher' else root/proof[key]['path']
            self.assertEqual(file_sha256(bound),proof[key]['sha256'])
        self.assertEqual(review._NATIVE_OUTPUT_POLICY_SOL_MAX_V4['launcher_sha256'],
                         proof['frozen_local_launcher']['sha256'])
        self.assertEqual(review._NATIVE_OUTPUT_POLICY_SOL_MAX_V4['launcher_equivalence_proof_sha256'],
                         proof['prior_source_proof']['sha256'])
        self.assertEqual(policy['source_proof_sha256'],proof['installed_runtime_source_proof']['sha256'])
        self.assertEqual(policy['native_canary_sha256'],proof['historical_native_canary']['sha256'])
        for key in ('cli_version','kas_version','installed_source_sha256'):
            self.assertEqual(policy[key],proof[key])
        frozen=(root/proof['frozen_local_launcher']['path']).read_bytes()
        current=frozen_current.read_bytes()
        before=b"MODEL, EFFORT = 'gpt-5.6-sol', 'max'"
        after=b"MODEL, EFFORT = 'claude-opus-5.5', 'max'"
        self.assertEqual([{'before':before.decode(),'after':after.decode()}],proof['allowed_changes'])
        self.assertEqual(1,frozen.count(before));self.assertEqual(1,current.count(after))
        self.assertEqual(frozen.replace(before,after),current)

    @requires_evidence('artifacts/phase3a', 'artifacts/phase3b')
    def test_historical_cloud_policy_binds_unchanged_transport_not_whole_guard(self):
        from unittest.mock import patch
        from scripts.prove_cloud_launcher_transport import build_proof, OUTPUT
        # Startup recovery is separately source-proven. Rebuild the original
        # cloud proof from its byte-identical pre-recovery snapshot, rather
        # than relabel the latest launcher as that historical version.
        root=Path(__file__).resolve().parents[1]
        snapshot=root/'artifacts/phase3b/startup-transport-proof-01/launcher-before.py'
        with patch('scripts.prove_cloud_launcher_transport.CURRENT',snapshot):
            proof=build_proof()
        saved=read_json(OUTPUT)
        proof['current_local_launcher']['path']=saved['current_local_launcher']['path']
        policy=review._NATIVE_OUTPUT_POLICY
        self.assertEqual(saved,proof)
        self.assertEqual(file_sha256(OUTPUT),policy['transport_compatibility_proof_sha256'])
        self.assertEqual(file_sha256(snapshot),policy['launcher_sha256'])
        self.assertEqual(proof['guard_bootstrap']['sha256'],policy['guard_bootstrap_sha256'])
        self.assertIsNone(policy['launcher_equivalence_proof_sha256'])
        self.assertFalse(proof['live_model_run'])
        self.assertEqual(set(proof['unchanged_sections']),{'MODEL','APPROVAL','LocalACPClient','_run_native'})

    @requires_evidence('artifacts/phase3a', 'artifacts/phase3b')
    def test_historical_diagnostic_policy_and_evidence_remain_readable(self):
        from scripts.prove_cloud_launcher_transport import BEFORE,PRIOR
        policy=review._NATIVE_OUTPUT_POLICY_DIAGNOSTIC_V3
        self.assertEqual(file_sha256(BEFORE),policy['launcher_sha256'])
        self.assertEqual(file_sha256(PRIOR),policy['transport_compatibility_proof_sha256'])
        context={**CONTEXT,**{k:policy[k] for k in ('cli_version','kas_version','launcher_sha256')}}
        result=audit(call()+[hook_event(1)],[],context)
        self.assertEqual(result['native_output_policy']['id'],policy['id'])
        self.assertEqual(result['status'],'fail')

    def test_historical_and_diagnostic_policy_keep_missing_decisions_failed(self):
        for policy in (review._NATIVE_OUTPUT_POLICY,review._NATIVE_OUTPUT_POLICY_OPUS55_MAX_V3,
                       review._NATIVE_OUTPUT_POLICY_SOL_MAX_V4):
            context={**CONTEXT,**{k:policy[k] for k in ('cli_version','kas_version','launcher_sha256')}}
            result=audit(call()+[hook_event(1)],[],context)
            self.assertEqual(result['native_output_policy']['id'],policy['id'])
            self.assertEqual(result['status'],'fail')

    @requires_evidence('artifacts/phase3a', 'artifacts/phase3b')
    def test_historical_sol_native_source_proof_remains_bound_to_frozen_launcher(self):
        root=Path(review.__file__).resolve().parents[2]
        proof_path=root/'artifacts/phase3a/native-hook-audit-serialization-source-proof.json'
        policy=review._NATIVE_OUTPUT_POLICY_SOL_MAX_V4
        proof=read_json(proof_path)
        current_path=root/'artifacts/phase3a/opus55-switch-01/launcher-before.py'
        self.assertEqual(file_sha256(current_path),policy['launcher_sha256'])
        self.assertEqual(file_sha256(proof_path),policy['launcher_equivalence_proof_sha256'])
        self.assertEqual('NativeHookAuditSerializationAndSolModelSourceEquivalence',proof['kind'])
        self.assertEqual('pass',proof['status'])
        self.assertEqual(policy['launcher_sha256'],proof['current_local_launcher']['sha256'])
        self.assertEqual(file_sha256(portable_launcher.__file__),policy['portable_client_sha256'])
        self.assertEqual(policy['portable_client_sha256'],proof['current_portable_client']['sha256'])
        self.assertTrue(proof['local_source_equivalent_outside_allowed_changes'])
        self.assertEqual(['module description','uuid import','MODEL assignment',
                          '_append_guard_audit helper','guard audit tail'],proof['allowed_changes'])
        self.assertEqual({'before':['claude-opus-5','max'],'after':['gpt-5.6-sol','max']},
                         proof['approved_model_change'])

        prompt_path=root/proof['prior_prompt_source_proof']['path']
        prompt=read_json(prompt_path)
        self.assertEqual(file_sha256(prompt_path),proof['prior_prompt_source_proof']['sha256'])
        self.assertEqual('pass',prompt['status'])
        self.assertTrue(prompt['local_source_equivalent_outside_prompt_assignments'])
        self.assertEqual(prompt['current_local_launcher']['sha256'],proof['frozen_local_launcher']['sha256'])
        self.assertEqual(prompt['current_portable_client']['sha256'],proof['current_portable_client']['sha256'])
        self.assertEqual(prompt['installed_kas']['proof_sha256'],
                         file_sha256(root/'artifacts/phase3a/native-hook-enforcement-repair-01/native-source/proof.json'))

        source_proof_path=root/'artifacts/phase3a/native-hook-sol24-installed-source-proof.json'
        source_proof=read_json(source_proof_path)
        self.assertEqual(file_sha256(source_proof_path),policy['source_proof_sha256'])
        self.assertEqual('pass',source_proof['status'])
        self.assertEqual(9,source_proof['fragments_verified'])
        self.assertEqual(policy['installed_source_sha256'],source_proof['source_sha256'])
        self.assertEqual(policy['kas_version'],source_proof['kas_version'])
        self.assertEqual(policy['cli_version'],'kiro-cli-chat '+source_proof['cli_version'])
        self.assertTrue(all(source_proof['source_checks'].values()))
        installed=Path(source_proof['source'])
        if installed.exists():
            data=installed.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),source_proof['source_sha256'])
            for fragment in source_proof['fragments'].values():
                begin=fragment['byte_offset'];end=begin+fragment['byte_length']
                self.assertEqual(hashlib.sha256(data[begin:end]).hexdigest(),fragment['sha256'])

        canary_path=root/'artifacts/phase3a/native-hook-canary-sol10/raw-observation.json'
        canary=read_json(canary_path)
        self.assertEqual(file_sha256(canary_path),policy['native_canary_sha256'])
        self.assertEqual('raw_observed_pass',canary['status'])
        self.assertEqual((canary['cli_version'],canary['kas_version']),
                         (policy['cli_version'],policy['kas_version']))
        self.assertEqual((canary['model'],canary['effort']),('gpt-5.6-sol','max'))
        self.assertEqual((canary['native_guard_rows'],canary['hook_notifications']),(5,5))
        self.assertEqual(2,sum(p['native_status']=='completed' and p['guard_allow'] for p in canary['probes']))
        self.assertEqual(3,sum(p['native_status']=='failed' and not p['guard_allow']
                               and not p['raw_output_present'] and not p['content_present']
                               for p in canary['probes']))
        self.assertTrue(canary['outside_sentinel_absent_from_native_trace'])
        self.assertTrue(canary['protected_file_unchanged'])

        prior_path=root/prompt['prior_client_source_proof']['path']
        prior=read_json(prior_path)
        self.assertEqual(file_sha256(prior_path),prompt['prior_client_source_proof']['sha256'])
        self.assertEqual('pass',prior['status'])
        self.assertTrue(prior['local_source_equivalent_after_allowlisted_changes'])
        self.assertTrue(prior['portable_source_equivalent_after_allowlisted_changes'])
        self.assertEqual(3,len(prior['portable_client_changes']))
        self.assertEqual(prior['current_local_launcher']['sha256'],prompt['frozen_local_launcher']['sha256'])
        self.assertEqual(prior['current_portable_client']['sha256'],prompt['current_portable_client']['sha256'])

        frozen_path=root/proof['frozen_local_launcher']['path']
        self.assertEqual(file_sha256(frozen_path),proof['frozen_local_launcher']['sha256'])
        self.assertEqual(file_sha256(current_path),proof['current_local_launcher']['sha256'])
        frozen=frozen_path.read_text('utf-8').splitlines(keepends=True)
        current=current_path.read_text('utf-8').splitlines(keepends=True)
        changes=[(tag,i1,i2,j1,j2) for tag,i1,i2,j1,j2 in
                 difflib.SequenceMatcher(a=frozen,b=current,autojunk=False).get_opcodes() if tag!='equal']
        self.assertEqual(5,len(changes))
        for actual,recorded in zip(changes,proof['changed_line_blocks']):
            tag,i1,i2,j1,j2=actual
            self.assertEqual((tag,i1+1,i2+1,j1+1,j2+1),
                             tuple(recorded[key] for key in ('kind','old_line_start','old_line_end_exclusive',
                                                               'new_line_start','new_line_end_exclusive')))
            self.assertEqual(''.join(frozen[i1:i2]),recorded['before'])
            self.assertEqual(''.join(current[j1:j2]),recorded['after'])
        diff=''.join(difflib.unified_diff(frozen,current,fromfile=proof['frozen_local_launcher']['path'],
                                        tofile=proof['current_local_launcher']['path']))
        self.assertEqual(hashlib.sha256(diff.encode('utf-8')).hexdigest(),proof['unified_diff_sha256'])

        def outside_allowed_ast(lines):
            tree=ast.parse(''.join(lines))
            self.assertIsInstance(tree.body[0],ast.Expr)
            self.assertIsInstance(tree.body[0].value,ast.Constant)
            self.assertIsInstance(tree.body[0].value.value,str)
            tree.body[0].value=ast.Constant(value=None)
            models=[n for n in tree.body if isinstance(n,ast.Assign) and len(n.targets)==1 and
                    isinstance(n.targets[0],ast.Tuple) and
                    [x.id for x in n.targets[0].elts if isinstance(x,ast.Name)]==['MODEL','EFFORT']]
            self.assertEqual(1,len(models))
            models[0].value=ast.Constant(value=None)
            imports=[n for n in tree.body if isinstance(n,ast.Import) and
                     len(n.names)==1 and n.names[0].name=='uuid']
            helpers=[n for n in tree.body if isinstance(n,ast.FunctionDef) and
                     n.name=='_append_guard_audit']
            self.assertEqual(len(helpers),len(imports))
            self.assertIn(len(imports),(0,1))
            tree.body=[n for n in tree.body if n not in imports+helpers]
            guard=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='guard')
            decisions=[i for i,n in enumerate(guard.body) if isinstance(n,ast.Assign) and
                       len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='decision']
            self.assertEqual(1,len(decisions))
            self.assertIsInstance(guard.body[-1],ast.Return)
            self.assertEqual('decision',guard.body[-1].value.id)
            guard.body=guard.body[:decisions[0]+1]+[guard.body[-1]]
            return ast.dump(tree,include_attributes=False)
        frozen_ast=outside_allowed_ast(frozen)
        self.assertEqual(frozen_ast,outside_allowed_ast(current))
        self.assertEqual(hashlib.sha256(frozen_ast.encode('utf-8')).hexdigest(),proof['normalized_ast_sha256'])

    def test_allowed_completed_read_is_bound(self):
        result=audit(call(),[decision()]);self.assertEqual(result['status'],'pass')
        self.assertFalse(result['calls'][0]['denial_proven'])

    def test_append_path_preview_binds_to_completed_text_and_guard_decision(self):
        args={'path':r'D:\workspace\target\.kiroclops-scratch\fit.py','text':'\nprint(1)\n'}
        events=append_call(args)
        rows=[decision('fs_append',args)]
        result=audit(events,rows,APPEND_CONTEXT)
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(result['calls'][0]['arguments_sha256'],digest(args))
        self.assertEqual(len(result['calls'][0]['raw_argument_sha256s']),3)
        self.audit_fails(events,[decision('fs_append',{**args,'text':'different'})],
                         'missing_or_ambiguous_tool_decision',APPEND_CONTEXT)
        self.audit_fails(events,[decision('fs_append',args,False)],
                         'denied_tool_executed_or_output_unverified',APPEND_CONTEXT)

    def test_append_relative_input_resolves_locations_to_workspace(self):
        args={'path':r'.kiroclops-scratch\fit.py','text':'\nprint(2)\n'}
        events=append_call(args)
        result=audit(events,[decision('fs_append',args)],APPEND_CONTEXT)
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(result['calls'][0]['arguments_sha256'],digest(args))

    def test_append_source_inserts_newline_after_unterminated_file(self):
        args={'path':r'D:\workspace\target\allowed.txt','text':'extra'}
        for old in ('previous content',''):
            events=append_call(args,old)
            self.assertEqual(audit(events,[decision('fs_append',args)],APPEND_CONTEXT)['status'],'pass')
            events[2]['params']['update']['content'][0]['newText']=old+args['text']
            self.audit_fails(events,[decision('fs_append',args)],'tool_identity_or_arguments_unproven',APPEND_CONTEXT)

    def test_append_preview_cannot_change_path_or_add_unproven_fields(self):
        args={'path':r'D:\workspace\target\allowed.txt','text':'input'}
        mutations=[lambda e:e[0]['params']['update']['rawInput'].update(path='outside/private.txt'),
            lambda e:e[0]['params']['update']['rawInput'].update(unproven=True),
            lambda e:e[1]['params']['update']['rawInput'].update(text='different'),
            lambda e:e[1]['params']['update']['rawInput'].update(path='outside/private.txt'),
            lambda e:e[-1]['params']['update']['rawInput'].update(extra='unproven'),
            lambda e:e[0]['params']['update'].update(rawOutput={'message':'early output'}),
            lambda e:e[1]['params']['update'].update(content=[{'type':'text','text':'early output'}]),
            lambda e:e[0]['params']['update'].update(errorMessage='early output'),
            lambda e:e[1]['params']['update']['_meta']['kiro']['preview'].update(originalContent='early output'),
            lambda e:e[1]['params']['update'].update(locations=[{'path':'outside/private.txt'}]),
            lambda e:e[0]['params']['update'].update(rawInput=deepcopy(args)),
            lambda e:e[1]['params']['update'].update(status='completed'),
            lambda e:e[2]['params']['update'].update(locations=[{'path':r'D:\outside\private.txt'}]),
            lambda e:e[2]['params']['update']['_meta']['kiro']['preview'].update(file=r'D:\outside\private.txt'),
            lambda e:e[2]['params']['update']['_meta']['kiro']['preview'].update(originalContent='private'),
            lambda e:e[2]['params']['update']['content'][0].update(path='file:///D%3A/outside/private.txt'),
            lambda e:e[2]['params']['update']['_meta']['kiro']['checkpoint'].update(local='file:///D%3A/outside/private.txt'),
            lambda e:e[2]['params']['update']['_meta']['kiro']['preview'].update(original=e[2]['params']['update']['_meta']['kiro']['preview']['original'].replace('originalPath%3Dallowed.txt','originalPath%3Dother%252Fallowed.txt'))]
        for mutate in mutations:
            events=append_call(args);mutate(events)
            with self.subTest(mutation=mutations.index(mutate)):
                self.audit_fails(events,[decision('fs_append',args)],'tool_identity_or_arguments_unproven',APPEND_CONTEXT)
        events=append_call(args)
        self.audit_fails([events[0],events[2]],[decision('fs_append',args)],'tool_identity_or_arguments_unproven',APPEND_CONTEXT)
        self.audit_fails(append_call(args),[decision('fs_append',args)],'tool_identity_or_arguments_unproven')
        for changed in ('kiro-snapshot-v2://unit-session:aaaaaaaa/?originalPath%3Dother%252Fallowed.txt',
                        'kiro-snapshot-v2://other-session:aaaaaaaa/?originalPath%3Dallowed.txt'):
            events=append_call(args);meta=events[2]['params']['update']['_meta']['kiro']
            meta['preview']['original']=changed;meta['checkpoint']['original']=changed
            self.audit_fails(events,[decision('fs_append',args)],'tool_identity_or_arguments_unproven',APPEND_CONTEXT)

    def test_denied_completed_read_list_write_shell_and_mcp_fail(self):
        for title,tool in [('Read File','read_file'),('List Directory','list_directory'),('Write File','fs_write'),
            ('Replace in File','str_replace'),('Run Command','execute_pwsh'),('@kiroclops/get_job','mcp_kiroclops_get_job')]:
            with self.subTest(tool=tool):self.audit_fails(call(title=title),[decision(tool=tool,allow=False)],'denied_tool_executed_or_output_unverified')

    def test_failed_with_data_is_not_prevention(self):
        self.audit_fails(call(status='failed'),[decision(allow=False)],'denied_tool_executed_or_output_unverified')

    def test_generic_failed_empty_without_recognized_provenance_fails(self):
        self.audit_fails(call(status='failed',output={}),[decision(allow=False)],'denied_tool_executed_or_output_unverified')

    def test_strict_source_supported_terminal_only_rejection(self):
        args={'path':'outside/private.txt'};result=audit([hook_event(1),empty_rejection(args=args)],[decision(args=args,allow=False)],CONTEXT)
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(result['calls'][0]['denial_class'],'denied_no_output')
        self.assertIn('not cryptographic',result['native_output_policy']['scope'])

    def test_terminal_only_allowed_call_not_mistaken_for_no_output_denial(self):
        args={'path':'outside/private.txt'}
        self.audit_fails([hook_event(1),empty_rejection(args=args)],[decision(args=args)],'orphan_native_tool_update',CONTEXT)

    def test_unknown_runtime_or_unbound_launcher_requires_requalification(self):
        args={'path':'outside/private.txt'}
        for field in ('cli_version','kas_version','launcher_sha256'):
            context={**CONTEXT,field:'unknown'}
            with self.subTest(field=field):self.audit_fails([hook_event(1),empty_rejection(args=args)],
                [decision(args=args,allow=False)],'denied_tool_executed_or_output_unverified',context)

    def test_no_output_denial_requires_real_configured_hook_completion(self):
        args={'path':'outside/private.txt'}
        for events in ([empty_rejection(args=args)],[hook_event(1),hook_event(1),empty_rejection(args=args)]):
            self.audit_fails(events,[decision(args=args,allow=False)],'denied_tool_executed_or_output_unverified',CONTEXT)

    def test_no_output_denial_requires_pre_tool_origin(self):
        args={'path':'outside/private.txt'};row=decision(args=args,allow=False);del row['audit_origin']
        self.audit_fails([hook_event(1),empty_rejection(args=args)],[row],'denied_tool_executed_or_output_unverified',CONTEXT)

    def test_no_output_rejection_rejects_all_returned_payload_fields(self):
        args={'path':'outside/private.txt'}
        for key,value in [('rawOutput',{}),('rawOutput',{'message':'secret'}),('content',[]),('content',[{'type':'text','text':'secret'}]),('errorMessage','secret')]:
            e=empty_rejection(args=args);e['params']['update'][key]=value
            with self.subTest(key=key,value=value):self.audit_fails([hook_event(1),e],[decision(args=args,allow=False)],'denied_tool_executed_or_output_unverified',CONTEXT)

    def test_input_only_replace_preview_is_not_claimed_as_file_read(self):
        args={'path':'outside/private.txt','oldStr':'request old','newStr':'request new','replace_all':False}
        first=call(title='Replace in File',args={k:v for k,v in args.items() if k!='replace_all'})[0]
        first['params']['update']['_meta']={'kiro':{'toolOrigin':'default','preview':{
            'file':args['path'],'originalContent':args['oldStr'],'modifiedContent':args['newStr']}}}
        events=[first,hook_event(1),empty_rejection(title='Replace in File',args=args)]
        rows=[decision('str_replace',args,False)]
        self.assertEqual(audit(events,rows,CONTEXT)['status'],'pass')
        first['params']['update']['_meta']['kiro']['preview']['originalContent']='ACTUAL PRIVATE CONTENT'
        self.audit_fails(events,rows,'denied_tool_executed_or_output_unverified',CONTEXT)

    def test_prior_returned_content_cannot_be_hidden_by_terminal_rejection(self):
        args={'path':'outside/private.txt'};start=call(args=args)[0]
        start['params']['update']['content']=[{'type':'text','text':'secret'}]
        self.audit_fails([start,hook_event(1),empty_rejection(args=args)], [decision(args=args,allow=False)],'denied_tool_executed_or_output_unverified',CONTEXT)

    def test_repeated_identical_polls_same_decisions_form_exact_multiset(self):
        args={'job_id':'unit-job','wait_seconds':1};events=[];rows=[]
        for n in range(4):events+=call(str(n),'@kiroclops/get_job',args);rows.append(decision('mcp_kiroclops_get_job',args))
        result=audit(events,rows);self.assertEqual(result['status'],'pass',result);self.assertEqual(len(result['calls']),4)

    def test_repeated_same_args_mixed_effect_or_reason_is_ambiguous(self):
        events=call('a')+call('b')
        for second in (decision(allow=False),decision(reason='different scope'),decision(effect='deny')):
            self.audit_fails(events,[decision(),second],'missing_or_ambiguous_tool_decision')

    def test_repeated_cardinality_missing_and_extra_fail(self):
        events=call('a')+call('b')
        for rows in ([decision()],[decision(),decision(),decision()]):self.audit_fails(events,rows,'missing_or_ambiguous_tool_decision')

    def test_unknown_or_mutated_arguments_not_discarded(self):
        rows=[decision(args={'path':'target/allowed.txt','arbitrary':'difference'})]
        self.audit_fails(call(),rows,'missing_or_ambiguous_tool_decision')
        events=call();events[-1]['params']['update']['rawInput']['path']='different.txt'
        self.audit_fails(events,[decision()],'tool_identity_or_arguments_unproven')

    def test_exact_native_call_ids_and_permission_origins(self):
        rows=[decision(tool_call_id='call-1'),decision(audit_origin='permission_reply',tool_call_id='call-1')]
        self.assertEqual(audit(call(),rows)['status'],'pass')
        rows[-1]['tool_call_id']='wrong'
        self.audit_fails(call(),rows,'unbound_permission_decision')

    def test_permission_cannot_replace_missing_pre_tool_decision(self):
        self.audit_fails(call(),[decision(audit_origin='permission_reply',tool_call_id='call-1')],'missing_or_ambiguous_tool_decision')

    def test_duplicate_and_wrong_explicit_call_ids_fail(self):
        for rows in ([decision(tool_call_id='wrong')],[decision(tool_call_id='call-1'),decision(tool_call_id='call-1')]):
            self.assertEqual(audit(call(),rows)['status'],'fail')

    def test_direct_guard_inside_launch_is_not_native_hook(self):
        self.audit_fails(call(),[decision(audit_origin='direct_guard')],'unknown_tool_decision_origin')

    def test_postlaunch_diagnostic_guard_replay_is_separately_excluded(self):
        result=audit(call(),[decision(),decision(at=21,audit_origin='direct_guard')]);self.assertEqual(result['status'],'pass')
        self.assertEqual(result['out_of_interval_decision_count'],1)

    def test_missing_and_nonfinite_decision_time_fails(self):
        for at in (None,True,float('nan'),float('inf')):self.audit_fails(call(),[decision(at=at)],'unbound_tool_decision')

    def test_argument_normalization_keeps_null_mcp_values_and_binds_metadata(self):
        args={'job_id':'j','wait_seconds':None};events=call(title='@kiroclops/get_job',args=args)
        for e in events:e['params']['update']['rawInput']['_meta']={'_isValid':True,'_activePath':[],'_completedPaths':[]}
        self.assertEqual(audit(events,[decision('mcp_kiroclops_get_job',args)])['status'],'pass')
        self.audit_fails(events,[decision('mcp_kiroclops_get_job',{'job_id':'j'})],'missing_or_ambiguous_tool_decision')
        events[0]['params']['update']['rawInput']['_meta']['hidden']='must not be ignored'
        self.audit_fails(events,[decision('mcp_kiroclops_get_job',args)],'tool_identity_or_arguments_unproven')

    def test_known_optional_null_and_defaults_but_no_value_rewriting(self):
        args={'command':'unit-command','cwd':'unit','timeout':None,'run_in_background':False}
        events=call(title='Run Command',args={k:v for k,v in args.items() if k!='timeout'})
        self.assertEqual(audit(events,[decision('execute_pwsh',args)])['status'],'pass')
        self.audit_fails(events,[decision('execute_pwsh',{**args,'timeout':100})],'missing_or_ambiguous_tool_decision')

    def test_cloud_startup_has_strict_no_payload_exclusion(self):
        cloud=[event({'sessionUpdate':'tool_call','toolCallId':'cloud','title':'Fetching your cloud config','kind':'other',
            'status':'in_progress','_meta':{'kiro':{'toolId':'fetch_cloud_config'}}}),
            event({'sessionUpdate':'tool_call_update','toolCallId':'cloud','status':'completed'})]
        result=audit(cloud+call(),[decision()]);self.assertEqual(result['status'],'pass')
        self.assertEqual(len(result['excluded_initialization']),1)
        cloud[-1]['params']['update']['rawOutput']={'secret':'not startup metadata'}
        self.audit_fails(cloud+call(),[decision()],'invalid_initialization_tool')

    def test_cloud_start_only_before_generation_has_no_returned_content(self):
        start=event({'sessionUpdate':'tool_call','toolCallId':'cloud','title':'Fetching your cloud config','kind':'other',
            'status':'in_progress','_meta':{'kiro':{'toolId':'fetch_cloud_config'}}})
        result=audit([start]+call(),[decision()])
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['excluded_initialization'][0]['completion_observed'],False)
        start['params']['update']['content']=[{'type':'text','text':'unexpected data'}]
        self.audit_fails([start]+call(),[decision()],'invalid_initialization_tool')

    def test_cloud_disabled_result_has_only_the_observed_public_shape(self):
        start=event({'sessionUpdate':'tool_call','toolCallId':'cloud','title':'Fetching your cloud config',
            'kind':'other','status':'in_progress','_meta':{'kiro':{'toolId':'fetch_cloud_config'}}})
        end=event({'sessionUpdate':'tool_call_update','toolCallId':'cloud','status':'completed',
            'rawOutput':{'kind':'notEnabled','retracted':False}})
        result=audit([start,end]+call(),[decision()])
        self.assertEqual(result['status'],'pass')
        self.assertTrue(result['excluded_initialization'][0]['completion_observed'])
        end['params']['update']['rawOutput']['account_data']='not allowed'
        self.audit_fails([start,end]+call(),[decision()],'invalid_initialization_tool')

    def test_second_cloud_start_is_not_silently_excluded(self):
        def start(identity):return event({'sessionUpdate':'tool_call','toolCallId':identity,
            'title':'Fetching your cloud config','kind':'other','status':'in_progress',
            '_meta':{'kiro':{'toolId':'fetch_cloud_config'}}})
        self.audit_fails([start('first'),start('second')]+call(),[decision()],'invalid_initialization_tool')

    def test_cloud_not_excluded_after_generation(self):
        start=event({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'started'}})
        cloud=event({'sessionUpdate':'tool_call','toolCallId':'cloud','title':'Fetching your cloud config','kind':'other',
            'status':'completed','_meta':{'kiro':{'toolId':'fetch_cloud_config'}}})
        self.audit_fails([start,cloud],[],'invalid_initialization_tool')

    def test_live_audit_makes_outcome_failure_mandatory(self):
        # Use independently synthetic prefix receipts, not historical session paths.
        from tests.test_local_native_prefix import NativePrefixTests
        fixture=NativePrefixTests('test_no_buffered_tail_remains_compatible');fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(review,'audit_native_tool_outcomes',return_value={'status':'fail','errors':[{'code':'denied_tool_executed_or_output_unverified'}]}):
            with self.assertRaisesRegex(VisualError,'required boundary'):fixture.audit()


if __name__=='__main__':unittest.main()
