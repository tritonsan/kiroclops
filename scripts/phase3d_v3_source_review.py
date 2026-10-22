"""Independent source and native-trace review for a V3 Kiro session.

Two steps, both read-only on the session:

``facts``
    Collects machine facts for the human inspection:

    - byte hashes of the native stream, outbound journal and guard log;
    - per-file source chains from the authoring baseline to the selected
      candidate, rebuilt from the recorded native diff updates;
    - a fresh native replay through the exact Kiro CLI 2.27.1 release identity
      mapping.

    It records no verdict.

``receipt``
    Binds the reviewer's inspection file to the facts and writes the
    ``IndependentSourceReview`` receipt, then validates it with the canonical
    reader inside the same identity mapping. The inspection file holds the five
    verdicts and their evidence. Writing it is the actual inspection;
    this tool cannot produce or infer a verdict.

Follows ``reviews/p04-source-native-v3-2026-10-02/build_source_review.py``.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import review, session
from kiroclops.visual.schema import VisualError, digest
from scripts import phase3d_v2_native_release_identity as identity

CHECKS = ('actual_code_authorship', 'no_coordinate_replay', 'no_private_reference_access',
          'no_operator_coaching', 'protected_behavior_preserved')
NATIVE_FILES = ('kiro/launch.json', 'kiro/config.json', 'kiro/run/stdout.jsonl', 'kiro/run/outbound.jsonl',
                'kiro/tool-audit.jsonl', 'session.json')


def _need(value, message, code='incomplete_review'):
    if not value:
        raise VisualError(code, message)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _lines(path):
    return [json.loads(line) for line in Path(path).read_bytes().decode('utf-8').splitlines() if line.strip()]


def _save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    return Path(path)


def _diff_updates(inbound, relative):
    """Completed native updates whose diff content targets ``relative``, in stream order."""
    found = []
    suffix = '/' + relative.lower()
    for number, event in enumerate(inbound, 1):
        update = event.get('params', {}).get('update', {})
        if update.get('status') != 'completed':
            continue
        for content in update.get('content', []) or []:
            path = str(content.get('path', '')).replace('\\', '/').lower()
            if content.get('type') == 'diff' and path.endswith(suffix):
                found.append({'line': number, 'tool_call_id': update.get('toolCallId'),
                              'checkpoint': update.get('_meta', {}).get('kiro', {}).get('checkpoint'),
                              'old': content.get('oldText'), 'new': content.get('newText')})
    return found


def _chain(directory, relative, candidate_source, inbound):
    """Rebuild one file's edit chain; full old/new texts must link baseline to the candidate bytes."""
    before = (directory / 'baseline' / relative).read_bytes()
    after = (candidate_source / relative).read_bytes()
    row = {'path': relative, 'changed': before != after, 'baseline_sha256': hashlib.sha256(before).hexdigest(),
           'candidate_sha256': hashlib.sha256(after).hexdigest(), 'native_updates': []}
    updates = _diff_updates(inbound, relative)
    for item in updates:
        row['native_updates'].append({k: item[k] for k in ('line', 'tool_call_id', 'checkpoint')} | {
            'old_sha256': hashlib.sha256(str(item['old'] or '').encode('utf-8')).hexdigest(),
            'new_sha256': hashlib.sha256(str(item['new'] or '').encode('utf-8')).hexdigest()})
    if not row['changed']:
        row['chain_status'] = 'unchanged' if not updates else 'edited_then_restored_or_later'
        return row
    current, reached = before, None
    for index, item in enumerate(updates):
        old, new = str(item['old'] or '').encode('utf-8'), str(item['new'] or '').encode('utf-8')
        if old != current:
            row['chain_status'] = 'broken_at_update_%d' % index
            return row
        current = new
        if current == after:
            reached = index
    row['chain_status'] = 'linked' if reached is not None else 'candidate_bytes_not_reached'
    row['candidate_reached_at_update'] = reached
    return row


def _environment(enabled, directory, v5=None):
    """Optional native policies.

    ``enabled``: the V4 one-server MCP environment (scripts/phase3d_v4_native_environment.py).
    ``v5``: the V5 stack fixed before the V5 runs (PHASE3D_V5_PLAN.md 3.4): the prevented-denial
    policy (scripts/phase3d_v5_native_denial.py), inside the receipted one-server environment when
    ``v5['receipt']`` names a power-removal window receipt. When ``v5['offloads']`` names a V5b
    offload manifest, the V5b offloaded shell output policy (PHASE3D_V5B_PLAN.md 2.4,
    scripts/phase3d_v5b_native_offload.py) is applied as well.
    """
    from contextlib import contextmanager, nullcontext
    if v5 is None:
        if not enabled:
            return nullcontext()
        from scripts import phase3d_v4_native_environment as environment
        return environment.environment_policy([directory])
    _need(not enabled, 'Choose the V4 environment or the V5 native stack, not both.')
    from scripts import phase3d_v4_native_environment as environment
    from scripts import phase3d_v5_native_denial as denial

    @contextmanager
    def stack():
        scope = (environment.environment_policy([directory], receipt=Path(v5['receipt']),
                                                receipt_sha256=v5['receipt_sha256'])
                 if v5.get('receipt') else nullcontext())
        if v5.get('offloads'):
            from scripts import phase3d_v5b_native_offload as offload
            extension = offload.offload_policy(Path(v5['offloads']), v5['offloads_sha256'])
        else:
            extension = nullcontext()
        with scope, denial.denial_policy() as ledger, extension:
            yield ledger
    return stack()


