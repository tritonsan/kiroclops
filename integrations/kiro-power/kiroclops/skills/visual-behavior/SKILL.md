---
name: visual-behavior
description: Make an application's motion match a reference. KiroClops sets up a session for the opened project, records the reference and every candidate, measures them frame by frame with OpenCV 5, and reveals held-out cases only after a candidate is sealed.
---

# KiroClops: match a reference motion

You write the application's motion code yourself. KiroClops records the reference and your candidates, measures them frame by frame with OpenCV 5 and compares them with limits fixed before the run. It never writes or supplies the implementation.

## Start

Call `start_session` with the absolute path of the opened workspace folder and the user's request. KiroClops checks that the project is one it can record (today: copies of the panel and SVG applications and of the Godot game project in the KiroClops repository), then creates its session from a snapshot of the project, or resumes the session it already has for this folder.

## Boundary

- Edit only the motion files that `inspect_session` lists as editable (for example `src/panel-motion.js`, `src/layout-motion.js`, `src/motion/*.js`, or `player.gd` and `motion/*.gd` in the game). `evaluate_candidate` copies exactly those files into KiroClops's measurement copy.
- If you want a helper script for analysis, create it only in the `kiroclops-scratch/` folder of this workspace. Never write a file outside this workspace (no temp folders, no session folder) and never into a folder whose name starts with a dot: in the Kiro IDE such a write stops the turn. KiroClops never measures `kiroclops-scratch/`. One-off inline commands that write nothing are fine anywhere.
- The tool results carry the per-window errors, limits and lag you need. Exported measurement files sit in KiroClops's session folder; read them there, do not copy them into the workspace.
- Never open or search for the reference application's source, other sessions, earlier solutions or held-out evidence.
- You have at most three submitted candidates and the authoring time that `inspect_session` reports (30 or 60 minutes); the clock starts with `prepare_experiment` and includes evaluation time. Do not replay recorded coordinates or branch on case names or times.

## Workflow

1. After `start_session`, call `inspect_session`.
2. Call `prepare_experiment` and poll `get_job` until it finishes. Read the measured reference profile, the scored windows and the errors of the unchanged app.
3. Edit the motion code. Submit candidate 01 early with `evaluate_candidate` and poll `get_job`.
4. Read the per-window errors, limits and lag. For a failing window, `inspect_window(job_id, case_id, object_id)` writes reference, candidate and difference strips into KiroClops's session copy and returns their paths; look at them before the next edit. KiroClops itself writes nothing into this workspace. `evaluate_candidate(case_ids=[...])` re-measures a few cases as a faster probe that does not count as a candidate.
5. When an eligible candidate exists, call `finalize_candidate`. It seals the lowest-error eligible candidate and only then measures the held-out cases. Do not edit afterwards.
6. Poll the final job, call `build_report`, and state the recorded outcome in a few sentences: calibration error, held-out cases passed, verdict. Report a failure as a failure.
