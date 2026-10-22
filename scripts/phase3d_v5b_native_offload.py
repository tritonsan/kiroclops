"""V5b native reader extension: shell output that Kiro offloaded to a file.

PHASE3D_V5B_PLAN.md §2.4 (user decision, 13 October 2026, fixed before any V5b run).

Kiro 2.27 moves any tool output above 30,000 characters to a file under
``~/.kiro/sessions/<id>/<session>/tool-outputs/`` and gives the model a preview.
The pinned 2.27 reader (``kiroclops/local_engine/review.py``) verifies such an
``execute_pwsh`` envelope only through ``observed_output_pins`` of its pinned v3
proof, which holds a single inspected p04 output. In V5, one agent listing of the
evidence folder (801,378 characters) therefore failed the whole native audit.

Two steps, both mechanical:

``preserve``
    Right after a run, byte-copies every offloaded file named in the session's
    native stream into a fresh repository folder and writes a manifest (tool call
    id, original path, archive path, SHA-256, size). Nothing is selected by hand.
``offload_policy``
    While the unchanged canonical audit runs, a manifest entry turns into one
    extra pin {tool_call_id, digest of the call's native updates, original path,
    archive, sha256} on the pinned v3 proof, for the duration of the reader's own
    ``_builtin_227_v3_offload_output`` call only. That unchanged function still
    checks everything:

    - the envelope fields and the session's tool-outputs path pattern;
    - the 32 MiB bound;
    - that the original file still exists and equals the archive byte for byte;
    - the character count;
    - the ``Output:`` / ``Exit Code: 0`` frame;
    - the preview clamp and the exact offload message template.

    The projected envelope then passes the ordinary V2 shell rules.

Calls not in the manifest go to the reader unchanged. Failed, denied, timed-out
and background calls are not touched. The pinned proof file is never rewritten,
and the original functions are restored even on error. The policy composes with
the V5 prevented-denial policy, the receipted one-server environment and the
2.27.1 release identity mapping.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import functools
import hashlib
import json
import ntpath
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import review
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_native_release_identity as identity
from scripts import phase3d_v5_native_denial as denial

POLICY_ID = 'v5b-offloaded-shell-output-v1'
MANIFEST_KIND = 'PreservedKiroOffloadedToolOutputs'
ROOT = Path(__file__).resolve().parents[1]


def _need(condition, message, code='offload_evidence_unproven'):
    if not condition:
        raise VisualError(code, message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def offloaded_calls(inbound):
    """[(tool call id, original path)] of every tool call whose final update is a Kiro offload envelope."""
    updates = defaultdict(list)
    for event in inbound:
        update = denial._update(event)
        if update.get('sessionUpdate') in ('tool_call', 'tool_call_update') and isinstance(update.get('toolCallId'), str):
            updates[update['toolCallId']].append(update)
    found = []
    for call_id, items in updates.items():
        meta = items[-1].get('_meta')
        change = ((meta or {}).get('kiro') or {}).get('outputTransformation') if isinstance(meta, dict) else None
        if isinstance(change, dict) and change.get('kind') == 'offloaded' and isinstance(change.get('absFilePath'), str):
            found.append((call_id, change['absFilePath']))
    return found


def preserve(session, output, *, root=ROOT):
    """Byte-copy every offloaded output of ``session`` into the fresh repository folder ``output``."""
    session, root = _ordinary(session), Path(root).resolve()
    output = Path(output)
    output = (root / output) if not output.is_absolute() else output
    _need(not output.exists(), 'Preserve offloads into a fresh folder.', 'output_exists')
    _need(output.resolve().is_relative_to(root), 'The archive must lie inside the repository.', 'unsafe_path')
    inbound = [json.loads(line) for line in (session / 'kiro/run/stdout.jsonl').read_text('utf-8').splitlines() if line.strip()]
    calls = offloaded_calls(inbound)
    names = [ntpath.basename(path) for _, path in calls]
    _need(len(set(names)) == len(names), 'Offloaded file names are not unique.')
    output.mkdir(parents=True)
    files = []
    for (call_id, original), name in zip(calls, names):
        data = Path(original).read_bytes()
        (output / name).write_bytes(data)
        _need((output / name).read_bytes() == data, 'The archive copy differs from the original.')
        files.append({'tool_call_id': call_id, 'original_path': original,
                      'archive': (output / name).resolve().relative_to(root).as_posix(),
                      'sha256': _sha(data), 'bytes': len(data)})
    manifest = {'kind': MANIFEST_KIND, 'schema_version': 1, 'policy_id': POLICY_ID, 'session': str(session),
                'copied_at_utc': datetime.now(timezone.utc).isoformat(), 'files': files,
                'producer_sha256': file_sha256(Path(__file__))}
    write_json(output / 'manifest.json', manifest)
    return manifest


def load_manifest(path, sha256, *, root=ROOT):
    """The preserved manifest, checked against its pinned sha256 and its archived bytes."""
    path, root = _ordinary(path), Path(root).resolve()
    _need(isinstance(sha256, str) and file_sha256(path) == sha256, 'The offload manifest changed or is missing.')
    value = read_json(path)
    _need(value.get('kind') == MANIFEST_KIND and value.get('schema_version') == 1 and value.get('policy_id') == POLICY_ID
          and isinstance(value.get('files'), list), 'Not a V5b offload manifest.')
    seen = set()
    for entry in value['files']:
        _need(isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ('tool_call_id', 'original_path', 'archive', 'sha256')),
              'Malformed offload manifest entry.')
        _need(entry['original_path'] not in seen, 'Duplicate offloaded path.')
        seen.add(entry['original_path'])
        archive = (root / entry['archive']).resolve()
        _need(archive.is_relative_to(root) and archive.is_file() and _sha(archive.read_bytes()) == entry['sha256'],
              'An archived offload changed or is missing: ' + entry['archive'])
    return value


@contextmanager
def offload_policy(manifest, manifest_sha256, *, root=ROOT):
    """Let the unchanged reader verify manifest offloads through a per-call pin; yields the ledger."""
    value = load_manifest(manifest, manifest_sha256, root=root)
    entries = {entry['original_path']: entry for entry in value['files']}
    original_offload, original_proof = review._builtin_227_v3_offload_output, review._builtin_227_v3_proof
    ledger = []

    @functools.wraps(original_offload)
    def offload_output(ident, updates, session_id, policy):
        try:
            name = updates[-1]['_meta']['kiro']['outputTransformation']['absFilePath']
        except (KeyError, TypeError, IndexError, AttributeError):
            name = None
        entry = entries.get(name) if isinstance(name, str) else None
        if entry is None or entry['tool_call_id'] != ident:
            return original_offload(ident, updates, session_id, policy)
        saved = original_proof(policy)
        if saved is None:
            return None
        pin = {'tool_call_id': ident, 'native_updates_sha256': digest(updates), 'original_path': name,
               'archive': entry['archive'], 'sha256': entry['sha256']}
        extended = {**saved, 'observed_output_pins': [*saved.get('observed_output_pins', []), pin]}

        def proof(candidate):
            return extended if candidate is policy else original_proof(candidate)

        review._builtin_227_v3_proof = proof
        try:
            result = original_offload(ident, updates, session_id, policy)
        finally:
            review._builtin_227_v3_proof = original_proof
        ledger.append({'tool_call_id': ident, 'archive': entry['archive'], 'sha256': entry['sha256'],
                       'verified': result is not None})
        return result

    review._builtin_227_v3_offload_output = offload_output
    try:
        yield ledger
    finally:
        review._builtin_227_v3_offload_output = original_offload


def native_stack(session, *, receipt=None, receipt_sha256=None, offloads=None, offloads_sha256=None):
    """Context manager for the V5b stack; yields (environment, denial ledger, offload ledger)."""
    @contextmanager
    def stack():
        if receipt is not None:
            from scripts import phase3d_v4_native_environment as environment
            _need(isinstance(receipt_sha256, str), 'A receipted environment needs its pinned receipt sha256.',
                  'environment_identity_unproven')
            scope = environment.environment_policy([session], receipt=receipt, receipt_sha256=receipt_sha256)
        else:
            scope = nullcontext()
        extension = offload_policy(offloads, offloads_sha256) if offloads is not None else nullcontext([])
        with scope as applied, denial.denial_policy() as refused, extension as verified:
            yield applied, refused, verified
    return stack()


def audit_native_run(session, *, receipt=None, receipt_sha256=None, offloads=None, offloads_sha256=None):
    """Release identity replay inside the V5b native stack."""
    with native_stack(session, receipt=receipt, receipt_sha256=receipt_sha256,
                      offloads=offloads, offloads_sha256=offloads_sha256) as (applied, refused, verified):
        value = identity.audit_native_run(session)
    return {**value, 'prevented_denial_policy': {'policy_id': denial.POLICY_ID, 'set_aside': refused},
            'offloaded_output_policy': {'policy_id': POLICY_ID, 'verified': verified},
            **({'environment_identity': applied} if applied else {})}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest='command', required=True)
    keep = commands.add_parser('preserve')
    keep.add_argument('--session', type=Path, required=True)
    keep.add_argument('--output', type=Path, required=True)
    audit = commands.add_parser('audit')
    audit.add_argument('--session', type=Path, required=True)
    audit.add_argument('--receipt', type=Path)
    audit.add_argument('--receipt-sha256')
    audit.add_argument('--offloads', type=Path)
    audit.add_argument('--offloads-sha256')
    args = parser.parse_args(argv)
    try:
        if args.command == 'preserve':
            value = preserve(args.session, args.output)
            print(json.dumps({'status': 'preserved', 'files': value['files']}, indent=1))
            return 0
        value = audit_native_run(_ordinary(args.session), receipt=args.receipt, receipt_sha256=args.receipt_sha256,
                                 offloads=args.offloads, offloads_sha256=args.offloads_sha256)
    except VisualError as exc:
        print(json.dumps({'status': 'fail', 'code': exc.code, 'message': str(exc)}))
        return 1
    print(json.dumps({'status': value.get('status', 'pass'),
                      'set_aside': value['prevented_denial_policy']['set_aside'],
                      'offloads_verified': value['offloaded_output_policy']['verified']}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
