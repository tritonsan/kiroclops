Apply horizontal acceleration and braking only while the player is actually grounded.
Review state-transition update order, including the first airborne update before
Idle changes to Fall or Run changes to Fall. Preserve the starting horizontal
air-control behavior on those boundary updates as well as during sustained flight.
Also preserve short and long normal jumps, double jumps, and wall launches.

An independent runtime review will additionally check the grounded-to-airborne
boundary and a run/jump/land/brake sequence. These additional checks are separate
from the seven existing MCP tools; their results are not yet included in MCP
candidate eligibility. Do not claim that an MCP pass proves those separate checks.

Use only calibration feedback for tuning. Do not inspect other sessions, old
solutions, reference game source, or held-out results before finalization. Author
the actual movement code yourself. No controller patch is supplied by the tools.
