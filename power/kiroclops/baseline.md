# Raw-reference baseline

Match the target game's normal jump to the supplied raw reference recordings.
Read `.kiroclops-reference` and the target's controller and state scripts. Write
your own implementation using ordinary file tools and visual inspection.

Work only in this target project. Do not read parent directories, other projects,
prior experiments, reference-game source, saved solutions or global memories.
Edit only GDScript under `source/characters/player/`, including new helpers there.
Keep all scene, project, fixture, reference, evidence and Kiro configuration files
unchanged. Preserve other movement and state-machine behavior.

You have at most three candidate captures and 30 minutes. Use only the supplied
3-tick and 24-tick calibration inputs. First run the exact prepare command in the
session prompt; it supplies raw reference and baseline recordings only.
These three commands can each take several minutes. Run each exact command in
the foreground using the shell tool with `timeout: 900000` (milliseconds).
Set `run_in_background: false` when that option is available; otherwise omit it.
Do not use background process tools or start another command while one is running.
Wait for the command's final JSON result and exit code before continuing; partial
output or a timeout is not completion. The per-command timeout does not extend
your shared 30-minute session budget: all preparation, captures and finalization
must finish within the time remaining.
Each exact capture command shown in the
session prompt snapshots your edits and exports six raw videos, contact sheets
and input manifests. Inspect that evidence visually and adjust if useful. This
baseline receives no fitted motion profile, numerical tracking feedback or
KiroClops analysis tools.

After your last candidate, run the exact finish command from the session prompt.
This locks the eligible candidate chosen by the experiment service, closes tuning,
and runs held-out validation. Do not request held-out observations or edit after
finish. Report its returned outcome honestly. A recording by itself does not
establish a pass. No other shell commands, external tools or subagents are needed.
