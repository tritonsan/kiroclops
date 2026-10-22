"""Session-local Kiro configuration and a bounded, auditable CLI launcher.

This module configures tools; it never supplies or patches a jump controller.
No account settings, global agents or global Powers are changed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import Any


MODEL = "claude-opus-5"
EFFORT = "high"
MAX_SECONDS = 1800
MAX_CANDIDATES = 3
MCP_TOOLS = (
    "inspect_session", "prepare_experiment", "evaluate_candidate",
    "finalize_candidate", "get_job", "cancel_job", "build_report",
)


class KiroError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def server_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _session_target(session_dir: Path) -> tuple[Path, Path]:
    session = Path(session_dir).resolve(strict=True)
    target = (session / "target").resolve(strict=True)
    if target.parent != session or not (target / "project.godot").is_file():
        raise KiroError("invalid_target", "Expected a real Godot project at session/target.")
    return session, target


def _python() -> Path:
    folder = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return server_root() / ".venv-kiro" / folder


def _shell_command(args: list[str]) -> str:
    # Hooks are commands, not arguments to an interpolated outer shell.
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _baseline_commands(session: Path) -> dict[str, str]:
    args = [str(_python()), "-m", "kiroclops.integration.session_cli"]
    if os.name == "nt":
        # Kiro's Windows shell tool uses PowerShell. Quote literals, including apostrophes.
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        return {action: f"& {quote(args[0])} -m {args[2]} {action} --session {quote(str(session))}"
                for action in ("prepare", "capture", "finish")}
    return {action: shlex.join(args + [action, "--session", str(session)])
            for action in ("prepare", "capture", "finish")}


def _permissions(target: Path, mode: str, commands: dict[str, str], *, power: bool = False) -> dict[str, Any]:
    scope = target.as_posix()
    writable = f"{scope}/source/characters/player/**/*.gd"
    namespace = "power-kiroclops-kiroclops" if power else "kiroclops"
    mcp = [f"{namespace}/{tool}" for tool in MCP_TOOLS] if mode == "tools" else []
    rules: list[dict[str, Any]] = [
        {"capability": "fs_read", "match": [f"{scope}/**"], "effect": "allow"},
        {"capability": "fs_read", "match": ["**"], "exclude": [f"{scope}/**"], "effect": "deny"},
        {"capability": "fs_write", "match": [writable], "effect": "allow"},
        {"capability": "fs_write", "match": ["**"], "exclude": [writable], "effect": "deny"},
        {"capability": "mcp", "match": ["*"], "exclude": mcp, "effect": "deny"},
        {"capability": "shell", "match": ["*"], "exclude": list(commands.values()), "effect": "deny"},
    ]
    if mcp:
        rules.append({"capability": "mcp", "match": mcp, "effect": "allow"})
    if commands:
        rules.append({"capability": "shell", "match": list(commands.values()), "effect": "allow"})
    if power:
        selected = ["kiroclops", *[f"kiroclops/kiroclops/{tool}" for tool in MCP_TOOLS]]
        rules.extend([
            {"capability": "power", "match": ["*"], "exclude": selected, "effect": "deny"},
            {"capability": "power", "match": selected, "effect": "allow"},
        ])
    for capability in ("web_fetch", "web_search", "subagent", "context", "skill") + (() if power else ("power",)):
        rules.append({"capability": capability, "effect": "deny"})
    return {"rules": rules}


def _power_server(server: dict[str, Any]) -> dict[str, Any]:
    """Agent Plugins requires a bare executable or a ./ package-local one.

    Windows Kiro resolves the bare executable using this child's PATH. The MCP
    checks sys.executable so an unsupported client's resolution fails closed.
    Custom-agent MCP continues to invoke the interpreter by absolute path.
    """
    if os.name == "nt":
        command = "python.exe"
        args = list(server["args"])
        environment = {**server["env"], "PATH": str(Path(server["command"]).parent) + os.pathsep + os.environ.get("PATH", ""),
                       "KIROCLOPS_EXPECTED_PYTHON": server["command"], "KIROCLOPS_WATCH_PARENT": "1"}
    else:
        command = "env"
        args = [server["command"], *server["args"]]
        environment = dict(server["env"])
    return {"type": "stdio", "command": command, "args": args, "env": environment}


def configure_kiro(session_dir: Path, mode: str = "tools", *, surface: str = "cli") -> dict[str, Any]:
    """Write only target/.kiro controls and a local, resolved Power copy.

    Preparation is idempotent for the same mode. A mode switch requires a fresh
    experiment session so an ablation cannot inherit the measured workflow.
    """
    if mode not in {"tools", "baseline"}:
        raise KiroError("invalid_mode", "mode must be tools or baseline")
    if surface not in {"cli", "ide"} or (surface == "ide" and mode != "tools"):
        raise KiroError("invalid_surface", "IDE Power configuration requires tools mode.")
    power = surface == "ide"
    session, target = _session_target(session_dir)
    config_dir = target / ".kiro"
    if config_dir.exists() and config_dir.resolve().parent != target:
        raise KiroError("unsafe_config_path", "The .kiro directory must stay inside target.")
    marker = config_dir / "kiroclops-session.json"
    if marker.exists():
        previous = json.loads(marker.read_text(encoding="utf-8"))
        if previous.get("mode") != mode or previous.get("session_dir") != str(session):
            raise KiroError("mode_conflict", "Use a fresh prepared session for another mode.")
        if previous.get("surface", "cli") != surface:
            state_path = session / "session.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
            if state.get("started_at") or state.get("candidates") or (session / "kiro/launch.json").exists():
                raise KiroError("surface_conflict", "A started session cannot change integration surface.")
    elif config_dir.exists() and any(config_dir.iterdir()):
        raise KiroError("existing_config", "Refusing to replace an unmanaged .kiro directory.")
    source = server_root() / "power" / "kiroclops"
    state_path = session / "session.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    experiment_id = state.get("experiment_id", "legacy-jump-v1")
    if experiment_id not in {"legacy-jump-v1", "ground-motion-v1"}:
        raise KiroError("unsupported_experiment", "No Kiro workflow for this experiment.")
    if experiment_id == "ground-motion-v1" and mode != "tools":
        raise KiroError("invalid_mode", "Ground-motion has no baseline cohort in this phase.")
    instruction_file = ("ground-motion.md" if experiment_id == "ground-motion-v1" else
                        "skills/motion-calibration/SKILL.md" if mode == "tools" else "baseline.md")
    instructions = source / instruction_file
    instruction_text = instructions.read_text(encoding="utf-8")
    agent_name = f"kiroclops-{mode}"
    commands = _baseline_commands(session) if mode == "baseline" else {}
    server = {
        "command": str(_python()),
        "args": ["-m", "kiroclops.integration.mcp", "--session", str(session)],
        "env": {"PYTHONPATH": str(server_root()), "PYTHONUTF8": "1"},
        "timeout": 30000,
    }
    hook_command = _shell_command([
        str(_python()), str(server_root() / "scripts" / "run_kiro.py"),
        "--guard", "--session", str(session), "--mode", mode,
    ])
    hook = {"name": "KiroClops session boundary", "trigger": "PreToolUse",
            "action": {"type": "command", "command": hook_command}, "timeout": 10}
    agent = {
        "name": agent_name,
        "description": "Bounded reference-motion experiment; session-local files and evidence only.",
        "model": MODEL,
        "effortLevel": EFFORT,
        "prompt": ("Use the installed kiroclops Power. First call kiro_powers with action=activate and powerName=kiroclops. "
                   "Then read its motion-calibration skill using action=readSkill. Follow that skill and invoke its seven "
                   "MCP tools through action=use, powerName=kiroclops, serverName=kiroclops. "
                   "Read only this target workspace and edit only its player GDScript. Do not use other Powers."
                   if power else instruction_text),
        "tools": ["read", "write"] + (["kiro_powers"] if power else (["@kiroclops"] if mode == "tools" else ["shell"])),
        "excludedTools": ["code", "delete_file", "semantic_rename", "smart_relocate"],
        "includeMcpJson": False,
        "includePowers": False,
        "mcpServers": {"kiroclops": server} if mode == "tools" and not power else {},
        "permissions": _permissions(target, mode, commands, power=power),
        "resources": [],
    }
    prompt = (
        "Run this prepared reference-motion experiment now. Use the selected agent's workflow.\n"
        f"Mode: {mode}. Model: {MODEL}. Effort: {EFFORT}.\n"
        f"The target workspace is exactly: {target}\n"
        f"The raw reference package is: {target / '.kiroclops-reference'}\n"
        f"Budget: at most {MAX_CANDIDATES} candidates and {MAX_SECONDS // 60} minutes.\n"
        "Read only this target workspace. Edit its actual player GDScript yourself.\n"
        "Reference inputs and raw imagery are in .kiroclops-reference.\n"
        "Complete the finalization workflow and state the recorded outcome and artifact path.\n"
    )
    if experiment_id == "ground-motion-v1":
        prompt += ("Experiment: ground-motion-v1. Transfer grounded running, braking after release, "
                   "and direction changes from the measured reference. Preserve the improved "
                   "short/long normal jump already in this starting project, its airborne control, "
                   "double jump and wall jumps. Implement the code yourself and verify both mechanics.\n")
    if commands:
        prompt += "\nThe only allowed shell commands, copied exactly, are:\n"
        prompt += "\n".join(f"{action}:\n{command}\n" for action, command in commands.items())
    generated: dict[str, str] = {
        f"agents/{agent_name}.json": json.dumps(agent, ensure_ascii=False, indent=2) + "\n",
        "kiroclops-instructions.md": instruction_text,
        "kiroclops-prompt.md": prompt,
        "hooks/kiroclops-guard.json": json.dumps({"version": "v1", "hooks": [hook]}, indent=2) + "\n",
        "settings/mcp.json": json.dumps({"mcpServers": {"kiroclops": server} if mode == "tools" and not power else {}}, indent=2) + "\n",
    }
    if mode == "tools":
        for relative in ("plugin.json", "skills/motion-calibration/SKILL.md"):
            generated[f"powers/kiroclops/{relative}"] = (instruction_text if relative.endswith("SKILL.md")
                                                       else (source / relative).read_text(encoding="utf-8"))
        generated["powers/kiroclops/mcp.json"] = json.dumps({
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            # Agent Plugins 1.0 has additionalProperties:false for stdio.
            # Kiro's custom-agent timeout is not part of the Power schema.
            "mcpServers": {"kiroclops": _power_server(server)},
        }, indent=2) + "\n"
    for name, content in generated.items():
        path = config_dir / name
        if not path.resolve().is_relative_to(config_dir.resolve()):
            raise KiroError("unsafe_config_path", f"Configuration path escaped .kiro: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    config = {
        "schema_version": 1, "mode": mode, "surface": surface, "integration": "ide_power" if power else "custom_agent_mcp",
        "session_dir": str(session), "target_dir": str(target),
        "agent_name": agent_name, "agent_path": str(config_dir / f"agents/{agent_name}.json"),
        "prompt_path": str(config_dir / "kiroclops-prompt.md"),
        "power_dir": str(config_dir / "powers/kiroclops") if mode == "tools" else None,
        "model": MODEL, "effort": EFFORT, "max_candidates": MAX_CANDIDATES,
        "experiment_id": experiment_id,
        "max_seconds": MAX_SECONDS, "allowed_commands": commands,
        "instruction_sha256": hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
        "generated_hashes": {name: hashlib.sha256(content.encode("utf-8")).hexdigest()
                             for name, content in generated.items()},
    }
    _json_file(marker, config)
    return config


def _load_config(session_dir: Path, mode: str) -> dict[str, Any]:
    session, target = _session_target(session_dir)
    marker = target / ".kiro/kiroclops-session.json"
    if not marker.is_file():
        raise KiroError("not_configured", "Run configure_kiro before launching the agent.")
    config = json.loads(marker.read_text(encoding="utf-8"))
    if config.get("mode") != mode or config.get("session_dir") != str(session):
        raise KiroError("mode_conflict", "Session configuration does not match requested mode.")
    state_path = session / "session.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("experiment_id", "legacy-jump-v1") != config.get("experiment_id", "legacy-jump-v1"):
            raise KiroError("experiment_conflict", "Kiro controls belong to another experiment.")
    for relative, digest in config["generated_hashes"].items():
        path = (target / ".kiro" / relative).resolve()
        if not path.is_relative_to(target / ".kiro") or not path.is_file():
            raise KiroError("config_changed", "A generated Kiro control file is missing or outside .kiro.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise KiroError("config_changed", f"Kiro control file changed: {relative}")
    return config


def _path_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            if key in {"path", "paths", "file_path", "filePath", "file_paths", "files",
                       "directory", "directory_path", "root", "cwd", "working_dir", "workingDirectory"}:
                if isinstance(item, str):
                    found.append(item)
                elif isinstance(item, list):
                    found.extend(v for v in item if isinstance(v, str))
                    found.extend(_path_values(item))
            elif isinstance(item, (dict, list)):
                found.extend(_path_values(item))
        return found
    if isinstance(value, list):
        return [path for item in value for path in _path_values(item)]
    return []


def _write_state_error(session_dir: Path) -> str | None:
    """Fail closed if live session state cannot authorize another source edit."""
    try:
        state = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return "Session state is not a JSON object; source writes are blocked."
        if state.get("selected_candidate_id") or state.get("sealed_at"):
            return "The experiment is sealed; source writes are forbidden."
        deadline = state.get("deadline_at")
        if deadline is not None:
            if type(deadline) in (int, float):
                if not math.isfinite(deadline):
                    return "The session deadline is invalid; source writes are blocked."
                parsed = datetime.fromtimestamp(deadline, timezone.utc)
            else:
                parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return "The session deadline has no timezone; source writes are blocked."
            if datetime.now(timezone.utc) >= parsed:
                return "The experiment deadline has passed; source writes are forbidden."
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return "Session state is unavailable or invalid; source writes are blocked."
    return None


def check_tool_request(session_dir: Path, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed PreToolUse decision, also directly unit-testable."""
    config = _load_config(session_dir, mode)
    target = Path(config["target_dir"])
    if not isinstance(payload, dict):
        return {"allow": False, "reason": "Tool event must be a JSON object."}
    tool = payload.get("tool_name", payload.get("toolName", ""))
    args = payload.get("tool_input", payload.get("toolInput", {}))
    if not isinstance(tool, str) or not isinstance(args, dict):
        return {"allow": False, "reason": "Unrecognized tool event."}
    name = tool.removeprefix("@builtin/")
    power = config.get("integration") == "ide_power"
    namespace = "power-kiroclops-kiroclops" if power else "kiroclops"
    read = {"fs_read", "read_file", "read_files", "readFile", "list_directory",
            "grep", "grep_search", "file_search", "glob", "read_image", "read_images",
            "readMultipleFiles", "listDirectory", "fileSearch", "grepSearch"}
    write = {"fs_write", "fs_append", "write_file", "str_replace", "replace_text", "append_lines", "edit_code",
             "fsWrite", "fsAppend", "strReplace", "editCode"}
    allowed = False
    reason = "Tool is outside this experiment's allowed interface."
    paths = _path_values(args)
    if name in read | write:
        if not paths and name in {"list_directory", "grep", "grep_search", "file_search", "glob",
                                  "listDirectory", "fileSearch", "grepSearch"}:
            paths = ["."]
        resolved = []
        for value in paths:
            # Native Kiro tools expand '~' and environment variables; pathlib
            # does not. Never reinterpret these as innocuous relative paths.
            if value.startswith("~") or "$" in value or "%" in value or "://" in value:
                return {"allow": False, "reason": "Paths must be explicit workspace paths.", "tool": tool, "paths": paths}
            path = Path(value)
            resolved.append((path if path.is_absolute() else target / path).resolve())
        allowed = bool(resolved) and all(path.is_relative_to(target) for path in resolved)
        reason = "File operations must stay in the prepared target workspace."
        if allowed and name in write:
            player = target / "source/characters/player"
            allowed = all(path.is_relative_to(player) and path.suffix == ".gd" for path in resolved)
            reason = "Only player GDScript files may be edited."
            if allowed:
                state_error = _write_state_error(Path(config["session_dir"]))
                if state_error:
                    allowed, reason = False, state_error
    elif name in {"execute_bash", "execute_pwsh", "execute_shell", "run_command", "shell",
                  "executeBash", "executePwsh", "runCommand"}:
        command = args.get("command", args.get("cmd"))
        allowed = mode == "baseline" and command in config["allowed_commands"].values()
        # The exact command may not be run under a different working directory.
        allowed = allowed and all((Path(v) if Path(v).is_absolute() else target / v).resolve() == target
                                  for v in paths)
        reason = "Only the exact session prepare, capture and finish commands are allowed."
    elif name in {"kiro_powers", "kiroPowers"}:
        action = args.get("action")
        allowed = power and mode == "tools" and args.get("powerName") == "kiroclops"
        if action == "activate":
            pass
        elif action == "readSkill":
            allowed = allowed and args.get("skillName") == "motion-calibration"
        elif action == "use":
            # Native Kiro's Power schema permits omitted/null arguments and
            # forwards those to MCP as {}. Keep this narrow: other falsey
            # values (strings, lists, booleans, numbers) remain invalid.
            arguments = args.get("arguments")
            allowed = (allowed and args.get("serverName") == "kiroclops"
                       and args.get("toolName") in MCP_TOOLS
                       and (arguments is None or isinstance(arguments, dict)))
        else:
            allowed = False
        reason = "Only activation, the motion-calibration skill and seven tools of the kiroclops Power are allowed."
    elif name in {"tool_load", "tool_call"}:
        # Kiro 1.x / CLI v3 can expose deferred MCP tools through this pair.
        identifiers = args.get("tool_ids", []) if name == "tool_load" else [args.get("tool_id")]
        accepted = {f"{namespace}::{tool}" for tool in MCP_TOOLS}
        if name == "tool_load":
            accepted.add(f"{namespace}::*")
        allowed = (mode == "tools" and isinstance(identifiers, list) and bool(identifiers)
                   and all(isinstance(item, str) and item in accepted for item in identifiers))
        reason = "Only this session's seven KiroClops MCP tools may be loaded or called."
    else:
        valid_names = {f"@{namespace}/{t}" for t in MCP_TOOLS} | {f"{namespace}/{t}" for t in MCP_TOOLS}
        valid_names |= {f"mcp_{namespace}_{t}" for t in MCP_TOOLS}
        valid_names |= {f"mcp__{namespace}__{t}" for t in MCP_TOOLS}
        valid_names |= {f"{namespace}___{t}" for t in MCP_TOOLS}
        valid_names |= {f"{namespace}::{t}" for t in MCP_TOOLS}
        allowed = mode == "tools" and name in valid_names
    return {"allow": bool(allowed), "reason": "Allowed session operation." if allowed else reason,
            "tool": tool, "paths": paths}


