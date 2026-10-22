"""Preregister eighteen trials and two-stage game chains before any generation."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import platform
import statistics
import time

from kiroclops.visual.schema import VisualError, read_json, write_json, file_sha256, digest
from kiroclops.visual.package import load_package
from kiroclops.integration.kiro import StreamConfigurationGuard, KiroError
from kiroclops.web_local.specs import fixture_project, scenarios
from .session import Session, create_session, source_hashes
from .launcher import configure, run, MODEL, EFFORT, APPROVAL, ROOT

GOALS={
 'panel':'Use the reference recordings to match panel opening, closing, return after dragging, and title/content timing. Preserve interrupted animation, repeated clicks, pointer release, and responsive layout. Change the real motion controller; do not replay recordings.',
 'svg':'Use the reference recordings to match the diagram layout motion when nodes are added, removed or dragged. Preserve node identity, selection, visible connector attachment, and no final node overlap. Change the real layout controller; do not replay recordings.',
 'jump':'Transfer Tiny Crate short/long normal jump to Heart from the supplied calibration recordings. Preserve airborne control, air jump, wall jump, coyote timing, landing and hazard return. Write the actual player code.',
 'ground':'Transfer grounded acceleration, release braking and reversal from the supplied calibration recordings. Preserve the inherited short/long jump, airborne control, air jump, wall/coyote behavior and landing. Write the actual player code.'}


def protocol_sources():
    # Freeze the complete used Python tree, fixture controllers, dependency lock
    # and local adapter source. New files also alter the binding.
    paths=set((ROOT/'kiroclops').rglob('*.py'))
    for base in ('fixtures/phase3a-web','fixtures/phase3a-svg','fixtures/phase3a-svg-polyline','adapters/heart-visual-boundary-v3','adapters/heart-visual-boundary-v4'):
        if (ROOT/base).exists():paths.update(p for p in (ROOT/base).rglob('*') if p.is_file() and '.godot' not in p.parts)
    paths.add(ROOT/'requirements-local.lock')
    return {p.relative_to(ROOT).as_posix():file_sha256(p) for p in sorted(paths)}


def _evidence_path(base,value):
    path=Path(value)
    return path if path.is_absolute() else Path(base)/path


def _bound_file(base,path,hashes):
    path=_evidence_path(base,path).resolve()
    matches=[h for name,h in hashes.items() if _evidence_path(base,name).resolve()==path]
    if len(matches)!=1 or not path.is_file() or file_sha256(path)!=matches[0]:
        raise VisualError('qualification_changed',f'Missing or changed bound evidence: {path.name}')
    return path


def _validate_sample(row,base,*,_review_cache=None):
    hashes=row.get('evidence_hashes',{})
    if not hashes:raise VisualError('not_qualified','A sample must bind its actual evidence files')
    package=_evidence_path(base,row['package_dir']);reference=_evidence_path(base,row['reference_package_dir'])
    manifest_path=_bound_file(base,package/'manifest.json',hashes)
    original_path=_bound_file(base,reference/'manifest.json',hashes)
    selection=_bound_file(base,row['selection_path'],hashes)
    contract=_bound_file(base,row['contract_path'],hashes)
    result_path=_bound_file(base,row['result_path'],hashes);result=read_json(result_path)
    if digest(read_json(contract))!=row.get('settings_sha256'):raise VisualError('qualification_changed','Qualification thresholds changed')
    if row.get('observed_class')!=row.get('expected_class'):raise VisualError('not_qualified','Actual classification differs')
    if result.get('observed_class') is not None and result['observed_class']!=row['observed_class']:
        raise VisualError('not_qualified','Summary and actual classifier result differ')
    review_path=_evidence_path(base,row['independent_review_path']).resolve()
    review_sha256=file_sha256(review_path)
    if review_sha256!=row.get('independent_review_sha256'):raise VisualError('stale_review','Qualification review changed')
    if row.get('kind')=='explicit_pixel_or_record_corruption':
        fault=read_json(review_path)
        expected={'source_manifest_sha256':file_sha256(original_path),'package_manifest_sha256':file_sha256(manifest_path),
                  'selection_sha256':file_sha256(selection),'result_sha256':file_sha256(result_path)}
        if (fault.get('kind')!='QualificationFaultReview' or fault.get('sample_id')!=row['id'] or fault.get('status')!='pass'
                or not fault.get('reviewer') or fault.get('method')!='independent_mutation_and_failure_audit'
                or fault.get('position_review_status')!='not_applicable_invalid_measurement'
                or any(fault.get(k)!=v for k,v in expected.items())):
            raise VisualError('incomplete_review','Fault controls need an independent mutation and failure audit')
        load_package(reference)
        # Invalid timing/missing pixels are the intended evidence, so never demand
        # that a deliberately corrupt package loads successfully.
        error=result.get('error')
        if error:
            if not error.get('code') or not error.get('message'):raise VisualError('not_qualified','Fault exception receipt incomplete')
            if row['expected_class']!='tracking_failure':
                try:load_package(package)
                except VisualError as exc:
                    if exc.code!=error['code']:raise VisualError('not_qualified','Fault exception no longer reproduces')
                else:raise VisualError('not_qualified','Expected corrupt package is now valid')
        else:
            analysis=read_json(_bound_file(base,row['analysis_path'],hashes))
            if analysis.get('measurement_status') not in ('fail','rejected') or not analysis.get('errors'):
                raise VisualError('not_qualified','No recorded tracking rejection supports this error control')
        return
    manifest=load_package(package);load_package(reference)
    analysis_path=_bound_file(base,row['analysis_path'],hashes);analysis=read_json(analysis_path)
    observations=analysis.get('observations')
    if observations is None:
        observations=read_json(_bound_file(base,analysis_path.parent/'observations.json',hashes))
    if observations.get('package_sha256')!=manifest['package_sha256'] or not observations.get('objects'):
        raise VisualError('not_qualified','Actual observations are not linked to the recorded pixels')
    provenance=analysis.get('provenance',{})
    if provenance.get('observation_sha256') and provenance['observation_sha256']!=digest(observations):
        raise VisualError('qualification_changed','Game measurement observation digest differs')
    if 'capture' in result:
        capture=result['capture']
        if capture.get('timing',{}).get('status')!='pass':observed='capture_timing_failure'
        elif capture.get('functional',{}).get('status')!='pass':observed='application_integrity_failure'
        elif analysis.get('measurement_status')!='ok':observed='tracking_failure'
        else:observed='correct' if (result.get('comparison') or {}).get('pass') is True else 'behavioral_deviation'
    elif type(result.get('pass')) is bool:observed='correct' if result['pass'] else 'behavioral_deviation'
    else:raise VisualError('not_qualified','Missing actual classified result')
    if observed!=row['observed_class']:raise VisualError('not_qualified','Claimed class does not follow the bound measurement result')
    if manifest.get('metadata',{}).get('source_hashes') and manifest['metadata']['source_hashes']!=row.get('source_hashes'):
        raise VisualError('qualification_changed','Captured application source binding differs')
    from .review import validate_independent_review
    # Only one caller-owned qualification invocation can share a full replay.
    # Every row still rechecks this file, its actual pixels and its membership.
    # A standalone sample check, or a new invocation, always replays afresh.
    key=(str(review_path),review_sha256)
    if _review_cache is not None and key in _review_cache:
        reviewed=_review_cache[key]
    else:
        reviewed=validate_independent_review(review_path)
        if _review_cache is not None:_review_cache[key]=reviewed
    if [manifest['package_sha256'],digest(observations)] not in reviewed['required_observations']:
        raise VisualError('incomplete_review','Independent evidence omits the evaluated recording')


def validate_qualification(path):
    path=Path(path).resolve()
    q=read_json(path)
    if q.get('status')!='pass' or q.get('independent_review_status')!='pass':
        raise VisualError('not_qualified','Classification and independent review must both pass before generation')
    rows=q.get('samples',[])
    if len(rows)<24 or len({r.get('id') for r in rows})!=len(rows) or not all(r.get('classification_pass') is True and r.get('independent_review_status')=='pass' for r in rows):
        raise VisualError('not_qualified','Expected at least twenty-four distinct correct/error qualification controls')
    for domain in ('game','panel','svg'):
        domainrows=[r for r in rows if r.get('domain') in ({'panel','web_panel'} if domain=='panel' else {domain})]
        if sum(r.get('expected_class')=='correct' for r in domainrows)<2 or sum(r.get('expected_class') in ('behavioral_deviation','application_integrity_failure') for r in domainrows)<4:
            raise VisualError('not_qualified',f'Missing positive/behavior-deviation controls for {domain}')
    if sum(r.get('expected_class') in ('capture_timing_failure','tracking_failure','package_integrity_failure','capture_integrity_failure','missing_frame','identity_ambiguity') for r in rows)<6:
        raise VisualError('not_qualified','Six distinct timing/tracking/package error controls are required')
    if not q.get('source_hashes') or not q.get('component_manifests'):
        raise VisualError('not_qualified','Actual source and component manifest bindings are required')
    for relative,expected in q['source_hashes'].items():
        if file_sha256(_evidence_path(ROOT,relative))!=expected:raise VisualError('qualification_changed',relative)
    component_rows=[];review_cache={}
    for name,expected in q['component_manifests'].items():
        component=_evidence_path(path.parent,name)
        if file_sha256(component)!=expected:raise VisualError('qualification_changed','Component qualification changed')
        data=read_json(component)
        if data.get('status')!='pass' or data.get('independent_review_status')!='pass':raise VisualError('not_qualified','Component review not complete')
        if any(q['source_hashes'].get(name)!=value for name,value in data.get('source_hashes',{}).items()):
            raise VisualError('qualification_changed','Component analysis used different source versions')
        for row in data.get('samples',[]):_validate_sample(row,component.parent,_review_cache=review_cache)
        component_rows.extend(data.get('samples',[]))
    if sorted(component_rows,key=lambda r:r['id'])!=sorted(rows,key=lambda r:r['id']):
        raise VisualError('qualification_changed','Combined samples differ from their actual components')
    return q


def reference_sources():
    paths=[]
    for directory in sorted((ROOT/'artifacts/runs').glob('tiny-reference-*')):
        paths.extend(p for p in directory.rglob('*') if p.is_file() and p.suffix in ('.png','.json'))
    paths.extend(p for p in (ROOT/'artifacts/phase2b/reference').rglob('*') if p.is_file() and p.suffix in ('.png','.json'))
    paths.extend(p for p in (ROOT/'config').rglob('*annotation*.json') if p.is_file())
    return {p.relative_to(ROOT).as_posix():file_sha256(p) for p in sorted(set(paths))}


def web_reference_binding(manifest, reference_gate=None):
    if manifest is None:
        raise VisualError('missing_reference','A shared frozen web/SVG reference pack is required before registering either arm')
    path=Path(manifest).resolve()
    from .reference_acceptance import validate_reference_gate
    gate=validate_reference_gate(path, reference_gate)
    policy=read_json(path).get('capture_policy')
    return {'manifest':str(path),'manifest_sha256':file_sha256(path),'observability':gate,
            **({'capture_policy':policy} if policy is not None else {})}


def register(directory, qualification, *, game_baseline=None, web_references=None, reference_gate=None):
    directory=Path(directory).resolve();qualification=Path(qualification).resolve()
    q=validate_qualification(qualification)
    if directory.exists():raise VisualError('output_exists','Never replace a registered cohort or its failed trials')
    shared_reference=web_reference_binding(web_references, reference_gate)
    baseline=Path(game_baseline or ROOT/'artifacts/phase2c-v2/setup/native-01/baseline').resolve()
    if not (baseline/'player.gd').exists():raise VisualError('missing_source','Verified clean Heart baseline missing')
    directory.mkdir(parents=True)
    rows=[];sessions=[]
    # Alternating arm order counters a simple always-tools-first time trend.
    for repeat in (1,2,3):
        for domain in ('game','panel','svg'):
            for mode in (('tools','baseline') if repeat%2 else ('baseline','tools')):
                identifier=f'{domain}-{mode}-{repeat:02}'
                row={'id':identifier,'domain':domain,'mode':mode,'repeat':repeat,'stages':[],'status':'registered'}
                for stage in (('jump','ground') if domain=='game' else (domain,)):
                    path=directory/'sessions'/identifier/stage
                    sessions.append(str(path));row['stages'].append({'stage':stage,'session':str(path),'status':'registered'})
                rows.append(row)
    registry={'schema_version':1,'kind':'LocalComparisonCohort','status':'registered','registered_at':time.time(),
              'model':MODEL,'effort':EFFORT,'approval_mode':'native_session_allow_always','trials':18,
              'qualification':str(qualification),'qualification_sha256':file_sha256(qualification),
              'source_hashes':protocol_sources(),'reference_hashes':reference_sources(),'game_baseline':str(baseline),'game_baseline_hashes':source_hashes(baseline),
              'web_reference_bundle':shared_reference,
              'sessions':sessions,'rows':rows,'goals':GOALS,
              'budget':{'max_candidates':3,'max_seconds':1800,'per':'authoring_stage',
                        'max_validation_seconds':1800,'validation_starts':'sealed_finalize_submission'},
              'ordinary_tools_both_arms':['read','write','shell'],'comparison_limits':[
                  'Three repeats per domain and arm; descriptive evidence only.',
                  'Failed trials remain in the denominator. No substitute run or operator patch.',
                  'Baseline has neutral recordings without KiroClops profiles or correction guidance.'],
              'platform':platform.platform(),'qualification_review':q['independent_review_status']}
    write_json(directory/'registration.json',registry)
    write_json(directory/'progress.json',{'registration_sha256':file_sha256(directory/'registration.json'),'rows':rows})
    return registry


def verify_registration(path, *, full=True):
    path=Path(path).resolve()
    progress=read_json(path.parent/'progress.json')
    if progress.get('registration_sha256')!=file_sha256(path):
        raise VisualError('invalid_registration','The preregistered experiment was changed')
    registry=read_json(path)
    _verify_progress_layout(registry,progress)
    if registry.get('status')!='registered' or (registry.get('model'),registry.get('effort'))!=(MODEL,EFFORT):
        raise VisualError('invalid_registration',f'Expected the frozen {MODEL}/{EFFORT} cohort')
    if not full:
        # The runner needs the frozen denominator before stage preparation.
        # launcher.run and standalone summarize call full verification before
        # any model prompt or success claim; no timestamp cache is involved.
        return registry
    if protocol_sources()!=registry['source_hashes']:raise VisualError('protocol_changed','Cohort source files changed')
    if reference_sources()!=registry['reference_hashes']:raise VisualError('reference_changed','Registered pixel references changed')
    if file_sha256(registry['qualification'])!=registry['qualification_sha256']:
        raise VisualError('qualification_changed','Registered qualification changed')
    validate_qualification(registry['qualification'])
    shared=registry.get('web_reference_bundle',{})
    if web_reference_binding(shared.get('manifest'),shared.get('observability',{}).get('path'))!=shared:
        raise VisualError('reference_changed','The shared web/SVG recordings changed after registration')
    if source_hashes(registry['game_baseline'])!=registry['game_baseline_hashes']:
        raise VisualError('baseline_changed','Starting game source changed')
    return registry


def _verify_progress_layout(registry,progress):
    def identities(rows):
        return [(r.get('id'),r.get('domain'),r.get('mode'),r.get('repeat'),
                 [(s.get('stage'),s.get('session')) for s in r.get('stages',[])]) for r in rows]
    if identities(registry.get('rows',[]))!=identities(progress.get('rows',[])):
        raise VisualError('invalid_registration','Progress changed the registered trial identities or denominator')


def _prepare_stage(registry,row,stage,previous=None):
    directory=Path(stage['session']);name=stage['stage']
    if row['domain']=='game':
        baseline=registry['game_baseline'] if name=='jump' else previous
        # Reference anchor contains only frozen annotation metadata. Game wrapper
        # uses the source-bound prerecorded calibration images outside workspace.
        reference=ROOT/'config/phase2c-v2'
        experiment={'stage':name,'original_baseline':registry['game_baseline'],'experiment_id':'local-heart-'+name}
        domain='game'
    else:
        baseline=fixture_project(row['domain'],'target');reference=fixture_project(row['domain'],'reference')
        experiment={'experiment_id':'local-'+row['domain'],'calibration':scenarios(row['domain'],'calibration'),
                    'validation':scenarios(row['domain'],'validation'),
                    'web_reference_bundle':registry['web_reference_bundle']['manifest'],
                    'web_reference_bundle_sha256':registry['web_reference_bundle']['manifest_sha256']}
        policy=registry['web_reference_bundle'].get('capture_policy')
        if policy is not None:
            from kiroclops.web_local.specs import capture_scenarios
            experiment.update(capture_policy=policy,
                calibration=capture_scenarios(row['domain'],'calibration',policy),
                validation=capture_scenarios(row['domain'],'validation',policy))
        if 'observability' in registry['web_reference_bundle']:
            # Private immutable experiment data; Session.inspect exports only
            # its existing public whitelist, never heldout measurement paths.
            experiment['web_reference_cache']=dict(registry['web_reference_bundle']['observability'])
        domain='web_panel' if row['domain']=='panel' else 'svg'
    if directory.exists():
        session=Session(directory);state=session._load();session._verify_sources(state)
        if state['started_at'] is not None:raise VisualError('already_launched','Do not replace or restart a started stage')
        if (source_hashes(state['target'])!=state['baseline_hashes'] or state['baseline_hashes']!=source_hashes(baseline)
                or state.get('candidates') or state.get('active_job_id') or state.get('sealed_at') or state['mode']!=row['mode']
                or state['goal']!=registry['goals'][name] or state['domain']!=domain or state['experiment']!=experiment
                or Path(state['reference']).resolve()!=Path(reference).resolve()):
            raise VisualError('not_fresh','A registered trial must start with the untouched registered baseline and workflow')
        return
    create_session(directory,domain,baseline,reference,registry['goals'][name],mode=row['mode'],experiment=experiment)
    configure(directory)


def _resume_cleanup(registry,progress,progress_path,explicit):
    blocked=progress.get('continuation_blocked')
    if not blocked:return
    if not explicit:
        raise VisualError('terminal_cleanup_unconfirmed','Cohort continuation is blocked; use --resume-after-cleanup only to reverify cleanup, not rerun the failed trial.')
    directory=blocked.get('session')
    if directory not in registry['sessions']:
        raise VisualError('cleanup_binding_changed','Blocked cleanup does not belong to a registered session')
    path=Path(directory)/'kiro/launch.json'
    if file_sha256(path)!=blocked.get('launch_sha256'):
        raise VisualError('cleanup_binding_changed','Original terminal cleanup record changed')
    request=read_json(path).get('terminal_cleanup',{}).get('request')
    if not isinstance(request,dict):
        raise VisualError('terminal_cleanup_unconfirmed','The original cleanup lacks an owned-process binding; automatic resume is unavailable')
    from .trial_cleanup import finish_terminal_cleanup
    checked=finish_terminal_cleanup(Session(directory),request,terminate=False)
    progress.setdefault('cleanup_rechecks',[]).append({'session':directory,'launch_sha256':blocked['launch_sha256'],'result':checked})
    if checked['status']=='pass':progress.pop('continuation_blocked')
    write_json(progress_path,progress)
    if checked['status']!='pass':
        raise VisualError('terminal_cleanup_unconfirmed','Previous owned process exit or capture lease release remains unconfirmed')


def _evidenced_prompt_throttle(directory, launched):
    """Attribute an incomplete turn to ACP throttling only from bound transport evidence.

    This is a failure classifier, never a substitute for the completed-turn
    native audit. Missing or changed evidence simply leaves the normal audit
    failure in place.
    """
    if (launched.get('status')!='acp_request_failed' or launched.get('native_turn_status')!='acp_request_failed'
            or launched.get('prompt_sent') is not True or 'response' in launched
            or launched.get('terminal_cleanup',{}).get('status')!='pass'
            or (launched.get('model'),launched.get('effort'),launched.get('approval_mode'))!=(MODEL,EFFORT,APPROVAL)):
        return False
    preflight=launched.get('preflight',{});stream=launched.get('stream_configuration',{})
    selected=preflight.get('selected');sid=preflight.get('session_id')
    if (not isinstance(selected,dict) or not isinstance(sid,str) or not sid
            or selected.get('model')!=MODEL or selected.get('effort')!=EFFORT
            or stream.get('selected')!=selected or stream.get('expected')!=selected
            or stream.get('generation_started') is not False):
        return False
    try:
        root=Path(directory)/'kiro/run';paths=[root/'outbound.jsonl',root/'stdout.jsonl']
        if any(not p.is_file() or p.stat().st_size>32*1024*1024 for p in paths):return False
        outbound=[json.loads(line) for line in paths[0].read_text('utf-8').splitlines() if line.strip()]
        inbound=[json.loads(line) for line in paths[1].read_text('utf-8').splitlines() if line.strip()]
        if not outbound or len(outbound)%2 or not inbound or not all(isinstance(v,dict) for v in outbound+inbound):
            return False
        messages=[]
        for index in range(0,len(outbound),2):
            attempt,sent=outbound[index:index+2];message=attempt.get('message')
            if (not isinstance(message,dict) or attempt.get('phase')!='attempt' or sent.get('phase')!='sent'
                    or attempt.get('sequence')!=sent.get('sequence') or sent.get('sequence')!=index//2+1
                    or attempt.get('payload_sha256')!=digest(message)
                    or sent.get('payload_sha256')!=digest(message)
                    or 'message' in sent):
                return False
            messages.append(message)
        prompts=[message for message in messages if message.get('method')=='session/prompt']
        if len(prompts)!=1:return False
        prompt=prompts[0];params=prompt.get('params',{})
        if (prompt.get('jsonrpc')!='2.0' or type(prompt.get('id')) is not int
                or not isinstance(params,dict) or params.get('sessionId')!=sid
                or not isinstance(params.get('prompt'),list) or len(params['prompt'])!=1
                or params['prompt'][0].get('type')!='text'
                or not isinstance(params['prompt'][0].get('text'),str) or not params['prompt'][0]['text']):
            return False
        responses=[event for event in inbound if 'method' not in event and type(event.get('id')) is int
                   and event['id']==prompt['id']]
        if len(responses)!=1 or 'result' in responses[0]:return False
        failure=responses[0].get('error');details=failure.get('data',{}) if isinstance(failure,dict) else {}
        return (isinstance(details,dict) and failure.get('code')==-32000
                and isinstance(failure.get('message'),str) and bool(failure['message'])
                and details.get('errorType')=='ModelThrottleError'
                and details.get('retryErrorType')=='THROTTLING'
                and isinstance(details.get('requestId'),str) and bool(details['requestId'])
                and launched.get('error')==f"session/prompt: {failure}")
    except (OSError,UnicodeError,ValueError,TypeError,KeyError,AttributeError):
        return False


def _evidenced_authoring_timeout(directory, launched):
    """Classify a spent native authoring budget only from its bound ACP stream.

    This records a failed trial, not a successful native turn. Missing or
    changed receipts must reach the ordinary completed-turn audit instead.
    """
    if not isinstance(launched,dict):return False
    cleanup=launched.get('terminal_cleanup');preflight=launched.get('preflight');stream=launched.get('stream_configuration')
    if not all(isinstance(value,dict) for value in (cleanup,preflight,stream)):return False
    if (launched.get('status')!='timeout' or launched.get('native_turn_status')!='timeout'
            or launched.get('prompt_sent') is not True or 'response' in launched
            or cleanup.get('status')!='pass'
            or (launched.get('model'),launched.get('effort'),launched.get('approval_mode'))!=(MODEL,EFFORT,APPROVAL)):
        return False
    selected=preflight.get('selected');sid=preflight.get('session_id')
    expected={'mode':selected.get('mode'),'model':MODEL,'effort':EFFORT} if isinstance(selected,dict) else None
    if (not isinstance(sid,str) or not sid or not isinstance(expected,dict)
            or not isinstance(expected['mode'],str) or not expected['mode']
            or selected!=expected or preflight.get('approval_mode')!=APPROVAL
            or preflight.get('autopilot')!='on' or preflight.get('prompt_sent') is not False
            or stream.get('selected')!=expected or stream.get('expected')!=expected
            or stream.get('generation_started') is not True
            or stream.get('generation_configuration_verified') is not True):
        return False
    started=launched.get('prompt_started_at');finished=launched.get('finished_at')
    cleanup_at=cleanup.get('cleanup_started_at')
    if (any(type(t) not in (int,float) or not math.isfinite(t) for t in (started,cleanup_at,finished))
            or not started<=cleanup_at<=finished
            or cleanup_at-started<1799.9 or finished-started<1799.9):
        return False
    try:
        root=Path(directory)/'kiro/run';outbound_path=root/'outbound.jsonl';inbound_path=root/'stdout.jsonl'
        if any(not p.is_file() or p.stat().st_size>32*1024*1024 for p in (outbound_path,inbound_path)):
            return False
        outbound=[json.loads(line) for line in outbound_path.read_text('utf-8').splitlines() if line.strip()]
        inbound=[json.loads(line) for line in inbound_path.read_text('utf-8').splitlines() if line.strip()]
        if (not outbound or len(outbound)%2 or not inbound
                or not all(isinstance(value,dict) for value in outbound+inbound)):
            return False
        prompts=[]
        for index in range(0,len(outbound),2):
            attempt,sent=outbound[index:index+2];message=attempt.get('message')
            if (not isinstance(message,dict) or attempt.get('phase')!='attempt' or sent.get('phase')!='sent'
                    or attempt.get('sequence')!=sent.get('sequence') or sent.get('sequence')!=index//2+1
                    or attempt.get('payload_sha256')!=digest(message)
                    or sent.get('payload_sha256')!=digest(message) or 'message' in sent):
                return False
            if message.get('method')=='session/prompt':prompts.append(message)
        if len(prompts)!=1:return False
        prompt=prompts[0];params=prompt.get('params',{})
        if (prompt.get('jsonrpc')!='2.0' or type(prompt.get('id')) is not int
                or not isinstance(params,dict) or params.get('sessionId')!=sid
                or not isinstance(params.get('prompt'),list) or len(params['prompt'])!=1
                or not isinstance(params['prompt'][0],dict)
                or params['prompt'][0].get('type')!='text'
                or not isinstance(params['prompt'][0].get('text'),str) or not params['prompt'][0]['text']):
            return False
        if any('method' not in event and event.get('id')==prompt['id'] for event in inbound):return False
        guard=StreamConfigurationGuard(expected['mode'],model=MODEL,effort=EFFORT)
        for event in inbound:guard.observe(event)
        return guard.summary()==stream and guard.generation_started
    except (OSError,UnicodeError,ValueError,TypeError,KeyError,AttributeError,KiroError):
        return False


def run_cohort(registration, *, selected_trial=None, resume_after_cleanup=False):
    registration=Path(registration).resolve();registry=verify_registration(registration,full=False)
    progress_path=registration.parent/'progress.json';progress=read_json(progress_path)
    if progress['registration_sha256']!=file_sha256(registration):raise VisualError('invalid_registration','Progress belongs to another registration')
    if selected_trial and selected_trial not in {r['id'] for r in progress['rows']}:
        raise VisualError('invalid_trial','Unknown preregistered trial')
    _resume_cleanup(registry,progress,progress_path,resume_after_cleanup)
    for row in progress['rows']:
        if selected_trial and selected_trial!=row['id']:continue
        if row['status'] in ('completed','failed','interrupted'):continue
        previous=None;row['status']='running';write_json(progress_path,progress)
        for stage in row['stages']:
            if stage['status']=='completed':
                saved=read_json(Path(stage['session'])/'session.json')
                previous=Path(stage['session'])/'candidates'/saved['selected_candidate_id']/'source';continue
            if stage['status']!='registered':
                row['status']='interrupted';stage['status']='interrupted';break
            stage.update(status='running',started_at=time.time());write_json(progress_path,progress)
            launched={};state={};native_status='fail';error=None
            try:
                _prepare_stage(registry,row,stage,previous)
                launched=run(stage['session'],registration)
                if launched.get('terminal_cleanup',{}).get('status')!='pass':
                    raise VisualError('terminal_cleanup_unconfirmed','Do not start another trial before owned execution cleanup is confirmed')
                state=Session(stage['session']).inspect()
                if _evidenced_prompt_throttle(stage['session'],launched):
                    raise VisualError('upstream_model_throttle',
                                      'Kiro model service throttled the exact selected model before generation')
                if _evidenced_authoring_timeout(stage['session'],launched):
                    raise VisualError('native_authoring_timeout',
                                      'Kiro generated with the selected model until the bounded authoring deadline')
                from .review import audit_native_run
                audit_native_run(stage['session']);native_status='pass'
                report=launched.get('postturn_report',{})
                if report.get('status')!='pass':
                    raise VisualError(report.get('code','postturn_report_incomplete'),
                                      report.get('message','The native turn ended without a completed local report'))
                measured=_stage_result(stage['session'])
                if not isinstance(measured,dict):
                    raise VisualError('invalid_result','No source-bound final result exists for the completed report')
                _verify_postturn_report({'session':stage['session'],'postturn_report':report},
                                        measured,state.get('selected_candidate_id'))
            except (VisualError,OSError,ValueError,KeyError) as exc:
                error={'code':getattr(exc,'code','trial_execution_error'),'message':str(exc)}
                launch_path=Path(stage['session'])/'kiro/launch.json'
                if not launched and launch_path.is_file():launched=read_json(launch_path)
                if launched and launched.get('terminal_cleanup',{}).get('status')!='pass':
                    error={'code':'terminal_cleanup_unconfirmed','message':'Terminal process cleanup has not been confirmed'}
                if (Path(stage['session'])/'session.json').exists():
                    # Inspect can respawn a queued durable job. A terminal turn
                    # with unconfirmed cleanup must only read its saved state.
                    try:state=Session(stage['session'])._load() if error['code']=='terminal_cleanup_unconfirmed' else Session(stage['session']).inspect()
                    except (VisualError,OSError,ValueError,KeyError):pass
            captured_pass=bool(state.get('final') and state['final'].get('pass') is True)
            report=launched.get('postturn_report',{})
            passed=(error is None and captured_pass and launched.get('status')=='completed' and native_status=='pass'
                    and report.get('status')=='pass')
            stage.update(status='completed' if passed else 'failed',launch_status=launched.get('status','launch_failed'),
                         numerical_pass=passed,captured_numerical_pass=captured_pass,native_validation_status=native_status,error=error,
                         postturn_report=report,
                         independent_review_status='pending',source_review_status='pending',
                         candidate_count=state.get('budget',{}).get('used_candidates',len(state.get('candidates',[]))),finished_at=time.time(),final=state.get('final'),
                         terminal_cleanup=launched.get('terminal_cleanup'))
            write_json(progress_path,progress)
            if not passed:
                row['status']='failed'
                for nextstage in row['stages']:
                    if nextstage['status']=='registered':nextstage['status']='not_run_predecessor_failed'
                if error and error['code']=='terminal_cleanup_unconfirmed':
                    launch_path=Path(stage['session'])/'kiro/launch.json'
                    progress['continuation_blocked']={'code':'terminal_cleanup_unconfirmed','trial_id':row['id'],
                        'session':stage['session'],'launch_sha256':file_sha256(launch_path) if launch_path.is_file() else None}
                    write_json(progress_path,progress)
                    raise VisualError('terminal_cleanup_unconfirmed','Failed trial retained; later registered trials were not started because cleanup is unconfirmed')
                break
            previous=Path(stage['session'])/'candidates'/state['selected_candidate_id']/'source'
        else:row['status']='completed'
        write_json(progress_path,progress)
    return summarize(registration)


def _review_stage(directory):
    """Replay exact selected-source reviews, never accept a report's status flag."""
    from .review import validate_independent_review,validate_source_review
    from .evidence import required_observation_bindings
    directory=Path(directory);session=Session(directory);state=session._load();session._verify_sources(state)
    selected=state.get('selected_candidate_id')
    candidate=next((c for c in state.get('candidates',[]) if c.get('candidate_id')==selected),None)
    if not candidate:return {'status':'pending','reason':'No selected candidate'}
    paths={name:directory/(name+'.json') for name in ('independent_review','source_review')}
    if not all(p.is_file() for p in paths.values()):return {'status':'pending','reason':'Independent image/source review not completed'}
    try:
        visual=validate_independent_review(paths['independent_review'],candidate_sha256=candidate['source_sha256'])
        source=validate_source_review(paths['source_review'],directory,candidate_sha256=candidate['source_sha256'])
        required=required_observation_bindings(directory,state)
        if not required or required!={tuple(v) for v in visual['required_observations']}:
            raise VisualError('incomplete_review','Reviews do not cover the selected candidate and its actual reference/baseline recordings')
        return {'status':'pass','observed_operator_coaching_count':0,
                'candidate_sha256':candidate['source_sha256'],'source_review_sha256':file_sha256(paths['source_review']),
                'visual_review_sha256':file_sha256(paths['independent_review']),
                'scope':'Independent operator review of raw images and the recorded native/source history; not third-party attestation.'}
    except (VisualError,OSError,ValueError,KeyError) as exc:
        return {'status':'fail','code':getattr(exc,'code','invalid_review'),'reason':str(exc)}


