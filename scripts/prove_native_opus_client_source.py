"""Bind Opus trial source to the observed native Kiro runtime and frozen client."""
from __future__ import annotations

import ast
from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'artifacts/phase3a'
FROZEN_LOCAL = BASE / 'local-kit-13/kiroclops/local_engine/launcher.py'
CURRENT_LOCAL = ROOT / 'kiroclops/local_engine/launcher.py'
FROZEN_CLIENT = BASE / 'local-kit-13/kiroclops/portable_v2/launcher.py'
CURRENT_CLIENT = ROOT / 'kiroclops/portable_v2/launcher.py'
PRIOR = BASE / 'native-hook-opus05-source-proof.json'
OUTPUT = BASE / 'native-hook-opus05-client-source-proof.json'
FROZEN_LOCAL_SHA = '8788c3bb6f90a2e791d820365e713e070bbd58f69f6b59f20c7aadd19e3935db'
FROZEN_CLIENT_SHA = '62f4ffa221ec9f0e5f06339bc9b6a625b3cdb0e7dded576523bb346a72675038'
PRIOR_SHA = 'ecaab758db8a4e6863143b2ccc52e82ae4427910d0b161ef32c96a7f162f155e'
INTERMEDIATE_LOCAL_SHA = '0201bc0623a9388d32b2d5e829b803d468d6a0d1ffaa8f4a8f12ce8d97b861fb'

LOCAL_CLIENT_CHANGE = (
    'class LocalACPClient(NativeClient):\n    def handshake(self):',
    'class LocalACPClient(NativeClient):\n    expected_model = MODEL\n'
    '    expected_effort = EFFORT\n    expected_approval_mode = APPROVAL\n\n'
    '    def handshake(self):',
)
CLIENT_CHANGES = [
    (
        'class ModelACPClient(SelectionClient):\n'
        '    """Reuse native transport/selection; replace every legacy stage boundary."""\n'
        '    def __init__(self, ready, log_dir, timeout, cancel):',
        'class ModelACPClient(SelectionClient):\n'
        '    """Reuse native transport/selection; replace every legacy stage boundary."""\n'
        '    expected_model = MODEL\n    expected_effort = EFFORT\n'
        '    expected_approval_mode = APPROVAL_MODE\n\n'
        '    def __init__(self, ready, log_dir, timeout, cancel):',
    ),
    (
        'if (self.config.get("model"), self.config.get("effort"), self.config.get("approval_mode")) '
        '!= (MODEL, EFFORT, APPROVAL_MODE):\n'
        '            raise KiroError("selection_changed", "Native Sol/max and Always Allow are required")',
        'if (self.config.get("model"), self.config.get("effort"), self.config.get("approval_mode")) '
        '!= (self.expected_model, self.expected_effort, self.expected_approval_mode):\n'
        '            raise KiroError("selection_changed", '
        '"The requested model, effort and native Always Allow must match this client")',
    ),
    (
        'self.guard = native.StreamConfigurationGuard(self.config["agent_name"], model=MODEL, effort=EFFORT)',
        'self.guard = native.StreamConfigurationGuard(self.config["agent_name"], '
        'model=self.expected_model, effort=self.expected_effort)',
    ),
]


def hexdigest(data: bytes) -> str:
    return sha256(data).hexdigest()


def exact_replace(source: bytes, changes: list[tuple[str, str]]) -> bytes:
    result = source
    for old, new in changes:
        old_bytes, new_bytes = old.encode('utf-8'), new.encode('utf-8')
        if result.count(old_bytes) != 1:
            raise RuntimeError('Old source does not contain exactly one allowed replacement')
        result = result.replace(old_bytes, new_bytes)
        if result.count(new_bytes) != 1:
            raise RuntimeError('New source replacement is ambiguous')
    return result


