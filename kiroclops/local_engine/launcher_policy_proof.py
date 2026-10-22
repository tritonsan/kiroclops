"""Exact static extension of the source-pinned Kiro 2.27 native output policy.

This does not authorize a tool, certify a native run, or waive an output error.
It binds one inspected launcher revision to unchanged native guard/permission
paths. Every actual guard decision and output envelope remains required.
"""
from __future__ import annotations

import ast
from pathlib import Path

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = 'reviews/staged-native227v3/v2-baseline/launcher.py'
ACTIVE = 'kiroclops/local_engine/launcher.py'
OUTPUT = 'artifacts/phase3d/launcher-execution-v2b/source-proof.json'
SESSION_ARCHIVE = 'reviews/staged-native227v3/execution-v2-baseline/session.py'
BASE_LAUNCHER_SHA256 = '15222b46591070ff18ed352a21bb7f13e0a1c240418a59ccb25386709d8403ac'
ACTIVE_LAUNCHER_SHA256 = '628ec724f7dc0d7aeba8f04593e62e969a83617f21419be63d126a0123f4cd1f'
KAS_SOURCE_SHA256 = '79a1a743ee7236a71bba9c6c68342deccfcffea4d61361eae0254288339b19c8'
SOURCE_PINS = {
    ARCHIVE: BASE_LAUNCHER_SHA256,
    ACTIVE: ACTIVE_LAUNCHER_SHA256,
    'kiroclops/local_engine/guard_diagnostics.py': '6c85b3adf2958b9cc8c52dc7fdcb81f391a9f594e063748e11193940ca13d2bc',
    'kiroclops/portable_v2/launcher.py': '63f64b50790876af6fbb1eac504426dba905ab3a31c329a939c2acc0d42a4e45',
    'kiroclops/integration/model_cli_max_v1.py': '2d74c4833d255b1eafa42a0fc4a1347bf170fc7afae1e43bd1cbb30389900db6',
    'kiroclops/integration/kiro.py': 'a428bbd1d25bc9e3862ba4cbf48cf52f16e5e00e417f22dfeb348c47ceb13254',
    'kiroclops/local_engine/execution_policy.py': '9ac050f4376c102dd26b6c44ab9ec802efaf1752672226eef52fa5f1abeb0db8',
    'kiroclops/local_engine/session.py': '330ff0abd2ef86519beedb8a76d669fdbe594d4bf5718f03cb8392f12c8b5aad',
    SESSION_ARCHIVE: 'd7c8c6107b20c2e3a60daf7de017617a6ebdf1b09eb6bbd0d36f85a3bb3c0be0',
    'kiroclops/local_engine/stage_closure.py': '5a3796aa7d06353bceb1f2e1a6fefada19e6b6722f01ca307d32989043a69bde',
    'reviews/staged-native227v3/source-proof-v3.json': '4b5cad90bdbf80ee4a386ab2e3a70342a5c4322a8ca2b2d9a44ea62508bab2ed',
    'reviews/staged-native227v3/v2-baseline/source-proof.json': '1ff376e4fb2709621d3014d2557c96f481003f74998d2d305ddef262a5678db1',
    'reviews/staged-native227v3/v2-baseline/archive.json': 'a700c0da12c97ba489c79528187a4db500681055fb31666303301b91876f1b65',
}
CHANGED_DEFINITIONS = ('LocalACPClient', '_run_native', 'configure', 'load_config')
CLASS_CHANGED_METHODS = ('__init__', 'prompt')
CLASS_ADDED_METHODS = ('_receive',)


def _need(value, message):
    if not value:
        raise VisualError('launcher_policy_extension_unproven', message)


def _ast(value):
    return ast.dump(value, include_attributes=False)


def _defs(tree):
    return {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}


def _remove_observer_assignment(method):
    value = ast.parse(ast.unparse(method)).body[0]
    expected = _ast(ast.parse('self.passive_activity = PassiveNativeActivity()').body[0])
    found = [node for node in value.body if _ast(node) == expected]
    _need(len(found) == 1, 'The passive observer assignment is not the exact inspected statement.')
    value.body = [node for node in value.body if _ast(node) != expected]
    return value


