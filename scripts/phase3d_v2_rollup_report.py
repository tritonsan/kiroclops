"""Append-only, offline Phase 3D snapshot from explicit evidence paths.

Configuration: {kind: Phase3DV2RollupInputs, schema_version: 1, domains:
{game: {stages: [{stage: jump, suite: ..., session: ...}, {stage: ground, ...}]},
panel: {stages: [{stage: panel, ...}]}, svg: {stages: [{stage: svg, ...}]}},
historical: [{label: ..., result: ...}], assets: [{label: ..., kind: video|curve|artifact|html, path: ...}]}.
Optional stage paths: calibration_preparation, calibration_review,
initial_quality, unchanged_result, source_axis, source_review, final_raw_review,
failed_preparation (a producer-bound pre-suite failure closure),
existing_report. Only named inputs and their declared receipt dependencies are
read. This command does not capture, submit jobs, run a model or alter evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import quote

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.session import Session, JOB_BINDING_FIELDS, _ordinary, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json

REQUIRED={'game':('jump','ground'),'panel':('panel',),'svg':('svg',)}
AXES=('measurement','behavior','preserved_functions','improvement','independent_raw_review','source','native_origin')
PATHS={'suite','session','calibration_preparation','calibration_review','initial_quality',
       'unchanged_result','source_axis','source_review','final_raw_review','existing_report',
       'failed_preparation','native_diagnostic','partial_final_diagnostic'}
KINDS={'video','curve','artifact','html'}
ORIGIN_PRODUCER_SHA256='1e9a75ad858cb696e78457ada25a9359f91dd684ed7a8fe41a7e385e3be71131'
FAILED_PREPARATION_PRODUCER_SHA256='2c21d6a7f6006a20b51fe973dd08305f7c8c883ad7bae91cee0eca541f36234a'
FAILED_PREPARATION_MESSAGE='Visible counter does not belong to this image observation interval'
FAILED_REPLAY_PRODUCER_SHA256='1183bc5cd93a2c9b2395dc31482d944a30297fa004ba1f52fad88bb204c562c9'
FAILED_REPLAY_MESSAGE='Two real captures did not match the fixed actual reference input intervals.'
PARTIAL_FINAL_PRODUCER_SHA256='ab248ca06688217d78e226f8a3ee725f0377ba2396efce9e783dc3a179732a16'


def need(value,message,code='invalid_rollup_evidence'):
    if not value:raise VisualError(code,message)


class Inputs:
    def __init__(self):self.files={};self.code_files={}
    def bind(self,path,expected=None):
        path=_ordinary(path);sha=file_sha256(path)
        need(expected is None or sha==expected,'Evidence bytes changed: '+str(path),'source_changed')
        need(str(path) not in self.files or self.files[str(path)]==sha,'Evidence changed during collection.','source_changed')
        self.files[str(path)]=sha
        if _code_origin(path):self.code_files[str(path)]=sha
        return path
    def read(self,path):
        path=self.bind(path);value=read_json(path)
        self.bind(path,self.files[str(path)])
        return value
    def declared(self,files):
        need(isinstance(files,dict) and bool(files),'A complete declared evidence-file map is required.')
        for name,sha in files.items():
            self.bind(name,sha)
            if _code_origin(name):self.code_files[str(_ordinary(name))]=sha
    def code(self,path,expected=None):
        path=self.bind(path,expected)
        need(_code_origin(path),'Only declared implementation Python files can be archived as code.')
        self.code_files[str(path)]=self.files[str(path)]
        return path
    def stable(self):
        for name,sha in tuple(self.files.items()):self.bind(name,sha)
    def raw_receipt(self,directory):
        directory=_ordinary(directory);receipt=self.read(directory/'evidence-receipt.json')
        self.bind(directory/'attestation.json')
        for name in ('source_files','code_sources'):self.declared(receipt[name])
        for name,sha in receipt['generated_files'].items():self.bind(safe_path(directory,name),sha)


def _code_origin(path):
    """Only implementation roots; experiment/candidate/evidence Python stays live."""
    path=Path(path).absolute();root=Path(__file__).resolve().parents[1]
    return path.suffix.lower()=='.py' and any(path.is_relative_to(root/name) for name in ('kiroclops','scripts'))


def _archive_code(inputs,output):
    archived={};written={}
    for index,(name,sha) in enumerate(sorted(inputs.code_files.items())):
        inputs.bind(name,sha)
        relative=f'snapshot/code/{index:03}-{Path(name).name}'
        target=output/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(name,target)
        need(file_sha256(target)==sha,'Archived implementation bytes changed.','source_changed')
        archived[name]={'origin_path':name,'relative_path':relative,'sha256':sha}
        written[relative]=sha
    return archived,written


def axis(status='pending',reason='Required evidence is not available.',**facts):
    return {'status':status,'reason':reason,**facts}


def _config(path,inputs):
    config=inputs.read(path)
    need(config.get('kind')=='Phase3DV2RollupInputs' and config.get('schema_version')==1,'Expected explicit v2 rollup inputs.')
    need(set(config)<= {'kind','schema_version','domains','historical','assets'},'Unknown input configuration field.')
    domains=config.get('domains',{})
    need(isinstance(domains,dict) and set(domains)<=set(REQUIRED),'Unsupported domain configuration.')
    for domain,spec in domains.items():
        need(isinstance(spec,dict) and set(spec)=={'stages'} and isinstance(spec['stages'],list),'Each domain needs an explicit stage list.')
        ids=[]
        for item in spec['stages']:
            need(isinstance(item,dict) and set(item)<={'stage'}|PATHS and item.get('stage') in REQUIRED[domain],'Unknown stage or path field.')
            ids.append(item['stage'])
            need(all(isinstance(v,str) and v for k,v in item.items() if k!='stage'),'Evidence paths must be explicit nonempty strings.')
        need(len(ids)==len(set(ids)),'A stage appears more than once.')
    need(isinstance(config.get('historical',[]),list) and isinstance(config.get('assets',[]),list),'Asset/history lists are malformed.')
    for row in config.get('historical',[]):
        need(isinstance(row,dict) and set(row)=={'label','result'} and all(isinstance(x,str) and x for x in row.values()),'Historical inputs need explicit labels and result paths.')
    for row in config.get('assets',[]):
        need(isinstance(row,dict) and set(row)<= {'label','kind','path','domain','role'}
            and set(row)>={'label','kind','path'} and row['kind'] in KINDS,'Unsupported named report asset.')
    return config


def _suite(inputs,path,stage):
    from kiroclops.local_engine import media_suite
    from kiroclops.local_engine.execution_policy import REVISED_ID,validate_policy
    from scripts.phase3d_v2_prepare import recipes
    path=_ordinary(path);suite=inputs.read(path/'suite.json');binding=inputs.read(path/'suite-binding.json')
    need(suite.get('kind')=='ReferenceMediaSuite' and suite.get('schema_version')==2
        and suite.get('stage')==stage and suite.get('calibration_frozen') is True
        and binding.get('suite_sha256')==digest(suite),'Frozen prospective suite identity differs.','suite_changed')
    execution=validate_policy(suite.get('execution_policy'),stage=stage)
    need(execution['policy_id']==REVISED_ID,'A historical execution policy cannot become a prospective success.')
    expected=recipes(stage);cases=suite.get('cases',[])
    required={(c['case_id'],c['role']) for c in expected}
    actual=[(c.get('case_id'),c.get('role')) for c in cases]
    lookup={(c['case_id'],c['role']):c for c in expected}
    need(len(actual)==len(required) and set(actual)==required
        and all(c['scenario']==lookup[c['case_id'],c['role']]['scenario']
            and c['stage']==lookup[c['case_id'],c['role']]['stage']
            and c['purpose']==lookup[c['case_id'],c['role']]['purpose'] for c in cases),
        'The required calibration, known-regression and new-held-out matrix is incomplete.','incomplete_matrix')
    need(len(suite['required_cases'])==len(required)
        and {(c['case_id'],c['role']) for c in suite['required_cases']}==required,'Required cases differ.','incomplete_matrix')
    sources=media_suite.analysis_source_hashes(2)
    need(suite.get('analysis_source_hashes')==sources and suite.get('source_epoch')==digest(sources),
        'The prospective source epoch is stale.','analysis_source_changed')
    for relative,sha in sources.items():inputs.bind(safe_path(media_suite.ROOT,relative),sha)
    if stage in ('panel','svg'):
        from kiroclops.local_engine.realistic_policy import validate_policy as validate_visual_policy
        policy=validate_visual_policy(suite.get('acceptance_policy'),domain=stage)
        need(suite.get('acceptance_profile')==policy['id'],'Visual quality profile differs.')
    else:need(suite.get('acceptance_profile')=='legacy-game-v1','Game thresholds must remain on their own preserved profile.')
    return suite


def _receipt(inputs,directory,result_name,binding_name,kind):
    directory=_ordinary(directory);value=inputs.read(directory/result_name);binding=inputs.read(directory/binding_name)
    need(binding.get('kind')==kind and binding.get('schema_version')==1
        and binding.get('result_sha256')==digest(value),'A result receipt is missing or changed.','result_changed')
    files=binding.get('output_file_hashes')
    need(isinstance(files,dict) and result_name in files and binding_name not in files,'Output-file receipt is incomplete.')
    for relative,sha in files.items():inputs.bind(safe_path(directory,relative),sha)
    return value,binding


def _job(inputs,directory,job_id,operation,candidate,state,suite,*,allow_execution_failure=False):
    from kiroclops.local_engine.media_suite import _name
    _name(job_id);folder=directory/'jobs'/job_id
    job=inputs.read(folder/'job.json');binding=inputs.read(folder/'binding.json')
    need(job.get('job_id')==job_id and job.get('operation')==operation
        and job.get('candidate_id')==candidate['candidate_id'] and job.get('source_sha256')==candidate['source_sha256']
        and job.get('source_hashes')==candidate['source_hashes']
        and binding.get('sha256')==digest({k:job[k] for k in JOB_BINDING_FIELDS}),
        'Canonical candidate/job binding differs.','wrong_job')
    need(_ordinary(job['source_path'])==directory/'candidates'/candidate['candidate_id']/'source',
        'The job used another candidate directory.','wrong_job')
    if job.get('status') in ('queued','running'):return job,None
    times=[job.get(k) for k in ('created_at','started_at','finished_at')]
    need(all(type(t) in (int,float) and math.isfinite(t) for t in times)
        and state['started_at']<=times[0]<=times[1]<=times[2], 'Actual job clock evidence is incomplete.')
    deadline=times[0]+state['budget']['max_validation_seconds'] if operation=='finalize' else state['deadline_at']
    need(times[2]<=deadline,'A phase exceeded its frozen time budget.','phase_timeout')
    if operation=='finalize':need(state.get('sealed_at') is not None and state['sealed_at']<=times[1], 'Private execution was not sealed first.')
    if allow_execution_failure and job.get('status')=='failed':
        error=job.get('error')
        need(operation=='finalize' and isinstance(error,dict) and error.get('code')=='execution_error'
            and isinstance(error.get('message'),str) and error['message']
            and job.get('result_sha256') is None and not (folder/'result.json').exists(),
            'A bound failed operation must not carry a usable result.','wrong_result')
        return job,None
    need(job.get('status')=='succeeded','The selected job did not complete.','incomplete_job')
    result=inputs.read(folder/'result.json')
    need(job.get('result_sha256')==digest(result) and result.get('suite_sha256')==digest(suite)
        and result.get('source_epoch')==suite['source_epoch'] and result.get('acceptance_profile')==suite['acceptance_profile'],
        'Result belongs to another job, suite or source epoch.','wrong_result')
    cases=[c for c in suite['cases'] if c['role'] in (('calibration',) if operation=='evaluate' else ('regression','validation'))]
    rows=result.get('comparisons',[]);keys=[(r.get('case_id'),r.get('role')) for r in rows]
    need(len(keys)==len(cases) and set(keys)=={(c['case_id'],c['role']) for c in cases},'A job omits required comparisons.','incomplete_matrix')
    return job,result


def _native_measurement_quality(inputs,directory,suite,evaluation_job,final_job):
    """Native reliability remains independent of movement's numerical verdict."""
    from kiroclops.local_engine import suite_game,suite_revision
    lookup={case['case_id']:case for case in suite['cases']}
    for job,roles in ((evaluation_job,('candidate',)),(final_job,('reference','original','before','final'))):
        for role in roles:
            rows=inputs.read(directory/'measurements'/job['job_id']/(role+'.json'))
            need(isinstance(rows,list) and rows,'Native measurement role is missing.','incomplete_matrix')
            for record in rows:
                case=lookup[record['scenario']['case_id']]
                suite_revision.verify_measurement_record(record,case['stage'],
                    expected_sources=suite_revision.measurement_source_hashes(case['stage']))
                inputs.bind(record['analysis'])
                inputs.bind(Path(record['package'])/'manifest.json')
                suite_game._reliable(suite_game._report(record),case['stage'])
    return True


