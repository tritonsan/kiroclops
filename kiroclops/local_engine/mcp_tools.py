"""The MCP tool set the local engine's session server exposes (PHASE4_V6_PLAN.md WP3.2).

``kiroclops.integration.kiro.MCP_TOOLS`` stays the frozen seven-tool tuple. Historical
portable workflows and the pinned native reader (``review.py`` through
``launcher.MCP_TOOLS``) replay recorded sessions against it, so it never changes.

The V6 session server adds ``inspect_window``. The V6 launcher configures, guards and
verifies this active set; a reference draft still exposes only the frozen seven.
"""
from kiroclops.integration.kiro import MCP_TOOLS as FROZEN_MCP_TOOLS

ACTIVE_MCP_TOOLS = (*FROZEN_MCP_TOOLS, 'inspect_window')
