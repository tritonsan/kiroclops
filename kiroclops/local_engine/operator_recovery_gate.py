"""Fail-closed admission for an explicitly interrupted, unevaluated prefix.

These receipts are operator-owned reproducibility evidence. A recorded prefix
cannot prove the absence of unrecorded activity or recover a lost OS identity.
"""
from datetime import datetime
import hashlib
import json
import math
import ntpath
from pathlib import Path

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json
from . import review
from .session import Session, source_hashes, JOB_BINDING_FIELDS, LIMITS

CHECKS={'whole_recorded_prefix_inspected','no_validation_exposure','no_operator_coaching',
        'only_expected_prepare_and_target_reads','no_unrecorded_owned_activity_observed'}


def _need(value,message):
    if not value:raise VisualError('recovery_not_eligible',message)


def _stream(path):
    _need(path.is_file() and path.stat().st_size<=32*1024*1024,'Missing or oversized recorded prefix.')
    text=path.read_text(encoding='utf-8')
    _need(text.endswith('\n'),'Incomplete recorded prefix row.')
    rows=[json.loads(line) for line in text.splitlines()]
    _need(rows and all(isinstance(r,dict) for r in rows),'Malformed recorded prefix.')
    return rows


def _builtin_call(call,updates,target,hashes):
    """Narrow actual 2.27 read/list envelopes; no hidden extra fields."""
    tool=call['tool'];read=tool=='read_file'
    shapes=[{'_meta','kind','rawInput','sessionUpdate','status','title','toolCallId'},
            {'_meta','rawInput','sessionUpdate','status','toolCallId'},
            {'_meta','content','rawInput','rawOutput','sessionUpdate','status','title','toolCallId'}]
    if read:shapes=[s|{'locations'} for s in shapes]
    _need(len(updates)==3 and all(set(u)==s for u,s in zip(updates,shapes)), 'Unknown target read/list envelope.')
    title='Read File' if read else 'List Directory';origin='default' if read else 'acp'
    _need([u['sessionUpdate'] for u in updates]==['tool_call','tool_call_update','tool_call_update']
          and [u['status'] for u in updates]==['pending','in_progress','completed']
          and updates[0]['kind']==('read' if read else 'search')
          and updates[0]['title']==updates[2]['title']==title
          and all(u['_meta']=={'kiro':{'toolOrigin':origin}} for u in updates), 'Unknown target read/list lifecycle.')
    raw=updates[0]['rawInput'];args=review._native_arguments(tool,raw)
    _need(all(u['rawInput']==raw for u in updates) and args is not None,'Target read/list arguments changed.')
    path=review._native_windows_path(args.get('path'),target);root=review._native_windows_path(target)
    _need(path is not None and root is not None,'Invalid target read/list path.')
    if read:
        names={name.casefold() for name in hashes}
        _need(set(args)=={'path'} and ntpath.commonpath((root,path))==root
              and len(names)==len(hashes)
              and ntpath.relpath(path,root).replace('\\','/').casefold() in names,'Read is not an unchanged target source.')
        _need(all(u['locations']==[{'path':raw['path']}] for u in updates),'Read locations differ from bound target.')
    else:
        _need(set(args)=={'path','depth'} and args['depth']==2 and path==root,'List must name the exact target root at depth two.')
    last=updates[-1];output=last['rawOutput']
    _need(isinstance(output,dict) and set(output)=={'message'} and isinstance(output['message'],str)
          and last['content']==[{'type':'content','content':{'type':'text','text':output['message']}}], 'Unknown target read/list output.')