def _final_error(value):
    """Keep domain-specific units; missing evaluations never become zero error."""
    if not isinstance(value,dict):return None
    game=value.get('comparison',{}).get('mean_rmse_body')
    if isinstance(game,(int,float)):return float(game)
    scores=[c['score'] for c in value.get('comparisons',[]) if isinstance(c.get('score'),(int,float))]
    return statistics.mean(scores) if scores else None


def _stage_result(directory):
    session=Session(directory);state=session._load();final=state.get('final')
    if not final:return None
    candidate=next((c for c in state['candidates'] if c['candidate_id']==state.get('selected_candidate_id')),None)
    job=session.get_job(final['job_id'])
    if (not candidate or job.get('candidate_id')!=candidate['candidate_id'] or job.get('source_sha256')!=candidate['source_sha256']
        or digest(source_hashes(Path(directory)/'candidates'/candidate['candidate_id']/'source'))!=candidate['source_sha256']):
        raise VisualError('wrong_result','Final error must belong to the actual selected immutable source')
    if job['status']!='succeeded':return None
    if (job['result'].get('pass') is True)!=final['pass']:raise VisualError('wrong_result','Final summary differs from the actual result')
    return job['result']


def _verify_postturn_report(stage, measured, selected_candidate_id):
    """Replay the exact post-turn report bound to a completed native stage."""
    from .reporting import _portable, report_tree_receipt
    directory=Path(stage['session']).resolve();receipt=stage.get('postturn_report',{})
    data_path=directory/'report/report.json';index_path=directory/'report/index.html'
    if (receipt.get('status')!='pass' or receipt.get('data')!=str(data_path)
        or receipt.get('report')!=str(index_path)
        or not data_path.is_file() or not index_path.is_file()
        or receipt.get('data_sha256')!=file_sha256(data_path)
        or receipt.get('report_sha256')!=file_sha256(index_path)
        or receipt.get('tree')!=report_tree_receipt(directory/'report')):
        raise VisualError('invalid_report','Completed trial report is missing or changed')
    data=read_json(data_path)
    state=Session(directory)._load()
    candidate=next((c for c in state.get('candidates',[])
                    if c.get('candidate_id')==selected_candidate_id),None)
    if (candidate is None or data.get('candidate_sha256')!=candidate.get('source_sha256')
            or data.get('acceptance_details')!=_portable(measured)):
        raise VisualError('invalid_report','Report content differs from the selected source or final result')
    if (data.get('native_run_status')!='pass' or data.get('kiro_run',{}).get('status')!='completed'
        or data.get('selected_candidate_id')!=selected_candidate_id
        or (data.get('acceptance_details') or {}).get('pass')!=measured.get('pass')):
        raise VisualError('invalid_report','Report does not belong to the selected completed candidate')
    return data


