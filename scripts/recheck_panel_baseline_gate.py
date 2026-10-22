"""Recheck frozen supplemental panel results after baseline coverage hardening."""
from copy import deepcopy
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.visual.package import load_package
from kiroclops.local_engine.media_session import _compare, LIMITS


def run(capture, review, session, output):
    capture, review, session, output = map(Path, (capture, review, session, output))
    if output.exists():
        raise ValueError('Fresh output required')
    protocol = read_json(capture / 'protocol.json')
    prior = read_json(capture / 'validation.json')
    state = read_json(session / 'session.json')
    selected = next(c for c in state['candidates'] if c['candidate_id'] == state['selected_candidate_id'])
    assert protocol['limits'] == LIMITS
    assert protocol['candidate_source_sha256'] == selected['source_sha256']
    assert prior['protocol_sha256'] == file_sha256(capture / 'protocol.json')
    inventory = read_json(review / 'inventory.json')
    bindings = {Path(r['record_path']).resolve(): r['record_sha256'] for r in inventory['records']}
    binding_path = Path(state['reference']) / 'capture-binding.json'
    base_binding = read_json(binding_path)
    rows = []
    for scenario in protocol['scenarios']:
        sid = scenario['id']
        old = read_json(capture / 'results' / (sid + '.json'))
        records = {}
        for role in ('reference', 'before', 'candidate'):
            path = capture / 'records' / sid / (role + '.json')
            assert file_sha256(path) == bindings[path.resolve()]
            record = read_json(path)
            assert record['source_sha256'] == digest(protocol['source_hashes'][role])
            assert load_package(record['package'])['package_sha256'] == record['observations']['package_sha256']
            assert record == old['records'][role]
            records[role] = record
        binding = deepcopy(base_binding)
        binding['scenario'] = scenario
        result = _compare(records['reference'], records['before'], records['candidate'], binding,
                          require_improvement=True)
        for field in ('windows', 'score', 'before_score', 'improvement_fraction', 'dynamics', 'improvement_gates'):
            assert result[field] == old['comparison'][field], field
        passed = result['pass'] and old['functional_pass']
        write_json(output / 'results' / (sid + '.json'), {
            'comparison': result, 'prior_result_sha256': file_sha256(capture / 'results' / (sid + '.json')),
            'numeric_metrics_unchanged': True, 'pass': passed})
        rows.append({'scenario_id': sid, 'pass': passed,
                     'baseline_measurement_pass': result['baseline_measurement_pass'],
                     'numeric_metrics_unchanged': True, 'score': result['score']})
    result = {'kind': 'FrozenPanelBaselineGateRecheck', 'status': 'pass' if all(r['pass'] for r in rows) else 'fail',
              'passed': sum(r['pass'] for r in rows), 'required': len(rows), 'rows': rows,
              'capture_protocol_sha256': file_sha256(capture / 'protocol.json'),
              'inventory_sha256': file_sha256(review / 'inventory.json'),
              'binding_sha256': file_sha256(binding_path), 'script_sha256': file_sha256(__file__),
              'evaluator_sha256': file_sha256(ROOT / 'kiroclops/local_engine/media_session.py'),
              'candidate_source_sha256': selected['source_sha256'], 'independent_review': 'pending',
              'model_started': False, 'candidate_edited': False, 'recaptured': False, 'thresholds_changed': False}
    write_json(output / 'result.json', result)
    print({k: result[k] for k in ('status', 'passed', 'required')}, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('capture', 'review', 'session', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    args = p.parse_args()
    run(args.capture, args.review, args.session, args.output)
