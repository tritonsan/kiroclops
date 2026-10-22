"""Read-only attribution of the first panel failure; never an acceptance override."""
from pathlib import Path
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,digest,file_sha256
from kiroclops.local_engine import review


def main():
    base=ROOT/'artifacts/phase3a'
    session=base/'completion-paired-cohort-01/sessions/panel-tools-01/panel'
    dest=base/'completion-20260927/panel-tools-01-capture-diagnostic.json'
    assert not dest.exists()
    names=['kiro/launch.json','kiro/config.json','kiro/run/stdout.jsonl',
           'kiro/run/outbound.jsonl','kiro/tool-audit.jsonl','session.json']
    before={n:file_sha256(session/n) for n in names}
    launch=read_json(session/'kiro/launch.json');config=read_json(session/'kiro/config.json')
    state=read_json(session/'session.json')
    inbound=[json.loads(s) for s in (session/'kiro/run/stdout.jsonl').read_text('utf-8').splitlines()]
    outbound=[json.loads(s) for s in (session/'kiro/run/outbound.jsonl').read_text('utf-8').splitlines()]
    prompts=[r['message'] for r in outbound if r.get('message',{}).get('method')=='session/prompt']
    assert len(prompts)==1
    ends=[r for r in inbound if 'method' not in r and r.get('id')==prompts[0]['id']]
    assert len(ends)==1 and ends[0].get('result',{}).get('stopReason')=='end_turn'
    stream=review._replay_consumed_native_stream(inbound,launch,config,ends[0])
    decisions=[json.loads(s) for s in (session/'kiro/tool-audit.jsonl').read_text('utf-8').splitlines()]
    tool_audit=review.audit_native_tool_outcomes(inbound,decisions,
        session_id=launch['preflight']['session_id'],started_at=launch['started_at'],
        finished_at=launch['finished_at'],scope_sha256=digest(config['generated_hashes']),
        runtime_context={'cli_version':launch['cli_version'],'kas_version':launch['kas_version'],
            'launcher_sha256':config['launcher_sha256'],'target_dir':config['target_dir'],
            'hook_id':str(Path(config['target_dir'])/'.kiro/hooks/kiroclops-guard.json')+'#hook-0'})
    job_path=session/'jobs'/state['final_job_id']/'job.json';job=read_json(job_path)
    assert job['status']=='failed' and job['error']['code']=='capture_timing_failure'
    assert not job_path.with_name('result.json').exists()
    assert launch['native_turn_status']=='completed' and launch['terminal_cleanup']['status']=='pass'
    assert (launch['model'],launch['effort'])==('claude-opus-5.5','max')
    after={n:file_sha256(session/n) for n in names};assert before==after
    value={'kind':'ReadOnlyFailureAttribution','classification':'capture_timing_failure',
        'original_registered_failure_unchanged':True,'acceptance_override':False,
        'model_fallback_observed':False,'complete_motion_result_available':False,
        'native_turn_completed':True,'stream_replay':stream,'tool_outcomes':tool_audit,
        'error':job['error'],'last_capture_progress':job['progress'],
        'cleanup':'pass','evidence_hashes':before,'job_sha256':file_sha256(job_path),
        'diagnostic_source_sha256':file_sha256(__file__),
        'host_load_causality':'Not established. Concurrent archive verification is a possible contributor; no profiler trace proves the173ms cause.',
        'limitation':'This replays generation configuration and tool outcomes only. It is not full native/source/visual acceptance and cannot supply missing heldout measurements.'}
    write_json(dest,value)
    print({'classification':value['classification'],'tool_replay':tool_audit['status'],
           'tool_calls':tool_audit['application_call_count'],'model_fallback_observed':False},flush=True)


if __name__=='__main__':main()
