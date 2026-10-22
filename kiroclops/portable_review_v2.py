"""Explicit post-hoc review revision for hook-denied native read notifications.

The registered portable implementation and its journals are not changed. Only
failure-only read notifications with an exact unused denial are removed from an
in-memory authorship view. Every original acceptance check still runs.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import types

from .portable import audit as original

VERSION = 'portable-posthoc-denied-read-review-v2'
DESTINATION = 'independent-review-v2.json'
POLICY = 'exact-hook-denied-native-read-failure-v1'


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def source_binding():
    return {str(Path(__file__).resolve()): original.sha256(Path(__file__)),
            str(Path(original.__file__).resolve()): original.sha256(Path(original.__file__))}


def _lines(path):
    path = original.ordinary(path)
    _need(path.stat().st_size <= 32 * 1024 * 1024, 'Native journal exceeds the original size limit')
    values, records = [], []
    for physical_line, raw in enumerate(path.read_bytes().splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        _need(isinstance(value, dict), 'Native journal is not an object stream')
        values.append(value)
        records.append({'line': physical_line, 'line_sha256': hashlib.sha256(raw).hexdigest()})
    return values, records


def _projection(directory, inbound, hooks):
    directory = original.ordinary(directory)
    stdout = directory / 'kiro/stdout.jsonl'
    hook_path = directory / 'kiro/tool-audit.jsonl'
    actual, raw_lines = _lines(stdout)
    actual_hooks, hook_lines = _lines(hook_path)
    _need(actual == inbound and actual_hooks == hooks, 'In-memory events differ from the unchanged raw journals')
    launch = original.read_json(directory / 'kiro/launch.json')
    sid = launch['acp_preflight']['session_id']
    start, finish = original._time(launch['started_at']), original._time(launch['finished_at'])
    all_events, cloud_ids, permission_ids = defaultdict(list), set(), set()
    for index, row in enumerate(inbound):
        if row.get('method') == 'session/request_permission':
            permission_ids.add(row.get('params', {}).get('toolCall', {}).get('toolCallId'))
        if row.get('method') != 'session/update':
            continue
        update = row.get('params', {}).get('update', {})
        if update.get('sessionUpdate') not in {'tool_call', 'tool_call_update'}:
            continue
        tid = update.get('toolCallId')
        _need(isinstance(tid, str) and bool(tid), 'Native tool identity is missing')
        all_events[tid].append(index)
        if update.get('_meta', {}).get('kiro', {}).get('toolId') == 'fetch_cloud_config':
            cloud_ids.add(tid)
    calls, removed, bindings, used_hooks, used_operations = set(), set(), [], set(), set()
    last_hook = -1
    for index, row in enumerate(inbound):
        if row.get('method') != 'session/update':
            continue
        params, update = row.get('params', {}), row.get('params', {}).get('update', {})
        kind, tid = update.get('sessionUpdate'), update.get('toolCallId')
        if kind not in {'tool_call', 'tool_call_update'} or tid in cloud_ids:
            continue
        if kind == 'tool_call':
            calls.add(tid)
            continue  # The original verifier retains duplicate/call/schema checks.
        if update.get('status') not in {'completed', 'failed', 'cancelled'} or tid in calls:
            continue
        _need(update.get('status') == 'failed', 'Post-hoc codec rejects successful or cancelled orphan completion')
        _need(all_events[tid] == [index] and tid not in permission_ids,
              'Orphan failure is repeated, has a hidden lifecycle, or requested permission')
        _need(params.get('sessionId') == sid, 'Orphan read belongs to another native session')
        _need(set(update) == {'sessionUpdate', 'toolCallId', 'status', 'title', 'rawInput', 'locations', '_meta'},
              'Orphan read has output, content, mutation metadata or unsupported fields')
        _need(update.get('title') == 'Read File' and update.get('_meta') in (
            {'kiro': {'toolOrigin': 'default'}}, {'kiro': {'toolOrigin': 'acp'}}),
            'Orphan failure is not the observed native read shape; MCP and unknown origins are rejected')
        args = update.get('rawInput')
        _need(isinstance(args, dict) and set(args) == {'path', 'offset', 'limit'}
              and args['offset'] is None and args['limit'] is None,
              'Orphan read arguments are not the qualified full-file read shape')
        path = args['path']
        _need(isinstance(path, str) and bool(path) and Path(path).is_absolute()
              and '..' not in Path(path).parts and not any(c in path for c in ('$', '%', '://'))
              and not path.startswith('~') and update['locations'] == [{'path': path}],
              'Orphan read lacks one exact literal path and matching native location')
        matches = [(i, h) for i, h in enumerate(hooks) if h.get('allow') is False
                   and h.get('tool') == 'read_file' and h.get('paths') == [path]]
        _need(len(matches) == 1, 'Orphan read has no unique exact-path denied read_file hook')
        hook_index, hook = matches[0]
        _need(hook_index not in used_hooks and hook_index > last_hook
              and isinstance(hook.get('reason'), str) and bool(hook['reason'].strip())
              and start <= original._time(hook['at']) <= finish,
              'Denied read hook was reused, reordered, unreasoned or outside the native launch')
        previous = inbound[index - 1] if index else {}
        previous_params = previous.get('params', {})
        previous_update = previous_params.get('update', {})
        meta = previous_update.get('_meta', {}).get('kiro', {})
        notification = meta.get('hook', {})
        operation = notification.get('operationId')
        _need(previous.get('method') == 'session/update' and previous_params.get('sessionId') == sid
              and previous_update.get('sessionUpdate') == 'session_info_update' and meta.get('kind') == 'hook_update'
              and notification.get('hookId') == str(directory / 'target/.kiro/hooks/kiroclops-guard.json') + '#hook-0'
              and notification.get('status') == 'completed' and notification.get('actionType') == 'runCommand'
              and isinstance(operation, str) and bool(operation) and operation not in used_operations,
              'Orphan read lacks its adjacent completed native scope-guard notification')
        removed.add(index)
        used_hooks.add(hook_index)
        used_operations.add(operation)
        last_hook = hook_index
        bindings.append({'tool_call_id': tid, 'path': path, 'native_origin': update['_meta']['kiro']['toolOrigin'],
                         'stdout': raw_lines[index], 'hook_notification': raw_lines[index - 1],
                         'hook_operation_id': operation, 'denied_hook': hook_lines[hook_index],
                         'denied_at': hook['at'], 'denied_reason': hook['reason'],
                         'executed': False, 'output_observed': False})
    projection = [row for i, row in enumerate(inbound) if i not in removed]
    index_map = [raw_lines[i]['line'] for i in range(len(inbound)) if i not in removed]
    proof = {'policy': POLICY, 'status': 'pass', 'denied_read_count': len(bindings), 'bindings': bindings,
             'stdout_path': str(stdout), 'stdout_sha256': original.sha256(stdout),
             'hook_path': str(hook_path), 'hook_sha256': original.sha256(hook_path),
             'launch_sha256': original.sha256(directory / 'kiro/launch.json'),
             'scope': 'Blocked read attempts remain recorded. They are not source reads, writes, or successful executions.'}
    return projection, index_map, proof


def verify_authorship(directory, state, candidate, inbound, hooks):
    """Strict observational codec followed by the original unchanged write replay."""
    projection, index_map, proof = _projection(directory, inbound, hooks)
    result = original.verify_authorship(directory, state, candidate, projection, hooks)
    result = deepcopy(result)
    for write in result.get('writes', []):
        logical_line = write['stdout_line']
        _need(type(logical_line) is int and 1 <= logical_line <= len(index_map), 'Original write line is invalid')
        write['projected_stdout_line'] = logical_line
        write['stdout_line'] = index_map[logical_line - 1]
    result['denied_read_codec'] = proof
    return result


def _compute(directory, observations_path, source_review_path, *, original_rejection_path=None):
    directory = original.ordinary(directory)
    bound = source_binding()
    prior = {}
    prior_path = directory / 'final/independent-review.json'
    for key, path in [('original_review', prior_path if prior_path.is_file() else None),
                      ('original_rejection_diagnosis', original_rejection_path)]:
        if path is not None:
            path = original.ordinary(path)
            prior[key] = {'path': str(path), 'sha256': original.sha256(path)}
    parent = original._compute
    globals_copy = dict(parent.__globals__)
    globals_copy['verify_authorship'] = verify_authorship
    compute = types.FunctionType(parent.__code__, globals_copy, parent.__name__, parent.__defaults__, parent.__closure__)
    compute.__kwdefaults__ = deepcopy(parent.__kwdefaults__)
    result = compute(directory, observations_path, source_review_path)
    _need(source_binding() == bound, 'Review implementation changed during replay')
    for value in prior.values():
        _need(original.sha256(value['path']) == value['sha256'], 'Original review or rejection evidence changed')
    _need(result.get('audit_version') == original.VERSION, 'Unexpected original audit implementation result')
    result['parent_audit_version'] = result['audit_version']
    result['audit_version'] = VERSION
    result['posthoc_revision'] = {'policy': POLICY, 'source_hashes': bound, 'original_evidence': prior,
        'registered_model_protocol_unchanged': True,
        'scope': 'Offline native event codec correction; all original acceptance rules replay without changes.'}
    result['evidence_files'].update(bound)
    result['evidence_files'].update({v['path']: v['sha256'] for v in prior.values()})
    return result


def audit_session(directory, observations_path, source_review_path, *, original_rejection_path=None):
    directory = original.ordinary(directory)
    destination = original.ordinary(directory / 'final' / DESTINATION)
    _need(not destination.exists(), 'Post-hoc review is immutable; preserve the previous attempt')
    try:
        record = _compute(directory, observations_path, source_review_path,
                          original_rejection_path=original_rejection_path)
    except Exception as exc:
        record = {'schema_version': 1, 'audit_version': VERSION, 'status': 'fail', 'reason': str(exc),
                  'parent_audit_version': original.VERSION, 'review_source_hashes': source_binding(),
                  'observations_path': str(observations_path), 'source_review_path': str(source_review_path),
                  'original_rejection_path': str(original_rejection_path) if original_rejection_path else None}
    record['reviewed_at'] = original.utc_now()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    return record


def verify_review(directory):
    directory = original.ordinary(directory)
    path = original.ordinary(directory / 'final' / DESTINATION)
    if not path.is_file():
        return {'status': 'pending', 'reason': 'Independent post-hoc review v2 is not present'}
    try:
        saved = original.read_json(path)
        _need(saved.get('status') == 'pass' and saved.get('audit_version') == VERSION,
              'Post-hoc review did not pass or has another version')
        _need(saved['posthoc_revision']['source_hashes'] == source_binding(), 'Post-hoc review code changed')
        for name, expected in saved['evidence_files'].items():
            _need(original.sha256(original.ordinary(name)) == expected, 'Independent evidence changed')
        rejection = saved['posthoc_revision']['original_evidence'].get('original_rejection_diagnosis')
        current = _compute(directory, saved['observations_path'], saved['source_review_path'],
                           original_rejection_path=rejection['path'] if rejection else None)
        _need(current == {k: v for k, v in saved.items() if k != 'reviewed_at'}, 'Post-hoc review no longer replays')
        return {'status': 'pass', 'candidate_id': saved['candidate_id'], 'source_sha256': saved['source_sha256'],
                'comparison_sha256': saved['comparison_sha256'], 'audit_sha256': original.sha256(path),
                'audit_version': VERSION, 'parent_audit_version': original.VERSION}
    except Exception as exc:
        return {'status': 'fail', 'reason': str(exc)}