def guard_request(session_dir: Path, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
    session, _ = _session_target(session_dir)
    try:
        decision = check_tool_request(session, mode, payload)
    except (KiroError, OSError, ValueError, KeyError) as exc:
        decision = {"allow": False, "reason": str(exc)}
    log = session / "kiro" / "tool-audit.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": _utc(), **decision}, ensure_ascii=False) + "\n")
    return decision


def _parse_inventory(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict) and isinstance(result.get("models"), list):
            return result
    raise KiroError("model_inventory_unavailable", "Kiro did not return a model inventory; refusing model fallback.")


def preflight_kiro(session_dir: Path, mode: str = "tools", executable: str | Path | None = None,
                   *, connect: bool = True) -> dict[str, Any]:
    config = _load_config(session_dir, mode)
    executable = str(executable or shutil.which("kiro-cli") or "")
    if not executable:
        raise KiroError("cli_missing", "kiro-cli was not found on PATH.")
    if not _python().is_file():
        raise KiroError("runtime_missing", "The .venv-kiro Python runtime is missing.")
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(server_root()), "PYTHONUTF8": "1"})
    inventory = subprocess.run([executable, "chat", "--agent-engine", "v3", "--list-models", "--format", "json"],
                               cwd=config["target_dir"], env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=45, shell=False)
    if inventory.returncode:
        raise KiroError("model_inventory_unavailable", "Kiro model inventory failed; no model call was started.")
    models = _parse_inventory(inventory.stdout)
    if MODEL not in {item.get("model_id") for item in models["models"] if isinstance(item, dict)}:
        raise KiroError("model_unavailable", f"Exact requested model {MODEL} is unavailable; no fallback is allowed.")
    validation = subprocess.run([executable, "agent", "validate", "--path", config["agent_path"]],
                                cwd=config["target_dir"], env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30, shell=False)
    if validation.returncode:
        raise KiroError("agent_config_invalid", validation.stderr.strip() or validation.stdout.strip())
    version = subprocess.run([executable, "--version"], cwd=config["target_dir"], env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=15, shell=False)
    ready = {"executable": executable, "config": config, "inventory": models,
             "agent_validation": validation.stdout.strip(), "cli_version": version.stdout.strip()}
    if connect:
        directory = Path(session_dir).resolve() / "kiro" / "preflight"
        client = _ACPClient(ready, directory, 90, None)
        try:
            ready["acp_preflight"] = client.handshake()
            ready["kas_version"] = client.kas_version
            _json_file(directory / "result.json", {"prompt_sent": False, **ready["acp_preflight"],
                                                     "cli_version": ready["cli_version"], "kas_version": client.kas_version})
        finally:
            client.close()
    return ready


