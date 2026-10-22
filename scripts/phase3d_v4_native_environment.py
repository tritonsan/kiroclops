"""V4 native environment identity: the receipted one-server MCP environment.

For the V4 Kiro runs the global "kiroclops" Kiro power was removed (user
decision, PHASE3D_V4_PLAN.md; receipt
``artifacts/phase3d-v4/kiro-power-window-01/receipt.json``). Each session
therefore listed one MCP server, the workspace server 'kiroclops'. The canonical
2.27 metadata rule enumerates exactly two servers: 'kiroclops' and
'power-kiroclops-kiroclops'.

This process-local policy accepts exactly that one-server status. The single
server must satisfy every field rule the canonical shape applies to the
'kiroclops' server, and it is checked by the unchanged canonical function. Every
other native check is unchanged: prompt count, model, effort, tool outcomes,
hook cardinality, stream replay and the requirement that the final metadata
equals a pre-generation event.

The policy applies only to sessions named in the receipt whose whole Kiro run
lies inside the removal window. It composes with the 2.27.1 release identity
mapping. The original function is restored even on error.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import functools
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import review
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.schema import VisualError, file_sha256, read_json
from scripts import phase3d_v2_native_release_identity as identity

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / 'artifacts/phase3d-v4/kiro-power-window-01/receipt.json'
RECEIPT_SHA256 = 'a15ea06ece9cda61476f585488130b732982cda344a032d2402b787a28255ad8'
POWER_SERVER = 'power-kiroclops-kiroclops'
POWER_ORIGIN = {'origin': 'power', 'power': {'name': 'kiroclops', 'source': {'origin': 'user'}}}


def _need(condition, message, code='environment_identity_unproven'):
    if not condition:
        raise VisualError(code, message)


def window(receipt=RECEIPT, receipt_sha256=RECEIPT_SHA256):
    receipt = _ordinary(receipt)
    _need(receipt.is_file() and file_sha256(receipt) == receipt_sha256,
          'The pinned Kiro power-removal receipt changed or is missing.')
    value = read_json(receipt)
    _need(value.get('kind') == 'V4KiroPowerRemovalWindow' and value.get('schema_version') == 1
          and value.get('restore', {}).get('log_text', '').endswith('RESTORED_IDENTICAL')
          and value.get('current_powers_equal_backup') is True,
          'The power-removal receipt does not document a restored removal window.')
    return value


def _inside(session, value):
    session = _ordinary(session)
    launch = read_json(session / 'kiro/launch.json')
    started, finished = launch.get('prompt_started_at'), launch.get('finished_at')
    rows = [row for row in value['sessions'] if _ordinary(row['session']) == session]
    return (len(rows) == 1 and rows[0].get('inside_window') is True
            and rows[0].get('launch_sha256') == file_sha256(session / 'kiro/launch.json')
            and type(started) in (int, float) and type(finished) in (int, float)
            and value['removal']['started_at'] < started <= finished < value['restore']['finished_at'])


def one_server_variant(original):
    """Accept exactly the one-server status whose server passes the canonical 'kiroclops' rules."""
    @functools.wraps(original)
    def shape(event, session_id):
        if original(event, session_id):
            return True
        if not isinstance(event, dict) or event.get('method') != '_kiro/mcp/status':
            return False
        servers = (event.get('params') or {}).get('servers')
        if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], dict) \
                or servers[0].get('name') != 'kiroclops':
            return False
        # The canonical function checks the two-server list as a whole. Add the
        # absent power server as an exact field copy with its canonical origin,
        # so the real server is checked by the unchanged canonical rules.
        twin = deepcopy(event)
        absent = deepcopy(servers[0])
        absent['name'] = POWER_SERVER
        try:
            absent['_meta']['kiro']['resource']['source'] = deepcopy(POWER_ORIGIN)
        except (KeyError, TypeError):
            return False
        twin['params']['servers'] = [servers[0], absent]
        return original(twin, session_id)
    return shape


@contextmanager
def environment_policy(sessions, *, receipt=RECEIPT, receipt_sha256=RECEIPT_SHA256):
    value = window(receipt, receipt_sha256)
    for session in sessions:
        _need(_inside(session, value), 'The session did not run inside the receipted power-removal window: ' + str(session))
    original = review._builtin_227_metadata_shape
    review._builtin_227_metadata_shape = one_server_variant(original)
    try:
        yield {'kind': 'V4OneServerEnvironmentPolicy', 'receipt': str(receipt), 'receipt_sha256': receipt_sha256,
               'sessions': [str(_ordinary(s)) for s in sessions]}
    finally:
        review._builtin_227_metadata_shape = original


def audit_native_run(session, *, receipt=RECEIPT, receipt_sha256=RECEIPT_SHA256):
    """Release identity replay inside the receipted one-server environment."""
    with environment_policy([session], receipt=receipt, receipt_sha256=receipt_sha256) as policy:
        value = identity.audit_native_run(session)
    return {**value, 'environment_identity': policy}


def replay(sessions, controls, output):
    """Record canonical, release-identity and environment outcomes; controls must stay unchanged."""
    output = Path(output)
    _need(not output.exists(), 'Preserve earlier native environment replays.', 'output_exists')

    def outcome(function):
        try:
            value = function()
            return {'status': value.get('status', 'pass')}
        except VisualError as exc:
            return {'status': 'fail', 'code': exc.code, 'message': str(exc)}

    rows = []
    for session in sessions:
        rows.append({'session': str(_ordinary(session)),
                     'canonical': outcome(lambda: review.audit_native_run(_ordinary(session))),
                     'release_identity': outcome(lambda: identity.audit_native_run(session)),
                     'environment_identity': outcome(lambda: audit_native_run(session))})
    checks = []
    for session in controls:
        before = outcome(lambda: identity.audit_native_run(session))
        refused = outcome(lambda: audit_native_run(session))
        checks.append({'session': str(_ordinary(session)), 'release_identity': before,
                       'environment_policy_refused': refused['status'] == 'fail'})
    value = {'kind': 'V4NativeEnvironmentReplay', 'schema_version': 1, 'sessions': rows, 'controls': checks,
             'receipt_sha256': RECEIPT_SHA256, 'producer_sha256': file_sha256(__file__),
             'scope': 'Only the one-server MCP status shape is mapped; every other native check is unchanged.'}
    output.mkdir(parents=True)
    (output / 'result.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--session', type=Path, action='append', required=True)
    parser.add_argument('--control', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    value = replay(args.session, args.control, args.output)
    print(json.dumps({'sessions': [{k: (r[k]['status'] if isinstance(r[k], dict) else r[k]) for k in r} for r in value['sessions']],
                      'controls': value['controls']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
