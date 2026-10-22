# KiroClops architecture

This page describes the system as it is at the V6 engineering epoch (October 2026). The engineering history is summarised in [`docs/history/README.md`](docs/history/README.md); [`README.md`](README.md) is the entry point.

## 1. Layers

```
┌───────────────────────────────────────────────────────────────────────────┐
│ scripts/            versioned evidence producers, closures, snapshots     │
├───────────────────────────────────────────────────────────────────────────┤
│ kiroclops/local_engine   session · jobs · suites · evaluation · feedback  │
│                          launcher (Kiro) · guard hook · native audit      │
│                          window inspection · closures · reporting         │
├─────────────────────────────┬─────────────────────────────────────────────┤
│ kiroclops/web_local         │ adapters/ (Godot)       │ kiroclops/cloud    │
│ CDP capture, visual clock,  │ fixed-rate game capture │ remote analysis    │
│ host quiet + priority       │                         │ client (optional)  │
├─────────────────────────────┴─────────────────────────────────────────────┤
│ kiroclops/visual   OpenCV 5 measurement core: package, tracking, events,   │
│                    comparison, polyline, pixel anchor, features5, opencv5 │
└───────────────────────────────────────────────────────────────────────────┘
```

`kiroclops/visual` imports nothing from the other packages. Everything above it treats its outputs (an `ObservationSet` per recording) as immutable evidence identified by hash.

## 2. Measurement core (`kiroclops/visual`)

* **Package** (`package.py`): PNG frames with real presentation timestamps, hashed into a manifest. Browser captures carry per-frame host/page clock brackets; videos keep ffprobe PTS.
* **Tracking** (`tracking.py`): per selected object, one of
  * fixed-template NCC (`cv2.matchTemplate`, `TM_CCOEFF_NORMED`) over a bank of mirror/quarter-turn variants with a colour-mean gate and a distinct-location margin gate;
  * Lucas-Kanade flow (`cv2.calcOpticalFlowPyrLK`, forward-backward check) with a RANSAC similarity (`cv2.estimateAffinePartial2D`);
  * contour geometry (`cv2.Canny`, `cv2.findContours`) for separately measured boundaries.
  Connections are straight segments (`cv2.fitLine` on connected components) or colour polylines. The V5 ring-aware rule removes a painted focus ring from the stroke mask and flags occluded endpoints. Invalid frames never carry coordinates.
  V6 additions: `opencv5.require_opencv5()` refuses a 4.x runtime and records the build (`opencv_runtime`); `anchor_convention` (`pixel_index` for explicit anchors, `continuous` for the default centre) keeps mirrored variants on the same physical point; `peak_refinement: upsampled_v1` scores symmetrically upsampled template and window for sub-pixel position (integer search origin unchanged).
* **Learned features** (`features5.py`): `cv2.ALIKED` keypoints and `cv2.LightGlueMatcher` (OpenCV 5 `features` module; absent in 4.x) locate a selected patch through a RANSAC similarity, reporting scale and rotation. Models are the files OpenCV's own test suite pins (`opencv_extra download_models.py`), fetched by `scripts/fetch_opencv5_models.py` and SHA-1 verified.
* **Events and comparison** (`events.py`, `comparison.py`): rest-band departure, confirmed rest, reversal, overshoot, oscillation, boundary and overlap relations, connector attachment (distance or occlusion flag); windowed RMSE on the union timeline with gap-limited interpolation; limits are fractions of a physical scale.

## 3. Capture

* **Web** (`web_local/capture.py`): headless Chromium via Playwright/CDP; trusted pointer input with receipts; `HeadlessExperimental.beginFrame` or screencast frames; each command has a 120 ms deadline. Timing summaries (median/p95 interval, motion gaps, clock uncertainty) gate the recording. V6: a one-second host scheduling sample before each capture (`host_quiet.py`, `machine_not_quiet`), above-normal priority for the capture process and its Chromium children (receipt `private/priority.json`), and scenario-level re-recording after a timing stall (`evaluation._capture_web_retrying`, `media_suite._capture`), keeping failed attempts as `<case>~failed-attemptN`.
* **Game** (`adapters/`, `local_engine/suite_game.py`): Godot capture scene at a fixed physics rate; input phase rules; no position or physics data leaves the game.

## 4. Agent loop (`kiroclops/local_engine`)

1. `launcher.configure` writes the Kiro agent, hook and prompt into the target, hashes every control file, and `session.bind_controls` pins them.
2. The Kiro CLI runs with eight MCP tools (`inspect_session`, `prepare_experiment`, `evaluate_candidate`, `inspect_window`, `finalize_candidate`, `get_job`, `cancel_job`, `build_report`). `evaluate_candidate` snapshots the target, verifies the editable scope and runs a detached worker that re-records every calibration case; `evaluate_candidate(case_ids=[...])` runs a partial probe (never finalizable, at most three). `inspect_window` renders reference/candidate/difference strips and an error-over-time plot into `.kiroclops-evidence/inspect`.
3. Feedback (`job_feedback.py`) stays under Kiro's 30k-character offload limit: per-window errors, limits, worse-than-before flags, candidate media root; full JSON stays in the workspace.
4. Budgets: three candidates, a frozen authoring wall clock, a separate validation clock after sealing. V6 refuses an evaluation the measured duration cannot fit (`budget_insufficient`).
5. The guard hook (pre-tool-use) allows reads inside the target, writes only to the motion code and scratch folder, denies session folders, parent traversal and unknown path keys, and records every decision. After the turn, `review.audit_native_run` replays Kiro's own logs against pinned envelope rules (certified for kiro-cli 2.27.x; later CLI versions are reported as uncertified).
6. Closures (`scripts/phase3d_*_closure.py`) combine measurement, behaviour, preserved functions, independent raw review, source review and native origin into one validated receipt bound to a source snapshot (`C:/kc3d/code/<epoch>`).

## 5. Evidence and integrity

* Preregistered thresholds and recipes; results are never rewritten; reruns get new identifiers.
* Source epochs: every accepted closure names the byte-identical source tree it was produced with; `artifacts/phase4-v6/epoch-start-01` proves the V6 epoch started from the V5b tree.
* `scripts/v6_equivalence.py` replays frozen recordings with the current tracker and reports any numeric difference against stored observations.

## 6. Known forks and debt

Earlier generations (`integration/`, `portable/`, `portable_v2/`, `suite_web` v1-v5, `suite_raw_review` v1-v3) are kept because recorded evidence binds them by hash. New work goes into versioned modules; nothing pinned is edited in place. In this public edition the comments and messages of those files are translated to English, so their bytes differ from the pinned originals; [`PUBLICATION.md`](PUBLICATION.md) lists every file that differs from the frozen source epoch.