def _scope_statements(definition):
    names = {'server', 'tools', 'rules', 'write_patterns'}
    statements = []
    for node in ast.walk(definition):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        if any(isinstance(target, ast.Name) and target.id in names for target in targets):
            statements.append(_ast(node))
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Attribute) and isinstance(node.value.func.value, ast.Name)
              and node.value.func.value.id == 'rules'):
            statements.append(_ast(node))
    return statements


def inspect_ast_extension(archive_bytes, active_bytes):
    """Check the exact changed set plus unchanged guards; no execution/import."""
    old, new = (ast.parse(value.decode('utf-8-sig')) for value in (archive_bytes, active_bytes))
    a, b = _defs(old), _defs(new)
    _need(set(a) == set(b), 'Native launcher top-level definitions were added or removed.')
    changed = tuple(sorted(name for name in a if _ast(a[name]) != _ast(b[name])))
    _need(changed == CHANGED_DEFINITIONS, 'Native launcher changed outside the inspected definition allowlist.')
    ca, cb = _defs(a['LocalACPClient']), _defs(b['LocalACPClient'])
    _need(tuple(sorted(set(cb) - set(ca))) == CLASS_ADDED_METHODS and not set(ca) - set(cb),
          'Native client methods changed outside the observer addition.')
    _need(tuple(sorted(name for name in ca if _ast(ca[name]) != _ast(cb[name]))) == CLASS_CHANGED_METHODS,
          'Native client methods changed outside passive observer construction/reset.')
    for name in CLASS_CHANGED_METHODS:
        _need(_ast(ca[name]) == _ast(_remove_observer_assignment(cb[name])),
              'Native client construction or prompt changed beyond its passive observer.')
    extra_import = _ast(ast.parse('from .execution_policy import policy_from_state, PassiveNativeActivity, REVISED_ID').body[0])
    old_remainder = [_ast(node) for node in old.body if not isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    new_remainder = [_ast(node) for node in new.body if not isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    _need(new_remainder.count(extra_import) == 1, 'Exact frozen execution-policy import is missing.')
    new_remainder.remove(extra_import)
    _need(new_remainder == old_remainder, 'Launcher imports, constants or top-level behavior changed unexpectedly.')
    scope = _scope_statements(a['configure'])
    _need(scope == _scope_statements(b['configure']), 'Native server/tool/permission scope generation changed.')
    protected = {name: digest(_ast(a[name])) for name in a if name not in CHANGED_DEFINITIONS}
    protected.update({'LocalACPClient.' + name: digest(_ast(ca[name]))
                      for name in ca if name not in CLASS_CHANGED_METHODS})
    # Native approval and model/MCP/agent verification cannot be waived.
    _need(all(name in protected for name in ('guard', 'LocalACPClient.handshake', 'LocalACPClient._permission_reply')),
          'Required native boundaries are absent.')
    return {'changed_definitions': list(changed), 'added_definitions': [], 'removed_definitions': [],
            'changed_client_methods': list(CLASS_CHANGED_METHODS), 'added_client_methods': list(CLASS_ADDED_METHODS),
            'protected_definition_ast_sha256': protected, 'unchanged_top_level_ast_sha256': digest(old_remainder),
            'unchanged_permission_scope_ast_sha256': digest(scope),
            'active_changed_definition_ast_sha256': {name: digest(_ast(b[name])) for name in CHANGED_DEFINITIONS}}


def inspect_session_closure(archive_bytes, active_bytes):
    """Bind only the three fail-closed hooks; existing source/job guards stay exact."""
    old, new = (ast.parse(value.decode('utf-8-sig')) for value in (archive_bytes, active_bytes))
    a, b = _defs(old), _defs(new)
    _need(set(a) == set(b), 'Session definitions were added or removed.')
    changed = sorted(name for name in a if _ast(a[name]) != _ast(b[name]))
    _need(changed == ['Session', 'run_session_job'], 'Session changed outside the fatal closure hooks.')
    ca, cb = _defs(a['Session']), _defs(b['Session'])
    _need(set(ca) == set(cb), 'Session methods were added or removed.')
    methods = sorted(name for name in ca if _ast(ca[name]) != _ast(cb[name]))
    _need(methods == ['inspect', 'submit'], 'Session source/job/cancel guards changed.')
    remainder = lambda tree: [_ast(n) for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    _need(remainder(old) == remainder(new), 'Session top-level imports/constants changed.')
    protected = {name: digest(_ast(a[name])) for name in a if name not in changed}
    protected.update({'Session.' + name: digest(_ast(ca[name])) for name in ca if name not in methods})
    _need(all(name in protected for name in ('create_session', 'Session._verify_sources',
        'Session._reconcile_active', 'Session._reconcile_job', 'Session.cancel_job', 'Session.get_job')),
        'Required Session source/reconciliation/cancellation boundaries are missing.')
    return {'changed_definitions': changed, 'changed_session_methods': methods,
        'protected_definition_ast_sha256': protected,
        'unchanged_top_level_ast_sha256': digest(remainder(old)),
        'active_hook_ast_sha256': {'Session.' + name: digest(_ast(cb[name])) for name in methods}
            | {'run_session_job': digest(_ast(b['run_session_job']))},
        'scope': 'Read-only fatal input-history inspection; block submission and queued execution. No new permission, candidate, clock reset, repair or behavioral acceptance.'}


def build_proof(root=None):
    root = Path(root or ROOT)
    for relative, expected in SOURCE_PINS.items():
        _need(file_sha256(root / relative) == expected, 'Pinned launcher or guard dependency changed: ' + relative)
    old = (root / ARCHIVE).read_bytes()
    active = (root / ACTIVE).read_bytes()
    extension = inspect_ast_extension(old, active)
    session_extension = inspect_session_closure((root / SESSION_ARCHIVE).read_bytes(),
        (root / 'kiroclops/local_engine/session.py').read_bytes())
    v3 = read_json(root / 'reviews/staged-native227v3/source-proof-v3.json')
    _need(v3.get('status') == 'pass' and v3.get('policy_version') == 3
          and v3.get('source_sha256') == KAS_SOURCE_SHA256,
          'Pinned 2.27 native output proof differs.')
    _need(file_sha256(v3['source']) == KAS_SOURCE_SHA256, 'Installed native producer source changed.')
    return {'kind': 'PinnedLauncherExecutionPolicyExtension', 'schema_version': 1, 'status': 'pass',
            'cli_version': 'kiro-cli-chat 2.27.0', 'kas_version': '0.66.22',
            'base_launcher_sha256': BASE_LAUNCHER_SHA256, 'launcher_sha256': ACTIVE_LAUNCHER_SHA256,
            'kas_source_sha256': KAS_SOURCE_SHA256, 'base_output_proof_sha256': SOURCE_PINS['reviews/staged-native227v3/source-proof-v3.json'],
            'source_pins': dict(SOURCE_PINS), 'proof_helper_sha256': file_sha256(__file__), 'ast_inspection': extension,
            'session_closure_ast_inspection': session_extension,
            'inspected_changes': {
                'configure': 'Frozen duration text and execution-policy metadata; agent permissions/tool boundaries unchanged.',
                'load_config': 'Additional Session binding and execution-policy equality checks; no permissions relaxed.',
                '_run_native': 'Deadline from bound policy plus passive progress and timeout classification; one task prompt remains.',
                'LocalACPClient': 'Passive observer creation/reset and observation after the inherited event reader; handshake/approval unchanged.'},
            'native_canary': False, 'model_started': False, 'tool_authorization_granted': False,
            'behavior_acceptance_granted': False, 'historical_receipts_rewritten': False,
            'scope': 'One exact launcher revision and native producer only. Actual canonical guard decisions, tool cardinality, source edits and output envelopes remain mandatory. No version alias, model fallback or runtime signature.'}


def verify_extension(context, *, root=None):
    """Return the current static receipt only for the exact release context."""
    if not isinstance(context, dict) or any(context.get(key) != expected for key, expected in
            {'cli_version': 'kiro-cli-chat 2.27.0', 'kas_version': '0.66.22', 'launcher_sha256': ACTIVE_LAUNCHER_SHA256}.items()):
        return None
    root = Path(root or ROOT)
    try:
        saved = read_json(root / OUTPUT)
        _need(saved == build_proof(root), 'Launcher extension receipt does not match current pinned sources.')
        return saved
    except (VisualError, OSError, ValueError, TypeError, KeyError, SyntaxError):
        return None
