"""Portable read-only presentation of the supplemental recorder validation."""
from pathlib import Path
import argparse
import html
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.local_engine.reporting import HTML, _media, _diff, _portable
from kiroclops.local_engine.environment import configure_environment


def portable_protocol(protocol):
    result = _portable(protocol)
    # The generic value sanitizer does not rewrite dictionary keys.
    result['evidence_hashes'] = {Path(path).parent.name + '/' + Path(path).name: sha
                                 for path, sha in protocol['evidence_hashes'].items()}
    if len(result['evidence_hashes']) != len(protocol['evidence_hashes']):
        raise ValueError('Ambiguous portable evidence labels')
    return result


def run(validation, output):
    validation, output = Path(validation).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Fresh report directory required')
    result = read_json(validation / 'validation.json')
    protocol = read_json(validation / 'protocol.json')
    results = [read_json(validation / 'results' / (r['scenario_id'] + '.json')) for r in result['results']]
    records = [record for row in results for record in row['records'].values()]
    measurement_ok = (len(records) == 36 and not any(r['errors'] for r in result['results']) and
                      all(r['measurement_status'] == 'ok' and r['timing']['status'] == 'pass' for r in records))
    output.mkdir(parents=True)
    configure_environment()
    axes = {}
    facts = {
        'measurement_validity': ('pass' if measurement_ok else 'fail', 'fresh_pixels'),
        'observed_behavior': (result['status'], 'twelve_supplemental_scenarios'),
        'application_integrity': ('pass' if all(r['functional_pass'] for r in result['results']) else 'fail', 'executed_checks'),
        'model_completion': ('pass', 'historical_run_no_new_model'),
        'native_evidence': (result['native_audit_status'], 'sealed_candidate_authorship'),
        'unseen_validation': (result['status'], 'original_scenarios_new_recorder'),
        'independent_visual_review': ('pending', 'new_images_require_new_review'),
        'source_review': ('pending', 'historical_review_not_recertified_here'),
    }
    for name, (status, scope) in facts.items():
        axes[name] = {'status': status, 'scope': scope, 'reason_codes': [scope], 'evidence': []}
    data = {'schema_version': 1, 'kind': 'RecorderSupplementalReport',
            'goal': 'Same Kiro code · new recording method · unchanged 12 scenarios and thresholds',
            'acceptance_summary': {'title': f"{result['passed']}/12 supplemental validations passed · No full phase acceptance",
                                   'status': result['status'], 'certified_success': False},
            'acceptance_axes': axes, 'overall_acceptance': 'pending' if result['status'] == 'pass' else result['status'],
            'numerical': _portable(result), 'acceptance_details': _portable(result),
            'candidate_sha256': result['candidate_source_sha256'],
            'candidate_diff': _diff({'baseline': str(validation / 'sources/before')}, validation / 'sources/candidate', output),
            'candidates': [{'id': 'candidate-02', 'source_sha256': result['candidate_source_sha256'], 'edited': False}],
            'kiro_run': {'session': 'panel-opus55-media-05', 'model': 'claude-opus-5.5', 'effort': 'max',
                         'new_model_run': False, 'native_audit': result['native_audit_status']},
            'limitations': ['This is a supplemental validation after the recording fix. It is not a new, preregistered model run.',
                            'The old 10/12 and calibration results are preserved; the Kiro code and thresholds did not change.',
                            '36 new recordings were attempted for the reference, baseline and candidate. No failed recording was re-captured.',
                            'The independent visual review of the new images is pending. There is no full three-domain acceptance yet.',
                            'The presentation videos are copies. The frame checks use the original PNGs and their times.'],
            'tolerance_assessment': {'limits_changed': False, 'limits': protocol['limits']},
            'groups': []}
    data['measurement_summary'] = {'successful_recordings': len(records), 'required_recordings': 36,
         'minimum_valid_tracking_ratio': min((o['valid_ratio'] for r in records for o in r['observations']['objects']), default=None),
         'maximum_reported_clock_uncertainty_s': max((r['timing']['clock_uncertainty_s'] for r in records), default=None),
         'pixel_presentation_accuracy_claimed': False, 'independent_review': 'pending'}
    rows = []
    for summary in result['results']:
        sid = summary['scenario_id']
        row = read_json(validation / 'results' / (sid + '.json'))
        print('report', sid, flush=True)
        relative = 'results/' + sid + '.json'
        write_json(output / relative, _portable(row))
        group = {'id': sid, 'roles': {}, 'comparison': relative}
        for role, record in row['records'].items():
            group['roles'][role] = _media(record, output, 'media/' + sid + '/' + role, True)
        if group['roles']:
            data['groups'].append(group)
        score = '—' if summary['score'] is None else f"{summary['score']:.6f}"
        improvement = '—' if summary['improvement'] is None else f"{summary['improvement']*100:.2f}%"
        rows.append(f'<tr><td>{html.escape(sid)}</td><td>{html.escape(summary["status"].upper())}</td>'
                    f'<td>{score}</td><td>{improvement}</td><td>{len(summary["errors"])}</td></tr>')
    write_json(output / 'protocol.json', portable_protocol(protocol))
    write_json(output / 'report.json', data)
    write_json(output / 'evidence-binding.json', {'validation_sha256': file_sha256(validation / 'validation.json'),
               'protocol_sha256': file_sha256(validation / 'protocol.json'), 'candidate_sha256': result['candidate_source_sha256'],
               'original_files_unchanged': result['original_files_unchanged']})
    script = json.dumps(data, ensure_ascii=False, allow_nan=False).replace('</', '<\\/')
    table = ('<section><h2>All 12 scenarios</h2><p>In each scenario, a fresh reference, baseline and the pinned candidate. '
             'The curve error is normalized by the corresponding reference motion distance.</p>'
             '<table><tr><th>Scenario</th><th>Result</th><th>Curve error</th><th>Improvement</th><th>Tool error</th></tr>'
             + ''.join(rows) + '</table><p><a href="protocol.json">Experiment protocol</a> · '
             '<a href="evidence-binding.json">Source and result bindings</a></p></section>')
    page = HTML.replace('__DATA__', script).replace('</header>', '</header>' + table)
    page = page.replace('<title>KiroClops · local evidence</title>', '<title>KiroClops · 12-scenario supplemental validation</title>')
    page = page.replace('<style>', '<style>table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px;border-bottom:1px solid #304458}', 1)
    (output / 'index.html').write_text(page, encoding='utf-8')
    return {'report': str(output / 'index.html'), 'groups': len(data['groups'])}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--validation', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    print(run(args.validation, args.output), flush=True)
