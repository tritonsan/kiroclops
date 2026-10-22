"""Recorded native Kiro CLI trials, with symmetric ordinary tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from kiroclops.integration import kiro as native
from kiroclops.portable_v2.launcher import ModelACPClient as NativeClient
from kiroclops.visual.schema import VisualError, read_json, write_json, file_sha256, digest
from .session import Session, editable
from .execution_policy import policy_from_state, PassiveNativeActivity, REVISED_ID

MODEL, EFFORT = 'claude-opus-5.5', 'max'
APPROVAL = 'native_session_allow_always'
ROOT = Path(__file__).resolve().parents[2]
MCP_TOOLS = native.MCP_TOOLS
# V6 plan WP3.2: the session server also exposes inspect_window. MCP_TOOLS stays the
# frozen seven, because the pinned native reader imports it to replay history.
from .mcp_tools import ACTIVE_MCP_TOOLS


def configure(directory):
    directory = Path(directory).resolve()
    state = read_json(directory/'session.json')
    if state['status'] != 'new' or (directory/'kiro/config.json').exists():
        raise VisualError('not_fresh', 'Configure only a fresh experiment')
    target = Path(state['target'])
    execution_policy = policy_from_state(state)
    name = 'kiroclops-local-' + state['mode']
    controls = target/'.kiro'
    if controls.exists() and any(controls.iterdir()):
        raise VisualError('existing_config', 'Do not overwrite existing Kiro controls')
    server = {'command': sys_python(), 'args': ['-m','kiroclops.local_engine.mcp','--session',str(directory)],
              'env': {'PYTHONPATH':str(ROOT),'PYTHONUTF8':'1'}, 'timeout':30000}
    backend_mode=os.environ.get('KIROCLOPS_ANALYSIS_BACKEND','local')
    if backend_mode not in ('local','remote'):raise VisualError('invalid_backend','Select local or remote explicitly')
    server['env']['KIROCLOPS_ANALYSIS_BACKEND']=backend_mode
    suite_mode=state.get('experiment',{}).get('reference_mode')=='media_suite'
    if suite_mode:
        if backend_mode!='local':raise VisualError('unsupported_backend','Behavior repair stages run locally.')
        from .media_suite import runtime_environment
        server['env'].update(runtime_environment(state['experiment']))
    analysis_execution={'backend':backend_mode}
    if backend_mode=='remote':
        service_config=os.environ.get('KIROCLOPS_CLOUD_CONFIG')
        if not service_config:raise VisualError('remote_not_configured','Cloud config is required before preparing the Kiro profile')
        server['env']['KIROCLOPS_CLOUD_CONFIG']=str(Path(service_config).resolve())
        analysis_execution.update(config_path=str(Path(service_config).resolve()),config_sha256=file_sha256(service_config))
    command = native._shell_command([sys_python(),'-m','kiroclops.local_engine.guard_diagnostics','--session',str(directory),'--guard'])
    neutral = {op:('& ' if os.name=='nt' else '')+native._shell_command(
                   [sys_python(),'-m','kiroclops.local_engine','trial',op,'--session',str(directory)]
                   + (['--async'] if op in ('prepare','evaluate','finalize','report') else []))
               for op in ('inspect','prepare','evaluate','finalize','report','job','cancel')}
    limits = (f'You have at most {execution_policy["max_candidates"]} submitted candidates and '
              f'{execution_policy["max_seconds"]} seconds of model authoring per stage. '
              'The clock starts with this task and includes waiting for preparation and candidate evaluation. '
              f'Aim to submit candidate 01 by about minute {execution_policy["max_seconds"] // 120} so candidate '
              'evaluation and sealing can finish in time. '
              f'The runner has a separate {execution_policy["max_validation_seconds"]}-second validation budget '
              'after sealing; it never extends the authoring clock. '
              'Failed builds count. Author the actual code yourself; never replay reference coordinates. '
              'Do not use capture/scenario IDs or test times in application code. Preserve the stated existing behavior. '
              'Use only this fresh workspace and supplied calibration media. Never inspect previous solutions, '
              'reference source, private evaluation files or held-out evidence. You may use standard file, image '
              'and terminal tools, and write your own analysis helpers under .kiroclops-scratch/. '
              'Editable application scope is shown below. All other application assets are protected. '
              'After finalization do not change code or return to calibration. '
              'Use the provided candidate evaluation as the first application run check; do not spend the early '
              'authoring window locating an engine binary or building a separate engine harness. '
              'Once the finalization submission returns a job ID, end this turn promptly. '
              'The local runner waits for that exact finalization job and builds the evidence report after your turn ends; '
              'do not start a report job yourself. '
              'No follow-up coaching will be sent.')
    workflow = ('First inspect_session and prepare_experiment. Treat the returned measured profile, units and confidence '
                'as the starting hypothesis for your first real code edit. Submit candidate 01 to evaluate_candidate early, '
                'wait using get_job, and use calibration feedback for corrections instead of fully re-tracking and re-fitting '
                'the reference before the first evaluation. After an evaluation, call inspect_window(job_id, case_id, object_id) '
                'for a failing window: it writes reference/candidate/difference PNG strips and an error-over-time plot under '
                '.kiroclops-evidence/inspect inside this workspace; open them with your image reader before editing. '
                'evaluate_candidate(case_ids=[...]) re-measures only the named calibration cases as a partial probe; it is faster, '
                'does not consume a candidate, but can never be finalized. Finalize the lowest-error eligible '
                'candidate, then end your turn after its job ID is returned. The runner waits for the final result '
                'and builds the report afterward. '
                'Tools never generate a patch.') if state['mode']=='tools' else (
                'Use the neutral terminal commands below to prepare raw reference/before recordings, capture candidates, '
                'select the candidate you prefer and finish. Calibration commands give no motion profile or numeric guidance. '
                'You can analyze the raw images with your ordinary tools and your own code. Submit your first candidate '
                'early enough to leave time for feedback. Call prepare once, evaluate '
                'after each edit (maximum three), finalize your selected candidate, then end your turn after its job ID '
                'is returned. The runner waits for the final result and builds the report afterward. '
                'Submissions return a job ID immediately. '
                'For preparation and evaluation, poll the job command with --job-id ID --wait-seconds 10 until it completes; '
                'the result contains the raw media paths. '
                'Use inspect for session status or cancel with --job-id ID to stop your own job. '
                'Append --candidate candidate-01 (or 02/03) to finalize to select a captured candidate. '
                'Do not resubmit a running operation just because its result is pending. Do not import or invoke '
                'KiroClops internals: this arm measures what ordinary Kiro can do with its own analysis.')
    scope = Session(directory).inspect().get('editable_scope', [])
    instructions = f'{limits}\n\n{workflow}\n\nTask: {state["goal"]}\nEditable: {scope}\n'
    if state['mode']=='baseline':
        instructions += '\nNeutral commands:\n'+'\n'.join(f'{k}: {v}' for k,v in neutral.items())+'\n'
    media_mode=state.get('experiment',{}).get('reference_mode')=='media_bundle'
    if media_mode:
        instructions += ('\nThis is a user-supplied reference clip, not a preregistered benchmark. '
            'Its declared inputs are conditional evidence, not verified interaction rules. '
            'The supplied clip has no held-out behavior: finish with the explicit partial acceptance result. '
            'Do not invent missing inputs, claim generalization, or weaken the contract.\n')
    if suite_mode:
        instructions += ('\nThis is a frozen multi-clip ReferenceMediaSuite. Inspect reference coverage before editing. '
            'Use every calibration example together. Improvement is measured against the original unchanged target, '
            'while your authoring parent is an earlier successful candidate with separate preservation checks. '
            'If required calibration evidence is missing, close with needs_reference; do not invent inputs or '
            'expand the pool after this task starts. Finalization opens fresh validation and known regressions separately.\n')
    task_kind='multi-reference behavior repair stage' if suite_mode else 'user-provided local video task' if media_mode else 'registered local experiment'
    prompt = (f'Complete this {task_kind} now. {state["goal"]}\nModel: {MODEL}; effort: {EFFORT}.\n'
              'Follow the selected agent workflow through finalization submission; submit the first candidate early, '
              'then end the turn promptly after finalization. '
              'The local runner waits for the submitted job and packages the report after your turn.\n')
    root = target.as_posix()
    write_patterns = [root+'/.kiroclops-scratch/**']
    if state['domain']=='game':
        write_patterns += [root+'/player.gd',root+'/motion/*.gd']
    elif state['domain'] in ('web','web_panel','panel'):
        write_patterns += [root+'/src/panel-motion.js',root+'/src/motion/*.js']
    else:
        write_patterns += [root+'/src/layout-motion.js',root+'/src/motion/*.js']
    tools = ['read','write','shell'] + (['@kiroclops'] if state['mode']=='tools' else [])
    rules = [{'capability':'fs_read','match':[root+'/**'],'effect':'allow'},
             {'capability':'fs_read','match':['**'],'exclude':[root+'/**'],'effect':'deny'},
             {'capability':'fs_write','match':write_patterns,'effect':'allow'},
             {'capability':'fs_write','match':['**'],'exclude':write_patterns,'effect':'deny'},
             {'capability':'shell','effect':'allow'}]
    if state['mode']=='tools':
        rules.append({'capability':'mcp','match':['kiroclops/'+t for t in ACTIVE_MCP_TOOLS],'effect':'allow'})
    agent = {'name':name,'description':'Local visual behavior trial with ordinary tools preserved.',
             'model':MODEL,'effortLevel':EFFORT,'prompt':instructions,'tools':tools,
             'includeMcpJson':False,'includePowers':False,'resources':[],
             'mcpServers':{'kiroclops':server} if state['mode']=='tools' else {}, 'permissions':{'rules':rules}}
    write_json(controls/f'agents/{name}.json',agent)
    write_json(controls/'hooks/kiroclops-guard.json',{'version':'v1','hooks':[{'name':'Local trial boundary','trigger':'PreToolUse',
               'action':{'type':'command','command':command},'timeout':10}]})
    (controls/'instructions.md').write_text(instructions,encoding='utf-8')
    (controls/'prompt.md').write_text(prompt,encoding='utf-8')
    config = {'session_dir':str(directory),'target_dir':str(target),'agent_name':name,'mode':state['mode'],
              'analysis_execution':analysis_execution,
              'model':MODEL,'effort':EFFORT,'approval_mode':APPROVAL,'agent_path':str(controls/f'agents/{name}.json'),
              'prompt_path':str(controls/'prompt.md'),'neutral_commands':neutral,
              'generated_hashes':{p.relative_to(controls).as_posix():file_sha256(p) for p in controls.rglob('*') if p.is_file()},
              'launcher_sha256':file_sha256(__file__),
              'guard_sources':{'guard_diagnostics.py':file_sha256(Path(__file__).with_name('guard_diagnostics.py'))}}
    if state.get('experiment', {}).get('execution_policy') is not None:
        config['execution_policy'] = execution_policy
    write_json(directory/'kiro/config.json',config)
    Session(directory).bind_controls()
    return config


def sys_python():
    import sys
    return sys.executable


def load_config(directory):
    config = read_json(Path(directory)/'kiro/config.json')
    state = Session(directory)._load()
    execution_policy = policy_from_state(state)
    if (state.get('experiment', {}).get('execution_policy') is not None
            and config.get('execution_policy') != execution_policy):
        raise VisualError('config_changed', 'Prepared launcher execution policy changed.')
    execution=config.get('analysis_execution',{'backend':'local'})
    if execution['backend']=='remote' and file_sha256(execution['config_path'])!=execution['config_sha256']:
        raise VisualError('config_changed','Pinned cloud service configuration changed')
    if (config.get('model'),config.get('effort'),config.get('approval_mode')) != (MODEL,EFFORT,APPROVAL):
        raise VisualError('model_changed',f'Exact {MODEL}/{EFFORT} and native Always Allow required')
    if config['launcher_sha256'] != file_sha256(__file__):
        raise VisualError('launcher_changed','Launcher differs from prepared trial')
    for relative, expected in config.get('guard_sources', {}).items():
        if relative != 'guard_diagnostics.py' or file_sha256(Path(__file__).with_name(relative)) != expected:
            raise VisualError('guard_source_changed','Guard bootstrap differs from prepared trial')
    hook = Path(config['target_dir'])/'.kiro/hooks/kiroclops-guard.json'
    if ('kiroclops.local_engine.guard_diagnostics' in hook.read_text(encoding='utf8')
            and set(config.get('guard_sources', {})) != {'guard_diagnostics.py'}):
        raise VisualError('guard_source_changed','Guard bootstrap has no prepared source binding')
    controls = Path(config['target_dir'])/'.kiro'
    for relative, expected in config['generated_hashes'].items():
        if file_sha256(controls/relative) != expected:
            raise VisualError('config_changed','Prepared agent controls changed')
    return config


def _neutral_command_allowed(command, commands):
    """Exact prepared commands plus bounded, session-owned job arguments."""
    for key, value in commands.items():
        if key in ('job','cancel'):
            pattern=re.escape(value)+r' --job-id [0-9a-f]{32}'
            if key=='job':pattern+=r'(?: --wait-seconds (?:[0-9]|[1-5][0-9]|60))?'
        elif key=='finalize':
            pattern=re.escape(value)+r'(?: --candidate candidate-0[123])?'
        else:
            pattern=re.escape(value)
        if re.fullmatch(pattern,command):return True
    return False


def _append_guard_audit(directory, record):
    """Commit a whole audit row before a hook can allow its tool call.

    Kiro can start several PreToolUse hooks in separate processes at once.
    Serialize their read/append/replace sequence, and use a same-directory
    atomic replacement so an interrupted writer cannot leave a partial row.
    An already damaged audit remains an error rather than being repaired here.
    """
    from .backend import _lock

    audit = Path(directory) / 'kiro/tool-audit.jsonl'
    audit.parent.mkdir(parents=True, exist_ok=True)
    row = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    if len(row) > 1024 * 1024:
        raise VisualError('invalid_native_evidence', 'Tool audit row exceeds the hook bound')
    # The Kiro hook itself times out at ten seconds. Leave time to return an
    # explicit denial if the lease is unexpectedly held or the audit is large.
    with _lock(audit.parent / 'tool-audit.lock', timeout=5):
        if audit.exists() and audit.stat().st_size > 8 * 1024 * 1024:
            raise VisualError('invalid_native_evidence', 'Tool audit exceeds the hook bound')
        previous = audit.read_bytes() if audit.exists() else b''
        if previous and not previous.endswith(b'\n'):
            raise VisualError('invalid_native_evidence', 'Tool audit ends with an incomplete row')
        try:
            if any(not isinstance(json.loads(line), dict) for line in previous.splitlines()):
                raise ValueError('Tool audit contains a non-object row')
        except (UnicodeDecodeError, ValueError) as exc:
            raise VisualError('invalid_native_evidence', 'Tool audit contains an invalid row') from exc
        temporary = audit.with_name(f'.{audit.name}.{uuid.uuid4().hex}.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(previous + row)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, audit)
        finally:
            temporary.unlink(missing_ok=True)


def guard(directory, payload, *, audit_origin='direct_guard', tool_call_id=None, _diagnostics=None):
    from .guard_diagnostics import GuardDiagnostics, error_record

    diagnostic = _diagnostics or GuardDiagnostics(directory, origin=audit_origin, tool_call_id=tool_call_id)
    own_diagnostic = _diagnostics is None
    audit_attempted = False
    origin_valid = False
    try:
        diagnostic.identify_payload(payload)
        if audit_origin not in ('direct_guard','pre_tool_use','permission_reply'):
            raise VisualError('invalid_audit_origin','Unknown guard invocation origin')
        if tool_call_id is not None and (audit_origin!='permission_reply' or not isinstance(tool_call_id,str) or not tool_call_id):
            raise VisualError('invalid_audit_origin','Only an actual permission callback may supply a tool call ID')
        origin_valid = True
        diagnostic.emit('load_config', outcome='started')
        config = load_config(directory)
        state = read_json(Path(directory)/'session.json')
        diagnostic.emit('load_config', outcome='completed')
        diagnostic.emit('guard', outcome='started')
        decision = _guard_policy(directory, config, state, payload)
        diagnostic.emit('guard', outcome='allowed' if decision['allow'] else 'denied')
        audit_attempted = True
        diagnostic.emit('decision_persist', outcome='started', allow=decision['allow'])
        _append_guard_audit(directory, _guard_record(payload, decision, audit_origin, tool_call_id))
        diagnostic.emit('decision_persist', outcome='committed', allow=decision['allow'])
        if own_diagnostic:
            diagnostic.emit('return', outcome='allowed' if decision['allow'] else 'denied',
                            exit_code=0 if decision['allow'] else 2)
        return decision
    except Exception as exc:
        diagnostic.emit('failure', outcome='error_denied', error=error_record(exc))
        # A real denial caused by configuration/policy failure is worth recording.
        # Invalid origins are not promoted to native evidence; failed audit writes
        # are never retried as a different decision or substituted with success.
        if not audit_attempted and origin_valid:
            denial = {'allow': False, 'reason': 'Guard error: ' + error_record(exc)['code'],
                      'tool': payload.get('tool_name', payload.get('toolName', '')) if isinstance(payload, dict) else '',
                      'paths': []}
            diagnostic.emit('decision_persist', outcome='started', allow=False)
            try:
                _append_guard_audit(directory, _guard_record(payload, denial, audit_origin, tool_call_id))
                diagnostic.emit('decision_persist', outcome='committed', allow=False)
            except Exception as audit_exc:
                diagnostic.emit('decision_persist', outcome='failed', allow=False, error=error_record(audit_exc))
        elif audit_attempted:
            diagnostic.emit('decision_persist', outcome='failed', error=error_record(exc))
        if own_diagnostic:
            diagnostic.emit('return', outcome='error_denied', exit_code=2, error=error_record(exc))
        raise
    finally:
        if own_diagnostic:
            diagnostic.finish()


def _guard_record(payload, decision, audit_origin, tool_call_id):
    args = payload.get('tool_input', payload.get('toolInput', {})) if isinstance(payload, dict) else payload
    return {'at':time.time(),'arguments':args,'audit_origin':audit_origin,
            **({'tool_call_id':tool_call_id} if tool_call_id is not None else {}),**decision}


def _parent_traversal(command):
    """True when a shell command names a parent directory as its own token or path component."""
    import re
    return re.search(r'(^|[\s"\'=;&|(/])\.\.($|[\s"\'/;&|)])', command) is not None


def _unrecognised_path_arguments(args):
    """Strings that look like paths but sit under keys ``_path_values`` does not know."""
    def strings(value):
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return [value] if isinstance(value, str) else []
    return [s for s in strings(args) if any(k in s for k in ('/', '\\', ':'))]


def _guard_policy(directory, config, state, payload):
    target = Path(config['target_dir'])
    if not isinstance(payload, dict):
        return {'allow':False,'reason':'Malformed tool event','tool':'','paths':[]}
    tool = payload.get('tool_name',payload.get('toolName',''))
    args = payload.get('tool_input',payload.get('toolInput',{}))
    if not isinstance(tool,str) or not isinstance(args,dict):
        return {'allow':False,'reason':'Malformed tool event','tool':tool,'paths':[]}
    name = tool.removeprefix('@builtin/')
    reads = {'fs_read','read_file','read_files','readFile','list_directory','grep','grep_search','file_search','glob',
             'read_image','read_images','readMultipleFiles','listDirectory','fileSearch','grepSearch'}
    writes = {'fs_write','fs_append','write_file','str_replace','replace_text','append_lines','edit_code','fsWrite','fsAppend','strReplace','editCode'}
    shells = {'execute_bash','execute_pwsh','execute_shell','run_command','shell','executeBash','executePwsh','runCommand'}
    paths = native._path_values(args)
    allowed = False
    if name in reads|writes:
        if not paths and name in reads:
            # V6 plan WP3.4: a read tool whose arguments carry a path under an
            # unrecognised key must not fall open to the workspace root.
            if _unrecognised_path_arguments(args):
                return {'allow':False,'reason':'Unrecognised path argument','tool':tool,'paths':[]}
            paths=['.']
        resolved=[]
        for value in paths:
            if value.startswith('~') or any(k in value for k in ('$', '%', '://')):
                return {'allow':False,'reason':'Use explicit workspace paths','tool':tool,'paths':paths}
            path=Path(value);path=(path if path.is_absolute() else target/path).resolve()
            resolved.append(path)
        allowed=bool(resolved) and all(p.is_relative_to(target) for p in resolved)
        if allowed and name in writes:
            allowed=state.get('sealed_at') is None and all(editable(state['domain'],p.relative_to(target).as_posix())
                     or p.is_relative_to(target/'.kiroclops-scratch') for p in resolved)
    elif name in shells:
        command=args.get('command',args.get('cmd',''))
        # Ordinary shell tools remain available in both arms. This is an
        # auditable cooperative experiment boundary, not a claimed OS sandbox.
        allowed=isinstance(command,str) and bool(command.strip()) and all(
            (Path(v) if Path(v).is_absolute() else target/v).resolve().is_relative_to(target) for v in paths)
        neutral = _neutral_command_allowed(command, config['neutral_commands'])
        if not neutral:
            forbidden=[str(Path(directory)/p).lower().replace('\\','/') for p in ('reference','baseline','measurements','jobs','candidates','kiro')]
            forbidden += [str(state[k]).lower().replace('\\','/') for k in ('reference','baseline')]
            # V6 plan WP3.4: the session's own control files and any parent
            # traversal (``cd ..``, ``..\x``) are outside the boundary too.
            forbidden += ['session.json','binding.json']
            normalized=command.lower().replace('\\','/')
            allowed &= not any(p in normalized for p in forbidden) and '../' not in normalized
            allowed &= not _parent_traversal(normalized)
            allowed &= '.kiro/' not in normalized and 'kiroclops.' not in normalized
            allowed &= state.get('sealed_at') is None
    elif name in ('tool_load','tool_call'):
        ids=args.get('tool_ids',[]) if name=='tool_load' else [args.get('tool_id')]
        permitted={'kiroclops::'+t for t in ACTIVE_MCP_TOOLS}|({'kiroclops::*'} if name=='tool_load' else set())
        allowed=config['mode']=='tools' and isinstance(ids,list) and bool(ids) and all(t in permitted for t in ids)
    else:
        permitted={pattern.format(t) for t in ACTIVE_MCP_TOOLS for pattern in ('@kiroclops/{}','kiroclops/{}','mcp_kiroclops_{}','mcp__kiroclops__{}','kiroclops::{}','kiroclops___{}')}
        allowed=config['mode']=='tools' and name in permitted
    decision={'allow':bool(allowed),'reason':'Allowed local trial operation' if allowed else 'Outside the registered experiment boundary','tool':tool,'paths':paths}
    return decision


def _process_identity(pid):
    from .backend import _identity
    if type(pid) is not int or pid<=0:raise VisualError('owned_identity_missing','Owned process PID is invalid.')
    identity=_identity(pid)
    if not isinstance(identity,str) or not identity:
        raise VisualError('owned_identity_missing','Owned process creation identity is unavailable; do not start the prompt.')
    return {'pid':pid,'identity':identity}


def _write_process_identity(path,value):
    from .backend import _atomic
    path=Path(path)
    if path.exists():raise VisualError('owned_identity_exists','Preserve the earlier owned process receipt.')
    _atomic(path,value)  # Flush/fsync the temporary file before its atomic rename.
    return {'path':str(path.resolve()),'sha256':file_sha256(path)}


def _saved_client_process(client):
    """Return the startup identity; a dead/recycled PID never supplies a replacement."""
    saved=getattr(client,'owned_process_receipt',None)
    if not isinstance(saved,dict) or set(saved)!={'path','sha256'}:
        raise VisualError('owned_identity_missing','Native client has no durable startup identity receipt.')
    path=Path(saved['path'])
    if file_sha256(path)!=saved['sha256']:
        raise VisualError('owned_identity_changed','Native client startup identity receipt changed.')
    receipt=read_json(path);process=receipt.get('native_client_process')
    if (receipt.get('kind')!='OwnedNativeClientStartupIdentity' or receipt.get('status')!='bound'
            or not isinstance(process,dict) or set(process)!={'pid','identity'}
            or type(process['pid']) is not int or process['pid']<=0
            or not isinstance(process['identity'],str) or not process['identity']
            or client.process.pid!=process['pid']):
        raise VisualError('owned_identity_changed','The client differs from its durable startup identity.')
    return dict(process)


def _verify_saved_client_process(client,*,require_alive=False):
    from .backend import _identity
    saved=_saved_client_process(client);current=_identity(saved['pid'])
    if current is not None and current!=saved['identity']:
        raise VisualError('owned_identity_changed','A recycled PID cannot replace the recorded native client.')
    if require_alive and (current is None or client.process.poll() is not None):
        raise VisualError('owned_client_not_alive','The bound native client exited before the task prompt.')
    return saved


class LocalACPClient(NativeClient):
    expected_model = MODEL
    expected_effort = EFFORT
    expected_approval_mode = APPROVAL

    def __init__(self,ready,log_dir,timeout,cancel):
        super().__init__(ready,log_dir,timeout,cancel)
        self.passive_activity = PassiveNativeActivity()
        path=Path(log_dir)/'owned-processes.json'
        receipt={'kind':'OwnedNativeClientStartupIdentity','schema_version':1,
                 'recorded_at':time.time(),'phase':'native_transport_started_before_handshake',
                 'prompt_sent':False,'session_directory':self.config['session_dir'],
                 'launcher_sha256':self.config['launcher_sha256']}
        try:
            receipt.update(status='bound',launcher_process=_process_identity(os.getpid()),
                           native_client_process=_process_identity(self.process.pid))
            self.owned_process_receipt=_write_process_identity(path,receipt)
        except (VisualError,OSError) as exc:
            # Preserve the failed identity acquisition and use the existing
            # client close path; add no PID search, adoption or termination API.
            try:
                if not path.exists():
                    _write_process_identity(path,{**receipt,'status':'unconfirmed','native_client_pid':self.process.pid,
                                                  'error':{'code':getattr(exc,'code','owned_identity_missing'),'message':str(exc)}})
            finally:self.close()
            raise

    def _receive(self, deadline=None):
        event = super()._receive(deadline)
        if getattr(self, 'prompt_sent', False) and hasattr(self, 'passive_activity'):
            self.passive_activity.observe(event, time.time())
        return event

    def handshake(self):
        result=self.request('initialize',{'protocolVersion':1,'clientCapabilities':{'_meta':{'kiro':{'hooks':{'enabled':True,'v2':True}}}},
                            'clientInfo':{'name':'kiroclops-local','version':'1'}})
        if result.get('protocolVersion')!=1:raise native.KiroError('acp_protocol_mismatch','Expected ACP1')
        created=self.request('session/new',{'cwd':self.config['target_dir'],'mcpServers':[]})
        self.session_id=created.get('sessionId')
        target=Path(self.config['target_dir']).resolve()
        roots=created.get('_meta',{}).get('workspacePaths',[])
        if not self.session_id or not roots or any(Path(p).resolve()!=target for p in roots):
            raise native.KiroError('workspace_mismatch','Fresh target workspace not confirmed')
        profile=next((p for p in created.get('modes',{}).get('availableModes',[]) if p.get('id')==self.config['agent_name']),{})
        origin=profile.get('_meta',{}).get('kiro',{}).get('resource',{}).get('source',{})
        if origin.get('origin')!='workspace' or Path(origin.get('root','.')).resolve()!=target:
            raise native.KiroError('agent_not_loaded','Workspace agent missing')
        self.request('session/set_mode',{'sessionId':self.session_id,'modeId':self.config['agent_name']})
        deadline=min(self.deadline,time.monotonic()+45)
        while MODEL not in self.model_choices:self._receive(deadline)
        previous=self.model_echo;self.effort_options=None
        self.request('session/set_config_option',{'sessionId':self.session_id,'configId':'model','value':MODEL})
        while self.model_echo<=previous or self.guard.selected.get('model')!=MODEL or not self.effort_options or self.effort_options['observed_model']!=MODEL:
            self._receive(deadline)
        options=dict(self.effort_options)
        if EFFORT not in options['values']:raise native.KiroError('effort_unavailable','No maximum effort option')
        previous=self.effort_echo
        self.request('session/set_config_option',{'sessionId':self.session_id,'configId':options['config_id'],'value':EFFORT})
        while self.effort_echo<=previous:self._receive(deadline)
        expected={f'@kiroclops/{t}' for t in ACTIVE_MCP_TOOLS} if self.config['mode']=='tools' else set()
        deadline=min(self.deadline,time.monotonic()+40)
        while True:
            server=self.servers.get('kiroclops',{})
            exposed={t.get('name') for t in server.get('tools',[]) if not t.get('disabled')}
            if (not expected or server.get('status')=='connected' and exposed==set(ACTIVE_MCP_TOOLS) and expected<=self.tags) and {'read','write','shell'}<=self.tags:
                break
            if server.get('status') in ('failed','error'):raise native.KiroError('mcp_startup_failed','Analysis tools unavailable')
            self._receive(deadline)
        if self.tags-({'read','write','shell'}|expected):raise native.KiroError('unexpected_tools','Unregistered tools exposed')
        hooks=self.request('_kiro/hooks/list',{'sessionId':self.session_id,'workspacePaths':[str(target)],'trigger':'PreToolUse'}).get('hooks',[])
        enabled=[h for h in hooks if h.get('_meta',{}).get('enabled') is True]
        hook_path=target/'.kiro/hooks/kiroclops-guard.json'
        expected_command=read_json(hook_path)['hooks'][0]['action']['command']
        if len(enabled)!=1 or Path(enabled[0].get('_meta',{}).get('filePath','.')).resolve()!=hook_path or enabled[0].get('action',{}).get('command')!=expected_command:
            raise native.KiroError('hook_not_loaded','Registered experiment hook missing')
        if self.autopilot!='on':
            self.request('session/set_config_option',{'sessionId':self.session_id,'configId':'autopilot','value':'on'})
            autopilot_deadline=min(self.deadline,time.monotonic()+15)
            while self.autopilot!='on':self._receive(autopilot_deadline)
        self.guard._verify()
        if self.guard.generation_started:raise native.KiroError('premature_generation','Task not sent yet')
        return {'session_id':self.session_id,'selected':dict(self.guard.selected),'tool_tags':sorted(self.tags),
                'approval_mode':APPROVAL,'autopilot':self.autopilot,'prompt_sent':False,'effort_options':options}

    def prompt(self,text):
        load_config(self.config['session_dir']);self.guard._verify();self.prompt_sent=True
        self.passive_activity = PassiveNativeActivity()
        return self.request('session/prompt',{'sessionId':self.session_id,'prompt':[{'type':'text','text':text}]},
                            timeout=max(.1,self.deadline-time.monotonic()))

    def _permission_reply(self,event):
        params=event.get('params',{});cid=params.get('toolCall',{}).get('toolCallId')
        tool=params.get('_meta',{}).get('kiro',{}).get('toolId');raw=self.tool_inputs.get(cid)
        decision=guard(self.config['session_dir'],{'tool_name':tool,'tool_input':raw},audit_origin='permission_reply',tool_call_id=cid) if isinstance(tool,str) and isinstance(raw,dict) and self.tool_identifiers.get(cid,tool)==tool else {'allow':False}
        options=params.get('options',[])
        choice=next((o for o in options if isinstance(o,dict) and o.get('kind')=='allow_always'
                     and isinstance(o.get('optionId'),str) and o['optionId']),None) if isinstance(options,list) else None
        response={'outcome':{'outcome':'selected','optionId':choice['optionId']}} if decision['allow'] and choice else {'outcome':{'outcome':'cancelled'}}
        if decision['allow'] and choice:response['_meta']={'kiro':{'consent':{'scope':'session'}}}
        self._send({'jsonrpc':'2.0','id':event['id'],'result':response})
        if decision['allow'] and not choice:
            raise native.KiroError('native_approval_unavailable','Native Always Allow is required; no approval fallback is permitted')
        if response['outcome']['outcome']=='cancelled':raise native.KiroError('tool_denied','Tool did not match registered scope')


def preflight(directory, *, connect=True):
    from .environment import configure_environment
    configure_environment()
    config=load_config(directory);executable=shutil.which('kiro-cli')
    if not executable:raise VisualError('missing_dependency','kiro-cli missing')
    options={'cwd':config['target_dir'],'capture_output':True,'text':True,'encoding':'utf-8','errors':'replace',
             'creationflags':subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0}
    inventory=subprocess.run([executable,'chat','--agent-engine','v3','--list-models','--format','json'],timeout=45,**options)
    models=native._parse_inventory(inventory.stdout) if inventory.returncode==0 else {'models':[]}
    if MODEL not in {m.get('model_id') for m in models['models']}:
        raise VisualError('model_unavailable',f'Exact {MODEL} model unavailable; no fallback')
    validation=subprocess.run([executable,'agent','validate','--path',config['agent_path']],timeout=30,**options)
    if validation.returncode:raise VisualError('agent_config_invalid',validation.stderr or validation.stdout)
    version=subprocess.run([executable,'--version'],timeout=15,**options)
    ready={'executable':executable,'config':config,'inventory':models,'cli_version':version.stdout.strip()}
    if connect:
        output=Path(directory)/'kiro'/f'preflight-{time.time_ns()}'
        from .startup import probe_stdio, connect_native
        if config['mode']=='tools':
            server=read_json(config['agent_path'])['mcpServers']['kiroclops']
            ready['stdio_preflight']=probe_stdio(server,output.parent/(output.name+'-stdio'),config['target_dir'])
        client,handshake,attempts=connect_native(ready,output,LocalACPClient)
        try:
            ready['preflight']=handshake
            ready['startup_attempts']=attempts
            write_json(output/'result.json',{'status':'pass','prompt_sent':False,**ready['preflight']})
        finally:client.close()
    return ready


def _postturn_report(directory, record):
    """Package evidence only after the native turn and its jobs are terminal.

    Report rendering is deterministic local work, not model authoring time. The
    seven MCP tools remain available, but an active native turn cannot start a
    report job. Both comparison arms use this same post-turn path.
    """
    directory = Path(directory).resolve()
    if record.get('status') != 'completed':
        return {'status': 'not_run', 'reason': 'native_turn_incomplete'}
    if record.get('terminal_cleanup', {}).get('status') != 'pass':
        raise VisualError('terminal_cleanup_unconfirmed', 'Do not build a report while owned processes remain')
    state = Session(directory).inspect()
    if state.get('active_job_id') is not None:
        raise VisualError('job_active', 'A native job is still active after the completed turn')
    handoff = record.get('postturn_finalization')
    if handoff is not None:
        final = state.get('final') or {}
        selected = state.get('selected_candidate_id')
        candidate = next((c for c in state.get('candidates',[]) if c.get('candidate_id') == selected), None)
        if (handoff.get('status') != 'terminal' or final.get('job_id') != handoff.get('job_id')
                or final.get('status') != handoff.get('job_status')
                or selected != handoff.get('candidate_id') or candidate is None
                or candidate.get('source_sha256') != handoff.get('source_sha256')):
            raise VisualError('wrong_result', 'Post-turn finalization differs from the sealed selected source')
    from .review import audit_native_run
    audit_status, audit_error = 'pass', None
    try:
        audit_native_run(directory)
    except (VisualError, OSError, ValueError, KeyError) as exc:
        # Missing or invalid native evidence blocks certification, not the
        # user's access to already recorded measurements and failure details.
        audit_status = 'fail'
        audit_error = {'code': getattr(exc, 'code', 'invalid_native_evidence'),
                       'message': str(exc)}
    from .reporting import build_report, report_tree_receipt
    result = build_report(directory)
    data_path = Path(result['data']).resolve()
    index_path = Path(result['report']).resolve()
    if data_path != directory / 'report' / 'report.json' or index_path != directory / 'report' / 'index.html':
        raise VisualError('wrong_result', 'Post-turn report escaped its session')
    data = read_json(data_path)
    if (data.get('native_run_status') != audit_status
            or data.get('kiro_run', {}).get('status') != 'completed'
            or data.get('selected_candidate_id') != state.get('selected_candidate_id')):
        raise VisualError('invalid_report', 'Post-turn report differs from the completed native turn')
    if audit_status != 'pass' and (data.get('overall_acceptance') == 'pass'
            or data.get('acceptance_summary', {}).get('certified_success') is True):
        raise VisualError('invalid_report', 'Unverified native evidence cannot certify success')
    return {'status': 'pass' if audit_status == 'pass' else 'incomplete',
            'render_status': 'built', 'native_evidence_status': audit_status,
            'native_evidence_error': audit_error,
            'report': str(index_path), 'report_sha256': file_sha256(index_path),
            'data': str(data_path), 'data_sha256': file_sha256(data_path),
            'tree': report_tree_receipt(directory/'report'),
            'overall_acceptance': data.get('overall_acceptance'),
            'acceptance_summary': data.get('acceptance_summary')}


def _with_postturn_report(directory, record):
    try:
        record['postturn_report'] = _postturn_report(directory, record)
    except (VisualError, OSError, ValueError, KeyError) as exc:
        record['postturn_report'] = {'status': 'failed', 'code': getattr(exc, 'code', 'report_failed'),
                                     'message': str(exc)}
    return record


def _await_preserved_finalization(session, request):
    """Wait for the sealed, source-bound final job after the model turn ends."""
    from .backend import TERMINAL

    job_id = request['job_id']
    deadline = request['validation_deadline_at'] + 5.0  # Worker watchdog/reconciliation grace.
    while True:
        remaining = max(0.0, deadline - time.time())
        job = session.get_job(job_id, wait_seconds=min(10.0, remaining), allow_final_result=True)
        if (job.get('job_id') != job_id or job.get('operation') != 'finalize'
                or job.get('candidate_id') != request.get('candidate_id')
                or job.get('source_sha256') != request.get('source_sha256')):
            raise VisualError('cleanup_binding_changed', 'Final job or selected source changed after the model turn')
        if job['status'] in TERMINAL:
            return {'status': 'terminal', 'job_id': job_id, 'job_status': job['status'],
                    'candidate_id': job.get('candidate_id'), 'source_sha256': job.get('source_sha256'),
                    'finished_at': job.get('finished_at'),
                    'validation_deadline_at': request['validation_deadline_at']}
        if remaining <= 0:
            raise VisualError('finalization_wait_timeout', 'The sealed final job did not reach a terminal result in its bounded validation time')


def _await_preserved_worker_release(session, request):
    """Keep the CLI's Windows Job open until its descendant worker has exited.

    Closing the ACP client closes a kill-on-close Windows Job. The MCP server
    and the worker it started are descendants of that Job even though the
    worker has its own process group. A terminal job record can precede worker
    exit and release of the shared capture lease by a few moments.
    """
    from .backend import TERMINAL, _alive, _lock
    from .trial_cleanup import _adopt_preserved_worker, _job

    # Process release is cleanup, not motion validation time. Give a terminal
    # worker five seconds to leave its capture lease even if it committed its
    # result at the validation deadline.
    deadline = time.time() + 5.0
    while True:
        _, job, _ = _job(session, request['job_id'], request['job_binding_sha256'])
        if job['status'] not in TERMINAL:
            raise VisualError('cleanup_binding_changed', 'The sealed final job stopped being terminal')
        worker = _adopt_preserved_worker(request, job)
        if not _alive(worker):
            try:
                with _lock(Path(request['capture_lock']), timeout=0):
                    return {'worker_exit_verified_before_client_close': True,
                            'capture_lease_released_before_client_close': True}
            except TimeoutError:
                pass
        if time.time() >= deadline:
            raise VisualError('finalization_wait_timeout', 'The sealed final worker did not exit and release its capture lease')
        time.sleep(.025)


def run(directory, registration):
    from .cohort import verify_registration
    directory=Path(directory).resolve();registry=verify_registration(registration)
    if registry.get('status')!='registered' or str(directory) not in registry.get('sessions',[]):
        raise VisualError('not_registered','Fresh trials must be in the preregistered cohort')
    qualification=read_json(registry['qualification'])
    if qualification.get('status')!='pass' or file_sha256(registry['qualification'])!=registry['qualification_sha256']:
        raise VisualError('not_qualified','Measured qualification has not passed')
    for relative,expected in registry['source_hashes'].items():
        if file_sha256(ROOT/relative)!=expected:raise VisualError('protocol_changed',relative)
    record = _run_native(directory, {'registration_sha256': file_sha256(registration),
                                     'run_kind': 'preregistered_trial'})
    return _with_postturn_report(directory, record)


def run_media(directory):
    """Product workflow; separate from, and never counted as, a benchmark trial."""
    directory=Path(directory).resolve();session=Session(directory);state=session._load()
    if state.get('experiment',{}).get('reference_mode')!='media_bundle':
        raise VisualError('not_media_session','This launcher is only for explicitly bound user-video sessions')
    from .media_session import load_bundle
    load_bundle(state['reference'])
    session._verify_sources(state)
    from .session import source_hashes
    if source_hashes(state['target'])!=state['baseline_hashes']:
        raise VisualError('not_fresh','Autonomous media task must start with its exact captured target baseline')
    if state['started_at'] is not None or state['candidates']:
        raise VisualError('not_fresh','Start the autonomous workflow from a fresh media session')
    record = _run_native(directory, {'run_kind': 'user_video_task', 'benchmark_claim': False})
    return _with_postturn_report(directory, record)


def run_suite(directory):
    """One fresh, qualified multi-reference stage; never part of the 3A cohort."""
    directory=Path(directory).resolve();session=Session(directory);state=session._load()
    if state.get('experiment',{}).get('reference_mode')!='media_suite':
        raise VisualError('not_suite_session','Expected a frozen multi-reference session.')
    from .media_suite import load_suite,runtime_environment
    suite=load_suite(state['reference']);runtime_environment(state['experiment'])
    session._verify_sources(state)
    from .session import source_hashes
    if source_hashes(state['target'])!=state['baseline_hashes'] or state['started_at'] is not None or state['candidates']:
        raise VisualError('not_fresh','Each repair stage needs its original parent and a fresh conversation.')
    from .suite_readiness import validate as validate_suite_readiness
    admission=validate_suite_readiness(directory,suite)
    record=_run_native(directory,{'run_kind':'multi_reference_repair','suite_sha256':digest(suite),
                                  'phase':'3D','admission_sha256':digest(admission),'benchmark_claim':False})
    return _with_postturn_report(directory,record)


def _run_native(directory,provenance):
    output=directory/'kiro/launch.json'
    if output.exists():raise VisualError('already_launched','One fresh conversation per registered stage')
    ready=preflight(directory,connect=False);session=Session(directory)
    record={'status':'starting','started_at':time.time(),'model':MODEL,'effort':EFFORT,'mode':ready['config']['mode'],
            'approval_mode':APPROVAL,'prompt_sent':False,'cli_version':ready['cli_version'],**provenance}
    write_json(output,record);client=None
    try:
        launcher_identity=_process_identity(os.getpid())
        record['launcher_process_receipt']=_write_process_identity(directory/'kiro/launcher-process.json',
            {'kind':'OwnedLauncherStartupIdentity','schema_version':1,'status':'bound','recorded_at':time.time(),
             'phase':'launcher_started_before_native_transport','prompt_sent':False,
             'session_id':session._load()['session_id'],'launcher_sha256':ready['config']['launcher_sha256'],
             'launcher_process':launcher_identity})
        write_json(output,record)
        from .startup import probe_stdio, connect_native
        if ready['config']['mode']=='tools':
            server=read_json(ready['config']['agent_path'])['mcpServers']['kiroclops']
            record['stdio_preflight']=probe_stdio(server,directory/'kiro/stdio-preflight',ready['config']['target_dir'])
        client,handshake,attempts=connect_native(ready,directory/'kiro/run',LocalACPClient)
        record['startup_attempts']=attempts
        record['native_client_process']=_verify_saved_client_process(client,require_alive=True)
        record['native_client_process_receipt']=dict(client.owned_process_receipt)
        record['preflight']=handshake;record['status']='running';write_json(output,record)
        # Connection/model/MCP verification is not candidate authoring time.
        # Start both durable and transport budgets at the actual task prompt.
        clock = session.start_clock()
        policy = policy_from_state(session._load())
        client.deadline = time.monotonic() + max(0., clock['deadline_at'] - time.time())
        if policy['policy_id'] == REVISED_ID:
            record['execution_policy'] = policy
        record['prompt_started_at']=time.time();write_json(output,record)
        record['response']=client.prompt(Path(ready['config']['prompt_path']).read_text('utf-8'))
        record['response_received_at']=time.time()
        record['status']='completed' if record['response'].get('stopReason')=='end_turn' else 'turn_stopped'
        if not client.guard.generation_started:raise VisualError('generation_unverified',f'No actual {MODEL}/{EFFORT} generation confirmed')
        # Persist the native end_turn before potentially long local validation.
        # An interrupted runner cannot turn this into a completed trial without
        # the later cleanup, final-result and report receipts.
        write_json(output,record)
    except (VisualError,native.KiroError,OSError) as exc:
        record.update(status=getattr(exc,'code','launch_failed'),error=str(exc))
        if getattr(exc, 'code', None) == 'timeout' and client is not None and client.prompt_sent:
            record['failure_classification'] = 'authoring_timeout'
    finally:
        from .trial_cleanup import begin_terminal_cleanup, cancel_preserved_finalization, finish_terminal_cleanup
        record['native_turn_status']=record['status'];request=None;cleanup_setup_error=None
        cleanup_started=time.time()
        # A completed native turn may hand off only its already sealed final job.
        # Other pending operations still take the ordinary cancellation path.
        preserve_finalization=False
        try:
            state=session._load()
            preserve_finalization=(record['status']=='completed' and state.get('active_job_id') is not None
                                   and state.get('active_job_id')==state.get('final_job_id')
                                   and state.get('sealed_at') is not None)
            request=begin_terminal_cleanup(session,preserve_finalization=preserve_finalization)
            if client:
                # Attach the already saved identity even if the client exited.
                # Verify a live identity only to reject reuse, never recapture it.
                request['client_process']=_saved_client_process(client)
                _verify_saved_client_process(client)
        except (VisualError,OSError,ValueError,KeyError,TimeoutError) as exc:
            cleanup_setup_error={'code':getattr(exc,'code','terminal_cleanup_unconfirmed'),'message':str(exc)}
            record['terminal_cleanup']={'kind':'TerminalTrialCleanup','status':'unconfirmed',
                'request':request,'error':cleanup_setup_error}
            if preserve_finalization:
                # A bad handoff must still cancel its owned worker. Keep the
                # original binding failure as the terminal trial outcome.
                try:
                    request=begin_terminal_cleanup(session)
                    preserve_finalization=False
                except (VisualError,OSError,ValueError,KeyError,TimeoutError) as fallback_exc:
                    cleanup_setup_error['fallback_cancellation_error']={
                        'code':getattr(fallback_exc,'code','terminal_cleanup_unconfirmed'),
                        'message':str(fallback_exc)}
        finally:
            # Capture the native settings before the connection is closed. A
            # preserved final worker still lives in the client's Windows Job.
            if client:
                try:
                    record.update(prompt_sent=client.prompt_sent,stream_configuration=client.guard.summary(),kas_version=client.kas_version)
                    if hasattr(client, 'passive_activity'):
                        record['passive_activity'] = client.passive_activity.summary()
                except Exception as exc:
                    cleanup_setup_error={'code':'terminal_cleanup_unconfirmed','message':str(exc)}
        try:
            if request is not None and preserve_finalization and request.get('job_id'):
                try:
                    handoff=_await_preserved_finalization(session,request)
                    handoff.update(_await_preserved_worker_release(session,request))
                    record['postturn_finalization']=handoff
                    if handoff['job_status']!='succeeded':
                        record['status']='finalization_failed'
                except (VisualError,OSError,ValueError,KeyError,TypeError) as exc:
                    record['postturn_finalization']={'status':'failed','code':getattr(exc,'code','finalization_wait_failed'),
                                                     'message':str(exc),'job_id':request['job_id']}
                    record['status']='finalization_wait_failed'
                    if getattr(exc, 'code', None) == 'finalization_wait_timeout':
                        record['failure_classification'] = 'validation_timeout'
                    try:cancel_preserved_finalization(session,request)
                    except (VisualError,OSError,ValueError,KeyError,TypeError) as cancel_exc:
                        cleanup_setup_error={'code':getattr(cancel_exc,'code','terminal_cleanup_unconfirmed'),
                                             'message':str(cancel_exc)}
        finally:
            # All other paths close promptly. For a valid handoff, this runs
            # only after the exact final worker and capture lease are gone.
            if client:
                try:record['cleanup_errors']=client.close()
                except Exception as exc:record['cleanup_errors']=[str(exc)]
        if request is not None:
            record['terminal_cleanup']=finish_terminal_cleanup(session,request)
            if request['had_pending_job'] and not preserve_finalization and record['status']=='completed':
                record['status']='ended_with_active_job'
        if record.get('cleanup_errors'):
            record['terminal_cleanup']['status']='unconfirmed'
            record['terminal_cleanup']['client_cleanup_errors']=record['cleanup_errors']
        if cleanup_setup_error:
            record['terminal_cleanup'].update(status='unconfirmed',error=cleanup_setup_error)
        record['terminal_cleanup'].update(cleanup_started_at=cleanup_started,
            cleanup_total_wall_seconds=time.time()-cleanup_started)
        if record['terminal_cleanup']['status']!='pass':record['status']='terminal_cleanup_unconfirmed'
        record['finished_at']=time.time();write_json(output,record)
    return record


def main():
    import argparse,sys
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--session',required=True,type=Path)
    parser.add_argument('--guard',action='store_true');parser.add_argument('--preflight',action='store_true');parser.add_argument('--registration',type=Path)
    parser.add_argument('--user-video',action='store_true',help='Run an explicitly bound local media session, outside the benchmark cohort')
    parser.add_argument('--reference-suite',action='store_true',help='Run a frozen multi-reference repair stage')
    args=parser.parse_args()
    try:
        if args.guard:
            from .guard_diagnostics import run_hook
            return run_hook(args.session, guard_callable=guard)
        if sum(bool(v) for v in (args.user_video,args.registration,args.reference_suite))>1:raise VisualError('invalid_request','Run modes are separate')
        if not (args.preflight or args.user_video or args.reference_suite or args.registration):raise VisualError('missing_registration','Select a registered trial, bound media or reference suite')
        result=preflight(args.session) if args.preflight else run_suite(args.session) if args.reference_suite else run_media(args.session) if args.user_video else run(args.session,args.registration)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if args.preflight or (result.get('status')=='completed'
                and result.get('postturn_report',{}).get('status')=='pass') else 1
    except (VisualError,native.KiroError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'error','code':getattr(exc,'code','invalid_request'),'message':str(exc)}));return 2


if __name__=='__main__':
    raise SystemExit(main())
