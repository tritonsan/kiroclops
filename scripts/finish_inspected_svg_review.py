"""Bind an actually inspected SVG kit, check frozen evidence, then publish.

This does not mark images as inspected and cannot skip the visual journal.
Production sources and the original trial outcomes stay unchanged.
"""
from contextlib import ExitStack
from pathlib import Path
import argparse
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.local_engine import evidence
from kiroclops.visual.schema import read_json, write_json, file_sha256
from bind_final_svg_pixels import run as bind
from review_browser_with_staged_policy import load_verified_helper
from publish_completed_visual_review import run as publish


def main(name):
    assert name in ('svg-tools-02', 'svg-tools-03')
    base = ROOT/'artifacts/phase3a'
    review = base/'completion-postcohort-reviews-01'/f'{name}-svg'
    aid = base/f'completion-{name}-raw-review-01'
    receipt = base/'completion-20260927'/f'{name}-visual-review-publication.json'
    state_path = aid/'completion-check-progress.json'
    assert not state_path.exists() and not receipt.exists()
    sources = [Path(__file__), ROOT/'scripts/bind_final_svg_pixels.py',
               ROOT/'scripts/review_browser_with_staged_policy.py',
               ROOT/'scripts/publish_completed_visual_review.py']
    hashes = {str(p): file_sha256(p) for p in sources}
    state = {'status': 'binding_actual_inspection', 'sources': hashes,
             'new_model_run': False, 'primary_outcomes_modified': False}
    write_json(state_path, state)
    bound = bind(review, aid)
    state.update(status='checking_frozen_kit', binding=bound)
    write_json(state_path, state)
    helper, reader, bound_sources = load_verified_helper()
    with ExitStack() as stack:
        stack.enter_context(patch.object(evidence, 'required_observation_bindings', reader))
        stack.enter_context(patch.object(helper, 'required_observation_bindings', reader))
        stack.enter_context(patch.object(helper, 'helper_sources', bound_sources))
        checked = helper.check(review, review/'completed-annotations',
                               Path(read_json(review/'preparation.json')['session'])/'source_review.json')
    assert all(file_sha256(p)==sha for p,sha in hashes.items())
    assert checked['visual_status']=='pass', checked['visual_status']
    published = publish(review, receipt)
    state.update(status='published', receipt_sha256=file_sha256(receipt),
                 visual_status=published['visual_status'],
                 source_native_status=published['source_native_status'])
    write_json(state_path, state)
    print({k:state[k] for k in ('status', 'visual_status', 'source_native_status')}, flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', choices=('svg-tools-02','svg-tools-03'))
    main(parser.parse_args().name)
