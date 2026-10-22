"""Read saved trial state only; never reconnect or restart an experiment."""
from collections import Counter
from pathlib import Path
import json
import time

ROOT = Path(__file__).resolve().parents[1] / 'artifacts/phase3a/completion-paired-cohort-01'


def read(path):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return {}


def latest_tool(path):
    """Read only the tail and expose tool metadata, never model reasoning text."""
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - 262144)
            stream.seek(start)
            lines = stream.read().splitlines()
        if start:
            lines = lines[1:]
        latest = None
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            update = event.get('params', {}).get('update', {})
            if update.get('sessionUpdate') not in ('tool_call', 'tool_call_update'):
                continue
            identity = update.get('toolCallId')
            if not isinstance(identity, str) or not identity:
                continue
            if latest is None:
                latest = {key: update.get(key) for key in ('toolCallId', 'title', 'kind', 'status')}
            if identity == latest['toolCallId']:
                for key in ('title', 'kind'):
                    if latest[key] is None and update.get(key) is not None:
                        latest[key] = update[key]
                if update.get('sessionUpdate') == 'tool_call':
                    return latest
        return latest
    except OSError:
        pass
    return None


progress = read(ROOT / 'progress.json')
rows = progress.get('rows', [])
result = {'counts': dict(Counter(row['status'] for row in rows)),
          'blocked': progress.get('continuation_blocked'), 'active': [], 'terminal': []}
for row in rows:
    if row['status'] in ('completed', 'failed', 'interrupted'):
        result['terminal'].append({'trial': row['id'], 'status': row['status'],
            'stages': [{'stage': s['stage'], 'status': s['status'], 'error': s.get('error'),
                        'final_job_error': (s.get('final') or {}).get('error'),
                        'numerical_pass': s.get('captured_numerical_pass')} for s in row['stages']]})
    if row['status'] != 'running':
        continue
    for stage in row['stages']:
        if stage['status'] != 'running':
            continue
        directory = Path(stage['session'])
        state = read(directory / 'session.json')
        launch = read(directory / 'kiro/launch.json')
        job_id = state.get('active_job_id')
        job = read(directory / 'jobs' / job_id / 'job.json') if job_id else {}
        start = launch.get('prompt_started_at')
        native_end = launch.get('response_received_at')
        stage_start = stage.get('started_at')
        before_prompt_end = start or launch.get('finished_at') or time.time()
        pre_prompt_s = (round(before_prompt_end - stage_start, 1)
                        if stage_start is not None else None)
        authoring_s = round((native_end or time.time()) - start, 1) if start else None
        validation_s = (round(time.time() - job['created_at'], 1)
                        if job.get('operation') == 'finalize' and job.get('created_at') else None)
        phase = ('local_validation' if job.get('operation') == 'finalize' else
                 'postturn_evidence_processing' if launch.get('finished_at') and not job_id else
                 'native_authoring_or_tools' if start and native_end is None else
                 'local_tool_job' if job_id else 'prelaunch_or_setup')
        packages = None
        if job_id and state.get('domain') in ('web_panel', 'svg'):
            measurements=directory/'measurements'/job_id
            packages={role:len(list((measurements/role).glob('*/package/manifest.json')))
                      for role in ('reference','before','candidate')}
        result['active'].append({'trial': row['id'], 'stage': stage['stage'],
            'completion_phase': phase,
            'postturn_report_status': (launch.get('postturn_report') or {}).get('status'),
            'launch': launch.get('status'), 'model': launch.get('model'), 'effort': launch.get('effort'),
            'native_response_received': native_end is not None,
            'terminal_launch_receipt_available': launch.get('finished_at') is not None and launch.get('prompt_sent') is True,
            'elapsed_s': round(time.time() - start, 1) if start else None,
            'pre_prompt_wall_s': pre_prompt_s,
            'stage_wall_s': round(time.time() - stage_start, 1) if stage_start is not None else None,
            'authoring_and_tools_s': authoring_s, 'validation_job_elapsed_s': validation_s,
            'session': state.get('status'), 'candidates': len(state.get('candidates', [])),
            'selected': state.get('selected_candidate_id'),
            'latest_tool_in_tail': latest_tool(directory/'kiro/run/stdout.jsonl'),
            'native_log_age_s': round(time.time() - (directory/'kiro/run/stdout.jsonl').stat().st_mtime, 1)
                if (directory/'kiro/run/stdout.jsonl').is_file() else None,
            'packages_written_not_acceptance': packages,
            'job': {'operation': job.get('operation'), 'status': job.get('status'),
                    'progress': job.get('progress'), 'error': job.get('error')} if job_id else None})
print(json.dumps(result, ensure_ascii=False))