def _v5(args):
    if not args.native_v5:
        _need(args.v5_receipt is None and args.v5_receipt_sha256 is None
              and args.v5b_offloads is None and args.v5b_offloads_sha256 is None,
              'The V5 receipt and V5b offload options need --native-v5.')
        return None
    _need((args.v5_receipt is None) == (args.v5_receipt_sha256 is None), 'Give the V5 window receipt with its sha256.')
    _need((args.v5b_offloads is None) == (args.v5b_offloads_sha256 is None), 'Give the V5b offload manifest with its sha256.')
    value = {'receipt': str(args.v5_receipt) if args.v5_receipt else None, 'receipt_sha256': args.v5_receipt_sha256}
    if args.v5b_offloads is not None:
        value.update(offloads=str(args.v5b_offloads), offloads_sha256=args.v5b_offloads_sha256)
    return value


def facts(directory, output, environment=False, v5=None):
    directory, output = Path(directory).resolve(), Path(output).resolve()
    _need(not output.exists(), 'Preserve earlier source-review facts.', 'output_exists')
    state = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
    selected = state.get('selected_candidate_id')
    _need(selected and state.get('status') == 'finalized', 'A finalized session with a selected candidate is required.')
    source = directory / 'candidates' / selected / 'source'
    source_hashes = session.source_hashes(source)
    baseline_hashes = session.source_hashes(directory / 'baseline')
    _need(baseline_hashes == state['baseline_hashes'], 'The authoring baseline changed.', 'source_changed')
    candidate = next(c for c in state['candidates'] if c['candidate_id'] == selected)
    _need(digest(source_hashes) == candidate['source_sha256'], 'The selected candidate source changed.', 'source_changed')
    files = {name: _sha(directory / name) for name in NATIVE_FILES if (directory / name).is_file()}
    inbound = _lines(directory / 'kiro/run/stdout.jsonl')
    outbound = _lines(directory / 'kiro/run/outbound.jsonl')
    guards = _lines(directory / 'kiro/tool-audit.jsonl')
    chains = [_chain(directory, relative, source, inbound)
              for relative in sorted(set(source_hashes) | set(baseline_hashes))
              if (directory / 'baseline' / relative).is_file() and (source / relative).is_file()]
    added = sorted(set(source_hashes) - set(baseline_hashes))
    removed = sorted(set(baseline_hashes) - set(source_hashes))
    with _environment(environment, directory, v5):
        native = identity.audit_native_run(directory)
    output.mkdir(parents=True)
    native_path = _save(output / 'native-audit.json', native)
    value = {'kind': 'V3SourceReviewFacts', 'schema_version': 1, 'session': str(directory),
             'session_id': state.get('session_id'), 'selected_candidate_id': selected,
             'candidate_sha256': candidate['source_sha256'], 'source_hashes': source_hashes,
             'baseline_hashes': baseline_hashes, 'added_files': added, 'removed_files': removed,
             'final_job_id': state.get('final_job_id'),
             'final_job_hashes': {name: _sha(directory / 'jobs' / state['final_job_id'] / name)
                                  for name in ('job.json', 'binding.json', 'result.json')} if state.get('final_job_id') else {},
             'native_files_sha256': files, 'inbound_events': len(inbound), 'outbound_rows': len(outbound),
             'guard_rows': len(guards), 'guard_tool_counts': dict(Counter(str(g.get('tool')) for g in guards)),
             'source_file_chain': chains,
             'native_replay': {'path': str(native_path), 'sha256': _sha(native_path), 'status': native.get('status'),
                               'evidence_hashes': native['native']['evidence_hashes']},
             'native_policy': {'environment_v4': bool(environment), 'v5': v5},
             'reviewer_engine_sha256': _sha(review.__file__), 'producer_sha256': _sha(__file__),
             'verdicts_recorded': False,
             'scope': 'Machine facts for a human source/native inspection; no verdict.'}
    _save(output / 'facts.json', value)
    return value


