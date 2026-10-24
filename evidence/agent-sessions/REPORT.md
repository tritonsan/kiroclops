# Agent sessions: post-hoc evidence table

Read-only, post-hoc extraction from the 13 logged Kiro CLI sessions: every folder under `C:/kc3d/s` that has a launcher record (`kiro/launch.json`). The other 14 folders there hold recordings, preparation or measurements without a model run and have no launcher record, so no model prompt was launched from them. No measurement was repeated and no model was run. Every number below is read from the recorded session files or recomputed from the recorded rows; a value that is not recorded is shown as n/a and `result.json` says why (`null_reasons`). Source files and their SHA-256 are listed per session in `result.json`. Times are UTC.

## Reading guide

- "Error" is the recorded calibration score of a candidate (lower is better): the mean over cases of the case error, where a case error is the mean over its windows (panel) or the single recorded case error (game). The unchanged app is the `before` side of the same rows on the same formula. The recomputed scores reproduce the recorded ones to 1e-12 for all 17 scored candidates (17 of 17 match) and for 7 of 7 held-out job scores.
- Panel errors are in object-scale units (window position RMSE divided by the recorded object scale), game errors in body heights; they are not comparable across domains.
- Calibration cases are the feedback the agent sees while it works. Held-out cases (known regression and unseen validation) are measured only after the candidate is sealed.

## 1. Headline numbers

- Sessions logged: 13 (V1 3, V2 3, V3 2, V4 2, V5 1, V5b 1, V6 1); first tool calls from 2026-10-01T22:35:37Z to 2026-10-16T17:10:48Z. Model / effort: claude-opus-5.5 / max. Kiro CLI versions: 2.27.0 x4, 2.27.1 x8, 2.31.0 x1.
- Domains: game (jump) x3, web_panel (panel) x7, game (ground) x3. No logged Kiro session covers the SVG domain.
- Audited tool calls: 1135, of which 387 are KiroClops MCP calls and 566 are the agent's own scripts or file writes. Guard refusals: 5 (in v3p01, v4p01).
- In 13 of 13 sessions the client sent exactly one task prompt and no other message after the protocol set-up (no follow-up, resume or cancel message). Among the 10 sessions whose launcher recorded passive activity, 10 had no upstream pause, no automatic resume, no prompt retry and no deadline extension.
- 11 of 13 sessions evaluated at least one candidate: 17 full evaluations (13 of the 17 scored candidates were eligible) and 1 partial probe.
- Candidate error fell from the first to the last candidate in 5 of 6 sessions with two or more candidates (v2j03, v2p08, v3p01, v5p01, v6p01); it rose in 1 (v2g01).
- In all 6 of those sessions the candidate source differs between the first and the last candidate.
- The last candidate beat the unchanged app on calibration error in 11 of 11 sessions with a candidate; so did every one of the 17 evaluated candidates (17 of 17).
- Median factor by which the last candidate's calibration error is below the unchanged app's: web panel 6.3x (n=7), game 14.2x (n=4). The game factor is large because the unchanged app lacks the recorded behaviour; the two domains must not be pooled.
- Between the first candidate result and the next full evaluation the agent made at least one script or file-write call in 6 of 6 sessions (range 9 to 24 calls).
- 7 sessions reached sealed held-out scoring: 297 of 326 held-out cases passed (regression 190/206; validation 107/120). The candidate's mean held-out error was below the unchanged app's in 7 of the 7 sessions and on 308 of 326 held-out cases.
- All held-out cases passed in 1 of those 7 sessions (v4g01).
- Recorded verdicts: accepted 1, failed 8, infrastructure_failure 2, not_finalized 2. Sessions: accepted: v4g01; failed: p04, v2g01, v2p08, v3g02, v3p01, v4p01, v5p01, v6p01; infrastructure_failure: v2j03, v5p02c; not_finalized: gj04, gj05.
- Native tool-audit certification as recorded in the logs: fail 3, not_certified 3, pass 3, reaudit_pass 4. 12 of 13 sessions ran on kiro-cli 2.27.x, the only range the pinned audit reader covers.
- Every measurement record that carries an `opencv_version` field reports OpenCV 5.x (5.0.0; v6p01 also records package 5.0.0.93) in 12 of 13 sessions; gj04 has no measurement record.

## 2. Sessions and agent tooling

| Session | Ver. | Domain | First tool call (UTC) | Kiro CLI | Audited calls (MCP) | Refused | Full evaluations + probes | Scripts / file writes |
|---|---|---|---|---|---|---|---|---|
| gj04 | V1 | game (jump) | 2026-10-01 22:35 | 2.27.0 | 8 (2) | 0 | 0 + 0 | 0 |
| gj05 | V1 | game (jump) | 2026-10-02 00:26 | 2.27.0 | 31 (4) | 0 | 0 + 0 | 14 |
| p04 | V1 | web_panel (panel) | 2026-10-02 01:00 | 2.27.0 | 60 (15) | 0 | 1 + 0 | 29 |
| v2g01 | V2 | game (ground) | 2026-10-03 04:25 | 2.27.1 | 113 (33) | 0 | 2 + 0 | 59 |
| v2j03 | V2 | game (jump) | 2026-10-02 12:12 | 2.27.0 | 71 (19) | 0 | 2 + 0 | 36 |
| v2p08 | V2 | web_panel (panel) | 2026-10-02 23:13 | 2.27.1 | 92 (20) | 0 | 2 + 0 | 58 |
| v3g02 | V3 | game (ground) | 2026-10-09 15:01 | 2.27.1 | 77 (20) | 0 | 1 + 0 | 38 |
| v3p01 | V3 | web_panel (panel) | 2026-10-10 04:04 | 2.27.1 | 98 (27) | 1 | 2 + 0 | 54 |
| v4g01 | V4 | game (ground) | 2026-10-10 11:59 | 2.27.1 | 74 (22) | 0 | 1 + 0 | 33 |
| v4p01 | V4 | web_panel (panel) | 2026-10-10 13:42 | 2.27.1 | 93 (22) | 4 | 1 + 0 | 54 |
| v5p01 | V5 | web_panel (panel) | 2026-10-13 13:43 | 2.27.1 | 104 (43) | 0 | 2 + 0 | 54 |
| v5p02c | V5b | web_panel (panel) | 2026-10-13 19:45 | 2.27.1 | 84 (24) | 0 | 1 + 0 | 51 |
| v6p01 | V6 | web_panel (panel) | 2026-10-16 17:10 | 2.31.0 | 230 (136) | 0 | 2 + 1 | 86 |