def _reported_models(event: Any) -> set[str]:
    """Read explicit selected/current model metadata, not available-model lists or message text."""
    if not isinstance(event, dict):
        return set()
    result = {value for key, value in event.items()
              if key in {"currentModelId", "selectedModelId", "model_id", "modelId"}
              and isinstance(value, str)}
    for key in ("data", "params", "update", "result", "metadata", "_meta", "kiro", "session", "models"):
        if isinstance(event.get(key), dict):
            result.update(_reported_models(event[key]))
    for option in event.get("configOptions", []) if isinstance(event.get("configOptions"), list) else []:
        if isinstance(option, dict) and option.get("category", option.get("id")) == "model":
            selected = option.get("currentValue")
            if isinstance(selected, str):
                result.add(selected)
    return result


def _stream_objects(event: Any):
    """Visit protocol envelopes only, never free text or advertised choices."""
    if not isinstance(event, dict):
        return
    yield event
    for key in ("data", "params", "update", "result", "metadata", "_meta", "kiro", "session", "models", "modes"):
        yield from _stream_objects(event.get(key))


class StreamConfigurationGuard:
    """Track selected config in stream order; startup defaults are not usage.

    This is an evidence guard, not a replacement for pre-prompt tool/config
    preflight. A generation event can arrive after a request has started.
    """
    def __init__(self, agent_name: str, model: str = MODEL, effort: str = EFFORT):
        self.expected = {"mode": agent_name, "model": model, "effort": effort}
        self.selected: dict[str, str] = {}
        self.generation_started = False
        self.observed_models: set[str] = set()
        self.events_seen = 0

    def _verify(self) -> None:
        for key, expected in self.expected.items():
            if self.selected.get(key) != expected:
                raise KiroError(f"{key}_mismatch", f"Kiro selected {key} {self.selected.get(key)!r}; expected {expected!r}.")

    def observe(self, event: dict[str, Any]) -> None:
        self.events_seen += 1
        objects = list(_stream_objects(event))
        for obj in objects:
            for source, key in (("currentModelId", "model"), ("selectedModelId", "model"),
                                ("modelId", "model"), ("model_id", "model"),
                                ("currentModeId", "mode"), ("effortLevel", "effort")):
                if isinstance(obj.get(source), str):
                    self.selected[key] = obj[source]
            options = obj.get("configOptions", [])
            for option in options if isinstance(options, list) else []:
                if not isinstance(option, dict):
                    continue
                key = option.get("category") or option.get("id")
                if key in {"effortLevel", "thought_level", "effort"}:
                    key = "effort"
                if key in self.expected and isinstance(option.get("currentValue"), str):
                    self.selected[key] = option["currentValue"]
        self.observed_models.update(_reported_models(event))
        boundary = False
        for obj in objects:
            kind = obj.get("sessionUpdate", obj.get("type"))
            if kind in {"agent_thought_chunk", "agent_message_chunk", "assistant", "messageStart"}:
                boundary = True
            elif kind == "tool_call":
                # Startup configuration sync is deterministic, not model use.
                meta = obj.get("_meta", {}).get("kiro", {})
                tool = meta.get("toolId", obj.get("name", obj.get("title")))
                if tool not in {"fetch_cloud_config", "Fetching your cloud config"}:
                    boundary = True
        if boundary or self.generation_started:
            self._verify()
        self.generation_started |= boundary

    def summary(self) -> dict[str, Any]:
        return {"selected": dict(self.selected), "expected": dict(self.expected),
                "generation_started": self.generation_started,
                "generation_configuration_verified": self.generation_started and self.selected == self.expected,
                "observed_models": sorted(self.observed_models), "events_seen": self.events_seen}


