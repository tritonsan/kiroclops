"""Isolated, final-only known-regression alias bridge for panel/SVG pixels.

The authoritative suite uses ``regression`` while its public recording recipe
uses ``known_regression``.  Only the temporary validation envelope normalizes
that alias.  Every returned scenario and all saved record/package bytes remain
canonical.  This producer does not grant behavior, source or model acceptance.
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

from kiroclops.local_engine import realistic_review as review, suite_evidence
from kiroclops.local_engine.session import Session, _ordinary
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json

VERSION = 'realistic-final-known-regression-envelope-v1'
_active = False


def _need(condition, message, code='invalid_review'):
    if not condition:
        raise VisualError(code, message)


def _extra_sources():
    path = Path(__file__).resolve()
    return {str(path): file_sha256(path)}


def _matrix(context, rows):
    """Authorize a complete canonical matrix, without changing any scenario."""
    stage = context.get('stage')
    expected_counts = {'panel': {'calibration': 8, 'regression': 12, 'validation': 12},
                       'svg': {'calibration': 4, 'regression': 12, 'validation': 12}}
    _need(stage in expected_counts and context.get('domain') == ('web_panel' if stage == 'panel' else 'svg')
          and context.get('acceptance_policy', {}).get('id') == 'realistic-behavior-v1',
          'Only the canonical prospective panel/SVG realistic policy can use this envelope.', 'wrong_final_seal')
    cases = context.get('cases', [])
    _need(isinstance(cases, list) and len(cases) == sum(expected_counts[stage].values())
          and all(sum(c.get('role') == role for c in cases) == count
                  for role, count in expected_counts[stage].items()),
          'The full calibration, known-regression and validation case grid is required.', 'wrong_final_seal')
    by_case = {c.get('case_id'): c for c in cases}
    _need(len(by_case) == len(cases) and all(isinstance(name, str) and name for name in by_case),
          'Canonical case identities must be unique.', 'wrong_final_seal')
    seen, aliases = set(), []
    for value in rows:
        row = review.legacy._record(value, len(seen))
        case = by_case.get(row.get('case_id'))
        _need(case is not None and row.get('split_role') == case['role']
              and row.get('scenario') == case.get('scenario')
              and row.get('domain') == stage,
              'A review row must retain its exact canonical scenario and split.', 'wrong_final_seal')
        key = (row['role'], row['case_id'])
        _need(row['role'] in review.ROLES and key not in seen, 'The raw matrix contains a duplicate or unknown role.')
        seen.add(key)
        scenario = row['scenario']
        declared = scenario.get('suite_split', scenario.get('split'))
        if case['role'] == 'regression':
            _need(scenario.get('suite_split') == 'known_regression'
                  and scenario.get('blind_validation') is False,
                  'Only explicit nonblind known_regression recipes authorize the alias.', 'unauthorized_regression_alias')
            aliases.append({'record_id': row['id'], 'case_id': row['case_id'], 'capture_role': row['role'],
                'canonical_split': 'regression', 'declared_suite_split': 'known_regression',
                'original_scenario_sha256': digest(scenario),
                'temporary_validation_field': 'scenario.suite_split',
                'temporary_validation_value': 'regression', 'canonical_scenario_restored': True})
        else:
            _need(declared == case['role'] and scenario.get('blind_validation') is (case['role'] == 'validation'),
                  'Calibration and new validation cannot be relabelled by this bridge.', 'unauthorized_regression_alias')
    expected = {(role, name) for role in review.ROLES for name in by_case}
    _need(seen == expected and len(rows) == len(expected), 'Every exact case needs all four source roles.', 'wrong_final_seal')
    _need(len({r.get('id') for r in rows}) == len(rows), 'Independent recording IDs must be unique.')
    return {'kind': 'FinalKnownRegressionEnvelopeNormalization', 'schema_version': 1,
        'version': VERSION, 'stage': stage, 'suite_sha256': context['suite_sha256'],
        'candidate_sha256': context['candidate_sha256'], 'record_count': len(rows),
        'alias_record_count': len(aliases), 'aliases': aliases,
        'normalization_scope': 'temporary_final_review_validation_envelope_only',
        'canonical_scenarios_changed': False, 'record_package_or_session_bytes_changed': False,
        'heldout_relabelled_as_calibration': False, 'full_behavior_acceptance': False}


def _canonical_inputs(context):
    authority = _ordinary(context['session_directory'])
    state = Session(authority)._load()
    required = suite_evidence.required_bindings(authority, state, require_complete=True)
    suite = _ordinary(context['suite_directory'])
    files = {authority / 'session.json', authority / 'binding.json', suite / 'suite.json', suite / 'suite-binding.json'}
    for job_id in (state['prepare_job_id'], context['evaluation_job_id'], context['final_job_id']):
        for name in ('job.json', 'binding.json', 'result.json'):
            files.add(authority / 'jobs' / job_id / name)
    _, layout = suite_evidence._matrix(read_json(suite / 'suite.json'), state)
    files.update(authority / 'measurements' / job / (role + '.json') for job, role, _ in layout)
    return {'canonical_input_files': {str(path): file_sha256(path) for path in sorted(files)},
        'required_observations': sorted([list(pair) for pair in required]),
        'native_record_and_source_bindings_replayed': True,
        'native_model_generation_acceptance_granted': False, 'source_code_acceptance_granted': False}


@contextmanager
def _controller():
    """Restore every changed callable, including when extraction/validation fails."""
    global _active
    _need(not _active, 'Only one final alias controller may run in this process.', 'controller_active')
    saved = {'version': review.VERSION, 'context': review._session_context,
             'records': review._records, 'sources': review._code_sources}
    binding = {}
    _active = True
    try:
        def context(directory):
            current, rows = saved['context'](directory)
            normalization = _matrix(current, rows)
            current = deepcopy(current)
            current['independent_consumer_version'] = VERSION
            current['review_envelope_normalization_sha256'] = digest(normalization)
            current['canonical_input_proof'] = _canonical_inputs(current)
            binding.update(context=current, rows=deepcopy(rows), normalization=normalization)
            return current, rows

        def records(values, domain, phase):
            _need(phase == 'final' and binding and values == binding['rows']
                  and domain == binding['context']['stage'],
                  'Only the previously replayed final canonical matrix may use this envelope.', 'wrong_final_seal')
            temporary = deepcopy(values)
            canonical = {}
            for value in temporary:
                merged = review.legacy._record(value, 0)
                canonical[merged['id']] = deepcopy(merged['scenario'])
                if merged['split_role'] == 'regression':
                    # An outer envelope overrides the nested record only for the
                    # old strict predicate; the original recorded dictionary stays intact.
                    value['scenario'] = {**merged['scenario'], 'suite_split': 'regression'}
            checked = saved['records'](temporary, domain, phase)
            for row in checked:
                row['scenario'] = canonical[row['id']]
            _need(all(row['scenario'] == canonical[row['id']] for row in checked),
                  'The temporary review envelope must never enter measurement/scoring.')
            return checked

        def sources():
            return saved['sources']() | _extra_sources()

        review.VERSION, review._session_context = VERSION, context
        review._records, review._code_sources = records, sources
        yield binding
    finally:
        review.VERSION, review._session_context = saved['version'], saved['context']
        review._records, review._code_sources = saved['records'], saved['sources']
        _active = False


def _check_files(files):
    _need(isinstance(files, dict) and files, 'A nonempty canonical byte inventory is required.')
    for name, sha in files.items():
        path = _ordinary(name)
        _need(path.is_file() and file_sha256(path) == sha, 'Bound canonical input changed: ' + str(path), 'stale_review')


def prepare(session_directory, output):
    authority, output = map(_ordinary, (session_directory, output))
    extra = _extra_sources()
    with _controller() as binding:
        summary = review.prepare_final_review(authority, output)
        current, _ = review._session_context(authority)
        _need(current == read_json(output / 'result.json')['context'], 'Canonical final input changed during extraction.', 'stale_review')
        normalization = deepcopy(binding['normalization'])
    _need(extra == _extra_sources(), 'Consumer changed during extraction.', 'stale_review')
    producer = output / 'consumer-sources' / Path(__file__).name
    producer.parent.mkdir(parents=True, exist_ok=True)
    producer.write_bytes(Path(__file__).read_bytes())
    _need(file_sha256(producer) == next(iter(extra.values())), 'Producer archive differs.', 'stale_review')
    write_json(output / 'review-envelope-normalization.json', normalization)
    write_json(output / 'prepare-api-result.json', summary)
    consumer = {'kind': 'FinalWebKnownRegressionConsumerBinding', 'schema_version': 1, 'version': VERSION,
        'session_directory': str(authority), 'extra_sources': extra, 'producer_archive': str(producer),
        'producer_sha256': file_sha256(producer), 'normalization_sha256': digest(normalization),
        'context_sha256': digest(current), 'record_count': normalization['record_count'],
        'raw_only': True, 'ordinary_reader_compatibility_claimed': False,
        'source_code_acceptance_granted': False, 'native_model_generation_acceptance_granted': False,
        'new_model_generation': False, 'full_behavior_acceptance': False}
    write_json(output / 'consumer-binding.json', consumer)
    receipt = read_json(output / 'evidence-receipt.json')
    receipt['consumer_binding_sha256'] = digest(consumer)
    receipt['generated_files'] = {path.relative_to(output).as_posix(): file_sha256(path)
        for path in sorted(output.rglob('*')) if path.is_file() and path != output / 'evidence-receipt.json'}
    write_json(output / 'evidence-receipt.json', receipt)
    return {'summary': summary, 'result': read_json(output / 'result.json'), 'consumer_binding': consumer}


def _binding(output, authority):
    consumer = read_json(output / 'consumer-binding.json')
    receipt = read_json(output / 'evidence-receipt.json')
    normalization = read_json(output / 'review-envelope-normalization.json')
    _need(consumer.get('kind') == 'FinalWebKnownRegressionConsumerBinding' and consumer.get('schema_version') == 1
          and consumer.get('version') == VERSION and consumer.get('raw_only') is True
          and consumer.get('ordinary_reader_compatibility_claimed') is False
          and consumer.get('source_code_acceptance_granted') is False
          and consumer.get('native_model_generation_acceptance_granted') is False
          and consumer.get('new_model_generation') is False and consumer.get('full_behavior_acceptance') is False
          and _ordinary(consumer['session_directory']) == authority
          and consumer.get('extra_sources') == _extra_sources()
          and consumer.get('normalization_sha256') == digest(normalization)
          and receipt.get('consumer_binding_sha256') == digest(consumer),
          'Final-only consumer identity or scope changed.', 'stale_review')
    _check_files(consumer['extra_sources'])
    archived = _ordinary(consumer['producer_archive'])
    _need(archived.is_relative_to(output / 'consumer-sources')
          and consumer['producer_sha256'] == next(iter(consumer['extra_sources'].values()))
          and file_sha256(archived) == consumer['producer_sha256'], 'Bound producer archive differs.', 'stale_review')
    return consumer, receipt, normalization


def validate(output, *, session_directory, require_attested=True):
    output, authority = map(_ordinary, (output, session_directory))
    consumer, receipt, normalization = _binding(output, authority)
    with _controller() as binding:
        context, _ = review._session_context(authority)
        _need(binding['normalization'] == normalization and consumer['context_sha256'] == digest(context),
              'Canonical scenario alias authorization or seal changed.', 'stale_review')
        result = review.validate_review(output, require_attested=require_attested)
        _need(result.get('phase') == 'final' and result.get('context') == context
              and len(result.get('records', [])) == consumer['record_count'],
              'The final raw proposal belongs to another complete seal.', 'stale_review')
        _check_files(context['canonical_input_proof']['canonical_input_files'])
    consumed = {**receipt['source_files'], **receipt['code_sources'],
        **context['canonical_input_proof']['canonical_input_files'],
        **{str(safe_path(output, name)): sha for name, sha in receipt['generated_files'].items()},
        str(output / 'evidence-receipt.json'): file_sha256(output / 'evidence-receipt.json')}
    if require_attested:
        consumed[str(output / 'attestation.json')] = file_sha256(output / 'attestation.json')
    return {'kind': 'IndependentFinalWebEnvelopeRawVerification', 'schema_version': 1, 'version': VERSION,
        'status': 'pass' if require_attested else 'proposal_verified',
        'numeric_status': result['numeric_status'], 'domain': result['domain'],
        'session_directory': str(authority), 'raw_review_directory': str(output),
        'candidate_sha256': context['candidate_sha256'], 'suite_sha256': context['suite_sha256'],
        'selected_candidate_id': context['selected_candidate_id'],
        'evaluation_job_id': context['evaluation_job_id'], 'final_job_id': context['final_job_id'],
        'raw_result_sha256': digest(result), 'evidence_receipt_sha256': file_sha256(output / 'evidence-receipt.json'),
        'consumer_binding_sha256': digest(consumer), 'normalization_sha256': digest(normalization),
        'required_observations': context['canonical_input_proof']['required_observations'],
        'record_count': consumer['record_count'], 'verified_scopes': {'position': 'pass', 'events': 'pass', 'relationships': 'pass'} if require_attested else {},
        'attestation_sha256': file_sha256(output / 'attestation.json') if require_attested else None,
        'code_sources': receipt['code_sources'], 'canonical_input_files': context['canonical_input_proof']['canonical_input_files'],
        'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
        'raw_only': True, 'source_code_review_status': 'pending', 'native_model_generation_review_status': 'pending',
        'new_model_generation': False, 'ordinary_reader_compatibility_claimed': False, 'full_behavior_acceptance': False}


def attest(output, *, session_directory, reviewer, inspected_sheets, accepted, images_inspected):
    validate(output, session_directory=session_directory, require_attested=False)
    with _controller():
        return review.attest_review(output, reviewer=reviewer, inspected_sheets=inspected_sheets,
                                   accepted=accepted, images_inspected=images_inspected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('prepare', 'attest', 'validate'):
        item = commands.add_parser(command)
        item.add_argument('--session', required=True, type=Path)
        item.add_argument('--output', required=True, type=Path)
        if command == 'attest':
            item.add_argument('--inspection', required=True, type=Path)
            item.add_argument('--reviewer', required=True)
            item.add_argument('--accepted', action='store_true')
            item.add_argument('--images-inspected', action='store_true')
        if command == 'validate':
            item.add_argument('--receipt', type=Path, help='Optional fresh external raw-only verification receipt.')
            item.add_argument('--proposal-only', action='store_true', help='Verify bytes without granting raw acceptance.')
    args = parser.parse_args()
    if args.command == 'prepare':
        value = prepare(args.session, args.output)
        printed = value['summary']
    elif args.command == 'attest':
        printed = attest(args.output, session_directory=args.session, reviewer=args.reviewer,
            inspected_sheets=read_json(args.inspection), accepted=args.accepted, images_inspected=args.images_inspected)
    else:
        printed = validate(args.output, session_directory=args.session, require_attested=not args.proposal_only)
        if args.receipt:
            receipt = _ordinary(args.receipt)
            _need(not receipt.exists() and not receipt.is_relative_to(_ordinary(args.output))
                  and not receipt.is_relative_to(_ordinary(args.session)),
                  'Use a fresh external raw-only receipt; canonical Session/review bytes remain immutable.', 'unsafe_path')
            write_json(receipt, printed)
    print(json.dumps(printed, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
