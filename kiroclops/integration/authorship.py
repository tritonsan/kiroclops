"""Read-only ACP source-authorship audit using the native IDE replay core.

Neither model calls nor source patches run here. Friendly tool titles do not
establish write identity: structured native inputs must match ordered allowed
hook records, and every successful write needs a full completed-file preview.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from . import ide_evidence as replay
from .kiro import _baseline_commands

SHELL = {"execute_pwsh", "execute_bash", "run_command", "shell"}
MUTATIONS = replay.WRITES | replay.REPLACES | replay.OTHER_MUTATIONS


def _jsonl(session, relative):
    path = replay._owned(session, relative)
    if not path.is_file() or path.stat().st_size > replay.MAX_MEMBER_BYTES:
        raise replay.IDEExportError("missing_trace", f"Missing or oversized {relative}")
    data = path.read_bytes()
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise replay.IDEExportError("invalid_trace", "Trace is not UTF-8") from exc
    rows = [(line, replay._json(text)) for line, text in enumerate(lines, 1) if text.strip()]
    return rows, {"path": relative, "sha256": replay._sha(data)}


def _update(row):
    if row.get("method") == "session/update":
        params = row.get("params", {})
    elif row.get("type") == "sessionUpdate":
        params = row.get("data", {})
    else:
        return None, None
    if not isinstance(params, dict):
        return None, None
    return params.get("sessionId"), params.get("update")


def _native_input(update, original=None):
    args = update.get("rawInput", original)
    if not isinstance(args, dict):
        raise replay.IDEExportError("missing_input", "A native tool lacks structured rawInput")
    args = {key: value for key, value in args.items() if key != "_meta"}
    if original is not None:
        if any(args.get(key) != value for key, value in original.items()):
            raise replay.IDEExportError("changed_input", "Completed tool input disagrees with its original call")
        extras = set(args) - set(original)
        if extras - {"replace_all"} or ("replace_all" in extras and args["replace_all"] is not False):
            raise replay.IDEExportError("changed_input", "Completed tool added unsupported input fields")
    return args


def _normalize(session, launch, rows, hooks):
    preflight = launch.get("acp_preflight", {})
    sid = preflight.get("session_id")
    if not isinstance(sid, str) or preflight.get("workspace_verified") != str(session / "target"):
        raise replay.IDEExportError("missing_identity", "Launch must bind the ACP session ID to this target workspace")
    permissions = {}
    for _, row in rows:
        if row.get("method") != "session/request_permission":
            continue
        params = row.get("params", {})
        if params.get("sessionId") != sid:
            raise replay.IDEExportError("session_mismatch", "Permission record belongs to another ACP session")
        tid = params.get("toolCall", {}).get("toolCallId")
        tool = replay._kiro(params).get("toolId")
        if isinstance(tid, str) and isinstance(tool, str):
            if tid in permissions and permissions[tid] != tool:
                raise replay.IDEExportError("conflicting_tool", "Permission requests disagree on tool identity")
            permissions[tid] = tool
    allowed_writes = [(line, row) for line, row in hooks if row.get("allow") is True and row.get("tool") in MUTATIONS]
    allowed_shells = [(line, row) for line, row in hooks if row.get("allow") is True and row.get("tool") in SHELL]
    hook_index, shell_index, calls, messages, bindings, completed_writes = 0, 0, {}, [], [], 0
    controlled_rejections = []
    controlled_commands = []
    baseline = _baseline_commands(session) if launch.get("mode") == "baseline" else {}
    if baseline:
        config, _ = replay._read_json_file(session, "target/.kiro/kiroclops-session.json")
        if config.get("allowed_commands") != baseline or config.get("mode") != "baseline":
            raise replay.IDEExportError("baseline_commands", "Recorded baseline commands differ from the exact controlled API")
    for line, row in rows:
        row_sid, update = _update(row)
        if not isinstance(update, dict) or update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            continue
        tid = update.get("toolCallId")
        meta = replay._kiro(update)
        if meta.get("toolId") == "fetch_cloud_config" or (tid in calls and calls[tid].get("cloud")):
            calls[tid] = {"cloud": True}
            continue
        if row_sid != sid or not isinstance(tid, str):
            raise replay.IDEExportError("session_mismatch", "Tool record belongs to another or missing ACP session")
        if update["sessionUpdate"] == "tool_call":
            if tid in calls:
                raise replay.IDEExportError("duplicate_call", "ACP tool call ID was reused")
            args = _native_input(update)
            explicit = meta.get("toolId") or permissions.get(tid)
            name = explicit or "unknown_native_tool"
            kind = update.get("kind")
            mutation = kind in ("edit", "delete", "move") or explicit in MUTATIONS
            action = None
            if mutation:
                if meta.get("toolOrigin") not in ("default", "acp") or meta.get("serverName"):
                    raise replay.IDEExportError("write_identity", "Source mutation is not a native tool")
                if {"oldStr", "newStr"} <= set(args):
                    inferred = "str_replace"
                elif "text" in args or "content" in args:
                    inferred = "fs_write"
                else:
                    raise replay.IDEExportError("unsupported_write", "Native write input has no supported complete contents")
                if explicit and explicit not in (replay.REPLACES if inferred == "str_replace" else replay.WRITES):
                    raise replay.IDEExportError("write_identity", "Explicit native tool ID disagrees with write input")
                relative = replay._relative_source(session / "target", args.get("path", args.get("file")))
                if hook_index >= len(allowed_writes):
                    raise replay.IDEExportError("hook_binding", "A native write has no allowed hook record")
                hook_line, hook = allowed_writes[hook_index]
                hook_index += 1
                paths = hook.get("paths", [])
                accepted = replay.REPLACES if inferred == "str_replace" else replay.WRITES
                if hook.get("tool") not in accepted or not isinstance(paths, list) or len(paths) != 1 or \
                        replay._relative_source(session / "target", paths[0]) != relative:
                    raise replay.IDEExportError("hook_binding", "Ordered hook tool/path does not match the native write")
                name = inferred
                bindings.append({"tool_call_id": tid, "stdout_line": line, "hook_line": hook_line,
                                 "native_tool": name, "path": relative, "hook_at": hook.get("at")})
            elif kind == "execute" or explicit in SHELL:
                command = args.get("command")
                action = next((key for key, value in baseline.items() if command == value), None)
                if action is None or args.get("run_in_background") is True:
                    raise replay.IDEExportError("unsupported_shell", "Only exact foreground baseline prepare/capture/finish can be audited")
                if explicit and explicit not in SHELL:
                    raise replay.IDEExportError("shell_identity", "Shell permission identity disagrees with the tool")
                if shell_index >= len(allowed_shells):
                    raise replay.IDEExportError("hook_binding", "Controlled baseline command lacks a permitted shell hook")
                shell_hook_line, shell_hook = allowed_shells[shell_index]
                shell_lines = [shell_hook_line]
                shell_index += 1
                if tid in permissions:
                    # Kiro's native execute hook and the ACP permission guard
                    # are separate checks for the same command, not two runs.
                    # Bind the recorded permission ID to its own adjacent audit
                    # entry; never discard an unexplained extra shell record.
                    if shell_hook.get("tool") not in {"execute_pwsh", "execute_bash"} or shell_index >= len(allowed_shells):
                        raise replay.IDEExportError("hook_binding", "Controlled shell permission lacks its separate native/ACP audit pair")
                    permission_line, permission_hook = allowed_shells[shell_index]
                    if permission_hook.get("tool") != permissions[tid]:
                        raise replay.IDEExportError("hook_binding", "Shell permission tool ID disagrees with its audit entry")
                    shell_lines.append(permission_line)
                    shell_index += 1
                controlled_commands.append({"tool_call_id": tid, "stdout_line": line, "action": action,
                                            "hook_lines": shell_lines, "permission_tool": permissions.get(tid)})
                # This represents the verified controlled command's role, not an
                # invented MCP invocation. The source trace and command remain bound.
                name = "controlled_baseline_capture" if action == "capture" else f"controlled_baseline_{action}"
                kind = "other"
            elif explicit == "mcp_kiroclops_evaluate_candidate":
                if meta.get("serverName") != "kiroclops":
                    raise replay.IDEExportError("mcp_identity", "Evaluation permission is not from the expected MCP server")
                if not any(hook.get("tool") == explicit and hook.get("allow") is True for _, hook in hooks):
                    raise replay.IDEExportError("hook_binding", "Evaluation has no allowed hook evidence")
                name = explicit
            payload = {"type": "tool_call", "toolCallId": tid, "toolName": name, "actionType": name,
                       "args": args, "kind": kind, "status": update.get("status"), "_meta": update.get("_meta", {})}
            normalized = {"id": f"acp-{line}", "timestamp": None, "member": "kiro/stdout.jsonl", "line": line,
                          "main": True, "payload": payload}
            calls[tid] = {"entry": normalized, "original": args, "mutation": mutation, "baseline_action": action}
            messages.append(normalized)
        elif update.get("status") in ("completed", "failed", "cancelled"):
            if tid not in calls or "entry" not in calls[tid]:
                raise replay.IDEExportError("orphan_result", "ACP result has no preceding tool call")
            call = calls[tid]
            if call.get("completed"):
                raise replay.IDEExportError("duplicate_result", "ACP completion was duplicated")
            call["completed"] = True
            args = _native_input(update, call["original"])
            call["entry"]["payload"]["args"] = args
            if call["mutation"] and update.get("status") == "completed":
                preview = meta.get("preview")
                if not isinstance(preview, dict) or not all(isinstance(preview.get(key), str) for key in ("originalContent", "modifiedContent")):
                    raise replay.IDEExportError("missing_checkpoint", "Completed native write has no full before/after preview")
                completed_writes += 1
            output = update.get("rawOutput")
            if not isinstance(output, (dict, str)):
                raise replay.IDEExportError("missing_output", "Completed tool has no structured rawOutput")
            if call["baseline_action"]:
                exit_code = output.get("exitCode", output.get("exit_code")) if isinstance(output, dict) else None
                rejected_finish = False
                if type(exit_code) is int and exit_code == 1 and call["baseline_action"] == "finish" and update["status"] == "failed":
                    try:
                        failure = replay._json(output.get("output", ""))
                    except replay.IDEExportError:
                        failure = {}
                    # Session.submit rejects this before sealing, snapshotting,
                    # starting held-out capture or modifying the controller.
                    rejected_finish = (failure.get("status") == "error" and isinstance(failure.get("error"), dict)
                                       and failure["error"].get("code") == "no_eligible_candidate")
                    if rejected_finish:
                        controlled_rejections.append({"tool_call_id": tid, "stdout_line": line, "action": "finish",
                                                      "exit_code": 1, "code": "no_eligible_candidate"})
                if (type(exit_code) is not int or exit_code != 0) and not rejected_finish:
                    raise replay.IDEExportError("shell_exit", "Controlled baseline command needs recorded exit code zero")
            result = {"type": "tool_result", "toolCallId": tid, "success": update["status"] == "completed",
                      "content": json.dumps(output) if isinstance(output, dict) else output, "_meta": update.get("_meta", {})}
            messages.append({"id": f"acp-{line}", "timestamp": None, "member": "kiro/stdout.jsonl", "line": line,
                             "main": True, "payload": result})
    if hook_index != len(allowed_writes) or shell_index != len(allowed_shells):
        raise replay.IDEExportError("hook_binding", "Allowed native write/shell hooks are missing from the ACP trace")
    return messages, {"native_session_id": sid, "native_write_bindings": bindings,
                      "completed_write_checkpoints": completed_writes,
                      "controlled_rejections": controlled_rejections,
                      "controlled_commands": controlled_commands,
                      "timestamp_note": "ACP stdout has no per-record timestamps; source line order is preserved."}


def audit_kiro_authorship(session_dir, candidate_id=None):
    """Audit explicit ACP evidence in memory; never overwrite an existing review."""
    session = replay._session(session_dir)
    base = {"schema_version": 1, "surface": "cli", "status": "unverified", "candidate_id": candidate_id,
            "method": "acp_native_tool_replay", "reviewed_by": "kiroclops.authorship",
            "reviewed_at": datetime.now(timezone.utc).isoformat(), "evidence": []}
    try:
        state, state_evidence = replay._read_json_file(session, "session.json")
        candidate_id = candidate_id or state.get("selected_candidate_id")
        base["candidate_id"] = candidate_id
        if not candidate_id:
            return {**base, "reason": "The session has no selected candidate; pass an explicit candidate to inspect a snapshot"}
        launch, launch_evidence = replay._read_json_file(session, "kiro/launch.json")
        rows, trace_evidence = _jsonl(session, "kiro/stdout.jsonl")
        hooks, hook_evidence = _jsonl(session, "kiro/tool-audit.jsonl")
        base["evidence"] = [trace_evidence, hook_evidence, launch_evidence, state_evidence]
        if (launch.get("surface") != "cli" or launch.get("transport") != "acp" or launch.get("status") != "completed"
                or launch.get("prompt_result", {}).get("stopReason") != "end_turn"):
            return {**base, "reason": "A completed official CLI launch is required for this adapter"}
        messages, normalization = _normalize(session, launch, rows, hooks)
        calls, results, tools = replay._tool_records(messages)
        if tools["status"] != "complete":
            return {**base, "reason": "The ACP native tool chain is incomplete or invalid", "tools": tools}
        record = replay._authorship(session, candidate_id, messages, calls, results, trace_evidence,
            evaluation_predicate=lambda payload: replay._evaluation(payload) or
                payload.get("actionType") == "controlled_baseline_capture")
        record.update(schema_version=1, surface="cli", method="acp_native_tool_replay",
                      reviewed_by="kiroclops.authorship", normalization=normalization)
        evidence = {item["path"]: item for item in [*record["evidence"], *base["evidence"]]}
        record["evidence"] = list(evidence.values())
        record["limitation"] = ("Structured ACP outputs, native hook records and source hashes establish this replay. "
                                "Model/effort, budget and motion-quality gates remain separate benchmark checks.")
        return record
    except (replay.IDEExportError, OSError, UnicodeError) as exc:
        return {**base, "reason": str(exc), "error_code": getattr(exc, "code", type(exc).__name__)}


def write_authorship(session_dir, record):
    """Explicit opt-in persistence; existing independent reviews are immutable."""
    session = replay._session(session_dir)
    if record.get("status") != "verified":
        raise replay.IDEExportError("unverified_authorship", "Only a verified replay can be written as authorship evidence")
    # Recheck every bound evidence byte immediately before the exclusive write.
    for item in record.get("evidence", []):
        path = replay._owned(session, item["path"])
        if not path.is_file() or replay._sha(path.read_bytes()) != item.get("sha256"):
            raise replay.IDEExportError("evidence_changed", "Audited evidence changed before writing")
    candidate_id = record.get("candidate_id")
    if not isinstance(candidate_id, str) or not replay.IDENTIFIER.fullmatch(candidate_id):
        raise replay.IDEExportError("invalid_candidate", "A verified candidate ID is required")
    required = {"session.json", "kiro/stdout.jsonl", "kiro/tool-audit.jsonl", "kiro/launch.json",
                f"candidates/{candidate_id}/candidate.json", f"candidates/{candidate_id}/patch.diff"}
    if not required <= {item.get("path") for item in record.get("evidence", [])}:
        raise replay.IDEExportError("missing_evidence", "Verified authorship must bind every original audit input")
    state, _ = replay._read_json_file(session, "session.json")
    baseline = replay._owned(session, state.get("baseline", "baseline"))
    selected = replay._owned(session, f"candidates/{candidate_id}/project")
    if (replay.project_hashes(baseline) != state.get("baseline_hashes")
            or replay.project_hashes(selected) != record.get("source_hashes")):
        raise replay.IDEExportError("source_changed", "Baseline or selected source bytes changed after the audit")
    output = replay._owned(session, "kiro/authorship.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    except FileExistsError as exc:
        raise replay.IDEExportError("output_exists", "Existing authorship review will never be overwritten") from exc
    return output
