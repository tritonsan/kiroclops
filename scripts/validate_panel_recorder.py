"""Supplemental validation after recorder repair; never rewrites old verdicts.

The scenarios were selected before the model run, but this recorder was qualified
afterward. This is explicitly not a fresh preregistered model trial.
"""
from pathlib import Path
from copy import deepcopy
import argparse
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.visual.package import load_package
from kiroclops.local_engine.session import source_hashes, copy_project, _lock
from kiroclops.local_engine.evaluation import _capture_web
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.media_session import _compare, LIMITS
from kiroclops.local_engine.review import audit_native_run
from kiroclops.web_local.specs import validate_scenario

ROOT = Path(__file__).resolve().parents[1]
DRIVER = 'natural_browser_raf_v1'


def implementation_hashes(root):
    return {p.relative_to(root).as_posix(): file_sha256(p)
            for folder in ('kiroclops/web_local', 'kiroclops/local_engine', 'kiroclops/visual')
            for p in sorted((root / folder).rglob('*.py'))}


def original_recipes(state, preparation, protocol_path):
    protocol = read_json(protocol_path)
    if (file_sha256(protocol_path) != preparation.get('protocol_sha256') or
            protocol['created_at'] >= state['started_at'] or protocol['limits'] != LIMITS):
        raise ValueError('Original protocol not bound before the model, or limits changed')
    recipes = deepcopy(protocol['heldout'])
    if len(recipes) != 12 or len({s['id'] for s in recipes}) != 12:
        raise ValueError('Twelve distinct original scenarios required')
    for scenario in recipes:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', scenario['id']):
            raise ValueError('Scenario IDs must be path-free')
        if scenario['domain'] != 'panel' or scenario['split'] != 'validation':
            raise ValueError('Wrong validation scope')
        # Only the recorder changes; inputs, viewports, durations and limits do not.
        scenario['frame_driver'] = DRIVER
        validate_scenario(scenario)
    return recipes


def verify_recorder(root, qualification, bridge_path, recipes):
    gate = read_json(qualification / 'result.json')
    qp = read_json(qualification / 'protocol.json')
    if (gate.get('status') != 'pass' or gate.get('passed') != 7 or
            gate.get('required') != 7 or len(gate.get('rows', [])) != 7 or
            not all(r['pass'] and not r['errors'] for r in gate['rows']) or qp['limits'] != LIMITS):
        raise ValueError('Full reference qualification with unchanged limits required')
    bridge = read_json(bridge_path)
    if (bridge.get('status') != 'pass' or not bridge.get('qualified_recorder_behavior_modules_unchanged') or
            bridge.get('changed_adapter_files') != ['specs.py'] or
            bridge.get('qualification_sha256') != file_sha256(qualification / 'result.json')):
        raise ValueError('Qualified recorder admission bridge is invalid')
    current = {p.name: file_sha256(p) for p in (root / 'kiroclops/web_local').glob('*.py')}
    if current.keys() != qp['adapter_hashes'].keys():
        raise ValueError('Recorder files changed after qualification')
    changed = {name for name, value in current.items() if value != qp['adapter_hashes'][name]}
    if changed != {'specs.py'}:
        raise ValueError('Only the documented specs admission change is allowed')
    for name, expected in bridge['implementation_hashes'].items():
        if file_sha256(root / name.replace('\\', '/')) != expected:
            raise ValueError('Recorder or evaluator changed after admission bridge')
    if bridge['implementation_hashes'].get('kiroclops\\web_local\\specs.py') != current['specs.py']:
        raise ValueError('Specs admission hash not bound')

    def behavior(scenario):
        s = deepcopy(scenario)
        for key in ('id', 'split', 'repeat'):
            s.pop(key, None)
        s.setdefault('click_schedule', 'dispatch_at_due_v1')
        return s
    qualified = [behavior(s) for s in qp['scenarios']]
    if any(behavior(s) not in qualified for s in recipes):
        raise ValueError('A scenario behavior was not covered by reference qualification')
    return qp


def validate_sources(sources, state, selected):
    expected = {role: source_hashes(path) for role, path in sources.items()}
    if digest(expected['candidate']) != selected['source_sha256']:
        raise ValueError('Sealed candidate source changed')
    if expected['before'] != state['baseline_hashes']:
        raise ValueError('Baseline changed')
    manifest = load_package(Path(state['reference']) / 'package')
    if expected['reference'] != manifest['metadata']['source_hashes']:
        raise ValueError('Reference source differs from supplied pixels')
    return expected


