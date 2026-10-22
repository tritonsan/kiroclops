"""Build the local Phase 2 comparison from explicitly selected session IDs."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.integration.benchmark import generate_benchmark_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", nargs="+", required=True,
                        help="Explicit session IDs under ROOT/sessions; no automatic discovery")
    arguments = parser.parse_args(argv)
    result = generate_benchmark_report(arguments.sessions)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
