"""Read-only, hash-bound evidence of one prepared task on the max ACP client.

This establishes what this recorded client sent. It does not attest that a
separate UI or another unobserved transport could never inject input.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from . import model_cli_max_v1 as launcher
from .ground_benchmark import _owned
from .storage import ROOT, sha256

LIMIT = 32 * 1024 * 1024
CONTROL_METHODS = {"initialize", "session/new", "session/set_mode",
                   "session/set_config_option", "_kiro/hooks/list"}


def _digest(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("ACP journal times must include a timezone")
    return result


def _jsonl(path):
    if path.stat().st_size > LIMIT:
        raise ValueError("ACP evidence exceeds the bounded native log size")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            raise ValueError("ACP journal contains a blank record")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("ACP evidence records must be objects")
        rows.append(row)
    return rows


def verify_outbound(session_dir):
    """Verify exact prepared prompt, one actual send, and its native end_turn.

    Returns pass/fail/unverified. Missing or unsent evidence cannot pass.
    Native selected-source authorship and experiment acceptance are separate.
    """
    try:
        session = _owned(ROOT / "sessions", Path(session_dir))
        if session == (ROOT / "sessions").resolve():
            raise ValueError("A named session is required")
        launch_path = _owned(session, "kiro/launch.json")
        journal_path = _owned(session, "kiro/outbound.jsonl")
        stdout_path = _owned(session, "kiro/stdout.jsonl")
        bound = {"launch": sha256(launch_path), "outbound": sha256(journal_path),
                 "stdout": sha256(stdout_path)}
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        config = launcher.load_model_config(session)
        target = _owned(session, config["target_dir"])
        prompt_path = _owned(target, config["prompt_path"])
        if not prompt_path.is_relative_to(target / ".kiro"):
            raise ValueError("Prepared task must be a generated target control")
        prompt = prompt_path.read_text(encoding="utf-8")
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if (not prompt.strip() or launch.get("model_requested") != "gpt-5.6-sol"
                or launch.get("effort_requested") != "max" or launch.get("transport") != "acp"
                or launch.get("surface") != "cli" or launch.get("mode") != "tools"
                or _owned(session, launch["outbound_journal"]) != journal_path
                or _owned(session, launch["stdout"]) != stdout_path
                or _owned(session, launch["prompt_path"]) != prompt_path
                or launch.get("prompt_sha256") != prompt_hash
                or launch.get("model_launcher") != config.get("model_launcher")):
            raise ValueError("Launch, prepared task or explicit max-client identity differs")
        bound["prompt"] = sha256(prompt_path)
        rows, inbound = _jsonl(journal_path), _jsonl(stdout_path)
        if not rows:
            return {"status": "unverified", "reason": "No outbound ACP messages were recorded"}
        # Every send has an adjacent write-ahead attempt and a post-flush receipt.
        messages, previous_time = [], _time(launch["started_at"])
        incomplete = False
        for offset in range(0, len(rows), 2):
            attempt = rows[offset]
            sequence = offset // 2 + 1
            if (attempt.get("phase") != "attempt" or type(attempt.get("sequence")) is not int
                    or attempt["sequence"] != sequence or not isinstance(attempt.get("message"), dict)
                    or attempt.get("payload_sha256") != _digest(attempt["message"])):
                raise ValueError("Outbound attempt sequence or canonical payload digest differs")
            attempted_at = _time(attempt["at"])
            if attempted_at < previous_time:
                raise ValueError("Outbound attempt chronology is not monotonic")
            if offset + 1 == len(rows):
                incomplete = True
                messages.append((attempt["message"], False, attempted_at))
                break
            sent = rows[offset + 1]
            if (sent.get("phase") != "sent" or type(sent.get("sequence")) is not int
                    or sent["sequence"] != sequence or sent.get("payload_sha256") != attempt["payload_sha256"]
                    or "message" in sent):
                raise ValueError("Outbound post-flush receipt does not match its attempt")
            sent_at = _time(sent["at"])
            if sent_at < attempted_at:
                raise ValueError("Outbound send predates its write-ahead receipt")
            previous_time = sent_at
            messages.append((attempt["message"], True, sent_at))
        prompts = [(m, sent, at) for m, sent, at in messages if m.get("method") == "session/prompt"]
        creates = [(m, sent) for m, sent, _ in messages if m.get("method") == "session/new"]
        if len(prompts) > 1 or len(creates) > 1:
            raise ValueError("More than one task prompt or native session was attempted")
        if not prompts or not creates:
            return {"status": "unverified", "reason": "A native session and task prompt are required",
                    "prompt_count": len(prompts), "source_hashes": bound}
        task, task_sent, task_at = prompts[0]
        create, create_sent = creates[0]
        if not task_sent or not create_sent or incomplete:
            return {"status": "unverified", "reason": "An ACP attempt lacks a post-flush send receipt",
                    "prompt_count": len(prompts), "source_hashes": bound}
        responses = lambda request: [event for event in inbound if "method" not in event
                                    and type(event.get("id")) is type(request.get("id"))
                                    and event.get("id") == request.get("id")]
        created = responses(create)
        if len(created) != 1 or "error" in created[0]:
            raise ValueError("The native session creation response is missing or ambiguous")
        native_id = created[0].get("result", {}).get("sessionId")
        if (not isinstance(native_id, str) or not native_id
                or launch.get("acp_preflight", {}).get("session_id") != native_id
                or Path(create.get("params", {}).get("cwd", "")).resolve() != target
                or task.get("params") != {"sessionId": native_id,
                    "prompt": [{"type": "text", "text": prompt}]}):
            raise ValueError("Native session or exactly prepared single task differs")
        seen_task, request_ids = False, set()
        for message, sent, _ in messages:
            if message.get("jsonrpc") != "2.0":
                raise ValueError("Outbound record is not an ACP JSON-RPC message")
            method = message.get("method")
            if method is None:
                origins = [event for event in inbound if "method" in event and "id" in event
                           and type(event["id"]) is type(message.get("id")) and event["id"] == message.get("id")]
                if len(origins) != 1 or ("result" in message) == ("error" in message):
                    raise ValueError("Client reply is not bound to exactly one native request")
                continue
            request_key = (type(message.get("id")).__name__, str(message.get("id")))
            if "id" not in message or request_key in request_ids:
                raise ValueError("Client request IDs are missing or duplicated")
            request_ids.add(request_key)
            if method == "session/prompt":
                seen_task = True
            elif method not in CONTROL_METHODS or seen_task:
                raise ValueError("An unexpected outbound request or post-task control message was sent")
            params = message.get("params", {})
            if not isinstance(params, dict) or ("sessionId" in params and params["sessionId"] != native_id):
                raise ValueError("Outbound command targets a different native session")
        response = responses(task)
        if not response:
            return {"status": "unverified", "reason": "The sole task has no native completion response",
                    "prompt_count": 1, "source_hashes": bound}
        if (len(response) != 1 or "error" in response[0]
                or response[0].get("result", {}).get("stopReason") != "end_turn"
                or response[0].get("result") != launch.get("prompt_result")
                or launch.get("status") != "completed" or launch.get("prompt_sent") is not True):
            raise ValueError("The sole prompt did not bind one successful native end_turn")
        finished = _time(launch["finished_at"])
        if previous_time > finished or task_at > finished:
            raise ValueError("Outbound task/control evidence extends beyond launch completion")
        current = {"launch": sha256(launch_path), "outbound": sha256(journal_path),
                   "stdout": sha256(stdout_path), "prompt": sha256(prompt_path)}
        if current != bound:
            raise ValueError("Autonomy input evidence changed during verification")
        return {"status": "pass", "prompt_count": 1, "native_session_count": 1,
                "native_session_id": native_id, "prompt_request_id": task["id"],
                "prompt_sha256": prompt_hash, "end_turn_response_sha256": _digest(response[0]),
                "outbound_message_count": len(messages), "source_hashes": bound,
                "scope": "Exactly one prepared task sent by this recorded ACP client, with a matching native end_turn.",
                "limitations": ["No claim about input through an unobserved separate UI or transport.",
                                "Selected-source native write authorship and experiment acceptance require separate verification."]}
    except FileNotFoundError as error:
        return {"status": "unverified", "reason": "Required ACP input evidence is missing", "error": str(error)}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError) as error:
        return {"status": "fail", "reason": str(error)}
