"""Standalone V3 ground domain closure over the unchanged V2 stage reader.

``phase3d_v2_rollup_report._stage`` replays:

- every saved comparator verdict;
- the complete pixel matrix and native measurement quality;
- executed function and wall/normal-level checks;
- the current source review with its native trace.

For one call, this module changes only the following:

- **Suite check.** It accepts the floor-aware ground profile and its frozen
  rule (V3a) instead of ``legacy-game-v1``. The recipe matrix, source epoch and
  execution policy checks are the V2 checks.
- **Native policy.** The reader runs inside the exact Kiro CLI 2.27.1 release
  identity mapping (``phase3d_v2_native_release_identity``).
- **Raw review axis.** It comes from the V3 ground final and normal-map raw
  proofs (``phase3d_v3_ground_raw``). Their custom controllers are not readable
  by the ordinary reader.
- **Session measurements, V4 profile only** (user decision, 11 October 2026,
  post hoc). The V2 native measurement check requires a worker-receipt stamp
  on every candidate record. Session jobs measure their own captures in
  process and never stamp them; v3g02 has the same gap. For the V4 speed-aware
  profile, session-produced records ("candidate", "final") therefore pass the
  exact record binding instead: package hashes, observations equal to the
  saved common analysis, and the native projection bound to the candidate
  source. They also pass the same reliability check. Suite-origin records
  ("reference", "original", "before") keep the full stamp check. Every other
  profile uses the unchanged V2 check.

This closure covers the ground domain only. It does not change Session
acceptance, historical results or any threshold.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import suite_evidence
from kiroclops.local_engine.media_suite import (GROUND_FLOOR_PROFILE, GROUND_FLOOR_RULE, GROUND_SPEED_PROFILE,
                                               GROUND_SPEED_RULE)
from kiroclops.local_engine.session import Session, _ordinary
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts import phase3d_v2_native_release_identity as identity
from scripts import phase3d_v2_rollup_report as base
from scripts import phase3d_v3_ground_raw as v3raw

VERSION = 'v3-standalone-ground-domain-closure-v2'
ROOT = Path(__file__).resolve().parents[1]
MANDATORY = tuple(key for key in base.AXES if key != 'improvement')
SPEC_PATHS = {'session', 'source_review'}
# Each frozen ground profile closes against its own rule and recipe set (V3a, V4).
PROFILE_RULES = {GROUND_FLOOR_PROFILE: GROUND_FLOOR_RULE, GROUND_SPEED_PROFILE: GROUND_SPEED_RULE}


def _expected_recipes(profile, stage):
    if profile == GROUND_SPEED_PROFILE:
        from scripts.phase3d_v4_ground import recipes
        return recipes()
    from scripts.phase3d_v2_prepare import recipes
    return recipes(stage)


def _need(value, message, code='invalid_ground_closure'):
    if not value:
        raise VisualError(code, message)


def _suite_v3(inputs, path, stage):
    """The V2 suite check with the floor-aware ground profile in place of legacy-game-v1."""
    from kiroclops.local_engine import media_suite
    from kiroclops.local_engine.execution_policy import REVISED_ID, validate_policy
    _need(stage == 'ground', 'This V3 reader closes the ground stage only.', 'wrong_stage')
    path = _ordinary(path)
    suite = inputs.read(path / 'suite.json')
    binding = inputs.read(path / 'suite-binding.json')
    base.need(suite.get('kind') == 'ReferenceMediaSuite' and suite.get('schema_version') == 2
              and suite.get('stage') == stage and suite.get('calibration_frozen') is True
              and binding.get('suite_sha256') == digest(suite), 'Frozen prospective suite identity differs.', 'suite_changed')
    execution = validate_policy(suite.get('execution_policy'), stage=stage)
    base.need(execution['policy_id'] == REVISED_ID, 'A historical execution policy cannot become a prospective success.')
    profile = suite.get('acceptance_profile')
    base.need(profile in PROFILE_RULES and suite.get('game_acceptance_rule') == PROFILE_RULES[profile],
              'The ground suite must carry a frozen floor-aware profile and its rule.')
    expected = _expected_recipes(profile, stage)
    cases = suite.get('cases', [])
    required = {(c['case_id'], c['role']) for c in expected}
    actual = [(c.get('case_id'), c.get('role')) for c in cases]
    lookup = {(c['case_id'], c['role']): c for c in expected}
    base.need(len(actual) == len(required) and set(actual) == required
              and all(c['scenario'] == lookup[c['case_id'], c['role']]['scenario']
                      and c['stage'] == lookup[c['case_id'], c['role']]['stage']
                      and c['purpose'] == lookup[c['case_id'], c['role']]['purpose'] for c in cases),
              'The required calibration, known-regression and new-held-out matrix is incomplete.', 'incomplete_matrix')
    base.need(len(suite['required_cases']) == len(required)
              and {(c['case_id'], c['role']) for c in suite['required_cases']} == required,
              'Required cases differ.', 'incomplete_matrix')
    sources = media_suite.analysis_source_hashes(2)
    base.need(suite.get('analysis_source_hashes') == sources and suite.get('source_epoch') == digest(sources),
              'The prospective source epoch is stale.', 'analysis_source_changed')
    for relative, sha in sources.items():
        inputs.bind(safe_path(media_suite.ROOT, relative), sha)
    return suite


SESSION_ROLES = ('candidate', 'final')
_V2_MEASUREMENT = base._native_measurement_quality


def _native_measurement_quality_v4(inputs, directory, suite, evaluation_job, final_job):
    """The V2 native measurement check, with session-produced records bound exactly instead of stamped (V4 only)."""
    if suite.get('acceptance_profile') != GROUND_SPEED_PROFILE:
        return _V2_MEASUREMENT(inputs, directory, suite, evaluation_job, final_job)
    from kiroclops.local_engine import suite_game, suite_revision
    lookup = {case['case_id']: case for case in suite['cases']}
    for job, roles in ((evaluation_job, ('candidate',)), (final_job, ('reference', 'original', 'before', 'final'))):
        for role in roles:
            rows = inputs.read(directory / 'measurements' / job['job_id'] / (role + '.json'))
            base.need(isinstance(rows, list) and rows, 'Native measurement role is missing.', 'incomplete_matrix')
            for record in rows:
                case = lookup[record['scenario']['case_id']]
                stamped = record.get('measurement_provenance') is not None or record.get('measurement_source_hashes') is not None
                if role in SESSION_ROLES and not stamped:
                    base.need(record.get('measurement_status') == 'ok', 'A session measurement did not qualify.',
                              'tracking_failure')
                    suite_revision._record_binding(record, case['stage'])
                else:
                    suite_revision.verify_measurement_record(record, case['stage'],
                        expected_sources=suite_revision.measurement_source_hashes(case['stage']))
                inputs.bind(record['analysis'])
                inputs.bind(Path(record['package']) / 'manifest.json')
                suite_game._reliable(suite_game._report(record), case['stage'])
    return True


@contextmanager
def _reader(environment=None, session=None):
    from contextlib import nullcontext
    original, measurement = base._suite, base._native_measurement_quality
    base._suite, base._native_measurement_quality = _suite_v3, _native_measurement_quality_v4
    try:
        if environment == 'v4-one-server':
            from scripts import phase3d_v4_native_environment as env
            scope = env.environment_policy([session])
        else:
            _need(environment is None, 'Unknown native environment mapping.')
            scope = nullcontext()
        with scope, identity.release_identity_policy(root=ROOT) as applied:
            yield applied
    finally:
        base._suite, base._native_measurement_quality = original, measurement


def _config(value):
    _need(isinstance(value, dict) and set(value) - {'environment'} == {'kind', 'schema_version', 'stage', 'final_raw', 'normal_raw'}
          and value.get('environment') in (None, 'v4-one-server')
          and value['kind'] == 'Phase3DV3GroundClosureInputs' and value['schema_version'] == 1,
          'Supply the explicit V3 ground closure input schema.')
    spec = value['stage']
    _need(isinstance(spec, dict) and set(spec) == {'stage'} | SPEC_PATHS and spec['stage'] == 'ground'
          and all(isinstance(spec[k], str) and spec[k] for k in SPEC_PATHS),
          'The ground stage needs its session and current source review.')
    final, normal = value['final_raw'], value['normal_raw']
    _need(isinstance(final, dict) and set(final) == {'output', 'parent_chain', 'parent_chain_sha256'}
          and isinstance(normal, dict) and set(normal) == {'output'}
          and all(isinstance(v, str) and v for v in (*final.values(), *normal.values())),
          'Name the final raw review, its trusted parent chain and the normal raw review.')
    return deepcopy(value)


def _derive(inputs, config):
    spec = config['stage']
    directory = _ordinary(spec['session'])
    final_cfg, normal_cfg = config['final_raw'], config['normal_raw']
    with _reader(config.get('environment'), directory) as applied:
        row = base._stage(inputs, spec)
    _need(row.get('status') != 'invalid' and row.get('complete_matrix_verified') is True,
          'The canonical ground stage must have a complete, valid terminal matrix: '
          + json.dumps(row.get('errors', [])), 'invalid_canonical_stage')
    state = Session(directory)._load()
    _need(state.get('status') == 'finalized' and state.get('active_job_id') is None,
          'Wait for terminal recording and model work.', 'stage_not_terminal')
    selected = [c for c in state.get('candidates', []) if c.get('candidate_id') == state.get('selected_candidate_id')]
    _need(len(selected) == 1 and row.get('selected_candidate', {}).get('source_sha256') == selected[0]['source_sha256'],
          'One exact selected candidate is required.', 'wrong_candidate')
    common = {'session': directory, 'parent_chain': final_cfg['parent_chain'],
              'parent_chain_sha256': final_cfg['parent_chain_sha256']}
    final = v3raw.run('final', 'validate', output=final_cfg['output'], **common)
    normal = v3raw.run('normal', 'validate', output=normal_cfg['output'], **common)
    required = suite_evidence.required_bindings(directory, state, require_complete=True)
    covered = {tuple(pair) for pair in final.get('required_observations', [])}
    # Both validators raise unless the review is attested and numerically
    # passing; they return status 'pass' only (no separate numeric field).
    raw_pass = (final.get('status') == 'pass' and covered == required
                and normal.get('status') == 'pass' and normal.get('mandatory_route_status') == 'pass')
    for proof in (final, normal):
        consumed = proof.get('consumed_files')
        _need(isinstance(consumed, dict) and consumed and digest(consumed) == proof.get('consumed_files_sha256'),
              'A V3 raw proof lacks its byte closure.', 'incomplete_review')
        inputs.declared(consumed)
    row['axes']['independent_raw_review'] = base.axis(
        'pass' if raw_pass else 'fail',
        f"Ground final ({final.get('record_count')} records) and normal-map raw proofs replayed with their actual attestations.",
        final_consumer_version=final.get('consumer_version'), normal_status=normal.get('status'),
        normal_mandatory_route=normal.get('mandatory_route_status'), required_observations=len(required),
        ordinary_reader_compatibility_claimed=False)
    statuses = [row['axes'][name]['status'] for name in MANDATORY]
    row['status'] = 'pass' if all(s == 'pass' for s in statuses) else 'fail' if 'fail' in statuses else 'pending'
    suite = inputs.read(_ordinary(state['reference']) / 'suite.json')
    return {'kind': 'V3StandaloneGroundDomainClosure', 'schema_version': 1, 'version': VERSION,
            'domain': 'game', 'stage_name': 'ground', 'status': row['status'],
            'domain_behavior_acceptance': row['status'] == 'pass', 'stage': row,
            'release_identity_mappings': applied, 'source_epoch': suite['source_epoch'],
            'acceptance_profile': suite['acceptance_profile'], 'acceptance_rule': suite['game_acceptance_rule'],
            'raw_proofs': {'final': {k: final.get(k) for k in ('kind', 'status', 'record_count',
                                                                 'consumer_version', 'consumed_files_sha256')},
                           'normal': {k: normal.get(k) for k in ('kind', 'status', 'mandatory_route_status',
                                                                  'consumed_files_sha256')}},
            'completed_current_authorship_verified': base._verified_current_authorship(row),
            'canonical_session_acceptance_changed': False, 'historical_results_changed': False,
            'full_phase_acceptance': False, 'recording_or_model_started': False,
            'comparison_thresholds_recomputed': False,
            'scope': 'Only the V3 ground domain in its V3a source epoch; no overall phase acceptance.'}


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
    summary = {k: value[k] for k in ('kind', 'version', 'status', 'domain_behavior_acceptance', 'source_epoch',
                                     'acceptance_profile')} | {
        'axes': {name: axis['status'] for name, axis in value['stage']['axes'].items()}}
    write_json(output / 'summary.json', summary)
    write_json(output / 'receipt.json', {'kind': 'V3StandaloneGroundDomainClosureReceipt', 'schema_version': 1,
                                         'version': VERSION, 'config': str(config_path),
                                         'config_sha256': file_sha256(config_path), 'closure_sha256': digest(value),
                                         'consumed_files': inputs.files, 'consumed_files_sha256': digest(inputs.files)})
    return value


def validate(output):
    output = _ordinary(output)
    value, receipt = read_json(output / 'closure.json'), read_json(output / 'receipt.json')
    _need(value.get('kind') == 'V3StandaloneGroundDomainClosure' and value.get('version') == VERSION
          and receipt.get('kind') == 'V3StandaloneGroundDomainClosureReceipt' and receipt.get('version') == VERSION
          and digest(value) == receipt.get('closure_sha256')
          and digest(receipt.get('consumed_files')) == receipt.get('consumed_files_sha256'),
          'Closure identity differs.', 'stale_closure')
    for name, sha in receipt['consumed_files'].items():
        _need(file_sha256(name) == sha, 'Consumed evidence changed: ' + name, 'stale_closure')
    inputs = base.Inputs()
    fresh = _derive(inputs, _config(inputs.read(receipt['config'])))
    keys = [k for k in value if k not in ('release_identity_mappings',)]
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
