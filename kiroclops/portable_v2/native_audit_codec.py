"""Registered native event decoding for exact hook-denied failure-only reads.

Only uniquely bound, nonexecuted native read failures are omitted from the
in-memory authorship lifecycle. The original journals are never changed.
All other completions remain subject to strict local authorship replay.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path

from kiroclops.integration.storage import read_json, sha256
from .session import ordinary

POLICY = 'exact-hook-denied-native-read-failure-v1'
VERSION = 'portable-v2-native-audit-codec-v1'


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _time(value):
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _need(instant.tzinfo is not None, 'Evidence time must include its timezone')
    return instant


def _lines(path):
    path = ordinary(path)
    _need(path.stat().st_size <= 32 * 1024 * 1024, 'Native journal exceeds the registered size limit')
    values, records = [], []
    for physical_line, raw in enumerate(path.read_bytes().splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        _need(isinstance(value, dict), 'Native journal is not an object stream')
        values.append(value)
        records.append({'line': physical_line, 'line_sha256': hashlib.sha256(raw).hexdigest()})
    return values, records


def decode(directory, inbound, hooks):
    directory = ordinary(directory)
    stdout = directory / 'kiro/stdout.jsonl'
    hook_path = directory / 'kiro/tool-audit.jsonl'
    actual, raw_lines = _lines(stdout)
    actual_hooks, hook_lines = _lines(hook_path)
    _need(actual == inbound and actual_hooks == hooks, 'In-memory events differ from the unchanged raw journals')
    launch = read_json(directory / 'kiro/launch.json')
    sid = launch['acp_preflight']['session_id']
    start, finish = _time(launch['started_at']), _time(launch['finished_at'])
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
            continue  # The local lifecycle verifier retains duplicate/call/schema checks.
        if update.get('status') not in {'completed', 'failed', 'cancelled'} or tid in calls:
            continue
        _need(update.get('status') == 'failed', 'Registered codec rejects successful or cancelled orphan completion')
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
              and start <= _time(hook['at']) <= finish,
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
    proof = {'policy': POLICY, 'codec_version': VERSION, 'codec_sha256': sha256(Path(__file__)), 'status': 'pass', 'denied_read_count': len(bindings), 'bindings': bindings,
             'stdout_path': str(stdout), 'stdout_sha256': sha256(stdout),
             'hook_path': str(hook_path), 'hook_sha256': sha256(hook_path),
             'launch_sha256': sha256(directory / 'kiro/launch.json'),
             'scope': 'Blocked read attempts remain recorded. They are not source reads, writes, or successful executions.'}
    return projection, index_map, proof

