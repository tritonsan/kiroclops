"""V5 native reader extension: tool calls the experiment boundary refused before they ran.

PHASE3D_V5_PLAN.md §3.4 (user decision, 13 October 2026, fixed before any V5 run).

The pinned 2.27 reader (``kiroclops/local_engine/review.py``) accepts no hook
denial at all. A single refused attempt, which the boundary stopped and which
produced nothing, makes the whole native audit fail. In V4, v4p01 had four such
refusals: a file delete, a read outside the target and two shell commands.

This process-local, versioned policy sets aside only calls that are proven
refused, before the unchanged canonical tool-outcome audit runs. A call is set
aside when all of the following hold:

- its native updates are exactly the canonical pre-handle rejection
  (``review._hook_denied_no_output``): no output, no content, no progress, only
  toolOrigin default/acp/client, and locations equal to the requested path;
- its id is not a ``run_command_`` id, which would mean a shell that started;
- its title maps to a built-in tool (``review._native_tool_name``), never to a
  kiroclops MCP tool;
- a pre_tool_use hook decision with ``allow: false`` names the same tool and the
  identical arguments. Equal (tool, arguments) groups must have equal call and
  decision counts, or none of the group is set aside.

The matched decisions are set aside with their calls. Everything else goes to
the canonical audit unchanged, which keeps rejecting:

- any denied call that produced output or progress;
- offloaded MCP output;
- images;
- allowed failures other than the canonical PowerShell exit 1;
- unmatched hook rows;
- every other boundary violation.

The result lists every call set aside. The policy composes with the 2.27.1
release identity mapping and the receipted one-server environment. The original
function is restored even on error.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager, nullcontext
import functools
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import review
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.schema import VisualError, digest
from scripts import phase3d_v2_native_release_identity as identity

POLICY_ID = 'v5-prevented-denial-v1'


def _update(event):
    params = event.get('params') if isinstance(event, dict) else None
    update = params.get('update') if isinstance(params, dict) else None
    return update if isinstance(update, dict) else {}


def _key(tool, arguments):
    return tool, json.dumps(arguments, sort_keys=True, ensure_ascii=True)


def refused_calls(inbound, decisions):
    """Return (call ids, decision indices, ledger) for calls proven refused before they ran."""
    updates = defaultdict(list)
    for event in inbound:
        update = _update(event)
        if update.get('sessionUpdate') in ('tool_call', 'tool_call_update') and isinstance(update.get('toolCallId'), str):
            updates[update['toolCallId']].append(update)
    candidates = defaultdict(list)
    for call_id, items in updates.items():
        if call_id.startswith('run_command_'):
            continue
        final = items[-1]
        tool, arguments = review._native_tool_name(final.get('title')), final.get('rawInput')
        if not isinstance(tool, str) or tool.startswith('@kiroclops/') or not isinstance(arguments, dict):
            continue
        if review._hook_denied_no_output(items, tool, arguments):
            candidates[_key(tool, arguments)].append(call_id)
    denials = defaultdict(list)
    for index, row in enumerate(decisions):
        if isinstance(row, dict) and row.get('audit_origin') == 'pre_tool_use' and row.get('allow') is False \
                and isinstance(row.get('arguments'), dict) and isinstance(row.get('tool'), str):
            denials[_key(row['tool'], row['arguments'])].append(index)
    call_ids, indices, ledger = set(), set(), []
    for key, calls in candidates.items():
        rows = denials.get(key, [])
        if len(rows) != len(calls):
            continue
        call_ids.update(calls)
        indices.update(rows)
        for call_id, row in zip(calls, rows):
            ledger.append({'tool_call_id': call_id, 'tool': key[0], 'decision_index': row + 1,
                           'reason': decisions[row].get('reason'),
                           'arguments_sha256': digest(decisions[row]['arguments'])})
    return call_ids, indices, sorted(ledger, key=lambda row: row['decision_index'])


@contextmanager
def denial_policy():
    """Set aside proven refusals before the canonical tool-outcome audit; yields the ledger."""
    original = review.audit_native_tool_outcomes
    ledger = []

    @functools.wraps(original)
    def audited(inbound, decisions, **kwargs):
        call_ids, indices, rows = refused_calls(inbound, decisions)
        ledger.extend(rows)
        kept = [event for event in inbound if _update(event).get('toolCallId') not in call_ids]
        return original(kept, [row for index, row in enumerate(decisions) if index not in indices], **kwargs)

    review.audit_native_tool_outcomes = audited
    try:
        yield ledger
    finally:
        review.audit_native_tool_outcomes = original


def audit_native_run(session, *, receipt=None, receipt_sha256=None):
    """Release identity replay with proven refusals set aside, inside an optional receipted environment."""
    if receipt is not None:
        from scripts import phase3d_v4_native_environment as environment
        if not isinstance(receipt_sha256, str):
            raise VisualError('environment_identity_unproven', 'A receipted environment needs its pinned receipt sha256.')
        scope = environment.environment_policy([session], receipt=receipt, receipt_sha256=receipt_sha256)
    else:
        scope = nullcontext()
    with scope as applied, denial_policy() as ledger:
        value = identity.audit_native_run(session)
    return {**value, 'prevented_denial_policy': {'policy_id': POLICY_ID, 'set_aside': ledger},
            **({'environment_identity': applied} if applied else {})}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--receipt-sha256')
    args = parser.parse_args(argv)
    try:
        value = audit_native_run(_ordinary(args.session), receipt=args.receipt, receipt_sha256=args.receipt_sha256)
    except VisualError as exc:
        print(json.dumps({'status': 'fail', 'code': exc.code, 'message': str(exc)}))
        return 1
    print(json.dumps({'status': value.get('status', 'pass'),
                      'set_aside': value['prevented_denial_policy']['set_aside']}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
