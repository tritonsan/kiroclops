"""Run a supplemental integrity check on an explicitly disposable copy."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.movement_probe import run_probe, verify_probe, PROBES
from kiroclops.integration.storage import read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--disposable-root", required=True, type=Path)
    parser.add_argument("--expected-source-hashes", required=True, type=Path,
                        help="JSON containing the exact copied-project source hash map")
    parser.add_argument("--probe", choices=sorted(PROBES), default="air_idle_transition")
    parser.add_argument("--normal-level", action="store_true")
    parser.add_argument("--video", action="store_true")
    args = parser.parse_args()
    expected = read_json(args.expected_source_hashes)
    run_probe(args.project, args.output, disposable_root=args.disposable_root,
              expected_source_hashes=expected, probe=args.probe, normal_level=args.normal_level, video=args.video)
    print(json.dumps(verify_probe(args.project, args.output, expected_source_hashes=expected), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