def _source_native(inputs,spec,directory,state,suite,candidate,axes):
    from kiroclops.local_engine.review import validate_source_review
    if spec.get('source_axis'):
        folder=_ordinary(spec['source_axis']);receipt=inputs.read(folder/'receipt.json')
        value=inputs.read(folder/'source-axis.json');bindings=inputs.read(folder/'evidence-bindings.json')
        need(receipt.get('kind')=='UnchangedHistoricalSourceAxisReceipt' and receipt.get('status')=='pass'
            and receipt.get('source_axis_sha256')==digest(value)==bindings.get('source_axis_sha256')
            and bindings.get('consumed_files_sha256')==receipt.get('consumed_files_sha256')==digest(bindings.get('consumed_files')),
            'Historical-origin source-axis receipt changed.','source_changed')
        need(set(receipt.get('output_file_hashes',{}))=={'source-axis.json','evidence-bindings.json'},'Source-axis output closure differs.')
        for relative,sha in receipt['output_file_hashes'].items():inputs.bind(safe_path(folder,relative),sha)
        inputs.declared(bindings['consumed_files'])
        producer=Path(__file__).with_name('phase3d_v2_origin_review.py').resolve()
        need(bindings['consumed_files'].get(str(producer))==ORIGIN_PRODUCER_SHA256,
            'The source-axis producer is not the inspected frozen implementation.','producer_changed')
        inputs.bind(producer,ORIGIN_PRODUCER_SHA256)
        need(value.get('kind')=='UnchangedHistoricalSourceAxisReview' and value.get('source_axis_status')=='pass'
            and _ordinary(value['new_session_directory'])==directory and value.get('new_session_id')==state['session_id']
            and value.get('selected_measurement_candidate_id')==candidate['candidate_id']
            and value.get('source_sha256')==candidate['source_sha256'] and value.get('source_hashes')==candidate['source_hashes']
            and value.get('suite_sha256')==digest(suite) and value.get('source_epoch')==suite['source_epoch']
            and value.get('final_job_id')==state.get('final_job_id') and value.get('new_model_generation') is False,
            'Historical origin belongs to another selected measurement snapshot.','wrong_origin')
        historic=value['historical_generation_scope']
        validated=validate_source_review(historic['source_review'],historic['historical_session'],candidate_sha256=candidate['source_sha256'])
        inputs.bind(historic['source_review'],historic['source_review_sha256'])
        native=validated.get('native_validation',{})
        need(validated.get('status')=='pass' and validated.get('source_hashes')==candidate['source_hashes']
            and native.get('status')=='pass' and native.get('model')==historic['recorded_model']
            and native.get('effort')==historic['recorded_effort'] and native.get('evidence_hashes')==historic['native_evidence_hashes'],
            'Actual historical native/source replay did not validate.','invalid_native_evidence')
        axes['source']=axis('pass','Verified exact historical Kiro source; unchanged measurement snapshot.',new_code_authored=False)
        axes['native_origin']=axis('pass','Historical native generation replayed; this Session made no model claim.',historical_only=True,
            model=native['model'],effort=native['effort'])
    elif spec.get('source_review'):
        inputs.bind(spec['source_review'])
        validated=validate_source_review(spec['source_review'],directory,candidate_sha256=candidate['source_sha256'])
        native=validated.get('native_validation',{})
        need(validated.get('status')==native.get('status')=='pass','Actual current source/native replay did not validate.')
        axes['source']=axis('pass','Current selected source review replayed.',new_code_authored=True)
        axes['native_origin']=axis('pass','Current native generation evidence replayed.',historical_only=False,
            model=native.get('model'),effort=native.get('effort'))


