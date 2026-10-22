---
name: motion-calibration
description: Calibrate a target Godot controller using reference video measurements and honest recorded validation.
---

# KiroClops motion calibration

You edit the actual target game's player controller. KiroClops measures rendered
pixels and supplies experiment evidence; it does not write the controller for you.

## Experiment boundary

- Work only in the opened target project. Read its code and the raw reference
  package `.kiroclops-reference`. Do not inspect parent folders, prior experiments,
  other projects, reference-game source, saved solutions, or global memories.
- Edit only GDScript files under `source/characters/player/`, including new
  helpers in that directory. Preserve movement and state-machine behavior outside
  the intended normal-jump change. Keep project, scene, input, fixture, reference,
  measurement and Kiro configuration files unchanged.
- Use at most three candidate evaluations within 30 minutes. Calibration uses
  only the supplied 3-tick and 24-tick holds. Do not obtain or tune against held-out
  validation evidence before finalization. The service enforces this boundary.
- A successful capture or a completed job is not a passing experiment. Report
  missing annotations, tracking problems, rejected edits and failed gates plainly.

## Workflow

1. Before reading or editing game code or starting any job, call `inspect_session`.
   Compare its `target_project` and `session_id` with the exact opened/prepared
   target path in the task. Both must identify this experiment. An IDE-installed
   Power may still be bound to an older session; if either value differs, stop
   and report the binding mismatch. Do not prepare, edit, evaluate or finalize
   either session until the correct session-bound Power is installed.
2. Call `prepare_experiment`, then poll the returned job with `get_job` until it
   finishes. Inspect the reference evidence and measurement diagnostics supplied
   by this session. Do not infer precise motion parameters from a filename.
3. Read the target controller and relevant state scripts. Explain the intended
   mapping between measured motion and the target game's own units and update
   order. Write the controller changes yourself using the normal file tools.
4. Call `evaluate_candidate`. It snapshots the edited controller, records the
   calibration scenarios and returns measured feedback. Poll `get_job`; inspect
   returned errors and evidence before deciding whether a further edit helps.
   Every submitted candidate consumes one of the three evaluation slots.
5. Once the calibration evidence is sufficient, call `finalize_candidate`.
   Finalization closes tuning and evaluates the selected eligible snapshot on
   held-out scenarios. Do not edit or resubmit after this boundary.
6. Poll completion and call `build_report`. Summarize the recorded decision and
   give the report path. Distinguish measured transfer success from normal-level
   gameplay footage, which is visual context rather than a measured trial.

`cancel_job` cancels a running job. Use job states rather than repeated blind
submissions. The tools are `inspect_session`, `prepare_experiment`,
`evaluate_candidate`, `finalize_candidate`, `get_job`, `cancel_job`, `build_report`.
There is no need for a general terminal, external website, or another agent.
