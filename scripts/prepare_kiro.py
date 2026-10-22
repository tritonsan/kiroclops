"""Configure a prepared KiroClops target without starting a model run."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.integration.kiro import configure_kiro


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--mode", choices=("tools", "baseline"), default="tools")
    parser.add_argument("--surface", choices=("cli", "ide"), default="cli")
    args = parser.parse_args()
    print(json.dumps(configure_kiro(args.session_dir, args.mode, surface=args.surface), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