def receipt(directory, facts_dir, inspection_path, output, environment=False, v5=None):
    directory, facts_dir, output = Path(directory).resolve(), Path(facts_dir).resolve(), Path(output).resolve()
    _need(not output.exists(), 'Preserve earlier source reviews.', 'output_exists')
    known = json.loads((facts_dir / 'facts.json').read_text(encoding='utf-8'))
    inspection = json.loads(Path(inspection_path).read_text(encoding='utf-8'))
    _need(Path(known['session']) == directory and known.get('kind') == 'V3SourceReviewFacts',
          'Facts belong to another session.', 'wrong_session')
    for name, sha in known['native_files_sha256'].items():
        _need(_sha(directory / name) == sha, 'Native evidence changed after the facts were collected: ' + name, 'stale_review')
    _need(isinstance(inspection, dict) and isinstance(inspection.get('reviewer'), str) and inspection['reviewer'].strip()
          and isinstance(inspection.get('checks'), list) and [c.get('id') for c in inspection['checks']] == list(CHECKS)
          and all(c.get('verdict') in ('pass', 'fail') and isinstance(c.get('evidence'), str) and c['evidence'].strip()
                  for c in inspection['checks']),
          'The inspection must give all five verdicts, in order, each with written evidence.')
    passed = all(c['verdict'] == 'pass' for c in inspection['checks'])
    output.mkdir(parents=True)
    value = {
        'kind': 'IndependentSourceReview', 'schema_version': 1, 'status': 'pass' if passed else 'fail',
        'reviewer': inspection['reviewer'].strip(), 'method': 'independent_source_and_native_trace_audit',
        'inspected_at': datetime.now(timezone.utc).isoformat(), 'session_directory': str(directory),
        'candidate_id': known['selected_candidate_id'], 'candidate_sha256': known['candidate_sha256'],
        'source_hashes': known['source_hashes'], 'baseline_hashes': known['baseline_hashes'],
        'native_evidence_hashes': known['native_replay']['evidence_hashes'],
        'reviewer_engine_sha256': known['reviewer_engine_sha256'],
        'facts': {'path': str(facts_dir / 'facts.json'), 'sha256': _sha(facts_dir / 'facts.json')},
        'inspection': {'path': str(Path(inspection_path).resolve()), 'sha256': _sha(inspection_path)},
        'independent_fresh_native_replay': {'path': known['native_replay']['path'], 'sha256': known['native_replay']['sha256']},
        'release_identity': '2.27.1 runs are replayed with the proven 2.27.0 rules (phase3d_v2_native_release_identity).',
        'final_job_id': known['final_job_id'], 'final_job_hashes': known['final_job_hashes'],
        'source_file_chain': known['source_file_chain'], 'checks': inspection['checks'],
        'native_validation_status': 'pending_validation', 'full_behavior_acceptance': False,
        'limits': inspection.get('limits', 'Source/native evidence only; pixel, event, relationship and executed '
                                 'function gates are separate. No full behavior acceptance is granted.')}
    path = _save(output / 'source-review.json', value)
    validation = {'kind': 'V3SourceReviewValidation', 'status': 'fail' if not passed else None,
                  'source_review_path': str(path), 'source_review_sha256': _sha(path)}
    if passed:
        with _environment(environment, directory, v5), identity.release_identity_policy() as applied:
            verified = review.validate_source_review(path, directory, candidate_sha256=known['candidate_sha256'])
        validation.update(status='pass' if verified['native_validation']['status'] == 'pass' else 'fail',
                          release_identity_mappings=applied)
    _save(output / 'validation.json', validation)
    return {**value, 'validation': validation}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest='command', required=True)
    first = commands.add_parser('facts')
    first.add_argument('--session', type=Path, required=True)
    first.add_argument('--output', type=Path, required=True)
    first.add_argument('--environment-v4', action='store_true')
    first.add_argument('--native-v5', action='store_true')
    first.add_argument('--v5-receipt', type=Path)
    first.add_argument('--v5-receipt-sha256')
    first.add_argument('--v5b-offloads', type=Path)
    first.add_argument('--v5b-offloads-sha256')
    second = commands.add_parser('receipt')
    second.add_argument('--session', type=Path, required=True)
    second.add_argument('--facts', type=Path, required=True)
    second.add_argument('--inspection', type=Path, required=True)
    second.add_argument('--output', type=Path, required=True)
    second.add_argument('--environment-v4', action='store_true')
    second.add_argument('--native-v5', action='store_true')
    second.add_argument('--v5-receipt', type=Path)
    second.add_argument('--v5-receipt-sha256')
    second.add_argument('--v5b-offloads', type=Path)
    second.add_argument('--v5b-offloads-sha256')
    args = parser.parse_args(argv)
    try:
        if args.command == 'facts':
            value = facts(args.session, args.output, args.environment_v4, _v5(args))
            print(json.dumps({k: value[k] for k in ('selected_candidate_id', 'candidate_sha256', 'inbound_events',
                                                    'guard_rows', 'guard_tool_counts')} | {
                'chains': {c['path']: c['chain_status'] for c in value['source_file_chain']},
                'native_replay': value['native_replay']['status']}, ensure_ascii=False))
        else:
            value = receipt(args.session, args.facts, args.inspection, args.output, args.environment_v4, _v5(args))
            print(json.dumps({'status': value['status'], 'validation': value['validation']['status']}))
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
