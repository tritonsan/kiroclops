"""Replay one CLI session's source writes. Dry-run unless --write is explicit."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.integration.authorship import audit_kiro_authorship, write_authorship
from kiroclops.integration.ide_evidence import IDEExportError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--candidate", help="Defaults to the session's recorded selected candidate")
    parser.add_argument("--write", action="store_true", help="Write verified kiro/authorship.json; never replace an existing review")
    args = parser.parse_args(argv)
    try:
        record = audit_kiro_authorship(args.session, args.candidate)
        if args.write:
            output = write_authorship(args.session, record)
            print(json.dumps({"status": record["status"], "path": str(output)}, indent=2))
        else:
            print(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if record["status"] == "verified" else 2
    except (IDEExportError, OSError) as exc:
        print(json.dumps({"status": "error", "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
