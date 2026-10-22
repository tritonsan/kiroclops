"""Standalone V5b panel domain closure over the unchanged V2 stage reader (PHASE3D_V5B_PLAN.md 2.5).

``phase3d_v2_rollup_report._stage`` replays:

- every saved comparator verdict and the complete pixel matrix;
- function;
- the ordinary final raw review receipt (``realistic_review`` final);
- the current source review with its native trace.

This is the V5 panel closure (``phase3d_v5_panel_closure``) with only these
changes, all fixed before the V5b panel run:

- **Suite check.** It accepts the V5b recipe matrix (``phase3d_v5b_prepare.recipes``:
  16 calibration, 44 regression and 12 validation cases), the V5b session goal and
  the frozen ``realistic-behavior-v1`` window rule under the unchanged
  ``phase3d-execution-v2`` policy.
- **Native policy.** On top of the V5 stack (2.27.1 release identity mapping,
  prevented-denial policy, optional receipted one-server environment), the V5b
  offloaded shell output policy (``phase3d_v5b_native_offload``) verifies every
  preserved offload listed in the named manifest.

The policy rebuild from the attested calibration review is unchanged. This closure
covers the panel domain only. It does not change Session acceptance, historical
results or any threshold.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.session import Session, _ordinary
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts import phase3d_v2_native_release_identity as identity
from scripts import phase3d_v2_rollup_report as base
from scripts import phase3d_v5_native_denial as denial
from scripts import phase3d_v5_panel_closure as v5

VERSION = 'v5b-standalone-panel-domain-closure-v1'
ROOT = Path(__file__).resolve().parents[1]
MANDATORY = v5.MANDATORY
STAGE_PATHS = v5.STAGE_PATHS
CALIBRATION_PATHS = v5.CALIBRATION_PATHS
NATIVE_KEYS = {'environment_receipt', 'environment_receipt_sha256', 'offloads', 'offloads_sha256'}


def _need(value, message, code='invalid_panel_closure'):
    if not value:
        raise VisualError(code, message)


def _suite_v5b(inputs, path, stage):
    """The V2 suite check with the V5b panel recipes and the realistic-behavior-v1 window rule."""
    from kiroclops.local_engine import media_suite, realistic_policy
    from kiroclops.local_engine.execution_policy import REVISED_ID, validate_policy
    from scripts.phase3d_v5b_prepare import GOAL, recipes
    _need(stage == 'panel', 'This V5b reader closes the panel stage only.', 'wrong_stage')
    path = _ordinary(path)
    suite = inputs.read(path / 'suite.json')
    binding = inputs.read(path / 'suite-binding.json')
    base.need(suite.get('kind') == 'ReferenceMediaSuite' and suite.get('schema_version') == 2
              and suite.get('stage') == stage and suite.get('calibration_frozen') is True
              and binding.get('suite_sha256') == digest(suite), 'Frozen prospective suite identity differs.', 'suite_changed')
    execution = validate_policy(suite.get('execution_policy'), stage=stage)
    base.need(execution['policy_id'] == REVISED_ID, 'A historical execution policy cannot become a prospective success.')
    expected = recipes()
    cases = suite.get('cases', [])
    required = {(c['case_id'], c['role']) for c in expected}
    actual = [(c.get('case_id'), c.get('role')) for c in cases]
    lookup = {(c['case_id'], c['role']): c for c in expected}
    base.need(len(actual) == len(required) and set(actual) == required
              and all(c['scenario'] == lookup[c['case_id'], c['role']]['scenario']
                      and c['stage'] == lookup[c['case_id'], c['role']]['stage']
                      and c['purpose'] == lookup[c['case_id'], c['role']]['purpose'] for c in cases),
              'The V5b calibration, regression and new-held-out matrix is incomplete.', 'incomplete_matrix')
    base.need(len(suite['required_cases']) == len(required)
              and {(c['case_id'], c['role']) for c in suite['required_cases']} == required,
              'Required cases differ.', 'incomplete_matrix')
    base.need(suite.get('goal') == GOAL, 'The suite carries another session goal.', 'suite_changed')
    sources = media_suite.analysis_source_hashes(2)
    base.need(suite.get('analysis_source_hashes') == sources and suite.get('source_epoch') == digest(sources),
              'The prospective source epoch is stale.', 'analysis_source_changed')
    for relative, sha in sources.items():
        inputs.bind(safe_path(media_suite.ROOT, relative), sha)
    policy = realistic_policy.validate_policy(suite.get('acceptance_policy'), domain=stage)
    base.need(suite.get('acceptance_profile') == policy['id'] == realistic_policy.POLICY_ID,
              'The panel suite must carry the frozen realistic-behavior-v1 window rule.')
    return suite


@contextmanager
def _reader(native, session):
    from scripts import phase3d_v5b_native_offload as offload
    original = base._suite
    base._suite = _suite_v5b
    try:
        if native.get('environment_receipt'):
            from scripts import phase3d_v4_native_environment as environment
            scope = environment.environment_policy([session], receipt=_ordinary(native['environment_receipt']),
                                                   receipt_sha256=native['environment_receipt_sha256'])
        else:
            scope = nullcontext()
        extension = (offload.offload_policy(_ordinary(native['offloads']), native['offloads_sha256'])
                     if native.get('offloads') else nullcontext([]))
        with scope, denial.denial_policy() as ledger, extension as verified, \
                identity.release_identity_policy(root=ROOT) as applied:
            yield applied, ledger, verified
    finally:
        base._suite = original


def _pair(native, first, second):
    return ((native[first] is None and native[second] is None)
            or (isinstance(native[first], str) and isinstance(native[second], str) and len(native[second]) == 64))


def _config(value):
    _need(isinstance(value, dict) and set(value) == {'kind', 'schema_version', 'stage', 'calibration', 'native'}
          and value['kind'] == 'Phase3DV5bPanelClosureInputs' and value['schema_version'] == 1,
          'Supply the explicit V5b panel closure input schema.')
    spec, calibration, native = value['stage'], value['calibration'], value['native']
    _need(isinstance(spec, dict) and set(spec) == {'stage'} | STAGE_PATHS and spec['stage'] == 'panel'
          and all(isinstance(spec[k], str) and spec[k] for k in STAGE_PATHS),
          'The panel stage needs its session, source review and attached final raw review receipt.')
    _need(isinstance(calibration, dict) and set(calibration) == CALIBRATION_PATHS
          and all(isinstance(v, str) and v for v in calibration.values()),
          'Name the calibration preparation and its attested review.')
    _need(isinstance(native, dict) and set(native) == NATIVE_KEYS
          and _pair(native, 'environment_receipt', 'environment_receipt_sha256')
          and _pair(native, 'offloads', 'offloads_sha256'),
          'Name the power-removal window receipt and the offload manifest with their sha256, or null for each pair.')
    return deepcopy(value)


def _derive(inputs, config):
    spec = config['stage']
    directory = _ordinary(spec['session'])
    with _reader(config['native'], directory) as (applied, ledger, verified):
        row = base._stage(inputs, spec)
    _need(row.get('status') != 'invalid' and row.get('complete_matrix_verified') is True,
          'The canonical panel stage must have a complete, valid terminal matrix: '
          + json.dumps(row.get('errors', [])), 'invalid_canonical_stage')
    state = Session(directory)._load()
    _need(state.get('status') == 'finalized' and state.get('active_job_id') is None,
          'Wait for terminal recording and model work.', 'stage_not_terminal')
    suite = inputs.read(_ordinary(state['reference']) / 'suite.json')
    row['calibration_review'] = v5._policy_rebuild(inputs, config['calibration'], suite)
    statuses = [row['axes'][name]['status'] for name in MANDATORY]
    row['status'] = 'pass' if all(s == 'pass' for s in statuses) else 'fail' if 'fail' in statuses else 'pending'
    return {'kind': 'V5bStandalonePanelDomainClosure', 'schema_version': 1, 'version': VERSION,
            'domain': 'panel', 'status': row['status'], 'domain_behavior_acceptance': row['status'] == 'pass',
            'stage': row, 'release_identity_mappings': applied,
            'prevented_denials_set_aside': ledger, 'offloaded_outputs_verified': verified,
            'native_environment': deepcopy(config['native']),
            'source_epoch': suite['source_epoch'], 'acceptance_profile': suite['acceptance_profile'],
            'acceptance_policy_sha256': suite['acceptance_policy']['policy_sha256'],
            'completed_current_authorship_verified': base._verified_current_authorship(row),
            'canonical_session_acceptance_changed': False, 'historical_results_changed': False,
            'full_phase_acceptance': False, 'recording_or_model_started': False,
            'comparison_thresholds_recomputed': False,
            'scope': 'Only the V5b panel domain in its V5b source epoch; no overall phase acceptance.'}


def build(config_path, output):
    config_path, output = _ordinary(config_path), _ordinary(output)
    _need(not output.exists(), 'Preserve earlier closures; use a fresh output.', 'output_exists')
    inputs = base.Inputs()
    config = _config(inputs.read(config_path))
    inputs.code(__file__)
    value = _derive(inputs, config)
    inputs.stable()
    output.mkdir(parents=True)
    write_json(output / 'closure.json', value)
    write_json(output / 'summary.json', {k: value[k] for k in ('kind', 'version', 'status', 'domain_behavior_acceptance',
                                                               'source_epoch', 'acceptance_profile')} | {
        'axes': {name: axis['status'] for name, axis in value['stage']['axes'].items()}})
    write_json(output / 'receipt.json', {'kind': 'V5bStandalonePanelDomainClosureReceipt', 'schema_version': 1,
                                         'version': VERSION, 'config': str(config_path),
                                         'config_sha256': file_sha256(config_path), 'closure_sha256': digest(value),
                                         'consumed_files': inputs.files, 'consumed_files_sha256': digest(inputs.files)})
    return value


def validate(output):
    output = _ordinary(output)
    value, receipt = read_json(output / 'closure.json'), read_json(output / 'receipt.json')
    _need(value.get('kind') == 'V5bStandalonePanelDomainClosure' and value.get('version') == VERSION
          and receipt.get('kind') == 'V5bStandalonePanelDomainClosureReceipt' and receipt.get('version') == VERSION
          and digest(value) == receipt.get('closure_sha256')
          and digest(receipt.get('consumed_files')) == receipt.get('consumed_files_sha256'),
          'Closure identity differs.', 'stale_closure')
    for name, sha in receipt['consumed_files'].items():
        _need(file_sha256(name) == sha, 'Consumed evidence changed: ' + name, 'stale_closure')
    inputs = base.Inputs()
    fresh = _derive(inputs, _config(inputs.read(receipt['config'])))
    keys = [k for k in value if k != 'release_identity_mappings']
    _need({k: fresh.get(k) for k in keys} == {k: value[k] for k in keys}, 'A fresh replay differs from the saved closure.',
          'stale_closure')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('build')
    make.add_argument('--config', type=Path, required=True)
    make.add_argument('--output', type=Path, required=True)
    check = commands.add_parser('validate')
    check.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = build(args.config, args.output) if args.command == 'build' else validate(args.output)
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}))
        return 1
    print(json.dumps({'status': value['status'], 'axes': {k: v['status'] for k, v in value['stage']['axes'].items()}}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