Model / effort as recorded in each `kiro/launch.json`: claude-opus-5.5 / max; approval mode: native_session_allow_always. KiroClops MCP tools called at least once: cancel_job, evaluate_candidate, finalize_candidate, get_job, inspect_session, prepare_experiment; never called in any session: build_report, inspect_window. "Audited calls" are the rows of `kiro/tool-audit.jsonl` (one per tool call, including one `tool_load` row). "Scripts / file writes" counts execute_bash, execute_pwsh, fs_write and str_replace calls. Refusals are guard decisions with `allow` false: v3p01 +19 s read_file (access to the CLI's own tool-output file outside the experiment folder); v4p01 +10 s read_file (access to the CLI's own tool-output file outside the experiment folder); v4p01 +65 s execute_pwsh (access to the CLI's own tool-output file outside the experiment folder); v4p01 +641 s execute_pwsh (a path outside the experiment boundary); v4p01 +2472 s delete_file (file deletion).

## 3. Calibration evolution (what the agent saw)

| Session | Candidates in order: error (eligible; cases passed) | Unchanged app | First to last | Last vs unchanged app |
|---|---|---|---|---|
| gj04 | none evaluated | n/a | n/a | n/a |
| gj05 | none evaluated | n/a | n/a | n/a |
| p04 | 0.0078 (eligible; 8/8) | 0.2282 | single candidate | 29.4x lower (better on 8/8 cases) |
| v2g01 | 0.0448 (not eligible; 6/12) -> 0.0561 (not eligible; 9/12) | 0.6303 | rose (+25.2%) | 11.2x lower (better on 12/12 cases) |
| v2j03 | 0.0763 (eligible; 6/6) -> 0.0498 (eligible; 6/6) | 0.7865 | fell (-34.7%) | 15.8x lower (better on 6/6 cases) |
| v2p08 | 0.0158 (eligible; 8/8) -> 0.0050 (eligible; 8/8) | 0.0389 | fell (-68.1%) | 7.7x lower (better on 8/8 cases) |
| v3g02 | 0.0438 (eligible; 12/12) | 0.6303 | single candidate | 14.4x lower (better on 12/12 cases) |
| v3p01 | 0.0211 (eligible; 12/12) -> 0.0200 (eligible; 12/12) | 0.0391 | fell (-5.3%) | 2.0x lower (better on 11/12 cases) |
| v4g01 | 0.0448 (eligible; 12/12) | 0.6303 | single candidate | 14.1x lower (better on 12/12 cases) |
| v4p01 | 0.0223 (eligible; 16/16) | 0.0353 | single candidate | 1.6x lower (better on 10/16 cases) |
| v5p01 | 0.0233 (not eligible; 21/22) -> 0.0219 (not eligible; 21/22) | 0.0463 | fell (-6.1%) | 2.1x lower (better on 21/22 cases) |
| v5p02c | 0.0075 (eligible; 16/16) | 0.0467 | single candidate | 6.3x lower (better on 16/16 cases) |
| v6p01 | 0.0244 (eligible; 16/16) -> 0.0067 (eligible; 16/16) | 0.0502 | fell (-72.6%) | 7.5x lower (better on 16/16 cases) |

## 4. Descriptive: what the agent did after it saw the first candidate result

Counts of audited calls strictly between the `finished_at` of the first full evaluation and the next full evaluation call. This is a descriptive signal that the agent kept working after the feedback arrived; it does not show that the feedback caused the change. The last column compares the two candidates window by window (panel) or case by case (game).

| Session | Window (s) | Scripts + file writes in it | Shell scripts / file writes | Candidate source changed | Windows or cases improved / worsened / equal, first to last candidate |
|---|---|---|---|---|---|
| gj04 | no second full evaluation | n/a (until the finalize call: n/a) | n/a | n/a | n/a |
| gj05 | no second full evaluation | n/a (until the finalize call: n/a) | n/a | n/a | n/a |
| p04 | no second full evaluation | n/a (until the finalize call: 1) | n/a | n/a | n/a |
| v2g01 | 565 | 24 | 13 / 11 | yes | 3 / 3 / 6 of 12 cases |
| v2j03 | 267 | 16 | 9 / 7 | yes | 6 / 0 / 0 of 6 cases |
| v2p08 | 243 | 9 | 6 / 3 | yes | 48 / 0 / 0 of 48 windows |
| v3g02 | no second full evaluation | n/a (until the finalize call: 4) | n/a | n/a | n/a |
| v3p01 | 261 | 16 | 9 / 7 | yes | 48 / 30 / 0 of 78 windows |
| v4g01 | no second full evaluation | n/a (until the finalize call: 4) | n/a | n/a | n/a |
| v4p01 | no second full evaluation | n/a (until the finalize call: 15) | n/a | n/a | n/a |
| v5p01 | 314 | 13 | 11 / 2 | yes | 83 / 55 / 0 of 138 windows |
| v5p02c | no second full evaluation | n/a (until the finalize call: 0) | n/a | n/a | n/a |
| v6p01 | 684 | 20 | 11 / 9 | yes | 98 / 4 / 0 of 102 windows |

## 5. Sealed held-out outcome and recorded verdict

| Session | Held-out passed / total | Held-out error: candidate vs unchanged app | Failure signatures | Recorded verdict | Native audit per logs |
|---|---|---|---|---|---|
| gj04 | n/a (no finalize attempt) | n/a | n/a | not_finalized | not_certified |
| gj05 | n/a (no finalize attempt) | n/a | n/a | not_finalized | not_certified |
| p04 | 19/24 (regression 10/12; validation 9/12) | 0.0107 vs 0.4555 | per_window_pixel_failure x5 | failed | reaudit_pass |
| v2g01 | n/a (finalize failed: no_eligible_candidate) | n/a | n/a | failed | reaudit_pass |
| v2j03 | n/a (finalize failed: execution_error) | n/a | n/a | infrastructure_failure | reaudit_pass |
| v2p08 | 23/24 (regression 11/12; validation 12/12) | 0.0117 vs 0.0481 | per_window_pixel_failure x1 | failed | reaudit_pass |
| v3g02 | 54/60 (regression 30/30; validation 24/30) | 0.0459 vs 0.5787 | failed_gates:speed x6 | failed | pass |
| v3p01 | 29/36 (regression 21/24; validation 8/12) | 0.0216 vs 0.0536 | per_window_pixel_failure x3, required_order_unobservable x4 | failed | fail |
| v4g01 | 78/78 (regression 48/48; validation 30/30) | 0.0499 vs 0.5925 | none | accepted | pass |
| v4p01 | 39/48 (regression 27/36; validation 12/12) | 0.0218 vs 0.0517 | per_window_pixel_failure x5, required_order_unobservable x4 | failed | fail |
| v5p01 | n/a (finalize failed: no_eligible_candidate) | n/a | n/a | failed | fail |
| v5p02c | n/a (finalize failed: capture_timing_failure) | n/a | n/a | infrastructure_failure | pass |
| v6p01 | 55/56 (regression 43/44; validation 12/12) | 0.0121 vs 0.0500 | required_order_unobservable x1 | failed | not_certified |

Size of the sealed candidate relative to the baseline (`report/candidate.diff`; written only for sessions that sealed a candidate and built a report):

| Session | Files changed | Lines added | Lines removed | Files |
|---|---|---|---|---|
| gj04 | n/a | n/a | n/a | n/a |
| gj05 | n/a | n/a | n/a | n/a |
| p04 | 2 | 33 | 22 | src/motion/spring.js, src/panel-motion.js |
| v2g01 | n/a | n/a | n/a | n/a |
| v2j03 | n/a | n/a | n/a | n/a |
| v2p08 | 2 | 94 | 10 | src/motion/input-clock.js, src/panel-motion.js |
| v3g02 | 1 | 17 | 2 | player.gd |
| v3p01 | 1 | 10 | 8 | src/panel-motion.js |
| v4g01 | 1 | 21 | 2 | player.gd |
| v4p01 | 1 | 8 | 2 | src/panel-motion.js |
| v5p01 | n/a | n/a | n/a | n/a |
| v5p02c | n/a | n/a | n/a | n/a |
| v6p01 | 3 | 164 | 15 | src/motion/input-clock.js, src/motion/spring.js, src/panel-motion.js |

## 6. Recorded verdicts and their sources

- **gj04** (V1): not_finalized. Operator interruption: a helper process stopped the live launcher during the first preparation job; no candidate was produced. The log records the behaviour as unevaluated and does not count it as an agent failure. Source: `PHASE3D_STATUS.md` L66-L68. Derived from the session files: not_finalized (agrees).
- **gj05** (V1): not_finalized. The 1800 s request deadline was reached after 31 audited calls with zero candidates, so behaviour was not evaluated; the log leaves the cause of the stalled generation undetermined (the CLI reported two upstream pauses). Source: `PHASE3D_STATUS.md` L11, `PHASE3D_STATUS.md` L76-L78. Derived from the session files: not_finalized (agrees).
- **p04** (V1): failed. candidate-01 passed 8/8 calibration cases and was sealed; held-out numerical acceptance is false (regression 10/12, validation 9/12). Source: `PHASE3D_STATUS.md` L12, `PHASE3D_STATUS.md` L80. Derived from the session files: numerical_fail (agrees).
- **v2g01** (V2): failed. Two candidates were evaluated and neither was eligible, so finalize was refused (no eligible candidate) and no held-out case was evaluated. A later log analysis puts the 50% improvement gate of the shortest calibration recipe close to the measurement floor. Source: `PHASE3D_V2_PENDING_RESULTS.md` L36-L53, `PHASE3D_V2_PENDING_RESULTS.md` L140, `PHASE3D_V2_PENDING_RESULTS.md` L172-L176. Derived from the session files: no_eligible_candidate (agrees).
- **v2j03** (V2): infrastructure_failure. Two eligible candidates, candidate-02 sealed. The canonical finalize job failed with a harness execution error (a cancellation callback of the wrong type) before any held-out comparison was recorded; the log classes it as neither a behaviour nor a model failure. A later separate re-verification of the same code passed; it is outside the session and does not change the recorded verdict. Source: `PHASE3D_V2_STATUS.md` L194-L200, `PHASE3D_V2_RESULTS.md` L30-L31, `PHASE3D_V2_REPAIR.md` L107-L132. Derived from the session files: finalize_infrastructure_error (agrees).
- **v2p08** (V2): failed. candidate-02 was sealed after 8/8 calibration cases; held-out regression 11/12 and validation 12/12. The single failing known-regression window is above the 2 px absolute limit, so numerical acceptance is false. Source: `PHASE3D_V2_PENDING_RESULTS.md` L138, `PHASE3D_V2_PENDING_RESULTS.md` L145-L152. Derived from the session files: numerical_fail (agrees).
- **v3g02** (V3): failed. candidate-01 was sealed; held-out regression 30/30 and validation 24/30. All six failures are repeats of one validation recipe at the speed gate; a labelled post-hoc diagnostic attributes this to the gate looking at a short frame window. Source: `reviews/phase3d-v3/v3g02-game-result.md` L3-L23. Derived from the session files: numerical_fail (agrees).
- **v3p01** (V3): failed. candidate-02 was sealed; held-out regression 21/24 and validation 8/12. All four validation failures use a recipe whose required event order is unobservable (a recipe design flaw according to the log). The native origin check failed as well. Source: `reviews/phase3d-v3/v3p01-panel-result.md` L3-L24. Derived from the session files: numerical_fail (agrees).
- **v4g01** (V4): accepted. candidate-01 was sealed; held-out regression 48/48 and validation 30/30. Independent raw review, source review and native origin passed, and a separate domain closure artifact records behaviour acceptance for the game domain (scope: this domain in its V3a source epoch; the closure states that the canonical session acceptance is unchanged and that no overall phase acceptance is given). Source: `reviews/phase3d-v4/v4-results.md` L3-L25. Derived from the session files: numerical_pass (agrees).
- **v4p01** (V4): failed. candidate-01 was sealed; held-out validation 12/12 but regression 27/36: four cases use an event order that is unobservable even in the reference (a recipe design flaw according to the log) and five fail on pixel windows. The native tool-result audit failed as well. Source: `reviews/phase3d-v4/v4-results.md` L44-L51. Derived from the session files: numerical_fail (agrees).
- **v5p01** (V5): failed. Two candidates were evaluated inside the 60 minute budget (about 15 minutes each) and both were ineligible at 21/22 calibration cases (one wide-viewport star window above the absolute limit); finalize was refused (no eligible candidate). The native audit failed on one offloaded shell output. Source: `reviews/phase3d-v5/v5-results.md` L56-L104. Derived from the session files: no_eligible_candidate (agrees).
- **v5p02c** (V5b): infrastructure_failure. candidate-01 was found with a single evaluation (16/16 calibration cases, eligible). The finalize job failed with a capture timing error (an input deadline missed by 0.239 s) while an operating-system update was installing, so no held-out case could be measured and the session cannot be re-finalized by design. Source: `reviews/phase3d-v5/v5-results.md` L164-L183. Derived from the session files: finalize_infrastructure_error (agrees).
- **v6p01** (V6): failed. Two eligible candidates; between them the agent started a partial probe on three cases and cancelled it after 151 s. candidate-02 was sealed. Held-out 55/56 (regression 43/44, validation 12/12); overall acceptance is fail: the single failed case is flagged required_order_unobservable and the native evidence axis is invalid because the pinned audit reader does not cover kiro-cli 2.31.0. Presented in the log as an additional demonstration, not as an acceptance claim. Source: `reviews/phase4-v6/v6-results.md` L246-L285. Derived from the session files: numerical_fail (agrees).

## 7. Cross-checks against the results logs

96 of 97 checks agree. Each row compares a number or identifier written in a results log (transcribed by the script author, in English) with the value read from the session files. Log files are identified by path and line range only.

| Id | Ver. | Session | Quantity | Log value | File value | Agree | Log source |
|---|---|---|---|---|---|---|---|
| C01 | V1 | gj04 | audited tool calls | 8 | 8 | yes | PHASE3D_STATUS.md L68 |
| C02 | V1 | gj05 | audited tool calls | 31 | 31 | yes | PHASE3D_STATUS.md L11; L78 |
| C03 | V1 | gj05 | candidates evaluated | 0 | 0 | yes | PHASE3D_STATUS.md L76 |
| C04 | V1 | gj05 | request deadline in seconds (budget) | 1800 | 1800 | yes | PHASE3D_STATUS.md L11 |
| C05 | V1 | gj05 | native event log lines | 1386 | 1386 | yes | PHASE3D_STATUS.md L78 |
| C06 | V1 | gj05 | prompt start time of day (UTC) | 00:26:13 | 00:26:13 | yes | PHASE3D_STATUS.md L76 |
| C07 | V1 | p04 | prompt start time of day (UTC) | 01:00:51 | 01:00:51 | yes | PHASE3D_STATUS.md L80 |
| C08 | V1 | p04 | audited tool calls | 60 | 60 | yes | PHASE3D_STATUS.md L84 |
| C09 | V1 | p04 | native event log lines | 3037 | 3037 | yes | PHASE3D_STATUS.md L84 |
| C10 | V1 | p04 | calibration cases passed by candidate-01 (passed, total) | [8, 8] | [8, 8] | yes | PHASE3D_STATUS.md L12; L80 |
| C11 | V1 | p04 | held-out regression (passed, total) | [10, 12] | [10, 12] | yes | PHASE3D_STATUS.md L12; L80 |
| C12 | V1 | p04 | held-out validation (passed, total) | [9, 12] | [9, 12] | yes | PHASE3D_STATUS.md L12; L80 |
| C13 | V2 | v2g01 | audited tool calls | 113 | 113 | yes | PHASE3D_V2_PENDING_RESULTS.md L27-L28 |
| C14 | V2 | v2p08 | audited tool calls | 92 | 92 | yes | PHASE3D_V2_PENDING_RESULTS.md L27-L28 |
| C15 | V2 | v2p08 | calibration cases passed (passed, total) | [8, 8] | [8, 8] | yes | PHASE3D_V2_PENDING_RESULTS.md L138 |
| C16 | V2 | v2p08 | held-out regression (passed, total) | [11, 12] | [11, 12] | yes | PHASE3D_V2_STATUS.md L14 |
| C17 | V2 | v2p08 | held-out validation (passed, total) | [12, 12] | [12, 12] | yes | PHASE3D_V2_STATUS.md L14 |
| C18 | V2 | v2j03 | candidate-01 calibration error | 0.07634287 | 0.0763428701 | yes | PHASE3D_V2_STATUS.md L179 |
| C19 | V2 | v2j03 | candidate-02 calibration error | 0.04984168 | 0.0498416830 | yes | PHASE3D_V2_STATUS.md L181 |
| C20 | V2 | v2j03 | authoring clock in seconds (sealed minus started) | 1919.727 | 1919.72700 | yes | PHASE3D_V2_STATUS.md L186 |
| C21 | V2 | v2j03 | finalize error message | 'function' object has no attribute 'is_set' | 'function' object has no attribute 'is_set' | yes | PHASE3D_V2_STATUS.md L194-L195 |
| C22 | V2 | v2j03 | prompt start, sealing and final-job end time of day (UTC) | ["12:12:23", "12:44:23", "12:56:32"] | ["12:12:23", "12:44:23", "12:56:32"] | yes | PHASE3D_V2_STATUS.md L167; L189; L194 |
| C23 | V2 | v2g01 | model response time in seconds (response minus prompt) | 2898.493 | 2898.49300 | yes | PHASE3D_V2_PENDING_RESULTS.md L159-L160 |
| C24 | V2 | v2g01 | finalize error code | no_eligible_candidate | no_eligible_candidate | yes | PHASE3D_V2_PENDING_RESULTS.md L172 |
| C25 | V2 | v2j03 | startup check seconds and MCP tools verified at preflight | [8.609, 7] | [8.609, 7] | yes | PHASE3D_V2_STATUS.md L168 |
| C26 | V2 | v2j03 | upstream pause notifications | 0 | 0 | yes | PHASE3D_V2_STATUS.md L197 |
| C27 | V2 | v2g01 | task prompts sent, automatic resume sent, deadline extended, pause notifications | [1, false, false, 0] | [1, false, false, 0] | yes | PHASE3D_V2_PENDING_RESULTS.md L157-L160 |
| C28 | V2 | v2g01 | candidate-02 calibration cases passed (passed, total): long run 3/3, protected jumps 6/6, short run 0/3 | [9, 12] | [9, 12] | yes | PHASE3D_V2_PENDING_RESULTS.md L140 |
| C29 | V2 | v2g01 | right-6 scenario: unchanged-app error | 0.0643216 | 0.064321573 | yes | PHASE3D_V2_PENDING_RESULTS.md L167 |
| C30 | V2 | v2g01 | right-6 scenario: candidate-01 error | 0.0417506 | 0.041750588 | yes | PHASE3D_V2_PENDING_RESULTS.md L167 |
| C31 | V2 | v2g01 | right-6 scenario: candidate-02 error | 0.0374311 | 0.037431132 | yes | PHASE3D_V2_PENDING_RESULTS.md L167 |
| C32 | V2 | v2g01 | right-48 scenario: candidate-02 error | 0.0872237 | 0.087223724 | yes | PHASE3D_V2_PENDING_RESULTS.md L168 |
| C33 | V3 | v3g02 | held-out regression (passed, total) | [30, 30] | [30, 30] | yes | reviews/phase3d-v3/v3g02-game-result.md L9 |
| C34 | V3 | v3g02 | held-out validation (passed, total) | [24, 30] | [24, 30] | yes | reviews/phase3d-v3/v3g02-game-result.md L9 |
| C35 | V3 | v3g02 | failed held-out cases that belong to recipe ground-validation-l33 (of all failed) | [6, 6] | [6, 6] | yes | reviews/phase3d-v3/v3g02-game-result.md L17 |
| C36 | V3 | v3g02 | sealed candidate source starts with | e0054455 | e0054455 | yes | reviews/phase3d-v3/v3g02-game-result.md L27 |
| C37 | V3 | v3g02 | task prompts sent | 1 | 1 | yes | reviews/phase3d-v3/v3g02-game-result.md L12 |
| C38 | V3 | v3p01 | held-out regression (passed, total) | [21, 24] | [21, 24] | yes | reviews/phase3d-v3/v3p01-panel-result.md L7 |
| C39 | V3 | v3p01 | held-out validation (passed, total) | [8, 12] | [8, 12] | yes | reviews/phase3d-v3/v3p01-panel-result.md L7 |
| C40 | V3 | v3p01 | final job id starts with | a7163f41 | a7163f41 | yes | reviews/phase3d-v3/v3p01-panel-result.md L7 |
| C41 | V3 | v3p01 | sealed candidate and its source prefix | ["candidate-02", "52ff07b0"] | ["candidate-02", "52ff07b0"] | yes | reviews/phase3d-v3/v3p01-panel-result.md L7 |
| C42 | V3 | v3p01 | validation failures flagged required_order_unobservable (of validation failures) | [4, 4] | [4, 4] | yes | reviews/phase3d-v3/v3p01-panel-result.md L24 |
| C43 | V4 | v4g01 | held-out regression (passed, total) | [48, 48] | [48, 48] | yes | reviews/phase3d-v4/v4-results.md L7 |
| C44 | V4 | v4g01 | held-out validation (passed, total) | [30, 30] | [30, 30] | yes | reviews/phase3d-v4/v4-results.md L8 |
| C45 | V4 | v4g01 | sealed candidate | candidate-01 | candidate-01 | yes | reviews/phase3d-v4/v4-results.md L10 |
| C46 | V4 | v4g01 | audited tool calls (74 calls and 74 hooks) | 74 | 74 | yes | reviews/phase3d-v4/v4-results.md L61 |
| C47 | V4 | v4g01 | task prompts sent | 1 | 1 | yes | reviews/phase3d-v4/v4-results.md L61 |
| C48 | V4 | v4g01 | closure.json SHA-256 | a3232cd47fea1d2e6a1b59e8375d6e9f20663a426073087569145115198726dc | a3232cd47fea1d2e6a1b59e8375d6e9f20663a426073087569145115198726dc | yes | reviews/phase3d-v4/v4-results.md L25 |
| C49 | V4 | v4g01 | closure receipt.json SHA-256 | 0800150d9f0771548f7cfb0915d372508b2f4f6c6699290579414f5845eafce5 | 0800150d9f0771548f7cfb0915d372508b2f4f6c6699290579414f5845eafce5 | yes | reviews/phase3d-v4/v4-results.md L25 |
| C50 | V4 | v4p01 | held-out validation (passed, total) | [12, 12] | [12, 12] | yes | reviews/phase3d-v4/v4-results.md L46 |
| C51 | V4 | v4p01 | held-out regression (passed, total) | [27, 36] | [27, 36] | yes | reviews/phase3d-v4/v4-results.md L47 |
| C52 | V4 | v4p01 | failed cases flagged required_order_unobservable | 4 | 4 | yes | reviews/phase3d-v4/v4-results.md L48 |
| C53 | V4 | v4p01 | failed cases that are pixel-window failures | 5 | 5 | yes | reviews/phase3d-v4/v4-results.md L49 |
| C54 | V5 | v5p01 | audited rows (hook completions) | 104 | 104 | yes | reviews/phase3d-v5/v5-results.md L94 |
| C55 | V5 | v5p01 | candidate-01 calibration cases passed (passed, total) | [21, 22] | [21, 22] | yes | reviews/phase3d-v5/v5-results.md L79 |
| C56 | V5 | v5p01 | candidate-02 calibration cases passed (passed, total) | [21, 22] | [21, 22] | yes | reviews/phase3d-v5/v5-results.md L80 |
| C57 | V5 | v5p01 | candidate-01 calibration error (rounded to 3 decimals) | 0.023 | 0.02328 | yes | reviews/phase3d-v5/v5-results.md L169 |
| C58 | V5 | v5p01 | candidate-02 calibration error (rounded to 3 decimals) | 0.022 | 0.02186 | yes | reviews/phase3d-v5/v5-results.md L169 |
| C59 | V5 | v5p01 | prompt duration in minutes | 55 | 54.99 | yes | reviews/phase3d-v5/v5-results.md L68 |
| C60 | V5 | v5p01 | prompt start and response time of day (logs use local time, UTC+3) | ["16:42:55", "17:37:55"] | ["16:42:55", "17:37:55"] | yes | reviews/phase3d-v5/v5-results.md L68 |
| C61 | V5 | v5p01 | evaluation duration in minutes (candidate-01) | 15 | 14.95 | yes | reviews/phase3d-v5/v5-results.md L84 |
| C62 | V5 | v5p01 | finalize error code | no_eligible_candidate | no_eligible_candidate | yes | reviews/phase3d-v5/v5-results.md L75 |
| C63 | V5 | v4p01 | evaluation duration in minutes of the V4 panel run with 16 calibration cases (quoted in the V5 log) | 11 | 13.13 | **NO** | reviews/phase3d-v5/v5-results.md L84 |
| C64 | V5 | v5p01 | failing calibration case of candidate-01 is the wide reverse 1280 px repeat 1 | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1 | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1 | yes | reviews/phase3d-v5/v5-results.md L79 |
| C65 | V5 | v5p01 | failing calibration case of candidate-02 is the wide reverse 1280 px repeat 2 | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2 | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2 | yes | reviews/phase3d-v5/v5-results.md L80 |
| C66 | V5b | v5p02c | candidate-01 calibration cases passed (passed, total) | [16, 16] | [16, 16] | yes | reviews/phase3d-v5/v5-results.md L169 |
| C67 | V5b | v5p02c | candidate-01 calibration error (rounded to 4 decimals) | 0.0075 | 0.007452 | yes | reviews/phase3d-v5/v5-results.md L169 |
| C68 | V5b | v5p02c | finalize error message | Input deadline missed by 0.239428s; late commands will not be compressed into a burst | Input deadline missed by 0.239428s; late commands will not be compressed into a burst | yes | reviews/phase3d-v5/v5-results.md L170 |
| C69 | V5b | v5p02c | prompt start and response time of day (logs use local time, UTC+3) | ["22:45:43", "23:32:56"] | ["22:45:43", "23:32:56"] | yes | reviews/phase3d-v5/v5-results.md L168 |
| C70 | V5b | v5p02c | finalize job start and end time of day (logs use local time, UTC+3) | ["23:31", "00:07"] | ["23:31", "00:07"] | yes | reviews/phase3d-v5/v5-results.md L170 |
| C71 | V6 | v6p01 | audit rows | 230 | 230 | yes | reviews/phase4-v6/v6-results.md L264 |
| C72 | V6 | v6p01 | MCP tools exposed at preflight | 8 | 8 | yes | reviews/phase4-v6/v6-results.md L22 |
| C73 | V6 | v6p01 | MCP calls: get_job | 126 | 126 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C74 | V6 | v6p01 | MCP calls: inspect_session | 4 | 4 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C75 | V6 | v6p01 | MCP calls: evaluate_candidate | 3 | 3 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C76 | V6 | v6p01 | MCP calls: prepare_experiment | 1 | 1 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C77 | V6 | v6p01 | MCP calls: cancel_job | 1 | 1 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C78 | V6 | v6p01 | MCP calls: finalize_candidate | 1 | 1 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C79 | V6 | v6p01 | tool calls: execute_pwsh | 61 | 61 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C80 | V6 | v6p01 | tool calls: fs_write | 18 | 18 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C81 | V6 | v6p01 | tool calls: str_replace | 7 | 7 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C82 | V6 | v6p01 | tool calls: read_file | 6 | 6 | yes | reviews/phase4-v6/v6-results.md L264-L265 |
| C83 | V6 | v6p01 | preparation job in seconds | 426 | 426.40 | yes | reviews/phase4-v6/v6-results.md L259 |
| C84 | V6 | v6p01 | candidate-01 calibration error (rounded to 4 decimals) | 0.0244 | 0.024435 | yes | reviews/phase4-v6/v6-results.md L260 |
| C85 | V6 | v6p01 | candidate-02 calibration error (rounded to 4 decimals) | 0.0067 | 0.006695 | yes | reviews/phase4-v6/v6-results.md L262 |
| C86 | V6 | v6p01 | candidate-02 versus candidate-01 improvement factor (rounded to 1 decimal) | 3.6 | 3.650 | yes | reviews/phase4-v6/v6-results.md L262 |
| C87 | V6 | v6p01 | partial probe cancelled after seconds | 151 | 151.00 | yes | reviews/phase4-v6/v6-results.md L261 |
| C88 | V6 | v6p01 | partial probe cases requested | 3 | 3 | yes | reviews/phase4-v6/v6-results.md L261 |
| C89 | V6 | v6p01 | held-out cases passed (passed, total) | [55, 56] | [55, 56] | yes | reviews/phase4-v6/v6-results.md L268 |
| C90 | V6 | v6p01 | held-out validation (passed, total) | [12, 12] | [12, 12] | yes | reviews/phase4-v6/v6-results.md L269 |
| C91 | V6 | v6p01 | held-out regression (passed, total) | [43, 44] | [43, 44] | yes | reviews/phase4-v6/v6-results.md L270 |
| C92 | V6 | v6p01 | the failed held-out case | realistic-v4-suite-panel-validation-drag-release-v4-w1440-r1 | realistic-v4-suite-panel-validation-drag-release-v4-w1440-r1 | yes | reviews/phase4-v6/v6-results.md L270 |
| C93 | V6 | v6p01 | failing post-release windows: candidate error range (2 decimals) and unchanged-app error range (3 decimals) | [0.78, 0.86, 0.005, 0.006] | [0.78, 0.86, 0.005, 0.006] | yes | reviews/phase4-v6/v6-results.md L270 |
| C94 | V6 | v6p01 | finalize job in minutes | 38 | 37.67 | yes | reviews/phase4-v6/v6-results.md L253 |
| C95 | V6 | v6p01 | report.json SHA-256 starts with | 0b1427f9 | 0b1427f9 | yes | reviews/phase4-v6/v6-results.md L273 |
| C96 | V6 | v6p01 | kiro-cli, KAS and model | ["2.31.0", "0.66.34", "claude-opus-5.5"] | ["2.31.0", "0.66.34", "claude-opus-5.5"] | yes | reviews/phase4-v6/v6-results.md L255 |
| C97 | V6 | v6p01 | inspect_window calls across all 13 audited sessions | 0 | 0 | yes | reviews/phase4-v6/v6-results.md L266; L310 |

Consistency with the single-session analysis (`scripts/v6_agent_baseline_posthoc.py`): 9 of 9 v6p01 quantities are identical to the output of that script (unchanged app 0.0502, candidate-01 0.0244, candidate-02 0.0067, held-out 0.0121 vs 0.0500).

## 8. Places where a log and the files use different definitions

- **D1** (v5p01; reviews/phase3d-v5/v5-results.md L94): the log says 103 application calls and 104 hook completions; the files say the audit file has 104 rows (one row per hook decision, including one tool_load row). The audit file counts hook decisions; the separate application-call count of 103 cannot be derived from it. The 104 matches.
- **D2** (v6p01; reviews/phase4-v6/v6-results.md L252): the log says Kiro turn 20:04-21:31 (87 minutes) and a 38 minute finalize job; the files say agent turn (prompt 20:10:42 to end_turn 21:00:37 local time, UTC+3) 49.9 minutes; launcher 20:10:28 to 21:38:07 local time = 87.6 minutes wall time, of which the held-out finalize job took 37.7 minutes. The 87 minutes match the launcher wall time (which includes the post-turn held-out finalize), not the agent turn; the clock times printed in the log differ from the launcher's recorded start and end by about 6 to 7 minutes and the files do not show where they come from. The 38 minutes match the finalize job.
- **D3** (v4g01; reviews/phase3d-v4/v4-results.md L15-L25): the log says the game domain closure passed (behaviour acceptance given); the files say the session's own report.json records overall_acceptance 'fail' and the closure states canonical_session_acceptance_changed false. The accepted verdict rests on a separate domain closure artifact. The session report is written at the end of the run, when the independent review status is still 'pending'.
- **D4** (p04; PHASE3D_STATUS.md L84): the log says 3,037 events; the files say kiro/run/stdout.jsonl has 3037 lines; launch.json events_seen is 3034. The log counts stdout lines (events_seen plus the post-response notifications); both numbers are recorded and consistent.
- **D5** (all; all results logs): the log says dates and clock times in local time (UTC+3 according to the logs that name the zone); the files say this table uses UTC from the audit timestamps (for example gj04 first call 2026-10-01T22:35:37Z). Sessions that started between 21:00 and 24:00 UTC fall on the next calendar day in the logs.
- **D6** (v2g01; v5p01; PHASE3D_V2_PENDING_RESULTS.md L172-L173; reviews/phase3d-v5/v5-results.md L75): the log says finalize_candidate was refused with no_eligible_candidate; the files say session.json keeps status 'prepared' (v2g01) and 'prepared' (v5p01) with final null although a failed finalize job exists in each. The refusal is recorded in the job record only; the session state machine was never advanced.
- **D7** (gj04; PHASE3D_STATUS.md L66): the log says operator_interrupted, behaviour unevaluated; the files say launch.json status 'running' and session.json status 'new'; operator-interruption.json carries the final status. The launcher was stopped before it could close its own record.
- **D8** (v6p01; reviews/phase4-v6/v6-results.md L264-L265): the log says a per-tool list for 230 audit rows; the files say the listed tools sum to 228; the audit file also has list_directory 1 and tool_load 1. The log omits two rows from its breakdown; its total of 230 matches.

96 of 97 cross-checks agree. The following do not:

- **C63** (V5, v4p01; reviews/phase3d-v5/v5-results.md L84): evaluation duration in minutes of the V4 panel run with 16 calibration cases (quoted in the V5 log). The log says 11; the files say 13.13. The only V4 panel agent evaluation (v4p01 candidate-01, 16 cases) took 13.1 minutes (787.7 s started to finished) in its job record; the V5 log may refer to another measurement, which the session files do not show.

Every other number in the logs that was checked agrees with the files.

## 9. Caveats

- These are 13 single runs, not a controlled experiment: no repeated runs per recipe, no control arm without feedback, no randomisation, one model setting. The unchanged app is a no-agent reference on the same rows, not a competing agent. Nothing here shows that the feedback caused the error reduction.
- Feedback formats and recipes changed between versions. Acceptance profiles differ (ground-floor-aware-v1, ground-floor-speed-aware-v1, legacy-game-v1, realistic-behavior-v1, realistic-behavior-v3), calibration suites differ in size (game: gj04 6, gj05 6, v2g01 12, v2j03 6, v3g02 12, v4g01 12; web_panel: p04 8, v2p08 8, v3p01 12, v4p01 16, v5p01 22, v5p02c 16, v6p01 16), thresholds and gates were revised after failures, and V6 added window diagnostics (offset, lag, trajectories) and the `inspect_window` tool, which the agent never used. A failure under an older rule is not a failure under a later one.
- Several recorded failures (held-out or calibration) are recipe or gate faults according to the logs, not agent behaviour: required-event-order cases that are unobservable even in the reference (v3p01: 4 of 7 failures; v4p01: 4 of 9), a speed gate that looked at a short frame window (v3g02: all 6 failures) and a 50% improvement gate near the measurement floor (v2g01, which ended with no eligible candidate). The single v6p01 failure carries the same `required_order_unobservable` label, but its failing post-release windows show candidate errors of 0.78-0.86 against 0.005-0.006 for the unchanged app, so it is counted as a failure of the candidate.
- Some runs did not end on the agent's own work: v2j03 (harness callback error at finalize), v5p02c (capture timing at finalize while an operating-system update was installing) and gj04 (operator interruption). gj05 reached its request deadline with zero candidates and the log leaves the cause undetermined. These rows keep their recorded verdicts; they are neither successes nor, except for gj05's missing candidate, evidence about the agent.
- Native tool-audit certification covers kiro-cli 2.27.x only. v6p01 ran on 2.31.0 and is native-uncertified. Among the 2.27.x sessions the logs record clean passes for v3g02, v4g01, v5p02c, re-audit passes after reader updates (the earlier failure is retained) for p04, v2g01, v2j03, v2p08, failures for v3p01, v4p01, v5p01, and no certification for the interrupted gj04 and gj05.
- Only 1 of 13 sessions (v4g01) carries an accepted verdict, and that rests on a separate domain closure with independent reviews, not on the session report. The accepted count is not a success rate of the agent.
- Held-out "cases" are not independent: game repeats are deterministic copies of a scenario and panel repeats are separate recordings of the same recipe, so case counts overstate the number of independent trials.
- Game and panel errors use different units and different row formats (per-case for games, per-window for panels); averaging or ranking across domains is not meaningful.
- Scope: the table covers the 13 sessions under the sessions root that have a tool-audit log; within them no session was left out (the 14 other folders have no launcher record). Kiro runs of earlier phases are stored elsewhere under `artifacts/` and are not part of it; for example PHASE3A_STATUS.md (L19-L20) records the Phase 3A original cohort of 18 runs as 3 completed and 15 failed under the rules of that phase. This scope is a storage and format boundary, not a performance filter.
- The audit file records hook decisions; the launcher event counts and the application-call counts quoted in some logs are different quantities (section 8). 4 audit files contain rows up to 0.12 s out of order (parallel tool calls), which does not affect the windows used here.
- Dates are UTC from the audit timestamps; the logs use local time (UTC+3).
- "Scripts and file writes" counts the tools execute_bash, execute_pwsh, fs_write and str_replace as attempted calls (2 of the 566 were refused by the guard); no other equivalent tool names occur in these audit files (the refused delete_file call is not counted). The windows start at the `finished_at` of the first full evaluation, so they show activity, not intent.
- OpenCV version: every measurement record carries its own `opencv_version`; the table reports what the records say and does not re-run any measurement.

## 10. Files read besides the session folders

Results logs (read for verdicts and cross-checks; identified here by path and SHA-256 only) and the single-session analysis used for the consistency check:

- `PHASE3A_STATUS.md` 6d7b66b6955858dc561a4e9e2be59e9f1123f2293a3e498ba2a24e48ffb707d9
- `PHASE3D_STATUS.md` f9258fd312ea80a7b6ba1f2fe3015e15e3aa7e689dbb23457e755182f1ab79f3
- `PHASE3D_V2_PENDING_RESULTS.md` 414dd0d51c0d59f58b2cba4db19d2180f32cbda5630f58dc9da8aff233479178
- `PHASE3D_V2_REPAIR.md` 0c98e1d09e92a890eb90bc45faed6d1b86824014ff0331665b80b0fbf264fc96
- `PHASE3D_V2_RESULTS.md` 6b1510f82fe2a56a404735b791ce60844f9658861375dc087b78391805984e47
- `PHASE3D_V2_STATUS.md` 0084c6db8da9ad4764b23e8dcf765dff4ac86c5c0b0c9e4a54bbd82fff2e3737
- `artifacts/phase4-v6/agent-baseline-posthoc-01/result.json` dfaf3cde0025220ffd58a9810b853142295a20d9544a1494c237f2628703acbf
- `reviews/phase3d-v3/v3g02-game-result.md` 2f0864ce722554aadf2610fb8543df4caa5b0a4e201669d1289cb19ddbd8c61a
- `reviews/phase3d-v3/v3p01-panel-result.md` f02223be365c762ef97da3cc83a5ae54340b8e8ca6efd74640a6fcd32cf4c7dd
- `reviews/phase3d-v4/v4-results.md` cb9b3125ae9d14e753f542145a8778df7b945d74bf90f9b8a653b75193bc199f
- `reviews/phase3d-v5/v5-results.md` 090427436c1b4e7619e26ddc9ffd70b81d58844ba2f5813ea3bc92f4b99a8837
- `reviews/phase4-v6/v6-results.md` 5f05391e03ee9647aeed7b61a8d582d2ad7ebbf8fbb078ddaee0c05af7b4a887
- `C:/kc3d/r/v4g01-closure-02/closure.json` a3232cd47fea1d2e6a1b59e8375d6e9f20663a426073087569145115198726dc
- `C:/kc3d/r/v4g01-closure-02/receipt.json` 0800150d9f0771548f7cfb0915d372508b2f4f6c6699290579414f5845eafce5

