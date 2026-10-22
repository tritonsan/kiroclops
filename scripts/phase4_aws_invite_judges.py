"""Create judge accounts on the live COOL deployment and check that each can log in.

Phase 4 AWS addition after the V6 freeze. ``services.analysis_api.invite`` registers an
account the way the service expects (Cognito user, tenant and account records, the
five-invitation counter), but it accepts only the standard-OpenCV beta deployment and
asks the operator for a temporary password. Cognito temporary passwords expire after
seven days and judging runs until 9 November, so this wrapper:

* runs ``invite()`` unchanged, supplying only its deployment-purpose check and the
  password prompt (a generated temporary password that is never stored),
* then sets a generated permanent password, so judges are not forced to change it,
* stores the username, the password and the bootstrap ``service.json`` only under
  ``<deployment>/judges/<username>/`` (``artifacts/`` is git-ignored),
* logs in once from a separate copy of the configuration, reads ``/usage`` and logs
  out again (revoking the session), so the judges' ``service.json`` stays untouched.

    python scripts/phase4_aws_invite_judges.py --deployment artifacts/phase4-aws/live-cool-02 \
        --usernames <judge-a> <judge-b>
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import shutil
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.cloud import __main__ as client  # noqa: E402
from kiroclops.visual.schema import read_json, write_json  # noqa: E402
from services.analysis_api import invite as invitation  # noqa: E402
from services.analysis_api.deploy import aws  # noqa: E402


def password():
    # Cognito policy of this pool: at least 20 characters with upper, lower, digit and symbol.
    return 'Kc5!'+secrets.token_urlsafe(18)


def invite_judge(deployment, username):
    deployment = Path(deployment).resolve()
    state = read_json(deployment/'state.json')
    if state.get('purpose') != 'invited-beta-cool':
        raise ValueError('Use the persistent COOL deployment')
    folder = deployment/'judges'/username
    if folder.exists():
        raise ValueError('This judge folder already exists; never replace credentials silently')
    folder.mkdir(parents=True)

    def state_for_invite(path):
        value = read_json(path)
        # invite() only verifies that this is a reusable, invited-beta deployment.
        return {**value, 'purpose': 'invited-beta-standard-opencv'} if Path(path).name == 'state.json' else value

    temporary = password()
    with patch.object(invitation, 'read_json', state_for_invite), patch('getpass.getpass', return_value=temporary):
        receipt = invitation.invite(deployment, username, folder/'service.json')
    del temporary
    permanent = password()
    aws('cognito-idp', 'admin-set-user-password', user_pool_id=state['Pool'], username=username,
        password=permanent, permanent=True)
    write_json(folder/'credentials.private.json', {
        'username': username, 'password': permanent, 'password_kind': 'permanent', 'service_config': 'service.json',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'note': 'Hand over through a private channel only (e.g. Devpost testing instructions). Disable after judging.'})

    verify = folder/'login-check'
    verify.mkdir()
    shutil.copyfile(folder/'service.json', verify/'service.json')
    with patch('getpass.getpass', return_value=permanent):
        logged_in = client.login(verify/'service.json', username)
    config = read_json(verify/'service.json')
    backend = client.human_backend(config, verify/'.kiroclops-private'/'control')
    usage = backend._call('GET', '/usage')
    logged_out = client.logout(config, backend)
    shutil.rmtree(verify/'.kiroclops-private', ignore_errors=True)
    check = {'checked_at': datetime.now(timezone.utc).isoformat(), 'username': username,
             'invite': receipt, 'login': {k: logged_in[k] for k in ('status', 'agent_scope')},
             'usage_keys': sorted(usage), 'logout': logged_out, 'tokens_logged': False}
    write_json(folder/'login-check.json', check)
    return {'username': username, 'login': logged_in['status'], 'logout': logged_out['status']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--usernames', nargs='+', required=True)
    args = parser.parse_args()
    for name in args.usernames:
        print(json.dumps(invite_judge(args.deployment, name)), flush=True)
