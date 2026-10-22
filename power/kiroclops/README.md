# KiroClops Power

This folder uses Kiro's current Agent Plugins format: `plugin.json`, `mcp.json`
and `skills/motion-calibration/SKILL.md`.

Run the repository's session preparation first, then
`python scripts/prepare_kiro.py SESSION_DIRECTORY --mode tools`. It writes only
the prepared target's `.kiro` configuration and a session-specific copy of this
Power with resolved executable/session paths. It does not modify account settings
or install a global Power.

For an IDE run, before the session starts, run preparation with `--surface ide`.
This removes the direct MCP copy from the selected agent and workspace config;
the agent uses only the native `kiro_powers` tool, restricted to this Power.
In the Kiro IDE, open that target folder. For Power activation, use **Powers → Add Custom Power →
Import power from a folder** and choose `target/.kiro/powers/kiroclops`. Select the
generated custom agent and verify **Claude Opus 5**, **high** effort before sending
the prepared `.kiro/kiroclops-prompt.md` prompt. Importing a folder is a separate
IDE action; copying a directory is not evidence that the IDE installed it.
Verify native `activate`, `readSkill` and `use` calls in the run evidence. The
Power's MCP namespace is `power-kiroclops-kiroclops`. Agent Plugins forbids absolute
`command` paths and custom-agent `timeout` fields. On the verified Windows Kiro
client, child-only PATH resolves bare `python.exe` to the venv; MCP checks the
actual interpreter path and refuses a mismatch. This client-specific resolution
is not a cross-client portability guarantee. No shell or global PATH change is
used. The Power-only parent watcher closes owned jobs if its launcher disappears.

The CLI agent consumes the exact same skill text and MCP server through its local
agent profile. `scripts/run_kiro.py` launches official CLI v3 over public stdio ACP.
Before sending any task, it verifies the returned workspace, workspace-origin
agent, exact model/effort and all seven connected MCP tools. Model/effort are
selected through `session/set_config_option`. Runtime, stream logs and session-local
audit hooks are bounded.
CLI evidence is labelled CLI evidence, not an IDE demonstration.

The raw-reference baseline has the same target, raw references and candidate/time
budget, but no Power or measurement MCP tools. Its only shell operations are the
exact prepare, capture and finish commands generated for the session.

Permission rules and a pre-tool guard limit the agent's file/tool access. These
are experiment controls, not an operating-system sandbox: Godot executes the
controller code. Use a disposable environment when evaluating untrusted code.
The ACP client explicitly negotiates Kiro's native hooks capability and checks
the loaded boundary hook. Hook denials exit with code 2. Interactive tool approvals
require structured arguments bound to the same call and grant only `allow_once`.

Format and behavior references checked 2026-09-18:

- https://kiro.dev/docs/powers/create/
- https://kiro.dev/docs/cli/v3/agent-config/
- https://kiro.dev/docs/permissions/
- https://kiro.dev/docs/hooks/
- https://kiro.dev/docs/cli/acp/

Installed probes: IDE 1.1.14 supports `kiro chat --mode`, but its help exposes no
model/effort flags; `kiro agent` is unavailable in this installation because its
`kiro-tunnel.exe` companion is missing. CLI 2.21.0 / KAS 0.54.8 no-prompt ACP
initialization verified the target workspace, exact model/effort and all seven
MCP tools. A headless-chat attempt did not establish correct tool integration
and is excluded from benchmark evidence; the launcher now gates the prompt on
the live ACP handshake.
No native IDE run is claimed solely from these probes.
