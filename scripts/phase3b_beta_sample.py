"""Prepare a relocatable public-fixture request, without credentials/results."""
from pathlib import Path
import argparse
import shutil

from kiroclops.visual.package import load_package
from kiroclops.visual.schema import read_json, write_json, file_sha256


def prepare(output):
    output = Path(output)
    if output.is_absolute():
        raise ValueError('Use a path relative to the directory where the CLI will run')
    source = Path('artifacts/phase3b/calibration-corpus-02/panel-open-w960-r1')
    before = load_package(source / 'package')
    if output.exists():
        raise ValueError('Choose a new directory; preserve existing data')
    output.mkdir(parents=True)
    shutil.copytree(source / 'package', output / 'package')
    for name in ('selection.json', 'contract.json'):
        shutil.copyfile(source / name, output / name)
    assert load_package(output / 'package')['package_sha256'] == before['package_sha256']
    write_json(output / 'request.json', {'package_path': (output / 'package').as_posix(),
        'selection': read_json(output / 'selection.json'), 'contract': read_json(output / 'contract.json'),
        'candidate_sha256': None, 'retention_class': 'reference'})
    write_json(output / 'sample-receipt.json', {'package_sha256': before['package_sha256'],
        'request_sha256': file_sha256(output / 'request.json'),
        'scope': 'Bundled panel calibration fixture; no prior result, user data, credential, or source code.',
        'request_path_policy': 'Run the CLI from the parent of the supplied relative output path.'})
    print(output / 'request.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    prepare(parser.parse_args().output)
