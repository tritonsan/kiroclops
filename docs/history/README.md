# Engineering history

KiroClops was built in phases. Every phase began with a written plan approved before execution, and every results log keeps failed attempts next to successful ones. Those internal working documents are not part of this repository; this page summarises them in order. For the current state, read [`README.md`](../../README.md), [`ARCHITECTURE.md`](../../ARCHITECTURE.md) and the [technical report](../../submission/TECHNICAL_REPORT.md).

## Phase 1 — 18 September 2026: motion profile from video

- Implementation plan approved on 18 September. The acceptance criteria were met: a motion profile was extracted from Tiny Crate Classic frames.

## Phase 2 — 18–21 September: Kiro-driven motion transfer

- Kiro-managed transfer; three independent tool-using CLI runs.
- A second mechanic that must preserve the earlier gain; implemented, but the phase was not accepted.
- CLI fixes and pre-checks.
- CLI trials with another model configuration.
- Autonomous error repair, done in 22 min 25 s with two candidates.
- Autonomous repair that preserves the air response; 3/3 pre-registered CLI runs passed.
- Autonomous transfer to a second game (Heart Platformer): partial success, followed by post-hoc review corrections.
- Heart preservation feedback v2: full acceptance, 3/3 chains.

## Phase 3 — 21 September–15 October: local engine, AWS service, user flow, behaviour repair

- The local engineering engine (sessions, guarded tools, evidence); completed on 1 October after repair.
- The measurement-first AWS analysis service. Internal AWS/CLI evidence was completed; the invited-beta plan was left out of hackathon scope.
- Local user flow and demo reliability.
- Behaviour repair with acceptance in three domains (game, SVG, web panel); Kiro writes the motion code.
- The realistic acceptance profile, recorder and validation repairs, and pending scenarios.
- Measurement-based acceptance for the remaining scenarios (9 October).
- Game and panel V4 (10 October): the game domain was accepted; the panel was not.
- Panel and SVG V5/V5b (13 October): the SVG domain was accepted on 15 October; the panel was not.

## Phase 4 — 15–22 October: the V6 engineering epoch (OpenCV 5)

- The OpenCV 5 runtime gate, sub-pixel and learned-feature measurement, capture robustness, agent-loop feedback and repository packaging.
- The V6 panel demonstration run (`v6p01`): 55 of 56 held-out comparisons passed; one drag-release regression, so the run is recorded as failed.
- One post-freeze fix to `inspect_window` (technical report §6.4).

## Other documents in this repository

- [`ARCHITECTURE.md`](../../ARCHITECTURE.md): design overview.
- [`docs/CAPTURE_CLOCK_RUNTIME.md`](../CAPTURE_CLOCK_RUNTIME.md): the Windows capture clock.
- [`submission/TECHNICAL_REPORT.md`](../../submission/TECHNICAL_REPORT.md): the technical report.
- [`evidence/README.md`](../../evidence/README.md): the public subset of the frozen evidence.
