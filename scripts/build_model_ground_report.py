"""Package only the preregistered Sol CLI cohort; no model or engine is started."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.model_ground_benchmark_v1 import generate_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", nargs="+", help="Explicit subset of the three preregistered Sol sessions")
    args = parser.parse_args(argv)
    print(generate_report(args.sessions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
