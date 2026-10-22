"""Inspect native call arguments without emitting model text or tool outputs.

Terminal-only rejected calls are included. File write bodies are summarized by
digest unless explicitly requested; these summaries are not a source review.
"""
import argparse
import hashlib
import json
from pathlib import Path


def calls(path):
    groups = {}
    incomplete_tail = False
    lines = Path(path).read_bytes().splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            if number == len(lines):
                incomplete_tail = True
                break
            raise ValueError(f'Invalid recorded JSON on line {number}')
        if not isinstance(event, dict) or not isinstance(event.get('params'), dict):
            continue
        update = event['params'].get('update')
        if not isinstance(update, dict) or update.get('sessionUpdate') not in {'tool_call', 'tool_call_update'}:
            continue
        identity = update.get('toolCallId')
        if not isinstance(identity, str) or not identity:
            raise ValueError(f'Native call has no identity on line {number}')
        row = groups.setdefault(identity, {'tool_call_id': identity, 'first_line': number,
            'started': False, 'title': None, 'status': None, 'arguments': []})
        row['started'] |= update['sessionUpdate'] == 'tool_call'
        if update.get('title') is not None:
            row['title'] = update['title']
        row['status'] = update.get('status')
        if 'rawInput' in update and update['rawInput'] not in row['arguments']:
            row['arguments'].append(update['rawInput'])
    return list(groups.values()), incomplete_tail


def render_arguments(values, include_edits):
    def shorten(value):
        if not isinstance(value, dict) or include_edits:
            return value
        result = dict(value)
        for field in ('text', 'oldStr', 'newStr'):
            if isinstance(result.get(field), str):
                raw = result[field].encode('utf-8')
                result[field] = {'not_displayed': True, 'utf8_bytes': len(raw),
                                 'sha256': hashlib.sha256(raw).hexdigest()}
        return result
    return [shorten(v) for v in values]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--after', type=int, default=0, help='Skip this many distinct native call identities')
    parser.add_argument('--tool', default='', help='Only titles containing this text')
    parser.add_argument('--include-edits', action='store_true')
    args = parser.parse_args()
    rows, tail = calls(args.session / 'kiro/run/stdout.jsonl')
    print(json.dumps({'native_calls_seen': len(rows), 'incomplete_tail': tail,
                      'source_review_granted': False}))
    for index, row in enumerate(rows, 1):
        if index <= args.after or args.tool.lower() not in str(row['title']).lower():
            continue
        print(json.dumps({**row, 'ordinal': index,
            'arguments': render_arguments(row['arguments'], args.include_edits)}, ensure_ascii=False))
