"""Attribute a terminal capture failure from raw native and timing receipts."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, digest, file_sha256
from kiroclops.local_engine import review


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', type=Path, required=True)
    p.add_argument('--capture', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    s = args.session.resolve()
    assert not args.output.exists()
    names = ['session.json', 'kiro/launch.json', 'kiro/config.json',
             'kiro/run/stdout.jsonl', 'kiro/run/outbound.jsonl', 'kiro/tool-audit.jsonl']
    before = {n: file_sha256(s / n) for n in names}
    state = read_json(s / 'session.json')
    launch = read_json(s / 'kiro/launch.json')
    config = read_json(s / 'kiro/config.json')
    job_path = s / 'jobs' / state['final_job_id'] / 'job.json'
    job = read_json(job_path)
    assert not state.get('active_job_id') and job['status'] == 'failed'
    assert job['error']['code'] == 'capture_timing_failure'
    assert not job_path.with_name('result.json').exists()
    assert (launch['model'], launch['effort']) == ('claude-opus-5.5', 'max')
    assert launch['native_turn_status'] == 'completed' and launch['terminal_cleanup']['status'] == 'pass'
    inbound = [json.loads(l) for l in (s / 'kiro/run/stdout.jsonl').read_text('utf-8').splitlines()]
    outbound = [json.loads(l) for l in (s / 'kiro/run/outbound.jsonl').read_text('utf-8').splitlines()]
    prompts = [r['message'] for r in outbound if r.get('message', {}).get('method') == 'session/prompt']
    assert len(prompts) == 1
    responses = [r for r in inbound if 'method' not in r and r.get('id') == prompts[0]['id']]
    assert len(responses) == 1 and responses[0].get('result', {}).get('stopReason') == 'end_turn'
    stream = review._replay_consumed_native_stream(inbound, launch, config, responses[0])
    decisions = [json.loads(l) for l in (s / 'kiro/tool-audit.jsonl').read_text('utf-8').splitlines()]
    audit = review.audit_native_tool_outcomes(inbound, decisions,
        session_id=launch['preflight']['session_id'], started_at=launch['started_at'],
        finished_at=launch['finished_at'], scope_sha256=digest(config['generated_hashes']),
        runtime_context={'cli_version': launch['cli_version'], 'kas_version': launch['kas_version'],
            'launcher_sha256': config['launcher_sha256'], 'target_dir': config['target_dir'],
            'hook_id': str(Path(config['target_dir']) / '.kiro/hooks/kiroclops-guard.json') + '#hook-0'})
    capture = args.capture.resolve()
    assert capture.is_relative_to(s / 'measurements' / state['final_job_id'])
    raw = read_json(capture)
    assert not raw['recording_complete'] and not raw['valid_analysis_package']
    assert raw['error']['code'] == job['error']['code']
    result = {'kind': 'ReadOnlyFailureAttribution', 'classification': 'capture_timing_failure',
        'original_registered_failure_unchanged': True, 'acceptance_override': False,
        'model_fallback_observed': False, 'complete_motion_result_available': False,
        'stream_replay': stream, 'tool_outcomes': audit, 'error': job['error'],
        'last_capture_progress': job['progress'], 'completed_capture_inputs': len(raw['commands']),
        'received_frame_count': len(raw['frames']), 'timed_operations': raw['command_timings'],
        'delivery_backpressure': raw['delivery_backpressure'],
        'cleanup': 'pass', 'evidence_hashes': before, 'job_sha256': file_sha256(job_path),
        'raw_capture_sha256': file_sha256(capture), 'diagnostic_source_sha256': file_sha256(__file__),
        'limitation': 'Transport/generation replay is not full native/source/visual acceptance. Timing receipts do not establish the underlying scheduler/filesystem/browser cause. Private oracle coordinates excluded.'}
    assert before == {n: file_sha256(s / n) for n in names}
    write_json(args.output, result)
    print({'classification': result['classification'], 'tools': audit['status'],
           'tool_calls': audit['application_call_count'], 'model_fallback_observed': False})


if __name__ == '__main__':
    main()
