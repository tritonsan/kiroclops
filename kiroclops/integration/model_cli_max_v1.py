"""Explicit maximum-effort CLI adapter; previous high-effort evidence is immutable.

Only fresh session-local controls and launch evidence are written here. The
frozen CLI module supplies native process ownership, the seven-tool boundary,
stream parsing and permissions; its MODEL global is never changed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any

from . import kiro as frozen

KiroError = frozen.KiroError
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "max"
SUPPORTED_MODELS = frozenset({DEFAULT_MODEL})
REVISION = "model-cli-max-v1"


def _selection(model: str, effort: str) -> None:
    if not isinstance(model, str) or model not in SUPPORTED_MODELS or effort != DEFAULT_EFFORT:
        raise KiroError("unsupported_selection", "This adapter supports exact gpt-5.6-sol/max only; no fallback.")


def launcher_binding() -> dict[str, Any]:
    """Bind this extension and the existing safety implementation by source bytes."""
    root = frozen.server_root()
    paths = (Path(__file__).resolve(), root / "kiroclops/integration/model_cli_v1.py",
             Path(frozen.__file__).resolve(), root / "scripts/run_kiro.py")
    return {"revision": REVISION, "source_hashes": {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}


def configure_model_cli(session_dir: Path, *, model: str = DEFAULT_MODEL,
                        effort: str = DEFAULT_EFFORT,
                        supplemental_instructions: str | None = None) -> dict[str, Any]:
    """Configure a new, unstarted tools session; never replace previous controls."""
    _selection(model, effort)
    if supplemental_instructions is not None and (
            not isinstance(supplemental_instructions, str) or not supplemental_instructions.strip()
            or "\x00" in supplemental_instructions):
        raise KiroError("invalid_instructions", "Supplemental instructions must be nonempty plain text.")
    session, target = frozen._session_target(session_dir)
    state = json.loads((session / "session.json").read_text(encoding="utf-8"))
    if (state.get("schema_version") != 3 or state.get("experiment_id") != "ground-motion-v1"
            or state.get("session_id") != session.name or state.get("mode") != "tools"
            or state.get("status") != "new"
            or any(state.get(k) is not None for k in ("started_at", "deadline_at", "selected_candidate_id", "sealed_at", "active_job_id"))
            or state.get("candidates") not in (None, [])
            or (session / "kiro/launch.json").exists()):
        raise KiroError("not_fresh", "An unstarted ground-motion-v1 tools session is required.")
    config_dir = target / ".kiro"
    if config_dir.is_symlink() or (config_dir.exists() and (not config_dir.is_dir() or any(config_dir.iterdir()))):
        raise KiroError("existing_config", "Model-specific controls require an unconfigured fresh session.")
    binding = launcher_binding()
    # Preserve the exact MCP, permissions, hook and workflow format. Only these
    # newly generated model fields are replaced before any client can be started.
    config = frozen.configure_kiro(session, mode="tools", surface="cli")
    agent_path, prompt_path = Path(config["agent_path"]), Path(config["prompt_path"])
    agent = json.loads(agent_path.read_text(encoding="utf-8"))
    agent.update(model=model, effortLevel=effort)
    prompt = prompt_path.read_text(encoding="utf-8")
    old_line = f"Mode: tools. Model: {frozen.MODEL}. Effort: {frozen.EFFORT}."
    if prompt.count(old_line) != 1:
        raise KiroError("unexpected_config", "The frozen prompt model field was not found exactly once.")
    prompt = prompt.replace(old_line, f"Mode: tools. Model: {model}. Effort: {effort}.")
    changed_paths = [agent_path, prompt_path]
    if supplemental_instructions is not None:
        addition = "\n\n## Supplemental cohort instructions\n\n" + supplemental_instructions + "\n"
        agent["prompt"] += addition
        prompt += addition
        for relative in ("kiroclops-instructions.md", "powers/kiroclops/skills/motion-calibration/SKILL.md"):
            path = config_dir / relative
            path.write_bytes((path.read_text(encoding="utf-8") + addition).encode("utf-8"))
            changed_paths.append(path)
        config["supplemental_instructions"] = {
            "kind": "cohort_instruction", "enforced_by_mcp": False,
            "sha256": hashlib.sha256(supplemental_instructions.encode("utf-8")).hexdigest()}
        config["instruction_sha256"] = hashlib.sha256(agent["prompt"].encode("utf-8")).hexdigest()
    agent_path.write_bytes((json.dumps(agent, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    prompt_path.write_bytes(prompt.encode("utf-8"))
    config.update(model=model, effort=effort, model_launcher=binding)
    for path in changed_paths:
        config["generated_hashes"][path.relative_to(config_dir).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    frozen._json_file(config_dir / "kiroclops-session.json", config)
    return config


def load_model_config(session_dir: Path) -> dict[str, Any]:
    config = frozen._load_config(session_dir, "tools")
    _selection(config.get("model"), config.get("effort"))
    if (config.get("surface") != "cli" or config.get("integration") != "custom_agent_mcp"
            or config.get("experiment_id") != "ground-motion-v1"
            or config.get("model_launcher") != launcher_binding()):
        raise KiroError("launcher_changed", "The CLI selection or bound launcher source has changed.")
    return config


class ModelACPClient(frozen._ACPClient):
    """Native ACP transport with explicit expected selection and fresh option echoes."""

    def __init__(self, ready: dict[str, Any], log_dir: Path, timeout: float,
                 cancel: threading.Event | None):
        _selection(ready["config"].get("model"), ready["config"].get("effort"))
        self.model_echo = 0
        self.effort_echo = 0
        self.effort_options: dict[str, Any] | None = None
        self.outbound_path = Path(log_dir) / 'outbound.jsonl'
        self.outbound_path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve every previous preflight/run; never quietly replace its input journal.
        with self.outbound_path.open('x', encoding='utf-8'):
            pass
        self._outbound_sequence = 0
        self._task_prompt_count = 0
        super().__init__(ready, log_dir, timeout, cancel)
        # The inherited readers only queue bytes; _receive/handshake have not run.
        # Pass both values explicitly instead of inheriting Opus default arguments.
        self.guard = frozen.StreamConfigurationGuard(
            self.config["agent_name"], model=self.config["model"], effort=self.config["effort"])

    def _send(self, value: dict) -> None:
        """Journal actual outbound ACP and enforce one unchanged prepared task."""
        if value.get('method') == 'session/prompt':
            if self._task_prompt_count:
                raise KiroError('multiple_prompts_forbidden', 'Autonomous trials accept exactly one task; no follow-up coaching.')
            expected = Path(self.config['prompt_path']).read_text(encoding='utf-8')
            params = value.get('params', {})
            if (params.get('sessionId') != self.session_id
                    or params.get('prompt') != [{'type': 'text', 'text': expected}]):
                raise KiroError('prompt_changed', 'Only the registered prepared task may be sent to this session.')
            self._task_prompt_count += 1
        if self.outbound_path.stat().st_size > 32 * 1024 * 1024:
            raise KiroError('log_limit', 'Outbound ACP journal exceeded its bound.')
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        payload_sha = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        self._outbound_sequence += 1
        base = {'sequence': self._outbound_sequence, 'payload_sha256': payload_sha}
        def record(phase, **extra):
            with self.outbound_path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps({**base, 'phase': phase, 'at': frozen._utc(), **extra},
                                        ensure_ascii=False, allow_nan=False) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
        record('attempt', message=value)
        super()._send(value)
        record('sent')

    def _observe_options(self, event: dict[str, Any] | None) -> None:
        for obj in frozen._stream_objects(event):
            options = obj.get("configOptions", [])
            for option in options if isinstance(options, list) else []:
                if not isinstance(option, dict):
                    continue
                if option.get("id") == "model" and isinstance(option.get("currentValue"), str):
                    self.model_echo += 1
                if option.get("id") in {"effortLevel", "effort", "thought_level"} or option.get("category") == "thought_level":
                    if isinstance(option.get("currentValue"), str):
                        self.effort_echo += 1
                    choices = option.get("options")
                    if isinstance(option.get("id"), str) and isinstance(choices, list):
                        self.effort_options = {
                            "config_id": option["id"], "observed_model": self.guard.selected.get("model"),
                            "values": sorted({x["value"] for x in choices if isinstance(x, dict) and isinstance(x.get("value"), str)})}

    def _receive(self, deadline: float | None = None):
        event = super()._receive(deadline)
        self._observe_options(event)
        return event

    def handshake(self) -> dict[str, Any]:
        initialized = self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "_meta": {"kiro": {"hooks": {"enabled": True, "v2": True}}}},
            "clientInfo": {"name": "kiroclops-model-cli", "version": "1"}})
        if initialized.get("protocolVersion") != 1:
            raise KiroError("acp_protocol_mismatch", "Kiro did not negotiate ACP protocol 1.")
        created = self.request("session/new", {"cwd": self.config["target_dir"], "mcpServers": []})
        self.session_id = created.get("sessionId")
        if not self.session_id:
            raise KiroError("acp_session_missing", "Kiro did not return an ACP session ID.")
        target = Path(self.config["target_dir"]).resolve()
        roots = created.get("_meta", {}).get("workspacePaths", [])
        if not roots or any(Path(root).resolve() != target for root in roots):
            raise KiroError("workspace_mismatch", "Kiro's session workspace differs from the prepared target.")
        profiles = created.get("modes", {}).get("availableModes", [])
        profile = next((p for p in profiles if p.get("id") == self.config["agent_name"]), {})
        origin = profile.get("_meta", {}).get("kiro", {}).get("resource", {}).get("source", {})
        if origin.get("origin") != "workspace" or Path(origin.get("root", ".")).resolve() != target:
            raise KiroError("agent_not_loaded", "The requested agent was not loaded from this target workspace.")
        self.request("session/set_mode", {"sessionId": self.session_id, "modeId": self.config["agent_name"]})
        model, effort = self.config["model"], self.config["effort"]
        deadline = min(self.deadline, time.monotonic() + 45)
        while model not in self.model_choices:
            self._receive(deadline)
        before_model = self.model_echo
        self.effort_options = None  # Never credit the initialization model's choices.
        self.request("session/set_config_option", {"sessionId": self.session_id, "configId": "model", "value": model})
        while (self.model_echo <= before_model or self.guard.selected.get("model") != model
               or not self.effort_options or self.effort_options["observed_model"] != model):
            self._receive(deadline)
        supported = dict(self.effort_options)
        if effort not in supported["values"]:
            raise KiroError("effort_unavailable", f"The selected model does not expose requested effort {effort!r}.")
        before_effort = self.effort_echo
        self.request("session/set_config_option", {"sessionId": self.session_id,
                     "configId": supported["config_id"], "value": effort})
        while self.effort_echo <= before_effort:
            self._receive(deadline)
        self.guard._verify()
        expected = {f"@kiroclops/{tool}" for tool in frozen.MCP_TOOLS}
        deadline = min(self.deadline, time.monotonic() + 40)
        while True:
            server = self.servers.get("kiroclops", {})
            tools = {item.get("name") for item in server.get("tools", []) if not item.get("disabled")}
            if server.get("status") in {"failed", "error"}:
                raise KiroError("mcp_startup_failed", "KiroClops MCP failed during initialization.")
            if server.get("status") == "connected" and tools == set(frozen.MCP_TOOLS) and expected <= self.tags:
                break
            self._receive(deadline)
        if self.tags - ({"read", "write"} | expected):
            raise KiroError("unexpected_tools", "Kiro exposed tools beyond the session agent's allowed set.")
        hooks = self.request("_kiro/hooks/list", {"sessionId": self.session_id,
            "workspacePaths": [str(target)], "trigger": "PreToolUse"}).get("hooks", [])
        enabled = [hook for hook in hooks if hook.get("_meta", {}).get("enabled") is True]
        hook_path = target / ".kiro/hooks/kiroclops-guard.json"
        command = json.loads(hook_path.read_text(encoding="utf-8"))["hooks"][0]["action"]["command"]
        if (len(enabled) != 1 or Path(enabled[0].get("_meta", {}).get("filePath", ".")).resolve() != hook_path
                or enabled[0].get("action", {}).get("command") != command):
            raise KiroError("hook_not_loaded", "The exact session boundary hook is not the sole enabled PreToolUse hook.")
        if self.guard.generation_started:
            raise KiroError("premature_generation", "Model generation began before the client sent the task.")
        return {"session_id": self.session_id, "workspace_verified": str(target),
                "agent_verified": self.config["agent_name"], "selected": dict(self.guard.selected),
                "mcp_tools": sorted(tools), "tool_tags": sorted(self.tags),
                "boundary_hook_verified": str(hook_path), "effort_selection": {**supported, "selected": effort},
                "selection_echo_verified": True, "prompt_sent": False}

    def prompt(self, text: str) -> dict:
        if self.cancel is not None and self.cancel.is_set():
            raise KiroError("cancelled", "Launch was cancelled before sending the task.")
        if time.monotonic() >= self.deadline:
            raise KiroError("timeout", "The session deadline elapsed before sending the task.")
        if load_model_config(Path(self.config["session_dir"])) != self.config:
            raise KiroError("config_changed", "Model controls changed after the preflight.")
        return super().prompt(text)


def preflight_model_cli(session_dir: Path, *, executable: str | Path | None = None,
                        connect: bool = True) -> dict[str, Any]:
    config = load_model_config(session_dir)
    executable = str(executable or shutil.which("kiro-cli") or "")
    if not executable:
        raise KiroError("cli_missing", "kiro-cli was not found on PATH.")
    if not frozen._python().is_file():
        raise KiroError("runtime_missing", "The .venv-kiro Python runtime is missing.")
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(frozen.server_root()), "PYTHONUTF8": "1"})
    inventory = subprocess.run([executable, "chat", "--agent-engine", "v3", "--list-models", "--format", "json"],
        cwd=config["target_dir"], env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=45, shell=False)
    if inventory.returncode:
        raise KiroError("model_inventory_unavailable", "Kiro model inventory failed; no model call was started.")
    models = frozen._parse_inventory(inventory.stdout)
    if config["model"] not in {m.get("model_id") for m in models["models"] if isinstance(m, dict)}:
        raise KiroError("model_unavailable", f"Exact requested model {config['model']} is unavailable; no fallback.")
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
        directory = Path(session_dir).resolve() / "kiro/preflight"
        client = ModelACPClient(ready, directory, 90, None)
        try:
            ready["acp_preflight"] = client.handshake()
            ready["kas_version"] = client.kas_version
            frozen._json_file(directory / "result.json", {"prompt_sent": False, **ready["acp_preflight"],
                "cli_version": ready["cli_version"], "kas_version": client.kas_version,
                "model_launcher": config["model_launcher"]})
        finally:
            errors = client.close()
            if errors:
                raise KiroError("cleanup_failed", "No-prompt ACP cleanup failed: " + "; ".join(errors))
    return ready


def run_model_cli(session_dir: Path, *, timeout: float = frozen.MAX_SECONDS,
                  executable: str | Path | None = None,
                  cancel: threading.Event | None = None) -> dict[str, Any]:
    """One native ACP run, retaining the original clock and owned-process lifecycle."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= frozen.MAX_SECONDS:
        raise KiroError("invalid_timeout", f"timeout must be in (0, {frozen.MAX_SECONDS}].")
    if cancel is not None and cancel.is_set():
        raise KiroError("cancelled", "Launch was cancelled before starting Kiro.")
    ready = preflight_model_cli(session_dir, executable=executable, connect=False)
    config = ready["config"]
    session, target = frozen._session_target(session_dir)
    prompt_file = Path(config["prompt_path"]).resolve(strict=True)
    if not prompt_file.is_relative_to(target):
        raise KiroError("unsafe_prompt", "The prompt must be inside this target workspace.")
    prompt = prompt_file.read_text(encoding="utf-8")
    if not prompt.strip():
        raise KiroError("empty_prompt", "Kiro requires a nonempty prepared prompt.")
    log_dir = session / "kiro"
    log_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = log_dir / "launch.json"
    if metadata_path.exists():
        raise KiroError("already_launched", "Use a fresh session; launch evidence already exists.")
    record = {"schema_version": 2, "surface": "cli", "transport": "acp", "status": "starting", "started_at": frozen._utc(),
        "mode": "tools", "model_requested": config["model"], "effort_requested": config["effort"],
        "model_launcher": config["model_launcher"], "timeout_seconds": timeout, "cwd": str(target),
        "prompt_path": str(prompt_file), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "model_inventory_verified": True, "model_reported": [], "prompt_sent": False,
        "cli_version": ready.get("cli_version"), "stdout": str(log_dir / "stdout.jsonl"), "stderr": str(log_dir / "stderr.log")}
    record['outbound_journal'] = str(log_dir / 'outbound.jsonl')
    from .session import Session
    Session(session).start_clock()
    frozen._json_file(log_dir / "model-inventory.json", ready["inventory"])
    frozen._json_file(metadata_path, record)
    started, client = time.monotonic(), None
    try:
        client = ModelACPClient(ready, log_dir, timeout, cancel)
        record.update(pid=client.process.pid, argv=client.argv, status="preflighting")
        frozen._json_file(metadata_path, record)
        record["acp_preflight"] = client.handshake()
        record.update(status="running", prompt_sent_at=frozen._utc())
        frozen._json_file(metadata_path, record)
        result = client.prompt(prompt)
        record["prompt_result"] = result
        record["status"] = "completed" if result.get("stopReason") == "end_turn" else "turn_stopped"
        if not client.guard.generation_started:
            raise KiroError("generation_unverified", "No model-generation event was observed in the ACP stream.")
    except (KiroError, OSError, BrokenPipeError) as exc:
        record.update(status=exc.code if isinstance(exc, KiroError) else "launch_failed", error=str(exc))
    finally:
        if client is not None:
            record.update(prompt_sent=client.prompt_sent, kas_version=client.kas_version)
            summary = client.guard.summary()
            record["stream_configuration"] = summary
            record["model_reported"] = [config["model"]] if summary["generation_configuration_verified"] else []
            record["model_confirmation"] = "stream_confirmed" if summary["generation_configuration_verified"] else "generation_unverified"
            try:
                errors = client.close()
                if errors:
                    record.update(cleanup_errors=errors, status="cleanup_failed")
            except Exception as exc:
                record.update(cleanup_errors=[str(exc)], status="cleanup_failed")
            record["process_returncode_after_cleanup"] = client.process.poll()
        record.update(finished_at=frozen._utc(), elapsed_seconds=round(time.monotonic() - started, 3))
        frozen._json_file(metadata_path, record)
    return record
