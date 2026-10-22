"""Preserve native hook provenance across the exact Opus 5.5 model-only change.

This source proof sends no model prompt and makes no live Opus outcome claim.
The earlier runtime inspection and canary remain labeled with their own model.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'artifacts/phase3a/opus55-switch-01/launcher-before.py'
CURRENT = ROOT / 'kiroclops/local_engine/launcher.py'
CLIENT = ROOT / 'kiroclops/portable_v2/launcher.py'
PRIOR = ROOT / 'artifacts/phase3a/native-hook-audit-serialization-source-proof.json'
RUNTIME = ROOT / 'artifacts/phase3a/native-hook-sol24-installed-source-proof.json'
CANARY = ROOT / 'artifacts/phase3a/native-hook-canary-sol10/raw-observation.json'
OUTPUT = ROOT / 'artifacts/phase3a/native-hook-opus55-model-source-proof.json'
FROZEN_SHA = 'a816685f811ebb1fa04f50c51bea2f126be8228163161226f092474f8f0efa32'
PRIOR_SHA = 'ec4a69d38c158ae33c2bc4bca20439e211e6e54881425c50de9dc47bd173ea3a'
RUNTIME_SHA = 'fd350131186aefaafe13e925888461e09fc659a9297616b74341b8fb4cafd10a'
CANARY_SHA = 'f3c226836e95169cb2b14b1aec4b8ac2ad584073aa8f7c9c6538ee741266421b'
BEFORE = b"MODEL, EFFORT = 'gpt-5.6-sol', 'max'"
AFTER = b"MODEL, EFFORT = 'claude-opus-5.5', 'max'"


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def binding(path: Path) -> dict[str, str]:
    return {'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path.read_bytes())}


def build_proof() -> dict:
    frozen, current = FROZEN.read_bytes(), CURRENT.read_bytes()
    for path, expected in ((FROZEN, FROZEN_SHA), (PRIOR, PRIOR_SHA),
                           (RUNTIME, RUNTIME_SHA), (CANARY, CANARY_SHA)):
        if digest(path.read_bytes()) != expected:
            raise RuntimeError(f'Historical evidence changed: {path.name}')
    prior, runtime, canary = (json.loads(path.read_bytes()) for path in (PRIOR, RUNTIME, CANARY))
    if (prior['status'] != 'pass' or prior['current_local_launcher']['sha256'] != FROZEN_SHA
            or prior['current_portable_client']['sha256'] != digest(CLIENT.read_bytes())):
        raise RuntimeError('Prior source proof does not bind the frozen launcher and current client')
    if frozen.count(BEFORE) != 1 or current.count(AFTER) != 1 or frozen.replace(BEFORE, AFTER) != current:
        raise RuntimeError('Launcher changed beyond the exact model identifier substitution')
    if (runtime['status'] != 'pass' or digest(Path(runtime['source']).read_bytes()) != runtime['source_sha256']
            or not all(runtime['source_checks'].values())):
        raise RuntimeError('Inspected installed KAS runtime source changed')
    if (canary['status'] != 'raw_observed_pass'
            or (canary['model'], canary['effort']) != ('gpt-5.6-sol', 'max')
            or (canary['cli_version'], canary['kas_version']) !=
               ('kiro-cli-chat ' + runtime['cli_version'], runtime['kas_version'])):
        raise RuntimeError('Historical native canary identity differs')
    return {
        'kind': 'NativeHookOpus55ModelSourceEquivalence', 'status': 'pass',
        'frozen_local_launcher': binding(FROZEN), 'current_local_launcher': binding(CURRENT),
        'current_portable_client': binding(CLIENT), 'prior_source_proof': binding(PRIOR),
        'installed_runtime_source_proof': binding(RUNTIME), 'historical_native_canary': binding(CANARY),
        'approved_model_change': {'before': ['gpt-5.6-sol', 'max'], 'after': ['claude-opus-5.5', 'max']},
        'allowed_changes': [{'before': BEFORE.decode(), 'after': AFTER.decode()}],
        'local_source_equivalent_outside_model_assignment': True,
        'installed_source_sha256': runtime['source_sha256'],
        'cli_version': 'kiro-cli-chat ' + runtime['cli_version'], 'kas_version': runtime['kas_version'],
        'scope': ('Byte-exact launcher equality after one model identifier substitution; maximum effort, '
                  'native hook/guard, ACP client and transport are unchanged. Runtime inspection and the '
                  'five-call Sol canary remain historical source/output evidence. This is not a live Opus '
                  '5.5 outcome, model-availability proof, or cryptographic runtime attestation.'),
    }


def main() -> None:
    proof = build_proof()
    encoded = (json.dumps(proof, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if OUTPUT.exists() and OUTPUT.read_bytes() != encoded:
        raise RuntimeError('Existing immutable Opus 5.5 source proof would change')
    if not OUTPUT.exists():
        OUTPUT.write_bytes(encoded)
    print(json.dumps({'status': 'pass', 'output': str(OUTPUT), 'proof_sha256': digest(encoded),
                      'launcher_sha256': proof['current_local_launcher']['sha256']}, indent=2))


if __name__ == '__main__':
    main()
