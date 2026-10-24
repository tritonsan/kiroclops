"""Build the public, English-only evidence subset of the KiroClops repository.

Reads frozen evidence from the engineering workspace and the C:/kc3d evidence
roots and writes small files under <output>/evidence:

* files that are already English and carry no personal path or AWS account
  number are copied byte for byte;
* files that carry a local user path or an AWS account number are copied with
  those values masked (``%USERPROFILE%``, ``<account>``);
* files with Turkish text are not copied (their facts are summarised in the
  technical report);
* the V6 panel held-out outcome is extracted per case from the finalize
  comparison, and the source epochs are listed by tree hash.

MANIFEST.json records, for every written file, its source path, the source's
SHA-256, the written file's SHA-256 and the transformation applied, so every
public number stays traceable to the frozen evidence it came from.

    python scripts/build_public_evidence.py --output <publication root>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

LIVE = Path('D:/Amazon Developer Hackathon')
KC3D = Path('C:/kc3d')
TURKISH = re.compile('[\u011f\u011e\u00fc\u00dc\u015f\u015e\u0131\u0130\u00f6\u00d6\u00e7\u00c7]')
USER_PATH = re.compile(r'(?i)C:(\\\\|\\|/)Users(\\\\|\\|/)[^\\/"\s]+')
ACCOUNT = re.compile(r'(arn:aws:[a-z0-9-]*:[a-z0-9-]*:|amazonaws\.com/|kiroclops-probe-)\d{12}')

ITEMS = [
    # (public path, source path, report section)
    ('closures/game-v4g01-closure-02.summary.json', KC3D/'r/v4g01-closure-02/summary.json', 'Summary, 6.2'),
    ('closures/svg-v5s01c-closure-01.summary.json', KC3D/'r/v5s01c-closure-01/summary.json', 'Summary, 6.2'),
    ('svg-focus-ring-blind-qualification.json', KC3D/'r/v5s-ring-score-01/result.json', '3.1'),
    ('opencv5-symbol-diff/README.md', LIVE/'artifacts/phase4-v6/opencv-symbol-diff-01/README.md', '3.2'),
    ('opencv5-symbol-diff/diff.json', LIVE/'artifacts/phase4-v6/opencv-symbol-diff-01/diff.json', '3.2'),
    ('opencv5-symbol-diff/opencv-4.14.0-symbols.json', LIVE/'artifacts/phase4-v6/opencv-symbol-diff-01/opencv-4.14.0-symbols.json', '3.2'),
    ('opencv5-symbol-diff/opencv-5.0.0-symbols.json', LIVE/'artifacts/phase4-v6/opencv-symbol-diff-01/opencv-5.0.0-symbols.json', '3.2'),
    ('subpixel-benchmark/REPORT.md', LIVE/'artifacts/phase4-v6/subpixel-benchmark-01/REPORT.md', '3.3'),
    ('subpixel-benchmark/report.json', LIVE/'artifacts/phase4-v6/subpixel-benchmark-01/report.json', '3.3'),
    ('marks-error/REPORT.md', LIVE/'artifacts/phase4-v6/marks-error-01/REPORT.md', '3.3'),
    ('marks-error/report.json', LIVE/'artifacts/phase4-v6/marks-error-01/report.json', '3.3'),
    ('feature-cross-check/panel/REPORT.md', LIVE/'artifacts/phase4-v6/cross-check-01/panel-cal/REPORT.md', '3.3'),
    ('feature-cross-check/panel/report.json', LIVE/'artifacts/phase4-v6/cross-check-01/panel-cal/report.json', '3.3'),
    ('feature-cross-check/svg/REPORT.md', LIVE/'artifacts/phase4-v6/cross-check-01/svg-cal/REPORT.md', '3.3'),
    ('feature-cross-check/svg/report.json', LIVE/'artifacts/phase4-v6/cross-check-01/svg-cal/report.json', '3.3'),
    ('v6-tracker-equivalence/panel/REPORT.md', LIVE/'artifacts/phase4-v6/equivalence-01/panel-cal/REPORT.md', '3.3'),
    ('v6-tracker-equivalence/panel/report.json', LIVE/'artifacts/phase4-v6/equivalence-01/panel-cal/report.json', '3.3'),
    ('v6-tracker-equivalence/svg/REPORT.md', LIVE/'artifacts/phase4-v6/equivalence-01/svg-cal/REPORT.md', '3.3'),
    ('v6-tracker-equivalence/svg/report.json', LIVE/'artifacts/phase4-v6/equivalence-01/svg-cal/report.json', '3.3'),
    ('lightglue-sweep/main.json', LIVE/'artifacts/phase4-v6/lightglue-sweep-01/results/main.json', '3.2'),
    ('lightglue-sweep/order-1.json', LIVE/'artifacts/phase4-v6/lightglue-sweep-01/results/order-1.json', '3.2'),
    ('lightglue-sweep/order-2.json', LIVE/'artifacts/phase4-v6/lightglue-sweep-01/results/order-2.json', '3.2'),
    ('inspect-window-post-freeze/result.json', LIVE/'artifacts/phase4-v6/post-freeze-inspect-window-01/result.json', '6.4'),
    ('tests/full-tests-02.result.json', LIVE/'artifacts/phase4-v6/full-tests-02/result.json', '6.4'),
    ('tests/full-tests-03.result.json', LIVE/'artifacts/phase4-v6/full-tests-03/result.json', '6.4'),
    ('aws/speed/REPORT.md', LIVE/'artifacts/phase4-aws/speed-01/REPORT.md', '5.2'),
    ('aws/speed/report.json', LIVE/'artifacts/phase4-aws/speed-01/report.json', '5.2'),
    ('aws/cool-equivalence/result.json', LIVE/'artifacts/phase4-aws/v6-cool-equivalence-02/result.json', '5.2'),
    ('aws/cool-equivalence/posthoc-runtime-excluded.json', LIVE/'artifacts/phase4-aws/v6-cool-equivalence-02/posthoc-runtime-excluded.json', '5.2'),
    ('aws/standard-equivalence/result.json', LIVE/'artifacts/phase4-aws/v6-equivalence-01/result.json', '5.2'),
    ('aws/wake/result.json', LIVE/'artifacts/phase4-aws/live-cool-wake-02/result.json', '5.2'),
    ('aws/speed-posthoc/result.json', LIVE/'artifacts/phase4-aws/speed-01-posthoc-01/result.json', '5.2'),
    ('aws/cool-kernels/REPORT.md', LIVE/'artifacts/phase4-aws/cool-kernels-01/REPORT.md', '5.2'),
    ('aws/cool-kernels/summary.json', LIVE/'artifacts/phase4-aws/cool-kernels-01/summary.json', '5.2'),
    ('aws/cool-kernels/runs/result-cool-1.json', LIVE/'artifacts/phase4-aws/cool-kernels-01/runs/result-cool-1.json', '5.2'),
    ('aws/cool-kernels/runs/result-cool-2.json', LIVE/'artifacts/phase4-aws/cool-kernels-01/runs/result-cool-2.json', '5.2'),
    ('aws/cool-kernels/runs/result-cool-3.json', LIVE/'artifacts/phase4-aws/cool-kernels-01/runs/result-cool-3.json', '5.2'),
    ('aws/cool-kernels/runs/result-stock-1.json', LIVE/'artifacts/phase4-aws/cool-kernels-01/runs/result-stock-1.json', '5.2'),
    ('aws/cool-kernels/runs/result-stock-2.json', LIVE/'artifacts/phase4-aws/cool-kernels-01/runs/result-stock-2.json', '5.2'),
    ('aws/cool-kernels/runs/result-stock-3.json', LIVE/'artifacts/phase4-aws/cool-kernels-01/runs/result-stock-3.json', '5.2'),
    ('v6-agent-baseline/result.json', LIVE/'artifacts/phase4-v6/agent-baseline-posthoc-01/result.json', '4.2'),
    ('agent-sessions/REPORT.md', LIVE/'artifacts/phase4-v6/agent-sessions-posthoc-01/REPORT.md', '4.2'),
    ('agent-sessions/result.json', LIVE/'artifacts/phase4-v6/agent-sessions-posthoc-01/result.json', '4.2'),
    ('aws/cool-scaling/REPORT.md', LIVE/'artifacts/phase4-aws/cool-scaling-01/REPORT.md', '5.2'),
    ('aws/cool-scaling/summary.json', LIVE/'artifacts/phase4-aws/cool-scaling-01/summary.json', '5.2'),
    ('aws/judge-dryrun/result.json', LIVE/'artifacts/phase4-aws/judge-dryrun-01/result.json', '5.3'),
    ('aws/judge-dryrun/judge-check-01-svg.json', LIVE/'artifacts/phase4-aws/judge-dryrun-01/judge-check-01-svg.json', '5.3'),
    ('aws/judge-dryrun/judge-check-02-game-compare-local.json', LIVE/'artifacts/phase4-aws/judge-dryrun-01/judge-check-02-game-compare-local.json', '5.3'),
    ('tests/public-tree-tests.log', Path('C:/kc3d/publish/public-tree-tests.log'), '6.4'),
]
# Every round of the scaling benchmark, and the cross-platform LightGlue reproduction (outputs, backtraces, scripts).
ITEMS += [(f'aws/cool-scaling/{run.parent.parent.name}/{run.name}', run, '5.2')
          for run in sorted((LIVE/'artifacts/phase4-aws/cool-scaling-01').glob('*/runs/result-*.json'))]
ITEMS += [(f'lightglue-platforms/{path.name}', path, '3.2')
          for path in sorted((LIVE/'artifacts/phase4-v6/lightglue-platforms-01').glob('*')) if path.is_file()]
HELDOUT_JOB = KC3D/'s/v6p01/measurements/d19d9b76a015463fbf9708cb4fbde348'
EPOCHS = ('v4-final', 'v5b', 'v6', 'v6b', 'v6c')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(root, rel, data, manifest, **record):
    path = root/rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    manifest[rel] = {**record, 'sha256': sha(data)}


def heldout_extract():
    rows = json.loads((HELDOUT_JOB/'comparison.json').read_text(encoding='utf-8'))
    cases = []
    for row in rows:
        failing = [{'object': w['reference_id'], 'window': w['window_id'], 'position_rmse': round(w['position_rmse'], 4),
                    'rmse_limit': round(w['limits']['rmse_limit'], 4)}
                   for w in row['pixel_comparison']['object_windows']
                   if w.get('position_rmse') is not None and w['position_rmse'] > w['limits']['rmse_limit']]
        cases.append({'case_id': row['case_id'], 'role': row.get('role'), 'pass': row.get('pass'),
                      'score': round(row['score'], 5) if isinstance(row.get('score'), (int, float)) else None,
                      'failure_class': row.get('failure_class'), 'failing_windows': failing})
    return {'kind': 'V6PanelHeldOutOutcome', 'session': 'v6p01', 'job': HELDOUT_JOB.name, 'candidate': 'candidate-02',
            'cases': len(cases), 'passed': sum(1 for c in cases if c['pass']),
            'by_role': {role: {'cases': sum(1 for c in cases if c['role'] == role), 'passed': sum(1 for c in cases if c['role'] == role and c['pass'])}
                        for role in sorted({c['role'] for c in cases})},
            'rows': cases}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path, required=True, help='publication root; evidence/ is created inside')
    args = parser.parse_args(argv)
    root = args.output/'evidence'
    if root.exists():
        raise SystemExit(f'Refusing to overwrite {root}')
    manifest, skipped = {}, []
    for rel, source, section in ITEMS:
        data = source.read_bytes()
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            skipped.append({'path': rel, 'source': str(source), 'reason': 'not UTF-8 text'})
            continue
        if TURKISH.search(text):
            skipped.append({'path': rel, 'source': str(source), 'reason': 'contains Turkish text'})
            continue
        masked = ACCOUNT.sub(r'\1<account>', USER_PATH.sub('%USERPROFILE%', text))
        transform = 'copied' if masked == text else 'masked user path or AWS account number'
        write(root, rel, masked.encode('utf-8'), manifest, source=str(source), source_sha256=sha(data),
              transform=transform, report_section=section)
    comparison = HELDOUT_JOB/'comparison.json'
    write(root, 'panel-v6p01/heldout-outcome.json', json.dumps(heldout_extract(), indent=1).encode('utf-8'), manifest,
          source=str(comparison), source_sha256=sha(comparison.read_bytes()), transform='extracted per case', report_section='6.2')
    epochs = {}
    for name in EPOCHS:
        epoch_file = KC3D/'code'/name/'epoch.json'
        epoch = json.loads(epoch_file.read_text(encoding='utf-8'))
        epochs[name] = {'kind': epoch.get('kind'), 'created_at': epoch.get('created_at'), 'file_count': epoch.get('file_count'),
                        'source_tree_sha256': epoch.get('source_tree_sha256'), 'kiroclops_tree_sha256': epoch.get('kiroclops_tree_sha256'),
                        'epoch_json_sha256': sha(epoch_file.read_bytes())}
    write(root, 'source-epochs.json', json.dumps(epochs, indent=1).encode('utf-8'), manifest,
          source=str(KC3D/'code'), source_sha256=None, transform='tree hashes of frozen source epochs', report_section='6.4, 8')
    for rel, entry in manifest.items():
        if TURKISH.search((root/rel).read_text(encoding='utf-8')):
            raise SystemExit(f'Turkish text left in {rel}')
    (root/'MANIFEST.json').write_text(json.dumps({'kind': 'PublicEvidenceManifest', 'files': manifest, 'not_copied': skipped},
                                                 indent=1), encoding='utf-8')
    print(json.dumps({'written': len(manifest), 'not_copied': skipped}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
