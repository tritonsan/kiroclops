"""Read compact progress from the three registered local Sol sessions."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    print(datetime.now(timezone.utc).isoformat())
    for number in (1, 2, 3):
        directory = ROOT / 'sessions' / f'cli-sol-ground-{number:02}'
        try:
            state = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
            record = {'session': directory.name, 'status': state['status'],
                      'selected': state.get('selected_candidate_id')}
            if state.get('deadline_at'):
                record['seconds_remaining'] = max(0, round(state['deadline_at'] - time.time()))
            record['candidates'] = [
                {'id': c['candidate_id'], 'status': c['status'],
                 'calibration_rmse': (c.get('feedback') or {}).get('mean_rmse_body'),
                 'calibration_numerical': (c.get('feedback') or {}).get('numerical_status')}
                for c in state['candidates']]
            if state.get('active_job_id'):
                job = json.loads((directory / 'jobs' / (state['active_job_id'] + '.json')).read_text(encoding='utf-8'))
                record['job'] = {'id': job['job_id'], 'status': job['status'], 'progress': job.get('progress')}
            print(json.dumps(record, ensure_ascii=False))
        except (OSError, ValueError, KeyError) as error:
            print(json.dumps({'session': directory.name, 'status': 'unavailable',
                              'error': f'{type(error).__name__}: {error}'}))


if __name__ == '__main__':
    main()
