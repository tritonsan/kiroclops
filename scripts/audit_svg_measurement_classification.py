"""Replay classification fixes against frozen SVG pixels; no new qualification.

Existing numerical gates and scores must remain identical. The output records
an engineering reassessment, never overwrites a capture or promotes the mode.
"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.visual.package import load_package
from kiroclops.local_engine.evaluation import _compare_web
from kiroclops.local_engine.media_session import _compare, LIMITS
from scripts.compare_svg_recorder_qualification import binding_for


def run(capture, prior, output):
    capture, prior, output = map(Path, (capture, prior, output))
    if output.exists():
        raise ValueError('Fresh output required')
    protocol = read_json(capture / 'protocol.json')
    original = read_json(prior / 'result.json')
    assert original['original_protocol_sha256'] == file_sha256(capture / 'protocol.json')
    assert protocol['limits'] == LIMITS
    implementation = {name: file_sha256(ROOT / name) for name in (
        'kiroclops/local_engine/evaluation.py', 'kiroclops/local_engine/media_session.py',
        'kiroclops/local_engine/comparison_diagnostics.py', 'kiroclops/visual/comparison.py')}
    rows = []
    for scenario in protocol['scenarios']:
        sid = scenario['id']
        old = read_json(prior / 'results' / (sid + '.json'))
        if old['errors']:
            rows.append({'scenario_id': sid, 'status': 'capture_failure_preserved',
                         'pass': False, 'errors': old['errors']})
            continue
        records = []
        for repeat in (1, 2):
            path = capture / 'records' / sid / (str(repeat) + '.json')
            assert file_sha256(path) == old['record_hashes'][str(repeat)]
            record = read_json(path)
            assert record['source_sha256'] == digest(protocol['source_hashes'])
            assert load_package(record['package'])['package_sha256'] == record['observations']['package_sha256']
            records.append(record)
        a, b = records
        media = _compare(a, a, b, binding_for(a, scenario), require_improvement=False)
        benchmark = _compare_web(a, a, b, improvement=False)
        # Require byte-equivalent semantic metrics, not just an unchanged mean.
        assert media['windows'] == old['input_window_comparison']['windows']
        assert benchmark['pixel_comparison'] == old['benchmark_comparison']['pixel_comparison']
        assert benchmark['score'] == old['benchmark_comparison']['score']
        assert media['score'] == old['input_window_comparison']['score']
        passed = media['pass'] and benchmark['pass'] and old['functional_pass']
        assert passed == old['pass']
        write_json(output / 'results' / (sid + '.json'), {
            'scenario_id': sid, 'input_window_comparison': media, 'benchmark_comparison': benchmark,
            'prior_result_sha256': file_sha256(prior / 'results' / (sid + '.json')),
            'numeric_metrics_unchanged': True, 'pass': passed})
        rows.append({'scenario_id': sid, 'status': 'reassessed', 'pass': passed,
                     'prior_benchmark_measurement_pass': old['benchmark_comparison']['measurement_pass'],
                     'benchmark_measurement_pass': benchmark['measurement_pass'],
                     'benchmark_failure_class': benchmark['failure_class'],
                     'input_window_failure_class': media['failure_class'],
                     'numeric_metrics_unchanged': True})
    result = {'kind': 'FrozenSVGMeasurementClassificationAudit',
              'status': 'pass', 'meaning': 'Classification fix verified; SVG qualification remains failed.',
              'svg_qualified_pairs': sum(r['pass'] for r in rows), 'required_pairs': len(rows),
              'rows': rows, 'prior_result_sha256': file_sha256(prior / 'result.json'),
              'implementation_sha256s': implementation, 'script_sha256': file_sha256(__file__),
              'thresholds_changed': False, 'source_edited': False, 'recaptured': False,
              'model_started': False, 'production_mode_promoted': False}
    write_json(output / 'result.json', result)
    print({'audit': result['status'], 'svg_pairs': result['svg_qualified_pairs'], 'required': len(rows)}, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('capture', 'prior', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    run(args.capture, args.prior, args.output)