def _native_task(inputs,directory,state):
    """Bound prompt-clock metadata is separate from completed native authorship."""
    started=state.get('started_at');deadline=state.get('deadline_at')
    prompt_clock=(state.get('clock_origin')=='model_generation'
        and type(started) in (int,float) and math.isfinite(started)
        and type(deadline) in (int,float) and math.isfinite(deadline))
    value={'status':'prompt_clock_started' if prompt_clock else 'not_started',
        'prompt_clock_started':prompt_clock,'started_at':started if prompt_clock else None,
        'deadline_at':deadline if prompt_clock else None,'clock_origin':state.get('clock_origin'),
        'completed_authorship_verified':False,'basis':'Recorded Session and launch metadata only; native/source acceptance remains separate.'}
    path=directory/'kiro/launch.json'
    if path.is_file():
        launch=inputs.read(path)
        prompt=launch.get('prompt_started_at')
        need(prompt is None or (prompt_clock and type(prompt) in (int,float) and math.isfinite(prompt)
            and started<=prompt<=deadline),'Launch prompt clock differs from the actual Session clock.','wrong_native_clock')
        value.update(status_as_recorded=launch.get('status'),native_turn_status_as_recorded=launch.get('native_turn_status'),
            model_as_recorded=launch.get('model'),effort_as_recorded=launch.get('effort'),
            prompt_started_at_as_recorded=prompt,prompt_sent_flag_as_recorded=launch.get('prompt_sent'),
            finished_at_as_recorded=launch.get('finished_at'),error_as_recorded=launch.get('error'))
    return value


def _verified_current_authorship(stage):
    axes=stage.get('axes',{})
    return (axes.get('source',{}).get('status')==axes.get('native_origin',{}).get('status')=='pass'
        and axes['source'].get('new_code_authored') is True and axes['native_origin'].get('historical_only') is False)


def _failed_native_diagnostic(inputs,path,directory,row):
    """A negative native verdict is replayed; metadata never promotes authorship."""
    from kiroclops.local_engine.review import audit_native_run
    value=inputs.read(path)
    need(value.get('kind')=='ReadOnlyNativePostResponseDiagnostic' and value.get('schema_version')==1
        and _ordinary(value.get('session',''))==directory and value.get('acceptance_override') is False
        and value.get('canonical_native_audit',{}).get('status')=='fail', 'Unexpected native failure diagnostic.')
    for item in value['evidence_copies']:
        inputs.bind(item['source'],item['sha256']);inputs.bind(item['copy'],item['sha256'])
    for item in value['source_files']:inputs.bind(item['path'],item['sha256'])
    config=inputs.read(directory/'kiro/config.json')
    for relative,sha in config['generated_hashes'].items():inputs.bind(safe_path(Path(config['target_dir'])/'.kiro',relative),sha)
    inputs.bind(config['prompt_path'])
    try:audit_native_run(directory)
    except VisualError as exc:
        actual={'status':'fail','exception_type':'VisualError','code':exc.code,'message':str(exc)}
        need(actual==value['canonical_native_audit'],'Saved native failure does not replay.','wrong_native_diagnostic')
    else:raise VisualError('wrong_native_diagnostic','A failed native diagnostic cannot substitute a successful audit.')
    row['axes']['native_origin']=axis('fail','Frozen current native reader rejected the source-bound post-response tail.',
        canonical_audit=value['canonical_native_audit'],model_deviation_evidenced_by_tail=value['model_deviation_evidenced_by_this_tail'])
    row['native_diagnostic']={'path':str(path),'sha256':digest(value),'classification':value['classification'],
        'acceptance_override':False,'current_authorship_verified':False}


def _partial_final_diagnostic(inputs,path,directory,state,suite,candidate,job,row):
    """Bind the completed frozen diagnostic output, without rerunning its pixels."""
    path=_ordinary(path);value=inputs.read(path/'diagnostic.json');receipt=inputs.read(path/'receipt.json')
    need(value.get('kind')=='FailedFinalNumericalDiagnostic' and value.get('schema_version')==1
        and value.get('status')=='partial_numerical_diagnostic'
        and receipt.get('kind')=='FailedFinalNumericalDiagnosticReceipt' and receipt.get('schema_version')==1
        and receipt.get('diagnostic_sha256')==file_sha256(path/'diagnostic.json')
        and value.get('full_behavior_acceptance') is receipt.get('full_behavior_acceptance') is False,
        'Partial numerical diagnostic is not an acceptance result.','wrong_partial_diagnostic')
    need(_ordinary(value['session'])==directory and value['session_id']==state['session_id']
        and value['original_job_id']==job['job_id'] and value['original_job_status']==job['status']=='failed'
        and value['original_job_error']==job['error'] and value['candidate_id']==candidate['candidate_id']
        and value['source_sha256']==candidate['source_sha256'] and value['suite_sha256']==digest(suite)
        and value['source_epoch']==suite['source_epoch'], 'Partial diagnostic belongs to another failed snapshot.','wrong_partial_diagnostic')
    producer=Path(__file__).with_name('phase3d_v2_partial_final.py').resolve()
    need(receipt.get('producer')=={'path':str(producer),'sha256':PARTIAL_FINAL_PRODUCER_SHA256},
        'Partial diagnostic producer differs.','producer_changed');inputs.code(producer,PARTIAL_FINAL_PRODUCER_SHA256)
    outputs=receipt.get('output_file_hashes',{})
    need(set(outputs)=={'diagnostic.json','comparisons.json','parent-preservation.json','record-bindings.json','evidence-bindings.json'},
        'Partial diagnostic output closure differs.')
    for name,sha in outputs.items():inputs.bind(safe_path(path,name),sha)
    comparisons=inputs.read(path/'comparisons.json');required={(c['case_id'],c['role']) for c in suite['cases'] if c['role']!='calibration'}
    keys=[(r['case_id'],r['role']) for r in comparisons]
    need(len(keys)==len(required) and set(keys)==required and value['required']==len(required)
        and value['passed']==sum(r.get('pass') is True for r in comparisons), 'Partial diagnostic matrix differs.','incomplete_matrix')
    need(all(value.get(k) is False for k in ('canonical_result_created','original_job_rewritten','new_model_generation',
        'new_capture_started','failed_trial_replaced')) and value['visual_boundary_status']==value['normal_level_status']=='not_evaluated',
        'Partial diagnostics cannot complete the missing executed gates.')
    row['partial_final_diagnostic']={k:value[k] for k in ('status','groups','required','passed','mean_curve_error_body',
        'retained_parent_behavior','recorded_functional_status','visual_boundary_status','normal_level_status','full_behavior_acceptance')}
    row['partial_final_diagnostic'].update(path=str(path),
        scope='Frozen producer output receipt verified; source pixels were verified by that producer, not replayed by this rollup. Canonical failed job is unchanged.')


