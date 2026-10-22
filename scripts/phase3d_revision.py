"""Remeasure a pre-model suite's saved pixels and reset raw review to pending."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from kiroclops.local_engine.backend import _lock
from kiroclops.local_engine.media_suite import create_suite_session, load_suite
from kiroclops.local_engine.reliability import prepare_runtime
from kiroclops.local_engine.session import Session, _ordinary, source_hashes
from kiroclops.local_engine.suite_revision import revise_suite
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


def run(previous, identifier, root=Path('C:/kc3d')):
    if not re.fullmatch(r'[a-z0-9-]{1,40}', identifier):
        raise VisualError('unsafe_path', 'Use a short lowercase revision identifier.')
    previous, root = _ordinary(previous), _ordinary(root)
    output, session = root / 'p' / identifier, root / 's' / identifier
    if output.exists() or session.exists():
        raise VisualError('output_exists', 'Keep prior preparation and sessions unchanged.')
    saved = Session(previous)
    state = saved._load()
    if state.get('experiment', {}).get('reference_mode') != 'media_suite':
        raise VisualError('invalid_suite', 'Only a reference media suite can be revised.')
    if state.get('started_at') is not None or state.get('candidates'):
        raise VisualError('session_started', 'Measurement revisions must precede model work.')
    browser = state['experiment'].get('runtime_environment', {}).get('PLAYWRIGHT_BROWSERS_PATH')
    runtime = prepare_runtime(root, browser_path=browser)
    old_environment = {key: os.environ.get(key) for key in (*runtime['environment'], 'KIROCLOPS_ANALYSIS_BACKEND')}
    os.environ.update(runtime['environment'])
    os.environ['KIROCLOPS_ANALYSIS_BACKEND'] = 'local'

    def progress(phase, completed=0, total=1):
        print(phase, flush=True)
        write_json(output / 'progress.json', {'phase': phase, 'completed': completed,
            'total': total, 'model_started': False, 'native_capture_started': False})

    try:
        with _lock(Path(runtime['capture_lock']), timeout=60), _lock(previous / 'session.lock', timeout=60):
            state = saved._load()
            if state.get('started_at') is not None or state.get('candidates'):
                raise VisualError('session_started', 'Measurement revisions must precede model work.')
            if (source_hashes(state['reference']) != state['reference_hashes']
                    or source_hashes(state['baseline']) != state['baseline_hashes']
                    or digest(load_suite(state['reference'], verify_analysis_code=False)) != state['experiment']['suite_sha256']):
                raise VisualError('source_changed', 'Session reference or authoring parent changed.')
            for job in (previous / 'jobs').glob('*/job.json'):
                if read_json(job).get('status') in ('queued', 'running'):
                    raise VisualError('job_running', 'Wait for existing session work before revising evidence.')
            old_ready = read_json(previous / 'environment-readiness.json')
            result = revise_suite(state['reference'], output, state['baseline'], progress=progress)
            create_suite_session(session, result['suite'], state['baseline'], runtime=runtime)
            write_json(session / 'environment-readiness.json', {
                **old_ready, 'runtime': runtime, 'suite_sha256': result['suite_sha256'],
                'initial_record_count': result['initial_cases'], 'reference_revision': str(previous),
                'measurement_revision': str(output / 'revision.json'), 'status': 'pending_raw_review',
                'measurement_revision_required': False, 'private_baseline_records': result['private_baseline_records'],
                'reference_raw_review_status': 'pending', 'initial_raw_review_status': 'pending',
                'model_started': False})
            result = {**result, 'session': str(session)}
            write_json(output / 'result.json', result)
            return result
    finally:
        for key, value in old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    args = parser.parse_args()
    try:
        result = run(args.previous, args.id, args.root)
    except (VisualError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', 'revision_failed'), 'message': str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
