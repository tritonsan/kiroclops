"""Pinned Windows native background *launch* receipt; not child completion."""
import re


def background_launch_terminal(update):
    if update.get('status') != 'completed' or update.get('_meta') != {'kiro': {'toolOrigin': 'default'}}:
        return None
    raw = update.get('rawOutput')
    if not isinstance(raw, dict) or set(raw) != {'output', 'exitCode', 'message'}:
        return None
    if type(raw['exitCode']) is not int or raw['exitCode'] != 0 or not isinstance(raw['output'], str):
        return None
    matched = re.fullmatch(r'Background process started \(pid: ([1-9][0-9]*)\)', raw['output'])
    if not matched:
        return None
    message = 'Output:\n' + raw['output'] + '\n\nExit Code: 0'
    if raw['message'] != message or update.get('content') != [{'type': 'content', 'content': {'type': 'text', 'text': message}}]:
        return None
    return {'pid': int(matched[1]), 'effect': 'background_process_spawned',
            'child_completion': 'not_attested', 'child_exit_code': None,
            'note': 'Native exitCode 0 acknowledges spawn only. Process lifetime requires separate evidence.'}