def _candidate_sources(inputs,directory,candidate):
    source=directory/'candidates'/candidate['candidate_id']/'source';hashes=source_hashes(source)
    need(digest(hashes)==candidate['source_sha256'],'Selected source changed.','source_changed')
    for relative,sha in hashes.items():inputs.bind(safe_path(source,relative),sha)
    return {**candidate,'source_hashes':hashes}


def _failed_final(inputs,directory,state,suite,candidate,evaluation,job,row):
    need(evaluation is not None,'Failed finalization requires the actual completed selected calibration.','incomplete_job')
    need(state.get('status')=='finalization_failed' and (state.get('final') or {}).get('status')=='failed'
        and state['final'].get('job_id')==job['job_id'] and state['final'].get('pass') is False
        and state['final'].get('error')==job['error'],'Actual Session and failed final job differ.','wrong_result')
    history=[]
    for item in state['candidates']:
        source=_candidate_sources(inputs,directory,item)
        prior_job,result=_job(inputs,directory,item['job_id'],'evaluate',source,state,suite)
        need(result is not None,'Candidate history must contain completed bound calibration results.')
        history.append({'candidate_id':item['candidate_id'],'source_sha256':item['source_sha256'],
            'status_as_recorded':prior_job['status'],'eligible_as_recorded':item['eligible'],
            'score_as_recorded':item['score'],'calibration_passed':sum(c.get('pass') is True for c in result['comparisons']),
            'calibration_required':len(result['comparisons']),'full_behavior_acceptance':False})
    retained={}
    for role in ('reference','original','before','final'):
        path=directory/'measurements'/job['job_id']/(role+'.json')
        if path.is_file():
            records=inputs.read(path);need(isinstance(records,list),'Retained final index is malformed.')
            case_ids=[r.get('scenario',{}).get('case_id') for r in records]
            required={c['case_id'] for c in suite['cases'] if c['role'] in ('regression','validation')}
            need(len(case_ids)==len(set(case_ids)) and set(case_ids)<=required,'Retained final index has foreign scenarios.')
            retained[role]={'retained_records':len(records),'index_sha256':digest(records),
                'scope':'Source-bound retained index; no recovered comparison or final acceptance claimed.'}
    row.update(status='fail',complete_matrix_verified=False,calibration_candidates=history,
        window_results=evaluation['comparisons'],execution_failure={
            'job_id':job['job_id'],'operation':'finalize','status':'failed','error':job['error'],
            'source_sha256':candidate['source_sha256'],'retained_final_indexes':retained,
            'result_available':False,'full_behavior_acceptance':False})
    row['axes']['measurement']=axis('incomplete','Final operation failed; retained measurements are not a completed acceptance matrix.')
    row['axes']['behavior']=axis('not_evaluated','No authoritative final comparison result was written; tool failure is separate from movement failure.')
    row['axes']['preserved_functions']=axis('incomplete','Final boundary/normal-level and final preservation acceptance did not complete.')
    row['axes']['improvement']=axis('diagnostic','Completed calibration gains are recorded; no final motion verdict inferred.')
    return row