def run(session, original_protocol, reference_project, qualification, bridge_path, output):
    session, original_protocol, reference_project, qualification, bridge_path, output = [
        Path(p).resolve() for p in (session, original_protocol, reference_project, qualification, bridge_path, output)]
    if output.exists():
        raise ValueError('Fresh output required; preserve historical failures')
    if any(output.is_relative_to(p) for p in (session, reference_project, qualification, bridge_path.parent)):
        raise ValueError('Keep output outside original evidence and sources')
    state = read_json(session / 'session.json')
    preparation = read_json(session / 'operator-preparation.json')
    if state['status'] != 'finalized' or not state['selected_candidate_id'] or state['active_job_id']:
        raise ValueError('Only an idle sealed candidate can be validated')
    recipes = original_recipes(state, preparation, original_protocol)
    qp = verify_recorder(ROOT, qualification, bridge_path, recipes)
    selected = next(c for c in state['candidates'] if c['candidate_id'] == state['selected_candidate_id'])
    sources = {'reference': reference_project, 'before': Path(state['baseline']).resolve(),
               'candidate': session / 'candidates' / state['selected_candidate_id'] / 'source'}
    if any(output.is_relative_to(p) for p in sources.values()):
        raise ValueError('Keep output outside source projects')
    expected = validate_sources(sources, state, selected)
    if expected['reference'] != qp['source_hashes']:
        raise ValueError('Qualified reference source changed')
    native = audit_native_run(session)
    if native['status'] != 'pass':
        raise ValueError('Native authorship audit must pass')
    snapshot = {p.relative_to(session).as_posix(): file_sha256(p) for p in session.rglob('*') if p.is_file()}
    implementation = implementation_hashes(ROOT)
    evidence_paths = [original_protocol, qualification / 'result.json', qualification / 'protocol.json', bridge_path]
    evidence = {str(p): file_sha256(p) for p in evidence_paths}
    output.mkdir(parents=True)
    protocol = {'kind': 'PostRecorderFixSupplementalValidation', 'created_at': time.time(),
                'fresh_preregistered_model_trial': False, 'historical_verdicts_replaced': False,
                'model_started': False, 'candidate_edited': False, 'retries': 0,
                'original_protocol_sha256': preparation['protocol_sha256'],
                'qualification_sha256': evidence[str(qualification / 'result.json')],
                'admission_bridge_sha256': evidence[str(bridge_path)], 'evidence_hashes': evidence,
                'candidate_source_sha256': selected['source_sha256'], 'source_hashes': expected,
                'scenarios': recipes, 'limits': LIMITS, 'implementation_hashes': implementation,
                'runner_sha256': file_sha256(Path(__file__)), 'native_evidence_hashes': native['evidence_hashes']}
    write_json(output / 'protocol.json', protocol)
    protocol_sha = file_sha256(output / 'protocol.json')
    write_json(output / 'original-files.json', snapshot)
    write_json(output / 'native-audit.json', native)
    for role, path in sources.items():
        copy_project(path, output / 'sources' / role)
    configure_environment()

    def check():
        if implementation_hashes(ROOT) != implementation or file_sha256(Path(__file__)) != protocol['runner_sha256']:
            raise ValueError('Recorder/evaluator implementation changed during validation')
        if file_sha256(output / 'protocol.json') != protocol_sha:
            raise ValueError('Supplemental protocol changed')
        for path, sha in evidence.items():
            if file_sha256(Path(path)) != sha:
                raise ValueError('Original evidence changed')
        for role, hashes in expected.items():
            if source_hashes(output / 'sources' / role) != hashes:
                raise ValueError('Frozen validation source changed')

    rows = []
    for scenario in recipes:
        sid = scenario['id']
        row = {'scenario': scenario, 'records': {}, 'errors': []}
        for role in ('reference', 'before', 'candidate'):
            print(f'{len(rows)+1}/12 {sid} {role}', flush=True)
            try:
                with _lock(Path(state['capture_lock']), timeout=30):
                    check()
                    ref = row['records'].get('reference')
                    record = _capture_web(output / 'sources' / role, output / 'captures' / sid / role,
                                          scenario, check, lambda *args: None, lambda: False, role,
                                          reference_selection=ref['selection'] if ref else None)
                if record['source_sha256'] != digest(expected[role]):
                    raise ValueError('Record belongs to another source')
                row['records'][role] = record
                write_json(output / 'records' / sid / (role + '.json'), record)
            except Exception as exc:
                row['errors'].append({'role': role, 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)})
        if not row['errors']:
            try:
                binding = read_json(Path(state['reference']) / 'capture-binding.json')
                binding['scenario'] = deepcopy(scenario)
                r = row['records']
                comparison = _compare(r['reference'], r['before'], r['candidate'], binding, require_improvement=True)
                row.update(comparison=comparison, functional_pass=all(x['functional']['status'] == 'pass' for x in r.values()))
                row['pass'] = comparison['pass'] and row['functional_pass']
            except Exception as exc:
                row['errors'].append({'role': 'comparison', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)})
        row.setdefault('pass', False)
        row['status'] = 'error' if row['errors'] else ('pass' if row['pass'] else 'fail')
        write_json(output / 'results' / (sid + '.json'), row)
        rows.append({'scenario_id': sid, 'status': row['status'], 'pass': row['pass'], 'errors': row['errors'],
                     'score': row.get('comparison', {}).get('score'),
                     'improvement': row.get('comparison', {}).get('improvement_fraction'),
                     'functional_pass': row.get('functional_pass')})
        write_json(output / 'progress.json', {'completed': len(rows), 'total': 12, 'results': rows})
        print(rows[-1], flush=True)
    check()
    actual = {p.relative_to(session).as_posix(): file_sha256(p) for p in session.rglob('*') if p.is_file()}
    if actual != snapshot:
        raise ValueError('Original sealed session changed during validation')
    if validate_sources(sources, state, selected) != expected:
        raise ValueError('Original source changed during validation')
    result = {'status': 'error' if any(r['errors'] for r in rows) else ('pass' if all(r['pass'] for r in rows) else 'fail'),
              'passed': sum(r['pass'] for r in rows), 'required': 12, 'results': rows,
              'candidate_source_sha256': selected['source_sha256'], 'native_audit_status': native['status'],
              'original_files_unchanged': len(snapshot), 'protocol_sha256': protocol_sha,
              'independent_visual_review': 'pending', 'certified_phase_success': False,
              'historical_verdicts_replaced': False, 'model_started': False, 'candidate_edited': False,
              'fresh_preregistered_model_trial': False, 'limits_changed': False}
    write_json(output / 'validation.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('session', 'original-protocol', 'reference-project', 'qualification', 'bridge-path', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = run(args.session, args.original_protocol, args.reference_project, args.qualification, args.bridge_path, args.output)
    print({'status': result['status'], 'passed': result['passed'], 'required': result['required']}, flush=True)
