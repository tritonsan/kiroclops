"""Collect one terminal panel recovery without replacing the registered failure."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.review import audit_native_run
from scripts.phase3b_aws_probe import aws


def run(root, deployment, output):
    root, deployment, output = map(Path, (root, deployment, output))
    session=root/'session'; state=read_json(session/'session.json')
    launch=read_json(session/'kiro/launch.json')
    if state.get('active_job_id') or launch['status'] in ('starting','running'):
        raise ValueError('Wait for the actual run and its validation to finish')
    if output.exists():raise ValueError('Preserve earlier evidence')
    output.mkdir(parents=True)
    try:
        audit=audit_native_run(session)
    except Exception as exc:
        audit={'status':'fail','code':getattr(exc,'code',type(exc).__name__),'message':str(exc)}
    write_json(output/'native-audit.json',audit)
    service=read_json(deployment/'state.json')
    tenant=read_json(deployment/'account-owner.json')['tenant']
    jobs=[]
    for path in sorted((session/'jobs').glob('*/job.json')):
        value=read_json(path)
        jobs.append({k:value.get(k) for k in ('job_id','operation','candidate_id','status','created_at','started_at','finished_at','error')})
    cloud=[]; metrics=[]
    for path in sorted((session/'measurements').glob('**/delivery-v2.json')):
        delivery=read_json(path); identity=path.parent.name
        raw=aws('dynamodb','get-item',table_name=service['Table'],consistent_read=True,
            key={'pk':{'S':'user#'+tenant},'sk':{'S':'user#'+tenant+'#job#'+identity}}).get('Item')
        value=json.loads(raw['value']['S']) if raw else {}
        cloud.append({'job_id':identity,'status':value.get('status'),
            'created_at':value.get('created_at'),'started_at':value.get('started_at'),'finished_at':value.get('finished_at'),
            'measurement_sha256':delivery['measurement_sha256'],'receipt':delivery['receipt'],
            'receipt_sha256':delivery['receipt_sha256'],'delivery_sha256':file_sha256(path),
            'service_document_sha256':digest(value) if value else None})
    for path in sorted((session/'measurements').glob('**/transport-metrics.jsonl')):
        metrics.extend(json.loads(line) for line in path.read_text('utf8').splitlines() if line.strip())
    totals={}
    for phase in sorted({m['phase'] for m in metrics}):
        selected=[m for m in metrics if m['phase']==phase]
        totals[phase]={'calls':len(selected),'wall_seconds':sum(m['wall_seconds'] for m in selected),
            'process_cpu_seconds':sum(m['process_cpu_seconds'] for m in selected),
            'incomplete_calls':sum(not m['completed'] for m in selected)}
    analysis=sum(totals.get(k,{}).get('wall_seconds',0) for k in ('submit_total','poll_and_deliver_total'))
    ends=[j['finished_at'] for j in jobs if j.get('finished_at')]
    end=max([launch.get('response_received_at') or launch['finished_at']]+ends)
    elapsed=end-state['started_at'] if state.get('started_at') else None
    final_id=state.get('final_job_id'); comparison=[]
    if final_id and (session/'measurements'/final_id/'comparison.json').exists():
        comparison=read_json(session/'measurements'/final_id/'comparison.json')
    report=read_json(session/'report/report.json') if (session/'report/report.json').exists() else {}
    value={'kind':'PanelRecoveryServiceEvidence','schema_version':1,
        'created_at':datetime.now(timezone.utc).isoformat(),'launcher_status':launch['status'],
        'session_status':state['status'],'model':'claude-opus-5.5','effort':'max',
        'native_audit_status':audit['status'],'candidate_count':len(state['candidates']),
        'candidates':state['candidates'],'selected_candidate_id':state['selected_candidate_id'],
        'final':state.get('final'),'comparison':comparison,'jobs':jobs,'cloud_jobs':cloud,
        'client_phases':totals,'analysis_client_wall_seconds':analysis,
        'start_to_validation_seconds':elapsed,'strict_1800_second_stage_pass':elapsed<=1800 if elapsed else False,
        'analysis_fraction_of_stage':analysis/elapsed if elapsed else None,
        'overall_acceptance':report.get('overall_acceptance'),
        'acceptance_summary':report.get('acceptance_summary'),'startup_attempts':launch.get('startup_attempts'),
        'stdio_preflight':launch.get('stdio_preflight'),
        'operator_followup_or_solution_patch':False,'external_users':0,'heldout_status':'not_provided',
        'previous_failure_preserved':True,'replaces_registered_run':False,
        'evidence':{'launch_sha256':file_sha256(session/'kiro/launch.json'),
            'session_sha256':file_sha256(session/'session.json'),
            'registration_sha256':file_sha256(root/'run-registration.json')},
        'timing_scope':'Includes all preparation retries and final validation. Outer submit/poll phases are added; nested API/upload/server durations are diagnostic subsets, never added twice.'}
    write_json(output/'result.json',value)
    print({k:value[k] for k in ('launcher_status','session_status','native_audit_status','candidate_count','strict_1800_second_stage_pass')})
    return value


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True);parser.add_argument('--deployment',required=True)
    parser.add_argument('--output',required=True);args=parser.parse_args()
    run(args.root,args.deployment,args.output)