def _failed_preparation(inputs,spec,row):
    """Recheck a named pre-suite failure closure without opening any PNGs."""
    from scripts.phase3d_v2_prepare import recipes
    stage=spec['stage'];need(stage in ('svg','panel'),'Only the two inspected web preparation failures are supported.')
    svg=stage=='svg';code='capture_timing_failure' if svg else 'input_history_mismatch'
    message=FAILED_PREPARATION_MESSAGE if svg else FAILED_REPLAY_MESSAGE
    counts=(18,6,54,19) if svg else (7,17,23,8)
    need(not any(spec.get(k) for k in ('suite','session','initial_quality','unchanged_result','source_axis',
        'source_review','final_raw_review','existing_report')), 'A pre-suite failure cannot replace later evidence.')
    directory=_ordinary(spec['failed_preparation'])
    closure=inputs.read(directory/'closure.json');receipt=inputs.read(directory/'receipt.json')
    bindings=inputs.read(directory/'evidence-bindings.json')
    need(closure.get('kind')=='FailedReferenceSuitePreparationClosure' and closure.get('schema_version')==1
        and closure.get('stage')==spec['stage'] and closure.get('status')=='measurement_insufficient'
        and closure.get('measurement_status')=='fail' and closure.get('behavior_status')=='not_evaluated',
        'Expected the source-bound preparation clock failure.')
    false_fields=('suite_created','session_created','model_started','native_prompt_sent','new_model_generation',
        'candidate_budget_consumed','admission_started','initial_quality_started','full_behavior_acceptance',
        'historical_acceptance_changed','automatic_replacement_started','capture_restarted')
    need(all(closure.get(k) is False for k in false_fields),'A preparation failure cannot claim later progress.')
    need(receipt.get('kind')=='FailedReferenceSuitePreparationReceipt' and receipt.get('schema_version')==1
        and receipt.get('status')=='measurement_insufficient' and receipt.get('full_behavior_acceptance') is False
        and receipt.get('closure_sha256')==digest(closure), 'Failure receipt differs.','result_changed')
    need(bindings.get('kind')=='FailedPreparationEvidenceBindings' and bindings.get('schema_version')==1
        and bindings.get('consumed_files_sha256')==receipt.get('consumed_files_sha256')
            ==closure.get('source_bindings_sha256')==digest(bindings.get('consumed_files')),
        'Failure source bindings differ.','source_changed')
    producer=_ordinary(Path(__file__).with_name('phase3d_v2_failed_preparation.py' if svg else 'phase3d_v2_failed_replay_preparation.py'))
    identity={'path':str(producer),'sha256':FAILED_PREPARATION_PRODUCER_SHA256 if svg else FAILED_REPLAY_PRODUCER_SHA256}
    need(closure.get('producer')==receipt.get('producer')==identity
        and bindings['consumed_files'].get(str(producer))==identity['sha256'],
        'Failure producer is not the inspected frozen script.','producer_changed')
    inputs.bind(producer,identity['sha256'])
    generated={'closure.json','evidence-bindings.json'}
    if 'diagnostic_sha256' in closure:
        diagnostic=inputs.read(directory/'diagnostic.json');generated.add('diagnostic.json')
        need(closure['diagnostic_sha256']==digest(diagnostic)
            and diagnostic.get('kind')==('VisibleClockFailureDiagnostic' if svg else 'ActualInputReplayFailureDiagnostic')
            and diagnostic.get('case_id')==closure['failed_attempt']['case_id']
            and diagnostic.get('full_behavior_acceptance') is False and diagnostic.get('capture_started') is False,
            'The optional clock diagnostic belongs to another failure.','wrong_result')
    else:diagnostic=None
    if not svg:need(diagnostic is not None,'The inspected input-replay failure requires its actual diagnostic.')
    need(set(receipt.get('output_file_hashes',{}))==generated,'Failure output closure differs.')
    for name,sha in receipt['output_file_hashes'].items():inputs.bind(safe_path(directory,name),sha)
    failed=_ordinary(closure['failed_preparation_directory']);previous=_ordinary(closure['previous_calibration_directory'])
    need(_ordinary(closure['previous_calibration'])==previous
        and spec.get('calibration_preparation') and _ordinary(spec['calibration_preparation'])==previous,
        'Failure belongs to another calibration preparation.','wrong_preparation')
    expected_session=failed.parents[1]/'s'/failed.name
    need(_ordinary(closure['expected_session_directory'])==expected_session
        and closure.get('preparation_id')==failed.name and not expected_session.exists()
        and all(not (failed/name).exists() for name in ('suite','result.json','calibration.json')),
        'Actual suite, Session or completed result exists; this is not a pre-suite failure.')
    expected_paths={str(producer)}
    def read(path):
        path=_ordinary(path);expected_paths.add(str(path));return inputs.read(path)
    failure=read(failed/'failure.json');progress=read(failed/'progress.json')
    saved=read(previous/'calibration.json');cases=read(previous/'recipes.json');starts=read(previous/'start-binding.json')
    need(saved.get('stage')==starts.get('stage')==spec['stage'] and saved.get('model_started') is False
        and starts.get('model_started') is False and cases==recipes(spec['stage'])
        and starts.get('recipes_sha256')==digest(cases),'Canonical calibration or recipe binding differs.','wrong_preparation')
    need(failure==closure['failure'] and failure.get('code')==code
        and failure.get('message')==message and failure.get('model_started') is False
        and failure.get('previous_attempts_preserved') is True and progress.get('model_started') is False,
        'Actual failure is not the inspected preparation error.')
    need(closure.get('failure_classification')==('visual_clock_capture_timing_failure' if svg else 'two_actual_input_history_mismatches'),
        'Preparation failure classification differs.')
    private=[c for c in cases if c['role']!='calibration'];complete=[];incomplete=[];records=[];failed_attempt=None
    need(len(private)==24 and len({c['case_id'] for c in private})==24,'Private recipe matrix differs.','incomplete_matrix')
    for ordinal,case in enumerate(private,1):
        case_id=case['case_id'];need(safe_path(failed/'reference',case_id,must_exist=False).parent==failed/'reference','Unsafe case path.')
        present=[];absent=[]
        for role in ('reference','original','before'):
            path=failed/role/case_id/'record.json'
            if path.is_file():
                record=read(path)
                need(record.get('measurement_status')=='ok' and record.get('scenario')==case['scenario'],
                    'A retained record is not complete or uses another scenario.','wrong_result')
                if role!='reference':need(record.get('source_sha256')==starts['original_source_sha256' if role=='original' else 'parent_source_sha256'],
                    'A retained target record belongs to another starting source.','source_changed')
                records.append({'case_id':case_id,'role':role,'source_role':role,'record_path':str(_ordinary(path)),
                    'record_file_sha256':inputs.files[str(_ordinary(path))],'record_sha256':digest(record),
                    'source_sha256':record.get('source_sha256'),
                    'scope':'retained completed record receipt; not a new raw/behavior acceptance' if svg else 'retained completed receipt; no new acceptance'})
                present.append(role)
            else:absent.append(role)
            replay_path=path.parent/'capture-replay.json'
            if not svg and replay_path.is_file():
                replay=read(replay_path)
                if replay.get('status')=='input_history_mismatch':
                    need(failed_attempt is None and role=='before' and not path.exists()
                        and replay.get('selected_attempt') is None and replay.get('duration_rescaled') is False
                        and [a.get('attempt') for a in replay.get('attempts',[])]==[1,2],
                        'Exactly two unselected before-role failed captures are required.')
                    from kiroclops.local_engine.realistic_policy import input_history_check,replay_scenario
                    reference=read(failed/'reference'/case_id/'record.json')
                    execution,proof=replay_scenario(case['scenario'],reference['observations']);attempts=[]
                    for item in replay['attempts']:
                        attempt=path.parent/f"attempt-{item['attempt']:02d}"
                        need(_ordinary(item['directory'])==_ordinary(attempt),'Input attempt directory is foreign.')
                        attempt_receipt=read(attempt/'capture-attempt.json');record=attempt_receipt['record']
                        start=read(attempt/'capture-start.json')
                        need(digest(attempt_receipt)==item.get('receipt_sha256') and attempt_receipt.get('attempt')==item['attempt']
                            and _ordinary(attempt_receipt['directory'])==_ordinary(attempt)
                            and attempt_receipt.get('status')==item.get('status')=='input_history_mismatch',
                            'Actual replay attempt receipt changed.','result_changed')
                        need(record.get('scenario')==case['scenario'] and record.get('execution_scenario')==execution
                            and record.get('input_replay')==proof and start.get('scenario')==execution
                            and record.get('source_sha256')==starts.get('parent_source_sha256')
                            and digest(start.get('source_hashes'))==record['source_sha256'],
                            'Actual pointer recipe or captured source differs.','source_changed')
                        history=input_history_check(reference['observations'],record['observations'])
                        need(history==attempt_receipt.get('input_history')==item.get('input_history')
                            and history.get('pass') is False and history.get('duration_rescaled') is False,
                            'Actual inputs do not reproduce the claimed mismatch.','wrong_input_history')
                        attempts.append({'attempt':item['attempt'],'directory':str(_ordinary(attempt)),
                            'receipt_file_sha256':inputs.files[str(_ordinary(attempt/'capture-attempt.json'))],
                            'receipt_sha256':digest(attempt_receipt),'source_sha256':record['source_sha256'],
                            'input_history':history,'failed_events':[p for p in history['differences'] if p['pass'] is False],
                            'source_hashes':start['source_hashes'],'adapter_hashes':start.get('adapter_hashes')})
                    failed_attempt={'case_id':case_id,'ordinal':ordinal,'role':role,'directory':str(_ordinary(path.parent)),
                        'replay_file_sha256':inputs.files[str(_ordinary(replay_path))],'source_sha256':attempts[0]['source_sha256'],
                        'attempts':attempts,'selected_attempt':None,'duration_rescaled':False}
        entry={'ordinal':ordinal,'case_id':case_id,'split_role':case['role'],'present_roles':present,'missing_roles':absent}
        (complete if not absent else incomplete).append(entry)
        attempt=failed/'reference'/case_id/'attempt-01'
        if svg and (attempt/'capture-error.json').is_file():
            need(failed_attempt is None,'There must be one interrupted reference attempt.')
            error=read(attempt/'capture-error.json');start=read(attempt/'capture-start.json')
            need(error.get('code')==failure['code'] and error.get('message')==failure['message']
                and start.get('scenario')==case['scenario'] and start.get('variant')=='reference',
                'Capture error is not bound to the actual recipe.')
            failed_attempt={'case_id':case_id,'ordinal':ordinal,'directory':str(_ordinary(attempt)),
                'capture_error_sha256':inputs.files[str(_ordinary(attempt/'capture-error.json'))],
                'capture_start_sha256':inputs.files[str(_ordinary(attempt/'capture-start.json'))],
                'source_hashes':start.get('source_hashes'),'adapter_hashes':start.get('adapter_hashes')}
            for name in ('received-evidence.json','frame-receipts.json','clock-runtime.json','progress-runtime.json',
                'transport-context.json','transport-order.json','stream-baseline.json','preflight.json'):read(attempt/'private'/name)
    need((len(complete),len(incomplete),len(records))==counts[:3] and failed_attempt is not None
        and failed_attempt['ordinal']==counts[3] and (not svg or progress['phase'].startswith(failed_attempt['case_id']+':')),
        'Actual named-path completion differs from the failed preparation.','incomplete_matrix')
    need(closure.get('completed_cases')==complete and closure.get('incomplete_cases')==incomplete
        and closure.get('completed_records')==records and closure.get('failed_attempt')==failed_attempt
        and _ordinary(closure['attempt_directory'])==_ordinary(failed_attempt['directory'])
        and (closure.get('private_case_count'),closure.get('completed_case_count'),closure.get('missing_case_count'),
            closure.get('completed_role_record_count'))==(24,*counts[:3]),
        'Failure counts, identities or retained records changed.','incomplete_matrix')
    root=Path(__file__).resolve().parents[1]
    source_names=('scripts/phase3d_v2_prepare.py','kiroclops/local_engine/suite_web_v2.py',
        'kiroclops/web_local/capture.py','kiroclops/web_local/visual_clock.py') if svg else (
        'scripts/phase3d_v2_failed_preparation.py','scripts/phase3d_v2_prepare.py','kiroclops/local_engine/suite_web_v2.py',
        'kiroclops/local_engine/realistic_policy.py','kiroclops/web_local/capture.py')
    for relative in source_names:expected_paths.add(str(_ordinary(root/relative)))
    if not svg:
        inputs.bind(root/'scripts/phase3d_v2_failed_preparation.py',FAILED_PREPARATION_PRODUCER_SHA256)
        need(diagnostic.get('attempts')==failed_attempt['attempts'] and diagnostic.get('role')=='before'
            and diagnostic.get('attempt_count')==2 and diagnostic.get('comparison_recomputed_from_actual_input_metadata') is True
            and all(diagnostic.get(k) is False for k in ('model_started','thresholds_changed','replay_rescaled','raw_png_decoded_or_hashed')),
            'Actual replay diagnostic does not match the two immutable attempts.')
    need(set(bindings['consumed_files'])==expected_paths,'Failure metadata dependency closure differs.','source_changed')
    inputs.declared(bindings['consumed_files'])
    row.update(status='fail',complete_matrix_verified=False,model_started=False,
        acceptance_profile='realistic-behavior-v1',failed_preparation={
            'status':closure['status'],'code':failure['code'],'message':failure['message'],
            'private_case_count':24,'completed_case_count':counts[0],'missing_case_count':counts[1],'completed_role_record_count':counts[2],
            'case_id':failed_attempt['case_id'],'candidate_budget_consumed':False,'model_started':False,
            'full_behavior_acceptance':False,'raw_images_revalidated':False,'closure_sha256':digest(closure),
            'failure_classification':closure['failure_classification'],
            **({'input_attempts':failed_attempt['attempts']} if not svg else {})})
    row['axes']['measurement']=axis('fail',('Actual visible-clock recording failure' if svg else 'Two actual captures failed fixed input comparability')
        +' before suite/session creation.',code=failure['code'])
    for name in ('behavior','preserved_functions','improvement','source','native_origin'):
        row['axes'][name]=axis('not_evaluated','No complete suite, selected candidate or new Kiro task exists.')
    row['axes']['independent_raw_review']=axis('pending','Final raw review is unavailable; calibration review does not grant final acceptance.')
    return row


