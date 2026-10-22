"""Prepare one shared raw reference corpus before any cohort model runs.

Usage: .venv-local/Scripts/python.exe -B scripts/prepare_local_web_references.py
         --output artifacts/phase3a/web-references-01
Verify: ... --verify artifacts/phase3a/web-references-01/manifest.json
Existing and failed outputs are never overwritten. No model/analysis is run.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.local_engine.web_reference import prepare_reference_pack, verify_reference_pack

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path)
    group.add_argument('--verify', type=Path)
    parser.add_argument('--capture-policy',choices=['qualified-natural-20260927'])
    args = parser.parse_args()
    if args.verify:
        result = verify_reference_pack(args.verify)
        print(json.dumps({'status': 'pass', 'records': result['verified_records'],
            'manifest_sha256': result['manifest_sha256']}))
    else:
        print(json.dumps(prepare_reference_pack(args.output,capture_policy=args.capture_policy), ensure_ascii=False))
