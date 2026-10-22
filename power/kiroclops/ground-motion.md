---
name: motion-calibration
description: Transfer grounded running, braking and reversal from measured pixels while preserving the starting game's jump behavior.
---

# KiroClops grounded motion transfer

You write the target player's actual GDScript. KiroClops measures pixels and
evaluates your candidate; it never writes or supplies a controller solution.

## Boundary

- Work only in the opened target project. The starting code deliberately includes
  an earlier successful jump adaptation. Preserve that existing behavior.
- Read only this target and its `.kiroclops-reference` and exported evidence.
  Do not inspect parent directories, other sessions, reference-game source,
  global memories or any previously produced ground-motion solution.
- Edit only `.gd` files under `source/characters/player/`, including new helpers.
  Keep scenes, assets, collision geometry, physics rate, camera, capture adapters,
  annotations, reference data and Kiro configuration unchanged.
- Change grounded horizontal movement only. Retain airborne control and the
  existing short/long normal jump, double jump and wall launch paths.
- Use at most three candidates and 30 minutes. Compilation and runtime failures
  consume attempts. Calibration consists of right inputs held for 6 and 48
  physics steps; other durations/directions belong to sealed validation.
- Do not tune on held-out recordings. Missing evidence and invalid tracking
  are not motion failures or successful validation.

## Workflow

1. Before reading or editing game code, call `inspect_session`. Check that its
   target and session ID match this opened workspace and that the experiment is
   `ground-motion-v1`. An IDE Power can remain bound to an older session; stop
   on a mismatch without editing or starting a job.
2. Call `prepare_experiment`, then poll its job using `get_job`. Read the measured
   profile, units, confidence and original target's calibration results. The
   reference physics constants are intentionally unavailable; use the pixels.
3. Read the player's state machine. Map body-heights/second and acceleration to
   its native units and discrete update order. Explain your mapping and author
   the changes yourself. Do not implement a recorded position playback or branch
   on experiment names, ticks, fixtures, environment variables or known scenarios.
4. Call `evaluate_candidate` and poll it. Inspect horizontal feedback and jump/
   gameplay regression results before deciding whether another candidate helps.
   Match the input-aligned position curve including acceleration and braking;
   a good long-run average alone is insufficient. Preserve existing air behavior.
5. Call `finalize_candidate` when an eligible calibration result exists. It
   selects the lowest-error eligible immutable snapshot and reveals held-out
   validation once. Do not edit or return to calibration afterward.
6. Poll completion, call `build_report`, and state the recorded outcome and report
   path. Separate numerical success, independent visual review, preserved jump
   checks and normal-level footage. Do not claim unperformed checks passed.

The seven tools are `inspect_session`, `prepare_experiment`, `evaluate_candidate`,
`finalize_candidate`, `get_job`, `cancel_job`, and `build_report`. No terminal,
external website, other Power, or delegated model is required.