def _terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None and os.name == "nt":
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                           timeout=15, shell=False, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


class _WindowsJob:
    """Own the suspended CLI and every descendant with kill-on-close semantics.

    A suspended start avoids the race where KAS escapes before assignment. Only
    the primary thread belonging to our newly created process is resumed.
    """
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [w.LPVOID, w.LPCWSTR], w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes, k.AssignProcessToJobObject.restype = [w.HANDLE, w.HANDLE], w.BOOL
        k.CloseHandle.argtypes, k.CloseHandle.restype = [w.HANDLE], w.BOOL
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", w.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", Counters), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, process: subprocess.Popen) -> None:
        c, w, k = self.ctypes, self.w, self.kernel
        if not k.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle))):
            raise c.WinError(c.get_last_error())
        class ThreadEntry(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("thread_id", w.DWORD),
                ("owner_pid", w.DWORD), ("base_priority", w.LONG), ("delta_priority", w.LONG), ("flags", w.DWORD)]
        k.CreateToolhelp32Snapshot.argtypes, k.CreateToolhelp32Snapshot.restype = [w.DWORD, w.DWORD], w.HANDLE
        k.Thread32First.argtypes = k.Thread32Next.argtypes = [w.HANDLE, c.POINTER(ThreadEntry)]
        k.Thread32First.restype = k.Thread32Next.restype = w.BOOL
        k.OpenThread.argtypes, k.OpenThread.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.ResumeThread.argtypes, k.ResumeThread.restype = [w.HANDLE], w.DWORD
        snapshot = k.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = c.sizeof(entry)
            found = []
            valid = k.Thread32First(snapshot, c.byref(entry))
            while valid:
                if entry.owner_pid == process.pid:
                    found.append(entry.thread_id)
                valid = k.Thread32Next(snapshot, c.byref(entry))
            if len(found) != 1:
                raise KiroError("process_ownership_failed", "Suspended Kiro process did not have exactly one primary thread.")
            thread = k.OpenThread(0x2, False, found[0])  # THREAD_SUSPEND_RESUME
            if not thread:
                raise c.WinError(c.get_last_error())
            try:
                if k.ResumeThread(thread) != 1:
                    raise KiroError("process_ownership_failed", "Kiro primary thread was not suspended exactly once.")
            finally:
                k.CloseHandle(thread)
        finally:
            k.CloseHandle(snapshot)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _ACPClient:
    """Small public stdio ACP client; never contacts a private model endpoint.

    Filesystem and terminal capabilities are not delegated to this client. Kiro's
    native tools therefore retain its loaded agent permissions and PreToolUse hook.
    Unexpected interactive approvals are rejected, never globally trusted.
    """
    def __init__(self, ready: dict[str, Any], log_dir: Path, timeout: float,
                 cancel: threading.Event | None):
        self.config = ready["config"]
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.cancel = cancel
        self.deadline = time.monotonic() + timeout
        self.events: queue.Queue = queue.Queue()
        self.guard = StreamConfigurationGuard(self.config["agent_name"])
        self.session_id = None
        self.servers: dict[str, dict] = {}
        self.tags: set[str] = set()
        self.model_choices: set[str] = set()
        self.tool_inputs: dict[str, dict] = {}
        self.tool_identifiers: dict[str, str] = {}
        self.kas_version = None
        self.next_id = 0
        self.prompt_sent = False
        self.argv = [ready["executable"], "acp", "--agent-engine", "v3", "--auth-method", "cli"]
        env = os.environ.copy()
        env.update({"PYTHONPATH": str(server_root()), "PYTHONUTF8": "1",
                    "KIRO_DISABLE_SESSION_TITLE_LLM": "true"})
        self.job = _WindowsJob() if os.name == "nt" else None
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
                _terminate_tree(self.process)
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
            raise
        self.readers = []
        for stream, name in ((self.process.stdout, "stdout.jsonl"), (self.process.stderr, "stderr.log")):
            thread = threading.Thread(target=self._drain, args=(stream, name), daemon=True)
            thread.start()
            self.readers.append(thread)

    def _drain(self, stream, name: str) -> None:
        total = 0
        with (self.log_dir / name).open("w", encoding="utf-8", newline="\n") as handle:
            while True:
                line = stream.readline(1024 * 1024)
                if not line:
                    break
                total += len(line.encode("utf-8"))
                if total > 32 * 1024 * 1024:
                    self.events.put(("error", "log_limit"))
                    break
                handle.write(line)
                handle.flush()
                self.events.put((name, line))

    def _send(self, value: dict) -> None:
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _receive(self, deadline: float | None = None) -> dict | None:
        if self.cancel is not None and self.cancel.is_set():
            raise KiroError("cancelled", "Kiro run cancelled.")
        if time.monotonic() >= min(self.deadline, deadline or self.deadline):
            raise KiroError("timeout", "Kiro reached its bounded request or session deadline.")
        try:
            kind, line = self.events.get(timeout=0.1)
        except queue.Empty:
            if self.process.poll() is not None:
                raise KiroError("cli_failed", "Kiro ACP exited before completing the request.")
            return None
        if kind == "error":
            raise KiroError(line, "Kiro exceeded the bounded log size.")
        if kind == "stderr.log":
            if "kas.server.starting" in line:
                try:
                    self.kas_version = json.loads(line[line.index("{"):])["version"]
                except (ValueError, KeyError):
                    pass
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise KiroError("invalid_stream", "Kiro ACP emitted non-JSON stdout.")
        self.guard.observe(event)
        for obj in _stream_objects(event):
            for option in obj.get("configOptions", []) if isinstance(obj.get("configOptions"), list) else []:
                if isinstance(option, dict) and option.get("id") == "model":
                    self.model_choices = {item.get("value") for item in option.get("options", []) if isinstance(item, dict)}
        method, params = event.get("method"), event.get("params", {})
        if method == "session/update":
            update = params.get("update", {})
            if update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
                tool_call_id, raw = update.get("toolCallId"), update.get("rawInput")
                if isinstance(tool_call_id, str) and isinstance(raw, dict):
                    self.tool_inputs[tool_call_id] = raw
                    identifier = update.get("_meta", {}).get("kiro", {}).get("toolId")
                    if isinstance(identifier, str):
                        self.tool_identifiers[tool_call_id] = identifier
        if method == "_kiro/mcp/status":
            self.servers = {item["name"]: item for item in params.get("servers", [])}
        elif method == "_kiro/tools/didChange":
            self.tags = {item.get("tag") for item in params.get("tags", [])}
        if "method" in event and "id" in event:
            if method == "session/request_permission":
                self._permission_reply(event)
                return event
            self._send({"jsonrpc": "2.0", "id": event["id"], "error": {
                "code": -32601, "message": "Client capability is not enabled."}})
        return event

    def _permission_reply(self, event: dict) -> None:
        """Grant one operation only, bound to prior structured tool input.

        Workspace agent policy cannot grant every MCP permission or restrict all
        outside reads. The enabled native hook enforces every call; this second
        guard handles interactive approvals without trusting a friendly title.
        """
        params = event.get("params", {})
        tool_call_id = params.get("toolCall", {}).get("toolCallId")
        tool = params.get("_meta", {}).get("kiro", {}).get("toolId")
        raw = self.tool_inputs.get(tool_call_id)
        original_tool = self.tool_identifiers.get(tool_call_id)
        if not isinstance(tool, str) or not isinstance(raw, dict) or (original_tool is not None and original_tool != tool):
            decision = {"allow": False, "reason": "Permission lacks bound structured tool arguments."}
        else:
            decision = guard_request(Path(self.config["session_dir"]), self.config["mode"],
                                     {"tool_name": tool, "tool_input": raw})
        once = next((o.get("optionId") for o in params.get("options", [])
                     if isinstance(o, dict) and o.get("kind") == "allow_once"), None)
        outcome = {"outcome": "selected", "optionId": once} if decision["allow"] and once else {"outcome": "cancelled"}
        self._send({"jsonrpc": "2.0", "id": event["id"], "result": {"outcome": outcome}})
        if outcome["outcome"] == "cancelled":
            raise KiroError("tool_denied", decision["reason"])

    def request(self, method: str, params: dict, timeout: float = 45) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = min(self.deadline, time.monotonic() + timeout)
        while True:
            event = self._receive(deadline)
            if not event or event.get("id") != request_id or "method" in event:
                continue
            if "error" in event:
                raise KiroError("acp_request_failed", f"{method}: {event['error']}")
            return event.get("result", {})

    def handshake(self) -> dict[str, Any]:
        initialized = self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "_meta": {"kiro": {"hooks": {"enabled": True, "v2": True}}}},
            "clientInfo": {"name": "kiroclops", "version": "0.2"}})
        if initialized.get("protocolVersion") != 1:
            raise KiroError("acp_protocol_mismatch", "Kiro did not negotiate ACP protocol 1.")
        created = self.request("session/new", {"cwd": self.config["target_dir"], "mcpServers": []})
        self.session_id = created.get("sessionId")
        if not self.session_id:
            raise KiroError("acp_session_missing", "Kiro did not return an ACP session ID.")
        # Verify returned location and agent origin, not merely a requested name.
        roots = created.get("_meta", {}).get("workspacePaths", [])
        target = Path(self.config["target_dir"]).resolve()
        if not roots or any(Path(root).resolve() != target for root in roots):
            raise KiroError("workspace_mismatch", "Kiro's session workspace differs from the prepared target.")
        profiles = created.get("modes", {}).get("availableModes", [])
        profile = next((p for p in profiles if p.get("id") == self.config["agent_name"]), {})
        origin = profile.get("_meta", {}).get("kiro", {}).get("resource", {}).get("source", {})
        if origin.get("origin") != "workspace" or Path(origin.get("root", ".")).resolve() != target:
            raise KiroError("agent_not_loaded", "The requested agent was not loaded from this target workspace.")
        self.request("session/set_mode", {"sessionId": self.session_id, "modeId": self.config["agent_name"]})
        deadline = min(self.deadline, time.monotonic() + 45)
        while MODEL not in self.model_choices:
            self._receive(deadline)
        for key, value in (("model", MODEL), ("effortLevel", EFFORT)):
            self.request("session/set_config_option", {"sessionId": self.session_id, "configId": key, "value": value})
        self.guard._verify()
        expected = {f"@kiroclops/{tool}" for tool in MCP_TOOLS}
        deadline = min(self.deadline, time.monotonic() + 40)
        while True:
            server = self.servers.get("kiroclops", {})
            tools = {item.get("name") for item in server.get("tools", []) if not item.get("disabled")}
            if self.config["mode"] == "tools":
                if server.get("status") in {"failed", "error"}:
                    raise KiroError("mcp_startup_failed", "KiroClops MCP failed during initialization.")
                complete = server.get("status") == "connected" and tools == set(MCP_TOOLS) and expected <= self.tags
            else:
                complete = {"read", "write", "shell"} <= self.tags and not any(tag.startswith("@") for tag in self.tags)
            if complete:
                break
            self._receive(deadline)
        permitted_tags = {"read", "write"} | (expected if self.config["mode"] == "tools" else {"shell"})
        if self.tags - permitted_tags:
            raise KiroError("unexpected_tools", "Kiro exposed tools beyond the session agent's allowed set.")
        hooks = self.request("_kiro/hooks/list", {"sessionId": self.session_id,
            "workspacePaths": [str(target)], "trigger": "PreToolUse"}).get("hooks", [])
        enabled = [hook for hook in hooks if hook.get("_meta", {}).get("enabled") is True]
        hook_path = target / ".kiro/hooks/kiroclops-guard.json"
        command = json.loads(hook_path.read_text(encoding="utf-8"))["hooks"][0]["action"]["command"]
        if len(enabled) != 1 or Path(enabled[0].get("_meta", {}).get("filePath", ".")).resolve() != hook_path or enabled[0].get("action", {}).get("command") != command:
            raise KiroError("hook_not_loaded", "The exact session boundary hook is not the sole enabled PreToolUse hook.")
        if self.guard.generation_started:
            raise KiroError("premature_generation", "Model generation began before the client sent the task.")
        return {"session_id": self.session_id, "workspace_verified": str(target),
                "agent_verified": self.config["agent_name"], "selected": dict(self.guard.selected),
                "mcp_tools": sorted(tools) if self.config["mode"] == "tools" else [],
                "tool_tags": sorted(self.tags), "boundary_hook_verified": str(hook_path), "prompt_sent": False}

    def prompt(self, text: str) -> dict:
        # Recheck controls and selected model immediately before paid generation.
        _load_config(Path(self.config["session_dir"]), self.config["mode"])
        self.guard._verify()
        self.prompt_sent = True
        return self.request("session/prompt", {"sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}]}, timeout=max(0.1, self.deadline - time.monotonic()))

    def close(self) -> list[str]:
        errors = []
        if self.job:
            self.job.close()  # Kills descendants even if the parent already exited.
        _terminate_tree(self.process)
        for reader in self.readers:
            reader.join(timeout=3)
        if any(reader.is_alive() for reader in self.readers):
            # Closing a TextIOWrapper while another thread holds its read lock
            # could hang indefinitely. Preserve evidence and return boundedly.
            return ["A log reader did not exit after owned process cleanup."]
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError as exc:
                    errors.append(str(exc))
        return errors


def run_kiro(session_dir: Path, mode: str = "tools", *, prompt_path: Path | None = None,
             timeout: float = MAX_SECONDS, executable: str | Path | None = None,
             cancel: threading.Event | None = None) -> dict[str, Any]:
    """Run official Kiro CLI over ACP after a verified no-prompt handshake.

    Explicit workspace, workspace agent origin, model, effort and seven MCP tools
    must be confirmed before session/prompt. A completed turn is not experiment PASS.
    """
    if not 0 < timeout <= MAX_SECONDS:
        raise KiroError("invalid_timeout", f"timeout must be in (0, {MAX_SECONDS}].")
    if cancel is not None and cancel.is_set():
        raise KiroError("cancelled", "Launch was cancelled before starting Kiro.")
    ready = preflight_kiro(session_dir, mode, executable, connect=False)
    config = ready["config"]
    session, target = _session_target(session_dir)
    prompt_file = Path(prompt_path or config["prompt_path"]).resolve(strict=True)
    if not prompt_file.is_relative_to(target):
        raise KiroError("unsafe_prompt", "The prompt must be a file inside this target workspace.")
    prompt = prompt_file.read_text(encoding="utf-8")
    if not prompt.strip():
        raise KiroError("empty_prompt", "Kiro requires a nonempty prepared prompt.")
    log_dir = session / "kiro"
    log_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = log_dir / "launch.json"
    if metadata_path.exists():
        raise KiroError("already_launched", "This session already has launch evidence; use a fresh session.")
    record = {"schema_version": 2, "surface": "cli", "transport": "acp", "status": "starting", "started_at": _utc(),
              "mode": mode, "model_requested": MODEL, "effort_requested": EFFORT,
              "timeout_seconds": timeout, "cwd": str(target),
              "prompt_path": str(prompt_file), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
              "model_inventory_verified": True, "model_reported": [], "prompt_sent": False,
              "cli_version": ready.get("cli_version"),
              "stdout": str(log_dir / "stdout.jsonl"), "stderr": str(log_dir / "stderr.log")}
    from .session import Session
    Session(session).start_clock()
    _json_file(log_dir / "model-inventory.json", ready["inventory"])
    _json_file(metadata_path, record)
    started = time.monotonic()
    client = None
    try:
        client = _ACPClient(ready, log_dir, timeout, cancel)
        record.update({"pid": client.process.pid, "argv": client.argv, "status": "preflighting"})
        _json_file(metadata_path, record)
        record["acp_preflight"] = client.handshake()
        record["status"] = "running"
        record["prompt_sent_at"] = _utc()
        _json_file(metadata_path, record)
        result = client.prompt(prompt)
        record["prompt_result"] = result
        record["status"] = "completed" if result.get("stopReason") == "end_turn" else "turn_stopped"
        if not client.guard.generation_started:
            raise KiroError("generation_unverified", "No model-generation event was observed in the ACP stream.")
    except (KiroError, OSError, BrokenPipeError) as exc:
        record["status"] = exc.code if isinstance(exc, KiroError) else "launch_failed"
        record["error"] = str(exc)
    finally:
        if client is not None:
            record["prompt_sent"] = client.prompt_sent
            record["kas_version"] = client.kas_version
            summary = client.guard.summary()
            record["stream_configuration"] = summary
            record["model_reported"] = [MODEL] if summary["generation_configuration_verified"] else []
            record["model_confirmation"] = "stream_confirmed" if summary["generation_configuration_verified"] else "generation_unverified"
            try:
                cleanup_errors = client.close()
                if cleanup_errors:
                    record["cleanup_errors"] = cleanup_errors
                    record["status"] = "cleanup_failed"
            except Exception as exc:
                record["cleanup_errors"] = [str(exc)]
                record["status"] = "cleanup_failed"
            record["process_returncode_after_cleanup"] = client.process.poll()
        record["finished_at"] = _utc()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _json_file(metadata_path, record)
    return record
