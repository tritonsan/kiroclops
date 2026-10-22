"""Reuse prior visual inspection only for byte-identical complete image boards.

This transfers neither measurements nor acceptance. New/changed boards still
require actual viewing. The bound prior journal and image must remain intact.
"""
from pathlib import Path
import argparse
import hashlib
import json
import tempfile
import unittest


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(prior, current):
    prior, current = Path(prior).resolve(), Path(current).resolve()
    assert prior != current
    origin = prior / 'visual-inspection-progress.json'
    prior_hash = sha(origin)
    previous = json.loads(origin.read_text(encoding='utf-8'))
    pool = {}
    for row in previous['viewed']:
        path = (prior / row['file']).resolve()
        assert path.is_relative_to(prior) and path.is_file()
        assert sha(path) == row['sha256'], 'Prior inspected board changed'
        pool.setdefault(row['sha256'], row['file'])
    dest = current / 'visual-inspection-progress.json'
    journal = json.loads(dest.read_text(encoding='utf-8')) if dest.exists() else {
        'status': 'in_progress', 'reviewer': 'Codex assistant',
        'external_human_review': False, 'viewed': []}
    recorded = {r['file']: r for r in journal['viewed']}
    for name, row in recorded.items():
        assert sha(current / name) == row['sha256'], 'Current inspected board changed'
    added = 0
    for path in sorted(current.rglob('*.png')):
        assert path.resolve().is_relative_to(current)
        name = path.relative_to(current).as_posix()
        if name in recorded:
            continue
        digest = sha(path)
        if digest not in pool:
            continue
        journal['viewed'].append({'file': name, 'sha256': digest,
            'inspection_reuse': {'prior_journal': str(origin),
                'prior_journal_sha256': prior_hash, 'prior_file': pool[digest],
                'method': 'byte_identical_complete_board',
                'new_visual_inspection_claimed': False,
                'measurement_or_acceptance_transferred': False}})
        added += 1
    assert sha(origin) == prior_hash
    dest.write_text(json.dumps(journal, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return {'reused_boards': added, 'journal_total': len(journal['viewed']),
            'acceptance_granted': False}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = Path(self.tmp.name)/'old'; self.old.mkdir()
        self.new = Path(self.tmp.name)/'new'; self.new.mkdir()
        (self.old/'a.png').write_bytes(b'raw bytes')
        (self.old/'visual-inspection-progress.json').write_text(json.dumps({
            'viewed': [{'file': 'a.png', 'sha256': sha(self.old/'a.png')}]}))

    def test_exact_different_filename_reused(self):
        (self.new/'b.png').write_bytes(b'raw bytes')
        self.assertEqual(run(self.old,self.new)['reused_boards'],1)
        self.assertEqual(run(self.old,self.new)['reused_boards'],0)

    def test_changed_pixels_not_reused(self):
        (self.new/'a.png').write_bytes(b'changed')
        self.assertEqual(run(self.old,self.new)['reused_boards'],0)

    def test_changed_prior_rejected(self):
        (self.old/'a.png').write_bytes(b'changed')
        with self.assertRaises(AssertionError): run(self.old,self.new)

    def test_changed_current_rejected(self):
        (self.new/'a.png').write_bytes(b'raw bytes');run(self.old,self.new)
        (self.new/'a.png').write_bytes(b'changed')
        with self.assertRaises(AssertionError): run(self.old,self.new)

    def test_uninspected_prior_not_reused(self):
        (self.old/'c.png').write_bytes(b'uninspected')
        (self.new/'c.png').write_bytes(b'uninspected')
        self.assertEqual(run(self.old,self.new)['reused_boards'],0)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prior',type=Path);p.add_argument('--current',type=Path)
    p.add_argument('--self-test',action='store_true');a=p.parse_args()
    if a.self_test:
        raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Tests)).wasSuccessful())
    assert a.prior and a.current
    print(run(a.prior,a.current))
