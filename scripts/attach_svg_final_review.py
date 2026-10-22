"""Attach completed image-bound review to the mutable supplemental report only."""
from pathlib import Path
import argparse
import json
import shutil
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.review import validate_independent_review


def run(review, aggregate):
    review, aggregate = Path(review).resolve(), Path(aggregate).resolve()
    summary = read_json(aggregate / 'summary.json')
    receipt = review / 'independent-review.json'
    reviewed = read_json(review / 'review-summary.json')
    assert reviewed['status'] == 'pass'
    assert reviewed['candidate_sha256'] == summary['candidate_sha256']
    assert reviewed['validation_sha256'] == summary['evidence']['validation']['sha256']
    validate_independent_review(receipt, candidate_sha256=summary['candidate_sha256'])
    inventory = read_json(review / 'inventory.json')
    expected = set()
    for entry in inventory['records']:
        assert file_sha256(entry['record_path']) == entry['record_sha256']
        record = read_json(entry['record_path'])
        obs = record['observations']
        expected.add((obs['package_sha256'], digest(obs)))
    assert len(expected) == 36
    assert expected == {tuple(x) for x in read_json(receipt)['required_observations']}
    report = aggregate / 'report'
    history = aggregate / 'before-final-visual-review'
    history.mkdir(exist_ok=False)
    for relative in ('summary.json', 'attempts.json', 'REPORT.md', 'report/report.json',
                     'report/index.html', 'report/receipt.json'):
        dest = history / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(aggregate / relative, dest)
    summary['status'] = 'supplemental_acceptance_pass'
    summary['independent_final_visual_review'] = 'pass'
    summary['final_visual_review'] = reviewed
    summary['evidence']['final_visual_review'] = {
        'path': receipt.relative_to(ROOT).as_posix(), 'sha256': file_sha256(receipt)}
    summary['limitations'][0] = ('All 36 holdout recordings have assistant computer-assisted raw-pixel review; '
                                 'this is not external human review or manual clicking of every coordinate.')
    write_json(aggregate / 'summary.json', summary)
    attempts = read_json(aggregate / 'attempts.json')
    attempts['attempts'][1].update(status=summary['status'], independent_visual_review='pass',
                                   independent_review_sha256=file_sha256(receipt))
    write_json(aggregate / 'attempts.json', attempts)
    data = read_json(report / 'report.json')
    old_data = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    page = (report / 'index.html').read_text(encoding='utf-8')
    assert page.count(old_data) == 1
    data['visual'] = ('Passed — 36 recordings, 5,999 position points, event and relationship review; '
                      'by Codex using an independent raw-pixel method, not external human review')
    data['visualReview'] = reviewed
    page = page.replace(old_data, json.dumps(data, ensure_ascii=False).replace('</', '<\\/'))
    (report / 'index.html').write_text(page, encoding='utf-8')
    write_json(report / 'report.json', data)
    old_receipt = read_json(report / 'receipt.json')
    old_receipt.update(independent_visual_review='pass',
        independent_review_sha256=file_sha256(receipt),
        report_sha256=file_sha256(report / 'report.json'), page_sha256=file_sha256(report / 'index.html'),
        previous_receipt_sha256=file_sha256(history / 'report/receipt.json'))
    write_json(report / 'receipt.json', old_receipt)
    print({'status': summary['status'], 'records': len(expected)}, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--review', required=True, type=Path)
    p.add_argument('--aggregate', required=True, type=Path)
    a = p.parse_args()
    run(a.review, a.aggregate)
