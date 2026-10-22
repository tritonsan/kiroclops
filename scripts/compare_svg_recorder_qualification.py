"""Evaluate existing reference pairs after a runner binding error; never recapture.

The original capture protocol and failed runner results are retained. This
replay changes only the missing input-provenance/claim binding, not thresholds.
"""
from pathlib import Path
from copy import deepcopy
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.visual.package import load_package
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine.evaluation import _compare_web
from kiroclops.local_engine.media_session import _compare, LIMITS, VIEWPORT_ALIGNMENT
from scripts.validate_panel_recorder import implementation_hashes


def binding_for(record, scenario):
    provenance = record['observations']['inputs']['provenance']
    if provenance == 'unknown':
        raise ValueError('Reference repeatability needs known input receipts')
    return {'scenario': deepcopy(scenario), 'spatial_alignment': VIEWPORT_ALIGNMENT,
            'lifecycle_bindings': record['selection']['lifecycle_bindings'],
            'connections': record['selection']['connections'],
            'input_provenance': provenance,
            'claim': 'conditional_on_user_declared_inputs' if provenance == 'user_declared' else 'recorded_input_response'}


def run(capture, output):
    capture, output = Path(capture).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Preserve previous evaluation')
    protocol = read_json(capture / 'protocol.json')
    original = read_json(capture / 'result.json')  # Require capture completion.
    if original['protocol_sha256'] != file_sha256(capture / 'protocol.json'):
        raise ValueError('Original protocol changed')
    if protocol['limits'] != LIMITS or protocol['implementation_hashes'] != implementation_hashes(ROOT):
        raise ValueError('Frozen thresholds or implementation changed')
    if protocol['source_hashes'] != source_hashes(capture / 'source'):
        raise ValueError('Reference source changed')
    if len(protocol['scenarios']) != 8 or protocol['repeats'] != 2:
        raise ValueError('Wrong qualification scope')
    output.mkdir(parents=True)
    rows = []
    for scenario in protocol['scenarios']:
        sid = scenario['id']
        original_row = read_json(capture / 'results' / (sid + '.json'))
        row = {'scenario_id': sid, 'pass': False, 'errors': [], 'record_hashes': {}}
        # Preserve genuine capture errors. A repeat is never substituted.
        capture_errors = [e for e in original_row['errors'] if e.get('role') != 'comparison']
        if capture_errors:
            row['errors'] = capture_errors
        else:
            records = []
            for repeat in (1, 2):
                path = capture / 'records' / sid / (str(repeat) + '.json')
                record = read_json(path)
                manifest = load_package(record['package'])
                if (record['source_sha256'] != digest(protocol['source_hashes']) or
                        record['observations']['package_sha256'] != manifest['package_sha256'] or
                        record['scenario'] != scenario):
                    raise ValueError('Source, recording, or scenario binding changed')
                row['record_hashes'][str(repeat)] = file_sha256(path)
                records.append(record)
            a, b = records
            row['input_window_comparison'] = _compare(a, a, b, binding_for(a, scenario), require_improvement=False)
            row['benchmark_comparison'] = _compare_web(a, a, b, improvement=False)
            row['functional_pass'] = all(r['functional']['status'] == 'pass' for r in records)
            row['pass'] = (row['input_window_comparison']['pass'] and row['benchmark_comparison']['pass'] and row['functional_pass'])
        write_json(output / 'results' / (sid + '.json'), row)
        summary = {k: row[k] for k in ('scenario_id', 'pass', 'errors')}
        summary.update(input_window_pass=row.get('input_window_comparison', {}).get('pass'),
                       benchmark_pass=row.get('benchmark_comparison', {}).get('pass'))
        rows.append(summary)
        print(summary, flush=True)
    result = {'status': 'pass' if all(r['pass'] for r in rows) else 'fail',
              'passed': sum(r['pass'] for r in rows), 'required': 8, 'rows': rows,
              'original_protocol_sha256': file_sha256(capture / 'protocol.json'),
              'original_result_sha256': file_sha256(capture / 'result.json'),
              'replay_script_sha256': file_sha256(__file__),
              'repair': 'Populate provenance from actual InputTimeline and its corresponding claim; no numeric policy change.',
              'recaptured': False, 'limits_changed': False, 'model_started': False,
              'production_mode_promoted': False, 'independent_visual_review': 'pending'}
    write_json(output / 'result.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print(run(a.capture, a.output)['status'])
