"""Audit one session-owned native Kiro IDE ZIP; do not launch Kiro or extract files."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.integration.ide_evidence import IDEExportError, _owned, _session, audit_ide_export


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="Explicit session ID or absolute session directory")
    parser.add_argument("--export", required=True, help="ZIP inside this session; relative paths use the session directory")
    parser.add_argument("--candidate", help="Explicit candidate ID to reconstruct from native writes")
    parser.add_argument("--agent", default="kiroclops-tools")
    parser.add_argument("--output", default="ide/evidence.json", help="New JSON file inside the session")
    args = parser.parse_args(argv)
    try:
        session = _session(args.session)
        output = _owned(session, args.output)
        if output.exists():
            raise IDEExportError("output_exists", "Refusing to overwrite existing audit evidence; choose a new --output")
        if output.suffix.lower() != ".json":
            raise IDEExportError("invalid_output", "Audit output must be a new JSON file")
        report = audit_ide_export(session, args.export, expected_agent=args.agent, candidate_id=args.candidate)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"path": str(output), "status": report["status"],
                          "authorship_status": report["authorship"]["status"],
                          "strict_configuration_verified": False}, indent=2))
        return 2 if report["status"] == "failed" else 0
    except (IDEExportError, OSError) as exc:
        print(json.dumps({"status": "error", "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
