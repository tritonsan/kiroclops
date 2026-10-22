"""Bind the diagnostic launcher to unchanged ACP transport; no live model call.

Guard/bootstrap and report behavior changed. This is deliberately narrower than
whole-launcher equivalence and never substitutes for native decision evidence.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'artifacts/phase3a/acceptance-reporting-01'
FROZEN = BASE / 'launcher-before.py'
CURRENT = ROOT / 'kiroclops/local_engine/launcher.py'
GUARD = ROOT / 'kiroclops/local_engine/guard_diagnostics.py'
CLIENT = ROOT / 'kiroclops/portable_v2/launcher.py'
PRIOR = ROOT / 'artifacts/phase3a/native-hook-opus55-model-source-proof.json'
RUNTIME = ROOT / 'artifacts/phase3a/native-hook-sol24-installed-source-proof.json'
OUTPUT = BASE / 'native-transport-proof.json'
PINNED = {
    FROZEN: 'bf20ded67d2d527c1fec4c79e3a48b8d563fcbae45018a88b767e051d7bf8bcc',
    CURRENT: '07431f33111a78c8fc54ce5ff68526f0e87b881b67be1eb134d45e29f5a1f592',
    GUARD: '6c85b3adf2958b9cc8c52dc7fdcb81f391a9f594e063748e11193940ca13d2bc',
    CLIENT: '63f64b50790876af6fbb1eac504426dba905ab3a31c329a939c2acc0d42a4e45',
    PRIOR: 'd95681ef08b7c39aaa733ff1cbc26631160de7ef30743c23c88b3c19dfbad6b3',
    RUNTIME: 'fd350131186aefaafe13e925888461e09fc659a9297616b74341b8fb4cafd10a',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def binding(path):
    return {'path': path.relative_to(ROOT).as_posix(), 'sha256': sha(path.read_bytes())}


def sections(text):
    wanted = {'LocalACPClient', '_run_native', 'MODEL', 'APPROVAL'}
    result = {}
    for node in ast.parse(text).body:
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign):
            names = [n.id for n in ast.walk(node.targets[0]) if isinstance(n, ast.Name)]
            name = next((n for n in names if n in wanted), None)
        if name in wanted:
            result[name] = ast.get_source_segment(text, node)
    if set(result) != wanted:
        raise RuntimeError('Missing pinned transport section')
    return result


def build_proof():
    for path, expected in PINNED.items():
        if sha(path.read_bytes()) != expected:
            raise RuntimeError(f'Pinned source changed: {path.name}')
    before = sections(FROZEN.read_text('utf8'))
    after = sections(CURRENT.read_text('utf8'))
    if before != after:
        raise RuntimeError('Native client, transport or model/approval changed')
    prior = json.loads(PRIOR.read_bytes())
    runtime = json.loads(RUNTIME.read_bytes())
    if prior['current_local_launcher']['sha256'] != PINNED[FROZEN]:
        raise RuntimeError('Historical source proof mismatch')
    if (runtime['status'] != 'pass' or not all(runtime['source_checks'].values())
            or sha(Path(runtime['source']).read_bytes()) != runtime['source_sha256']):
        raise RuntimeError('Inspected installed runtime source changed')
    return {
        'kind': 'NativeDiagnosticTransportCompatibility', 'status': 'pass',
        'frozen_local_launcher': binding(FROZEN), 'current_local_launcher': binding(CURRENT),
        'guard_bootstrap': binding(GUARD), 'current_portable_client': binding(CLIENT),
        'prior_source_proof': binding(PRIOR), 'installed_runtime_source_proof': binding(RUNTIME),
        'unchanged_sections': {name: sha(value.encode('utf8')) for name, value in before.items()},
        'cli_version': 'kiro-cli-chat ' + runtime['cli_version'], 'kas_version': runtime['kas_version'],
        'installed_source_sha256': runtime['source_sha256'], 'live_model_run': False,
        'scope': ('Exact source sections for ACP client, native transport, model/effort and approval '
                  'are unchanged. Guard diagnostics, configuration bindings and post-turn report handling '
                  'changed and are tested separately. This source compatibility evidence qualifies '
                  'native output decoding only; it is not whole-guard equivalence, a live canary, '
                  'or permission to replace missing canonical decision rows with diagnostic receipts.'),
    }


if __name__ == '__main__':
    proof = build_proof()
    encoded = (json.dumps(proof, indent=2) + '\n').encode('utf8')
    if OUTPUT.exists() and OUTPUT.read_bytes() != encoded:
        raise RuntimeError('Existing proof would change')
    if not OUTPUT.exists():
        OUTPUT.write_bytes(encoded)
    print(json.dumps({'status': 'pass', 'proof_sha256': sha(encoded), 'output': str(OUTPUT)}))
