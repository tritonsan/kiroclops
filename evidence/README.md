# Evidence (public subset)

This folder holds the parts of the frozen KiroClops evidence that back the numbers in the [technical report](../submission/TECHNICAL_REPORT.md) and are small, in English and free of personal data. `scripts/build_public_evidence.py` produced every file from the internal evidence roots. [`MANIFEST.json`](MANIFEST.json) gives, for each file, its source path, the source's SHA-256, this file's SHA-256 and the transformation applied:

- copied byte for byte;
- copied with a local user path or an AWS account number (in ARNs, queue URLs and bucket names) masked: the two AWS equivalence results and the public test log;
- extracted from a larger file.

Frames, recordings, closure receipts (up to 33 MB each) and the Kiro session workspaces are not included. They hold gigabytes of images and local machine paths, and can be shown on request.

| Path | What it shows | Report |
|---|---|---|
| `closures/game-v4g01-closure-02.summary.json` | Game domain closure: every acceptance axis passes | Summary, §6.2 |
| `closures/svg-v5s01c-closure-01.summary.json` | SVG domain closure: pass, with the historical candidate re-measured | Summary, §6.2 |
| `svg-focus-ring-blind-qualification.json` | Connector endpoints at focus rings against blind marks: 32/32 within 2 px, 32/32 attachment decisions agree | §3.1 |
| `opencv5-symbol-diff/` | Python symbol tables of OpenCV 4.14.0 and 5.0.0 and their difference | §3.2 |
| `lightglue-platforms/` | The same LightGlue failure on Windows x64, Linux x86-64 and Linux AArch64, with the 5.0.0 wheel, a 5.1.0-dev build and the current 5.x branch built from source: outputs, a gdb backtrace that places the division by zero in `GatherNDLayerImpl::forward_impl`, and the scripts. Reported upstream as opencv/opencv#30501 (division by zero) and opencv/opencv#30502 (Windows abort) | §3.2 |
| `lightglue-sweep/` | OpenCV 5.0.0 LightGlue: `main.json` reproduces the failure (cold sweep of 1,470 keypoint-count pairs, 89 C++ exceptions); `order-1.json` and `order-2.json` validate the warm-up and helper-process design (1,617 pairs in two random orders, identical results, 0 helper restarts) | §3.2 |
| `subpixel-benchmark/` | Sub-pixel refinement on synthetic shifts: integer peak, three-point parabola, symmetric upsampling | §3.3 |
| `marks-error/` | Tracker scatter against independent marks on real panel and SVG recordings | §3.3 |
| `feature-cross-check/` | ALIKED + LightGlue cross-check of the template tracker on panel and SVG calibration recordings | §3.3 |
| `v6-tracker-equivalence/` | V6 defaults reproduce the stored panel and SVG observations bit for bit | §3.3 |
| `agent-sessions/` | All 13 logged Kiro sessions, post-hoc: per session the candidates' calibration errors, the unchanged app on the same formula, the sealed held-out outcome and the recorded verdict, with every source file's SHA-256; every recorded score is reproduced | §4.2 |
| `v6-agent-baseline/result.json` | No-agent baseline, post-hoc: the unchanged app scores 0.0502 on calibration against 0.0244 and 0.0067 for Kiro's two candidates, and 0.0500 on the held-out cases against 0.0121 for the sealed candidate; the recorded scores are reproduced exactly | §4.2 |
| `panel-v6p01/heldout-outcome.json` | V6 panel run, sealed candidate 02: 55 of 56 held-out comparisons (regression 43/44, validation 12/12), with the failing drag-release windows | §6.2 |
| `aws/standard-equivalence/`, `aws/cool-equivalence/` | The same 17 recordings measured locally and on the Graviton4 worker, with the standard wheel and with COOL | §5.2 |
| `aws/speed/` | Speed and cost: COOL against the standard wheel and the local engine | §5.2 |
| `aws/speed-posthoc/result.json` | Bootstrap intervals for the speed test, post-hoc: paired COOL/standard ratio 1.003 (95% interval 0.991–1.010) | §5.2 |
| `aws/cool-scaling/` | Post-hoc scaling study: COOL and the standard wheel on c8g.xlarge and c8g.4xlarge, the standard wheel on x86 c8i.xlarge and c8a.xlarge; kernels by frame size (960 × 640, 1080p, 4K) and thread count, the measurement by thread count, throughput with one job per vCPU and cost per 1,000 analyses; every round (`REPORT.md`, `summary.json`, one folder per instance) | §5.2 |
| `aws/judge-dryrun/` | The judge path from a clean clone of the public repository: both checks against the live COOL service, and the local comparison | §5.3 |
| `aws/cool-kernels/` | Both engines on one Graviton4 instance, three alternating rounds: per-kernel times (`REPORT.md`, `summary.json`), the six raw runs, and an in-process profile of the measurement | §5.2 |
| `aws/wake/result.json` | Wake-up from a stopped worker to a finished job | §5.2 |
| `inspect-window-post-freeze/result.json` | The post-freeze `inspect_window` check over 102 calibration windows | §6.4 |
| `tests/` | Full-suite results on the engineering machine (`full-tests-02`, `full-tests-03`) and the full run of this public tree (`public-tree-tests.log`: 3,757 tests, 173 skipped, no failures) | §6.4 |
| `source-epochs.json` | Tree hashes of the frozen source epochs | §6.4, §8 |

The Agentic Vision trace of the V6 panel run, with the agent's own helper scripts and code changes, is in [`submission/evidence/agentic-trace-v6p01`](../submission/evidence/agentic-trace-v6p01).
