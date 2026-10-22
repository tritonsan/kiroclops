"""Native Kiro ACP Sol/max launcher for portable sessions, with no fallback.

The proven transport and selection handshake are reused by subclassing. Session
validation, prompts, filesystem permissions and approval checks are local here;
no legacy guards or module globals are patched.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

from kiroclops.common import ROOT
from kiroclops.capture import process_options
from kiroclops.integration import kiro as native
from kiroclops.integration.model_cli_max_v1 import ModelACPClient as SelectionClient
from kiroclops.integration.storage import IntegrationError, read_json, sha256, utc_now, write_json
from .session import Session, digest, editable, ordinary

MODEL, EFFORT = "gpt-5.6-sol", "max"
APPROVAL_MODE = "native_session_allow_always"
MCP_TOOLS = native.MCP_TOOLS
KiroError = native.KiroError
REVISION = "portable-cli-sol-max-native-consent-v3"
INSTRUCTIONS = """Use this fresh workspace, native read/write and the seven KiroClops tools.
You must write the actual character GDScript yourself. No tool supplies a motion
patch. Only player.gd and direct motion/*.gd helper files are editable. The shared
movement resources, scenes, sprites, timers, collision shapes, camera, capture
adapters and acceptance constants are fixed. Never branch on experiment inputs,
scenario identifiers, capture processes or frame numbers to replay a solution.

First inspect_session, then prepare_experiment. Read measured calibration, profile
units, confidence and diagnostics. Read the actual target code and implement the
requested behavior. Evaluate each candidate and use only calibration feedback to
correct your own errors. A failed integrity check makes the candidate ineligible,
even if motion matches. A null motion score or not_run check is never a pass.

At most three candidate evaluations and thirty minutes are available. Failed
compilation and rejected source edits consume an attempt. Reserve time for final
validation. Call finalize_candidate for the lowest-error eligible candidate,
then build_report and report the recorded outcome. After sealing, never edit or
return to calibration. Keep waiting with get_job while any job is queued/running.

Only this stage's workspace and calibration evidence may guide changes. Do not
seek parent conversations, parent held-out results, reference source or previous
solutions. New stage conversations do not inherit the previous conversation.
Numerical success and later independent visual review are separate statuses.
"""


def launcher_binding():
    paths = [Path(__file__), Path(native.__file__),
             ROOT / "kiroclops/integration/model_cli_max_v1.py", ROOT / "kiroclops/portable/session.py",
             ROOT / "kiroclops/portable/mcp.py"]
    return {"revision": REVISION, "source_hashes": {p.relative_to(ROOT).as_posix(): sha256(p) for p in paths}}


def acp_argv(executable):
    # Native v3 rejects --trust-all-tools/--model/--effort despite general help.
    # Select Sol/max through verified RPC and grant native session consent below.
    return [str(executable), "acp", "--agent-engine", "v3", "--auth-method", "cli"]


def _permissions(target):
    root = target.as_posix()
    writable = [root + "/player.gd", root + "/motion/*.gd"]
    forbidden = [root + "/.kiroclops-private/**", root + "/.kiroclops-evidence/validation/**",
                 root + "/.kiroclops-evidence/heldout/**"]
    mcp = ["kiroclops/" + t for t in MCP_TOOLS]
    rules = [
        {"capability": "fs_read", "match": forbidden, "effect": "deny"},
        {"capability": "fs_read", "match": [root + "/**"], "exclude": forbidden, "effect": "allow"},
        {"capability": "fs_read", "match": ["**"], "exclude": [root + "/**"], "effect": "deny"},
        {"capability": "fs_write", "match": writable, "effect": "allow"},
        {"capability": "fs_write", "match": ["**"], "exclude": writable, "effect": "deny"},
        {"capability": "mcp", "match": mcp, "effect": "allow"},
        {"capability": "mcp", "match": ["*"], "exclude": mcp, "effect": "deny"},
    ]
    rules.extend({"capability": c, "effect": "deny"} for c in
                 ("shell", "web_fetch", "web_search", "subagent", "context", "skill", "power"))
    return {"rules": rules}


def configure_model_cli(directory, *, supplemental_instructions=None):
    session = Session(directory)
    state = session.verify()
    if (state["status"] != "new" or state.get("started_at") or state["candidates"]
            or state.get("active_job_id") or (session.directory / "kiro/launch.json").exists()):
        raise KiroError("not_fresh", "Only an unstarted portable session can be configured")
    target = Path(state["target"])
    controls = ordinary(target / ".kiro")
    if controls.exists() and any(controls.iterdir()):
        raise KiroError("existing_config", "Do not replace stage controls")
    instruction = INSTRUCTIONS
    if supplemental_instructions:
        if not isinstance(supplemental_instructions, str) or "\0" in supplemental_instructions:
            raise KiroError("invalid_instructions", "Supplemental instructions must be plain text")
        instruction += "\n" + supplemental_instructions + "\n"
    if state["experiment_id"] == "heart-jump-v1":
        goal = ("Transfer Tiny Crate's short/long normal jump from the h03/h24 calibration pixels. "
                "Preserve Heart's airborne horizontal control, air-jump entitlement, wall-jump behavior, "
                "coyote timing and hazard return. Normal ground/coyote jump trajectory may change.")
    else:
        goal = ("Transfer grounded running, release braking and direction reversal from r06/r48 calibration. "
                "Preserve the inherited short/long normal jump, airborne horizontal control, air jump, "
                "wall jump, coyote timing and hazard return. Ground contact is a physical predicate.")
    agent_name = "kiroclops-portable"
    server = {"command": str(native._python()), "args": ["-m", "kiroclops.portable.mcp", "--session", str(session.directory)],
              "env": {"PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}, "timeout": 30000}
    command = native._shell_command([str(native._python()), "-m", "kiroclops.portable.launcher",
                                    "--guard", "--session", str(session.directory)])
    hook = {"name": "KiroClops portable session boundary", "trigger": "PreToolUse",
            "action": {"type": "command", "command": command}, "timeout": 10}
    prompt = (f"Run this prepared experiment now. {goal}\n"
              f"Experiment: {state['experiment_id']}. Target workspace: {target}\n"
              "Model: gpt-5.6-sol. Effort: max. Three candidates, 1800 seconds.\n"
              "Use the selected agent instructions. Author the actual player code and finish the recorded validation workflow.\n")
    agent = {"name": agent_name, "description": "Portable Heart motion transfer; one bounded stage.",
             "model": MODEL, "effortLevel": EFFORT, "prompt": instruction + "\n" + goal,
             "tools": ["read", "write", "@kiroclops"],
             "excludedTools": ["code", "delete_file", "semantic_rename", "smart_relocate"],
             "includeMcpJson": False, "includePowers": False, "resources": [],
             "mcpServers": {"kiroclops": server}, "permissions": _permissions(target)}
    files = {f"agents/{agent_name}.json": json.dumps(agent, ensure_ascii=False, indent=2) + "\n",
             "kiroclops-instructions.md": instruction + "\n" + goal,
             "kiroclops-prompt.md": prompt,
             "hooks/kiroclops-guard.json": json.dumps({"version": "v1", "hooks": [hook]}, indent=2) + "\n"}
    for rel, text in files.items():
        path = controls / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    config = {"schema_version": 2, "mode": "tools", "surface": "cli", "integration": "custom_agent_mcp",
              "session_dir": str(session.directory), "target_dir": str(target), "agent_name": agent_name,
              "agent_path": str(controls / f"agents/{agent_name}.json"), "prompt_path": str(controls / "kiroclops-prompt.md"),
              "model": MODEL, "effort": EFFORT, "approval_mode": APPROVAL_MODE, "experiment_id": state["experiment_id"],
              "target_spec_sha256": state["target_spec_sha256"], "experiment_spec_sha256": state["experiment_spec_sha256"],
              "max_candidates": 3, "max_seconds": 1800, "allowed_commands": {},
              "generated_hashes": {rel: sha256(controls / rel) for rel in files}, "model_launcher": launcher_binding()}
    write_json(controls / "kiroclops-session.json", config)
    write_json(session.directory / "execution-controls.json", {"registered_at": utc_now(), "config": config,
              "config_sha256": sha256(controls / "kiroclops-session.json")})
    return config


def load_model_config(directory):
    session = Session(directory)
    state = session.verify()
    controls = ordinary(Path(state["target"]) / ".kiro")
    config = read_json(controls / "kiroclops-session.json")
    receipt = read_json(session.directory / "execution-controls.json")
    if (config.get("model") != MODEL or config.get("effort") != EFFORT or config.get("approval_mode") != APPROVAL_MODE
            or config.get("model_launcher") != launcher_binding() or receipt.get("config") != config
            or receipt.get("config_sha256") != sha256(controls / "kiroclops-session.json")
            or config.get("session_dir") != str(session.directory) or config.get("target_dir") != state["target"]
            or config.get("experiment_id") != state["experiment_id"]
            or config.get("target_spec_sha256") != state["target_spec_sha256"]
            or config.get("experiment_spec_sha256") != state["experiment_spec_sha256"]):
        raise KiroError("launcher_changed", "Stage configuration or exact Sol/max launcher binding changed")
    for rel, expected in config["generated_hashes"].items():
        path = ordinary(controls / rel)
        if not path.is_relative_to(controls) or not path.is_file() or sha256(path) != expected:
            raise KiroError("config_changed", "Generated stage instructions or permissions changed")
    return config


def check_tool_request(directory, payload):
    config = load_model_config(directory)
    target = Path(config["target_dir"])
    if not isinstance(payload, dict):
        return {"allow": False, "reason": "Malformed tool event"}
    tool, args = payload.get("tool_name", payload.get("toolName", "")), payload.get("tool_input", payload.get("toolInput", {}))
    if not isinstance(tool, str) or not isinstance(args, dict):
        return {"allow": False, "reason": "Malformed tool arguments"}
    name = tool.removeprefix("@builtin/")
    reads = {"fs_read", "read_file", "read_files", "readFile", "list_directory", "grep", "grep_search",
             "file_search", "glob", "read_image", "read_images", "readMultipleFiles", "listDirectory", "fileSearch", "grepSearch"}
    writes = {"fs_write", "fs_append", "write_file", "str_replace", "replace_text", "append_lines", "edit_code",
              "fsWrite", "fsAppend", "strReplace", "editCode"}
    paths, allowed, reason = native._path_values(args), False, "Only this stage's native files and seven MCP tools are allowed"
    if name in reads | writes:
        if not paths and name in {"list_directory", "grep", "grep_search", "file_search", "glob", "listDirectory", "fileSearch", "grepSearch"}:
            paths = ["."]
        resolved = []
        for value in paths:
            if value.startswith("~") or any(x in value for x in ("$", "%", "://")):
                return {"allow": False, "reason": "Use literal workspace paths", "tool": tool, "paths": paths}
            path = Path(value)
            resolved.append(ordinary(path if path.is_absolute() else target / path))
        allowed = bool(resolved) and all(p.is_relative_to(target) for p in resolved)
        if allowed:
            forbidden = [target / ".kiroclops-private", target / ".kiroclops-evidence/validation", target / ".kiroclops-evidence/heldout"]
            allowed = not any(p.is_relative_to(f) for p in resolved for f in forbidden)
            # A recursive search/list rooted above forbidden evidence could
            # otherwise read it without a second per-file native hook event.
            if name in {"list_directory", "grep", "grep_search", "file_search", "glob", "listDirectory", "fileSearch", "grepSearch"}:
                allowed = allowed and not any(f.exists() and f.is_relative_to(p) for f in forbidden for p in resolved)
        reason = "Parent, held-out and external files are outside this stage's readable workspace"
        if allowed and name in writes:
            allowed = all(editable(p.relative_to(target).as_posix()) for p in resolved)
            reason = "Only player.gd and direct motion/*.gd helpers may change"
            if allowed:
                error = native._write_state_error(Path(directory))
                if error:
                    allowed, reason = False, error
    elif name in {"tool_load", "tool_call"}:
        names = args.get("tool_ids", []) if name == "tool_load" else [args.get("tool_id")]
        accepted = {"kiroclops::" + t for t in MCP_TOOLS}
        if name == "tool_load":
            accepted.add("kiroclops::*")
        allowed = isinstance(names, list) and bool(names) and all(isinstance(n, str) and n in accepted for n in names)
    else:
        accepted = {pattern.format(t) for t in MCP_TOOLS for pattern in
                    ("@kiroclops/{}", "kiroclops/{}", "mcp_kiroclops_{}", "mcp__kiroclops__{}", "kiroclops___{}", "kiroclops::{}")}
        allowed = name in accepted
    return {"allow": bool(allowed), "reason": "Allowed stage operation" if allowed else reason, "tool": tool, "paths": paths}


def guard_request(directory, payload):
    try:
        decision = check_tool_request(directory, payload)
    except (KiroError, IntegrationError, OSError, ValueError, KeyError) as exc:
        decision = {"allow": False, "reason": str(exc)}
    log = ordinary(directory) / "kiro/tool-audit.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": utc_now(), **decision}, ensure_ascii=False) + "\n")
        stream.flush()
    return decision


class ModelACPClient(SelectionClient):
    """Reuse native transport/selection; replace every legacy stage boundary."""
    def __init__(self, ready, log_dir, timeout, cancel):
        # Own only process construction: the frozen constructor has a fixed argv.
        # Keep its suspended Windows Job assignment before any child can execute.
        self.config = ready["config"]
        if (self.config.get("model"), self.config.get("effort"), self.config.get("approval_mode")) != (MODEL, EFFORT, APPROVAL_MODE):
            raise KiroError("selection_changed", "Native Sol/max and Always Allow are required")
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.outbound_path = self.log_dir / "outbound.jsonl"
        with self.outbound_path.open("x", encoding="utf-8"):
            pass
        self._outbound_sequence = self._task_prompt_count = 0
        self.model_echo = self.effort_echo = 0
        self.effort_options = None
        self.autopilot = None
        self.cancel, self.deadline = cancel, time.monotonic() + timeout
        self.events = queue.Queue()
        self.guard = native.StreamConfigurationGuard(self.config["agent_name"], model=MODEL, effort=EFFORT)
        self.session_id = None
        self.servers, self.tool_inputs, self.tool_identifiers = {}, {}, {}
        self.tags, self.model_choices = set(), set()
        self.kas_version = None
        self.next_id, self.prompt_sent = 0, False
        self.argv = acp_argv(ready["executable"])
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1", "KIRO_DISABLE_SESSION_TITLE_LLM": "true"}
        self.job = native._WindowsJob() if os.name == "nt" else None
        try:
            self.process = subprocess.Popen(self.argv, cwd=self.config["target_dir"], env=env,
                shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if self.job else 0,
                start_new_session=os.name != "nt")
            if self.job:
                self.job.assign_and_resume(self.process)
        except BaseException:
            if self.job:
                self.job.close()
            if hasattr(self, "process"):
                native._terminate_tree(self.process)
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
            raise
        self.readers = []
        for stream, name in ((self.process.stdout, "stdout.jsonl"), (self.process.stderr, "stderr.log")):
            thread = threading.Thread(target=self._drain, args=(stream, name), daemon=True)
            thread.start()
            self.readers.append(thread)

    def _receive(self, deadline=None):
        event = super()._receive(deadline)
        if event and event.get("method") == "_kiro/mcp/governance_disabled":
            reason = event.get("params", {}).get("reason", "unspecified")
            raise KiroError("mcp_governance_unavailable",
                            f"Native Kiro disabled MCP governance ({reason}); no bypass or model fallback is permitted")
        for obj in native._stream_objects(event):
            for option in obj.get("configOptions", []) if isinstance(obj.get("configOptions"), list) else []:
                if isinstance(option, dict) and option.get("id") == "autopilot":
                    self.autopilot = option.get("currentValue")
        return event

    def handshake(self):
        result = super().handshake()
        if self.autopilot != "on":
            self.request("session/set_config_option", {"sessionId": self.session_id, "configId": "autopilot", "value": "on"})
            deadline = min(self.deadline, time.monotonic() + 15)
            while self.autopilot != "on":
                self._receive(deadline)
        self.guard._verify()
        return {**result, "approval_mode": APPROVAL_MODE, "argv": list(self.argv), "autopilot": self.autopilot,
                "approval_implementation": "guarded native allow_always response with session-scoped consent; allow_once only if persistence is unavailable",
                "unsupported_v3_flags": ["--trust-all-tools", "--model", "--effort"]}

    def prompt(self, text):
        if self.cancel is not None and self.cancel.is_set():
            raise KiroError("cancelled", "Cancelled before generation")
        if time.monotonic() >= self.deadline:
            raise KiroError("timeout", "Deadline before generation")
        if load_model_config(Path(self.config["session_dir"])) != self.config:
            raise KiroError("config_changed", "Prepared controls changed")
        self.guard._verify()
        self.prompt_sent = True
        return self.request("session/prompt", {"sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}]}, timeout=max(.1, self.deadline - time.monotonic()))

    def _permission_reply(self, event):
        params = event.get("params", {})
        cid = params.get("toolCall", {}).get("toolCallId")
        tool = params.get("_meta", {}).get("kiro", {}).get("toolId")
        raw, original = self.tool_inputs.get(cid), self.tool_identifiers.get(cid)
        if not isinstance(tool, str) or not isinstance(raw, dict) or (original is not None and original != tool):
            decision = {"allow": False, "reason": "Approval lacks bound native tool arguments"}
        else:
            decision = guard_request(self.config["session_dir"], {"tool_name": tool, "tool_input": raw})
        choice = next((o for kind in ("allow_always", "allow_once") for o in params.get("options", [])
                       if isinstance(o, dict) and o.get("kind") == kind and isinstance(o.get("optionId"), str)), None)
        outcome = {"outcome": "selected", "optionId": choice["optionId"]} if decision["allow"] and choice else {"outcome": "cancelled"}
        response = {"outcome": outcome}
        if decision["allow"] and choice and choice["kind"] == "allow_always":
            response["_meta"] = {"kiro": {"consent": {"scope": "session"}}}
        self._send({"jsonrpc": "2.0", "id": event["id"], "result": response})
        if outcome["outcome"] == "cancelled":
            raise KiroError("tool_denied", decision["reason"])


def preflight_model_cli(directory, *, executable=None, connect=True):
    config = load_model_config(directory)
    executable = str(executable or shutil.which("kiro-cli") or "")
    if not executable:
        raise KiroError("cli_missing", "kiro-cli is not available")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}
    options = dict(cwd=config["target_dir"], env=env, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", shell=False, **process_options())
    inventory = subprocess.run([executable, "chat", "--agent-engine", "v3", "--list-models", "--format", "json"], timeout=45, **options)
    if inventory.returncode:
        raise KiroError("model_inventory_unavailable", "Inventory unavailable; no generation started")
    models = native._parse_inventory(inventory.stdout)
    if MODEL not in {m.get("model_id") for m in models["models"] if isinstance(m, dict)}:
        raise KiroError("model_unavailable", "Exact Sol model unavailable; no fallback")
    validation = subprocess.run([executable, "agent", "validate", "--path", config["agent_path"]], timeout=30, **options)
    if validation.returncode:
        raise KiroError("agent_config_invalid", validation.stderr or validation.stdout)
    version = subprocess.run([executable, "--version"], timeout=15, **options)
    ready = {"executable": executable, "config": config, "inventory": models,
             "agent_validation": validation.stdout.strip(), "cli_version": version.stdout.strip()}
    if connect:
        log_dir = Path(directory) / "kiro/preflight"
        client = ModelACPClient(ready, log_dir, 120, None)
        try:
            ready["acp_preflight"] = client.handshake()
            ready["kas_version"] = client.kas_version
            write_json(log_dir / "result.json", {"prompt_sent": False, **ready["acp_preflight"],
                       "status": "pass", "cli_version": ready["cli_version"], "kas_version": client.kas_version})
        except KiroError as exc:
            write_json(log_dir / "result.json", {"status": "fail", "error_type": exc.code,
                       "error": str(exc), "prompt_sent": bool(client.prompt_sent),
                       "approval_mode": APPROVAL_MODE, "argv": list(client.argv),
                       "cli_version": ready["cli_version"], "kas_version": client.kas_version})
            raise
        finally:
            errors = client.close()
            if errors:
                raise KiroError("cleanup_failed", "; ".join(errors))
    return ready


def run_model_cli(directory, *, timeout=1800, executable=None, cancel=None):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 1800:
        raise KiroError("invalid_timeout", "Stage timeout must be positive and at most 1800 seconds")
    if cancel is not None and cancel.is_set():
        raise KiroError("cancelled", "Cancelled before launch")
    from .cohort import verify_launch_registration
    verify_launch_registration(directory)
    ready = preflight_model_cli(directory, executable=executable, connect=False)
    config = ready["config"]
    session = Session(directory)
    prompt = Path(config["prompt_path"]).read_text(encoding="utf-8")
    log_dir = session.directory / "kiro"
    log_dir.mkdir(parents=True, exist_ok=True)
    metadata = log_dir / "launch.json"
    if metadata.exists():
        raise KiroError("already_launched", "Each stage gets exactly one fresh native conversation")
    session.start_clock()
    record = {"schema_version": 3, "surface": "cli", "transport": "acp", "status": "starting",
              "started_at": utc_now(), "mode": "tools", "model_requested": MODEL, "effort_requested": EFFORT,
              "approval_mode": APPROVAL_MODE,
              "model_launcher": config["model_launcher"], "timeout_seconds": timeout,
              "cwd": config["target_dir"], "prompt_path": config["prompt_path"],
              "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "prompt_sent": False,
              "model_inventory_verified": True, "model_reported": [], "cli_version": ready["cli_version"],
              "stdout": str(log_dir / "stdout.jsonl"), "stderr": str(log_dir / "stderr.log"),
              "outbound_journal": str(log_dir / "outbound.jsonl")}
    write_json(log_dir / "model-inventory.json", ready["inventory"])
    write_json(metadata, record)
    start, client = time.monotonic(), None
    try:
        client = ModelACPClient(ready, log_dir, timeout, cancel)
        record.update(pid=client.process.pid, argv=client.argv, status="preflighting")
        write_json(metadata, record)
        record["acp_preflight"] = client.handshake()
        record.update(status="running", prompt_sent_at=utc_now())
        write_json(metadata, record)
        record["prompt_result"] = client.prompt(prompt)
        record["status"] = "completed" if record["prompt_result"].get("stopReason") == "end_turn" else "turn_stopped"
        if not client.guard.generation_started:
            raise KiroError("generation_unverified", "No native generation configuration observed")
    except (KiroError, IntegrationError, OSError, BrokenPipeError) as exc:
        record.update(status=getattr(exc, "code", "launch_failed"), error=str(exc))
    finally:
        if client is not None:
            summary = client.guard.summary()
            record.update(prompt_sent=client.prompt_sent, kas_version=client.kas_version,
                          stream_configuration=summary,
                          model_reported=[MODEL] if summary["generation_configuration_verified"] else [],
                          model_confirmation="stream_confirmed" if summary["generation_configuration_verified"] else "generation_unverified")
            try:
                errors = client.close()
                if errors:
                    record.update(status="cleanup_failed", cleanup_errors=errors)
            except Exception as exc:
                record.update(status="cleanup_failed", cleanup_errors=[str(exc)])
            record["process_returncode_after_cleanup"] = client.process.poll()
        record.update(finished_at=utc_now(), elapsed_seconds=round(time.monotonic() - start, 3))
        write_json(metadata, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--guard", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    try:
        if args.guard:
            decision = guard_request(args.session, json.load(sys.stdin))
            if not decision["allow"]:
                print(decision["reason"], file=sys.stderr)
                return 2
            return 0
        result = preflight_model_cli(args.session) if args.preflight else run_model_cli(args.session)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if args.preflight or result["status"] == "completed" else 1
    except (KiroError, IntegrationError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "error", "code": getattr(exc, "code", "invalid_input"), "error": str(exc)}), file=sys.stderr)
        return 2 if args.guard else 1


if __name__ == "__main__":
    raise SystemExit(main())
