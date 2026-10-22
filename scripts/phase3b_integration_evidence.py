"""Summarize the three registered runs without changing their evidence."""
from pathlib import Path
import argparse
import json

from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.review import audit_native_run
from scripts.phase3b_aws_probe import aws


def summarize(root, deployment, output):
    root, deployment, output = Path(root), Path(deployment), Path(output)
    if output.exists():
        raise ValueError('Preserve earlier summaries; choose a fresh output')
    registered = read_json(root / 'registration.json')
    if read_json(root / 'run-result.json')['status'] != 'completed':
        raise ValueError('All three registered integrations must be terminal')
    unchanged = all(file_sha256(p) == h for p, h in registered['sources'].items())
    state = read_json(deployment / 'state.json')
    tenant = read_json(deployment / 'account-owner.json')['tenant']
    output.mkdir(parents=True)
    rows = []
    for domain in registered['domains']:
        session = root / domain / 'session'
        local = read_json(session / 'session.json')
        launch = read_json(session / 'kiro/launch.json')
        try:
            audit = audit_native_run(session)
        except Exception as exc:
            audit = {'status': 'fail', 'code': getattr(exc, 'code', type(exc).__name__),
                     'message': str(exc)}
        write_json(output / (domain + '-native-audit.json'), audit)
        job_rows = []
        for path in sorted((session / 'jobs').glob('*/job.json')):
            job = read_json(path)
            result_path = path.with_name('result.json')
            result = read_json(result_path) if result_path.exists() else None
            job_rows.append({'job_id': path.parent.name,
                             **{k: job.get(k) for k in ('operation', 'status', 'created_at', 'started_at',
                                                       'finished_at', 'candidate_id', 'error')},
                             'result': result, 'job_sha256': file_sha256(path)})
        cloud_rows, metrics = [], []
        for path in sorted((session / 'measurements').glob('**/delivery-v2.json')):
            delivery = read_json(path)
            identity = path.parent.name
            raw = aws('dynamodb', 'get-item', table_name=state['Table'], consistent_read=True,
                      key={'pk': {'S': 'user#' + tenant},
                           'sk': {'S': 'user#' + tenant + '#job#' + identity}}).get('Item')
            cloud = json.loads(raw['value']['S']) if raw else {}
            created, started, finished = (cloud.get(k) for k in ('created_at', 'started_at', 'finished_at'))
            cloud_rows.append({'job_id': identity, 'status': cloud.get('status'),
                               'created_at': created, 'started_at': started, 'finished_at': finished,
                               'queue_seconds': started-created if started and created else None,
                               'worker_seconds': finished-started if finished and started else None,
                               'measurement_sha256': delivery['measurement_sha256'],
                               'receipt': delivery['receipt'], 'receipt_sha256': delivery['receipt_sha256'],
                               'delivery_file_sha256': file_sha256(path),
                               'service_job_document_sha256': digest(cloud) if cloud else None})
        for path in sorted((session / 'measurements').glob('**/transport-metrics.jsonl')):
            metrics += [json.loads(line) for line in path.read_text('utf8').splitlines() if line.strip()]
        totals = {}
        for phase in sorted({m['phase'] for m in metrics}):
            selected = [m for m in metrics if m['phase'] == phase]
            totals[phase] = {'calls': len(selected),
                             'wall_seconds': sum(m['wall_seconds'] for m in selected),
                             'process_cpu_seconds': sum(m['process_cpu_seconds'] for m in selected),
                             'incomplete_calls': sum(not m['completed'] for m in selected)}
        analysis_wait = sum(totals.get(k, {}).get('wall_seconds', 0)
                            for k in ('submit_total', 'poll_and_deliver_total')) if metrics else None
        end_times = [j['finished_at'] for j in job_rows if j['finished_at'] is not None]
        # Include final validation, not only the model's authoring interval.
        finished = max([launch.get('response_received_at') or launch['finished_at']] + end_times)
        started = local.get('started_at')
        elapsed = finished-started if started else None
        report_path = session / 'report/report.json'
        report = read_json(report_path) if report_path.exists() else {}
        rows.append({'domain': domain, 'session_status': local['status'],
                     'launcher_status': launch['status'], 'native_audit_status': audit['status'],
                     'candidate_count': len(local['candidates']), 'candidates': local['candidates'],
                     'selected_candidate_id': local['selected_candidate_id'], 'final': local.get('final'),
                     'jobs': job_rows, 'cloud_jobs': cloud_rows, 'client_phases': totals,
                     'analysis_client_wall_seconds': analysis_wait,
                     'start_to_validation_seconds': elapsed,
                     'strict_1800_second_stage_pass': elapsed <= 1800 if elapsed is not None else False,
                     'analysis_fraction_of_stage': analysis_wait / elapsed if elapsed and analysis_wait is not None else None,
                     'overall_acceptance': report.get('overall_acceptance'),
                     'acceptance_summary': report.get('acceptance_summary'),
                     'evidence': {'session_sha256': file_sha256(session / 'session.json'),
                                  'launch_sha256': file_sha256(session / 'kiro/launch.json')}})
    result = {'kind': 'AwsKiroIntegrationEvidence', 'schema_version': 1,
              'sources_unchanged': unchanged, 'registration_sha256': file_sha256(root / 'registration.json'),
              'rows': rows, 'external_users': 0, 'comparative_claim': False,
              'scope': 'Three service integrations, not the 18-run comparison or held-out acceptance.',
              'timing_policy': 'Outer submit/poll phases include upload/API/queue/delivery; nested API/upload and server times are diagnostic subsets, never added twice. Local captures and preservation remain in the full stage duration.'}
    write_json(output / 'result.json', result)
    print(json.dumps({'domains': len(rows), 'sources_unchanged': unchanged,
                      'native_audits': [r['native_audit_status'] for r in rows]}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    summarize(args.root, args.deployment, args.output)
