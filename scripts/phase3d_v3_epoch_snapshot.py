"""Freeze one named source epoch of the repository, byte for byte.

Copies every source file of the selected repository folders, byte for byte, to a
new code root (default C:/kc3d/code/v2-final) and writes an epoch manifest with
SHA-256 digests. A zip archive and the same manifest are also written to a new
evidence folder. Existing folders are never overwritten.

After V3 changes the live repository, V2 evidence can still be verified against
this frozen copy (for example by running the V2 verify-only bridges from inside
the copied code root).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("kiroclops", "scripts", "tests", "adapters", "config", "services", "power", "patches",
           "package-resources", "docs", "fixtures", "diagnostics", "examples")
TOP_LEVEL_SUFFIXES = (".md", ".toml", ".lock", ".txt", ".cmd")
TOP_LEVEL_NAMES = (".gitignore",)


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _files():
    selected = []
    for folder in FOLDERS:
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                selected.append(path)
    for path in sorted(ROOT.iterdir()):
        if path.is_file() and (path.suffix in TOP_LEVEL_SUFFIXES or path.name in TOP_LEVEL_NAMES):
            selected.append(path)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--baseline-verify", type=Path,
                        help="Optional JSON file holding a verification output to bind into the epoch.")
    args = parser.parse_args(argv)
    for target in (args.code_root, args.evidence):
        if target.exists():
            raise SystemExit(f"Refusing to overwrite an existing folder: {target}")

    files = _files()
    manifest = {}
    for path in files:
        manifest[path.relative_to(ROOT).as_posix()] = _sha(path)

    args.code_root.mkdir(parents=True)
    for rel in manifest:
        destination = args.code_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, destination)
    copy_mismatches = [rel for rel, sha in manifest.items() if _sha(args.code_root / rel) != sha]
    if copy_mismatches:
        raise SystemExit(f"Copied bytes differ for {len(copy_mismatches)} files, e.g. {copy_mismatches[:3]}")
    live_changes = [rel for rel, sha in manifest.items() if _sha(ROOT / rel) != sha]
    if live_changes:
        raise SystemExit(f"Live sources changed during the snapshot, e.g. {live_changes[:3]}")

    epoch = {
        "kind": args.kind,
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": args.reason,
        "repository": str(ROOT),
        "folders": list(FOLDERS),
        "file_count": len(manifest),
        "source_files": manifest,
        "source_tree_sha256": _digest(manifest),
        "kiroclops_tree_sha256": _digest({k: v for k, v in manifest.items() if k.startswith("kiroclops/")}),
    }
    if args.baseline_verify:
        epoch["verification_before_freeze"] = json.loads(args.baseline_verify.read_text(encoding="utf-8"))
    (args.code_root / "epoch.json").write_text(json.dumps(epoch, indent=2, ensure_ascii=False), encoding="utf-8")

    args.evidence.mkdir(parents=True)
    archive = args.evidence / "sources.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for rel in manifest:
            bundle.write(ROOT / rel, rel)
    with zipfile.ZipFile(archive) as bundle:
        archived = {name: hashlib.sha256(bundle.read(name)).hexdigest() for name in bundle.namelist()}
    if archived != manifest:
        raise SystemExit("Archive contents differ from the manifest.")
    (args.evidence / "epoch.json").write_text(json.dumps(epoch, indent=2, ensure_ascii=False), encoding="utf-8")
    receipt = {
        "kind": "SourceSnapshotReceipt",
        "schema_version": 1,
        "epoch_sha256": _sha(args.evidence / "epoch.json"),
        "archive_sha256": _sha(archive),
        "code_root": str(args.code_root),
        "code_root_epoch_sha256": _sha(args.code_root / "epoch.json"),
        "producer_sha256": _sha(Path(__file__)),
        "file_count": len(manifest),
        "source_tree_sha256": epoch["source_tree_sha256"],
    }
    (args.evidence / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
