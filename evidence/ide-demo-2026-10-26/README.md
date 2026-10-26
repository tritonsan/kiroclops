# Kiro IDE demonstration runs (post-freeze, 26 October 2026)

These runs were made for the video after the results freeze. They show the Kiro IDE power (`integrations/kiro-power/`) in use and change no result in the technical report. Each run used Kiro IDE 1.1.70 with Claude Opus 5.5 and a fresh KiroClops session; numbers come from each session's own job results. The session folders hold gigabytes of recordings and local machine paths and are not published; they can be shown on request.

"Passed" below means numerical acceptance only: no independent visual or source review was done for these runs.

## Quick IDE profile

The workspace server's default profile: three candidates, 30 minutes of authoring and 30 minutes for the held-out cases. For the panel and the diagram it uses four calibration cases, with the reference recorded when the session is prepared; for the game, the recorded ground stage with its pre-recorded Tiny reference.

| Domain | Session | Candidates (calibration error) | Held-out | Outcome |
|---|---|---|---|---|
| Web panel | `panel-20261026-013003` | 01: 0.0066, 02: 0.0043 (sealed) | 3 of 12 pass: 3 of 4 drag-release; all 4 interrupt and all 4 reverse cases fail on title and content position. All 12 cases beat the unchanged app on every object. | not accepted |
| SVG diagram | `svg-20261026-022123` | 01: 0.0053 (sealed), 02: 0.0062 | 6 of 12 pass. The other 6 fall under the 95% tracking-coverage rule, 4 of them in the unchanged app's own recording; in 2 drag cases the dragged card also misses its position limit (0.0396 and 0.1015 against 0.03). | not accepted |
| Godot game (ground) | `ground-20261026-034617` | 01: 0.0397 body lengths (sealed) | 18 of 18 pass; jump preservation 12 of 12; visual wall 8 of 8 | passed, review pending |

A first panel run (`panel-20261026-004500`) ended in a capture failure: one held-out recording loaded without its stylesheet, so KiroClops marked it invalid and judged nothing. That run was repeated as `panel-20261026-013003`.

The game run uses the workspace server's ground stage against the original Heart project, which needs Godot 4.5.1, the Heart project and the Tiny reference recordings, none of which a fresh clone carries.

## Recorded systems, re-run in the IDE

Each era's frozen suite and a byte copy of its source epoch, served to the IDE through a thin adapter, with a 60-minute authoring budget.

| Domain | Suite and code | Candidates (calibration error) | Held-out | Outcome |
|---|---|---|---|---|
| SVG diagram | `v5s01c`, `v5b` | 01: 0.0017 (sealed) | regression 12/12, validation 12/12 | passed, review pending |
| Godot game | `v4g01`, `v4-final` | 01 cancelled by Kiro, 02: 0.0446 (sealed) | regression 48/48, validation 30/30 | passed, review pending |
| Web panel | `v6p01`, `v6c` | 01: 0.0438, 02: 0.0625, neither eligible | not measured: no candidate passed the 16 calibration clips before the budget ran out | no verdict |

Evaluations of the V6 panel ran 45–57% slower than in the recorded run of 16 October, which left no time for a third candidate. In the V4 game run, Kiro read four calibration analysis files from the session's measurement folder; none of them belongs to a held-out case.

## Differences from the recorded runs

The recorded results in the report come from Kiro CLI sessions with the guard hook and native audit, which do not run in the IDE. For the panel and the diagram, the quick IDE profile has fewer calibration cases than the frozen suites (4 against 16 for the panel) and records its reference on the spot, so those verdicts are not comparable with the recorded ones.