def summarize(registration):
    registration=Path(registration);registry=verify_registration(registration);progress=read_json(registration.parent/'progress.json')
    if progress.get('registration_sha256')!=file_sha256(registration):raise VisualError('invalid_registration','Summary registration changed')
    _verify_progress_layout(registry,progress)
    verified={};reviewed={};review_status={};stage_results={};replay_errors=[]
    for row in progress['rows']:
        verified[row['id']]=False;reviewed[row['id']]=False;review_status[row['id']]='pending'
        for stage in row['stages']:
            try:
                if (Path(stage['session'])/'session.json').is_file():
                    stage_results[stage['session']]=_stage_result(stage['session'])
            except (VisualError,OSError,ValueError,KeyError) as exc:
                replay_errors.append({'trial_id':row['id'],'code':getattr(exc,'code','invalid_result'),'message':str(exc)})
        if row['status']!='completed':continue
        try:
            from .review import audit_native_run
            for stage in row['stages']:
                measured=stage_results.get(stage['session'])
                if not measured or measured.get('pass') is not True:
                    raise VisualError('invalid_trial','Final candidate/source/result binding did not replay as pass')
                if stage.get('numerical_pass') is not True or stage.get('native_validation_status')!='pass':
                    raise VisualError('invalid_trial','Stage was not a verified completed trial')
                session=Session(stage['session']);state=session.inspect();final=state.get('final') or {}
                if final.get('pass') is not True:raise VisualError('invalid_trial','Final capture did not pass')
                job=session.get_job(final['job_id'])
                if job['status']!='succeeded' or (job.get('result') or {}).get('pass') is not True:
                    raise VisualError('invalid_trial','Final source-bound job no longer passes')
                audit_native_run(stage['session'])
                _verify_postturn_report(stage,measured,state.get('selected_candidate_id'))
            verified[row['id']]=True
            reviews=[_review_stage(stage['session']) for stage in row['stages']]
            reviewed[row['id']]=all(r['status']=='pass' for r in reviews)
            review_status[row['id']]='pass' if reviewed[row['id']] else 'fail' if any(r['status']=='fail' for r in reviews) else 'pending'
            replay_errors.extend({'trial_id':row['id'],'code':r.get('code','invalid_review'),'message':r.get('reason')} for r in reviews if r['status']=='fail')
        except (VisualError,OSError,ValueError,KeyError) as exc:
            replay_errors.append({'trial_id':row['id'],'code':getattr(exc,'code','invalid_evidence'),'message':str(exc)})
    groups=[]
    for domain in ('game','panel','svg'):
        for mode in ('tools','baseline'):
            rows=[r for r in progress['rows'] if r['domain']==domain and r['mode']==mode]
            stages=[s for r in rows for s in r['stages']]
            finished=[s for s in stages if s.get('finished_at')]
            errors=[_final_error(stage_results.get(s['session'])) for s in stages];errors=[v for v in errors if v is not None]
            pending_review=sum(verified[r['id']] and review_status[r['id']]=='pending' for r in rows)
            groups.append({'domain':domain,'mode':mode,'required_trials':3,
                'numerical_successes':sum(verified[r['id']] for r in rows),
                'independently_verified_successes':sum(reviewed[r['id']] for r in rows),
                'captured_numerical_successes':sum(all(s.get('captured_numerical_pass') is True for s in r['stages']) for r in rows),
                'failed_trials':sum(r['status'] in ('failed','interrupted') or r['status']=='completed' and not verified[r['id']] for r in rows),
                'pending_trials':sum(r['status'] in ('registered','running') for r in rows),
                'candidate_count':sum(s.get('candidate_count',0) for s in finished),
                'elapsed_seconds':sum(s['finished_at']-s['started_at'] for s in finished),
                'final_errors':errors,'median_final_error':statistics.median(errors) if errors else None,
                'evaluated_stage_count':len(errors),'required_stage_count':len(stages),
                'error_coverage_note':'Only actual finalized measurements; absent or failed measurements are missing, not zero.',
                'error_units':'character heights; separate jump/ground stage errors' if domain=='game' else 'fraction of reference motion distance',
                'trials_waiting_independent_review':pending_review,
                'independent_review_status':'fail' if any(review_status[r['id']]=='fail' for r in rows) else 'pending' if pending_review or any(r['status'] in ('registered','running') for r in rows) else 'reviewed_completed_outputs',
                'observed_operator_coaching_count':0 if all(reviewed[r['id']] for r in rows) else None})
    comparisons=[]
    for domain in ('game','panel','svg'):
        arms={g['mode']:g for g in groups if g['domain']==domain};a,b=arms['tools'],arms['baseline']
        complete=not any(g['pending_trials'] or g['trials_waiting_independent_review'] for g in (a,b))
        comparisons.append({'domain':domain,'status':'descriptive_comparison_available' if complete else 'pending',
            'tools_verified_successes':a['independently_verified_successes'],'baseline_verified_successes':b['independently_verified_successes'],
            'required_trials_per_arm':3,'verified_success_count_difference':a['independently_verified_successes']-b['independently_verified_successes'],
            'median_error_difference':a['median_final_error']-b['median_final_error'] if a['median_final_error'] is not None and b['median_final_error'] is not None else None,
            'error_units':a['error_units'],'interpretation':'Descriptive results for these three independent runs per arm; no population significance or general success claim.'})
    tools_groups=[g for g in groups if g['mode']=='tools']
    integration=('pending' if any(g['pending_trials'] or g['trials_waiting_independent_review'] for g in tools_groups) else
                 'pass' if all(g['independently_verified_successes']==3 for g in tools_groups) else
                 'partial' if any(g['independently_verified_successes'] for g in tools_groups) else 'fail')
    result={'schema_version':1,'kind':'CohortSummary','model':registry['model'],'effort':registry['effort'],
            'groups':groups,'evidence_replay_errors':replay_errors,'integration_status':integration,
            'incremental_benefit_status':'comparison_available' if all(c['status']=='descriptive_comparison_available' for c in comparisons) else 'pending',
            'domain_comparisons':comparisons,
            'reason':'Report numerical, preservation, independent review and source audit separately; do not generalize from three repeats.'}
    write_json(registration.parent/'summary.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('register');p.add_argument('--output',required=True,type=Path);p.add_argument('--qualification',required=True,type=Path);p.add_argument('--game-baseline',type=Path);p.add_argument('--web-references',required=True,type=Path);p.add_argument('--reference-gate',required=True,type=Path)
    for name in ('run','summary'):
        p=sub.add_parser(name);p.add_argument('--registration',required=True,type=Path)
        if name=='run':
            p.add_argument('--trial')
            p.add_argument('--resume-after-cleanup',action='store_true',help='Reverify prior terminal cleanup, then continue only unrun trials; never rerun the failed trial')
    args=parser.parse_args()
    try:
        result=(register(args.output,args.qualification,game_baseline=args.game_baseline,web_references=args.web_references,reference_gate=args.reference_gate) if args.action=='register' else
                run_cohort(args.registration,selected_trial=args.trial,resume_after_cleanup=args.resume_after_cleanup) if args.action=='run' else summarize(args.registration))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (VisualError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'error','code':getattr(exc,'code','invalid_request'),'message':str(exc)}));return 1


if __name__=='__main__':raise SystemExit(main())
