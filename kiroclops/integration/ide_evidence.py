"""Read a single, explicitly selected native IDE export without extracting it.

This is an evidence reader, not an IDE runner. Mutable session configuration
does not prove historical model/effort settings. Source replay is a separate,
candidate-bound claim and never upgrades that configuration evidence.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import stat
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile

from kiroclops.common import ROOT
from .storage import editable, project_hashes

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}")
WRITES = {"write", "create", "fs_write", "fsWrite", "write_file"}
REPLACES = {"replace", "str_replace", "strReplace", "replace_text"}
OTHER_MUTATIONS = {"append", "fs_append", "fsAppend", "append_lines", "edit_code", "editCode",
                   "delete", "delete_file", "move", "smart_relocate", "semantic_rename"}


class IDEExportError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise IDEExportError("invalid_json", "Duplicate JSON keys are ambiguous evidence")
        value[key] = item
    return value


def _json(data):
    try:
        value = json.loads(data, object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (ValueError, UnicodeError) as exc:
        raise IDEExportError("invalid_json", "Export contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise IDEExportError("invalid_json", "An exported JSON record must be an object")
    return value


def _owned(session, value):
    raw = Path(value)
    path = (raw if raw.is_absolute() else session / raw).resolve()
    if not path.is_relative_to(session):
        raise IDEExportError("outside_session", "Evidence must remain inside the explicitly selected session")
    return path


def _session(value):
    base = (ROOT / "sessions").resolve()
    if base != ROOT.resolve() / "sessions":
        raise IDEExportError("outside_session", "The sessions directory must not redirect elsewhere")
    raw = Path(value)
    path = (raw if raw.is_absolute() else base / raw).resolve()
    if path.parent != base or not IDENTIFIER.fullmatch(path.name) or not path.is_dir():
        raise IDEExportError("invalid_session", "Select an existing immediate child of ROOT/sessions")
    return path


def _time(value):
    if not isinstance(value, str):
        raise IDEExportError("invalid_timestamp", "Exported records need an ISO timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone missing")
        return result
    except ValueError as exc:
        raise IDEExportError("invalid_timestamp", "Exported timestamps must include a timezone") from exc


def _read_export(path):
    if not path.is_file() or path.suffix.lower() != ".zip":
        raise IDEExportError("missing_export", "Select an existing session-owned ZIP export")
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise IDEExportError("archive_limit", "ZIP exceeds the audit size limit")
    data = path.read_bytes()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise IDEExportError("archive_limit", "ZIP exceeds the audit size limit")
    members, roots, seen, total = {}, set(), set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > MAX_MEMBERS:
                raise IDEExportError("archive_limit", "ZIP has too many members")
            for item in archive.infolist():
                name = item.filename
                parts = name.rstrip("/").split("/")
                if (item.orig_filename != name or not name or "\\" in name or "\x00" in name or ":" in name or name.startswith("/")
                        or any(part in ("", ".", "..") for part in parts)):
                    raise IDEExportError("unsafe_member", "ZIP contains an unsafe member path")
                if name.casefold() in seen:
                    raise IDEExportError("duplicate_member", "ZIP has duplicate or case-colliding members")
                seen.add(name.casefold())
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or item.flag_bits & 1:
                    raise IDEExportError("unsafe_member", "ZIP links and encrypted members are unsupported")
                if not IDENTIFIER.fullmatch(parts[0]):
                    raise IDEExportError("invalid_layout", "ZIP session directory is invalid")
                roots.add(parts[0])
                if item.is_dir():
                    if len(parts) > 2 or (len(parts) == 2 and parts[1] != "sub-executions"):
                        raise IDEExportError("invalid_layout", "Unexpected ZIP directory")
                    continue
                valid = (len(parts) == 2 and parts[1] in ("session.json", "messages.jsonl")) or (
                    len(parts) == 3 and parts[1] == "sub-executions" and parts[2].endswith(".jsonl")
                    and IDENTIFIER.fullmatch(parts[2][:-6]))
                if not valid:
                    raise IDEExportError("invalid_layout", "Unexpected member in native IDE export")
                total += item.file_size
                if item.file_size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
                    raise IDEExportError("archive_limit", "Expanded ZIP exceeds the audit size limit")
                content = archive.read(item)
                if len(content) != item.file_size:
                    raise IDEExportError("invalid_archive", "ZIP member size mismatch")
                members[name] = content
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        raise IDEExportError("invalid_archive", "Cannot read native ZIP export") from exc
    if len(roots) != 1:
        raise IDEExportError("invalid_layout", "Exactly one native session is required")
    sid = next(iter(roots))
    metadata = _json(members.get(f"{sid}/session.json", b"{}"))
    if metadata.get("id") != sid:
        raise IDEExportError("session_mismatch", "Metadata ID differs from the ZIP session directory")
    messages = []
    for name in sorted(members, key=lambda item: ("/sub-executions/" in item, item)):
        if not name.endswith(".jsonl"):
            continue
        try:
            lines = members[name].decode("utf-8-sig").splitlines()
        except UnicodeError as exc:
            raise IDEExportError("invalid_json", "Export is not UTF-8") from exc
        for line, text in enumerate(lines, 1):
            if not text.strip():
                continue
            entry = _json(text)
            payload = entry.get("payload")
            if (not isinstance(entry.get("id"), str) or not isinstance(payload, dict)
                    or not isinstance(payload.get("type"), str)):
                raise IDEExportError("invalid_message", "Expected native id/timestamp/payload message records")
            _time(entry.get("timestamp"))
            messages.append({**entry, "member": name, "line": line,
                             "main": name == f"{sid}/messages.jsonl"})
    return metadata, messages, {"sha256": _sha(data), "native_session_id": sid,
        "size_bytes": len(data), "expanded_bytes": total, "member_count": len(members),
        "members": [{"path": name, "sha256": _sha(value), "size_bytes": len(value)}
                    for name, value in sorted(members.items())]}


def _kiro(payload):
    meta = payload.get("_meta")
    value = meta.get("kiro") if isinstance(meta, dict) else None
    return value if isinstance(value, dict) else {}


def _structured_job_result(text):
    """Extract application outcomes from known JSON/MCP wrappers, never prose.

    Transport success is separate: a normal MCP response can contain a rejected
    request. Conflicting duplicated wrappers cannot establish an accepted job.
    """
    try:
        value = _json(text)
    except IDEExportError:
        return {"status": "unverified", "reason": "No structured JSON application result."}
    accepted, errors, invalid = set(), set(), False

    def visit(node, depth=0):
        nonlocal invalid
        if not isinstance(node, dict):
            return
        if depth > 8:
            invalid = True
            return
        status = node.get("status")
        if node.get("isError") is True or node.get("is_error") is True or isinstance(status, str) and status in {"error", "failed", "rejected", "cancelled"} or node.get("error"):
            error = node.get("error")
            errors.add(error.get("code") if isinstance(error, dict) and isinstance(error.get("code"), str) else "application_error")
        if status == "queued":
            jid, cid = node.get("job_id"), node.get("candidate_id")
            if (isinstance(jid, str) and jid.startswith("job-") and IDENTIFIER.fullmatch(jid)
                    and isinstance(cid, str) and IDENTIFIER.fullmatch(cid)):
                accepted.add((jid, cid))
            else:
                invalid = True
        for key in ("structuredContent", "structured_content", "result", "data", "response", "stdout", "output"):
            nested = node.get(key)
            if isinstance(nested, str):
                try:
                    nested = _json(nested)
                except IDEExportError:
                    continue
            visit(nested, depth + 1)
        for block in node.get("content", []) if isinstance(node.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                try:
                    visit(_json(block["text"]), depth + 1)
                except IDEExportError:
                    continue

    visit(value)
    if errors:
        return {"status": "error", "error_codes": sorted(errors)}
    if invalid or len(accepted) > 1:
        return {"status": "ambiguous", "reason": "Accepted job identity is invalid or conflicts across result wrappers."}
    if len(accepted) == 1:
        jid, cid = next(iter(accepted))
        return {"status": "accepted", "job_id": jid, "candidate_id": cid}
    return {"status": "unverified", "reason": "No explicit queued job with a candidate binding."}


def _tool_records(messages):
    calls, results, order, issues = {}, {}, [], []
    for entry in messages:
        payload = entry["payload"]
        kind = payload["type"]
        if kind not in ("tool_call", "tool_result"):
            continue
        tid = payload.get("toolCallId")
        if not isinstance(tid, str) or not tid:
            raise IDEExportError("invalid_tool", "Structured tool record is missing its call ID")
        key = (entry["member"], tid)
        if kind == "tool_call":
            if not isinstance(payload.get("toolName"), str) or not isinstance(payload.get("args"), dict):
                raise IDEExportError("invalid_tool", "Tool calls need a name and structured arguments")
            if any(value is not None and not isinstance(value, str) for value in
                   (payload.get("actionType"), _kiro(payload).get("toolId"), _kiro(payload).get("serverName"))):
                raise IDEExportError("invalid_tool", "Tool identity fields must be strings")
            if key in calls:
                old = calls[key]["payload"]
                if any(old.get(field) != payload.get(field) for field in ("toolName", "args", "actionType")):
                    raise IDEExportError("conflicting_tool", "Repeated tool call changed its identity or arguments")
            else:
                order.append(key)
            calls[key] = entry
        else:
            if type(payload.get("success")) is not bool or not isinstance(payload.get("content"), str):
                raise IDEExportError("invalid_tool", "Tool results need a boolean success and text content")
            if key in results and results[key]["payload"] != payload:
                raise IDEExportError("conflicting_tool", "Repeated tool results disagree")
            if key not in calls:
                issues.append(f"Result without preceding call: {tid}")
            results[key] = entry
    summaries = []
    for key in order:
        call, result = calls[key], results.get(key)
        payload = call["payload"]
        summary = {"tool_call_id": key[1], "member": key[0], "line": call["line"],
            "timestamp": call["timestamp"], "tool_name": payload["toolName"],
            "action_type": payload.get("actionType"), "tool_id": _kiro(payload).get("toolId"),
            "argument_keys": sorted(payload["args"]), "result_present": result is not None,
            "success": result["payload"]["success"] if result else None,
            "result_line": result["line"] if result else None}
        if "kiro_powers" in {payload.get("actionType"), payload.get("toolName"), _kiro(payload).get("toolId")}:
            summary["power"] = {field: payload["args"].get(field) for field in
                                ("action", "powerName", "serverName", "toolName", "skillName")}
            summary["structured_result"] = (_structured_job_result(result["payload"]["content"]) if result else
                                            {"status": "unverified", "reason": "Tool result is missing."})
        summaries.append(summary)
    missing = sum(key not in results for key in calls)
    return calls, results, {"status": "invalid" if issues else ("complete" if calls and not missing else "incomplete"),
        "call_count": len(calls), "result_count": len(results), "missing_results": missing,
        "successful_calls": sum(result["payload"]["success"] for key, result in results.items() if key in calls),
        "tool_counts": dict(sorted(Counter(row["tool_name"] for row in summaries).items())),
        "calls": summaries, "issues": issues}


def _metadata(metadata, target, agent, model, effort):
    expected = {"workspacePaths": [str(target)], "agentMode": agent, "modelId": model, "effortLevel": effort}
    checks = {}
    for key, wanted in expected.items():
        actual = metadata.get(key)
        if key == "workspacePaths" and isinstance(actual, list) and all(isinstance(item, str) for item in actual):
            # Resolving is read-only and also catches a workspace path redirected by a junction.
            matched = len(actual) == 1 and Path(actual[0]).is_absolute() and Path(actual[0]).resolve() == target
        else:
            matched = actual == wanted
        checks[key] = {"status": "unverified" if actual is None else ("pass" if matched else "fail"),
                       "expected": wanted, "observed": actual}
    for key in ("createdAt", "lastModifiedAt"):
        value = metadata.get(key)
        try:
            _time(value)
            status = "pass"
        except IDEExportError:
            status = "unverified" if value is None else "fail"
        checks[key] = {"status": status, "observed": value}
    states = [check["status"] for check in checks.values()]
    return {"status": "fail" if "fail" in states else ("pass" if all(item == "pass" for item in states) else "unverified"),
            "checks": checks, "historical_configuration_status": "unverified",
            "note": "Session metadata is mutable final configuration, not a history of model/effort for every request."}


def _relative_source(target, value):
    if not isinstance(value, str) or not value or any(token in value for token in ("~", "$", "%", "://")):
        raise IDEExportError("unsupported_write", "Write path is missing or requires expansion")
    if ".." in value.replace("\\", "/").split("/"):
        raise IDEExportError("outside_target", "Write path contains parent traversal")
    path = Path(value)
    path = (path if path.is_absolute() else target / path).resolve()
    if not path.is_relative_to(target):
        raise IDEExportError("outside_target", "Successful write points outside the selected target")
    relative = path.relative_to(target).as_posix()
    if not editable(relative):
        raise IDEExportError("protected_write", "Successful write is outside editable player GDScript")
    return relative


def _identity(payload):
    meta = _kiro(payload)
    # Prefer native IDs. A friendly title is never interpreted as executable identity.
    values = [payload.get("actionType"), meta.get("toolId"), payload.get("toolName")]
    return next((item for item in values if isinstance(item, str) and item), "")


def _evaluation(payload):
    meta = _kiro(payload)
    ids = {payload.get("actionType"), payload.get("toolName"), meta.get("toolId")}
    args = payload["args"]
    if "kiro_powers" in ids:
        return (args.get("action") == "use" and args.get("powerName") == "kiroclops"
                and args.get("serverName") == "kiroclops" and args.get("toolName") == "evaluate_candidate")
    accepted = {"@kiroclops/evaluate_candidate", "kiroclops/evaluate_candidate",
                "kiroclops___evaluate_candidate", "mcp_kiroclops_evaluate_candidate",
                "remote_kiroclops_evaluate_candidate"}
    return bool(ids & accepted) or (meta.get("serverName") == "kiroclops" and
        ("evaluate_candidate" in ids or payload["args"].get("name") == "evaluate_candidate"))


def _candidate_ids(text):
    """Only structured JSON output, never prose or a regex match on a title."""
    try:
        value = _json(text)
    except IDEExportError:
        return set()
    found = set()
    def visit(node, depth=0):
        if not isinstance(node, dict) or depth > 8:
            return
        if isinstance(node.get("candidate_id"), str):
            found.add(node["candidate_id"])
        for key in ("structuredContent", "structured_content", "result", "data", "output"):
            visit(node.get(key), depth + 1)
        for key in ("response", "stdout", "output"):
            if isinstance(node.get(key), str):
                try:
                    visit(_json(node[key]), depth + 1)
                except IDEExportError:
                    pass
        for block in node.get("content", []) if isinstance(node.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                try:
                    visit(_json(block["text"]), depth + 1)
                except IDEExportError:
                    pass
    visit(value)
    return found


def _apply_write(contents, target, payload, result_payload=None, *, document_eol=False):
    name, args = _identity(payload), payload["args"]
    relative = _relative_source(target, args.get("path", args.get("file", payload.get("filePath"))))
    current = contents.get(relative)
    if name in WRITES:
        text = args.get("text", args.get("content"))
        if not isinstance(text, str):
            raise IDEExportError("unsupported_write", "Full write contents are absent from the export")
        updated = text.encode("utf-8")
    elif name in REPLACES:
        old, new = args.get("oldStr"), args.get("newStr")
        if not isinstance(old, str) or not old or not isinstance(new, str) or current is None:
            raise IDEExportError("unsupported_write", "Replacement lacks exact old/new text or original file")
        text = current.decode("utf-8")
        # Kiro matches old text across CRLF/LF, then inserts the new text as
        # supplied. Existing and replaced regions can therefore use different
        # line endings. Final source hashes must still match exact bytes.
        pattern = re.compile(r"\r?\n".join(re.escape(part) for part in old.replace("\r\n", "\n").split("\n")))
        matches = list(pattern.finditer(text))
        if len(matches) != 1 or args.get("replace_all") is True:
            raise IDEExportError("ambiguous_replacement", "Replacement must match exactly once")
        match = matches[0]
        if document_eol:
            # The native IDE applies an edit to a TextDocument, whose existing
            # EOL is preserved. CLI filesystem tools have different semantics.
            # A mixed-EOL buffer is not inferred from a final candidate hash.
            lf = text.count("\n")
            crlf = text.count("\r\n")
            if not lf or text.count("\r") != crlf or crlf not in (0, lf):
                raise IDEExportError("unsupported_eol", "Native editor replay requires an unambiguous original document EOL")
            new = new.replace("\r\n", "\n")
            if "\r" in new:
                raise IDEExportError("unsupported_eol", "Lone carriage returns are unsupported in native editor replacements")
            if crlf:
                new = new.replace("\n", "\r\n")
        updated = (text[:match.start()] + new + text[match.end():]).encode("utf-8")
    else:
        raise IDEExportError("unsupported_write", f"Replay does not support native mutation {name}")
    preview = _kiro(result_payload or {}).get("preview")
    full_preview = isinstance(preview, dict)
    if not full_preview:
        preview = _kiro(payload).get("preview")
    if isinstance(preview, dict):
        # A pending replacement previews only oldStr/newStr. Its completed
        # result previews the full file. Do not confuse these two contracts.
        originals = (("originalContent", current), ("modifiedContent", updated))
        if name in REPLACES and not full_preview and preview.get("originalContent") == args.get("oldStr"):
            originals = (("originalContent", args["oldStr"].encode("utf-8")),
                         ("modifiedContent", args["newStr"].encode("utf-8")))
        for key, actual in originals:
            if isinstance(preview.get(key), str) and preview[key].encode("utf-8") != (actual or b""):
                raise IDEExportError("checkpoint_mismatch", "Native write preview disagrees with replay bytes")
    contents[relative] = updated
    return relative


def _native_editor_replayer(native_session_id):
    """Recognize IDE editor checkpoints without reading private snapshot data."""
    last_checkpoint = {}

    def parsed_uri(value):
        if not isinstance(value, str):
            raise IDEExportError("checkpoint_mismatch", "Native editor checkpoint URI must be text")
        try:
            return urlsplit(value)
        except ValueError as exc:
            raise IDEExportError("checkpoint_mismatch", "Native editor checkpoint URI is malformed") from exc

    def apply_native_editor_write(contents, target, payload, result_payload=None):
        checkpoint = _kiro(result_payload or {}).get("checkpoint")
        if checkpoint is None:
            return _apply_write(contents, target, payload, result_payload)
        if (payload.get("toolName") != "str_replace" or payload.get("actionType") != "replace"
                or _kiro(payload).get("toolOrigin") != "default"
                or not isinstance(checkpoint, dict) or set(checkpoint) != {"original", "modified", "local"}):
            raise IDEExportError("unsupported_checkpoint", "Native editor checkpoint does not match the known replacement contract")
        relative = _relative_source(target, payload["args"].get("path"))
        for name in ("original", "modified"):
            uri = parsed_uri(checkpoint[name])
            if (uri.scheme != "kiro-snapshot-v2" or uri.path != "/" or uri.fragment
                    or not re.fullmatch(re.escape(native_session_id) + r":[0-9a-f]{8}", uri.netloc)
                    or parse_qs(unquote(uri.query)) != {"originalPath": [relative]}):
                raise IDEExportError("checkpoint_mismatch", "Native editor snapshot identity/path differs from this session source")
        local = parsed_uri(checkpoint["local"])
        local_path = unquote(local.path)
        if re.match(r"^/[A-Za-z]:/", local_path):
            local_path = local_path[1:]
        if (local.scheme != "file" or local.netloc or local.query or local.fragment
                or _relative_source(target, local_path) != relative):
            raise IDEExportError("checkpoint_mismatch", "Native editor local checkpoint does not bind the target source")
        if relative in last_checkpoint and checkpoint["original"] != last_checkpoint[relative]:
            raise IDEExportError("checkpoint_mismatch", "Native editor checkpoint chain is discontinuous")
        result = _apply_write(contents, target, payload, result_payload, document_eol=True)
        last_checkpoint[relative] = checkpoint["modified"]
        return result

    return apply_native_editor_write


def _read_json_file(session, value):
    path = _owned(session, value)
    if not path.is_file() or path.stat().st_size > MAX_MEMBER_BYTES:
        raise IDEExportError("missing_candidate", "Candidate audit evidence is missing or oversized")
    data = path.read_bytes()
    return _json(data), {"path": path.relative_to(session).as_posix(), "sha256": _sha(data)}


def _authorship(session, candidate_id, messages, calls, results, export, *, evaluation_predicate=_evaluation,
                write_replayer=_apply_write):
    base = {"status": "unverified", "method": "native_export_source_replay", "candidate_id": candidate_id,
            "reviewed_by": "kiroclops.ide_evidence", "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "evidence": [{"path": export["path"], "sha256": export["sha256"]}]}
    if candidate_id is None:
        return {**base, "reason": "No candidate was explicitly selected for source replay"}
    if not IDENTIFIER.fullmatch(candidate_id):
        raise IDEExportError("invalid_candidate", "Candidate ID is invalid")
    try:
        state, state_evidence = _read_json_file(session, "session.json")
        base["candidate_selected_for_finalization"] = state.get("selected_candidate_id") == candidate_id
        candidate, candidate_evidence = _read_json_file(session, f"candidates/{candidate_id}/candidate.json")
        base["evidence"] += [state_evidence, candidate_evidence]
        expected = candidate.get("source_hashes")
        base["source_hashes"] = expected
        record = next((item for item in state.get("candidates", []) if item.get("candidate_id") == candidate_id), None)
        if not expected or candidate.get("candidate_id") != candidate_id or not record or record.get("source_hashes") != expected:
            return {**base, "reason": "Session and candidate snapshot metadata are incomplete or disagree"}
        baseline = _owned(session, state.get("baseline", "baseline"))
        selected = _owned(session, f"candidates/{candidate_id}/project")
        before = project_hashes(baseline)
        if before != state.get("baseline_hashes") or project_hashes(selected) != expected:
            return {**base, "status": "failed", "reason": "Current baseline/candidate bytes differ from recorded hashes"}
        changed = sorted(key for key in set(before) | set(expected) if before.get(key) != expected.get(key))
        if not changed:
            return {**base, "reason": "The candidate has no code change to attribute"}
        if any(not editable(key) or key not in expected for key in changed):
            return {**base, "status": "failed", "reason": "Selected candidate changes protected files or deletes sources"}
        patch = _owned(session, f"candidates/{candidate_id}/patch.diff")
        if not patch.is_file():
            return {**base, "reason": "Selected candidate diff is missing"}
        base["evidence"].append({"path": patch.relative_to(session).as_posix(), "sha256": _sha(patch.read_bytes())})
        contents = {key: (baseline / key).read_bytes() for key in before if editable(key)}
        if any(not entry["main"] and (_identity(entry["payload"]) in WRITES | REPLACES | OTHER_MUTATIONS
                                      or entry["payload"].get("kind") in ("edit", "delete", "move", "execute"))
               for entry in calls.values()):
            return {**base, "reason": "Sub-agent mutations do not have a proven ordering for source replay"}
        boundaries = [(key, entry, results[key]) for key, entry in calls.items()
                      if entry["main"] and evaluation_predicate(entry["payload"]) and key in results
                      and results[key]["payload"]["success"]
                      and _candidate_ids(results[key]["payload"]["content"]) == {candidate_id}]
        if len(boundaries) != 1:
            return {**base, "reason": "Exactly one successful structured evaluation result must bind this candidate"}
        key, evaluation_call, evaluation_result = boundaries[0]
        checkpoint = {"tool_call_id": key[1], "member": key[0], "call_line": evaluation_call["line"],
                      "result_line": evaluation_result["line"]}
        # A snapshot request is the boundary. Crediting a concurrent write that
        # finishes while the request is in flight would invent snapshot ordering.
        overlapping = [key for key, entry in calls.items() if entry["main"]
                       and entry["line"] < checkpoint["result_line"]
                       and (_identity(entry["payload"]) in WRITES | REPLACES | OTHER_MUTATIONS
                            or entry["payload"].get("kind") in ("edit", "delete", "move", "execute"))
                       and (key not in results or results[key]["line"] >= checkpoint["call_line"])]
        if overlapping:
            return {**base, "reason": "A source mutation overlaps the evaluation call boundary", "candidate_boundary": checkpoint}
        steps, seen_results = [], set()
        for entry in messages:
            if entry["main"] and entry["line"] >= checkpoint["call_line"]:
                break
            payload = entry["payload"]
            if payload["type"] != "tool_result":
                continue
            key = (entry["member"], payload["toolCallId"])
            if key in seen_results:
                continue
            seen_results.add(key)
            call_entry = calls.get(key)
            if call_entry is None:
                return {**base, "reason": "A tool result has no preceding structured call"}
            call = call_entry["payload"]
            name = _identity(call)
            if call.get("kind") == "execute" or name in {"execute_bash", "execute_pwsh", "run_command", "runCommand", "shell"}:
                return {**base, "reason": "Shell actions cannot be reconstructed as native source writes"}
            mutation = name in WRITES | REPLACES | OTHER_MUTATIONS or call.get("kind") in ("edit", "delete", "move")
            if mutation:
                if not entry["main"]:
                    return {**base, "reason": "Sub-agent writes do not have a proven ordering for source replay"}
                if payload["success"] is not True:
                    return {**base, "reason": "A failed write may have partially modified source files"}
                relative = write_replayer(contents, session / "target", call, payload)
                steps.append({"tool_call_id": key[1], "path": relative, "sha256_after": _sha(contents[relative]),
                              "member": entry["member"], "result_line": entry["line"],
                              "replay_semantics": write_replayer.__name__})
        reconstructed = {**before, **{key: _sha(value) for key, value in contents.items()}}
        if reconstructed != expected:
            return {**base, "status": "failed", "reason": "Exported writes do not reproduce the complete selected source hash map",
                    "replay_steps": steps, "candidate_boundary": checkpoint,
                    "mismatched_files": sorted(key for key in set(reconstructed) | set(expected) if reconstructed.get(key) != expected.get(key))}
        return {**base, "status": "verified", "reason": "Successful native writes reproduce the audited candidate at its evaluation call boundary",
                "changed_files": changed, "replay_steps": steps, "candidate_boundary": checkpoint,
                "limitation": "Replay binds source bytes to this export; it does not authenticate the exporter or prove historical effort settings."}
    except (IDEExportError, OSError, UnicodeError, RuntimeError) as exc:
        return {**base, "status": "failed" if getattr(exc, "code", "") in
                ("outside_session", "outside_target", "protected_write", "checkpoint_mismatch") else "unverified",
                "reason": str(exc), "error_code": getattr(exc, "code", type(exc).__name__)}


def _timing(messages, calls, results, metadata):
    """Separate only known, completed cloud-config preload pairs from a turn.

    The IDE can persist these records when its panel opens, long before Send.
    Arbitrary tools, assistant text, prior turns and unmatched results are never
    excluded from the experiment clock.
    """
    main = [entry for entry in messages if entry["main"]]
    boundary = next((i for i, entry in enumerate(main)
                     if entry["payload"]["type"] in {"user", "turn_start"}), len(main))
    prefix = main[:boundary]
    prefix_ids = set()
    valid_prefix = bool(boundary < len(main))
    for entry in prefix:
        payload = entry["payload"]
        tid = (entry["member"], payload.get("toolCallId"))
        if payload["type"] == "tool_call":
            valid_prefix &= (payload.get("toolName") == "fetch_cloud_config"
                             and payload.get("actionType") == "fetch_cloud_config"
                             and _kiro(payload).get("toolId") == "fetch_cloud_config"
                             and payload.get("args") == {"toolName": "fetch_cloud_config"}
                             and payload.get("status") == "completed" and tid not in prefix_ids)
            prefix_ids.add(tid)
        elif payload["type"] == "tool_result":
            call = calls.get(tid)
            valid_prefix &= (tid in prefix_ids and payload.get("success") is True
                             and call is not None and call["main"]
                             and call["line"] < entry["line"])
        else:
            valid_prefix = False
    for tid in prefix_ids:
        result = results.get(tid)
        valid_prefix &= (result is not None and result in prefix)
    if prefix and valid_prefix:
        first = main[boundary]
        valid_prefix = all(_time(entry["timestamp"]) <= _time(first["timestamp"]) for entry in prefix)
    prelude_status = "none" if not prefix else ("known_cloud_config" if valid_prefix else "unverified")
    excluded = {(entry["member"], entry["line"]) for entry in prefix} if valid_prefix else set()
    ends = [entry for entry in main if entry["payload"]["type"] == "turn_end"]
    starts = [entry for entry in main if entry["payload"]["type"] == "turn_start"]
    suffix = [entry for entry in main if ends and entry["line"] > ends[-1]["line"]]
    descriptor = suffix[0] if len(suffix) == 1 else None
    descriptor_payload = descriptor["payload"] if descriptor else {}
    valid_descriptor = bool(descriptor and ends[-1]["payload"].get("stopReason") == "end_turn"
        and descriptor["id"] == f"session_start_{metadata.get('id')}_{metadata.get('agentMode')}"
        and set(descriptor_payload) == {"type", "agentType", "content", "forcedRole", "messageId"}
        and descriptor_payload.get("type") == "session_start"
        and descriptor_payload.get("agentType") == metadata.get("agentMode")
        and descriptor_payload.get("forcedRole") is True
        and isinstance(descriptor_payload.get("content"), str)
        and isinstance(descriptor_payload.get("messageId"), str)
        and re.fullmatch(r"[0-9a-fA-F-]{36}", descriptor_payload["messageId"])
        and _time(descriptor["timestamp"]) >= _time(ends[-1]["timestamp"]))
    if valid_descriptor:
        excluded.add((descriptor["member"], descriptor["line"]))
    postlude = {"status": "none" if not suffix else ("native_session_descriptor" if valid_descriptor else "unverified"),
                "message_count": len(suffix), "excluded_from_experiment": valid_descriptor}
    if valid_descriptor:
        postlude.update(line=descriptor["line"], timestamp=descriptor["timestamp"],
                        content_sha256=_sha(descriptor_payload["content"].encode("utf-8")))
    experiment = [entry for entry in messages if (entry["member"], entry["line"]) not in excluded]
    users = [entry for entry in main if entry["payload"]["type"] == "user"]
    prompt_records = []
    for entry in users:
        content = entry["payload"].get("content")
        prompt_records.append({"line": entry["line"], "timestamp": entry["timestamp"],
            "content_sha256": _sha(content.encode("utf-8")) if isinstance(content, str) else None,
            "whitespace_normalized_sha256": _sha(" ".join(content.split()).encode("utf-8")) if isinstance(content, str) else None})
    return {"first_message_at": main[0]["timestamp"] if main else None,
            "last_message_at": main[-1]["timestamp"] if main else None,
            "first_experiment_message_at": min((entry["timestamp"] for entry in experiment), key=_time, default=None),
            "last_experiment_message_at": max((entry["timestamp"] for entry in experiment), key=_time, default=None),
            "first_user_at": users[0]["timestamp"] if users else None,
            "user_messages": prompt_records,
            "prelude": {"status": prelude_status, "message_count": len(prefix),
                        "call_count": len(prefix_ids), "excluded_from_experiment": bool(prefix and valid_prefix)},
            "postlude": postlude,
            "last_turn_end_at": ends[-1]["timestamp"] if ends else None,
            "last_turn_stop_reason": ends[-1]["payload"].get("stopReason") if ends else None,
            "turn_start_count": len(starts), "turn_end_count": len(ends),
            "later_turn_start_present": bool(starts and (not ends or starts[-1]["line"] > ends[-1]["line"])),
            "note": "Only complete fetch_cloud_config preload pairs and one final native session descriptor are excluded; all raw timestamps are preserved."}


def audit_ide_export(session_dir, export_path, *, expected_agent="kiroclops-tools",
                     expected_model="claude-opus-5", expected_effort="high", candidate_id=None):
    """Return a JSON-serializable audit, reading no other IDE/account sessions.

