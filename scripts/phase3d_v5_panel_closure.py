"""Standalone V5 panel domain closure over the unchanged V2 stage reader (PHASE3D_V5_PLAN.md 3.5).

``phase3d_v2_rollup_report._stage`` replays:

- every saved comparator verdict and the complete pixel matrix;
- function;
- the ordinary final raw review receipt (``realistic_review`` final);
- the current source review with its native trace.

For one call, this module changes only the following, all fixed before the V5
panel run:

- **Suite check.** It accepts the V5 panel recipe matrix
  (``phase3d_v5_prepare.recipes``) and the frozen ``realistic-behavior-v1``
  window rule (V5 has no retest allowance).
- **Policy rebuild.** The policy is rebuilt from the attested calibration
  review and must equal the frozen policy.
- **Native policy.** The reader runs inside the exact Kiro CLI 2.27.1 release
  identity mapping and the V5 prevented-denial policy
  (``phase3d_v5_native_denial``). When the run took place with the Kiro power
  removed, it also runs inside the receipted one-server environment
  (``phase3d_v4_native_environment`` with the V5 window receipt).

This closure covers the panel domain only. It does not change Session
acceptance, historical results or any threshold.
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

VERSION = 'v5-standalone-panel-domain-closure-v1'
ROOT = Path(__file__).resolve().parents[1]
MANDATORY = tuple(key for key in base.AXES if key != 'improvement')
STAGE_PATHS = {'session', 'source_review', 'final_raw_review'}
CALIBRATION_PATHS = {'preparation', 'review'}


def _need(value, message, code='invalid_panel_closure'):
    if not value:
        raise VisualError(code, message)


def _suite_v5(inputs, path, stage):
    """The V2 suite check with the V5 panel recipes and the realistic-behavior-v1 window rule."""
    from kiroclops.local_engine import media_suite, realistic_policy
    from kiroclops.local_engine.execution_policy import REVISED_ID, validate_policy
    from scripts.phase3d_v5_prepare import GOAL, recipes
    _need(stage == 'panel', 'This V5 reader closes the panel stage only.', 'wrong_stage')
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
              'The V5 calibration, regression and new-held-out matrix is incomplete.', 'incomplete_matrix')
    base.need(len(suite['required_cases']) == len(required)
              and {(c['case_id'], c['role']) for c in suite['required_cases']} == required,
              'Required cases differ.', 'incomplete_matrix')
    base.need(suite.get('goal') in (None, GOAL), 'The suite carries another session goal.', 'suite_changed')
    sources = media_suite.analysis_source_hashes(2)
    base.need(suite.get('analysis_source_hashes') == sources and suite.get('source_epoch') == digest(sources),
              'The prospective source epoch is stale.', 'analysis_source_changed')
    for relative, sha in sources.items():
        inputs.bind(safe_path(media_suite.ROOT, relative), sha)
    policy = realistic_policy.validate_policy(suite.get('acceptance_policy'), domain=stage)
    base.need(suite.get('acceptance_profile') == policy['id'] == realistic_policy.POLICY_ID,
              'The panel suite must carry the frozen realistic-behavior-v1 window rule.')
    return suite


def _policy_rebuild(inputs, calibration, suite):
    """The V1 window rule comes only from the attested calibration review."""
    from kiroclops.local_engine import realistic_policy
    from kiroclops.local_engine.realistic_review import calibration_review
    from scripts.phase3d_v2_prepare import verify_calibration_review
    verified = verify_calibration_review(calibration['preparation'], calibration['review'])
    inputs.bind(Path(calibration['preparation']) / 'calibration.json')
    inputs.bind(Path(calibration['preparation']) / 'start-binding.json')
    inputs.raw_receipt(calibration['review'])
    rebuilt = realistic_policy.build_policy('panel', calibration_review(calibration['review']))
    _need(rebuilt == suite['acceptance_policy'], 'The frozen window rule belongs to another calibration review.',
          'wrong_policy')
    return {'status': 'pass', 'numeric_status': verified['numeric_status'], 'floors_px': rebuilt['floors_px'],
            'policy_sha256': rebuilt['policy_sha256'], 'final_behavior_granted': False}


@contextmanager
def _reader(native, session):
    original = base._suite
    base._suite = _suite_v5
    try:
        if native.get('environment_receipt'):
            from scripts import phase3d_v4_native_environment as environment
            scope = environment.environment_policy([session], receipt=_ordinary(native['environment_receipt']),
                                                   receipt_sha256=native['environment_receipt_sha256'])
        else:
            scope = nullcontext()
        with scope, denial.denial_policy() as ledger, identity.release_identity_policy(root=ROOT) as applied:
            yield applied, ledger
    finally:
        base._suite = original


def _config(value):
    _need(isinstance(value, dict) and set(value) == {'kind', 'schema_version', 'stage', 'calibration', 'native'}
          and value['kind'] == 'Phase3DV5PanelClosureInputs' and value['schema_version'] == 1,
          'Supply the explicit V5 panel closure input schema.')
    spec, calibration, native = value['stage'], value['calibration'], value['native']
    _need(isinstance(spec, dict) and set(spec) == {'stage'} | STAGE_PATHS and spec['stage'] == 'panel'
          and all(isinstance(spec[k], str) and spec[k] for k in STAGE_PATHS),
          'The panel stage needs its session, source review and attached final raw review receipt.')
    _need(isinstance(calibration, dict) and set(calibration) == CALIBRATION_PATHS
          and all(isinstance(v, str) and v for v in calibration.values()),
          'Name the calibration preparation and its attested review.')
    _need(isinstance(native, dict) and set(native) == {'environment_receipt', 'environment_receipt_sha256'}
          and ((native['environment_receipt'] is None and native['environment_receipt_sha256'] is None)
               or (isinstance(native['environment_receipt'], str) and isinstance(native['environment_receipt_sha256'], str)
                   and len(native['environment_receipt_sha256']) == 64)),
          'Name the power-removal window receipt and its sha256, or null for both.')
    return deepcopy(value)


def _derive(inputs, config):
    spec = config['stage']
    directory = _ordinary(spec['session'])
    with _reader(config['native'], directory) as (applied, ledger):
        row = base._stage(inputs, spec)
    _need(row.get('status') != 'invalid' and row.get('complete_matrix_verified') is True,
          'The canonical panel stage must have a complete, valid terminal matrix: '
          + json.dumps(row.get('errors', [])), 'invalid_canonical_stage')
    state = Session(directory)._load()
    _need(state.get('status') == 'finalized' and state.get('active_job_id') is None,
          'Wait for terminal recording and model work.', 'stage_not_terminal')
    suite = inputs.read(_ordinary(state['reference']) / 'suite.json')
    row['calibration_review'] = _policy_rebuild(inputs, config['calibration'], suite)
    statuses = [row['axes'][name]['status'] for name in MANDATORY]
    row['status'] = 'pass' if all(s == 'pass' for s in statuses) else 'fail' if 'fail' in statuses else 'pending'
    return {'kind': 'V5StandalonePanelDomainClosure', 'schema_version': 1, 'version': VERSION,
            'domain': 'panel', 'status': row['status'], 'domain_behavior_acceptance': row['status'] == 'pass',
            'stage': row, 'release_identity_mappings': applied,
            'prevented_denials_set_aside': ledger, 'native_environment': deepcopy(config['native']),
            'source_epoch': suite['source_epoch'], 'acceptance_profile': suite['acceptance_profile'],
            'acceptance_policy_sha256': suite['acceptance_policy']['policy_sha256'],
            'completed_current_authorship_verified': base._verified_current_authorship(row),
            'canonical_session_acceptance_changed': False, 'historical_results_changed': False,
            'full_phase_acceptance': False, 'recording_or_model_started': False,
            'comparison_thresholds_recomputed': False,
            'scope': 'Only the V5 panel domain in its V5 source epoch; no overall phase acceptance.'}


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
    write_json(output / 'receipt.json', {'kind': 'V5StandalonePanelDomainClosureReceipt', 'schema_version': 1,
                                         'version': VERSION, 'config': str(config_path),
                                         'config_sha256': file_sha256(config_path), 'closure_sha256': digest(value),
                                         'consumed_files': inputs.files, 'consumed_files_sha256': digest(inputs.files)})
    return value


def validate(output):
    output = _ordinary(output)
    value, receipt = read_json(output / 'closure.json'), read_json(output / 'receipt.json')
    _need(value.get('kind') == 'V5StandalonePanelDomainClosure' and value.get('version') == VERSION
          and receipt.get('kind') == 'V5StandalonePanelDomainClosureReceipt' and receipt.get('version') == VERSION
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