def inspect_eligibility(previous):
    """Read-only unsigned evidence; never manufactures an exposure attestation."""
    previous=Path(previous).resolve();state=Session(previous)._load()
    incident=read_json(previous/'operator-interruption.json');cleanup=read_json(previous/'operator-terminal-cleanup.json')
    launch=read_json(previous/'kiro/launch.json');config=review._load_native_config_for_audit(previous)
    request=cleanup.get('request',{})
    _need(incident.get('kind')=='OperatorInterruptedTrial' and incident.get('status')=='operator_interrupted'
          and incident.get('session_id')==state['session_id'] and incident.get('behavior_outcome')=='unevaluated'
          and incident.get('cleanup_sha256')==file_sha256(previous/'operator-terminal-cleanup.json')
          and incident.get('launch_snapshot_sha256')==file_sha256(previous/'kiro/launch.json')
          and cleanup.get('kind')=='TerminalTrialCleanup' and cleanup.get('status')=='pass'
          and cleanup.get('worker_exit_verified') is True and cleanup.get('capture_lease_released') is True
          and request.get('session_id')==state['session_id'] and request.get('capture_lock')==state['capture_lock']
          and request.get('operation')=='prepare' and request.get('preserved_finalization') is False
          and launch.get('run_kind')=='multi_reference_repair',
          'Exact interrupted launch and prepare cleanup are required.')
    _need(not state.get('candidates') and state.get('sealed_at') is None and state.get('final_job_id') is None
          and state.get('selected_candidate_id') is None and state.get('final') is None
          and (state.get('prepared') is None or state.get('prepared') is False),
          'Evaluated, prepared or sealed trials cannot be relabelled as unevaluated.')
    _need(state['budget']==LIMITS and source_hashes(state['target'])==state['baseline_hashes']
          and source_hashes(state['baseline'])==state['baseline_hashes'],'Source or authoring budget changed.')
    jobs=list((previous/'jobs').glob('*/job.json'))
    _need(len(jobs)==1,'Only the one interrupted prepare job is eligible.')
    job=read_json(jobs[0]);binding=read_json(jobs[0].parent/'binding.json').get('sha256');worker=request.get('worker')
    _need(isinstance(worker,dict) and set(worker)=={'pid','identity'} and type(worker['pid']) is int and worker['pid']>0
          and isinstance(worker['identity'],str) and worker['identity'] and job.get('worker')==worker,
          'Recorded worker creation identity is required.')
    _need(job.get('job_id')==request.get('job_id')==state.get('prepare_job_id')
          and job.get('operation')=='prepare' and job.get('status')=='cancelled'
          and job.get('candidate_id') is None and job.get('result') is None
          and binding==request.get('job_binding_sha256')==digest({k:job.get(k) for k in JOB_BINDING_FIELDS})
          and job.get('source_hashes')==state['baseline_hashes'] and job.get('source_sha256')==digest(state['baseline_hashes'])
          and Path(job['source_path']).resolve()==Path(state['baseline']).resolve()
          and not (jobs[0].parent/'result.json').exists(),'Cancelled prepare job/source binding differs or contains a result.')
    inbound=_stream(previous/'kiro/run/stdout.jsonl');outbound=_stream(previous/'kiro/run/outbound.jsonl')
    decisions=_stream(previous/'kiro/tool-audit.jsonl');end=cleanup.get('finished_at');start=launch.get('started_at')
    _need(all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) and start<end,'Interrupted prefix timing is unbound.')
    _need(len(outbound)%2==0,'Incomplete native write-ahead prefix.')
    messages=[];last=start
    for index in range(0,len(outbound),2):
        attempt,sent=outbound[index:index+2];message=attempt.get('message')
        sha=hashlib.sha256(json.dumps(message,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        _need(set(attempt)=={'at','message','payload_sha256','phase','sequence'} and set(sent)=={'at','payload_sha256','phase','sequence'}
              and attempt['phase']=='attempt' and sent['phase']=='sent' and attempt['sequence']==sent['sequence']==index//2+1
              and attempt['payload_sha256']==sent['payload_sha256']==sha,'Native prefix send receipt changed.')
        a=datetime.fromisoformat(attempt['at'].replace('Z','+00:00')).timestamp();b=datetime.fromisoformat(sent['at'].replace('Z','+00:00')).timestamp()
        _need(last<=a<=b<=end,'Native prefix chronology changed.');last=b;messages.append(message)
    controls=['initialize','session/new','session/set_mode','session/set_config_option','session/set_config_option','_kiro/hooks/list','session/prompt']
    _need([m.get('method') for m in messages]==controls and all(m.get('jsonrpc')=='2.0' for m in messages)
          and len({(type(m.get('id')).__name__,str(m.get('id'))) for m in messages})==len(messages),'Expected one task prompt, exact controls and no addendum.')
    sid=launch.get('preflight',{}).get('session_id');prompt=Path(config['prompt_path']).read_text(encoding='utf-8')
    preflight=launch.get('preflight',{});effort_id=(preflight.get('effort_options') or {}).get('config_id')
    _need(messages[0]['params']=={'protocolVersion':1,'clientCapabilities':{'_meta':{'kiro':{'hooks':{'enabled':True,'v2':True}}}},
                                 'clientInfo':{'name':'kiroclops-local','version':'1'}}
          and messages[1]['params']=={'cwd':config['target_dir'],'mcpServers':[]}
          and messages[2]['params']=={'sessionId':sid,'modeId':config.get('agent_name')}
          and messages[3]['params']=={'sessionId':sid,'configId':'model','value':config.get('model')}
          and isinstance(effort_id,str) and effort_id
          and messages[4]['params']=={'sessionId':sid,'configId':effort_id,'value':config.get('effort')}
          and messages[5]['params']=={'sessionId':sid,'workspacePaths':[str(Path(config['target_dir']).resolve())],'trigger':'PreToolUse'},
          'Native controls differ from the frozen launch configuration.')
    _need(isinstance(sid,str) and sid and messages[-1]['params']=={'sessionId':sid,'prompt':[{'type':'text','text':prompt}]}
          and Path(messages[1]['params']['cwd']).resolve()==Path(state['target']).resolve()
          and not any('method' not in r and r.get('id')==messages[-1]['id'] for r in inbound), 'Prompt differs or a completed turn exists.')
    stderr=previous/'kiro/run/stderr.log';lines=stderr.read_text(encoding='utf-8').splitlines()
    versions=[json.loads(l.split('kas.server.starting ',1)[1]).get('version') for l in lines if l.startswith('[INFO] kas.server.starting ')]
    _need(versions==['0.66.22'] and launch.get('cli_version')=='kiro-cli-chat 2.27.0','Runtime prefix version is unsupported or ambiguous.')
    context={'cli_version':launch['cli_version'],'kas_version':versions[0],'launcher_sha256':config['launcher_sha256'],
             'target_dir':config['target_dir'],'hook_id':str(Path(config['target_dir'])/'.kiro/hooks/kiroclops-guard.json')+'#hook-0'}
    native=review.audit_native_tool_outcomes(inbound,decisions,session_id=sid,started_at=start,finished_at=end,
                                           scope_sha256=digest(config['generated_hashes']),runtime_context=context)
    _need(native['status']=='pass' and native['hook_cardinality_verified'] and native['out_of_interval_decision_count']==0
          and len(native['calls'])==8 and native['observed_tool_count']==8
          and all(c['decision_allow'] and c['status']=='completed' for c in native['calls']), 'Every native call must complete and bind a canonical allow decision.')
    expected={'tool_load':1,'@kiroclops/inspect_session':1,'@kiroclops/prepare_experiment':1,'list_directory':1,'read_file':4}
    _need({t:sum(c['tool']==t for c in native['calls']) for t in expected}==expected,'The prefix is not the supported harmless eight-call prepare/read prefix.')
    for call in native['calls']:
        updates=[inbound[i-1]['params']['update'] for i in call['lines']]
        raw=updates[0]['rawInput'];expected_args=raw['arguments'] if call['tool'].startswith('@kiroclops/') else raw
        rows=[r for r in decisions if review._native_tool_name(r.get('tool'))==call['tool'] and r.get('arguments')==expected_args]
        _need(len(rows)==1 and rows[0].get('audit_origin')=='pre_tool_use' and rows[0].get('allow') is True,
              'The interrupted prefix requires an exact raw pre-tool decision, without argument normalization.')
        if call['tool'] in ('read_file','list_directory'):_builtin_call(call,updates,config['target_dir'],state['baseline_hashes'])
        elif call['tool'].startswith('@kiroclops/'):
            _need(updates[0]['rawInput']['arguments']=={},'Inspect/prepare arguments are not empty.')
            if call['tool']=='@kiroclops/prepare_experiment':
                _need(json.loads(updates[-1]['rawOutput']['message'])=={'job_id':job['job_id'],'candidate_id':None,'status':'queued'}, 'Public prepare response differs from the cancelled job.')
    names=['session.json','operator-interruption.json','operator-terminal-cleanup.json','kiro/launch.json','kiro/config.json',
           'kiro/prompt.txt','kiro/run/stdout.jsonl','kiro/run/outbound.jsonl','kiro/run/stderr.log','kiro/tool-audit.jsonl',
           'admission.json','environment-readiness.json']
    # The generated prompt filename is authoritative; no assumed filename.
    names.remove('kiro/prompt.txt');names.append(Path(config['prompt_path']).resolve().relative_to(previous).as_posix())
    names.extend(p.relative_to(previous).as_posix() for p in jobs[0].parent.iterdir() if p.is_file() and p.name in ('job.json','binding.json','cancel.json','worker.log'))
    suite=read_json(Path(state['reference'])/'suite.json')
    _need(suite.get('authoring_parent_source_hashes')==state['baseline_hashes'] and digest(suite)==state['experiment']['suite_sha256'], 'Frozen suite or authoring parent changed.')
    hashes={name:file_sha256(previous/name) for name in sorted(set(names))}
    root=Path(__file__).resolve().parents[2]
    code={p.name:file_sha256(p) for p in (Path(__file__),Path(review.__file__),
          root/'scripts/phase3d_operator_recovery.py',root/'artifacts/phase3d/native-progressive-tool-source-proof.json')}
    return {'kind':'UnsignedOperatorRecoveryEligibilityEvidence','schema_version':1,'previous_session':str(previous),
            'previous_session_id':state['session_id'],'source_sha256':digest(state['baseline_hashes']),
            'suite_sha256':digest(suite),'evidence_hashes':hashes,'gate_code_hashes':code,'native_prefix':native,
            'worker':worker,'launcher_process_identity_status':'recorded' if request.get('client_process') else 'not_recorded',
            'launcher_exit_proof':'recorded_creation_identity' if request.get('client_process') else 'operator_report_only',
            'exposure_status':'requires_operator_attestation','behavior_outcome':'unevaluated','behavior_acceptance':False}


def validate_eligibility(previous,attestation):
    scope=inspect_eligibility(previous);receipt=read_json(attestation)
    _need(receipt.get('kind')=='OperatorRecoveryExposureAttestation' and receipt.get('schema_version')==1
          and receipt.get('status')=='pass' and receipt.get('method')=='manual_whole_recorded_prefix_review'
          and isinstance(receipt.get('reviewer'),str) and receipt['reviewer'].strip()
          and receipt.get('scope_sha256')==digest(scope) and receipt.get('evidence_hashes')==scope['evidence_hashes']
          and receipt.get('explicit_fresh_registration_authorized') is True
          and receipt.get('launcher_process_identity_status')==scope['launcher_process_identity_status']
          and receipt.get('launcher_exit_proof')==scope['launcher_exit_proof'], 'Explicit current operator exposure attestation is missing or stale.')
    checks=receipt.get('checks',[])
    _need(isinstance(checks,list) and len(checks)==len(CHECKS) and {c.get('id') for c in checks if isinstance(c,dict)}==CHECKS
          and all(c.get('verdict')=='pass' and isinstance(c.get('evidence'),str) and c['evidence'].strip() for c in checks), 'Every actual exposure/coaching review check is required.')
    residual=receipt.get('residual_owned_processes',{})
    _need(isinstance(residual,dict) and residual.get('assessment')=='none_observed' and isinstance(residual.get('evidence'),str) and residual['evidence'].strip()
          and isinstance(residual.get('scope'),str) and residual['scope'].strip(), 'Record the actual residual-process observation and its limits.')
    return scope,receipt


def replay_cleanup(previous):
    """Recheck only the exact recorded worker/lease; never terminate or spawn."""
    from .trial_cleanup import finish_terminal_cleanup
    previous=Path(previous).resolve();cleanup=read_json(previous/'operator-terminal-cleanup.json')
    result=finish_terminal_cleanup(Session(previous),cleanup['request'],timeout=.25,terminate=False)
    _need(result.get('status')=='pass' and result.get('worker_exit_verified') is True
          and result.get('capture_lease_released') is True and result.get('termination_requested') is False,
          'Exact recorded worker or capture lease is not released now.')
    # _alive(None) is not proof of a launcher exit; keep that fact explicit.
    result['launcher_process_identity_status']='recorded' if cleanup['request'].get('client_process') else 'not_recorded'
    result['launcher_exit_proof']='recorded_creation_identity' if cleanup['request'].get('client_process') else 'operator_report_only'
    return result
