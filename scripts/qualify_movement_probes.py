"""Qualify supplementary integrity probes on preserved positive/negative controls."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.common import ROOT
from kiroclops.integration.movement_probe import run_probe, verify_probe
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, utc_now, write_json


def qualify(output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Use a fresh qualification directory; old failures are retained')
    old = read_json(ROOT / 'sessions/cli-ground-01/session.json')
    parent = ROOT / 'sessions/cli-ground-01/baseline'
    bad = ROOT / 'sessions/cli-ground-01/candidates/candidate-02/project'
    cases = [
        ('parent-air', parent, old['baseline_hashes'], 'air_idle_transition', False, 'pass'),
        ('known-bad-air', bad, old['candidates'][1]['source_hashes'], 'air_idle_transition', False, 'fail'),
        ('parent-combined-normal', parent, old['baseline_hashes'], 'run_jump_land_brake', True, 'pass'),
    ]
    result = {'schema_version': 1, 'created_at': utc_now(), 'status': 'running', 'checks': {},
              'script_sha256': sha256(Path(__file__)), 'scope': 'Independent supplementary probe qualification; no model invocation.'}
    output.mkdir(parents=True)
    write_json(output / 'result.json', result)
    for name, source, expected, probe, normal, verdict in cases:
        if project_hashes(source) != expected:
            raise ValueError(f'Control source changed: {name}')
        folder = output / name
        folder.mkdir()
        project = copy_project(source, folder / 'project')
        run = run_probe(project, folder / 'capture', disposable_root=folder,
                        expected_source_hashes=expected, probe=probe, normal_level=normal,
                        video=True)
        checked = verify_probe(project, run, expected_source_hashes=expected)
        item = {'project': str(project), 'output': str(run), 'source': str(source),
                'original_source_hashes': expected, 'expected_status': verdict,
                'actual': checked, 'source_preserved': project_hashes(source) == expected}
        item['status'] = 'pass' if checked['status'] == verdict and item['source_preserved'] else 'fail'
        result['checks'][name] = item
        write_json(output / 'result.json', result)
        print(name, checked['status'], checked.get('checks', {}), flush=True)
        if item['status'] != 'pass':
            result['status'] = 'fail'
            write_json(output / 'result.json', result)
            return 1
    result.update(status='pass', finished_at=utc_now())
    write_json(output / 'result.json', result)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    raise SystemExit(qualify(parser.parse_args().output))
