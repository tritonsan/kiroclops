"""Apply explicit independent visual observations to a sealed Phase 2B run."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.ground_review import review_session, verify_review_receipt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session',type=Path,required=True)
    p.add_argument('--controls',type=Path)
    p.add_argument('--source',choices=['human','assistant_visual'],default='human')
    p.add_argument('--reuse-ground-controls',type=Path,nargs='*')
    p.add_argument('--reuse-jump-controls',type=Path)
    p.add_argument('--verify-only',action='store_true')
    a=p.parse_args()
    result=(verify_review_receipt(a.session) if a.verify_only else
        review_session(a.session,a.controls,source=a.source,
            reuse_ground_controls_from=a.reuse_ground_controls,
            reuse_jump_controls_from=a.reuse_jump_controls))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
