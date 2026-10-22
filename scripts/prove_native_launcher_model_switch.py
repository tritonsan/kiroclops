"""Bind the Opus launcher to the previously qualified native Kiro hook source.

Only the declared model identifier and model-specific presentation text may
change. Any other source edit, including guard/hook/client behavior, fails.
"""
from __future__ import annotations

import ast
from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / 'artifacts/phase3a/local-kit-13/kiroclops/local_engine/launcher.py'
CURRENT = ROOT / 'kiroclops/local_engine/launcher.py'
PRIOR_PROOF = ROOT / 'artifacts/phase3a/native-hook-canary-05-source-proof.json'
OUTPUT = ROOT / 'artifacts/phase3a/native-hook-opus05-source-proof.json'
OLD_SHA = '8788c3bb6f90a2e791d820365e713e070bbd58f69f6b59f20c7aadd19e3935db'
PRIOR_PROOF_SHA = 'c9d2e9822d08a40c7945fdb74412a668569220429b737d2bc127ddaa9cdda861'

# These are the complete allowed changes, each occurring exactly once.
REPLACEMENTS = [
    ('Recorded native Kiro CLI Sol/max trials',
     'Recorded native Kiro CLI Opus 5/max trials'),
    ("MODEL, EFFORT = 'gpt-5.6-sol', 'max'",
     "MODEL, EFFORT = 'claude-opus-5', 'max'"),
    ("'Exact Sol/max and native Always Allow required'",
     "f'Exact {MODEL}/{EFFORT} and native Always Allow required'"),
    ("'Exact Sol model unavailable; no fallback'",
     "f'Exact {MODEL} model unavailable; no fallback'"),
    ("'No actual Sol/max generation confirmed'",
     "f'No actual {MODEL}/{EFFORT} generation confirmed'"),
]


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def source_node_hashes(source: str, names: tuple[str, ...]) -> dict[str, str]:
    tree = ast.parse(source)
    selected = {node.name: node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names}
    if set(selected) != set(names):
        raise RuntimeError('A guarded launcher node is missing')
    return {name: digest(ast.get_source_segment(source, selected[name]).encode('utf-8'))
            for name in names}


def main() -> None:
    old_bytes = OLD.read_bytes()
    current_bytes = CURRENT.read_bytes()
    prior_bytes = PRIOR_PROOF.read_bytes()
    if digest(old_bytes) != OLD_SHA or digest(prior_bytes) != PRIOR_PROOF_SHA:
        raise RuntimeError('Historical launcher or source proof changed')
    prior = json.loads(prior_bytes)
    if prior['status'] != 'pass' or prior['current_launcher']['sha256'] != OLD_SHA:
        raise RuntimeError('Historical source proof does not bind the old launcher')
    installed = prior['installed_kas']
    if digest(Path(installed['path']).read_bytes()) != installed['sha256']:
        raise RuntimeError('Installed Kiro runtime source changed')

    expected = old_bytes
    changes = []
    for before, after in REPLACEMENTS:
        before_bytes, after_bytes = before.encode('utf-8'), after.encode('utf-8')
        if expected.count(before_bytes) != 1 or current_bytes.count(after_bytes) != 1:
            raise RuntimeError(f'Model replacement is missing or ambiguous: {before}')
        expected = expected.replace(before_bytes, after_bytes)
        changes.append({'before': before, 'after': after})
    if current_bytes != expected:
        raise RuntimeError('Launcher has an unapproved source change')

    old_source = old_bytes.decode('utf-8')
    current_source = current_bytes.decode('utf-8')
    ast.parse(old_source)
    ast.parse(current_source)
    protected_names = ('configure', 'sys_python', '_neutral_command_allowed',
                       'guard', 'LocalACPClient')
    protected_old = source_node_hashes(old_source, protected_names)
    protected_current = source_node_hashes(current_source, protected_names)
    if protected_old != protected_current:
        raise RuntimeError('Guard, hook configuration, or ACP client source changed')

    proof = {
        'kind': 'NativeHookModelSwitchSourceEquivalence',
        'status': 'pass',
        'old_launcher': {'path': OLD.relative_to(ROOT).as_posix(), 'sha256': OLD_SHA},
        'prior_source_proof': {'path': PRIOR_PROOF.relative_to(ROOT).as_posix(),
                               'sha256': PRIOR_PROOF_SHA},
        'current_launcher': {'path': CURRENT.relative_to(ROOT).as_posix(),
                             'sha256': digest(current_bytes)},
        'allowlisted_changes': changes,
        'source_equivalent_after_allowlisted_replacements': True,
        'protected_source_hashes_equal': True,
        'protected_source_hashes': protected_current,
        'installed_kas': installed,
        'scope': ('Exact launcher byte equality after five allowlisted model or diagnostic replacements. '
                  'The generated permission and hook configuration, guard, ACP client, and other source '
                  'are unchanged. The earlier five native canary probes and inspected runtime source '
                  'remain the evidence for observed native output semantics; this is not an OS sandbox '
                  'or cryptographic runtime attestation.'),
    }
    encoded = (json.dumps(proof, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if OUTPUT.exists() and OUTPUT.read_bytes() != encoded:
        raise RuntimeError('Existing immutable Opus source proof would change')
    if not OUTPUT.exists():
        OUTPUT.write_bytes(encoded)
    print(json.dumps({'status': 'pass', 'launcher_sha256': digest(current_bytes),
                      'proof_sha256': digest(encoded), 'output': str(OUTPUT)}, indent=2))


if __name__ == '__main__':
    main()
