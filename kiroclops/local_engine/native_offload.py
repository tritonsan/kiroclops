"""Pinned KAS 0.66.8 foreground shell preview envelope, without file access.

This recognizes a returned preview, not the missing middle of the output.
Native arguments, guard decisions and deny/no-output checks remain separate.
"""
import ntpath
import re


def _units(value):
    return len(value.encode('utf-16-le', 'surrogatepass')) // 2


def _slice(value, start=None, end=None):
    data = value.encode('utf-16-le', 'surrogatepass')
    return data[slice(None if start is None else start * 2,
                      None if end is None else end * 2)].decode('utf-16-le', 'surrogatepass')


def shell_offload_terminal(update, session_id):
    """Accept only a completed, bounded, session-bound shell preview shape.

    The current supported subset has an ASCII decimal exit code and a raw
    output string that KAS has also shortened using its 500-unit head/tail
    transport rule. Unrecognized variants stay unproven.
    """
    meta = update.get('_meta')
    if not isinstance(meta, dict) or set(meta) != {'kiro'}:
        return False
    km = meta['kiro']
    if (not isinstance(km, dict) or set(km) != {'toolOrigin', 'outputTransformation'}
            or km['toolOrigin'] != 'default' or update.get('status') != 'completed'):
        return False
    change = km['outputTransformation']
    if (not isinstance(change, dict) or set(change) != {'kind', 'absFilePath', 'totalChars'}
            or change['kind'] != 'offloaded' or type(change['totalChars']) is not int
            or not 30000 <= change['totalChars'] <= 32 * 1024 * 1024):
        return False
    path = change['absFilePath']
    if (not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', session_id)
            or not isinstance(path, str) or '\x00' in path or len(path) > 2048
            or not re.fullmatch(r'[A-Za-z]:\\[^\r\n<>"|?*]+', path)
            or ntpath.normpath(path) != path
            or not re.search(r'\\\.kiro\\sessions\\[0-9a-f]{16}\\' + re.escape(session_id)
                             + r'\\tool-outputs\\execute_pwsh-[0-9a-f]{8}\.txt\Z', path)):
        return False
    raw = update.get('rawOutput')
    if (not isinstance(raw, dict) or set(raw) != {'output', 'exitCode', 'message'}
            or type(raw['exitCode']) is not int or not -2147483648 <= raw['exitCode'] <= 2147483647
            or not isinstance(raw['output'], str) or not isinstance(raw['message'], str)
            or _units(raw['output']) > 1100 or _units(raw['message']) > 5000):
        return False
    match = re.fullmatch(r'(.*)\n\.\.\.\[truncated ([0-9]+) chars\]\.\.\.\n(.*)', raw['output'], re.DOTALL)
    if not match:
        return False
    head, omitted, tail = match.groups()
    if (_units(head) not in (499, 500) or _units(tail) not in (499, 500)
            or len(omitted) > 8 or omitted.startswith('0')):
        return False
    full_output_length = _units(head) + int(omitted) + _units(tail)
    suffix = '\n\nExit Code: ' + str(raw['exitCode'])
    total = _units('Output:\n') + full_output_length + _units(suffix)
    if full_output_length < 30000 or total != change['totalChars']:
        return False
    preview_head = _slice('Output:\n' + head, 0, 500)
    preview_tail = _slice(tail + suffix, -500)
    expected = '\n'.join([
        f'The output from "execute_pwsh" ({total:,} chars) exceeded the 30,000-char limit; here is a preview of the tool\'s result:',
        '', '--- HEAD (first 500 chars) ---', preview_head, '',
        f'--- {total - 1000:,} chars omitted ---', '', '--- TAIL (last 500 chars) ---', preview_tail,
        '--- END ---', '', 'If more context is needed:',
        '1. Retry the tool with narrower arguments — a tighter pattern, a filter, a smaller range or page — so the result fits.',
        "2. The full output was saved to a file under '" + path.replace('\\', '/')
        + "'. Inspect it with targeted shell commands, combining them so the result stays within the size limit (e.g. head -c 2000, tail -c 5000, grep -n 'pattern' | head -c 2000).",
    ])
    return (raw['message'] == expected and update.get('content') ==
            [{'type': 'content', 'content': {'type': 'text', 'text': expected}}])