def main() -> None:
    old_local, current_local = FROZEN_LOCAL.read_bytes(), CURRENT_LOCAL.read_bytes()
    old_client, current_client = FROZEN_CLIENT.read_bytes(), CURRENT_CLIENT.read_bytes()
    if (hexdigest(old_local), hexdigest(old_client), hexdigest(PRIOR.read_bytes())) != (
            FROZEN_LOCAL_SHA, FROZEN_CLIENT_SHA, PRIOR_SHA):
        raise RuntimeError('A frozen launcher, client or prior proof changed')
    previous = json.loads(PRIOR.read_bytes())
    if previous['status'] != 'pass' or previous['current_launcher']['sha256'] != INTERMEDIATE_LOCAL_SHA:
        raise RuntimeError('Prior Opus source proof does not bind the intermediate launcher')
    model_changes = [(row['before'], row['after']) for row in previous['allowlisted_changes']]
    intermediate = exact_replace(old_local, model_changes)
    if hexdigest(intermediate) != INTERMEDIATE_LOCAL_SHA:
        raise RuntimeError('Intermediate Opus source cannot be reconstructed')
    if exact_replace(intermediate, [LOCAL_CLIENT_CHANGE]) != current_local:
        raise RuntimeError('Local launcher changed beyond the client model override')
    if exact_replace(old_client, CLIENT_CHANGES) != current_client:
        raise RuntimeError('Portable ACP client changed beyond its expected-selection controls')
    ast.parse(current_local.decode('utf-8'))
    ast.parse(current_client.decode('utf-8'))
    runtime = previous['installed_kas']
    if hexdigest(Path(runtime['path']).read_bytes()) != runtime['sha256']:
        raise RuntimeError('Observed installed Kiro runtime changed')

    proof = {
        'kind': 'NativeHookOpusClientSourceEquivalence',
        'status': 'pass',
        'frozen_local_launcher': {'path': FROZEN_LOCAL.relative_to(ROOT).as_posix(),
                                  'sha256': FROZEN_LOCAL_SHA},
        'prior_opus_source_proof': {'path': PRIOR.relative_to(ROOT).as_posix(), 'sha256': PRIOR_SHA},
        'intermediate_local_launcher_sha256': INTERMEDIATE_LOCAL_SHA,
        'current_local_launcher': {'path': CURRENT_LOCAL.relative_to(ROOT).as_posix(),
                                   'sha256': hexdigest(current_local)},
        'local_client_override': {'before': LOCAL_CLIENT_CHANGE[0],
                                  'after': LOCAL_CLIENT_CHANGE[1]},
        'frozen_portable_client': {'path': FROZEN_CLIENT.relative_to(ROOT).as_posix(),
                                   'sha256': FROZEN_CLIENT_SHA},
        'current_portable_client': {'path': CURRENT_CLIENT.relative_to(ROOT).as_posix(),
                                    'sha256': hexdigest(current_client)},
        'portable_client_changes': [{'before': old, 'after': new} for old, new in CLIENT_CHANGES],
        'local_source_equivalent_after_allowlisted_changes': True,
        'portable_source_equivalent_after_allowlisted_changes': True,
        'installed_kas': runtime,
        'scope': ('Exact byte equality after the prior five model substitutions, one local client '
                  'override, and three portable client substitutions. Native guard, permission hook, '
                  'ACP transport and tool-audit code outside those changes are unchanged. Historical '
                  'five-probe canary and installed-source inspection remain upstream evidence; '
                  'this is not a live Opus outcome or an OS sandbox.'),
    }
    encoded = (json.dumps(proof, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if OUTPUT.exists() and OUTPUT.read_bytes() != encoded:
        raise RuntimeError('Existing immutable client source proof would change')
    if not OUTPUT.exists():
        OUTPUT.write_bytes(encoded)
    print(json.dumps({'status': 'pass', 'proof_sha256': hexdigest(encoded),
                      'current_local_sha256': hexdigest(current_local),
                      'current_portable_sha256': hexdigest(current_client),
                      'output': str(OUTPUT)}, indent=2))


if __name__ == '__main__':
    main()