def _stage(inputs,spec):
    stage=spec['stage'];axes={name:axis() for name in AXES}
    axes['improvement']=axis('pending','Diagnostic gain is separate from mandatory final quality.')
    row={'stage':stage,'axes':axes,'status':'pending','errors':[],'window_results':[], 'artifacts':deepcopy_paths(spec)}
    try:
        if spec.get('failed_preparation'):return _failed_preparation(inputs,spec,row)
        suite_path=spec.get('suite');state=None;directory=None
        if spec.get('session'):
            directory=_ordinary(spec['session']);inputs.bind(directory/'session.json');inputs.bind(directory/'binding.json')
            state=Session(directory)._load()
            need(state['experiment'].get('stage')==stage and state.get('schema_version')==2,'Foreign or historical Session.','wrong_session')
            if suite_path:need(_ordinary(suite_path)==_ordinary(state['reference']),'Session points to a different suite.','wrong_suite')
            suite_path=state['reference']
            row.update(session_status=state['status'],budget=state['budget'],runtime=state.get('capture_lock'),
                clock_origin=state.get('clock_origin'),candidate_count=len(state.get('candidates',[])))
            row['native_task']=_native_task(inputs,directory,state)
            readiness=directory/'environment-readiness.json'
            if readiness.is_file():row['runtime']=inputs.read(readiness).get('runtime')
        calibrated=None
        if spec.get('calibration_review') and spec.get('calibration_preparation') and stage in ('panel','svg'):
            from scripts.phase3d_v2_prepare import verify_calibration_review
            from kiroclops.local_engine.realistic_review import calibration_review
            from kiroclops.local_engine.realistic_policy import build_policy
            verified=verify_calibration_review(spec['calibration_preparation'],spec['calibration_review'])
            inputs.bind(Path(spec['calibration_preparation'])/'calibration.json')
            inputs.bind(Path(spec['calibration_preparation'])/'start-binding.json')
            inputs.raw_receipt(spec['calibration_review'])
            calibrated=build_policy(stage,calibration_review(spec['calibration_review']))
            row['calibration_review']={'status':'pass','numeric_status':verified['numeric_status'],'final_behavior_granted':False}
            row['pixel_floors']=calibrated['floors_px']
        if not suite_path:return row
        suite=_suite(inputs,suite_path,stage)
        if state:need(state['experiment'].get('suite_sha256')==digest(suite),'Session suite digest differs.','wrong_suite')
        row.update(acceptance_profile=suite['acceptance_profile'],source_epoch=suite['source_epoch'],
            acceptance_policy=suite.get('acceptance_policy'),execution_policy=suite['execution_policy'],
            required_matrix=dict(Counter(c['role'] for c in suite['cases'])),pixel_floors=(suite.get('acceptance_policy') or {}).get('floors_px'))
        if calibrated:need(calibrated==suite['acceptance_policy'],'Frozen pixel floors belong to another review.')
        if spec.get('initial_quality'):
            initial,receipt=_receipt(inputs,spec['initial_quality'],'result.json','result-binding.json','ExistingParentInitialQualityReceipt')
            need(initial.get('suite_sha256')==digest(suite) and initial.get('source_epoch')==suite['source_epoch'],'Initial result belongs to another suite.')
            bound=inputs.read(Path(spec['initial_quality'])/'source-bindings.json')
            need(receipt.get('input_bindings_sha256')==digest(bound['consumed_files']),'Initial input bindings differ.')
            inputs.declared(bound['consumed_files']);row['initial_quality']={'status':initial['status'],'complete':initial['complete'],
                'groups':initial.get('groups'),'full_behavior_granted':False}
        if spec.get('unchanged_result'):
            unchanged,_=_receipt(inputs,spec['unchanged_result'],'result.json','result-binding.json','NoCodeChangeStageResultReceipt')
            need(unchanged.get('suite_sha256')==digest(suite) and unchanged.get('stage')==stage,
                'Unchanged-stage result belongs to another suite.')
            row['unchanged_stage']={k:unchanged.get(k) for k in ('status','source_unchanged','numerical_pass','new_model_generation','full_behavior_acceptance')}
        if not state or not state.get('selected_candidate_id'):return row
        candidates=[c for c in state['candidates'] if c['candidate_id']==state['selected_candidate_id']]
        need(len(candidates)==1,'Selected candidate identity is ambiguous.')
        candidate=_candidate_sources(inputs,directory,candidates[0])
        row['selected_candidate']={k:candidate.get(k) for k in ('candidate_id','source_sha256','status','eligible')}
        evaluation_job,evaluation=_job(inputs,directory,candidate['job_id'],'evaluate',candidate,state,suite)
        if not state.get('final_job_id') or state.get('sealed_at') is None:return row
        final_job,final=_job(inputs,directory,state['final_job_id'],'finalize',candidate,state,suite,allow_execution_failure=True)
        if final_job['status']=='failed':
            row=_failed_final(inputs,directory,state,suite,candidate,evaluation,final_job,row)
            if spec.get('native_diagnostic'):_failed_native_diagnostic(inputs,spec['native_diagnostic'],directory,row)
            if spec.get('partial_final_diagnostic'):_partial_final_diagnostic(inputs,spec['partial_final_diagnostic'],directory,state,suite,candidate,final_job,row)
            return row
        if evaluation is None or final is None:return row
        need((state.get('final') or {}).get('job_id')==final_job['job_id']
            and (state.get('final') or {}).get('status')=='succeeded'
            and (state.get('final') or {}).get('pass') is final.get('pass'),'Terminal Session/final result differs.','wrong_result')
        all_rows=evaluation['comparisons']+final['comparisons']
        need(final.get('groups')=={role:{'required':sum(c['role']==role for c in suite['cases']),
            'passed':sum(c.get('pass') is True for c in final['comparisons'] if c['role']==role)} for role in ('regression','validation')},
            'Saved held-out group summary differs.','incomplete_matrix')
        row['window_results']=all_rows
        measured=(_native_measurement_quality(inputs,directory,suite,evaluation_job,final_job) if stage in ('jump','ground')
            else all(c.get('measurement_pass') is True for c in all_rows))
        from kiroclops.local_engine.suite_evidence import required_bindings
        # Sealed/terminal only: incomplete pre-model reports never open private pixels.
        required=required_bindings(directory,state,require_complete=True)
        need(bool(required),'The complete actual pixel matrix is missing.','incomplete_matrix')
        row['complete_matrix_verified']=True
        axes['measurement']=axis('pass' if measured else 'fail','Complete source-bound measurement matrix verified.',observation_bindings=len(required))
        passed=all(c.get('pass') is True for c in all_rows)
        axes['behavior']=axis('pass' if passed else 'fail','Every required saved comparator verdict; no thresholds recomputed.',new_validation=final.get('groups',{}).get('validation'))
        functional=all(r.get('preservation',{}).get('status')=='pass' and r.get('preservation_pass') is True for r in (evaluation,final))
        if stage in ('jump','ground'):functional=functional and final.get('visual_wall_status')==final.get('normal_level_status')=='pass'
        axes['preserved_functions']=axis('pass' if functional else 'fail','Executed candidate application and parent-preservation checks.',
            calibration=evaluation.get('preservation'),final=final.get('preservation'))
        diagnostic=[]
        for comparison in all_rows:
            windows=comparison.get('per_window_gates') or [comparison]
            for window in windows:
                diagnostic.append({**{k:window.get(k) for k in ('classification','improvement_fraction')},
                    'gain_required':window.get('gain_required',window.get('improvement_required'))})
        axes['improvement']=axis('recorded_game_gate' if stage in ('jump','ground') else 'diagnostic',
            'Original game improvement gates are retained in behavior verdicts.' if stage in ('jump','ground')
            else 'Browser gain is diagnostic and does not replace final absolute quality.',windows=diagnostic)
        row['input_history']=[c.get('input_history') for c in all_rows if c.get('input_history')]
        if spec.get('final_raw_review'):
            from kiroclops.local_engine.review import validate_independent_review
            inputs.bind(spec['final_raw_review'])
            raw=validate_independent_review(spec['final_raw_review'],candidate_sha256=candidate['source_sha256'],session_directory=directory)
            need({tuple(p) for p in raw.get('required_observations',[])}==required,'Raw review does not cover the exact final matrix.','incomplete_review')
            axes['independent_raw_review']=axis('pass','Independent raw-pixel position/event/relationship review replayed.',record_count=raw.get('record_count'))
            raw_path=raw.get('raw_review_directory')
            if raw_path:inputs.raw_receipt(raw_path)
        _source_native(inputs,spec,directory,state,suite,candidate,axes)
        row['native_task']['completed_authorship_verified']=_verified_current_authorship(row)
        required_axes=[axes[k]['status'] for k in AXES if k!='improvement']
        row['status']='pass' if all(s=='pass' for s in required_axes) else 'fail' if 'fail' in required_axes else 'pending'
    except (VisualError,OSError,ValueError,KeyError,TypeError) as exc:
        row['status']='invalid';row['errors'].append({'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
    return row


def deepcopy_paths(spec):return {k:v for k,v in spec.items() if k in PATHS}


def _curve(value):
    observed=value.get('observations',value);series=[]
    for obj in observed.get('objects',[]):
        points=[];segments=[];segment=[];origin=None
        for frame in obj.get('frames',[]):
            valid=frame.get('valid') is True and all(type(frame.get(k)) in (int,float)
                and math.isfinite(frame[k]) for k in ('time_s','x','y'))
            if valid:
                origin=origin or frame
                point=[frame['time_s'],frame['x']-origin['x'],frame['y']-origin['y']]
                points.append(point);segment.append(point)
            elif segment:segments.append(segment);segment=[]
        if segment:segments.append(segment)
        if points:series.append({'object_id':obj.get('id'),'points':points,'segments':segments})
    return series


def _chart(series,coordinate=1):
    points=[p for s in series for p in s['points']]
    if not points:return '<p>No valid plotted observations.</p>'
    t0,t1=min(p[0] for p in points),max(p[0] for p in points)
    ys=[p[coordinate] for p in points];lo,hi=min(ys),max(ys)
    lines=[]
    for i,series in enumerate(series):
        color=['#60a5fa','#34d399','#fbbf24','#f472b6'][i%4]
        for segment in series.get('segments',[series['points']]):
            pairs=[(20+560*(p[0]-t0)/max(t1-t0,.001),180-150*(p[coordinate]-lo)/max(hi-lo,.001)) for p in segment]
            if len(pairs)==1:lines.append(f'<circle cx="{pairs[0][0]:.2f}" cy="{pairs[0][1]:.2f}" r="3" fill="{color}"/>')
            else:lines.append('<polyline points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y in pairs)+f'" fill="none" stroke="{color}" stroke-width="2"/>')
    label='x' if coordinate==1 else 'y'
    return '<svg viewBox="0 0 600 200" role="img" aria-label="Measured '+label+' displacement against actual time">'+''.join(lines)+'</svg><p class="muted">Actual time; '+label+' displacement from first measured point. Presentation only.</p>'


def _href(path,output):
    path=Path(path).absolute();output=Path(output).absolute()
    try:return quote(os.path.relpath(path,output).replace(os.sep,'/'),safe='/')
    except ValueError:return path.as_uri()  # Explicit file links across Windows drives.


def _html(value,output):
    esc=lambda x:html.escape(str(x));cards=[]
    for domain,group in value['domains'].items():
        parts=[f'<h2>{esc(domain)} · {esc(group["status"])}</h2>']
        for stage in group['stages']:
            parts.append(f'<h3>{esc(stage["stage"])} · {esc(stage["status"])}</h3><table><tr><th>Axis</th><th>Status</th><th>Scope</th></tr>')
            for name,data in stage['axes'].items():parts.append(f'<tr><td>{esc(name)}</td><td>{esc(data["status"])}</td><td>{esc(data["reason"])}</td></tr>')
            parts.append('</table>')
            parts.append('<details><summary>Floors, clocks, budget, matrix and every window verdict</summary><pre>'+esc(json.dumps(stage,ensure_ascii=False,indent=2))+'</pre></details>')
            for label,path in stage['artifacts'].items():
                href=Path(path)/'index.html' if label=='existing_report' and Path(path).is_dir() else Path(path)
                parts.append(f'<p><a href="{esc(_href(href,output))}">{esc(label)}: {esc(href)}</a></p>')
        cards.append('<section>'+''.join(parts)+'</section>')
    assets=[]
    for asset in value['assets']:
        parts=[f'<h3>{esc(asset["label"])}</h3>']
        if asset['kind']=='video':parts.append(f'<video controls preload="metadata" src="{esc(quote(asset["relative_path"],safe="/"))}"></video>')
        elif asset['kind']=='curve':parts.append(_chart(asset.get('series',[]))+_chart(asset.get('series',[]),2))
        parts.append(f'<a href="{esc(quote(asset["relative_path"],safe="/"))}">Open named artifact</a>');assets.append('<section>'+''.join(parts)+'</section>')
    historical='<pre>'+esc(json.dumps(value['historical'],ensure_ascii=False,indent=2))+'</pre>'
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>KiroClops prospective behavior evidence</title><style>body{font:16px system-ui;background:#101827;color:#e5e7eb;max-width:1100px;margin:40px auto;padding:0 20px}section{background:#172235;padding:24px;margin:20px 0;border-radius:12px}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:10px;border-bottom:1px solid #374151}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#93c5fd}video{width:100%;max-height:500px}.muted{color:#9ca3af}svg{width:100%;max-width:800px}</style>'
        f'<h1>KiroClops · prospective acceptance: {esc(value["overall_status"])}</h1><p>Missing evidence is pending. Numerical quality, executed functions, independent pixels and Kiro source/native origin are separate.</p>'
        '<p>The report producer starts no capture or model. Bound experiment prompt clocks and verified completed Kiro authorship are recorded separately below.</p>'
        '<p>New browser profile: realistic-behavior-v1. Game thresholds retain legacy-game-v1. Historical decisions remain unchanged; no new model success is inferred from an unchanged-source measurement.</p>'
        +''.join(cards+assets)+'<section><h2>Historical results · original profile only</h2>'+historical+'</section></html>')


def build(config_path,output):
    inputs=Inputs();config_path=_ordinary(config_path);output=_ordinary(output)
    need(not output.exists(),'Preserve earlier rollups; use a fresh output.','output_exists')
    config=_config(config_path,inputs);inputs.code(__file__)
    need(not config_path.is_relative_to(output),'Output cannot contain its input configuration.','unsafe_path')
    for spec in config.get('domains',{}).values():
        for stage in spec['stages']:
            for name,path in deepcopy_paths(stage).items():
                protected=_ordinary(path)
                need(not output.is_relative_to(protected) and not protected.is_relative_to(output),
                    'Output must be outside named evidence.','unsafe_path')
    domains={}
    for domain,required in REQUIRED.items():
        specs={s['stage']:s for s in config.get('domains',{}).get(domain,{}).get('stages',[])}
        stages=[_stage(inputs,specs.get(stage,{'stage':stage})) for stage in required]
        status='pass' if all(s['status']=='pass' for s in stages) else 'invalid' if any(s['status']=='invalid' for s in stages) else 'fail' if any(s['status']=='fail' for s in stages) else 'pending'
        domains[domain]={'status':status,'required_stages':list(required),'stages':stages}
    historical=[]
    for row in config.get('historical',[]):
        saved=inputs.read(row['result']);historical.append({'label':row['label'],'path':row['result'],
            'status_as_recorded':saved.get('status',saved.get('overall_acceptance')),
            'profile_as_recorded':saved.get('acceptance_profile','historical profile'),
            'result_sha256':digest(saved),'promoted_to_new_acceptance':False})
    value={'kind':'Phase3DV2Rollup','schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),
        'overall_status':'pass' if all(d['status']=='pass' for d in domains.values()) else 'invalid' if any(d['status']=='invalid' for d in domains.values()) else 'fail' if any(d['status']=='fail' for d in domains.values()) else 'pending',
        'full_success':all(d['status']=='pass' for d in domains.values()),'domains':domains,'historical':historical,
        'assets':[],'new_model_generation_claimed':any(_verified_current_authorship(stage) for group in domains.values() for stage in group['stages']),
        'bound_model_task_started':any(stage.get('native_task',{}).get('prompt_clock_started') is True for group in domains.values() for stage in group['stages']),
        'report_producer':{'capture_started':False,'model_started':False},'historical_results_changed':False,
        'recording_or_model_started':False,'comparison_thresholds_recomputed':False}
    output.mkdir(parents=True)
    written={}
    for index,asset in enumerate(config.get('assets',[])):
        source=inputs.bind(asset['path']);relative=f'assets/{index:03}-{source.name}'
        target=output/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        need(file_sha256(target)==inputs.files[str(source)],'Copied presentation asset changed.','source_changed')
        item={**asset,'relative_path':relative,'sha256':file_sha256(target)}
        if asset['kind']=='curve':item['series']=_curve(inputs.read(source))
        value['assets'].append(item);written[relative]=item['sha256']
    inputs.stable();value['code_snapshot'],code_outputs=_archive_code(inputs,output);written.update(code_outputs)
    inputs.stable();value['consumed_file_sha256']=inputs.files
    write_json(output/'report.json',value);(output/'index.html').write_text(_html(value,output),encoding='utf-8')
    written.update({name:file_sha256(output/name) for name in ('report.json','index.html')})
    write_json(output/'receipt.json',{'kind':'Phase3DV2RollupReceipt','schema_version':1,'report_sha256':digest(value),
        'output_file_sha256':written,'consumed_files_sha256':digest(inputs.files),'full_success':value['full_success']})
    return value


def validate_report(output):
    output=_ordinary(output);value=read_json(output/'report.json');receipt=read_json(output/'receipt.json')
    need(receipt.get('kind')=='Phase3DV2RollupReceipt' and receipt.get('schema_version')==1
        and receipt.get('report_sha256')==digest(value)
        and receipt.get('consumed_files_sha256')==digest(value.get('consumed_file_sha256')),
        'Rollup receipt differs.','result_changed')
    files=value['consumed_file_sha256'];archives=value.get('code_snapshot',{});inputs=Inputs()
    need(isinstance(archives,dict),'Declared code snapshot is malformed.')
    need(set(archives)=={name for name in files if _code_origin(name)},
        'Every consumed implementation origin requires its archived bytes.','invalid_code_snapshot')
    archive_paths=[]
    for name,entry in archives.items():
        need(_code_origin(name) and name in files and isinstance(entry,dict)
            and set(entry)=={'origin_path','relative_path','sha256'} and entry['origin_path']==name
            and entry['sha256']==files[name] and entry['relative_path'].startswith('snapshot/code/'),
            'Only an exactly declared implementation origin can use archived bytes.','invalid_code_snapshot')
        inputs.bind(safe_path(output,entry['relative_path']),entry['sha256']);archive_paths.append(entry['relative_path'])
    need(len(archive_paths)==len(set(archive_paths)),'Archived code paths collide.','invalid_code_snapshot')
    for name,sha in files.items():
        if name not in archives:inputs.bind(name,sha)
    need(set(receipt.get('output_file_sha256',{}))=={'report.json','index.html'}|{a['relative_path'] for a in value['assets']}|set(archive_paths),
        'Presentation output closure differs.')
    for name,sha in receipt['output_file_sha256'].items():inputs.bind(safe_path(output,name),sha)
    passed=[]
    for domain,required in REQUIRED.items():
        group=value['domains'].get(domain,{})
        rows=group.get('stages',[])
        need(len(rows)==len(required) and {r.get('stage') for r in rows}==set(required),
            'Required domain/stage structure changed.','incomplete_matrix')
        derived=all(r.get('complete_matrix_verified') is True and all(r.get('axes',{}).get(k,{}).get('status')=='pass'
            for k in AXES if k!='improvement') for r in rows)
        need((group.get('status')=='pass') is derived,'A claimed domain success lacks mandatory axes.','incomplete_matrix')
        passed.append(derived)
    expected=all(passed)
    need(value.get('new_model_generation_claimed') is any(_verified_current_authorship(stage) for group in value['domains'].values() for stage in group['stages']),
        'New authorship cannot be claimed without completed current source/native acceptance.')
    need(value.get('bound_model_task_started',False) is any(stage.get('native_task',{}).get('prompt_clock_started') is True
        for group in value['domains'].values() for stage in group['stages']), 'Bound model-task metadata differs.')
    need(value['full_success'] is expected and receipt['full_success'] is expected
        and (value['overall_status']=='pass') is expected,'Incomplete domains cannot become full success.')
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    make=commands.add_parser('build');make.add_argument('--config',type=Path,required=True);make.add_argument('--output',type=Path,required=True)
    check=commands.add_parser('validate');check.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:result=build(args.config,args.output) if args.command=='build' else validate_report(args.output)
    except (VisualError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'status':'blocked','code':getattr(exc,'code',type(exc).__name__),'message':str(exc)}));return 1
    print(json.dumps({k:result[k] for k in ('overall_status','full_success','recording_or_model_started')}));return 0


if __name__=='__main__':raise SystemExit(main())