All paths are owned by one experiment session. The original ZIP is read as a
bounded byte buffer, hashed, and never extracted, rewritten or imported.
"""
    session = _session(session_dir)
    path = _owned(session, export_path)
    metadata, messages, export = _read_export(path)
    export["path"] = path.relative_to(session).as_posix()
    configuration = _metadata(metadata, (session / "target").resolve(), expected_agent, expected_model, expected_effort)
    calls, results, tools = _tool_records(messages)
    reasoning = [entry for entry in messages if entry["payload"]["type"] == "assistant"
                 and entry["payload"].get("operationType") == "Reasoning"]
    present = [entry["payload"].get("reasoningModelId") for entry in reasoning
               if isinstance(entry["payload"].get("reasoningModelId"), str)]
    raw_models = sorted(set(present))
    # Native IDE exports use this exact provider-qualified model identifier.
    # Preserve the raw value and never strip arbitrary provider prefixes.
    models = sorted({value.removeprefix("qdev::") for value in raw_models})
    mismatch = bool(set(models) - {expected_model}) or configuration["checks"]["modelId"]["status"] == "fail"
    model = {"status": "mismatch" if mismatch else ("limited" if models else "unverified"),
        "required": expected_model, "metadata_model": metadata.get("modelId"), "reasoning_models": models,
        "raw_reasoning_models": raw_models, "normalization": "Exact native qdev:: provider prefix only",
        "reasoning_records": len(reasoning), "reasoning_records_with_model": len(present),
        "historical_model_status": "unverified", "historical_effort_status": "unverified",
        "note": "Reasoning model IDs support only the records that contain them; final metadata and prose do not establish all request settings."}
    authorship = _authorship(session, candidate_id, messages, calls, results, export,
                            write_replayer=_native_editor_replayer(export["native_session_id"]))
    if configuration["status"] != "pass" or tools["status"] == "invalid" or mismatch:
        if authorship["status"] == "verified":
            authorship.update(status="unverified", reason="Source replay matched, but the export's identity/configuration/tool evidence is invalid or incomplete")
    failed = configuration["status"] == "fail" or mismatch or tools["status"] == "invalid" or authorship["status"] == "failed"
    timing = _timing(messages, calls, results, metadata)
    return {"schema_version": 1, "surface": "ide", "session_id": session.name,
        "generated_at": datetime.now(timezone.utc).isoformat(), "status": "failed" if failed else "limited",
        "strict_configuration_verified": False, "export": export, "configuration": configuration,
        "model": model, "message_count": len(messages), "timing": timing, "tools": tools, "authorship": authorship,
        "limitations": ["This native ZIP is not a CLI ACP stream and does not prove model/effort configuration for every generation.",
                        "The ZIP is preserved; no archive paths are extracted or followed.",
                        "Native exports omit separate tool-outputs files and are not cryptographically authenticated.",
                        "No success claim about motion quality, held-out tests or the shared time budget is derived here."]}
