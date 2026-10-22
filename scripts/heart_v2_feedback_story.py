"""Read-only, post-run evidence story for the six registered Heart v2 stages.

This is an engineering summary, not an acceptance or authorship verifier. It
refuses a running cohort before opening any candidate or native journal. Exact
native feedback, subsequent completed edits and the next evaluated source are
bound where observable. Their ordering does not prove a model's internal reason
for an edit; the independent native audit remains responsible for authorship.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.integration.storage import project_hashes, read_json, sha256, utc_now

VERSION = 'heart-v2-feedback-story-v1'
TERMINAL_JOBS = {'succeeded', 'failed', 'cancelled', 'interrupted'}
RUNNING_LAUNCHES = {'starting', 'preflighting', 'running'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def seconds(start, end):
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end.replace('Z', '+00:00'))
                      - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


class Evidence:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.files = {}

    def bind(self, path):
        path = Path(path).absolute()
        for component in (path, *path.parents):
            if component.exists():
                info = component.stat()
                require(not component.is_symlink() and not getattr(info, 'st_file_attributes', 0) & 0x400,
                        'Linked evidence is not supported')
        path = path.resolve()
        require(path.is_relative_to(self.root) and path.is_file(), 'Evidence must be an existing workspace file')
        require(path.stat().st_nlink == 1, 'Hard-linked evidence is not supported')
        value = sha256(path)
        require(str(path) not in self.files or self.files[str(path)] == value, 'Evidence changed during summary')
        self.files[str(path)] = value
        return path

    def data(self, path):
        return read_json(self.bind(path))

    def verify(self):
        require(all(sha256(path) == value for path, value in self.files.items()),
                'Evidence changed while the story was generated')


def objects(value, depth=0):
    """Decode the observed MCP JSON wrappers without searching arbitrary strings."""
    if depth > 8:
        return
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return
    if isinstance(value, dict):
        yield value
        for key in ('response', 'message', 'result', 'structuredContent', 'structured_content', 'content', 'text'):
            if key in value:
                yield from objects(value[key], depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item, depth + 1)


def native_calls(directory, launch, evidence):
    path = evidence.bind(directory / 'kiro/stdout.jsonl')
    require(path.stat().st_size <= 32 * 1024 * 1024, 'Native journal exceeds the audit size limit')
    sid = launch.get('acp_preflight', {}).get('session_id')
    calls, warnings = {}, []
    for line, raw in enumerate(path.read_bytes().splitlines(), 1):
        if not raw.strip():
            continue
        row = json.loads(raw)
        if row.get('method') != 'session/update':
            continue
        params, update = row.get('params', {}), row.get('params', {}).get('update', {})
        kind, tid = update.get('sessionUpdate'), update.get('toolCallId')
        if kind not in {'tool_call', 'tool_call_update'}:
            continue
        if params.get('sessionId') != sid or not isinstance(tid, str):
            warnings.append({'line': line, 'reason': 'Unbound native session/tool identity'})
            continue
        binding = {'line': line, 'line_sha256': hashlib.sha256(raw).hexdigest()}
        if kind == 'tool_call':
            if tid in calls:
                calls[tid]['ambiguous'] = True
                warnings.append({'line': line, 'reason': 'Repeated native tool start', 'tool_call_id': tid})
                continue
            meta = update.get('_meta', {}).get('kiro', {})
            calls[tid] = {'tool_call_id': tid, 'start': binding, 'title': update.get('title'),
                          'kind': update.get('kind'), 'meta': meta,
                          'args': {k: v for k, v in update.get('rawInput', {}).items() if k != '_meta'},
                          'completions': [], 'ambiguous': False}
        elif update.get('status') in {'completed', 'failed', 'cancelled'}:
            if tid not in calls:
                # The independent audit's strict denied-read codec handles these.
                # This helper neither accepts nor repairs an orphan lifecycle.
                warnings.append({'line': line, 'reason': 'Orphan terminal event omitted from causal claims',
                                 'tool_call_id': tid, 'status': update.get('status')})
                continue
            calls[tid]['completions'].append({'binding': binding, 'status': update['status'],
                                            'output': update.get('rawOutput')})
    return list(calls.values()), warnings


def completion(call):
    if call['ambiguous'] or len(call['completions']) != 1:
        return None
    final = call['completions'][0]
    return final if final['status'] == 'completed' else None


def is_mcp(call, name):
    return call['meta'].get('serverName') == 'kiroclops' and call['title'] == '@kiroclops/' + name


def call_receipt(call):
    final = completion(call)
    return {'tool_call_id': call['tool_call_id'], 'tool': call['title'], 'start': call['start'],
            'completed': final['binding'] if final else None}


def evaluation_call(calls, job):
    matches = []
    for call in calls:
        final = completion(call)
        if not final or not is_mcp(call, 'evaluate_candidate'):
            continue
        if any(obj.get('job_id') == job['job_id'] and obj.get('candidate_id') == job['candidate_id']
               for obj in objects(final['output'])):
            matches.append(call)
    return matches[0] if len(matches) == 1 else None


def feedback_calls(calls, job):
    matches = []
    field = 'result' if 'result' in job else 'error' if 'error' in job else None
    if field is None:
        return matches
    for call in calls:
        final = completion(call)
        if not final or not is_mcp(call, 'get_job') or call['args'].get('job_id') != job['job_id']:
            continue
        if any(obj.get('job_id') == job['job_id'] and obj.get('session_id') == job['session_id']
               and obj.get('candidate_id') == job['candidate_id'] and obj.get('status') == job['status']
               and obj.get('candidate_source_sha256') == job.get('candidate_source_sha256')
               and obj.get(field) == job[field] for obj in objects(final['output'])):
            matches.append(call)
    return sorted(matches, key=lambda call: completion(call)['binding']['line'])


def completed_edits(calls, target, after_line, before_line):
    result = []
    target = target.resolve()
    for call in calls:
        final = completion(call)
        if (not final or call['kind'] != 'edit' or call['meta'].get('serverName')
                or call['meta'].get('toolOrigin') not in {'default', 'acp'}
                or not after_line < call['start']['line'] < final['binding']['line'] < before_line):
            continue
        value = call['args'].get('path', call['args'].get('file'))
        if not isinstance(value, str) or any(symbol in value for symbol in ('$', '%', '://')):
            continue
        path = Path(value)
        path = (path if path.is_absolute() else target / path).resolve()
        if not path.is_relative_to(target):
            continue
        relative = path.relative_to(target)
        if relative.as_posix() != 'player.gd' and not (relative.parent.as_posix() == 'motion' and relative.suffix == '.gd'):
            continue
        result.append({**call_receipt(call), 'file': relative.as_posix(),
                       'input_sha256': digest(call['args'])})
    return result


def candidate_story(directory, state, candidate, calls, evidence):
    cid = candidate['candidate_id']
    folder = directory / 'candidates' / cid
    record_path = folder / 'candidate.json'
    record = evidence.data(record_path) if record_path.is_file() else candidate
    require(record.get('candidate_id') == cid, 'Candidate identity differs')
    source_map = record.get('source_hashes')
    source_verified = False
    if source_map is not None:
        require(record.get('source_sha256') == digest(source_map)
                and candidate.get('source_sha256') == record['source_sha256']
                and project_hashes(folder / 'project') == source_map, 'Candidate source binding differs')
        for relative, expected in source_map.items():
            path = evidence.bind(folder / 'project' / relative)
            require(evidence.files[str(path)] == expected, 'Candidate source bytes changed')
        source_verified = True
    jobs = [evidence.data(p) for p in sorted((directory / 'jobs').glob('job-*.json'))]
    jobs = [job for job in jobs if job.get('action') == 'evaluate' and job.get('candidate_id') == cid]
    require(len(jobs) == 1, 'Candidate must have one evaluation job')
    job = jobs[0]
    require(job.get('session_id') == state['session_id'] and job.get('status') in TERMINAL_JOBS,
            'Candidate job is not a terminal job from this stage')
    if source_verified:
        require(job.get('candidate_source_sha256') == record['source_sha256'], 'Job belongs to another source')
    if job.get('status') == 'succeeded':
        require(candidate.get('feedback') == job.get('result'), 'Candidate feedback differs from completed job')
    feedback = job.get('result', {})
    failures = sorted({row.get('case_id') for row in feedback.get('integrity', {}).get('mismatches', [])
                       if isinstance(row.get('case_id'), str)})
    evaluation = evaluation_call(calls, job)
    observed = feedback_calls(calls, job)
    patch_path = folder / 'patch.diff'
    patch_receipt = None
    if patch_path.is_file():
        path = evidence.bind(patch_path)
        patch_receipt = {'path': str(path), 'sha256': evidence.files[str(path)]}
    result = {'candidate_id': cid, 'status': candidate.get('status'), 'job_status': job['status'],
              'job_id': job['job_id'], 'source_sha256': record.get('source_sha256'),
              'source_binding_verified': source_verified, 'code_diff': patch_receipt,
              'changed_source_files': sorted(name for name, value in (source_map or {}).items()
                                             if state['baseline_hashes'].get(name) != value),
              'eligible': feedback.get('eligible') is True,
              'automatic_integrity_status': feedback.get('automatic_integrity_status'),
              'mean_rmse_body': feedback.get('mean_rmse_body'),
              'failure_class': feedback.get('failure_class'), 'error': job.get('error'),
              'failure_case_ids': failures,
              'wall_coyote_failure_case_ids': [name for name in failures if name.startswith('wall-coyote-')],
              'selected': cid == state.get('selected_candidate_id'),
              'created_at': job.get('created_at'), 'started_at': job.get('started_at'),
              'finished_at': job.get('finished_at'),
              'evaluation_seconds': seconds(job.get('started_at'), job.get('finished_at')),
              'feedback_value_sha256': digest(job.get('result', job.get('error'))),
              'native_evaluate': call_receipt(evaluation) if evaluation else None,
              'native_feedback_observations': [call_receipt(call) for call in observed]}
    return result, source_map


def stage_story(directory, state, launch, evidence):
    calls, warnings = native_calls(directory, launch, evidence) if launch and (directory / 'kiro/stdout.jsonl').is_file() else ([], [])
    candidates, maps = [], {}
    for candidate in state.get('candidates', []):
        row, source_map = candidate_story(directory, state, candidate, calls, evidence)
        candidates.append(row)
        maps[row['candidate_id']] = source_map
    sequences = []
    for old, new in zip(candidates, candidates[1:]):
        if old['eligible']:
            continue
        feedback = old['native_feedback_observations']
        evaluation = new['native_evaluate']
        edits = []
        if feedback and evaluation:
            edits = completed_edits(calls, directory / 'target', feedback[0]['completed']['line'], evaluation['start']['line'])
        changed = (old['source_binding_verified'] and new['source_binding_verified']
                   and old['source_sha256'] != new['source_sha256'])
        observed = bool(feedback and evaluation and edits and changed)
        wall = bool(old['wall_coyote_failure_case_ids'])
        if observed and new['eligible']:
            status = 'wall_failure_then_eligible_after_observed_feedback' if wall else 'failure_then_eligible_after_observed_feedback'
        elif observed and wall and new['automatic_integrity_status'] == 'pass':
            status = 'wall_failure_resolved_but_candidate_ineligible'
        elif observed:
            status = 'feedback_edit_evaluate_sequence_observed_still_ineligible'
        else:
            status = 'repair_sequence_not_fully_bound'
        sequences.append({'from_candidate': old['candidate_id'], 'to_candidate': new['candidate_id'],
                          'status': status, 'failed_case_ids': old['failure_case_ids'],
                          'feedback_observed': feedback[0] if feedback else None,
                          'completed_native_edits': edits, 'next_evaluate': evaluation,
                          'source_changed': bool(changed),
                          'before_source_sha256': old['source_sha256'], 'after_source_sha256': new['source_sha256'],
                          'next_eligible': new['eligible'], 'next_selected': new['selected']})
    selected = next((candidate for candidate in candidates if candidate['selected']), None)
    first_pass = bool(candidates and candidates[0]['eligible'])
    repaired = [row for row in sequences if row['status'] == 'wall_failure_then_eligible_after_observed_feedback']
    story = ('wall_feedback_edit_success_sequence_observed' if repaired else
             'first_candidate_eligible_no_feedback_repair_needed' if first_pass else
             'no_bound_wall_feedback_repair_success')
    return {'session_id': state['session_id'], 'experiment_id': state['experiment_id'],
            'stage': state['experiment_spec']['stage'], 'session_status': state['status'],
            'native_launch_status': launch.get('status') if launch else 'not_launched',
            'model_requested': launch.get('model_requested') if launch else None,
            'effort_requested': launch.get('effort_requested') if launch else None,
            'native_seconds': launch.get('elapsed_seconds') if launch else None,
            'story': story, 'first_candidate_eligible': first_pass,
            'candidate_count': len(candidates), 'selected_candidate_id': state.get('selected_candidate_id'),
            'selected_source_sha256': selected['source_sha256'] if selected else None,
            'final': {key: (state.get('final') or {}).get(key) for key in
                      ('numerical_status', 'automatic_integrity_status', 'measurement_status', 'normal_gameplay_status')},
            'candidates': candidates, 'repair_sequences': sequences, 'journal_warnings': warnings,
            'authorship_and_autonomy': 'Not evaluated by this summary. Require the independent native audit.'}


def terminal_metadata(registry, run, evidence):
    """Check the whole cohort before reading any candidate or native trace."""
    require(run.get('status') in {'pending_independent_review', 'fail'} and run.get('finished_at'),
            'Cohort is still active or lacks its terminal receipt; do not inspect candidate solutions')
    chains = registry.get('chains', [])
    require(len(chains) == 3 and len(run.get('chains', [])) == 3, 'Expected all three registered chains')
    require([row['chain_id'] for row in run['chains']] == [row['chain_id'] for row in chains],
            'Run does not cover the exact registered chains')
    prepared = []
    for chain, result in zip(chains, run['chains']):
        for stage in ('jump', 'ground'):
            item = result.get('stages', {}).get(stage)
            require(isinstance(item, dict) and item.get('status') in {'numerical_pass_review_pending', 'fail', 'not_run'},
                    'Every registered stage needs a terminal outcome')
            directory = evidence.root / 'sessions' / chain[stage + '_session']
            state_path = directory / 'session.json'
            if not state_path.is_file():
                require(item['status'] in {'fail', 'not_run'}, 'Successful stage is missing its session')
                prepared.append((chain['chain_id'], stage, item, directory, None, None))
                continue
            state = evidence.data(state_path)
            require(state.get('session_id') == directory.name, 'Wrong stage session identity')
            for path in sorted((directory / 'jobs').glob('job-*.json')):
                require(evidence.data(path).get('status') in TERMINAL_JOBS,
                        'A stage still has a live/unfinished job; no candidate reading allowed')
            require(not state.get('active_job_id'), 'Stage active job reference has not cleared')
            launch_path = directory / 'kiro/launch.json'
            launch = evidence.data(launch_path) if launch_path.is_file() else None
            if launch:
                require(launch.get('status') not in RUNNING_LAUNCHES and launch.get('finished_at'),
                        'Native launch has not terminated')
                if launch.get('pid'):
                    require(launch.get('process_returncode_after_cleanup') is not None,
                            'Native process cleanup is unconfirmed')
            prepared.append((chain['chain_id'], stage, item, directory, state, launch))
    return prepared


def build(registry_path):
    from kiroclops.portable_v2.cohort import verify_registry
    registry_path = Path(registry_path).resolve()
    # Read only the registry/cohort receipt first. Full verification includes
    # frozen qualification evidence but never reads the running candidates.
    initial = read_json(registry_path)
    require(initial.get('protocol_id') == 'heart-transfer-chain-v2', 'This helper only accepts the registered v2 protocol')
    evidence = Evidence(initial['root'])
    evidence.bind(Path(__file__))
    registry = evidence.data(registry_path)
    run = evidence.data(evidence.root / 'artifacts/phase2c-v2/cohorts' / registry['cohort_id'] / 'run.json')
    require(run.get('registry_sha256') == evidence.files[str(registry_path)], 'Run belongs to another registered cohort')
    prepared = terminal_metadata(registry, run, evidence)
    require(verify_registry(registry_path) == registry, 'Registry changed during verification')
    stories = []
    for chain_id, stage, item, directory, state, launch in prepared:
        value = stage_story(directory, state, launch, evidence) if state else {
            'session_id': directory.name, 'stage': stage, 'story': 'not_run', 'reason': item.get('reason', item.get('error'))}
        stories.append({'chain_id': chain_id, 'registered_outcome': item['status'], **value})
    evidence.verify()
    return {'schema_version': 1, 'version': VERSION, 'generated_at': utc_now(),
            'cohort_id': registry['cohort_id'], 'registry_sha256': sha256(registry_path),
            'scope': 'Post-run ordering of source-bound feedback, completed native edits and evaluations. No operator or model invocation.',
            'causality_limit': 'An observed sequence does not prove internal reasoning. First-attempt success is not feedback-driven repair. Independent native audit determines authorship and autonomy.',
            'stages': stories,
            'summary': {'total_registered_stages': 6,
                        'first_candidate_eligible_stages': sum(bool(row.get('first_candidate_eligible')) for row in stories),
                        'wall_feedback_edit_success_stages': sum(row.get('story') == 'wall_feedback_edit_success_sequence_observed' for row in stories),
                        'fully_bound_wall_repair_sequences': sum(sequence['status'] == 'wall_failure_then_eligible_after_observed_feedback'
                            for row in stories for sequence in row.get('repair_sequences', []))},
            'evidence_files': evidence.files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        output = args.output.resolve()
        require(output.is_relative_to(ROOT / 'artifacts/phase2c-v2'), 'Write engineering output only under artifacts/phase2c-v2')
        require(not output.exists(), 'Story output is immutable; do not replace a prior summary')
        result = build(args.registry)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        print(json.dumps({'status': 'complete', 'output': str(output), **result['summary']}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({'status': 'unavailable', 'reason': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
